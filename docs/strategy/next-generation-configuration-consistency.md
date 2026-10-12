# One resolved configuration for authentication and migration

The request-local YAML facade already supports all 155 legacy settings, but two
production consumers still read the process environment after resolving YAML.
OAuth indexes `os.environ` after a successful facade lookup, so a YAML-only issuer
raises `KeyError`. Hub KMS unwraps receive only ambient key ID and region values.
The config migration also lets a plaintext environment reference eclipse a
mounted-file reference, ignores `COMMONTRACE_CONFIG`, and duplicates a bounded
YAML reader without its input-length check.

Use the existing facade for OAuth and the two KMS routing settings. Share the
configuration pathname and bounded document reader between normal load and
migration. Migration must preserve the existing file-over-environment secret
precedence, explicit YAML values, and current config destination. Never copy
credential values into YAML or reports, and keep atomic writes inside the store
boundary. Existing explicit environment overrides remain supported.
Migration reports distinguish imported names from names whose explicit YAML
settings, selected profile options or secret references were retained.

Competitor inspection informed the approach:

- Hindsight at `a9eeed77f9bce99d8f97d39a30d3281747300824`,
  `hindsight-api-slim/tests/test_config_is_the_only_env_reader.py` (MIT), uses a
  structural regression gate against secondary environment readers. The new
  CommonTrace contract independently applies that invariant to the OAuth and
  Hub config consumers; no source is copied.
- Cognee at `0ec7a9fa61c9ff04bf7e02e0d57af363a993a5e1`,
  `cognee/infrastructure/llm/config.py:346-387` (Apache-2.0), validates resolved
  fields rather than consulting ambient environment state a second time.
- Mem0 at `b7ad69afda6b6ed030347c66d48a13e4de9dec08`,
  `cli/python/src/mem0_cli/config.py` (Apache-2.0), documents explicit precedence
  and persists one authoritative config file. CommonTrace retains its stricter
  credential-reference behavior instead of copying Mem0's plaintext key storage.
- The supplied Letta checkout at
  `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` contains repository policy and
  documentation only; there is no runtime configuration implementation to reuse.

Tests first cover YAML-only OAuth and KMS resolution, explicit environment
overrides, invalid OAuth values, file secret rotation after removing legacy
variables, configured migration destination, and oversized documents. The
structural contract prevents the two consumers from reintroducing direct
environment reads. Run their existing authentication, encryption and migration
contracts after implementing the fix.

This change removes real requirements to keep OAuth and KMS routing options in
the environment. It does not reduce the documented compatibility inventory:
the primary list has 25 names and the schema has 155 alias entries, with 24
overlapping names. That is 156 unique supported environment-variable names, so
the original fewer-than-40 documented-variable acceptance gate is still missed.
Reducing that surface safely requires a compatibility deprecation policy or an
explicitly narrower definition of the gate; this change does neither. No
time-to-first-injection claim changes.

## Observed validation

The first 21-case regression run against the unchanged
`7e0a22402b9e69653c2fa190a374b42b92ccb83b` implementation produced **9 failed,
12 passed** in 2.45 seconds (pytest exit 1). After implementing the changes and
adding the final boundary and precedence contracts, the focused configuration,
OAuth/JWT, secrets, gateway/platform, Hub configuration and KMS suite produced
**179 passed** in 6.52 seconds (pytest exit 0). All imports were checked against
the isolated worktree. The later full Hub suite had 2,506 passed and 5 skipped;
its only failure was a local PostgreSQL test role lacking `CREATEROLE`, and the
affected RLS test passed under a temporary superuser matching CI. The integrated
application/e2e suite reached 6,821 passed and 116 skipped; its one failure was
the test-only Hub-import guard, fixed and verified by a 31-test targeted rerun.
The full suite was not repeated after that test-only adjustment.

```sh
python -m pytest tests/test_configuration.py tests/test_oauth.py \
  tests/test_secrets_provider.py tests/test_retrieval_configuration_and_gateway_errors.py \
  tests/test_memory_platform_regressions.py hub/tests/test_config_and_limits.py \
  hub/tests/test_secrets_provider.py hub/tests/test_kms.py -q
python -m ruff check commontrace/configuration.py commontrace/oauth.py \
  hub/config.py tests/test_configuration.py
python scripts/dev.py docs --check
python -m bandit -q commontrace/configuration.py commontrace/oauth.py hub/config.py -ll
```

The lint, generated-reference drift and medium/high-severity Bandit checks pass.
These are component and compatibility results; they establish no P0 storage,
real-model answer-quality, first-injection latency or fewer-than-40-variable
acceptance result.
