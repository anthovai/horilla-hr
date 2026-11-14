# OrvaHR exports service

Go, stdlib only. Generates payroll export files from a JSON `PayBatch`.
Money is integer satang (same convention as `services/payroll-engine`); no floats.

## Run

    EXPORTS_BIND=127.0.0.1:8091 go run .      # default bind shown
    go vet ./... && go test ./...

| Method | Path | |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/v1/exporters` | list `{id, name}` |
| POST | `/v1/export/{id}` | JSON `PayBatch` in, file download out (`Content-Disposition`). 404 unknown exporter, 400 bad JSON, 422 `{"error","problems":[...]}` on validation failure |

Validation: 13-digit national/tax IDs with checksum, non-negative amounts,
`net == gross - income_tax_withheld - sso_employee - other_deductions` per line,
optional `totals` must equal the sum of lines. Text fields starting with
`= + - @` (or tab/CR) are prefixed with `'` in CSV output (formula injection).
CSVs are UTF-8 with BOM, CRLF line endings.

## Built-in exporters: NOT official formats

| id | What it is |
|---|---|
| `generic_bank_csv` | Generic transfer list for reference/manual entry. **Not any bank's format.** |
| `pnd1_summary_csv` | Withholding summary for accountants. **Not the Revenue Department PND.1 / PND.1 Kor e-filing format.** |
| `sso_monthly_csv` | SSO contribution summary for accountants. **Not SSO form 1-10 / e-filing.** |

Each file carries a disclaimer in its first row. Do not upload them to an
agency or bank. `sso_wage` (post-cap wage base) must be supplied by the caller;
this service does not know statutory caps or rates.

## TODO: official formats (specs needed, nothing invented here)

- SSO monthly contribution (form 1-10) e-filing file: current file layout
  specification from the Social Security Office (field widths, encoding,
  header/trailer records, branch codes, wage-bracket rules).
- PND.1 and PND.1 Kor e-filing: Revenue Department's current upload/RD Prep
  file specification and code tables (income type codes, title/prefix codes,
  conditions-of-withholding codes).
- KBank, SCB, BBL, KTB (and others) payroll / bulk-transfer files: each bank's
  corporate-channel file spec, plus the company's agreement/service codes,
  header/trailer/checksum rules, and sample files for acceptance testing.
- Likely also: Thai-to-bank-code table, and any required Thai-vs-English name
  handling, per bank.

## Adding a real exporter

1. Implement `export.Exporter` (`ID`, `Name`, `Generate`) in a new file under
   `internal/export/`. Call `batch.Validate()` first, then check extra
   requirements and return `*ValidationError` for bad input (maps to HTTP 422).
2. Use `FormatSatang` for amounts and `SafeText` for any free text.
3. Add it to `DefaultRegistry()` in `registry.go`.
4. Test against the agency/bank's own sample files or validator, not just
   against this repo's tests.
