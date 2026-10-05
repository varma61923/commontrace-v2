# Bounded report reuse and fresh scoped recall

Compared with `c8ce5c792916c10e23b0618c59259960294b7bcc`, these changes improve actual gateway handler throughput while preserving the complete normalized responses in the measured workloads. They also close stale scope and temporal eligibility gaps. They do not measure model answer accuracy or establish superiority over other memory systems.

| Workload | Baseline median | Candidate median | Speedup | Repeated work removed |
| --- | ---: | ---: | ---: | --- |
| Status, agents, 100 distinct occasion limits; 5,000 events | 125.053 ms | 50.626 ms | 2.47× | JSON decodes: 12,050 → 5,000 |
| Status, agents, 500 distinct occasion limits; 5,000 events | 955.743 ms | 172.266 ms | 5.55× | JSON decodes: 132,250 → 5,000 |
| 100 warm scoped recalls; 1,000 lessons, 500 eligible | 745.138 ms | 332.719 ms | 2.24× | Lexical index rebuilds: 100 → 0 |

Each sample includes `Gateway.handle` authentication where applicable, handler computation and response serialization. TCP, TLS and browser rendering are excluded. Five runs use deterministic synthetic fixtures created outside request timing. Report workloads create a fresh Gateway each run; scoped recall performs one warm request before timing. There are no model, embedding or network calls. The only normalized response field is the wall-time-dependent agent `seconds_since_seen`; every other field compares exactly across baseline and candidate. Individual samples, counters, environment and response hashes appear in [serving-phase6-results.json](serving-phase6-results.json).

## Production changes

The gateway retains one trailing event snapshot per instance, capped at 1,500,000 source bytes and 5,000 raw lines. Source identity includes device, inode, size, nanosecond modification time and nanosecond change time. Distinct report limits share incrementally decoded rows rather than repeatedly reading and decoding overlapping tails. Invalid JSON and non-object lines retain their original positions, preserving the previous newest-N-line behavior. Direct callers receive deep copies; internal report computations borrow read-only rows. The read itself has a byte cap, including when another process appends during a read.

The report LRU holds at most 128 variants instead of accumulating an entry for every requested page limit. Per-name single-flight locks coalesce simultaneous cold computations without blocking unrelated warm reports. The existing 5–60 second report memo policy remains: reports may deliberately retain recently computed aggregate data. Authentication and source lesson eligibility are independent of that policy and run on each relevant request. In the 502-request workload, retained report entries fall from 502 to 128; the 5,000 decoded event objects are shared rather than duplicated by limit.

The lesson body cache now bounds retained UTF-8 content to 16 MiB and 512 entries. Python object/key overhead is additional and bounded by the entry count. It retains one generation per path; oversized bodies remain readable without retention. An older reader cannot evict the newest generation. Ranked evidence carries its source identity into body loading: if the source changes between metadata selection and raw evidence loading, that candidate is skipped. This prevents newly private text from being returned under the previous scope metadata.

Generated metadata and binary lexical caches now validate complete device/inode/mtime/ctime/size source identities, including across processes. Same-size edits with restored modification times and atomic replacements invalidate cached frontmatter, scopes and lexical terms. Metadata format 5 and binary format 3 intentionally rebuild earlier generated caches; source lessons remain unchanged. The public listing API still returns `(path, mtime_ns, size)` entries.

Scoped and temporal subsets retain their source stamps and tokenized fields, enabling bounded in-memory lexical index reuse. They do not overwrite the full persisted index. Every gateway store recall now runs live scope and temporal eligibility checks, including requests without a scope header. Expired, not-yet-valid and malformed-validity lessons no longer bypass filtering through the unscoped path.

These changes independently implement cache correctness principles studied earlier in [Hindsight's tenant-qualified cache](https://github.com/vectorize-io/hindsight/blob/f7dd3f4fd7420f7beec60c32c965e5e5cf7be066/hindsight-api-slim/hindsight_api/engine/bank_info_cache.py) and [Letta Code's identity-qualified duplicate suppression](https://github.com/letta-ai/letta-code/blob/f898fda60932b34ddbcfd389ea414515b0a5d272/src/websocket/listener/device-status-cache.ts). No competitor source was copied and no new dependency was added. The broader reference study and license records remain in [the previous serving report](serving-ui-performance.md) and [competitor analysis](competitor-analysis.md).

## Reproduce and verify

Run each helper against a baseline checkout at the recorded SHA and the candidate checkout:

```bash
python research/serving-phase6-profile.py /path/to/baseline --events 5000 --pages 100 --runs 5
python research/serving-phase6-profile.py /path/to/candidate --events 5000 --pages 100 --runs 5
python research/serving-phase6-profile.py /path/to/baseline --events 5000 --pages 500 --runs 5
python research/serving-phase6-profile.py /path/to/candidate --events 5000 --pages 500 --runs 5
python research/serving-phase6-scoped-profile.py /path/to/baseline --lessons 1000 --requests 100 --runs 5
python research/serving-phase6-scoped-profile.py /path/to/candidate --lessons 1000 --requests 100 --runs 5
python -m pytest tests/test_serving_phase6.py tests/test_gateway.py tests/test_gateway_pagination.py tests/test_lesson_cache.py tests/test_perf_index.py -q
```

Thirteen new regression cases cover same-size/restored-mtime updates and replacements, persisted cross-process scoped indexes, unchanged public listing shape, binary format migration, UTF-8 content and entry bounds, oversized-body bypass, stale-reader publication, event line semantics and mutation isolation, report LRU bounds, concurrent report single-flight, unscoped temporal eligibility, subset index reuse and a scope/body race. Final targeted validation passed 195 cases across gateway, lesson caches, retrieval/indexes, MCP fusion, semantic scoring, workbench caches and UI refresh in 18.48 seconds; Ruff and whitespace checks passed. The metadata scan still scales linearly with the lesson count, changed sources still require parsing and indexing, and the event tail intentionally excludes earlier history. The measured gains apply to these serving workloads rather than all memory operations.
