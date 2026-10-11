# Existing native transport latency

P0 remains open on batch ingest and unverified hybrid/restart gates. This note
investigates the existing injector's measured transport miss; it does not
establish P1 acceptance or semantic relevance.

The frozen `fa1ef0c` transport fixture delivered 50/50 directives through fresh
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
