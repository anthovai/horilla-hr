"""Gap-free numbering under real concurrency: threads with their own connections."""

import datetime
import threading

from django.db import connection, connections, transaction
from django.test import TransactionTestCase

from orvahr_billing.models import DocType, DocumentSequence
from orvahr_billing.numbering import next_number

DAY = datetime.date(2099, 6, 1)  # BE 2642: a year no other test touches
BE = 2642


class ConcurrentNumberingTests(TransactionTestCase):
    """
    Commits for real (other connections must see the rows). The default
    TransactionTestCase teardown would TRUNCATE every table -- including the
    tenant registry -- so only this test's sequence rows are removed instead.
    """

    databases = {"default"}

    def setUp(self):
        connection.set_schema_to_public()
        DocumentSequence.objects.filter(be_year=BE).delete()

    def _fixture_teardown(self):
        connection.set_schema_to_public()
        DocumentSequence.objects.filter(be_year=BE).delete()

    def _run(self, workers, per_worker, fail_every=0):
        committed, errors = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(workers)

        def work(index):
            try:
                connection.set_schema_to_public()
                barrier.wait()
                for i in range(per_worker):
                    try:
                        with transaction.atomic():
                            number = next_number(DocType.CREDIT_NOTE, DAY)
                            if fail_every and (index + i) % fail_every == 0:
                                raise RuntimeError("rollback")
                        with lock:
                            committed.append(number)
                    except RuntimeError:
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        return committed

    def test_parallel_issuers_get_unique_consecutive_numbers(self):
        committed = self._run(workers=12, per_worker=8)
        self.assertEqual(len(committed), 96)
        self.assertEqual(sorted(committed), [f"CN{BE}-{n:06d}" for n in range(1, 97)])

    def test_rolled_back_numbers_are_reused_so_no_gaps(self):
        committed = self._run(workers=8, per_worker=6, fail_every=3)
        self.assertLess(len(committed), 48)
        self.assertEqual(
            sorted(committed), [f"CN{BE}-{n:06d}" for n in range(1, len(committed) + 1)]
        )
        self.assertEqual(
            DocumentSequence.objects.get(
                doc_type=DocType.CREDIT_NOTE, be_year=BE
            ).last_number,
            len(committed),
        )

    def test_requires_a_transaction(self):
        with self.assertRaises(RuntimeError):
            next_number(DocType.INVOICE, DAY)
