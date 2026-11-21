# SQL vs NoSQL benchmark

`python bench/run_bench.py` asks PostgreSQL and MongoDB the same questions
and records how long each takes. Everything below is produced by the script;
nothing is hand-edited.

## Design

**Three configurations, not two.** Comparing "PostgreSQL vs MongoDB" directly
mixes two things: the *engine* and the *data model* (5 normalised tables with
joins vs one denormalised document per sale). So the benchmark also runs
PostgreSQL on one flat table (`bench_txn_flat`, a materialized view of the same
rows) with the same indexes as MongoDB:

| Configuration | Schema | Joins |
|---|---|---|
| `postgres` | the application's real normalised schema | 4 |
| `postgres_flat` | one row per sale, same columns as the MongoDB document | 0 |
| `mongodb` | one document per sale (`transactions`) | 0 |

`postgres` vs `postgres_flat` = the cost of normalisation; `postgres_flat` vs
`mongodb` = the engine difference on the same data model.

**Index parity.** The indexes that serve each query exist on both sides, in the
same column order. The compound index for the search follows MongoDB's
Equality → Sort → Range rule and includes the sort's tie-breaker:
`(town, flat_type, month DESC, id DESC)`. A first version without the
tie-breaker made MongoDB sort the matches in memory (`SORT` stage, 8,465
documents examined, median 14.1 ms); with it the plan is `IXSCAN → FETCH → LIMIT`
(68 documents, 1.7 ms). Index design moved the result more than the choice of
engine did.

**Equivalence first.** Each workload runs on 20 parameter sets and every
configuration's answer is compared with the normalised PostgreSQL answer before
anything is timed (same ids in the same order, same medians, same station, and
the same blocks within 1 km). MongoDB measures on a sphere 0.11 % larger than the
one PostGIS uses, so a block 999 to 1,000 m from a station can fall inside the
radius in one engine and outside in the other. W5 therefore counts as the same
answer when PostGIS, given MongoDB's radius, returns exactly MongoDB's set.

**Medians.** MongoDB's `$median` (method `approximate`) returned exactly
PostgreSQL's `PERCENTILE_DISC(0.5)` (an observed value) in every check, so the
benchmark uses `PERCENTILE_DISC` on the PostgreSQL side to compare like with
like. The app itself uses `PERCENTILE_CONT(0.5)`, which averages the two middle
values: that gives a different number in 34.8 % of the
16,083 town × flat type × quarter groups (up to 53.57 % in very
small groups). This is a definition difference, not an engine error.

**Protocol.** Fixed seed (42); the same parameters replayed for every
configuration; the configuration order rotates every run; 30 warm-up runs
discarded; 300 measured runs; client-observed latency (includes the Python
driver decoding the result); median, p95, p99 and a bootstrap 95 % confidence
interval for the median (2,000 resamples). Then throughput at 1, 2, 4 and 8
threads for 10 s each (every thread connects and runs once before the clock
starts), bulk-load rate (COPY vs `insert_many`, 200,000 rows, 3 repeats),
single-row writes (W7), storage size, and query plans (`EXPLAIN (ANALYZE,
BUFFERS)` and `explain("executionStats")`). Versions, settings, CPU count, the
database containers' CPU and memory limits and the full index lists are saved
in the results file.

**CPU limit.** `docker-compose.yml` gives each database container 2 CPUs, the
same budget for both engines. Throughput therefore rises up to 2 threads and
then flattens: more threads only queue for CPU time, and at 8 threads the
waiting makes it drop. We confirmed this on 14 November by raising PostgreSQL's
limit to 4 CPUs for one test of the search (W1, 10 s per step): at 4 threads it
reached 604 operations per second instead of 371, with a p95 of 11 ms instead of
47 ms, and at 8 threads 549 instead of 260. Read the throughput table as "what
2 CPUs can do", not as each engine's ceiling.

