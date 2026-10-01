# Deploying the Hub

| Path | What |
|---|---|
| `helm/commontrace-hub` | The Hub as a chart: pinned image, existing-Secret only, migration hook, HPA/PDB, optional ingress, network policy, ServiceMonitor and SLO alerts. It refuses unsafe combinations at render time (more than one replica on the in-process rate limiter, a connection budget over `postgres.maxConnections`, an unpinned image). |
| `terraform/aws`, `terraform/gcp` | Postgres only: CMEK, TLS required, high availability, point-in-time recovery (7 to 35 days), connection-count alarms (AWS). No Hub workload and no ingress. |
| `k8s` | The same Hub as plain manifests, for platforms that do not use Helm. |

Validated in CI (`helm lint`/`template`, `terraform validate`/`fmt`); nothing here is applied by CI or talks to an
account. `terraform apply` creates billable resources: plan first. State files are gitignored; use a remote backend.

## Keys, regions and air-gapped installs

Storage is encrypted with a customer-managed key (`kms_key_arn` on AWS, `kms_key_name` on GCP); the Hub's own
at-rest key can be held by the customer's KMS too (`hub/kms.py`). An org can be pinned to a region
(`python -m hub.manage set-region <org> eu`); a Hub declaring a different `HUB_DATA_REGION` then refuses its keys.
For an install with no outbound network, mirror the image into your registry, set `networkPolicy.enabled` with only
the database reachable, leave `COMMONTRACE_LLM_*` unset (drafting then falls back to scaffolds) or point
`COMMONTRACE_LLM_PROVIDER=openai-compatible` at a model server inside the network.

## SLOs

Availability 99.9% (not 5xx) and latency 95% under 250 ms, over 30 days, as multi-window burn-rate alerts in
`helm/commontrace-hub/files/slo-alerts.yaml` (enable with `metrics.alerts=true`). A status page is whatever your
platform's uptime service polls: `/healthz` (process) and `/readyz` (process and database).

## Restoring to a point in time

AWS: `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier commontrace-hub
--target-db-instance-identifier commontrace-hub-restore --restore-time 2026-01-01T12:00:00Z` (or
`--use-latest-restorable-time`; the Terraform output `latest_restorable_time` says how recent that is).
GCP: `gcloud sql instances clone commontrace-hub commontrace-hub-restore --point-in-time 2026-01-01T12:00:00Z`.
Restore to a NEW instance, check it, then repoint `HUB_DATABASE_URL`. `hub/DEPLOYMENT.md` has the timed drill.
