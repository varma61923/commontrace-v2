---
name: prove-memory
description: Start, check and verify a randomized proof that a memory helps. Use when the user wants evidence that learned memory changes outcomes, or asks whether a memory is hurting.
---

A proof withholds each memory from a random share of tasks and compares outcomes. It needs the user's volume and a
definition of success (the function kit has a default).

```bash
commontrace proof wizard <function> --label <customer-or-fleet> --daily <tasks per day>   # plan, confirm, start
commontrace proof wizard <function> --label rehearsal --daily 100 --yes --simulate --dest /tmp/rehearsal   # a dry run
commontrace gateway            # any agent or robot calls POST /v1/recall then POST /v1/outcome
commontrace proof status       # progress, whether it can be trusted, each memory's verdict
commontrace proof report       # index.html to share, proof.json and assignments.csv
commontrace proof verify <dir> # recompute everything from the raw rows
```

Rules that matter: report every outcome under the same occasion id used at recall; never mark safety constraints
for withholding (use `--protect <prefix>` on the gateway); a COMPROMISED integrity verdict means no effect may be
quoted. Tell the user a verdict of "not yet decided" means more data is needed, not that the memory does nothing.
