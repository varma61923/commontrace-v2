# Standard API and SDKs

The gateway serves OpenAPI 3.0.3 at `/v1/openapi.json` and Swagger UI at
`/v1/docs`. The checked-in [memory contract](openapi.json) contains typed requests,
responses, error responses, stable operation IDs and bearer authentication.
Gateway admission enforces the same request definitions.

```bash
python scripts/export_openapi.py
python scripts/generate_sdks.py --languages typescript go rust java kotlin
```

Generation uses upstream OpenAPI Generator 7.26.0, checked against a pinned SHA256.
Java 17+ is required for generation. Use `--jar PATH` for an existing verified jar,
`--output PATH` for a fresh output directory, or `--validate-only` to validate the
specification. No custom code-generation templates or mandatory runtime dependencies
are added. Generated code and docs stay in ignored build directories; CI compiles
every language and provides source packages as artifacts.

| Language | Generated package | Build |
| --- | --- | --- |
| TypeScript | `sdk/generated/typescript` | `npm install` |
| Go | `sdk/generated/go` | `go test ./...` |
| Rust | `sdk/generated/rust` | `cargo check` |
| Java | `sdk/generated/java` | `mvn -B -DskipTests package` |
| Kotlin | `sdk/generated/kotlin` | `bash gradlew --no-daemon compileKotlin` |

Configure the generated client's base URL and bearer authorization. Use HTTPS for
hosted gateways. An agent credential binds its principal's scope on the server;
payload `context` cannot replace it. Operator tokens have store administration
authority and belong in a trusted service, not an untrusted assistant.

The checked-in `typescript`, `go`, `rust` and `jvm` directories also contain small
convenience clients with pinned scopes, response limits, redirect refusal and
HTTPS/loopback URL checks. They are maintained adapters, not generator output.
Java and Kotlin share the JVM transport. Generated responses and retrieved facts
never grant tool authorization: enforce directives at the tool boundary and only
treat an explicitly boolean approval as approval.

The schema is generated from the running gateway's definitions. Run
`python scripts/export_openapi.py --check` after API changes; CI rejects stale
contracts. Registry publication is not performed by this generation command.
