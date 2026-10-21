# Subject-key erasure and conditional certificates

Create a `certified_erasure.SubjectVault(store_root, vault_directory)` outside
content-store backups. Seal each subject's content before it is backed up. Each
envelope has a data key wrapped by an isolated subject key. The Hub's
`EnvelopeCipher.encrypt_subject` adds its existing envelope-key layer too. KMS
providers implement the independent destruction boundary; `hub.kms` exposes a
filesystem reference and the destruction interface.

`certified_erasure.erase` checks incident holds and hazards before any destructive
step, revokes the supplied records and their registered lineage descendants,
purges canonical facts/lesson histories and graph mirrors, and destroys the
subject wrapping key. It returns a signed certificate with a transparency anchor,
a hash tombstone, explicit live-probe status and backup scope. `verify` checks its
bytes and anchor offline. Unrelated subjects retain their own keys. Dream-rewrite
texts derived from erased lessons are deleted too; their journal keeps only digests.

The vault must be bound to the same store being erased. The reason, subject,
request fields and optional probe configuration are validated before deletion;
invalid requests preserve both source content and subject keys.

From the CLI, `unimem frontier certified-erase REQUEST.json --dest STORE`
takes `{"subject", "identities", "vault_directory", "reason"}` and an optional
`"probe": {"queries": [...], "forbidden": [...], "threshold": 0}` that runs the
membership probe against live `recall` after the purge. `frontier certified-verify`
takes `{"certificate": ...}` and prints `{"valid": true|false}`. Supplying a fact id
is enough: the lineage cascade finds lessons and summaries derived from it.

The backup claim covers only envelopes encrypted before backup, with subject
wrapping keys excluded from those backups and destroyed across every live vault
replica. The reference vault cannot attest destruction of unknown external key
copies or retroactively encrypt old plaintext snapshots. Remote stores, arbitrary
unencrypted journals and non-local vector services need their own deletion and
compaction adapters; the certificate explicitly does not claim remote erasure.

A bounded `probe` detects retained content through a live retriever and reports a
pass/fail threshold. It is a diagnostic, not a universal membership-inference or
GDPR-compliance guarantee. Use the security extra; missing cryptography raises a
clear `unimem[security]` error instead of storing plaintext silently.
