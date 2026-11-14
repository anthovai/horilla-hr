package export

import (
	"fmt"
	"regexp"
	"strings"
	"time"
)

// ValidationError lists every problem found, so callers can fix them in one go.
type ValidationError struct {
	Problems []string `json:"problems"`
}

func (e *ValidationError) Error() string {
	return "validation failed: " + strings.Join(e.Problems, "; ")
}

var (
	periodRe  = regexp.MustCompile(`^\d{4}-(0[1-9]|1[0-2])$`)
	accountRe = regexp.MustCompile(`^[0-9-]{4,20}$`)
	bankRe    = regexp.MustCompile(`^[0-9A-Za-z]{2,11}$`)
)

// ValidNationalID reports whether s is a 13-digit Thai national/tax ID with a
// correct check digit: sum(d[i]*(13-i), i=0..11), check = (11 - sum%11) % 10.
func ValidNationalID(s string) bool {
	if len(s) != 13 {
		return false
	}
	sum := 0
	for i := 0; i < 13; i++ {
		c := s[i]
		if c < '0' || c > '9' {
			return false
		}
		if i < 12 {
			sum += int(c-'0') * (13 - i)
		}
	}
	return (11-sum%11)%10 == int(s[12]-'0')
}

// Validate checks the batch structure, IDs, amounts and reconciliation.
// It returns nil or *ValidationError.
func (b PayBatch) Validate() error {
	var p []string
	add := func(f string, a ...any) { p = append(p, fmt.Sprintf(f, a...)) }

	if strings.TrimSpace(b.Employer.Name) == "" {
		add("employer.name is required")
	}
	if !ValidNationalID(b.Employer.TaxID) {
		add("employer.tax_id must be a valid 13-digit ID")
	}
	if !periodRe.MatchString(b.Period) {
		add("period must be YYYY-MM")
	}
	if _, err := time.Parse("2006-01-02", b.PayDate); err != nil {
		add("pay_date must be YYYY-MM-DD")
	}
	if len(b.Lines) == 0 {
		add("lines must not be empty")
	}

	seen := map[string]int{}
	for i, l := range b.Lines {
		pre := fmt.Sprintf("lines[%d]", i)
		if strings.TrimSpace(l.FullName) == "" {
			add("%s.full_name is required", pre)
		}
		if !ValidNationalID(l.NationalID) {
			add("%s.national_id is not a valid 13-digit ID", pre)
		} else if j, dup := seen[l.NationalID]; dup {
			add("%s.national_id duplicates lines[%d]", pre, j)
		} else {
			seen[l.NationalID] = i
		}
		if l.TaxID != "" && !ValidNationalID(l.TaxID) {
			add("%s.tax_id is not a valid 13-digit ID", pre)
		}
		for name, v := range map[string]Satang{
			"gross": l.Gross, "income_tax_withheld": l.IncomeTaxWithheld,
			"sso_employee": l.SSOEmployee, "sso_employer": l.SSOEmployer,
			"other_deductions": l.OtherDeductions, "net": l.Net, "sso_wage": l.SSOWage,
		} {
			if v < 0 {
				add("%s.%s must not be negative", pre, name)
			}
		}
		if want := l.Gross - l.IncomeTaxWithheld - l.SSOEmployee - l.OtherDeductions; want != l.Net {
			add("%s: net %d != gross - tax - sso_employee - other_deductions (%d)", pre, l.Net, want)
		}
		if l.BankCode != "" && !bankRe.MatchString(l.BankCode) {
			add("%s.bank_code is malformed", pre)
		}
		if l.AccountNo != "" && !accountRe.MatchString(l.AccountNo) {
			add("%s.account_no must be 4-20 digits/hyphens", pre)
		}
	}

	if b.Totals != nil {
		if got := b.Sum(); got != *b.Totals {
			add("totals do not match the sum of lines (computed %+v, supplied %+v)", got, *b.Totals)
		}
	}
	if len(p) > 0 {
		return &ValidationError{Problems: p}
	}
	return nil
}
