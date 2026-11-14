// Package export generates payroll export files for Thai statutory and bank
// submissions.
//
// Conventions (shared with services/payroll-engine): money is integer satang
// (1 baht = 100 satang); no floating point ever touches money.
//
// IMPORTANT: none of the exporters here produce an official Revenue
// Department, Social Security Office or bank file format. See README.md.
package export

// Satang is an amount of money in satang (1 baht = 100 satang).
type Satang = int64

// Exporter is the plug-in interface. A real, spec-conformant exporter (e.g. a
// bank's payroll file) is added by implementing this and registering it.
type Exporter interface {
	ID() string
	Name() string
	// Generate returns the file bytes and a suggested filename. It must
	// validate everything it relies on and return *ValidationError for bad
	// input.
	Generate(batch PayBatch) (data []byte, filename string, err error)
}

// PayBatch is one payroll run for one employer and one period.
type PayBatch struct {
	Employer Employer `json:"employer"`
	// Period is the payroll month, YYYY-MM.
	Period string `json:"period"`
	// PayDate is the payment date, YYYY-MM-DD.
	PayDate string    `json:"pay_date"`
	Lines   []PayLine `json:"lines"`
	// Totals is optional. When present it must equal the sum of the lines.
	Totals *Totals `json:"totals,omitempty"`
}

type Employer struct {
	Name string `json:"name"`
	// TaxID is the 13-digit employer tax ID (checksum validated).
	TaxID string `json:"tax_id"`
	// SSOEmployerNo is the employer's SSO account number (free text; format not validated).
	SSOEmployerNo string `json:"sso_employer_no,omitempty"`
	// Bank details of the paying account (used by bank exporters).
	BankCode  string `json:"bank_code,omitempty"`
	AccountNo string `json:"account_no,omitempty"`
}

// PayLine is one employee's payroll result. All amounts are satang.
type PayLine struct {
	EmployeeCode string `json:"employee_code"`
	FullName     string `json:"full_name"`
	// NationalID is the 13-digit Thai national ID (checksum validated).
	NationalID string `json:"national_id"`
	// TaxID is the personal tax ID; for Thai nationals it is normally the
	// national ID. Empty means "same as NationalID".
	TaxID string `json:"tax_id,omitempty"`
	// SSONo is the employee's SSO number (free text; format not validated).
	SSONo    string `json:"sso_no,omitempty"`
	BankCode string `json:"bank_code,omitempty"`
	// AccountNo: digits and hyphens only.
	AccountNo string `json:"account_no,omitempty"`

	Gross             Satang `json:"gross"`
	IncomeTaxWithheld Satang `json:"income_tax_withheld"`
	SSOEmployee       Satang `json:"sso_employee"`
	SSOEmployer       Satang `json:"sso_employer"`
	OtherDeductions   Satang `json:"other_deductions"`
	Net               Satang `json:"net"`
	// SSOWage is the wage base the SSO contribution was computed on (after
	// the statutory cap, which is the payroll engine's job, not ours).
	SSOWage Satang `json:"sso_wage,omitempty"`
}

// EffectiveTaxID returns TaxID, falling back to NationalID.
func (l PayLine) EffectiveTaxID() string {
	if l.TaxID != "" {
		return l.TaxID
	}
	return l.NationalID
}

type Totals struct {
	Gross             Satang `json:"gross"`
	IncomeTaxWithheld Satang `json:"income_tax_withheld"`
	SSOEmployee       Satang `json:"sso_employee"`
	SSOEmployer       Satang `json:"sso_employer"`
	OtherDeductions   Satang `json:"other_deductions"`
	Net               Satang `json:"net"`
}

// Sum computes the totals of the lines.
func (b PayBatch) Sum() Totals {
	var t Totals
	for _, l := range b.Lines {
		t.Gross += l.Gross
		t.IncomeTaxWithheld += l.IncomeTaxWithheld
		t.SSOEmployee += l.SSOEmployee
		t.SSOEmployer += l.SSOEmployer
		t.OtherDeductions += l.OtherDeductions
		t.Net += l.Net
	}
	return t
}
