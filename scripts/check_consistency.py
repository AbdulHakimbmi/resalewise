"""Cross-database consistency check (PostgreSQL <-> MongoDB).

    python scripts/check_consistency.py            # report only
    python scripts/check_consistency.py --fix      # also anonymise orphaned reviews

Two databases cannot share one transaction, so some facts exist in both and can
drift apart. This script looks for the drift the application can cause:

  1. Reviews whose `user_id` points at an account that no longer exists in
     PostgreSQL. Deleting an account anonymises the person's reviews as part of
     the same request (routes/account.py); a crash between the two databases, or
     a user removed by hand in SQL, would leave the review carrying a name.
     --fix sets author = "Former member" and removes user_id (safe to repeat).
  2. The denormalised copy of the sales in MongoDB (`transactions`) versus the
     PostgreSQL table: row counts must match, or the SQL-vs-NoSQL benchmark is
     comparing different data. Reported only (reload with
     scripts/load_mongo_transactions.py).

Exit status 0 when both checks pass, 1 otherwise (so it can run in a script).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resalewise import db  # noqa: E402


def orphan_owner_ids(pg_user_ids, review_owner_ids):
    """Review owners that have no account: the ids in MongoDB but not in PostgreSQL."""
    return sorted(set(review_owner_ids) - set(pg_user_ids))


def anonymise_reviews(reviews, user_ids):
    """Strip the owner from every review written by one of `user_ids`; returns how many changed."""
    if not user_ids:
        return 0
    res = reviews.update_many({"user_id": {"$in": list(user_ids)}},
                              {"$set": {"author": "Former member"}, "$unset": {"user_id": ""}})
    return getattr(res, "modified_count", 0) or 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fix", action="store_true", help="anonymise orphaned reviews")
    args = ap.parse_args()
    ok = True

    pg_users = [r["user_id"] for r in db.q("SELECT user_id FROM users")]
    reviews = db.mongo().reviews
    owners = [i for i in reviews.distinct("user_id") if i is not None]
    orphans = orphan_owner_ids(pg_users, owners)
    n_reviews = reviews.count_documents({"user_id": {"$in": orphans}}) if orphans else 0
    print(f"[1] Reviews with an owner: {len(owners)} distinct owners, {len(pg_users)} accounts in PostgreSQL")
    if orphans:
        ok = False
        print(f"    PROBLEM: {n_reviews} review(s) belong to {len(orphans)} account(s) that no longer exist: {orphans}")
        if args.fix:
            print(f"    fixed: {anonymise_reviews(reviews, orphans)} review(s) anonymised")
        else:
            print("    run again with --fix to anonymise them")
    else:
        print("    ok: every review owner still has an account")

    n_pg = db.q("SELECT COUNT(*) AS n FROM resale_transactions")[0]["n"]
    n_mongo = db.mongo().transactions.estimated_document_count()
    print(f"[2] Sales: PostgreSQL {n_pg:,} rows, MongoDB `transactions` {n_mongo:,} documents")
    if n_mongo == n_pg:
        print("    ok: the copy is complete")
    elif n_mongo == 0:
        print("    note: the MongoDB copy has not been loaded (python scripts/load_mongo_transactions.py)")
    else:
        ok = False
        print("    PROBLEM: counts differ; reload with python scripts/load_mongo_transactions.py")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
