package export

import (
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
)

const maxBody = 8 << 20

// NewHandler builds the HTTP API on top of a registry.
func NewHandler(r *Registry) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /v1/exporters", func(w http.ResponseWriter, _ *http.Request) {
		type item struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		}
		out := []item{}
		for _, e := range r.List() {
			out = append(out, item{e.ID(), e.Name()})
		}
		writeJSON(w, http.StatusOK, map[string]any{"exporters": out})
	})
	mux.HandleFunc("POST /v1/export/{id}", func(w http.ResponseWriter, req *http.Request) {
		e, ok := r.Get(req.PathValue("id"))
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]any{"error": "unknown exporter"})
			return
		}
		var batch PayBatch
		dec := json.NewDecoder(http.MaxBytesReader(w, req.Body, maxBody))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&batch); err != nil {
			code := http.StatusBadRequest
			var mbe *http.MaxBytesError
			if errors.As(err, &mbe) {
				code = http.StatusRequestEntityTooLarge
			}
			writeJSON(w, code, map[string]any{"error": "invalid JSON body: " + err.Error()})
			return
		}
		if _, err := dec.Token(); err != io.EOF {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid JSON body: trailing data"})
			return
		}
		data, name, err := e.Generate(batch)
		if err != nil {
			var ve *ValidationError
			if errors.As(err, &ve) {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"error": "validation failed", "problems": ve.Problems})
				return
			}
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": "export failed"})
			return
		}
		w.Header().Set("Content-Type", "text/csv; charset=utf-8")
		w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(data)
	})
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
