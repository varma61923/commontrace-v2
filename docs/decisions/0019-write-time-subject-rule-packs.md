# 0019: Regulated-subject rules precede canonical persistence

Operator scope bindings fix subject class and deployment context. Ingest supplies
bounded modality and semantic tags. All applicable packs evaluate at the shared
write boundary, before records become canonical; blocked content is represented
by commitments in the review ledger. The implementation records decisions, not
legal conclusions or proof of sensor provenance.

Child retention is an independent signed schedule, clipped by parental-consent
expiry and monotonically shortened across revisions. Retrieval checks it before
use, and the existing retention maintenance job physically purges expired
sources. Consent withdrawal and purpose expiry can revoke use sooner. Missing
vault or external evidence remains explicit rather than producing an erasure
certificate. Source class/context and medical exceptions cannot be supplied as
self-authorizing memory payload fields.
