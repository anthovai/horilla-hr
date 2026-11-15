"""
Database-level immutability of issued documents and recorded payments.

The models refuse changes in save()/delete(); these triggers also stop
``QuerySet.update()``, bulk deletes and raw SQL. An issued document may
only change its bookkeeping columns (keep in sync with
``models.MUTABLE_AFTER_ISSUE``).
"""

from django.db import migrations

MUTABLE = "ARRAY['payment_status','settled_amount','credited_amount','overdue_since','emailed_at','updated_at']"

SQL = f"""
CREATE OR REPLACE FUNCTION orvahr_billing_document_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'issued' THEN
            RAISE EXCEPTION 'orvahr_billing: issued document % cannot be deleted', OLD.number;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'issued'
       AND (to_jsonb(NEW) - {MUTABLE}) IS DISTINCT FROM (to_jsonb(OLD) - {MUTABLE}) THEN
        RAISE EXCEPTION 'orvahr_billing: issued document % is immutable; issue a credit note', OLD.number;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orvahr_billing_document_guard
BEFORE UPDATE OR DELETE ON orvahr_billing_billingdocument
FOR EACH ROW EXECUTE FUNCTION orvahr_billing_document_guard();

CREATE OR REPLACE FUNCTION orvahr_billing_line_guard() RETURNS trigger AS $$
DECLARE
    doc_id bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        doc_id := NEW.document_id;
    ELSE
        doc_id := OLD.document_id;
    END IF;
    IF EXISTS (SELECT 1 FROM orvahr_billing_billingdocument d
               WHERE d.id = doc_id AND d.status = 'issued') THEN
        RAISE EXCEPTION 'orvahr_billing: lines of an issued document are immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.document_id <> OLD.document_id
       AND EXISTS (SELECT 1 FROM orvahr_billing_billingdocument d
                   WHERE d.id = NEW.document_id AND d.status = 'issued') THEN
        RAISE EXCEPTION 'orvahr_billing: lines of an issued document are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orvahr_billing_line_guard
BEFORE INSERT OR UPDATE OR DELETE ON orvahr_billing_billingline
FOR EACH ROW EXECUTE FUNCTION orvahr_billing_line_guard();

CREATE OR REPLACE FUNCTION orvahr_billing_payment_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'orvahr_billing: a recorded payment cannot be deleted';
    END IF;
    -- The only change allowed: linking the receipt issued for it, once.
    IF (to_jsonb(NEW) - 'receipt_id') IS DISTINCT FROM (to_jsonb(OLD) - 'receipt_id')
       OR (OLD.receipt_id IS NOT NULL AND NEW.receipt_id IS DISTINCT FROM OLD.receipt_id) THEN
        RAISE EXCEPTION 'orvahr_billing: a recorded payment cannot be changed';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orvahr_billing_payment_guard
BEFORE UPDATE OR DELETE ON orvahr_billing_payment
FOR EACH ROW EXECUTE FUNCTION orvahr_billing_payment_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS orvahr_billing_payment_guard ON orvahr_billing_payment;
DROP FUNCTION IF EXISTS orvahr_billing_payment_guard();
DROP TRIGGER IF EXISTS orvahr_billing_line_guard ON orvahr_billing_billingline;
DROP FUNCTION IF EXISTS orvahr_billing_line_guard();
DROP TRIGGER IF EXISTS orvahr_billing_document_guard ON orvahr_billing_billingdocument;
DROP FUNCTION IF EXISTS orvahr_billing_document_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("orvahr_billing", "0001_initial")]

    operations = [migrations.RunSQL(SQL, REVERSE)]
