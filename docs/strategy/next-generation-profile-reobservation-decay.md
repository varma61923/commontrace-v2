# Preserve preference decay on re-observation

Date: 2026-10-12. F6 already has automatic literal static/dynamic profile
capture, custom profile records, scoped profile plus search in one turn,
read-time preference decay and bounded preference reranking. A small remaining
gap was in reinforcement: capture added one to the stored raw weight and reset
`observed_at`, reviving the entire accumulated weight after a long idle period.

Competitor source inspection informed the fix:

- Cognee `0ec7a9fa6`, `cognee/modules/user_preferences/weights.py:11–24`,
  computes effective weight on read; `update.py:311–318` applies that effective
  weight **before** incorporating fresh evidence and replacing the age marker.
- Supermemory `709723de`,
  `apps/docs/recall/user-profiles.mdx:14–74`, describes static/dynamic buckets;
  `apps/docs/user-profiles/buckets.mdx:8–26` describes custom topical buckets.
  These are client/documentation contracts for its closed backend, not locally
  reproduced profile-quality results. CommonTrace already has the corresponding
  three-bucket turn response, so this slice did not add a competing API.

The adaptation reuses CommonTrace's existing wall-clock exponential half-life
formula. Cognee's neutral baseline, turn counter and rating graph were not
copied; they are different semantics. No competitor source code was copied.

## Implementation and evidence

The shared `memory_record.preference_statistics` helper now decays the previous
weight from its `observed_at` and existing half-life before adding one fresh
observation. Local `maintain_preferences` and hosted `capture_turn` already call
that helper, so no caller, route, database schema or SDK change was needed.
An optional `at` argument provides a deterministic reference instant for tests.
Existing weight/count caps and the new-observation default half-life remain.
Legacy rows without an observation timestamp retain their earlier behavior;
malformed decay metadata contributes no old weight. Future timestamps cannot
amplify a stored weight.

Tests were written first. The in-memory local capture regression reproduced
the original behavior: a preference with weight **10**, last asserted **180
days** ago under a **90-day** half-life, returned weight **10** after one fresh
assertion. It now returns approximately **3.5**: `10 * 2**(-180/90) + 1`.
The fixed-time helper regression verifies that exact value. Additional cases
cover alternate timezone spellings, naive UTC timestamps, custom old half-life,
future timestamps, counter caps, legacy rows and malformed decay metadata.

```bash
./.venv/bin/python -m pytest tests/test_preference_reobservation_decay.py \
  tests/test_profile_preference_ranking.py \
  -k 'not profile_preference_reranking_respects_decay_and_scope' -q
```

These tests use pure helpers and an in-memory journal double; they do not open
any database. Observed: **18 passed, 1 deselected in 0.30 seconds**. The
deselected existing scope/decay integration test opens a temporary SQLite store;
these original focused tests establish arithmetic and local capture wiring, not
model extraction quality, user satisfaction or performance improvements. A
separate hosted-capture regression now seeds a 10-weight preference dated 180
days earlier and verifies `capture_turn` stores approximately 3.5 after one
fresh assertion. It passes against the owned disposable PostgreSQL database
(**1 passed in 1.57 seconds**). The combined local profile/preference checks
pass **20 tests**, in addition to that PostgreSQL case:

```bash
/tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_preference_reobservation_decay.py \
  tests/test_profile_preference_ranking.py \
  tests/test_profile_search_acceptance.py -q
```
Broader profile contradiction resolution, model-derived facets and quality
benchmarking remain separate work.
