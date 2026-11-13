// Package unimem implements the same memory API for local and hosted gateways.
package unimem

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	base, token string
	scopes      []string
	http        *http.Client
}

func New(base, token, agent string, scopes []string) (*Client, error) {
	u, err := url.Parse(base)
	if err != nil {
		return nil, err
	}
	loopback := u.Hostname() == "localhost" || u.Hostname() == "127.0.0.1" || u.Hostname() == "::1"
	if u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Host == "" || (u.Scheme != "https" && !(u.Scheme == "http" && loopback)) {
		return nil, fmt.Errorf("use HTTPS or a loopback gateway without embedded credentials")
	}
	labels := append([]string(nil), scopes...)
	if agent != "" {
		labels = append(labels, "agent:"+agent)
	}
	return &Client{strings.TrimRight(base, "/"), token, labels, &http.Client{Timeout: 30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func (c *Client) Call(ctx context.Context, operation string, data map[string]any) (map[string]any, error) {
	switch operation {
	case "add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose", "turn", "record-put", "record-search", "record-forget", "record-reap-due":
	default:
		return nil, fmt.Errorf("unknown memory operation")
	}
	return c.request(ctx, "memory", operation, data)
}
func (c *Client) request(ctx context.Context, category, operation string, data map[string]any) (map[string]any, error) {
	body := map[string]any{}
	for key, value := range data {
		body[key] = value
	}
	body["context"] = append([]string{}, c.scopes...)
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	method, endpoint := "POST", c.base+"/v1/"+category+"/"+operation
	var payload io.Reader = bytes.NewReader(encoded)
	if category == "learning" && (operation == "capabilities" || operation == "status") {
		query := url.Values{}
		if experiment, ok := data["experiment_id"]; ok {
			value, valid := experiment.(string)
			if !valid {
				return nil, fmt.Errorf("experiment_id must be a string")
			}
			query.Set("experiment_id", value)
		}
		for _, label := range c.scopes {
			query.Add("context", label)
		}
		endpoint += "?" + query.Encode()
		method, payload = "GET", nil
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, payload)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	response, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("gateway HTTP %d", response.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 8*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > 8*1024*1024 {
		return nil, fmt.Errorf("memory response too large")
	}
	var result map[string]any
	err = json.Unmarshal(raw, &result)
	if err == nil && result == nil {
		return nil, fmt.Errorf("memory response must be an object")
	}
	return result, err
}
func (c *Client) Add(ctx context.Context, text string) (map[string]any, error) {
	return c.Call(ctx, "add", map[string]any{"text": text, "local": true})
}
func (c *Client) Search(ctx context.Context, query string) (map[string]any, error) {
	return c.Call(ctx, "search", map[string]any{"query": query})
}
func (c *Client) Reflect(ctx context.Context, query, occasion string, budget int) (map[string]any, error) {
	return c.Call(ctx, "reflect", map[string]any{"query": query, "occasion_id": occasion, "budget": budget})
}
func (c *Client) Profile(ctx context.Context, query string) (map[string]any, error) {
	return c.Call(ctx, "profile", map[string]any{"query": query})
}
func (c *Client) Outcome(ctx context.Context, occasion string, succeeded bool) (map[string]any, error) {
	return c.Call(ctx, "outcome", map[string]any{"occasion_id": occasion, "succeeded": succeeded})
}

// OutcomeReward reports a graded outcome in [0, 1], realized by the gateway's audited randomized rounding.
func (c *Client) OutcomeReward(ctx context.Context, occasion string, reward float64) (map[string]any, error) {
	if math.IsNaN(reward) || reward < 0 || reward > 1 {
		return nil, fmt.Errorf("reward must be a finite number between 0 and 1")
	}
	return c.Call(ctx, "outcome", map[string]any{"occasion_id": occasion, "reward": reward})
}

// OutcomeMetrics reports named measurements that the store's configured utility scores into a reward.
func (c *Client) OutcomeMetrics(ctx context.Context, occasion string, metrics map[string]float64) (map[string]any, error) {
	if len(metrics) == 0 || len(metrics) > 64 {
		return nil, fmt.Errorf("metrics must hold 1 to 64 numbers")
	}
	for name, value := range metrics {
		if math.IsNaN(value) || math.IsInf(value, 0) {
			return nil, fmt.Errorf("metric %q must be finite", name)
		}
	}
	return c.Call(ctx, "outcome", map[string]any{"occasion_id": occasion, "metrics": metrics})
}

// PutRecord stores a versioned envelope without changing its payload or lineage.
func (c *Client) PutRecord(ctx context.Context, record map[string]any) (map[string]any, error) {
	return c.Call(ctx, "record-put", map[string]any{"record": record})
}

func (c *Client) SearchRecords(ctx context.Context, query string, limit int) (map[string]any, error) {
	return c.Call(ctx, "record-search", map[string]any{"query": query, "limit": limit})
}

func (c *Client) ForgetRecord(ctx context.Context, id, reason string) (map[string]any, error) {
	return c.Call(ctx, "record-forget", map[string]any{"id": id, "reason": reason})
}

// ReapDueRecords erases 1..100 due roots; dependent records may also be erased.
// The hosted operation requires an admin credential.
func (c *Client) ReapDueRecords(ctx context.Context, limit int) (map[string]any, error) {
	if limit < 1 || limit > 100 {
		return nil, fmt.Errorf("expiration batch must be in 1..100")
	}
	return c.Call(ctx, "record-reap-due", map[string]any{"limit": limit})
}

// Turn captures and retrieves in one request. Options may include text, budget,
// event and payload; session, query and client scope stay pinned.
func (c *Client) Turn(ctx context.Context, session, query string, options map[string]any) (map[string]any, error) {
	body := map[string]any{}
	for key, value := range options {
		body[key] = value
	}
	body["session_id"], body["query"] = session, query
	return c.Call(ctx, "turn", body)
}
