"""
Gap-free document numbers per type and Buddhist year: INV2569-000001,
RC2569-000001, CN2569-000001.

Thai tax invoices must carry a serial number (section 86/4 (4)), and the
Revenue Department expects the series to run without gaps. A PostgreSQL
SEQUENCE would not do: it never rolls back, so a failed issue leaves a hole.
Instead the counter is a row in ``DocumentSequence``, locked with
``SELECT ... FOR UPDATE`` and incremented inside the *same* transaction that
stores the issued document. A rollback undoes both; a concurrent issuer
waits on the row lock until the first one commits.
"""

from django.db import IntegrityError, connection, transaction

from orvahr_billing.models import DOC_PREFIX, DocumentSequence
from orvahr_th.formatting import be_year


def format_number(doc_type, year_be, number):
    return f"{DOC_PREFIX[doc_type]}{year_be}-{number:06d}"


def next_number(doc_type, on_date):
    """
    Reserve the next number for ``doc_type`` in the Buddhist year of
    ``on_date``. Must run inside ``transaction.atomic()`` together with the
    write that uses the number.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("next_number() must be called inside transaction.atomic()")
    year_be = be_year(on_date)
    # Make sure the row exists without racing: a concurrent creator makes
    # this INSERT fail, which the savepoint absorbs, and we then lock theirs.
    if not DocumentSequence.objects.filter(doc_type=doc_type, be_year=year_be).exists():
        try:
            with transaction.atomic():
                DocumentSequence.objects.create(
                    doc_type=doc_type, be_year=year_be, last_number=0
                )
        except IntegrityError:
            pass
    seq = DocumentSequence.objects.select_for_update().get(
        doc_type=doc_type, be_year=year_be
    )
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    return format_number(doc_type, year_be, seq.last_number)
