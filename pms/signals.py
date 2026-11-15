"""
pms/signals.py
"""

import copy
import logging
import threading
import types
from datetime import date

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from employee.methods.methods import check_relationship_with_employee_model
from horilla.horilla_middlewares import _thread_locals
from horilla.signals import pre_bulk_update
from pms.models import BonusPointSetting

logger = logging.getLogger(__name__)


from horilla.tenancy import (
    app_has_tables,
    current_schema,
    is_multi_tenant,
    schema_matches,
)

SIGNAL_HANDLERS = []
INSTANCE_HANDLERS = []

# SaaS mode only: each tenant's bonus-point automations live in a per-schema
# registry (horilla.tenant_config.TenantSignalRegistry), loaded lazily inside
# that tenant's schema, instead of being connected process-wide.
_TENANT_REGISTRY = None
_TENANT_LOADER = {}


def tenant_registry():
    """The SaaS-mode registry (None in single-tenant mode)."""
    global _TENANT_REGISTRY
    if not is_multi_tenant():
        return None
    if _TENANT_REGISTRY is None:
        from horilla.tenant_config import TenantSignalRegistry

        _TENANT_REGISTRY = TenantSignalRegistry(
            "pms-bonus-automations",
            lambda: _TENANT_LOADER["load"](),
            [pre_save, post_save],
        )
    return _TENANT_REGISTRY


