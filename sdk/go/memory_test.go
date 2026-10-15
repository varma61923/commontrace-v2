package commontrace

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestScopedMemoryAndRedirect(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var data map[string]any
		json.NewDecoder(r.Body).Decode(&data)
		if data["context"].([]any)[0] != "agent:alice" {
			t.Error("scope was replaced")
		}
		if r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("auth missing")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"facts":[]}`))
	}))
	defer server.Close()
	c, err := New(server.URL, "secret", "alice", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = c.Call(context.Background(), "add", map[string]any{"text": "fact", "context": []string{"agent:bob"}}); err != nil {
		t.Fatal(err)
	}
	if _, err = New("http://remote.example", "", "", nil); err == nil {
		t.Fatal("insecure remote URL")
	}
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, "https://example.invalid", 302) }))
	defer redirect.Close()
	c, _ = New(redirect.URL, "secret", "alice", nil)
	if _, err = c.Add(context.Background(), "fact"); err == nil {
		t.Fatal("redirect should fail")
	}
}

func TestRecordOperationsUseScopedWireContract(t *testing.T) {
	operations := []string{"turn", "record-put", "record-search", "record-forget", "record-reap-due"}
	requests := make(chan map[string]any, len(operations))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("missing authenticated POST")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		body["path"] = r.URL.Path
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"results":[{"deleted":["fact-1"],"reason":"retention","at":"2030-01-01T00:00:00Z"}],"context":"memory","record":{"id":"fact-1","version":2}}`))
	}))
	defer server.Close()
	client, err := New(server.URL, "secret", "alice", []string{"user:u"})
	if err != nil {
		t.Fatal(err)
	}
	for _, operation := range operations {
		arguments := map[string]any{}
		switch operation {
		case "turn":
			arguments["session_id"], arguments["query"] = "session", "hello"
		case "record-put":
			arguments["record"] = map[string]any{"id": "fact-1", "kind": "fact", "version": 1, "payload": map[string]any{"text": "hello"}}
		case "record-search":
			arguments["query"], arguments["limit"] = "hello", 20
		case "record-forget":
			arguments["id"], arguments["reason"] = "fact-1", "retention"
		case "record-reap-due":
			arguments["limit"] = 16
		}
		arguments["context"] = []string{"agent:mallory"}
		result, err := client.Call(context.Background(), operation, arguments)
		if err != nil {
			t.Fatalf("%s: %v", operation, err)
		}
		if result["context"] != "memory" {
			t.Fatal("response not decoded")
		}
		request := <-requests
		if request["path"] != "/v1/memory/"+operation {
			t.Fatal(request)
		}
		if !reflect.DeepEqual(request["context"], []any{"user:u", "agent:alice"}) {
			t.Fatal("context override", request)
		}
	}
}

func TestRecordAndTurnWrappers(t *testing.T) {
	requests := make(chan map[string]any, 5)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		body["path"] = r.URL.Path
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"results":[{"deleted":["fact-1"]}],"record":{"id":"fact-1","version":2},"context":"memory"}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, "secret", "alice", nil)
	ctx := context.Background()
	record := map[string]any{"id": "fact-1", "kind": "fact", "version": 2, "payload": map[string]any{"text": "temporary fact"}}
	put, err := client.PutRecord(ctx, record)
	if err != nil || put["record"].(map[string]any)["version"] != float64(2) {
		t.Fatalf("put: %v %v", put, err)
	}
	request := <-requests
	if request["path"] != "/v1/memory/record-put" || request["record"].(map[string]any)["version"] != float64(2) || request["record"].(map[string]any)["payload"].(map[string]any)["text"] != "temporary fact" {
		t.Fatal(request)
	}
	if _, err = client.SearchRecords(ctx, "temporary", 3); err != nil {
		t.Fatal(err)
	}
	request = <-requests
	if request["query"] != "temporary" || request["limit"] != float64(3) {
		t.Fatal(request)
	}
	if _, err = client.ForgetRecord(ctx, "fact-1", "retention"); err != nil {
		t.Fatal(err)
	}
	request = <-requests
	if request["id"] != "fact-1" || request["reason"] != "retention" {
		t.Fatal(request)
	}
	if _, err = client.ReapDueRecords(ctx, 16); err != nil {
		t.Fatal(err)
	}
	request = <-requests
	if request["path"] != "/v1/memory/record-reap-due" || request["limit"] != float64(16) {
		t.Fatal(request)
	}
	options := map[string]any{"text": "hello", "budget": 0, "event": "SessionEnd", "payload": map[string]any{"succeeded": true}, "session_id": "spoof"}
	if _, err = client.Turn(ctx, "session", "query", options); err != nil {
		t.Fatal(err)
	}
	request = <-requests
	if request["session_id"] != "session" || request["query"] != "query" || request["event"] != "SessionEnd" || request["budget"] != float64(0) || request["payload"].(map[string]any)["succeeded"] != true {
		t.Fatal(request)
	}
	if options["session_id"] != "spoof" {
		t.Fatal("caller options mutated")
	}
	for _, limit := range []int{0, -1, 101} {
		if _, err = client.ReapDueRecords(ctx, limit); err == nil {
			t.Fatalf("accepted invalid limit %d", limit)
		}
	}
	if len(requests) != 0 {
		t.Fatal("invalid request reached server")
	}
}
