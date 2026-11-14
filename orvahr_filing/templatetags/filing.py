from django import template

from orvahr_filing.common import problem_label

register = template.Library()


@register.filter
def filing_problem(code):
    """Human label for a validation problem code."""
    return problem_label(code)


@register.filter
def rd_file_problem(code):
    from orvahr_filing.rd_file import problem_label as rd_label

    return rd_label(code)
