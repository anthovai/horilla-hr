package export

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const (
	idA = "1101700230708" // checksum verified by hand-calculation of the weighted sum
	idB = "3101500123459"
	idC = "1234567890121" // weighted sum 352, 352%11==0 -> check digit 1
	emp = "5012345678900"
)

func TestValidNationalID(t *testing.T) {
	tests := []struct {
		id   string
		want bool
	}{
		{idA, true}, {idB, true}, {idC, true}, {emp, true},
		{"1101700230709", false}, // wrong check digit
		{"1234567890120", false},
		{"110170023070", false},   // 12 digits
		{"11017002307080", false}, // 14 digits
		{"", false},
		{"11017002307a8", false},
		{"1101700230 08", false},
		{"๑๑๐๑๗๐๐๒๓๐๗๐๘", false}, // Thai digits are not accepted
	}
	for _, tc := range tests {
		if got := ValidNationalID(tc.id); got != tc.want {
			t.Errorf("ValidNationalID(%q)=%v want %v", tc.id, got, tc.want)
		}
	}
}

func TestFormatSatang(t *testing.T) {
	tests := map[Satang]string{
		0: "0.00", 1: "0.01", 9: "0.09", 10: "0.10", 99: "0.99", 100: "1.00",
		123456: "1234.56", 100000000: "1000000.00", -5: "-0.05", -123456: "-1234.56",
		9223372036854775807:  "92233720368547758.07",
		-9223372036854775808: "-92233720368547758.08",
	}
	for in, want := range tests {
		if got := FormatSatang(in); got != want {
			t.Errorf("FormatSatang(%d)=%q want %q", in, got, want)
		}
	}
}

func TestSafeText(t *testing.T) {
	tests := map[string]string{
		"=1+1": "'=1+1", "+cmd": "'+cmd", "-2": "'-2", "@SUM(A1)": "'@SUM(A1)",
		"\tx": "'\tx", "\rx": "'\rx", "  =1": "'  =1",
		"Somchai": "Somchai", "สมชาย": "สมชาย", "": "", "a=b": "a=b", "  ": "  ",
	}
	for in, want := range tests {
		if got := SafeText(in); got != want {
			t.Errorf("SafeText(%q)=%q want %q", in, got, want)
		}
	}
}

func line(code, name, id string, gross, tax, sso Satang) PayLine {
	return PayLine{
		EmployeeCode: code, FullName: name, NationalID: id, SSONo: id,
		BankCode: "004", AccountNo: "123-4-56789-0",
		Gross: gross, IncomeTaxWithheld: tax, SSOEmployee: sso, SSOEmployer: sso, SSOWage: 1500000,
		Net: gross - tax - sso,
	}
}

func validBatch() PayBatch {
	return PayBatch{
		Employer: Employer{Name: "Acme (Thailand) Co., Ltd.", TaxID: emp, SSOEmployerNo: "1234567890"},
		Period:   "2026-11", PayDate: "2026-11-28",
		Lines: []PayLine{
			line("E1", "Somchai Jaidee", idA, 5000000, 123456, 75000),
			line("E2", "Malee Rakdee", idB, 3000050, 0, 75000),
		},
	}
}

func problems(t *testing.T, err error) string {
	t.Helper()
	ve, ok := err.(*ValidationError)
	if !ok {
		t.Fatalf("want *ValidationError, got %T (%v)", err, err)
	}
	return strings.Join(ve.Problems, "|")
}

