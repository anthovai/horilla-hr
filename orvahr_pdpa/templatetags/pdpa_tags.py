from django import template
from django.utils import timezone
from django.utils.translation import gettext as _

register = template.Library()


def format_span(seconds):
    seconds = abs(int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return _("%(d)sd %(h)sh") % {"d": days, "h": hours}
    return _("%(h)sh %(m)sm") % {"h": hours, "m": minutes}


@register.filter
def countdown(deadline):
    """'3d 4h left' / 'overdue by 2h 5m' relative to now."""
    if not deadline:
        return ""
    seconds = (deadline - timezone.now()).total_seconds()
    if seconds >= 0:
        return _("%(span)s left") % {"span": format_span(seconds)}
    return _("overdue by %(span)s") % {"span": format_span(seconds)}


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None
