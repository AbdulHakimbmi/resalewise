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
anything is timed (same ids in the same order, same medians, same station, set
overlap ≥ 98 % for the 1 km radius).

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
interval for the median (2,000 resamples). Then throughput at 1, 4 and 8
threads for 10 s each, bulk-load rate (COPY vs `insert_many`, 200,000 rows,
3 repeats), and query plans (`EXPLAIN (ANALYZE, BUFFERS)` and
`explain("executionStats")`). Versions, settings, CPU count and the full index
lists are saved in the results file.

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
