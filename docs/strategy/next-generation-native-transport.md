# Existing native transport latency

P0 remains open on batch ingest and unverified hybrid/restart gates. This note
investigates the existing injector's measured transport miss; it does not
establish P1 acceptance or semantic relevance.

The historical Node transport fixture delivered 50/50 directives through fresh
Node processes and a warm broker, but measured 152.27 ms p95. A separate
20-process startup diagnostic on the same host measured an empty Node process
at 105.42 ms p95. Improving broker selection alone cannot make that fresh-process
path meet the unchanged 50 ms target on this host.

Twenty fresh Python processes importing only JSON, sockets, OS and sys measured
42.17 ms p95 normally, 34.73 ms with `-S`, and 29.33 ms with `-I -S`. These are
startup/import controls, excluding delivery, acknowledgements, capture, formatting
and argument parsing. They are not hook latency results. Python's
[command-line documentation](https://docs.python.org/3.12/using/cmdline.html)
describes isolated mode and disabling automatic site imports.

Prototype a stdlib transport launched by the installed Python interpreter with
`-I -S`. Keep every admission, redaction, learning and scope decision in the
existing broker/HookKit core. Reuse the current request/reply/receipt protocol,
formatters and durable capture fallback. Prefer replacing the redundant native
bridge to introducing another maintained transport path. Verify packaging and
generated commands with paths containing spaces and from unrelated directories.

Before implementation, write real-process tests for successful output and receipt,
malformed/oversized frames, unavailable and slow brokers, capture recovery,
all agent response formats and isolated imports. Preserve the current 25 ms
response and 20 ms receipt bounds. A receipt must follow successful stdout
delivery; timeouts must produce no injection and exit successfully.

Then rerun the unchanged 50-process warm-broker fixture, reporting startup/warmup
separately, per-call samples and withheld deliveries. Require p95 below 50 ms
and complete fixture delivery. Keep model-backed injection precision and safety
evidence separate. Run the affected contracts, static checks, packaging checks
and Graphify audit before accepting a replacement.

## Replacement implementation

Generated commands now use the installed interpreter and the absolute packaged
`commontrace/hook_bridge.py` script with `-I -S`. The script imports only the
stdlib. It shares host response formatting and event/agent constants with the
existing adapters. The old Node file is a compatibility launcher for previously
installed commands; reinstalling hooks replaces it with the direct Python
command without duplicating entries or removing other tools' hooks.

The response deadline remains 25 ms across connection, request and response;
receipt waiting remains bounded by 20 ms. Host input and broker replies are
bounded at 100,000 bytes. Invalid JSON, invalid reply types, excessive nesting,
oversized frames, timeouts and failed stdout produce no receipt. Writes use
unbuffered stdout and acknowledge only after every byte is delivered. Windsurf
is capture-only and cannot acknowledge context delivery through UI stdout.

When the broker cannot connect, the existing capture-only HookKit runs detached
under the installed interpreter with `-I`, retaining site packages while
excluding foreign working-directory and `PYTHONPATH` imports. An inherited
anonymous temporary file holds stdin, so a large payload cannot block the
parent on a cold child's pipe. Capture, normalization, scope and redaction
continue through the existing core; no transcript is placed in argv.

Real-process contracts cover the shared formats, malformed/deep/oversized
frames, partial responses and stalled receipt peers, failed stdout, capture
recovery, old commands, reinstall idempotence and foreign import isolation.
An installed-wheel experiment also exercises interpreter/store paths containing
spaces and delivery/capture from an unrelated directory. A pre-existing cold
core test failed on the unchanged baseline because it imported causal-measurement
dependencies inside the operation deadline. Initializing that dependency with
the core preserves the deadline and passes the isolated regression.

The first working-tree diagnostic delivered 50/50 directives at 49.96 ms p95.
Three subsequent sequential runs on frozen `4b1d2eb` retain every sample and
report warmup separately:

| Run | Fresh process p95 | Delivery | Exit status |
| --- | --- | --- | --- |
| 1 | 51.59 ms | 50/50 | 1: latency miss |
| 2 | 46.64 ms | 50/50 | 0 |
| 3 | 48.64 ms | 50/50 | 0 |

The replacement is faster on these observations, but one of three runs misses
the unchanged 50 ms target. The native latency gate remains open. The historical
Node JSON reports checkout `54681d0`, despite being nested under the `fa1ef0c`
follow-up evidence. Node bridge and harness bytes match both commits; the raw
report does not bind every imported core dependency. Its 152.27 ms result is a
historical separate-run observation, not a proven frozen-`fa1ef0c` measurement
or a controlled causal comparison.

Validation passes 129 scoped tests with 8 skips, the isolated cold-core and
installer regressions, installed-wheel delivery and capture, Ruff, strict mypy,
Bandit, docs and both OpenAPI checks. Graphify reports 21,455 nodes, 63,732 edges,
298 modules, 300 reachability candidates, no dangling endpoints and no policy
violations. The ceilings remain 306 modules and 303 candidates; the new bridge
is an actual generated absolute-script entry point. The full application and
end-to-end suite completed against the same frozen commit in a dedicated
environment: **6,709 passed, 126 skipped, 26 warnings in 4,094.07 seconds**.
An initial tests-only invocation was stopped early and is not
counted as a validation pass.

Reproduce each transport run with:

```sh
python -m benchmarks.injector_bench --transport-only --repetitions 50 --out native-transport.json
# Or, with the installed environment on PATH:
./reproduce.sh --native-transport benchmark-results/native-transport
```

The [signed native evidence](measurements/p0-native-transport-signed.json) binds
the raw historical result, all three new rounds and validation/source hashes.
Signatures detect edits; they do not certify scientific validity. Neither these
transport checks nor earlier startup controls establish model relevance, safety
precision or any remaining P0 gate.
