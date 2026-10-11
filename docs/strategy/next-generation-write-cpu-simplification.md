# Remove overhead from pure write helpers

P0 remains open. Eight binding-only diagnostics on frozen `cd8c1dc` retain
all baseline and candidate rounds. Baseline batches reach 1,713/1,694 facts/s;
recomputing small content-word sets reaches 1,722/1,792; reusing a configured
JSON encoder reaches 1,801/1,731; combining them reaches 1,794/1,830.
All miss 2,000. Process CPU per combined batch is 0.532/0.520 seconds versus
0.573/0.566 baseline. A growing copy and concurrent regression load prevent
a controlled causal throughput claim.

Remove content-set memoization: its set-key validation, weight accounting and
retention cost exceed the work saved on these short facts. Keep content values
immutable, computed by the existing tokenizer/stopword rules. The remaining
normalization/signature/contradiction-slot cache accepts exact statement strings
only, with the same 4,096-entry/8-MiB accounting and transaction cleanup.
Count string children directly while preserving the same conservative retained
weight; avoid recursive calls for every word.

Use one private configured stdlib JSONEncoder for compact sorted Unicode JSON.
Configuration never changes; each encode call creates its own circular-reference
markers and traversal state. Preserve bytes, supported values, exception behavior
and final MemoryRecord validation. Test concurrent encodes, negative zero, Unicode,
non-finite values and cyclic/unsupported inputs before the runtime change, plus
the absence of retained content keys and unchanged weight accounting.

Measure frozen production source after contracts pass, preserving every round.
No cache budget, durability setting, deadline or acceptance threshold changes.

Remove the redundant rowid reselect after inserting new fact metadata. Read
the INSERT cursor's lastrowid before publishing FTS, while retaining the
already-known original rowid on the UPSERT update branch. Python documents
[lastrowid](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Cursor.lastrowid)
for successful INSERT execution; SQLite's
[last-insert semantics](https://www.sqlite.org/c3ref/last_insert_rowid.html)
do not identify an UPSERT's update target. Test an update after an unrelated
insert so that a stale last-insert value cannot redirect postings or FTS.
Keep the existing schema and SQLite version requirements.
