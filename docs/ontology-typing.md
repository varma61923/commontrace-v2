# Ontology typing and the self-building graph

CommonTrace types every entity it extracts into the store's ontology and keeps the
knowledge graph in step with the fact store on its own. This page describes how the
typing decides, how the graph is projected from facts, and what was measured.

## How a mention is typed

`ontology_classify.classify(name, context, onto=...)` scores every declared type
with independent kinds of evidence. Within one kind only the strongest score per type
counts; across kinds the scores combine by noisy-OR, so two independent cues agreeing
are stronger than either alone.

| Evidence | Example | Score |
|---|---|---|
| `alias` | an ontology alias (`service:postgres: [pg]`) | 0.95 |
| `shape` | `src/app/main.py`, `parse_header`, `KeyError`, `ECONNRESET`, `404 Not Found`, `RFC 7231`, `us-east-1`, `Sprint 14`, `Initech Corp` | 0.6-0.95 |
| `keyword` | a type word as head of the name ("Orders Database", camelCase "OmniQueue") | 0.75-0.92 |
| `lexicon` | well-known names: places, organizations, tools, datastores, events, error terms; first names inside a full name | 0.7-0.88 |
| `pattern` | the context states the type: "Velmora, a small town", "X is a library", "frameworks such as X", "the lead architect is X" | 0.75-0.9 |
| `cue` | what the context says the mention does: "Brannigan approved", "lives in X", "X signed a contract", "Run X", "raised a X", "during X", "X defines" | 0.6-0.85 |
| `exemplar` | how the store already typed the same name, or three or more names with the same head word | 0.65-0.85 |
| `form` | a weak person prior for a name shaped like a personal name (needs any other cue) | 0.55 |
| `embedding` | the mention in context against each type's description, with an abstain prototype and a margin rule | 0.5-0.9 |
| `llm` | an optional completion whose answer must be a declared type | model-reported |

Selection takes the best score; ties go to the more specific type, then to the more
reliable kind of evidence (a mention's own form beats what the context happens to
say). Two near-tied sibling types back off to their nearest common declared parent
instead of guessing. Below the threshold (0.6) the mention stays untyped. One-word
abstract nouns ("Observability", "Latency") get context evidence damped, and a type
noun must end its noun phrase ("a standard error code" is a code, not a standard).

Models are consulted only when the offline evidence is undecided. `classify_many`
embeds every undecided mention of a batch in one call. In-process embedders are
loaded once per tag.

Extraction (`entities.extract`) feeds the classifier. Capitalised names, including
camelCase and accented ones; shapes found in running text; a single pass over
well-known names (no regex per name); noun phrases ending in a type word; and words
after "run"/"use". A weak candidate, such as a sentence-initial word, survives only if
the classifier types it, and a sentence's opening verb is trimmed ("Follow the Style
Guide" yields "Style Guide").

## The graph builds itself

`graph_sync` projects every active fact into the graph:

- a `memory:<fact id>` node, linked by `mentions` edges to the typed entities it names;
- the relations it states, as typed edges (offline patterns, or an LLM with `--llm`);
- provenance naming the fact on every write.

It is incremental and idempotent: only new facts, or facts whose statement or scopes
changed, are processed. When a fact stops being current (superseded, invalidated,
deleted, forgotten or expired), the edges it produced are closed at the time it ended,
so the graph and the fact store share one history. When a fact asserts an exclusive
relation (`works_at`, `located_in`, `owned_by`, `status`, or any relation the ontology
marks exclusive) that ends an earlier fact's value, the two facts are recorded as an
`exclusive` contradiction. With `COMMONTRACE_FACT_CONFLICTS=supersede` the earlier fact
is superseded through the usual temporal guard.

A full pass over 2,000 facts takes 5.0 s on one core (2.5 ms per fact, including
typing, mentions, relations and provenance); an incremental pass is one fact's cost.

