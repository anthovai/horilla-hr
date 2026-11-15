"""
accessibility/signals.py
"""

import threading

from django.core.cache import cache
from django.db.models.signals import post_save
from django.dispatch import receiver

from accessibility.middlewares import ACCESSIBILITY_CACHE_USER_KEYS, user_cache_keys
from accessibility.models import DefaultAccessibility
from employee.models import EmployeeWorkInformation
from horilla.signals import post_bulk_update
from horilla.tenancy import TenantAwareThread


def _clear_accessibility_cache():
    # Keys of every tenant are listed; deleting another tenant's session key
    # is a no-op here because the cache prefixes keys with the active schema.
    for _user_id, cache_keys in ACCESSIBILITY_CACHE_USER_KEYS.copy().items():
        for key in cache_keys:
            cache.delete(key)


def _clear_bulk_employees_cache(queryset):
    for instance in queryset:
        if instance.employee_id and instance.employee_id.employee_user_id:
            for key in user_cache_keys(instance.employee_id.employee_user_id.id):
                cache.delete(key)


@receiver(post_save, sender=EmployeeWorkInformation)
def monitor_employee_update(sender, instance, created, **kwargs):
    """
    This method tracks updates to an employee's work information instance.
    """

    _sender = sender
    _created = created

    if instance.employee_id and instance.employee_id.employee_user_id:
        user_id = instance.employee_id.employee_user_id.id
        for key in user_cache_keys(user_id):
            cache.delete(key)


@receiver(post_save, sender=DefaultAccessibility)
def monitor_accessibility_update(sender, instance, created, **kwargs):
    """
    This method is used to track accessibility updates
    """
    _sender = sender
    _created = created
    _instance = instance
    thread = TenantAwareThread(target=_clear_accessibility_cache)
    thread.start()


@receiver(post_bulk_update, sender=EmployeeWorkInformation)
def monitor_employee_bulk_update(sender, queryset, *args, **kwargs):
    """
    This method is used to track accessibility updates
    """
    _sender = sender
    _queryset = queryset
    thread = TenantAwareThread(target=_clear_bulk_employees_cache(queryset))
    thread.start()