func TestValidate(t *testing.T) {
	if err := validBatch().Validate(); err != nil {
		t.Fatalf("valid batch rejected: %v", err)
	}
	tests := []struct {
		name   string
		mutate func(*PayBatch)
		want   string
	}{
		{"employer name", func(b *PayBatch) { b.Employer.Name = " " }, "employer.name"},
		{"employer tax id", func(b *PayBatch) { b.Employer.TaxID = "1" }, "employer.tax_id"},
		{"period", func(b *PayBatch) { b.Period = "2026-13" }, "period"},
		{"pay date", func(b *PayBatch) { b.PayDate = "2026-02-30" }, "pay_date"},
		{"no lines", func(b *PayBatch) { b.Lines = nil }, "lines must not be empty"},
		{"bad national id", func(b *PayBatch) { b.Lines[0].NationalID = "1101700230709" }, "lines[0].national_id"},
		{"bad tax id", func(b *PayBatch) { b.Lines[0].TaxID = "123" }, "lines[0].tax_id"},
		{"dup id", func(b *PayBatch) { b.Lines[1].NationalID = idA }, "duplicates"},
		{"negative gross", func(b *PayBatch) { b.Lines[0].Gross = -1 }, "gross must not be negative"},
		{"net mismatch by 1 satang", func(b *PayBatch) { b.Lines[0].Net++ }, "net"},
		{"bad account", func(b *PayBatch) { b.Lines[0].AccountNo = "12 ab" }, "account_no"},
		{"bad bank", func(b *PayBatch) { b.Lines[0].BankCode = "x" }, "bank_code"},
		{"empty name", func(b *PayBatch) { b.Lines[0].FullName = "" }, "full_name"},
		{"totals mismatch", func(b *PayBatch) {
			t := b.Sum()
			t.Net++
			b.Totals = &t
		}, "totals do not match"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			b := validBatch()
			tc.mutate(&b)
			if got := problems(t, b.Validate()); !strings.Contains(got, tc.want) {
				t.Errorf("problems %q missing %q", got, tc.want)
			}
		})
	}
	t.Run("matching totals ok", func(t *testing.T) {
		b := validBatch()
		tt := b.Sum()
		b.Totals = &tt
		if err := b.Validate(); err != nil {
			t.Fatal(err)
		}
	})
}

func TestSumReconciles(t *testing.T) {
	b := validBatch()
	s := b.Sum()
	if s.Gross != 8000050 || s.IncomeTaxWithheld != 123456 || s.SSOEmployee != 150000 || s.Net != s.Gross-s.IncomeTaxWithheld-s.SSOEmployee {
		t.Errorf("unexpected totals %+v", s)
	}
}

