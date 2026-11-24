package manager

import (
	"database/sql"
	"errors"
	"net/http"
	"newadmin/internal/adminapi"
	"strings"
	"time"
)

func (s *Service) deleteCode(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Kind string `json:"kind"`
		Code string `json:"code"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !s.config.DirectDatabase || body.Code == "" || len(body.Code) > 100 || strings.TrimSpace(body.Code) != body.Code {
		writeError(w, fail(400, "invalid_code_action", "请选择一个有效的兑换码或激活码"), "")
		return
	}
	var table, remove, module string
	switch body.Kind {
	case "cdk":
		table = "gmkit_cdks"
		remove = `DELETE FROM gmkit_cdks WHERE code=? AND used=0
			AND NOT EXISTS (SELECT 1 FROM gmkit_redemptions WHERE cdk_id=gmkit_cdks.id)`
		module = "CDK管理"
	case "registration":
		table = "gmkit_registration_codes"
		remove = "DELETE FROM gmkit_registration_codes WHERE code=? AND state IN ('unused','revoked')"
		module = "注册管理"
	default:
		writeError(w, fail(400, "invalid_code_action", "请选择一个有效的兑换码或激活码"), "")
		return
	}
	id := r.Header.Get("Idempotency-Key")
	s.mutate(w, r, body, module, "codes.delete", body.Code, func(tx *sql.Tx) (any, error) {
		ctx := r.Context()
		res, err := tx.ExecContext(ctx, remove, body.Code)
		if err != nil {
			return nil, err
		}
		changed, err := res.RowsAffected()
		if err != nil {
			return nil, err
		}
		if changed == 0 {
			var found int
			err = tx.QueryRowContext(ctx, "SELECT 1 FROM "+table+" WHERE code=?", body.Code).Scan(&found)
			if errors.Is(err, sql.ErrNoRows) {
				return nil, fail(404, "code_not_found", "该码已不存在")
			}
			if err != nil {
				return nil, err
			}
			return nil, fail(409, "code_used", "该码已使用，原记录需要保留")
		}
		// Retain the generated-code identifier so a future generation cannot reuse it.
		if _, err := tx.ExecContext(ctx, "UPDATE manager_codes SET state='deleted' WHERE kind=? AND code=?", body.Kind, body.Code); err != nil {
			return nil, err
		}
		return adminapi.Receipt{RequestID: id, Action: "codes.delete", State: "database_committed", DatabaseCommitted: true, Message: "已删除", CreatedAt: time.Now().UTC()}, nil
	})
}

func (s *Service) codeState(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Kind    string `json:"kind"`
		Code    string `json:"code"`
		Enabled bool   `json:"enabled"`
		Confirm bool   `json:"confirm"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !s.config.DirectDatabase || !body.Confirm || (body.Kind != "cdk" && body.Kind != "registration") || len(body.Code) < 1 || len(body.Code) > 100 {
		writeError(w, fail(400, "invalid_code_action", "请选择真实兑换码或激活码并确认"), "")
		return
	}
	s.mutate(w, r, body, "CDK管理", "codes.state", body.Code, func(tx *sql.Tx) (any, error) {
		table, column, value := "gmkit_cdks", "enabled", any(0)
		if body.Enabled {
			value = 1
		}
		if body.Kind == "registration" {
			table, column, value = "gmkit_registration_codes", "state", "revoked"
			if body.Enabled {
				value = "unused"
			}
			var state string
			if err := tx.QueryRowContext(r.Context(), "SELECT state FROM gmkit_registration_codes WHERE code=?", body.Code).Scan(&state); err != nil {
				return nil, fail(404, "code_not_found", "激活码不存在")
			}
			if state == "used" {
				return nil, fail(409, "code_used", "已使用的激活码不能重新启用或抹去使用状态")
			}
		}
		changed, err := tx.ExecContext(r.Context(), "UPDATE "+table+" SET "+column+"=? WHERE code=?", value, body.Code)
		if err != nil {
			return nil, err
		}
		n, _ := changed.RowsAffected()
		if n != 1 {
			return nil, fail(404, "code_not_found", "兑换码不存在")
		}
		return adminapi.Receipt{RequestID: r.Header.Get("Idempotency-Key"), Action: "codes.state", State: "database_committed", DatabaseCommitted: true, Message: "兑换码状态已提交，原使用记录保留", CreatedAt: time.Now().UTC()}, nil
	})
}
func (s *Service) sendAnnouncement(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Section string `json:"section"`
		Content string `json:"content"`
		Confirm bool   `json:"confirm"`
	}
	if err := decode(w, r, &body); err != nil {
		writeError(w, err, "")
		return
	}
	if !s.config.DirectDatabase || !body.Confirm || (body.Section != "announcement.navigation" && body.Section != "announcement.chat") || strings.TrimSpace(body.Content) == "" || len([]rune(body.Content)) > 400 {
		writeError(w, fail(400, "invalid_announcement", "即时公告需要1至400字内容和确认"), "")
		return
	}
	s.mutate(w, r, body, "公告管理", "announcement.send", body.Section, func(tx *sql.Tx) (any, error) {
		now := time.Now().In(chinaZone())
		res, err := tx.ExecContext(r.Context(), "INSERT INTO gmkit_announcements(content,color,source,created_at,expires_at) VALUES(?,?,?,?,?)", body.Content, "0xFF0000FF", "go-gm:manual:"+r.Header.Get("Idempotency-Key"), now.Format(time.RFC3339), now.Add(5*time.Minute).Format(time.RFC3339))
		if err != nil {
			return nil, err
		}
		id, _ := res.LastInsertId()
		return adminapi.Receipt{RequestID: r.Header.Get("Idempotency-Key"), Action: "announcement.send", State: "database_committed", DatabaseCommitted: true, CreatedAt: time.Now().UTC(), Message: "公告已进入玩家登录器公告队列，显示回执由玩家端上报", Result: map[string]any{"id": id}}, nil
	})
}
