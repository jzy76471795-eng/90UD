package manager

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strings"
)

var adventureCollectionFields = map[string][]string{
	"list":              {},
	"set":               {"character_id"},
	"remove":            {"character_id"},
	"expedition_start":  {"request_id", "area_id", "hours", "members", "rotation_key"},
	"expedition_cancel": {"run_id"},
	"expedition_claim":  {"run_id"},
}

var adventureOperationFields = map[string][]string{
	"state":          {"request_id"},
	"buy":            {"request_id", "character_id", "connection_generation", "item_id", "quantity"},
	"use_capsule":    {"request_id", "character_id", "connection_generation", "quantity"},
	"weekly_status":  {"request_id"},
	"weekly_sign":    {"request_id", "character_id", "connection_generation", "week"},
	"weekly_make_up": {"request_id", "character_id", "connection_generation", "week"},
	"weekly_claim":   {"request_id", "character_id", "connection_generation"},
}

func adventureRequest(r *http.Request, fields map[string][]string) (map[string]any, error) {
	body, err := launcherBody(r, []string{"action"},
		"request_id", "character_id", "area_id", "hours", "members", "rotation_key",
		"run_id", "connection_generation", "item_id", "quantity", "week")
	if err != nil {
		return nil, err
	}
	action, err := launcherString(body["action"], 24)
	required, known := fields[action]
	if err != nil || !known || len(body) != len(required)+1 {
		return nil, fail(400, "INVALID_REQUEST", "invalid adventure fields")
	}
	for _, name := range required {
		limit := int64(math.MaxUint32)
		switch name {
		case "members":
			members, ok := body[name].([]any)
			if !ok || len(members) < 1 || len(members) > 4 {
				return nil, fail(400, "INVALID_REQUEST", "invalid expedition members")
			}
			for _, member := range members {
				if _, err := launcherInteger(member, 1, limit); err != nil {
					return nil, err
				}
			}
			continue
		case "run_id":
			limit = math.MaxInt64
		case "quantity":
			limit = 99
		case "week":
			limit = 10
		}
		if _, err := launcherInteger(body[name], 1, limit); err != nil {
			return nil, err
		}
	}
	return body, nil
}

func (l *launcherService) adventure(ctx context.Context, path string, r *http.Request, ip string) (any, int, error) {
	action, fields := "adventure_collection", adventureCollectionFields
	if path == "/launcher/v1/adventure/operation" {
		action, fields = "adventure_operation", adventureOperationFields
	}
	body, err := adventureRequest(r, fields)
	if err != nil {
		return nil, 400, err
	}
	body["token"] = strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	body["remote_ip"] = ip
	var result json.RawMessage
	err = l.engine(ctx, action, body, &result)
	return result, 200, err
}
