package unimem

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLearningWireAndVersionNegotiation(t *testing.T) {
	var requests []map[string]any
	schema := LearningSchema
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/v1/learning/") {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer key" {
			t.Error("missing authorization")
		}
		if r.Method == "GET" {
			context := r.URL.Query()["context"]
			if len(context) != 2 || context[0] != "user:u" || context[1] != "agent:a" {
				t.Errorf("bad context %v", context)
			}
		} else {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			requests = append(requests, body)
		}
		json.NewEncoder(w).Encode(map[string]any{"schema": schema, "operations": []string{"configure", "assign", "delivery", "outcome", "run", "status", "rollback"}})
	}))
	defer server.Close()
	client, err := New(server.URL, "key", "a", []string{"user:u"})
	if err != nil {
		t.Fatal(err)
	}
	learning := client.Learning()
	ctx := context.Background()
	operations := []func() (map[string]any, error){
		func() (map[string]any, error) { return learning.Configure(ctx, "observe", nil) },
		func() (map[string]any, error) {
			return learning.Assign(ctx, "o", []string{"m"}, map[string]any{"experiment_id": "e"})
		},
		func() (map[string]any, error) { return learning.Delivery(ctx, "o", nil, "e") },
		func() (map[string]any, error) { return learning.Outcome(ctx, "o", map[string]any{"reward": 0.8}, "e") },
		func() (map[string]any, error) { return learning.Run(ctx, nil) },
		func() (map[string]any, error) { return learning.Status(ctx, map[string]any{"experiment_id": "e"}) },
		func() (map[string]any, error) { return learning.Rollback(ctx, "regression", nil) },
	}
	for _, operation := range operations {
		if _, err := operation(); err != nil {
			t.Fatal(err)
		}
	}
	if len(requests) != 6 {
		t.Fatalf("expected six POSTs: %v", requests)
	}
	if ids, ok := requests[2]["memory_ids"].([]any); !ok || len(ids) != 0 {
		t.Errorf("control delivery must serialize []: %v", requests[2])
	}
	if requests[3]["reward"] != 0.8 || requests[3]["experiment_id"] != "e" {
		t.Error(requests[3])
	}
	before := len(requests)
	schema = "unimem.learning.v0"
	if _, err := learning.Run(ctx, nil); err == nil {
		t.Fatal("version downgrade accepted")
	}
	if len(requests) != before {
		t.Fatal("mutation before successful negotiation")
	}
}
