# Anticipatory indexing

People rarely ask about a memory in the words that created it. "What might John's
degree be in?" shares no word with "I've been loving my policy classes", so neither
keyword nor embedding search reliably connects them. Anticipatory indexing spends
model time while the store is idle (sleep-time compute) so that queries need none:
for each stored message, the configured LLM writes the short questions the message
answers, with names and dates resolved ("I" becomes the speaker, "yesterday" becomes
2023-05-07), plus the keywords a searcher would use. These are indexed as extra
search units of that same message.

```bash
commontrace conversation anticipate <space> [--limit N] [--workers 4] [--json]
COMMONTRACE_ANTICIPATE=on   # also run it in the daemon's idle pass (240 messages per space per pass)
```

## Guarantees

- Generated text is a retrieval key, never evidence. Context lines and excerpts are
  always the message's own text; the generated units are never shown.
- The units are deleted with their message, so forgetting, expiry and session
  deletion remove them too (`anticipated` rows cascade the same way).
- Output that trips the injection screen is discarded. A batch the model fails on
  stays pending for the next pass; finished messages are never sent again.
- Nothing runs without a configured LLM, and the daemon only runs it when
  `COMMONTRACE_ANTICIPATE=on`, because it spends model calls.
- Query-time cost is unchanged: no model is consulted when answering.

## Measurement

LoCoMo, 1,500-token budget, lexical retrieval only (no embedding model at query
time), `gemma-4-31b-it` writing the keys. The model saw only stored messages, never
the benchmark questions. The provider's quota stopped generation after three of the
ten conversations, so the comparison is on those three, against the identical store
without generated units; the seven untouched conversations are a control.

| Questions | Evidence recall before / after | Complete before / after |
|---|---|---|
| Indexed conversations (26, 30, 41), 383 questions | 0.802 / **0.862** | 0.755 / **0.823** |
| Untouched conversations, 1,153 questions (control) | 0.782 / 0.782 | 0.712 / 0.712 |

The paired gain is +0.060 evidence recall (95% bootstrap interval +0.035 to +0.085);
37 questions improved and 7 got worse, and each of the three conversations improved
(+0.069, +0.033, +0.064). By question type: open-domain +0.136 (19 questions),
multi-hop +0.112 (74), single-hop +0.050 (200), temporal +0.022 (90). Median recall
latency was 8.4 ms either way.

Three conversations are a small sample, and some messages stayed pending when the
quota ran out (12, 9 and 51), so treat the size of the gain as preliminary.

## Personalized PageRank (opt-in)

`commontrace/conversation/graph_walk.py` runs a HippoRAG-style Personalized
PageRank over the message-entity graph, restarting at the question's entities and
the best matches, with each entity weighted by its specificity. On LoCoMo it lowered
evidence recall at every weight tried (0.15 to 0.6, about one point), including on
multi-hop questions: conversation entities are mostly the two speakers' names, so
the walk spreads mostly noise. It therefore ships off. It may suit entity-dense
corpora (projects, services, people across documents); enable it with
`COMMONTRACE_GRAPH_WALK=<weight>` or `Options(graph_walk=...)` and measure first.
