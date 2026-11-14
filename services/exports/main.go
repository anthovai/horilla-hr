// Command exports serves OrvaHR payroll export generation over HTTP.
package main

import (
	"log"
	"net/http"
	"os"
	"time"

	"github.com/orvahr/exports/internal/export"
)

func main() {
	bind := os.Getenv("EXPORTS_BIND")
	if bind == "" {
		bind = "127.0.0.1:8091"
	}
	srv := &http.Server{
		Addr:              bind,
		Handler:           export.NewHandler(export.DefaultRegistry()),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
	}
	log.Printf("exports listening on %s", bind)
	log.Fatal(srv.ListenAndServe())
}
