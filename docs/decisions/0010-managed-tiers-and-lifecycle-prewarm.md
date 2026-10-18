# UniMem scoped payload tiers and explicit lifecycle pre-warm

Move managed namespace payloads, retaining canonical authority and audit data
locally. Do not blindly migrate legacy files containing multiple tenant scopes.
Require a residency context for every managed read/write, including dependency
reads, and inherit actual source scopes. Region tags describe configured
placement rather than physical-location proof.

Verify all copies before withdrawal. Commit cold/hot authority before deleting
the previous tier, and preserve pending cleanup for retries. Serialize migration,
put and erasure with the namespace journal lock. Delete all managed cold versions
on source erasure; cloud versions/backups require separate verification.

Keep pre-warm opt-in at session/robot lifecycle boundaries. Report actual local
latency percentiles rather than a claim about arbitrary cloud latency. Push ACLs
into native indexes and re-admit returned IDs; exact-backend equivalence is not
an ANN recall guarantee.

Model adapters supply bounded vectors and held-out queries. Immutable generations
and an atomic pointer make cutover and rollback reviewable. A recall-delta estimate
can refuse a regression; it does not authorize causal promotion.