**Connections.** On Windows, `localhost` is tried as IPv6 first and Docker only
answers on IPv4, so every new PostgreSQL connection waited 2 s. The app now uses
127.0.0.1 for a local database, and the concurrency test opens its connections
before timing. Run 1 (26 October) did not, so its PostgreSQL throughput figures
include that wait; MongoDB's client was already open.

## Workloads

| | Question | PostgreSQL | MongoDB |
|---|---|---|---|
| W0 | one sale by id | `WHERE txn_id = $1` | `find_one({_id})` |
| W1 | town + type + budget, newest 50 | joins + `ORDER BY month DESC, txn_id DESC LIMIT 50` | `find().sort().limit(50)` |
| W2 | median price per town, one type, 24 months | `PERCENTILE_DISC … GROUP BY` | `$match → $group $median` |
| W3 | median per quarter, one town + type, all years | `DATE_TRUNC('quarter') … GROUP BY` | `$group` on `$dateTrunc` |
| W4 | nearest station to a block | PostGIS KNN `ORDER BY geog <-> pt LIMIT 1` (GiST) | `$geoNear` (2dsphere) |
| W5 | blocks within 1 km of a station | `ST_DWithin(…, 1000, false)` | `$geoWithin $centerSphere` |
| W6 | bulk load | `COPY` | `insert_many` (batches of 10,000) |
| W7 | insert, update, delete one sale (each committed) | `INSERT` / `UPDATE` / `DELETE` on the normalised table (3 foreign keys, 6 CHECKs, 8 indexes) | `insert_one` / `update_one` / `delete_one` (`$jsonSchema` validator, 6 indexes) |

