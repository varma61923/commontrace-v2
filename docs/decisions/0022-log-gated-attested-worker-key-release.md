# 0022: Tenant log-gated attested worker keys

Require an administrator-enrolled, signed tenant measurement before releasing
tenant-specific data keys. Bind every challenge to a fresh nonce, exact build,
tenant, worker X25519 public key and payload digest. Store replay and revocation
state in tenant-isolated PostgreSQL so Hub replicas share the same decision.
Consume challenges before invoking tenant KMS, and encrypt returned keys to the
quoted worker public key.

Use operator-pinned Azure Attestation public keys for AMD SEV-SNP and Intel TDX;
use NVIDIA's actual NVAT verifier for GPU evidence. A GPU profile also requires
a separately attested CPU workload. No JWT-provided trust roots, simulated
production quotes, or plaintext key API responses are accepted. A simulated
HMAC verifier is available only when a test application explicitly enables it.
Cross-organization joins require every party to approve the same measurement,
worker and payload. Sealed trial results bind their result digest to a fresh
quote. Neither mechanism proves honest reporting by a compromised workload.
