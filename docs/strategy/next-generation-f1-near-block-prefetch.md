# F1 near-block prefetch candidate: not promoted

Profiles showed one `SELECT 1` block-presence query per candidate statement in
`StatementIndex.near()`. A transaction-local candidate precomputed the batch's
numeric/negation blocks, fetched active blocks in groups of 200, and marked
staged active rows present. A focused 1,000-fact SQL trace replaced 1,000 point
probes with five bounded prefetches; same-batch near matching, removal/rebuild,
and strongest-match tests passed while the candidate was installed.

The unchanged 30K storage harness did not show a throughput improvement. The
baseline batch measured 2,291.688 facts/s and the candidate measured 2,261.888
facts/s, with one measured 1,000-fact batch per source. These sequential runs
are diagnostic only and do not establish a causal slowdown or gain. Their raw
reports and hashes are preserved in
[baseline](measurements/f1-near-block-prefetch-baseline-30k.json) and
[candidate](measurements/f1-near-block-prefetch-candidate-30k.json). Since the
candidate added preprocessing without a measured lift, it was removed. The
experiment is not acceptance evidence. A later detached-snapshot candidate
cleared the exact 1M write thresholds in one synthetic run; see the
[snapshot-reuse note](next-generation-f1-snapshot-reuse.md) and its
[source-hashed 1M report](measurements/f1-snapshot-reuse-candidate-1m.json).

The baseline and candidate used the same `91d0fb8` commit and benchmark command
with `--sizes 30000 --trials 5 --legacy-max 0`; source-file hashes in each
report identify the working-tree implementation measured.