func parse(t *testing.T, data []byte) [][]string {
	t.Helper()
	if !bytes.HasPrefix(data, utf8BOM) {
		t.Error("missing BOM")
	}
	r := csv.NewReader(bytes.NewReader(bytes.TrimPrefix(data, utf8BOM)))
	r.FieldsPerRecord = -1
	rows, err := r.ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

func TestGenericBankCSV(t *testing.T) {
	b := validBatch()
	b.Lines = append(b.Lines, PayLine{FullName: "Zero", NationalID: idC}) // net 0, no bank: skipped
	data, name, err := GenericBankCSV{}.Generate(b)
	if err != nil {
		t.Fatal(err)
	}
	if name != "generic_bank_transfer_2026-11.csv" {
		t.Errorf("filename %q", name)
	}
	rows := parse(t, data)
	if !strings.Contains(rows[0][0], "NOT ANY BANK'S OFFICIAL FORMAT") {
		t.Errorf("missing disclaimer: %v", rows[0])
	}
	if len(rows) != 5 { // disclaimer, header, 2 lines, total
		t.Fatalf("rows=%d: %v", len(rows), rows)
	}
	if rows[2][4] != "48015.44" { // 5000000-123456-75000 = 4801544 satang
		t.Errorf("amount %q", rows[2][4])
	}
	if rows[3][4] != "29250.50" { // 3000050-75000 = 2925050
		t.Errorf("amount %q", rows[3][4])
	}
	if rows[4][0] != "TOTAL" || rows[4][4] != "77265.94" { // 4801544+2925050 = 7726594
		t.Errorf("total row %v", rows[4])
	}
}

func TestGenericBankCSVMissingAccount(t *testing.T) {
	b := validBatch()
	b.Lines[1].AccountNo = ""
	_, _, err := GenericBankCSV{}.Generate(b)
	if got := problems(t, err); !strings.Contains(got, "lines[1]") {
		t.Errorf("got %q", got)
	}
}

func TestCSVInjectionNeutralised(t *testing.T) {
	b := validBatch()
	b.Employer.Name = "=HYPERLINK(\"http://x\")"
	b.Employer.SSOEmployerNo = "@x"
	b.Lines[0].FullName = "=cmd|' /C calc'!A0"
	b.Lines[0].EmployeeCode = "+1"
	b.Lines[1].FullName = "-2+3"
	for _, e := range DefaultRegistry().List() {
		data, _, err := e.Generate(b)
		if err != nil {
			t.Fatalf("%s: %v", e.ID(), err)
		}
		for ri, row := range parse(t, data) {
			if ri == 0 {
				continue // disclaimer row, our own text
			}
			for _, cell := range row {
				if cell != "" && strings.ContainsRune("=+-@\t\r", rune(cell[0])) {
					t.Errorf("%s: cell %q starts with a formula character", e.ID(), cell)
				}
			}
		}
		if !strings.Contains(string(data), "'=cmd") && e.ID() != "sso_monthly_csv" {
			t.Errorf("%s: expected neutralised name in output", e.ID())
		}
	}
}

func TestPND1Summary(t *testing.T) {
	b := validBatch()
	b.Lines[1].TaxID = idC
	data, name, err := PND1SummaryCSV{}.Generate(b)
	if err != nil || name != "pnd1_summary_2026-11.csv" {
		t.Fatal(name, err)
	}
	rows := parse(t, data)
	if !strings.Contains(rows[0][0], "NOT THE REVENUE DEPARTMENT") {
		t.Error("missing disclaimer")
	}
	if rows[3][0] != idA || rows[3][3] != "50000.00" || rows[3][4] != "1234.56" {
		t.Errorf("row %v", rows[3])
	}
	if rows[4][0] != idC || rows[4][1] != idB { // explicit tax id wins
		t.Errorf("row %v", rows[4])
	}
	last := rows[len(rows)-1]
	if last[0] != "TOTAL" || last[3] != "80000.50" || last[4] != "1234.56" {
		t.Errorf("total %v", last)
	}
}

func TestSSOMonthly(t *testing.T) {
	b := validBatch()
	data, name, err := SSOMonthlyCSV{}.Generate(b)
	if err != nil || name != "sso_monthly_summary_2026-11.csv" {
		t.Fatal(name, err)
	}
	rows := parse(t, data)
	if !strings.Contains(rows[0][0], "NOT THE SSO FORM 1-10") {
		t.Error("missing disclaimer")
	}
	last := rows[len(rows)-1]
	want := []string{"TOTAL", "", "", "30000.00", "1500.00", "1500.00", "3000.00"}
	if strings.Join(last, ",") != strings.Join(want, ",") {
		t.Errorf("total %v want %v", last, want)
	}

	t.Run("non-contributor skipped", func(t *testing.T) {
		b := validBatch()
		b.Lines[1].SSOEmployee, b.Lines[1].SSOEmployer, b.Lines[1].SSOWage, b.Lines[1].SSONo = 0, 0, 0, ""
		b.Lines[1].Net = b.Lines[1].Gross
		d, _, err := SSOMonthlyCSV{}.Generate(b)
		if err != nil {
			t.Fatal(err)
		}
		if n := len(parse(t, d)); n != 5 {
			t.Errorf("rows=%d", n)
		}
	})
	t.Run("missing wage and sso no", func(t *testing.T) {
		b := validBatch()
		b.Lines[0].SSOWage = 0
		b.Lines[1].SSONo = ""
		_, _, err := SSOMonthlyCSV{}.Generate(b)
		got := problems(t, err)
		if !strings.Contains(got, "lines[0]: sso_wage") || !strings.Contains(got, "lines[1]: sso_no") {
			t.Errorf("got %q", got)
		}
	})
}

func TestExportersRejectInvalidBatch(t *testing.T) {
	for _, e := range DefaultRegistry().List() {
		b := validBatch()
		b.Lines[0].NationalID = "1"
		if _, _, err := e.Generate(b); err == nil {
			t.Errorf("%s accepted invalid batch", e.ID())
		}
	}
}

type fake struct{ id string }

func (f fake) ID() string                                { return f.id }
func (f fake) Name() string                              { return "fake" }
func (f fake) Generate(PayBatch) ([]byte, string, error) { return []byte("x"), "x.txt", nil }

func TestRegistry(t *testing.T) {
	r := NewRegistry()
	if err := r.Register(fake{"b"}); err != nil {
		t.Fatal(err)
	}
	if err := r.Register(fake{"a"}); err != nil {
		t.Fatal(err)
	}
	if err := r.Register(fake{"a"}); err == nil {
		t.Error("duplicate accepted")
	}
	if err := r.Register(fake{""}); err == nil {
		t.Error("empty id accepted")
	}
	l := r.List()
	if len(l) != 2 || l[0].ID() != "a" || l[1].ID() != "b" {
		t.Errorf("list order wrong")
	}
	if _, ok := r.Get("zzz"); ok {
		t.Error("unexpected hit")
	}
	if got := len(DefaultRegistry().List()); got != 3 {
		t.Errorf("default registry has %d", got)
	}
}

func do(h http.Handler, method, path, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestHandler(t *testing.T) {
	h := NewHandler(DefaultRegistry())
	good, _ := json.Marshal(validBatch())

	t.Run("healthz", func(t *testing.T) {
		rec := do(h, "GET", "/healthz", "")
		if rec.Code != 200 || !strings.Contains(rec.Body.String(), "ok") {
			t.Error(rec.Code, rec.Body.String())
		}
	})
	t.Run("list", func(t *testing.T) {
		rec := do(h, "GET", "/v1/exporters", "")
		var out struct{ Exporters []struct{ ID, Name string } }
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || len(out.Exporters) != 3 || out.Exporters[0].ID != "generic_bank_csv" {
			t.Errorf("%v %s", err, rec.Body.String())
		}
	})
	t.Run("export ok", func(t *testing.T) {
		rec := do(h, "POST", "/v1/export/generic_bank_csv", string(good))
		if rec.Code != 200 {
			t.Fatal(rec.Code, rec.Body.String())
		}
		if cd := rec.Header().Get("Content-Disposition"); cd != `attachment; filename=generic_bank_transfer_2026-11.csv` {
			t.Errorf("CD %q", cd)
		}
		if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/csv") {
			t.Errorf("CT %q", ct)
		}
		if !strings.Contains(rec.Body.String(), "48015.44") {
			t.Error("body missing amount")
		}
	})
	t.Run("422", func(t *testing.T) {
		b := validBatch()
		b.Lines[0].Net++
		body, _ := json.Marshal(b)
		rec := do(h, "POST", "/v1/export/pnd1_summary_csv", string(body))
		if rec.Code != 422 || !strings.Contains(rec.Body.String(), "problems") {
			t.Error(rec.Code, rec.Body.String())
		}
		if rec.Header().Get("Content-Type") != "application/json" {
			t.Error("not json")
		}
	})
	t.Run("404", func(t *testing.T) {
		if rec := do(h, "POST", "/v1/export/nope", string(good)); rec.Code != 404 {
			t.Error(rec.Code)
		}
	})
	t.Run("400 bad json", func(t *testing.T) {
		if rec := do(h, "POST", "/v1/export/sso_monthly_csv", "{"); rec.Code != 400 {
			t.Error(rec.Code)
		}
	})
	t.Run("400 unknown field", func(t *testing.T) {
		if rec := do(h, "POST", "/v1/export/sso_monthly_csv", `{"bogus":1}`); rec.Code != 400 {
			t.Error(rec.Code)
		}
	})
	t.Run("400 trailing data", func(t *testing.T) {
		if rec := do(h, "POST", "/v1/export/sso_monthly_csv", string(good)+" {}"); rec.Code != 400 {
			t.Error(rec.Code)
		}
	})
	t.Run("413 too large", func(t *testing.T) {
		big := `{"period":"` + strings.Repeat("a", maxBody+10) + `"}`
		if rec := do(h, "POST", "/v1/export/sso_monthly_csv", big); rec.Code != 413 {
			t.Error(rec.Code)
		}
	})
	t.Run("405 wrong method", func(t *testing.T) {
		if rec := do(h, "GET", "/v1/export/sso_monthly_csv", ""); rec.Code != 405 {
			t.Error(rec.Code)
		}
	})
}
