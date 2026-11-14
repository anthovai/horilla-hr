package export

import (
	"fmt"
	"sort"
	"sync"
)

// Registry holds exporters by ID.
type Registry struct {
	mu sync.RWMutex
	m  map[string]Exporter
}

func NewRegistry() *Registry { return &Registry{m: map[string]Exporter{}} }

// Register adds an exporter; duplicate or empty IDs are an error.
func (r *Registry) Register(e Exporter) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	id := e.ID()
	if id == "" {
		return fmt.Errorf("exporter has empty ID")
	}
	if _, ok := r.m[id]; ok {
		return fmt.Errorf("exporter %q already registered", id)
	}
	r.m[id] = e
	return nil
}

func (r *Registry) Get(id string) (Exporter, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.m[id]
	return e, ok
}

// List returns exporters sorted by ID.
func (r *Registry) List() []Exporter {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]Exporter, 0, len(r.m))
	for _, e := range r.m {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID() < out[j].ID() })
	return out
}

// DefaultRegistry returns a registry with the built-in (non-official) exporters.
func DefaultRegistry() *Registry {
	r := NewRegistry()
	for _, e := range []Exporter{GenericBankCSV{}, PND1SummaryCSV{}, SSOMonthlyCSV{}} {
		if err := r.Register(e); err != nil {
			panic(err)
		}
	}
	return r
}
