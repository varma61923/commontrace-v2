package unimem

import (
	"context"
	"fmt"
)

const LearningSchema = "unimem.learning.v1"

type LearningClient struct{ client *Client }

func (c *Client) Learning() *LearningClient { return &LearningClient{client: c} }
func learningReply(reply map[string]any, err error) (map[string]any, error) {
	if err != nil {
		return nil, err
	}
	if reply["schema"] != LearningSchema {
		return nil, fmt.Errorf("peer does not support %s", LearningSchema)
	}
	return reply, nil
}
func (l *LearningClient) Capabilities(ctx context.Context) (map[string]any, error) {
	return learningReply(l.client.request(ctx, "learning", "capabilities", nil))
}
func (l *LearningClient) Call(ctx context.Context, operation string, body map[string]any) (map[string]any, error) {
	switch operation {
	case "capabilities":
		return l.Capabilities(ctx)
	case "configure", "assign", "delivery", "outcome", "run", "status", "rollback":
	default:
		return nil, fmt.Errorf("unknown learning operation")
	}
	capabilities, err := l.Capabilities(ctx)
	if err != nil {
		return nil, err
	}
	supported := false
	if operations, ok := capabilities["operations"].([]any); ok {
		for _, op := range operations {
			if op == operation {
				supported = true
			}
		}
	}
	if !supported {
		return nil, fmt.Errorf("peer does not support learning %s", operation)
	}
	return learningReply(l.client.request(ctx, "learning", operation, body))
}
func (l *LearningClient) Configure(ctx context.Context, mode string, options map[string]any) (map[string]any, error) {
	body := copyLearning(options)
	body["mode"] = mode
	return l.Call(ctx, "configure", body)
}
func copyLearning(input map[string]any) map[string]any {
	body := map[string]any{}
	for k, v := range input {
		body[k] = v
	}
	return body
}
func (l *LearningClient) Assign(ctx context.Context, occasion string, ids []string, options map[string]any) (map[string]any, error) {
	body := copyLearning(options)
	body["occasion_id"], body["memory_ids"] = occasion, ids
	return l.Call(ctx, "assign", body)
}
func (l *LearningClient) Delivery(ctx context.Context, occasion string, ids []string, experiment string) (map[string]any, error) {
	if ids == nil {
		ids = []string{}
	}
	body := map[string]any{"occasion_id": occasion, "memory_ids": ids}
	if experiment != "" {
		body["experiment_id"] = experiment
	}
	return l.Call(ctx, "delivery", body)
}
func (l *LearningClient) Outcome(ctx context.Context, occasion string, outcome map[string]any, experiment string) (map[string]any, error) {
	if len(outcome) != 1 {
		return nil, fmt.Errorf("exactly one learning outcome required")
	}
	for k := range outcome {
		if k != "succeeded" && k != "reward" && k != "metrics" {
			return nil, fmt.Errorf("unknown outcome")
		}
	}
	body := copyLearning(outcome)
	body["occasion_id"] = occasion
	if experiment != "" {
		body["experiment_id"] = experiment
	}
	return l.Call(ctx, "outcome", body)
}
func (l *LearningClient) Run(ctx context.Context, options map[string]any) (map[string]any, error) {
	return l.Call(ctx, "run", options)
}
func (l *LearningClient) Status(ctx context.Context, options ...map[string]any) (map[string]any, error) {
	if len(options) > 1 {
		return nil, fmt.Errorf("status accepts at most one options map")
	}
	var body map[string]any
	if len(options) == 1 {
		body = options[0]
	}
	return l.Call(ctx, "status", body)
}
func (l *LearningClient) Rollback(ctx context.Context, reason string, options map[string]any) (map[string]any, error) {
	body := copyLearning(options)
	body["reason"] = reason
	return l.Call(ctx, "rollback", body)
}