W7 runs at two durability levels, so both engines make the same promise. With
"durable", PostgreSQL waits for the WAL flush (`synchronous_commit = on`) and
MongoDB for the journal (`w=1, j=true`). With "fast", neither waits for the disk
(`synchronous_commit = off`; `w=1, j=false`, MongoDB's default on one server).
The flat table is a materialized view, so W7 compares the normalised schema
with MongoDB only. Each run deletes what it inserted.

## Control experiments

**Without indexes (`python bench/run_bench.py --no-index`).** A fast engine and a
good index look the same in a latency table, so this run separates them. It
copies the flat table and the `transactions` collection with **no secondary
index** (only the primary key / `_id` that both engines always keep), checks that
both copies still give the same answers, repeats W0–W3 on them and saves
`data/results/benchmark_noindex.json` with the slow-down factor of every query
(median without indexes ÷ median with the indexes of the main run). The W1 plan
is saved as well: a sequential scan in PostgreSQL, `COLLSCAN` in MongoDB. Only
the two flat configurations are repeated; the normalised schema's foreign keys
and joins cannot be taken away without changing what it is. The copies are
dropped when the run ends. Run the main benchmark first, on the same machine.

**Deep pages (`python bench/run_paging.py`).** The search pages use OFFSET so any
page number can be opened directly. OFFSET reads and discards every skipped row,
so page N costs about N times page 1. The script takes the application's real
search query, jumps to depths from 0 to 900,000, checks that OFFSET and keyset
paging (`WHERE (month, txn_id) < (…)`) return the same 20 rows, and times both
in PostgreSQL and MongoDB (`skip` vs a range query). Keyset time stays flat
with depth, which is why it would be the right design for an "infinite scroll"
or an API consumed by other programs. The website keeps OFFSET but limits a
search to its first 10,000 results (500 pages): nobody needs page 40,000 of
"every sale since 1990", and the cap bounds the cost of any request.
Output: `data/results/paging.json`.

## Results (2026-11-14, Laptop, Docker, 2 CPUs per database container (run 2))

PostgreSQL 17.5 (Debian 17.5-1.pgdg110+1) on x86_64-pc-linux-gnu + PostGIS 3.5.2, MongoDB 8.0.34, 985,219 sales, 300 measured runs after 30 warm-up runs. Each database container is limited to 2.0 CPUs and 2.0 GB.

Median latency in ms (95 % confidence interval of the median):

| Workload | Same answers | PostgreSQL (joins) | PostgreSQL (flat) | MongoDB |
|---|---|---|---|---|
| W0 Primary-key lookup | 20/20 | 1.52 (1.49–1.57) | 1.09 (1.06–1.12) | 1.48 (1.43–1.54) |
| W1 Filtered search | 20/20 | 4.98 (4.79–5.14) | 1.24 (1.20–1.27) | 1.93 (1.89–2.00) |
| W2 Median by town | 20/20 | 16.6 (15.9–17.3) | 6.82 (6.48–7.24) | 20.8 (19.6–21.4) |
| W3 Quarterly trend | 20/20 | 25.4 (24.1–27.5) | 9.55 (8.80–10.2) | 29.4 (27.8–32.8) |
| W4 Nearest station | 20/20 | 1.06 (1.02–1.10) | n/a | 2.07 (1.98–2.19) |
| W5 Blocks within 1 km | 20/20 | 1.32 (1.28–1.38) | n/a | 1.97 (1.77–2.42) |

W7 single-row writes, median ms (same answers after every step in 20/20 checks):

| Write | Commit waits for disk | PostgreSQL | MongoDB |
|---|---|---|---|
| insert | yes | 3.47 | 3.62 |
| update | yes | 2.52 | 2.79 |
| delete | yes | 2.17 | 2.66 |
| insert | no | 1.23 | 1.36 |
| update | no | 1.12 | 1.30 |
| delete | no | 1.01 | 1.25 |

Storage on disk (MB):

| Data model | Data | Indexes |
|---|---|---|
| PostgreSQL, 5 normalised tables | 80.6 | 106.3 |
| PostgreSQL, one flat table | 125.1 | 133.4 |
| MongoDB, one document per sale (compressed) | 53.7 | 79.1 |

MongoDB holds 263.2 MB of BSON before compression.

Throughput, operations per second:

| Workload | Configuration | 1 | 2 | 4 | 8 threads |
|---|---|---|---|---|---|
| W1 | postgres | 168.2 | 344.5 | 387.4 | 243.0 |
| W1 | postgres_flat | 733.2 | 1,623.4 | 2,789.8 | 3,910.8 |
| W1 | mongodb | 601.0 | 1,186.4 | 2,103.9 | 2,072.6 |
| W4 | postgres | 951.8 | 1,860.2 | 3,569.6 | 5,542.4 |
| W4 | mongodb | 514.3 | 986.4 | 1,305.0 | 1,515.7 |

Bulk load of 200,000 rows: COPY 331,599 rows/s, insert_many 105,006 docs/s.

Without secondary indexes (`--no-index`), times slower than with them:

| Workload | PostgreSQL (flat) | MongoDB |
|---|---|---|
| W0 | 0.9× | 0.9× |
| W1 | 50.1× | 123.3× |
| W2 | 5.4× | 11.2× |
| W3 | 4.1× | 8.3× |

Deep paging, all sales, 900,000 rows in: PostgreSQL OFFSET 831.1 ms vs keyset 3.69 ms; MongoDB skip 206.5 ms vs keyset 1.68 ms.

### Run 1 against run 2

Run 1 (2026-10-26) recorded no container limits. The median ratios below are what the comparison rests on; if they hold across runs, the conclusions do.

| Workload | joins ÷ flat, run 1 | run 2 | MongoDB ÷ flat, run 1 | run 2 |
|---|---|---|---|---|
| W0 | 1.4× | 1.4× | 1.6× | 1.4× |
| W1 | 3.9× | 4.0× | 1.7× | 1.6× |
| W2 | 2.4× | 2.4× | 2.6× | 3.0× |
| W3 | 2.2× | 2.7× | 2.5× | 3.1× |

**Limits.** One laptop; Python client overhead is in every number (the same for
both engines); warm caches (the data fits in memory); single-node MongoDB; 2 CPUs
and 2 GB per database container. Regenerate this section with
`python bench/summary.py` after a new run.

