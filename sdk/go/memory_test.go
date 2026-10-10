package commontrace

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
