package export

import (
	"bytes"
	"encoding/csv"
	"strconv"
	"strings"
)

// utf8BOM lets Excel open Thai text correctly.
var utf8BOM = []byte{0xEF, 0xBB, 0xBF}

// FormatSatang renders satang as a baht string with exactly two decimals,
// using integer arithmetic only: 123456 -> "1234.56", -5 -> "-0.05".
func FormatSatang(s Satang) string {
	neg := s < 0
	u := uint64(s)
	if neg {
		u = -u // two's complement; correct even for MinInt64
	}
	str := strconv.FormatUint(u/100, 10) + "." + string('0'+byte(u%100/10)) + string('0'+byte(u%10))
	if neg {
		return "-" + str
	}
	return str
}

// SafeText neutralises CSV/spreadsheet formula injection in user-supplied
// text: a cell starting with = + - @ (or tab / CR, which some spreadsheets
// also treat specially) is prefixed with a single quote. Leading whitespace
// is skipped when deciding, since some apps trim it before evaluating.
func SafeText(s string) string {
	t := strings.TrimLeft(s, " ")
	if t == "" {
		return s
	}
	switch t[0] {
	case '=', '+', '-', '@', '\t', '\r':
		return "'" + s
	}
	return s
}

// writeCSV encodes rows (already sanitised by the caller where needed).
func writeCSV(rows [][]string) ([]byte, error) {
	var buf bytes.Buffer
	buf.Write(utf8BOM)
	w := csv.NewWriter(&buf)
	w.UseCRLF = true
	if err := w.WriteAll(rows); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}
