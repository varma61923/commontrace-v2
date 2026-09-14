---
id: 2026-07-01_example-trace
title: "Fix missing optional argon2 dependency crash in hub auth"
agent_type: code
agent_id: ""
tags:
  - python
  - hub
  - auth
  - imports
profile: ""
created_at: "2026-07-01T14:00:00Z"
watch_condition: ""
review_after: ""
supersedes_trace_id: ""
outcome:
  resolved: true
  escalated: false
  repeated_error: false
  frustration_signal: false
  tokens_used: 1450
  llm_calls: 3
  baseline: false
---

## Context
When running regression tests in environments without argon2-cffi installed, importing hub.crud failed during module loading.

## Solution
Wrapped the argon2 import in a try/except ImportError block in hub/auth.py and made argon2 verification gracefully report inability to verify legacy hashes when the library is not installed.