The hook broker (`commontrace up`) and the daemon's pass run it whenever the fact
store changed; when nothing changed the check costs two `stat` calls.
`commontrace graph sync` runs a pass on demand, and `COMMONTRACE_GRAPH_AUTO=off`
turns the automatic runs off.

## Measurements

`python -m benchmarks.ontology_typing_eval --fixture <file>` scores two views:

- `classifier`: the classifier on (name, context);
- `pipeline`: the type the extractor gives the mention, which is what the graph
  stores.

The baseline is the classifier and extractor before this change; both views run on
the same rows.

| Fixture | Rows | Baseline classifier | New classifier | Baseline pipeline | New pipeline | New typed precision |
|---|---|---|---|---|---|---|
| `ontology_typing_v1.json`: hand-written, rules tuned on it | 124 | 0.185 | 0.992 | 0.355 | 0.976 | 0.991 |
| `ontology_typing_gemma_dev.json`: model-written, rules generalised on it | 132 | 0.197 | 0.924 | 0.417 | 0.917 | 0.973 |
| `ontology_typing_gemma_test.json`: model-written, **never inspected or tuned on** | 107 | 0.131 | 0.748 | 0.280 | 0.738 | 0.914 / 0.924 |

The test-set row is the honest estimate of offline generalisation: about three in
four mentions correctly typed with no model, and over nine in ten typed answers
correct. The abstract slice of the test set dropped from 1.00 to 0.86: some abstract
topics now receive a type.

On the dev set, local embedders (bge-small via fastembed, arctic-m) did not improve
accuracy over the offline evidence. They only stopped hurting at a margin of 0.08,
which is the shipped default; below 0.05 they typed abstract topics. The embedding
tier is a fallback, not where the accuracy comes from.

Both Gemma fixtures were generated by `gemma-4-31b-it` from a fixed prompt, and their
labels are the type the model was asked to write, unedited.

With the LLM tier enabled (`gemma-4-31b-it`, consulted only for mentions the offline
evidence left undecided), test-set accuracy was 0.907: 26 of 107 mentions (24%)
needed a model call, taking 852 s in total on the hosted API. The same model wrote
these labels, so this number flatters the LLM tier; treat it as an upper bound for a
comparable model, not a measured gain on independent data.

## Relation extraction

Offline extraction combines two strategies. Adjacent phrasings link two names joined
by a verb ("Payments Service depends on Redis"). Entity-anchored triggers link the
nearest typed entities on either side of a relation trigger across a few words, in
active or passive voice ("The VoidMapper leverages the NebulaDB cluster", "OmniRoute,
which handles all ingress traffic, requires QuarkSync", "X was utilized by Y"). Negated,
hedged and planned statements ("may depend on", "does not use", "will replace") never
produce a triple. The longest trigger phrase owns its span, so a passive phrase does
not also fire its inner verb in the opposite direction. When a component and its error
are both named, the component is the party ("a kernel panic in the VoidOS kernel").

`python -m benchmarks.relation_extraction_eval --fixture <file>` scores triples
strictly (exact names) and leniently (one name contains the other). Every triple from
a negative sentence is a false positive. The fixtures were written by
`gemma-4-31b-it` with unedited labels: 72 positive and 24 negative sentences each, a
dev set in documentation style and a test set in Slack, incident and review style.
The rules were tuned on the dev set only; the test set was scored once.

| Fixture | Baseline F1 (strict / lenient) | New F1 (strict / lenient) | New lenient P / R | Negatives clean |
|---|---|---|---|---|
| `relation_extraction_gemma_dev.json` | 0.337 / 0.382 | 0.783 / 0.927 | 0.970 / 0.889 | 24/24 |
| `relation_extraction_gemma_test.json` (never tuned on) | 0.209 / 0.279 | 0.672 / 0.797 | 0.911 / 0.708 | 24/24 |

Weakest on the test set: employment phrased conversationally (`works_at` recall 1/6),
because the person's name must be typed as a person before employment is asserted.

