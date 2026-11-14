# Reference data

## `station_opening_years.csv`

One row per station code (221 codes: North-South, East-West, North East, Circle,
Downtown and Thomson-East Coast lines, plus the Bukit Panjang, Sengkang and
Punggol LRT). Columns:

| column | meaning |
|---|---|
| `code` | station code as printed by `scripts/load_stations.py` and stored in `mrt_stations.codes` |
| `opened_year` | year the station opened (the only column the code reads) |
| `station`, `note` | station name and the exact date or construction stage, for checking by hand |

How it is used:

- `scripts/load_stations.py` gives each station the **earliest** year among its
  codes (an interchange opens when its first line does). It prints any station
  left without a year.
- `analysis/price_drivers.py` ignores the MRT distance of a sale when the
  block's nearest station opened in a later calendar year than the sale
  (`opened_year > sale_year`). The comparison is by year, so a sale in the
  opening year counts as "open"; the count of ignored sales is written to
  `drivers.json` as `excluded_station_not_open`.
- Validation check V5 reports how many stations have a year.

Sources (opening dates by line and stage): the published station lists on
Wikipedia (line and LRT articles), SGTrains network figures, and LTA's news
release for Circle Line Stage 6 (12 July 2026). Cross-checked against each
other; **spot-check a few stations against LTA before submitting.**

Known limits, to state in the report:

- Year granularity, not month.
- Four stations on the extensions due in the second half of 2026 (DT36 Xilin,
  DT37/TE31 Sungei Bedok, TE30 Bedok South) are recorded as 2026 because the
  opening was scheduled but not confirmed when the file was written. No sale
  before 2026 is counted as near them.
- Stations that are planned but not built (NS3A, TE10, TE21, CC18, and others)
  are not listed.
- If a block's nearest station was not yet open, the block is left out of the
  MRT bands for that sale (it is not re-assigned to the next nearest open
  station). This is conservative: it shrinks the MRT groups slightly.

## `station_exclusions.csv`

OneMap's search for "MRT STATION" also returns things that are not open stations.
`scripts/load_stations.py` drops every `name,kind` listed here before building
the station list. Two kinds of entry:

- **Not a station:** shops and restaurants named after a station (Burger King,
  Khun Thai and Popeyes at Woodlands, "Prommenade").
- **Not built yet:** Circle Line CC18 Bukit Brown, Thomson-East Coast Line TE10
  Mount Pleasant and TE21 Marina South, and Cross Island Line CR4, CR07 and
  CR12 (Pasir Ris East, Defu, Teck Ghee). Counting them would put some blocks
  "within 500 m of an MRT station" that have none.

The loader also prints a CHECK line for any MRT "station" with no station code,
because that is how the restaurants showed up. Remaining stations without an
opening year are the Punggol and Sengkang LRT hubs (no code in OneMap; both
opened before 2006, so they are treated as open).
