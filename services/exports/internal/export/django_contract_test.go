package export

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestDjangoPayloadContract posts the exact JSON shape orvahr_bank
// (orvahr_bank/services.py build_pay_batch) sends and checks the generic CSV
// byte for byte. The handler rejects unknown fields, so a key renamed on
// either side fails here.
func TestDjangoPayloadContract(t *testing.T) {
	body := `{
	 "employer": {"name": "Acme", "tax_id": "5012345678900", "bank_code": "004", "account_no": "9876543210"},
	 "period": "2026-11", "pay_date": "2026-11-28",
	 "lines": [{"employee_code": "E1", "full_name": "Anan Employee", "national_id": "1101700230708",
	   "bank_code": "004", "account_no": "1234567890", "gross": 2500050, "income_tax_withheld": 0,
	   "sso_employee": 0, "sso_employer": 0, "other_deductions": 0, "net": 2500050}]
	}`
	want := "\xef\xbb\xbf" +
		"# GENERIC TRANSFER LIST - NOT ANY BANK'S OFFICIAL FORMAT - for reference/manual entry only\r\n" +
		"employee_code,full_name,bank_code,account_no,amount_thb,pay_date,reference\r\n" +
		"E1,Anan Employee,004,1234567890,25000.50,2026-11-28,SALARY 2026-11\r\n" +
		"TOTAL,,,,25000.50,,\r\n"

	tests := []struct {
		name     string
		body     string
		wantCode int
		wantBody string
	}{
		{"generic csv", body, http.StatusOK, want},
		{"unknown field rejected", strings.Replace(body, `"period"`, `"promptpay_id": "x", "period"`, 1), http.StatusBadRequest, ""},
	}
	h := NewHandler(DefaultRegistry())
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/export/generic_bank_csv", strings.NewReader(tc.body)))
			if rec.Code != tc.wantCode {
				t.Fatalf("code %d: %s", rec.Code, rec.Body.String())
			}
			if tc.wantBody != "" && rec.Body.String() != tc.wantBody {
				t.Errorf("body\n got %q\nwant %q", rec.Body.String(), tc.wantBody)
			}
		})
	}
}
