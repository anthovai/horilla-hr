"""
Employee ceiling under concurrency: two creations racing for the last seat,
each on its own thread and therefore its own database connection, with real
commits (the test transaction of the main connection is not shared).

Counting is slowed down so both threads would count before either inserts;
without ``employee_limit_guard`` both would succeed (proved by the second
test), with it the second waits for the first to commit and is refused.
"""

import threading
import time
from contextlib import nullcontext
from unittest import mock

from django.db import close_old_connections, connection
from django.db.models import ProtectedError

from orvahr_saas.tests.base import SaasTestCase

PASSWORD = "Very-L0ng-Pass!"
PREFIX = "race-limit-"


def _in_thread(func):
    """Run ``func`` on a fresh thread/connection; return its result or raise."""
    box = {}

    def run():
        try:
            box["result"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(60)
    if "error" in box:
        raise box["error"]
    return box.get("result")


class EmployeeLimitRaceTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        from orvahr_saas.models import Tenant

        connection.set_schema_to_public()
        # A detached copy with the ceiling set in memory only: the threads
        # activate it, so nothing in the shared tenant row is committed.
        self.live = Tenant.objects.get(pk=self.alpha.pk)
        connection.set_tenant(self.tenant)
        self._cleanup()  # leftovers of an interrupted run
        self.addCleanup(self._cleanup)

    def _committed_active_count(self):
        def count():
            from orvahr_saas.limits import active_employee_count

            connection.set_tenant(self.live)
            return active_employee_count()

        return _in_thread(count)

    def _cleanup(self):
        def delete():
            from employee.models import Employee
            from horilla_auth.models import HorillaUser

            connection.set_tenant(self.live)
            for _attempt in range(5):
                try:
                    Employee.objects.entire().filter(email__startswith=PREFIX).delete()
                    break
                except ProtectedError as exc:  # e.g. the auto-created contract
                    for obj in exc.protected_objects:
                        type(obj).objects.filter(pk=obj.pk).delete()
            HorillaUser.objects.filter(username__startswith=PREFIX).delete()

        _in_thread(delete)
        connection.set_tenant(self.tenant)

    def _race(self, guard=None):
        """Two threads add one employee each with one seat left."""
        from orvahr_saas import limits

        base = self._committed_active_count()
        self.live.max_employees = base + 1
        barrier = threading.Barrier(2, timeout=30)
        results = {}
        real_count = limits.active_employee_count

        def slow_count():
            value = real_count()
            time.sleep(0.6)  # both would have counted before either inserts
            return value

        def worker(i):
            from employee.models import Employee
            from horilla_auth.models import HorillaUser

            close_old_connections()
            try:
                connection.set_tenant(self.live)
                email = f"{PREFIX}{i}@race.test"
                user = HorillaUser.objects.create_user(
                    username=email, password=PASSWORD
                )
                barrier.wait()
                Employee(
                    employee_user_id=user,
                    employee_first_name=f"Race{i}",
                    email=email,
                    phone="0800000000",
                ).save()
                results[i] = "created"
            except Exception as exc:  # noqa: BLE001
                results[i] = "limit" if limits.is_limit_error(exc) else repr(exc)
            finally:
                connection.close()

        patches = [mock.patch.object(limits, "active_employee_count", slow_count)]
        if guard is not None:
            patches.append(mock.patch.object(limits, "employee_limit_guard", guard))
        for patch in patches:
            patch.start()
        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            for patch in patches:
                patch.stop()
        return base, results

    def test_concurrent_creations_cannot_exceed_the_limit(self):
        base, results = self._race()
        self.assertEqual(sorted(results.values()), ["created", "limit"], results)
        self.assertEqual(self._committed_active_count(), base + 1)

    def test_without_the_guard_the_race_overshoots(self):
        """Sanity check of the harness: the race is real without the lock."""
        base, results = self._race(guard=lambda: nullcontext())
        self.assertEqual(sorted(results.values()), ["created", "created"], results)
        self.assertEqual(self._committed_active_count(), base + 2)

    def test_bulk_import_waits_for_the_lock_too(self):
        """A bulk import that starts while a single creation holds the lock
        counts that employee."""
        from orvahr_saas import limits

        base = self._committed_active_count()
        self.live.max_employees = base + 1
        holding = threading.Event()
        release = threading.Event()
        outcome = {}

        def holder():
            from employee.models import Employee
            from horilla_auth.models import HorillaUser

            connection.set_tenant(self.live)
            try:
                user = HorillaUser.objects.create_user(
                    username=f"{PREFIX}hold@race.test", password=PASSWORD
                )
                with limits.employee_limit_guard():
                    Employee(
                        employee_user_id=user,
                        employee_first_name="Hold",
                        email=f"{PREFIX}hold@race.test",
                        phone="0800000000",
                    ).save()
                    holding.set()
                    release.wait(30)
            finally:
                connection.close()

        def importer():
            from employee.methods.methods import (
                bulk_create_employee_import,
                bulk_create_user_import,
            )

            connection.set_tenant(self.live)
            try:
                holding.wait(30)
                row = {
                    "Email": f"{PREFIX}bulk@race.test",
                    "Phone": "0800000000",
                    "First Name": "Bulk",
                }
                bulk_create_user_import([row])
                timer = threading.Timer(0.5, release.set)
                timer.start()
                started = time.monotonic()
                try:
                    bulk_create_employee_import([row])
                    outcome["result"] = "created"
                except Exception as exc:  # noqa: BLE001
                    outcome["result"] = (
                        "limit" if limits.is_limit_error(exc) else repr(exc)
                    )
                outcome["waited"] = time.monotonic() - started
            finally:
                connection.close()

        threads = [threading.Thread(target=holder), threading.Thread(target=importer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(outcome.get("result"), "limit", outcome)
        self.assertGreaterEqual(outcome["waited"], 0.4)
        self.assertEqual(self._committed_active_count(), base + 1)
