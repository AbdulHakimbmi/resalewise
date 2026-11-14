ResaleWise SG — Dataset Description
====================================

Source files
------------
1. resale_clean.csv
   Origin: HDB Resale Flat Prices (data.gov.sg)
   Description: Historical HDB resale flat transactions in Singapore.
   Approximate rows: ~985,000
   Column count: 21
   Coverage: 1990 to present (last refreshed 2026)

2. HDB Property Information (block-level metadata)
   Origin: HDB Property Information (data.gov.sg)
   Description: Block-level attributes merged into the transaction file
   during ETL (max_floor_lvl, year_completed, total_dwelling_units,
   commercial, market_hawker, multistorey_carpark, precinct_pavilion).

Column list of resale_clean.csv (post-ETL)
------------------------------------------
month                       — transaction month (YYYY-MM)
town                        — HDB town (UPPERCASE)
flat_type                   — e.g. "3 ROOM", "4 ROOM", "EXECUTIVE"
blk_no                      — block number
street_clean                — cleaned street name
storey_low, storey_high     — storey range of the unit
floor_area_sqm              — floor area in square metres
flat_model                  — e.g. "MODEL A", "NEW GENERATION"
lease_commence_date         — year lease commenced
remaining_lease_total_months— total remaining lease in months
resale_price                — resale price in SGD
max_floor_lvl               — block max floor level (from Property Info)
year_completed              — block completion year
total_dwelling_units        — number of units in the block
commercial                  — Y/N, block has commercial space
market_hawker               — Y/N, block has market/hawker centre
multistorey_carpark         — Y/N, block has multi-storey carpark
precinct_pavilion           — Y/N, block has precinct pavilion

Value conventions
-----------------
- town, flat_type, flat_model are stored UPPERCASE.
- month is stored as YYYY-MM (converted to DATE at load time).
- Boolean-ish columns use "Y" / "N".
- Missing numeric values are empty cells, not zero.
- remaining_lease_total_months is derived from lease_commence_date
  and month at ETL time.

Files included in this folder
-----------------------------
- sample_10_rows.csv    (10 representative rows — small enough to paste)
- sample_1000_rows.csv  (1000 rows for fuller context)
- DATASET_README.txt    (this file)

Notes
-----
The full dataset is ~985,000 rows / ~120 MB and is not shared in this
folder. Only the samples above are for reference.