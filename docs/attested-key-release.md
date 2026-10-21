# Attested tenant key release

An enterprise worker holds an ephemeral X25519 private key inside its
confidential workload. Before decrypting a tenant's memory key, the Hub checks
that a tenant administrator enrolled the exact build policy in that tenant's
signed transparency log, then verifies a fresh platform appraisal bound to the
tenant, build, worker public key and requested payload. PostgreSQL stores nonce
use and revocation state across replicas. The tenant key is unwrapped through a
tenant-context KMS envelope and encrypted directly to the worker's public key.
It is never returned as plaintext to the API caller.

## Enroll a build

`POST /v1/attestation/measurements-register` requires a Hub admin API key, the
Hub transparency signing key and a closed JSON profile. The log entry records
the canonical profile hash. Revocation appends a signed monotone revocation
entry; revoked measurements cannot be re-enabled under the same identifier.
`POST /v1/attestation/measurements-list` returns the tenant's enrollment and
log receipts. Memory measurement enrollment is separate from the local store's
operator-only `unimem prove measurements register|list` interface.

An AMD SEV-SNP profile pins the Azure Attestation `sevsnpvm` claim, 48-byte
launch measurement, boot-loader SVN, TEE SVN, microcode SVN and SNP firmware
SVN. Its quote must report secure boot and TPM enabled. An Intel TDX profile pins
`tdxvm`, MRTD, all four RTMR registers, TEE TCB SVN and SEAM SVN. CPU profiles
require canonical firmware register values, a non-debug appraisal, an exact
pinned MAA policy hash and Azure's compliant-CVM appraisal claim. TDX also
requires the attester TCB status `UpToDate`.
An NVIDIA profile contains exact expected GPU claims and a nested AMD or Intel
CPU profile. The NVAT verifier checks signed Detached EAT tokens, the fresh
nonce and every device's secure-boot, debug and configured hardware/firmware
claims. A GPU quote alone cannot stand in for the measured worker binary.

The Hub does not discover or trust signing keys from JWT `jku`/`jwks_uri`
headers. Provide issuer URLs, JWKS public keys and policy hashes through a
deployment-owned absolute `HUB_ATTESTATION_CONFIG` JSON file, mode 0600 or 0640,
plus the native NVIDIA `nvattest` absolute binary and SHA-256 when configured.
The file may also map tenant IDs to base64 KMS ciphertext and key IDs. It never
contains plaintext data keys. `hub.kms.TenantKeyProvider` sends KMS the exact
context `{purpose: unimem-attested-tenant-key-v1, tenant: TENANT_ID}`;
provision ciphertext with that context and a restrictive KMS IAM policy.
These tenant release keys are distinct from the Hub's general at-rest key.

The application loads this registry when the REST routes are installed. The
hardware SDK and tenant KMS remain deployment dependencies. Without the signer,
an appraiser or tenant key provider, release fails closed with service
unavailable. The simulated HMAC verifier is test-only and `install_services`
rejects simulated backends unless the application composition explicitly
enables them for tests.

## Issue a fresh challenge and release

The administrator requests `POST /v1/attestation/challenge` with the registered
`measurement`, a 32-byte X25519 `worker_key`, a `payload_digest`, an operation
(`recall`, `sealed-trial` or `join`), and for a join the complete sorted tenant
party list. The Hub supplies a random 32-byte nonce and limits the challenge to
120 seconds. The worker's CPU report-data must equal
`SHA256(canonical_request_json) || 32 zero bytes`. NVIDIA NVAT receives the same
request hash as its GPU nonce. Quotes that omit this exact binding cannot release
a key.

The authenticated tenant posts the exact issued `request` and signed `evidence`
to `POST /v1/attestation/key-release`. The Hub verifies token signatures and
issuer using deployment-pinned keys, token age, the platform, policy, debug
state, every enrolled build register and the report-data nonce. The actual
verifier SDK checks NVIDIA Detached EAT signatures and nonce. Hub quote checks
finish before it locks shared nonce state; the nonce is consumed before KMS is
called, so a crash burns it. Measurement revocation and release serialize on
the same tenant row.

The returned envelope contains an ephemeral X25519 public key, a random
AES-GCM nonce and ciphertext. The worker derives a 256-bit wrapping key with
HKDF-SHA256, salted by the request hash and labeled `unimem-attested-key-v1`;
AES-GCM authenticates the canonical request as associated data. Only the
worker holding the quoted private key can unwrap the tenant key. The Hub log
anchors the envelope digest, never its plaintext.

For a cross-organization join, each organization separately registers and
approves the same compound CPU/GPU measurement, then uses the same worker key,
join payload digest and complete participant set. `attested_keys.assert_join`
rejects mismatched binaries, workers, queries, or missing parties. Each Hub
still requires its own authenticated admin, local live measurement enrollment,
challenge, quote appraisal, KMS key and release approval.

## Sealed trial results

`cleanroom.attested_trial` runs the existing seller-side sealed trial and then
asks its deployment-controlled runner to quote the canonical result digest
against a fresh `sealed-trial` challenge. It verifies and logs that bound result
before returning it. It preserves the ordinary buyer experiment's canonical
outcome semantics. This interface cannot prove that a compromised runner
reported honestly; trust depends on the quoted platform measurement, workload
build and the deployment's reviewed data path.

An administrator or client must also track a current trusted log checkpoint and
revocation state when accepting worker measurements. A previously valid
inclusion proof proves historical enrollment; it does not prove that no later
revocation was hidden from an observer. `verify_node` therefore accepts only a
receipt included in the caller's independently pinned checkpoint and rejects
measurements in its trusted revocation set.
