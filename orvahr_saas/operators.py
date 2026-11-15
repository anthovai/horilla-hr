"""Password policy for console operators (stricter than tenant users)."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

OPERATOR_MIN_LENGTH = 12


def operator_password_problems(password, operator):
    """List of reasons ``password`` is not acceptable for ``operator`` ([] = ok)."""
    problems = []
    try:
        validate_password(password, user=operator)
    except ValidationError as exc:
        problems.extend(exc.messages)
    if len(password) < OPERATOR_MIN_LENGTH:
        problems.append(
            _("Operator passwords need at least %(n)s characters.")
            % {"n": OPERATOR_MIN_LENGTH}
        )
    classes = sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )
    if classes < 3:
        problems.append(
            _(
                "Operator passwords need at least three of: lower-case, "
                "upper-case, digits, symbols."
            )
        )
    return problems
