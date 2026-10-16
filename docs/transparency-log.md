# Memory transparency log

New origin-bound writes, lesson writes and approvals, revocations, schedule
publications, lineage taints and incident operations publish signed statements.
The log uses RFC 6962 leaf/node domain separation, inclusion proofs, signed
checkpoints and logarithmic consistency proofs. Existing record hashes do not
change. The leaf journal is authoritative; the node cache is disposable.

Use `commontrace log head`, `log prove --leaf N`, `log consistency --from N`,
and `log verify --receipt FILE`, with `--dest STORE`. MCP `log_verify` checks
membership and signatures against the store's pinned identity. Head exports can
be co-signed by an external witness. Keep the old checkpoint and require a
consistency proof before accepting a new one.

HMAC is a shared-secret MAC, not public non-repudiation. Configure the store's
Ed25519 identity with `commontrace assurance signing --algorithm ed25519` and
install `commontrace[security]` for public verification. Verifiers must obtain
the public key through a trusted channel, not from an untrusted receipt.

On the Hub, configure `HUB_LEDGER_SIGNING_KEY` (at least 32 bytes) and optionally
`HUB_TRANSPARENCY_ALGORITHM=ed25519`. Audited writes append transactionally in
organization order. Read-only, authenticated routes are `/v1/log/head`,
`/v1/log/proof?leaf=N`, and `/v1/log/consistency?from=N&to=M`. Tenant tables use
forced row-level security and composite organization keys. An unconfigured
signer returns 503 rather than an unsigned guarantee.

A private log cannot detect a completely hidden or rolled-back history without
a pinned checkpoint or a witness. Inclusion proves membership, not truth.
Truncation against a retained checkpoint, reordering and invalid signatures fail
closed. Rotate bounded journals before 100000 rows or 64 MiB.

Writes are durable by default. The gateway's existing explicit `durable=False`
mode also skips transparency fsyncs and the disposable disk-node cache. Signatures
and inclusion proofs still authenticate the bytes written, but relaxed mode does
not promise persistence through a machine crash. Use the default for production
evidence retention.
