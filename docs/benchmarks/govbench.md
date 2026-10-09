# GovBench

Shared assistants have many principals writing to one memory and reading it
under different entitlements. GateMem (arXiv 2606.18829) and PiSAs (arXiv
2607.05318) found that no tested system achieved utility, access control and
reliable forgetting at the same time. GovBench measures all three through
CommonTrace's real gateway, with agent credentials minted by `/v1/agent/signup`.

The scenario is one hospital ward: a doctor entitled to patients A and B, a nurse
for each patient, and a billing agent. The operator writes shared ward, patient
and billing records with scope labels; each agent also writes private notes
through its own credential. A summary is derived from one patient record.
Every principal then recalls through `profile`, `search` and `reflect`. The
operator forgets two records, and recall is repeated.

| phase | utility | leaks / checks | forgotten delivered / checks |
| --- | --: | --: | --: |
| before forgetting | 100% | 0 / 69 | - |
| after forgetting | 100% | 0 / 48 | 0 / 36 |

The signed forgetting certificate lists both the forgotten record and the summary
derived from it, so forgetting follows lineage rather than only the named record.

A negative control in `tests/test_govbench.py` disables scope matching and
confirms that the benchmark then reports leaks, so a zero is a measurement
rather than an artifact of the harness.

Limits: one synthetic deployment; forgetting covers local recall surfaces, not
historical Git bytes or remote replicas (see `SECURITY.md`).

```bash
python -m benchmarks.govbench --out govbench.json
```
