package manager

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAdventureRequestValidation(t *testing.T) {
	cases := []struct {
		body      string
		operation bool
		ok        bool
	}{
		{body: "{\"action\":\"list\"}", ok: true},
		{body: "{\"action\":\"set\",\"character_id\":1}", ok: true},
		{body: "{\"action\":\"expedition_start\",\"request_id\":1,\"area_id\":4,\"hours\":12,\"members\":[1,2,3,4],\"rotation_key\":8}", ok: true},
		{body: "{\"action\":\"expedition_claim\",\"run_id\":9223372036854775807}", ok: true},
		{body: "{\"action\":\"expedition_cancel\",\"run_id\":0}"},
		{body: "{\"action\":\"expedition_start\",\"request_id\":1,\"area_id\":1,\"hours\":4,\"members\":[1,2,3,4,5],\"rotation_key\":8}"},
		{body: "{\"action\":\"list\",\"token\":\"caller\"}"},
		{body: "{\"action\":\"remove\",\"character_id\":1,\"character_id\":2}"},
		{body: "{\"action\":\"buy\",\"request_id\":1,\"character_id\":1,\"connection_generation\":1,\"quantity\":1,\"item_id\":2}", operation: true, ok: true},
		{body: "{\"action\":\"use_capsule\",\"request_id\":1,\"character_id\":1,\"connection_generation\":1,\"quantity\":100}", operation: true},
		{body: "{\"action\":\"state\",\"request_id\":1,\"quantity\":2}", operation: true},
	}
	for _, item := range cases {
		req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(item.body))
		fields := adventureCollectionFields
		if item.operation {
			fields = adventureOperationFields
		}
		_, err := adventureRequest(req, fields)
		if (err == nil) != item.ok {
			t.Errorf("%s: %v", item.body, err)
		}
	}
}
