package manager

import (
	"context"
	"newadmin/internal/adminapi"
	"testing"
	"time"
)

func TestFixedWelfareClockAndSnapshot(t *testing.T) {
	s, _, a, c := directFixture(t)
	ctx := context.Background()
	times, e := welfareTimes("20:30，08:00")
	if e != nil || times != "08:00,20:30" {
		t.Fatal(times, e)
	}
	for _, bad := range []string{"25:00", "12:60", "08:00,08:00"} {
		if _, e := welfareTimes(bad); e == nil {
			t.Fatal(bad)
		}
	}
	rule := jobRule{section: "welfare.online", revision: 91, value: map[string]any{"enabled": true, "times": times, "reward": adminapi.Reward{Points: 7}}}
	_, e = s.store.db.Exec("INSERT OR REPLACE INTO manager_job_rules(section,value_json,baseline_id,revision,updated_at) VALUES(?,?,0,91,'test')", rule.section, jsonText(rule.value))
	if e != nil {
		t.Fatal(e)
	}
	tm := time.Date(2026, 11, 22, 20, 30, 1, 0, chinaZone())
	snap := gameSession{Complete: true, Connections: []gameConnection{{CharacterID: c, AccountID: a}, {CharacterID: c, AccountID: a}}}
	run := func(tm time.Time, snap *gameSession) {
		t.Helper()
		if err := s.runFixedWelfare(ctx, rule, tm, snap); err != nil {
			t.Fatal(err)
		}
	}
	run(tm.Add(-time.Minute), &snap)
	run(tm, nil)
	if n := number(t, s.store.db, "SELECT COUNT(*) FROM manager_welfare_slots"); n != 0 {
		t.Fatal(n)
	}
	run(tm, &snap)
	run(tm.Add(time.Second), &snap)
	run(tm.Add(time.Minute), &snap)
	if n := number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", a); n != 7 {
		t.Fatal(n)
	}
	later := tm.Add(24 * time.Hour)
	run(later, &gameSession{Complete: true})
	run(later.Add(time.Second), &snap)
	run(later.Add(24*time.Hour+20*time.Second), &snap)
	if n := number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", a); n != 7 {
		t.Fatal(n)
	}
	run(later.Add(24*time.Hour+12*time.Hour-30*time.Minute), &snap)
	if n := number(t, s.store.db, "SELECT cera FROM accounts WHERE account_id=?", a); n != 14 {
		t.Fatal(n)
	}
}
