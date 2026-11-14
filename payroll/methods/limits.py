"""
limits.py

The ceiling on a component's amount.
"""

from payroll.methods.proration import period_factor


def compute_limit(component, amount, day_dict):
    """
    Apply the component's maximum, scaled to the pay period.

    The maximum is quoted on a basis — a month's worth, an amount per working
    day, or a flat figure — and ``maximum_unit`` says which. Scaling it was
    written inline here and then commented out, so the field has been on the
    form all along offering "For working days on month" while the code applied
    the number flat. A ten-day period was capped at a whole month's ceiling.

    A full month still caps at exactly the figure configured, because the
    period's working days and the month's working days are then the same. Only
    partial and multi-month periods move, which is the point.
    """
    if not component.has_max_limit:
        return amount

    ceiling = component.maximum_amount
    if ceiling is None:
        return amount

    ceiling = float(ceiling) * period_factor(
        day_dict, getattr(component, "maximum_unit", None)
    )
    return min(amount, ceiling)


def clamp_base(component, figure):
    """
    The figure a percentage is taken of, held within the component's minimum
    and maximum base (Deduction.contribution_base_min / _max).

    Components without those fields -- every Allowance -- and ones that leave
    them empty get the figure unchanged. A figure of zero or less stays as it
    is: the floor is "contribute as if you earned at least this", which says
    nothing about a period in which nothing was paid at all.
    """
    figure = float(figure or 0)
    if figure <= 0:
        return figure
    floor = getattr(component, "contribution_base_min", None)
    ceiling = getattr(component, "contribution_base_max", None)
    if floor is not None:
        figure = max(figure, float(floor))
    if ceiling is not None:
        figure = min(figure, float(ceiling))
    return figure
