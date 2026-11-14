"""Admin for orvahr_benefits. Deduction order references are never listed."""

from django.contrib import admin

from orvahr_benefits.models import (
    AnnualLeaveType,
    DeductionOrder,
    EmployeeProvidentFund,
    FinalSettlement,
    ProvidentFundPlan,
    PVDDeductionLink,
    PVDVestingStep,
    SeveranceRule,
)


class PVDVestingStepInline(admin.TabularInline):
    model = PVDVestingStep
    extra = 1


@admin.register(ProvidentFundPlan)
class ProvidentFundPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "fund_manager_name", "company_id", "default_employer_rate", "is_active")
    inlines = [PVDVestingStepInline]


@admin.register(EmployeeProvidentFund)
class EmployeeProvidentFundAdmin(admin.ModelAdmin):
    list_display = ("employee", "plan", "employee_rate", "employer_rate", "joined_on", "left_on")
    list_filter = ("plan",)
    raw_id_fields = ("employee",)


@admin.register(PVDDeductionLink)
class PVDDeductionLinkAdmin(admin.ModelAdmin):
    list_display = ("plan", "employee_rate", "employer_rate", "deduction")
    readonly_fields = ("plan", "employee_rate", "employer_rate", "deduction")


@admin.register(SeveranceRule)
class SeveranceRuleAdmin(admin.ModelAdmin):
    list_display = ("effective_from", "min_days", "max_days", "days_of_wages", "is_active")
    list_filter = ("effective_from", "is_active")


@admin.register(AnnualLeaveType)
class AnnualLeaveTypeAdmin(admin.ModelAdmin):
    list_display = ("leave_type",)


@admin.register(FinalSettlement)
class FinalSettlementAdmin(admin.ModelAdmin):
    list_display = ("employee", "last_working_day", "reason", "status", "total_payable")
    list_filter = ("status", "reason")
    raw_id_fields = ("employee",)
    readonly_fields = ("status", "approved_by", "approved_at", "paid_on")


@admin.register(DeductionOrder)
class DeductionOrderAdmin(admin.ModelAdmin):
    # reference_number deliberately not in list_display / search_fields.
    list_display = ("employee", "order_type", "masked_reference", "issuer", "status", "priority")
    list_filter = ("order_type", "status")
    raw_id_fields = ("employee",)
    readonly_fields = ("deduction",)
