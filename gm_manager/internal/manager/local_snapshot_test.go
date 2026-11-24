package manager

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"newadmin/internal/adminapi"
)

func TestOptionalLocalSnapshotReadonly(t *testing.T) {
	path := os.Getenv("GM_LOCAL_SNAPSHOT")
	if path == "" {
		t.Skip("仅在明确指定本机历史快照时运行")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	before := sha256.Sum256(raw)
	s, _ := newTestService(t)
	s.reader.database = path
	s.config.GameDatabase = path
	h := httptest.NewServer(s.Handler())
	defer h.Close()
	client, err := adminapi.NewClient(h.URL, s.config.Token, "")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	ctx := context.Background()
	var accounts adminapi.Page[adminapi.Account]
	start := time.Now()
	if err = client.Do(ctx, "GET", "/manage/v1/accounts?page_size=200", "", nil, &accounts); err != nil {
		t.Fatal(err)
	}
	accountMS := float64(time.Since(start).Microseconds()) / 1000
	var characters adminapi.Page[adminapi.Character]
	start = time.Now()
	if err = client.Do(ctx, "GET", "/manage/v1/characters?page_size=200", "", nil, &characters); err != nil {
		t.Fatal(err)
	}
	characterMS := float64(time.Since(start).Microseconds()) / 1000
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
	if err != nil {
		t.Fatal(err)
	}
	var ac, cc int
	err = db.QueryRow("SELECT COUNT(*) FROM accounts").Scan(&ac)
	if err == nil {
		err = db.QueryRow("SELECT COUNT(*) FROM characters WHERE delete_flag=0").Scan(&cc)
	}
	db.Close()
	if err != nil {
		t.Fatal(err)
	}
	if accounts.Total != ac || characters.Total != cc {
		t.Fatal("快照行数不一致", accounts.Total, characters.Total, ac, cc)
	}
	for _, row := range accounts.Items {
		if row.OnlineStatus != "unknown" {
			t.Fatal("历史快照推测了在线状态")
		}
	}
	var status adminapi.Status
	if err = client.Do(ctx, "GET", "/manage/v1/status", "", nil, &status); err != nil {
		t.Fatal(err)
	}
	if status.OnlineAccounts != nil || status.OnlineCharacters != nil {
		t.Fatal("历史数据库推测了在线数量")
	}
	var records adminapi.Page[adminapi.Record]
	if err = client.Do(ctx, "GET", "/manage/v1/records?kind=all&page_size=50", "", nil, &records); err != nil {
		t.Fatal(err)
	}
	afterRaw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	after := sha256.Sum256(afterRaw)
	if before != after {
		t.Fatal("历史快照被修改")
	}
	report := map[string]any{"scope": "明确指定的本机历史快照，只读；不代表当前线上状态或玩家验收", "database_sha256": hex.EncodeToString(before[:]), "hash_unchanged": true, "accounts": ac, "characters": cc, "account_query_local_ms": accountMS, "character_query_local_ms": characterMS, "record_matches_in_bounded_sources": records.Total, "records_source": records.Source, "online_status": "unknown", "server_contacted": false}
	if output := os.Getenv("GM_LOCAL_REPORT"); output != "" {
		data, _ := json.MarshalIndent(report, "", "  ")
		if err = os.WriteFile(output, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Logf("本机历史快照账号%d、角色%d；API查询%.2fms/%.2fms；文件哈希保持不变", ac, cc, accountMS, characterMS)
}
