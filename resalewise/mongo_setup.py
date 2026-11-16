"""MongoDB schema: collections, $jsonSchema validators and indexes.

MongoDB is schema-flexible, but the application still relies on a few fields
always being there with the right type. The validators make the database
itself reject a malformed document (validationLevel "moderate": existing
documents that predate the rule are left alone, new writes must comply).

Collections
-----------
places        MRT/LRT stations as GeoJSON MultiPoint (exits)      $geoNear
amenities     parks, hawker centres, gyms, childcare, schools     $geoNear (Point or MultiPoint)
blocks_geo    every geocoded HDB block as a GeoJSON Point         $geoNear / $geoWithin
transactions  denormalised copy of the resale sales               SQL-vs-NoSQL benchmark

Run directly to (re)apply everything:  python -m resalewise.mongo_setup
"""
from pymongo import ASCENDING, DESCENDING, GEOSPHERE
from pymongo.errors import CollectionInvalid, OperationFailure

POINT = {
    "bsonType": "object", "required": ["type", "coordinates"],
    "properties": {
        "type": {"enum": ["Point"]},
        # GeoJSON order is [longitude, latitude]; the ranges catch a swap.
        "coordinates": {"bsonType": "array", "minItems": 2, "maxItems": 2,
                        "items": [{"bsonType": "double", "minimum": -180, "maximum": 180},
                                  {"bsonType": "double", "minimum": -90, "maximum": 90}]},
    },
}

VALIDATORS = {
    "places": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["name", "kind", "loc", "centroid", "source"],
        "properties": {
            "name":     {"bsonType": "string"},
            "kind":     {"enum": ["mrt", "lrt"]},
            "codes":    {"bsonType": "array", "items": {"bsonType": "string"}},
            "loc":      {"bsonType": "object", "required": ["type", "coordinates"],
                         "properties": {"type": {"enum": ["Point", "MultiPoint"]}}},
            "centroid": POINT,
            "opened_year": {"bsonType": ["int", "long", "null"]},
            "source":   {"bsonType": "string"},
        },
    }},
    "amenities": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["name", "kind", "loc", "centroid", "n_points", "source"],
        "properties": {
            "name":     {"bsonType": "string", "minLength": 1},
            "kind":     {"enum": ["park", "hawker", "gym", "childcare", "primary_school", "secondary_school"]},
            "loc":      {"bsonType": "object", "required": ["type", "coordinates"],
                         "properties": {"type": {"enum": ["Point", "MultiPoint"]}}},
            "centroid": POINT,
            "n_points": {"bsonType": ["int", "long"], "minimum": 1},
            "source":   {"bsonType": "string"},
        },
    }},
    "blocks_geo": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["town", "block_no", "street", "loc"],
        "properties": {
            "town": {"bsonType": "string"}, "block_no": {"bsonType": "string"},
            "street": {"bsonType": "string"}, "loc": POINT,
            "match_quality": {"enum": ["exact", "fuzzy"]},
        },
    }},
    "transactions": {"$jsonSchema": {
        "bsonType": "object",
        "required": ["month", "town", "flat_type", "price", "floor_area_sqm"],
        "properties": {
            "month": {"bsonType": "date"}, "town": {"bsonType": "string"},
            "flat_type": {"bsonType": "string"}, "flat_model": {"bsonType": "string"},
            "price": {"bsonType": ["double", "int", "long", "decimal"], "minimum": 1},
            "floor_area_sqm": {"bsonType": ["double", "int", "long", "decimal"], "minimum": 1},
            "remaining_lease_months": {"bsonType": ["int", "long", "null"]},
        },
    }},
}

INDEXES = {
    "places": [
        ([("loc", GEOSPHERE)], {"name": "loc_2dsphere"}),
        ([("codes", ASCENDING)], {"name": "codes"}),
    ],
    # ONE geospatial index on `loc` (a second one would make $geoNear ask which to use);
    # `kind` has its own ordinary index for the per-kind queries and reloads.
    "amenities": [
        ([("loc", GEOSPHERE)], {"name": "loc_2dsphere"}),
        ([("kind", ASCENDING)], {"name": "kind"}),
    ],
    "blocks_geo": [
        ([("loc", GEOSPHERE)], {"name": "loc_2dsphere"}),
        ([("town", ASCENDING)], {"name": "town"}),
    ],
    # Mirrors the PostgreSQL indexes that serve the same benchmark queries
    # ("index parity"): see bench/README.md for the side-by-side table.
    "transactions": [
        ([("month", DESCENDING), ("_id", DESCENDING)], {"name": "month_desc_id"}),
        # Equality-Sort-Range: the _id tie-breaker of the sort must be in the
        # index too, or MongoDB sorts the matches in memory (see bench/README.md).
        ([("town", ASCENDING), ("flat_type", ASCENDING), ("month", DESCENDING), ("_id", DESCENDING)],
         {"name": "town_type_month_id"}),
        ([("flat_type", ASCENDING), ("month", ASCENDING)], {"name": "type_month"}),
        ([("price", ASCENDING)], {"name": "price"}),
        ([("block_id", ASCENDING)], {"name": "block"}),
    ],
}


def ensure_collection(db, name):
    """Create the collection with its validator, or attach the validator to an
    existing collection (collMod). Returns the collection."""
    validator = VALIDATORS.get(name)
    try:
        if validator:
            db.create_collection(name, validator=validator, validationLevel="moderate",
                                 validationAction="error")
        else:
            db.create_collection(name)
    except CollectionInvalid:                    # already exists
        if validator:
            try:
                db.command("collMod", name, validator=validator, validationLevel="moderate",
                           validationAction="error")
            except OperationFailure as e:        # e.g. a restricted Atlas user
                print(f"[mongo] could not attach validator to {name}: {e}")
    coll = db[name]
    for keys, opts in INDEXES.get(name, []):
        try:
            coll.create_index(keys, **opts)
        except OperationFailure as e:
            print(f"[mongo] index {opts.get('name')} on {name} skipped: {e}")
    return coll


def ensure_all(db, names=None):
    for name in names or VALIDATORS:
        ensure_collection(db, name)


if __name__ == "__main__":                       # pragma: no cover
    from . import db as dbm
    ensure_all(dbm.mongo())
    print("MongoDB collections, validators and indexes are in place.")
