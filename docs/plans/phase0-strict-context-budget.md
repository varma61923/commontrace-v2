# Phase 0: strict selected text-counter budgets

The pinned 154-question LoCoMo development baseline has 79 exact cl100k text
count exceedances at the nominal 1,000 estimated-unit budget. Exact mean context
size is 1,000.8831 text tokens. This is a measured mismatch between the product's
published estimate contract and target C's desired hard text budget; it is not a
reader-accuracy result or a full-split score.

Preserve that baseline. Add an opt-in `--strict-context-budget` requiring a named
`--tokenizer` to the existing harness, after the active frozen sweep finishes.
All capped memory/history profiles must return a counted fitting context before
reader dispatch. Full-context remains explicitly uncapped.

The controller lowers the native assembly budget and reruns the complete public
path. It retains the final native selection, attribution and proof; it never
truncates a returned context. Directive preservation depends on the native
assembler's contract; this helper does not introduce stronger guarantees. Every attempt is
counted, budgets strictly decrease, and a finite limit fails closed when no
fitting result is found. Nonmonotonic assembly means exhaustion does not prove
no fitting result exists. Adaptive recall time counts as retrieval cost.

Bind counter/enforcement settings and helper source to provenance. Preserve the
default estimate-based contract. No core or protocol API or mandatory dependency
changes. Validate real-store packing, reference modes, incompatible comparison
refusal, failure before reader calls and final source mapping. Exercise pinned
text counts on confirmation questions excluded from this change's development
selection; report their limits separately from judged accuracy.

Rollback selects the earlier harness and fresh outputs. Strict and estimated
budget runs have different evaluation contracts and cannot be silently paired.
