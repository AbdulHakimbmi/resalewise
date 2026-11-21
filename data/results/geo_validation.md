# Geospatial validation (2026-11-14T10:54:36+00:00)

| Check | Status | Criterion | Key numbers |
|---|---|---|---|
| V1 Coverage | PASS | >= 95 % of blocks and >= 99 % of 2017+ sales located | blocks=10010, exact=9792, fuzzy=0, unmatched=218, not_attempted=0 |
| V2 Plausibility | PASS | 100 % inside Singapore's bounding box; <= 0.5 % of blocks sit among neighbours of another town (6 of the 7 nearest from a different town) | located=9792, outside_bbox=0, neighbour_inconsistent=6, pct_neighbour_inconsistent=0.06, distance_test_flagged=351 |
| V3 Manual ground truth | PASS | at most 1 of 60 fresh blocks wrong on the map (2+ per town) | sampled=60, checked=60, correct=60, accuracy_pct=100.0, towns=27 |
| V4 Cross-engine distances | PASS | same nearest station >= 99.5 % (ties excluded); |Mongo - PostGIS sphere| <= 0.2 %; |Mongo - PostGIS spheroid| <= 1.0 % (beyond the 0.05 m storage rounding) | blocks_compared=9792, same_nearest_station=9792, ties_within_1pct=0, disagreements=0, agreement_pct_excl_ties=100.0 |
| V5 Edge cases | PASS | every probe behaves as specified | probes=11, failed=0, postgis_spheroid_boundary_flips=2 |
| V6 Idempotent rebuild | PASS | re-running geocode (from cache) and feature build reproduces the stored tables exactly | block_features_stored=cdff6fe9972f5cf1, block_features_rebuilt=cdff6fe9972f5cf1, block_locations_stored=b739c813a1b42f65, block_locations_rebuilt=b739c813a1b42f65 |
| V7 Amenity cross-check | PASS | on MongoDB's sphere every count and nearest distance matches PostGIS; on the WGS84 spheroid a count may differ only for a place within 1 % of a radius edge | rows_compared=58752, counts_identical_same_sphere=58752, counts_identical_same_sphere_pct=100.0, nearest_max_diff_same_sphere_m=0.05, counts_identical_spheroid_pct=88.85 |
