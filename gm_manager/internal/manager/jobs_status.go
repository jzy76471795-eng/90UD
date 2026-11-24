package manager

import (
	"encoding/json"
	"net/http"
	"newadmin/internal/adminapi"
	"time"
)

func (s *Service) jobsStatus(w http.ResponseWriter, r *http.Request) {
	section := r.URL.Query().Get("section")
	rows, err := s.store.db.QueryContext(r.Context(), "SELECT section,value_json,revision FROM manager_job_rules WHERE (?='' OR section=?) ORDER BY section", section, section)
	if err != nil {
		writeError(w, err, "")
		return
	}
	defer rows.Close()
	type rule struct {
		name, raw string
		revision  int64
	}
	rules := []rule{}
	for rows.Next() {
		var row rule
		if err = rows.Scan(&row.name, &row.raw, &row.revision); err != nil {
			writeError(w, err, "")
			return
		}
		rules = append(rules, row)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		writeError(w, err, "")
		return
	}
	items := []map[string]any{}
	for _, row := range rules {
		name, raw, revision := row.name, row.raw, row.revision
		value := map[string]any{}
		_ = json.Unmarshal([]byte(raw), &value)
		enabled := value["enabled"] == true
		if name == "registration" {
			enabled = value["reward_enabled"] == true
		}
		var count int
		var last, message string
		_ = s.store.db.QueryRowContext(r.Context(), "SELECT count(*),COALESCE(MAX(created_at),'') FROM manager_job_receipts WHERE section=?", name).Scan(&count, &last)
		_ = s.store.db.QueryRowContext(r.Context(), "SELECT message FROM manager_job_failures WHERE section=?", name).Scan(&message)
		items = append(items, map[string]any{"section": name, "revision": revision, "enabled": enabled, "delivered": count, "last_delivery": last, "last_error": message})
	}
	if err = rows.Err(); err != nil {
		writeError(w, err, "")
		return
	}
	write(w, 200, adminapi.Envelope{OK: true, Data: map[string]any{"worker_enabled": s.config.EnableJobs, "items": items, "checked_at": time.Now().UTC()}})
}
