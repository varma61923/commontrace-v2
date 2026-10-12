# Handwritten SDK parity for versioned records and turns

Measured on 2026-10-12. The authoritative request contract is
`commontrace/api_schema.py: FIELDS`, mirrored in `sdk/openapi.json`. It currently
contains thirteen memory operations. The handwritten Go, Rust and Java generic
clients admitted only eight; Kotlin delegated to Java and inherited that gap.
Generated OpenAPI endpoint coverage did not prove handwritten client coverage.

## Change

All four handwritten clients can now call `turn`, `record-put`, `record-search`,
`record-forget` and `record-reap-due`, alongside their existing eight operations.
They expose matching convenience methods for storing an envelope, searching
records, forgetting a record with a reason, bounded expiration cleanup and a
save-and-retrieve turn. The wrappers retain each language's existing generic
JSON response style instead of inventing another record format.

Turn options can carry `text`, `budget`, `event` and `payload`, including explicit
completion evidence; session and query arguments stay pinned. Each request
continues to replace caller-provided context with the client's scope labels and
agent identity. The original bearer authentication, HTTPS/loopback URL rules,
redirect refusal, timeout and 8 MiB response bound remain in the shared request
path. Reaping wrappers reject limits outside 1..100 before sending a request;
Java and Kotlin default to 16, while Go and Rust require an explicit limit.
Hosted reaping still requires the server's admin authorization. Client wrappers
cannot grant it. Other request validation remains the server's responsibility,
as with the existing handwritten methods.

## Evidence and reproduction

Tests were added before implementation. The Go, Java and Rust loopback tests
each reproduced an `Unknown memory operation` error when trying `turn` through
the original generic path. The revised tests use valid request bodies for all
five newly admitted operations, check actual POST paths and bearer headers,
attempt a context override and inspect decoded responses. Wrapper tests send
versioned record payloads, query limits, erasure reasons and completion turns,
and verify that invalid expiration batch limits cause no extra HTTP request.
The JVM suite invokes both Java and Kotlin wrappers over loopback HTTP.

| Suite | Before | After |
| --- | --- | --- |
| Go | 1 pass, 1 failure (missing operation) | 3 tests pass with race detector |
| Rust | 1 pass, 1 failure (missing operation) | 3 tests pass; 0 doctests |
| JVM | 1 pass, 1 error (missing operation) | 3 tests pass; Java and Kotlin compile |

Run these commands from the corresponding SDK directory:

```bash
# sdk/go
go test -race ./...
# sdk/rust
cargo test --locked
# sdk/jvm
mvn test
```

The observed Go toolchain was 1.27.2, selected automatically by the existing
`go.mod` from a 1.27.1 launcher. Rust stable 1.99.0 and Maven 3.9.11 were installed
under `/tmp` for this verification; JVM compilation used Java 17.0.20.1 and the
existing Kotlin 2.2.21 Maven plugin. No SDK dependency pins or lockfiles changed.
The exact isolated Rust command was:

```bash
RUSTUP_HOME=/tmp/commontrace-rustup CARGO_HOME=/tmp/commontrace-cargo CARGO_TARGET_DIR=/tmp/commontrace-sdk-rust-target /tmp/commontrace-cargo/bin/cargo test --locked
```

The exact isolated Maven command was:

```bash
/tmp/apache-maven-3.9.11/bin/mvn -q -Dmaven.repo.local=/tmp/commontrace-sdk-m2 test
```

These are transport and API-surface correctness measurements. They do not
establish live hosted authorization, Postgres cleanup behavior, answer quality
or performance. No competitor code was copied for this slice: it adapts the
existing Python/TypeScript clients and the repository's OpenAPI contract.
