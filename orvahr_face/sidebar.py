"""Accessibility checks for the face entries in the Attendance sidebar (attendance/sidebar.py)."""


def check_in_accessibility(request, submenu, user_perms, *args, **kwargs):
    """Shown to employees of a company that enabled face check-in."""
    try:
        from orvahr_face.services import settings_for_employee

        employee = getattr(request.user, "employee_get", None)
        row = settings_for_employee(employee) if employee is not None else None
        return bool(row and row.enabled)
    except Exception:
        return False


def enrolments_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_face.view_facetemplate")
