# Incident freeze and evidence packs

Use `unimem incident freeze CASE --valid-time ISO_TIME --dest STORE` to pin
valid-time facts, record-time metadata, lesson bodies/revisions, retrieval receipts,
delivered dream-rewrite texts and authenticated lineage. Facts are pinned twice: as
believed at the incident (record time = valid time) and as known at the freeze,
so the timeline separates what was believed then from what was corrected later.
The evidence is written as content-addressed parts under one signed manifest; every
part is digest-checked whenever the snapshot is read. Later corrections continue normally. A signed legal
hold blocks forgetting and hard erasure of pinned records until `incident release
CASE --reason REASON` is explicitly recorded.

`incident timeline CASE` and MCP `incident_timeline` show the frozen receipts and
later corrections. `incident.replay(root, case, occasion_id)` reconstructs the
actual injected lesson revision from the frozen current bodies and revision
history. Missing historical bytes raise an error; no replay is fabricated.

`incident pack CASE --regime eu-ai-act|iso42001|nist|gdpr22|pld` emits a signed
manifest, file digests, evidence/control mappings and a transparency inclusion
receipt. `incident.verify_pack` checks it offline using a store with a pinned
verifier key. HMAC verification requires the shared key; Ed25519 needs only the
pinned public key. Keep/witness tree heads to check current revocation or forks.

Hub operators configure the ledger signer and use authenticated
`POST /v1/incidents` with `{id, valid_time}`. Read routes are
`/v1/incidents/{id}/timeline`, `/v1/incidents/{id}/pack` and the workflow page
`/v1/incidents/console`. Snapshots and holdouts are tenant-scoped; a freeze places
an existing organization legal hold, including retention and explicit record/trace
erasure. Hub pack exports carry a manifest and inclusion receipt, verifiable by
`hub.audit_export.verify_incident_pack` with independently pinned principals.

Mappings are evidence organization aids, not a compliance certification or legal
opinion. Verify wording and applicability against the Official Journal and the
published ISO/NIST standards. A local snapshot is bounded at 32 MiB per part and
64 MiB in total; an oversized store is refused explicitly. The Hub snapshot keeps,
per evidence type, the newest 1000 rows recorded at or before the valid time,
flags `older_rows_omitted` when more exist, and pins bulky columns (record bodies,
audit summaries) by digest; the organization legal hold preserves the complete
tables. Unlogged historical retrieval content cannot be reconstructed.

The signed-in Hub console includes **Incidents** at `/app/incidents`.
Administrators can freeze the recorded state and place its legal hold through the
form, then download a signed evidence pack. Read-only members can inspect their
organization's packs. Writes retain the console's origin checks; another tenant
cannot fetch the snapshot. Configure the ledger signing key before freezing.
