// Package commontrace implements the same memory API for local and hosted gateways.
package commontrace

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
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
	case "add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose":
	default:
		return nil, fmt.Errorf("unknown memory operation")
	}
	body := map[string]any{}
	for key, value := range data {
		body[key] = value
	}
	body["context"] = append([]string{}, c.scopes...)
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", c.base+"/v1/memory/"+operation, bytes.NewReader(encoded))
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