def start_automation():
    """
    Automation signals
    """
    from horilla_automations.methods.methods import get_model_class, split_query_string

    registry = tenant_registry()

    def _connect(signal, handler, sender):
        """signal.connect(handler, sender=sender), or the tenant registry."""
        if registry is not None:
            registry.connect(signal, handler, sender)
        else:
            signal.connect(handler, sender=sender)

    if registry is not None:

        def _invalidate(sender, instance, **kwargs):
            registry.invalidate()

        for signal in (post_save, post_delete):
            signal.connect(
                _invalidate,
                sender=BonusPointSetting,
                weak=False,
                dispatch_uid="orvahr-pms-bonus-automation-reload",
            )

    @receiver(post_delete, sender=BonusPointSetting)
    @receiver(post_save, sender=BonusPointSetting)
    def automation_pre_create(sender, instance, **kwargs):
        """
        signal method to handle automation post save
        """
        if registry is not None:
            return  # SaaS: _invalidate above handles it
        start_connection()
        track_previous_instance()

    def clear_connection():
        """
        Method to clear signals handlers
        """
        for handler in SIGNAL_HANDLERS:
            post_save.disconnect(handler, sender=handler.model_class)
        SIGNAL_HANDLERS.clear()

    def start_connection():
        """
        Method to start signal connection accordingly to the automation
        """
        clear_connection()
        bonus_point_settings = BonusPointSetting.objects.filter(is_active=True)
        for bonus_point_setting in bonus_point_settings:
            model_path = bonus_point_setting.model
            model_class = get_model_class(model_path)
            related_fields = check_relationship_with_employee_model(model_class)
            field = None
            type = None
            for field_name, relation_type in related_fields:
                if (
                    bonus_point_setting.applicable_for == "members"
                    and relation_type == "ManyToManyField"
                ):
                    field = field_name
                    type = relation_type
                    break
                elif (
                    bonus_point_setting.applicable_for == "managers"
                    or bonus_point_setting.applicable_for == "owner"
                    and relation_type == "ForeignKey"
                ):
                    field = field_name
                    type = relation_type
                    break

            def create_signal_handler(
                name, bonus_point_setting, model_class, type=None, field=None
            ):
                # SaaS mode: the setting belongs to the schema it was read from.
                handler_schema = current_schema()

                def signal_handler(sender, instance, *args, **kwargs):
                    """
                    Signal handler for post-save events of the model instances.
                    """
                    if not schema_matches(handler_schema):
                        return
                    if type == "ManyToManyField":

                        @receiver(
                            m2m_changed, sender=getattr(model_class, field).through
                        )
                        def members_changed(sender, instance, action, **kwargs):
                            """
                            Handle m2m_changed signal for the members field in YourModel.
                            """
                            if (
                                action == "post_add"
                                or action == "post_remove"
                                or action == "post_clear"
                                or action == "post_save"
                            ):
                                # These actions occur after members are added, removed, or cleared
                                field_1 = date.today()
                                field_2 = instance.end_date
                                if bonus_point_setting.bonus_for == instance.status:
                                    if field and type:
                                        field_value = getattr(instance, field)
                                        if type == "ManyToManyField":
                                            # Now this should give the updated members
                                            employees = field_value.all()
                                        else:
                                            employees = field_value
                                        for employee in employees:
                                            bonus_point_setting.create_employee_bonus(
                                                employee, field_1, field_2, instance
                                            )
                                    else:
                                        logger.debug("No type and field")
                            else:
                                logger.debug("Not post add.")

                    else:
                        field_1 = date.today()
                        field_2 = instance.end_date
                        if bonus_point_setting.bonus_for == instance.status:
                            if field and type:
                                field_value = getattr(instance, field)
                                if type == "ManyToManyField":
                                    # Now this should give the updated members
                                    employees = field_value.all()
                                    for employee in employees:
                                        bonus_point_setting.create_employee_bonus(
                                            employee, field_1, field_2, instance
                                        )
                                else:
                                    employee = field_value
                                    bonus_point_setting.create_employee_bonus(
                                        employee, field_1, field_2, instance
                                    )
                            else:
                                logger.debug("No type and field")

                signal_handler.__name__ = name
                signal_handler.model_class = model_class
                signal_handler.type = type
                signal_handler.field = field
                signal_handler.bonus_point_setting = bonus_point_setting
                return signal_handler

            # Create and connect the signal handler
            handler_name = f"{bonus_point_setting.id}_signal_handler"
            # model_class is passed in rather than read from the enclosing
            # loop: the handler outlives the iteration, so a closure over the
            # loop variable would make every handler use the last model.
            dynamic_signal_handler = create_signal_handler(
                handler_name, bonus_point_setting, model_class, type=type, field=field
            )
            SIGNAL_HANDLERS.append(dynamic_signal_handler)
            _connect(
                post_save, dynamic_signal_handler, dynamic_signal_handler.model_class
            )

    def track_previous_instance():
        """
        method to add signal to track the automations model previous instances
        """

        def clear_instance_signal_connection():
            """
            Method to clear instance handler signals
            """
            for handler in INSTANCE_HANDLERS:
                pre_save.disconnect(handler, sender=handler.model_class)
                pre_bulk_update.disconnect(handler, sender=handler.model_class)
            INSTANCE_HANDLERS.clear()

        clear_instance_signal_connection()
        bonus_point_settings = BonusPointSetting.objects.filter(is_active=True)
        for bonus_setting in bonus_point_settings:
            model_class = get_model_class(bonus_setting.model)

            # _model/_setting are bound as defaults rather than read from the
            # enclosing loop: the handler stays connected after the loop ends,
            # so a closure would make every handler use the last iteration's
            # values. Django only ever passes sender/instance/**kwargs.
            def instance_handler(
                sender,
                instance,
                _model=model_class,
                _setting=bonus_setting,
                _schema=current_schema(),
                **kwargs,
            ):
                """
                Signal handler for pres-save events of the model instances.
                """
                if not schema_matches(_schema):
                    return
                # prevented storing the scheduled activities
                request = getattr(_thread_locals, "request", None)
                if instance.pk:
                    # to get the previous instance
                    instance = _model.objects.filter(id=instance.pk).first()
                if request:
                    _thread_locals.previous_record = {
                        "bonus_setting": _setting,
                        "instance": instance,
                    }

            instance_handler.__name__ = f"{bonus_setting.id}_instance_handler"
            instance_handler.model_class = model_class
            instance_handler.bonus_setting = bonus_setting
            _connect(pre_save, instance_handler, model_class)

            INSTANCE_HANDLERS.append(instance_handler)

    if registry is not None:
        # Loaded per tenant, in its schema, on first use there.
        def load_for_active_tenant():
            track_previous_instance()
            start_connection()

        _TENANT_LOADER["load"] = load_for_active_tenant
        return

    # SaaS mode: at startup the connection is on the public schema, which has
    # no pms tables; the handlers load when a tenant saves a setting.
    if not app_has_tables("pms"):
        return
    track_previous_instance()
    start_connection()
