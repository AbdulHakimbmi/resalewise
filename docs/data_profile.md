# Data profile: resale_clean.csv

Checked with pandas and `scripts/load.py` on the full file. These numbers set the
bounds of the CHECK constraints in `sql/schema_extras.sql`; nothing in the real
data violates them.

| Item | Value |
|---|---|
| Rows | 985,219 (0 duplicates, 0 rejected by the plausibility checks) |
| Months | January 1990 to September 2026 |
| Towns | 27 |
| Flat types | 8 spellings → 7 after normalisation ("MULTI GENERATION" / "MULTI-GENERATION") |
| Flat models | 21 (normalised: upper case, hyphens → spaces) |
| Blocks | 10,010 (block number + street + town) |
| Blocks without HDB property information | 192 blocks, 16,016 sales (mostly demolished estates, e.g. Queenstown, Clementi, Bukit Merah). The first loader dropped these sales silently; they are now kept with empty block attributes (`blocks.in_property_info = false`). |
| Floor area | 28 – 366.7 sqm (CHECK 20 – 400) |
| Resale price | $5,000 – $1,728,000 (CHECK > 0 and < $10m) |
| Storeys | 1 – 51; storey_low ≤ storey_high everywhere (CHECK 1 – 60) |
| Lease commencement | 1966 – 2023 (CHECK 1960 – 2100) |
| Remaining lease | 0 – 1,212 months (CHECK 0 – 1,260); 51 rows exceed 99 years, a quirk of how remaining lease was derived for early records |
| Sales since 2017 | 238,611 (used by the price-driver analysis) |
| Load time | 38.0 s locally (COPY 22.9 s, indexes 2.5 s, constraints + views 2.8 s) |
