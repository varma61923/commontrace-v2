"""Where a HUB_-prefixed secret's VALUE comes from, as distinct from which
value it holds. One indirection point, used everywhere this codebase reads
a secret (hub/config.py, hub/auth.py).

WHY A FILE, NOT A VENDOR SDK
----------------------------
hub/config.py's own comments already tell an operator to "keep it in a
secret store" for every sensitive setting, but until now the only way to
actually get a secret manager's value into this process was to have
something upstream write it into a plain environment variable -- there was
no code path that read a secret from anywhere else.

Adding a vendor SDK (boto3 for AWS Secrets Manager, the Vault HTTP API,
Azure's SDK, ...) would pick one secret manager over every other, add a
real dependency most deployments never need, and still leave every OTHER
secret manager unsupported. The `_FILE` convention below instead supports
all of them at once, because it delegates the actual fetching to whatever
already knows how to put a secret's value into a file:

- A Vault Agent or External Secrets Operator sidecar writing to a shared
  volume.
- Any cloud provider's Kubernetes Secrets Store CSI driver (AWS, GCP,
  Azure all ship one) mounting a secret as a file.
- Kubernetes' own Secret volumes (deploy/k8s/deployment.yaml's envFrom
  could be swapped for this without any code change here).
- Docker/Swarm secrets, which are always files under /run/secrets/.

This is the same convention the official postgres/mysql/etc. Docker
images popularized for exactly this reason, spelled `{VAR}_FILE` here to
match: `HUB_DATABASE_URL_FILE=/run/secrets/db_url` is read in preference
to `HUB_DATABASE_URL` if both are set, because a deployment that went to
the trouble of wiring up a real secret store should never silently lose
to a leftover plaintext env var from an earlier configuration.

WHICH SETTINGS THIS APPLIES TO
-------------------------------
Only genuinely secret HubConfig fields route through `env_secret` below --
see hub/config.py's from_env() for the exact list (HUB_DATABASE_URL,
HUB_ADMIN_TOKEN, HUB_CONSOLE_SECRET, the Stripe keys, HUB_LEDGER_SIGNING_KEY,
HUB_ENCRYPTION_KEY/_PREVIOUS) and hub/auth.py for HUB_API_KEY_PEPPER.
Operational settings (HUB_HOST, rate limits, HUB_OIDC_ISSUER, ...) are not
secrets and are read directly -- routing them through a secret store would
not be wrong, just pointless, and would make hub/config.py harder to read
for no benefit.
"""

from __future__ import annotations

import os


def env_secret(name: str, default: str = "") -> str:
    """`os.environ[name]`, or the contents of the file named by
    `{name}_FILE` if that variable is set instead.

    The file's contents are stripped of surrounding whitespace (a trailing
    newline is what every `echo`/editor-saved secret file has, and a
    secret value legitimately starting or ending in whitespace does not
    occur in this codebase's use of this function -- API keys, connection
    strings, and signing keys are none of them whitespace-sensitive).
    """
    file_path = os.environ.get(f"{name}_FILE")
    if not file_path:
        return os.environ.get(name, default)
    try:
        with open(file_path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        raise RuntimeError(
            f"{name}_FILE={file_path!r} is set but could not be read: {exc}"
        ) from None
