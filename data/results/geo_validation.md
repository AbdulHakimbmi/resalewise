# Geospatial validation (2026-10-24T00:11:38+00:00)

| Check | Status | Criterion | Key numbers |
|---|---|---|---|
| V1 Coverage | PASS | >= 95 % of blocks and >= 99 % of 2017+ sales located | blocks=10010, exact=9765, fuzzy=0, unmatched=245, not_attempted=0 |
| V2 Plausibility | PASS | 100 % inside Singapore's bounding box; <= 0.5 % of blocks sit among neighbours of another town (6 of the 7 nearest from a different town) | located=9765, outside_bbox=0, neighbour_inconsistent=6, pct_neighbour_inconsistent=0.06, distance_test_flagged=351 |
| V3 Manual ground truth | PASS | >= 29 of 30 sampled blocks confirmed on the map | sampled=30, checked=30, correct=30, accuracy_pct=100.0 |
| V4 Cross-engine distances | PASS | same nearest station >= 99.5 % (ties excluded); |Mongo - PostGIS sphere| <= 0.2 %; |Mongo - PostGIS spheroid| <= 1.0 % (beyond the 0.05 m storage rounding) | blocks_compared=9765, same_nearest_station=9765, ties_within_1pct=0, disagreements=0, agreement_pct_excl_ties=100.0 |
| V5 Edge cases | PASS | every probe behaves as specified | probes=11, failed=0, postgis_spheroid_boundary_flips=2 |
| V6 Idempotent rebuild | PASS | re-running geocode (from cache) and feature build reproduces the stored tables exactly | block_features_stored=cf6818247935dec2, block_features_rebuilt=cf6818247935dec2, block_locations_stored=4e09a52db637eae5, block_locations_rebuilt=4e09a52db637eae5 |
