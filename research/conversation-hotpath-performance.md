# Linear conversation preprocessing

Baseline: `c8ce5c792916c10e23b0618c59259960294b7bcc`. The full samples,
environment, source hashes and output hashes are in
`conversation-hotpath-performance.json`. No model or network calls occur.

| Workload | Baseline median | Candidate median | Speedup |
| --- | ---: | ---: | ---: |
| Split a 1,000,000-character flat paragraph into 1,438 units | 56.491 ms | 1.249 ms | 45.2× |
| Ground 2,000 weekday mentions in one 30,019-character clause | 3,956.408 ms | 19.677 ms | 201.1× |

Both paths produce identical complete output hashes. Each measurement contains
five samples. The repeated baseline is also retained; its chunking median is
higher, so the table uses the earlier baseline. Scheduling and hardware affect
these synthetic measurements; they establish reduced work on these specific
inputs, not a universal speedup or improved answer accuracy.

`split_units` previously copied and stripped the remaining paragraph after
every unit. It now advances offsets, emits bounded slices, and skips the
sentence-separator regex when no separator character exists. Sentence boundaries,
Unicode whitespace, unit packing and maximum length retain their original
semantics. Nonpositive sizes now raise rather than entering an infinite loop.

Weekday grounding previously rescanned the surrounding clause for every match.
It now indexes punctuation once and evaluates future/past patterns once per
relevant clause. The cache belongs to one invocation and disappears afterward;
it never retains an interaction globally. The original punctuation-only clause
and tense rules remain intact, including semicolons and newlines.

Reproduce from the candidate checkout, using a separate baseline checkout:

```sh
git worktree add --detach /tmp/commontrace-phase6-before c8ce5c792916c10e23b0618c59259960294b7bcc
python research/profile_conversation_hotpaths.py /tmp/commontrace-phase6-before --runs 5
python research/profile_conversation_hotpaths.py "$PWD" --runs 5
python -m pytest tests/test_conversation_hotpaths.py tests/test_conversation_memory.py tests/test_mcp_read_workers.py -q
```

Tests compare 500 seeded randomized Unicode/punctuation chunking inputs to the
frozen previous behavior, verify complete long-turn retention, and check that
1,000 repeated mentions invoke each tense matcher exactly once. Boundary
differential tests cover positions immediately beside punctuation. Document
retrieval and guarded SQL now run in owned worker threads; concurrent MCP
requests remain responsive, while catalog registration and store-root checks
still run before returning evidence or executing SQL.
