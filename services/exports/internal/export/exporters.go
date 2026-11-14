package export

import "fmt"

// ---------------------------------------------------------------- generic bank

// GenericBankCSV is a GENERIC transfer list. It is NOT any bank's official
// payroll/bulk-transfer format and will not be accepted by a bank portal as
// is. Lines with a zero net amount are omitted.
type GenericBankCSV struct{}

func (GenericBankCSV) ID() string { return "generic_bank_csv" }
func (GenericBankCSV) Name() string {
	return "Generic bank transfer list (CSV) - NOT an official bank format"
}

func (GenericBankCSV) Generate(b PayBatch) ([]byte, string, error) {
	if err := b.Validate(); err != nil {
		return nil, "", err
	}
	var p []string
	rows := [][]string{
		{"# GENERIC TRANSFER LIST - NOT ANY BANK'S OFFICIAL FORMAT - for reference/manual entry only"},
		{"employee_code", "full_name", "bank_code", "account_no", "amount_thb", "pay_date", "reference"},
	}
	var total Satang
	for i, l := range b.Lines {
		if l.Net == 0 {
			continue
		}
		if l.BankCode == "" || l.AccountNo == "" {
			p = append(p, fmt.Sprintf("lines[%d]: bank_code and account_no are required for a non-zero net", i))
			continue
		}
		total += l.Net
		rows = append(rows, []string{
			SafeText(l.EmployeeCode), SafeText(l.FullName), SafeText(l.BankCode), SafeText(l.AccountNo),
			FormatSatang(l.Net), b.PayDate, "SALARY " + b.Period,
		})
	}
	if len(p) > 0 {
		return nil, "", &ValidationError{Problems: p}
	}
	rows = append(rows, []string{"TOTAL", "", "", "", FormatSatang(total), "", ""})
	data, err := writeCSV(rows)
	return data, "generic_bank_transfer_" + b.Period + ".csv", err
}

// ---------------------------------------------------------------- PND.1 summary

// PND1SummaryCSV is an accountant-readable withholding summary. It is NOT the
// Revenue Department's PND.1 / PND.1 Kor e-filing format.
type PND1SummaryCSV struct{}

func (PND1SummaryCSV) ID() string { return "pnd1_summary_csv" }
func (PND1SummaryCSV) Name() string {
	return "PND.1 withholding summary (CSV) - NOT the official e-filing format"
}

func (PND1SummaryCSV) Generate(b PayBatch) ([]byte, string, error) {
	if err := b.Validate(); err != nil {
		return nil, "", err
	}
	rows := [][]string{
		{"# SUMMARY FOR ACCOUNTANTS - NOT THE REVENUE DEPARTMENT PND.1 E-FILING FORMAT"},
		{"employer", SafeText(b.Employer.Name), "employer_tax_id", b.Employer.TaxID, "period", b.Period},
		{"tax_id", "national_id", "full_name", "gross_income_thb", "income_tax_withheld_thb"},
	}
	for _, l := range b.Lines {
		rows = append(rows, []string{
			l.EffectiveTaxID(), l.NationalID, SafeText(l.FullName),
			FormatSatang(l.Gross), FormatSatang(l.IncomeTaxWithheld),
		})
	}
	t := b.Sum()
	rows = append(rows, []string{"TOTAL", "", fmt.Sprintf("%d employees", len(b.Lines)),
		FormatSatang(t.Gross), FormatSatang(t.IncomeTaxWithheld)})
	data, err := writeCSV(rows)
	return data, "pnd1_summary_" + b.Period + ".csv", err
}

// ---------------------------------------------------------------- SSO summary

// SSOMonthlyCSV is an accountant-readable SSO contribution summary. It is NOT
// the Social Security Office's form 1-10 / e-filing format.
type SSOMonthlyCSV struct{}

func (SSOMonthlyCSV) ID() string { return "sso_monthly_csv" }
func (SSOMonthlyCSV) Name() string {
	return "SSO monthly contribution summary (CSV) - NOT the official 1-10 / e-filing format"
}

func (SSOMonthlyCSV) Generate(b PayBatch) ([]byte, string, error) {
	if err := b.Validate(); err != nil {
		return nil, "", err
	}
	var p []string
	rows := [][]string{
		{"# SUMMARY FOR ACCOUNTANTS - NOT THE SSO FORM 1-10 / E-FILING FORMAT"},
		{"employer", SafeText(b.Employer.Name), "sso_employer_no", SafeText(b.Employer.SSOEmployerNo), "period", b.Period},
		{"sso_no", "national_id", "full_name", "sso_wage_thb", "employee_contribution_thb", "employer_contribution_thb", "total_thb"},
	}
	var wage, ee, er Satang
	for i, l := range b.Lines {
		if l.SSOEmployee == 0 && l.SSOEmployer == 0 {
			continue // not covered / no contribution this month
		}
		if l.SSONo == "" {
			p = append(p, fmt.Sprintf("lines[%d]: sso_no is required when SSO contributions are non-zero", i))
		}
		if l.SSOWage <= 0 {
			p = append(p, fmt.Sprintf("lines[%d]: sso_wage is required when SSO contributions are non-zero", i))
		}
		wage += l.SSOWage
		ee += l.SSOEmployee
		er += l.SSOEmployer
		rows = append(rows, []string{
			SafeText(l.SSONo), l.NationalID, SafeText(l.FullName), FormatSatang(l.SSOWage),
			FormatSatang(l.SSOEmployee), FormatSatang(l.SSOEmployer), FormatSatang(l.SSOEmployee + l.SSOEmployer),
		})
	}
	if len(p) > 0 {
		return nil, "", &ValidationError{Problems: p}
	}
	rows = append(rows, []string{"TOTAL", "", "", FormatSatang(wage), FormatSatang(ee), FormatSatang(er), FormatSatang(ee + er)})
	data, err := writeCSV(rows)
	return data, "sso_monthly_summary_" + b.Period + ".csv", err
}
