# Bounded Knowledge Base review serving

The Hub's operator review queue formerly selected every visible `Trace` ORM
instance, including its complete source text, signatures, metadata and stored
search vector. It then bound every ID into a second security-vote query, built
the whole queue and sorted it in Python before applying the page limit. Even the
count endpoint performed this full materialization.

`hub/crud.py` now projects only the eight values needed to build review cards.
Postgres combines security report counts with the same commons visibility rules,
standing thresholds and review cutoff, filters eligible entries, and applies
priority and page limits before sending rows to Python. Count is an aggregate;
the operator's combined page and total uses one window-count statement, so both
come from the same database snapshot. It transfers at most 500 narrow records.

The human-review policy is preserved: any security-concern report is urgent,
including an upvote from a new organization; disputed outranks stale; stale
outranks an entry that never matched; the busiest entry comes first within each
bucket. Ties now have a deterministic ascending trace ID. Source text, derived
search vectors and unrelated private entries never cross the query boundary.

## Local paired measurements

Baseline: published commit `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`.
Python 3.12.14 and PostgreSQL 16.15, local disposable database; 10,000 synthetic
visible entries with 2,080 context characters, 1,575 solution characters and 128
signature integers each, mixed review dates/standing/hits/security votes. Each
operation ran once to warm the connection pool and then five before/after pairs
against the identical corpus. Schema creation and ingestion were excluded.
Baseline functions are loaded directly from that git revision.

| Operation | Baseline median | Candidate median | Speedup |
| --- | ---: | ---: | ---: |
| Exact uncapped count | 704.864 ms | 8.796 ms | 80.13× |
| Page and exact total | 770.227 ms | 10.665 ms | 72.22× |
| 50-entry page | 924.448 ms | 13.937 ms | 66.33× |

Every baseline/candidate response was equal. A separate `tracemalloc` count run
measured peak Python allocation of 107,332,959 versus 312,353 bytes, roughly a
344× reduction; tracing was disabled for latency measurements. This measures
Python allocations, not database memory or overall process RSS. The raw samples,
environment and limitations are in [hub-queue-performance-results.json](hub-queue-performance-results.json).

The gain exceeds 20× on this operator-serving workload. It does not establish
20× faster memory answers, ingestion, or competitor benchmark superiority.
An exact count still scans eligible database records, and security aggregation
still reads vote data. Database index selectivity, corpus and vote sizes, cold
disk reads, concurrent users and distributed serving can change these timings.

## Reproduction and regression coverage

Install the Hub development dependencies and provision a disposable PostgreSQL
16 database. Set `HUB_TEST_DATABASE_URL` through the environment, then run:

```sh
python -m research.profile_hub_queue --entries 10000 --runs 5
python -m pytest hub/tests/test_kb_queue_sql.py hub/tests/test_kb_standing.py hub/tests/test_overflow_limits.py -q
```

The profiler creates and drops a uniquely named schema without reading or
modifying existing application tables. Use an account allowed to create schemas.
The Hub test fixtures reset their configured test database, so that database must
be isolated from production and other test workers.

The four additional real-Postgres regressions check policy and visibility across
all queue buckets, urgent reports from unestablished voters, deterministic ties,
the uncapped total above the 500-row page limit, empty results, one SQL statement
for a combined response, and the absence of source-body columns and ORM corpus
hydration. The existing standing and overflow suite's 94 cases also passed.
The broader operator/admin/manage/commons coverage finished with 434 passed and
one skipped in 104.60 seconds on the same isolated PostgreSQL instance.

This is an independent implementation of database projection/filter/aggregation
and bounded retrieval, a general systems technique rather than reused competitor
source. No license-bearing competitor code, official benchmark judge or database
schema was changed.
