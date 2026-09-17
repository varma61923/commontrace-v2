# Running the Hub on Kubernetes

A reference manifest set for operators whose platform is Kubernetes rather
than a single Docker host — `docker-compose.yml` at the repo root remains
the getting-started/evaluation path (see its own header), and
[`hub/DEPLOYMENT.md`](../../hub/DEPLOYMENT.md) is the source of truth for
every configuration decision referenced here (rate limits, connection
pooling, the two-database-roles requirement, TLS, backups). Read that file
first; these manifests are the same deployment expressed as Kubernetes
objects, not a second, divergent set of decisions.

## What is, and is not, here

| File | What it is |
|---|---|
| `configmap.yaml` | Non-secret `HUB_*` settings |
| `secret.example.yaml` | The **shape** of the Secret the Deployment expects — not a real Secret. See "Secrets" below. |
| `migration-job.yaml` | One-shot `alembic upgrade head`, mirroring `docker-compose.yml`'s `migrate` service |
| `deployment.yaml` | The Hub itself: liveness on `/healthz`, readiness on `/readyz` |
| `service.yaml` | ClusterIP, port 8420 |
| `hpa.yaml` | CPU-based autoscaling — read its own comments before enabling |
| `pdb.yaml` | Keeps at least one replica up during a voluntary node drain |

**No Postgres manifest is provided, on purpose.** `docker-compose.yml`'s
own header says its bundled Postgres is for local evaluation only —
`hub/DEPLOYMENT.md` says the same for production: a managed Postgres
(RDS, Cloud SQL, or equivalent), backed up and monitored by your platform,
not a database this Hub's own manifests provision and are then implicitly
responsible for. Point `HUB_DATABASE_URL` at that instead. If your
platform's convention is to run stateful workloads in-cluster anyway, use
your platform's own supported Postgres operator — that is a database
decision, independent of anything specific to this Hub.

**No Ingress is provided.** Which Ingress controller, class, and TLS
certificate mechanism (cert-manager, a cloud load balancer's own TLS,
etc.) is a platform decision this repository cannot make for you —
`hub/DEPLOYMENT.md` §10's first checklist item, "TLS terminated in front
of the Hub," applies here exactly as it does to any other deployment
shape. `service.yaml` is what an Ingress (or a Gateway API `HTTPRoute`)
should point at.

## Secrets

`secret.example.yaml` documents the keys the Deployment reads via
`envFrom` — it is **not** meant to be filled in and applied directly.
Generate the real values with the same commands `hub/.env.example` and
`hub/DEPLOYMENT.md` already document (e.g. `python -m hub.manage
generate-encryption-key`), and get them into the cluster through your
platform's actual secret mechanism: a CI/CD pipeline step backed by your
organization's secret store, External Secrets Operator syncing from
Vault/AWS Secrets Manager/etc., or Sealed Secrets — never a plaintext
Secret manifest committed to a repository, which is exactly the mistake
`hub/.env` being gitignored exists to prevent for the single-host case.

## Order of operations

```bash
kubectl apply -f configmap.yaml
# Create the real Secret via your platform's own mechanism (see above),
# named commontrace-hub-secrets to match deployment.yaml's envFrom.
kubectl apply -f migration-job.yaml
kubectl wait --for=condition=complete job/commontrace-hub-migrate --timeout=120s
kubectl apply -f deployment.yaml -f service.yaml -f pdb.yaml
# Only after deciding what hpa.yaml's own comments ask you to decide:
kubectl apply -f hpa.yaml
```

A rolling deploy that changes the image re-runs `migration-job.yaml`
first, same as `docker-compose.yml`'s `migrate` service running before
`hub` starts — migrations are a one-shot Job specifically so N replicas
can never race applying the same migration concurrently.

## What is NOT rehearsed

Unlike the Docker Compose stack (exercised end-to-end in CI —
`.github/workflows/ci.yml`'s "docker compose stack serves real MCP
traffic" job), these manifests are not applied against a real cluster in
CI. They are reviewed for correctness and kept consistent with
`hub/config.py`'s actual environment variables and `docker-compose.yml`'s
proven image/health-check configuration, but treat them as a reviewed
starting point to adapt and test against your own cluster, not a
rehearsed, turnkey deployment — the same honesty `hub/DEPLOYMENT.md` §11
already applies to its own "no production-like rehearsal" limitation.

## Scaling note

`hpa.yaml` can raise the replica count. `HUB_DB_POOL_SIZE` (in
`configmap.yaml`) is **per replica** — `hub/DEPLOYMENT.md` §2 and
`hub/.env.example` both say so directly: `HUB_DB_POOL_SIZE * replicas`
must stay under your Postgres's `max_connections`, or a rolling deploy
that briefly overlaps old and new replicas can exhaust connections.
Raising `maxReplicas` without checking that arithmetic first is the one
mistake this note exists to prevent.
