"""
Create (or reset the password of) an operator console account.

    ORVAHR_CONSOLE_PASSWORD='...' python manage.py create_console_operator \\
        --username ops --email ops@orvahr.example

The password is read from ORVAHR_CONSOLE_PASSWORD (or a TTY prompt), never
from the command line, and must pass the operator password policy
(AUTH_PASSWORD_VALIDATORS + 12 characters + three character classes).
``--deactivate`` disables the account (ends its sessions).
"""

import getpass
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

PASSWORD_ENV = "ORVAHR_CONSOLE_PASSWORD"


class Command(BaseCommand):
    help = "Create or update an OrvaHR operator console account (SaaS mode)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--deactivate", action="store_true")

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("create_console_operator needs ORVAHR_MULTI_TENANT=1.")
        from django.db import connection

        from orvahr_saas.models import ConsoleOperator
        from orvahr_saas.operators import operator_password_problems
        from orvahr_saas.services import record_action

        connection.set_schema_to_public()
        username = options["username"].strip()
        operator = ConsoleOperator.objects.filter(username=username).first()
        if options["deactivate"]:
            if operator is None:
                raise CommandError(f"No operator {username!r}.")
            operator.is_active = False
            operator.set_password(os.urandom(16).hex())  # also ends sessions
            operator.save()
            record_action("operator_deactivated", actor_label="cli", username=username)
            self.stdout.write(self.style.SUCCESS(f"Operator {username!r} deactivated."))
            return

        password = os.environ.get(PASSWORD_ENV)
        if not password:
            if not os.isatty(0):
                raise CommandError(f"Set {PASSWORD_ENV} or run on a terminal.")
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords do not match.")
        created = operator is None
        if created:
            operator = ConsoleOperator(username=username)
        if options["email"]:
            operator.email = options["email"]
        problems = operator_password_problems(password, operator)
        if problems:
            raise CommandError("Password rejected: " + " ".join(problems))
        operator.set_password(password)
        operator.is_active = True
        operator.is_staff = True
        operator.save()
        record_action(
            "operator_created" if created else "operator_password_reset",
            actor_label="cli",
            username=username,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Operator {username!r} {'created' if created else 'updated'}."
            )
        )
