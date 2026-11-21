"""
Seed town reviews into MongoDB so every town has a healthy set of community
perspectives. Tops each town up to a minimum number of reviews without
touching any that already exist.

Usage:
    python scripts/seed_reviews.py            # top up every town to 15 reviews
    python scripts/seed_reviews.py 20         # top up every town to 20 reviews
"""
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resalewise import db as dbm  # noqa: E402
from resalewise.mongo_setup import ensure_collection  # noqa: E402

# Connection settings come from .env (MONGO_URI); nothing is hard-coded.
db = dbm.mongo()
ensure_collection(db, "reviews")

MIN_PER_TOWN = int(sys.argv[1]) if len(sys.argv) > 1 else 15

# Town names are stored UPPERCASE in the reviews collection.
TOWNS = [
    "ANG MO KIO", "BEDOK", "BISHAN", "BUKIT BATOK", "BUKIT MERAH",
    "BUKIT PANJANG", "BUKIT TIMAH", "CENTRAL AREA", "CHOA CHU KANG",
    "CLEMENTI", "GEYLANG", "HOUGANG", "JURONG EAST", "JURONG WEST",
    "KALLANG/WHAMPOA", "MARINE PARADE", "PASIR RIS", "PUNGGOL",
    "QUEENSTOWN", "SEMBAWANG", "SENGKANG", "SERANGOON", "TAMPINES",
    "TOA PAYOH", "WOODLANDS", "YISHUN",
]

AUTHORS = [
    "Wei Ming", "Nurul", "Siti", "Ravi", "Jia Hui", "Marcus", "Priya",
    "Ahmad", "Denise", "Kelvin", "Sarah", "Faizal", "Mei Ling", "Daniel",
    "Aisyah", "Terence", "Rachel", "Hafiz", "Joanne", "Xin Yi", "Gopal",
    "Bryan", "Farah", "Cheryl", "Zhi Wei", "Nadia", "Ben", "Huda",
    "Adrian", "Suresh",
]

TAG_POOL = [
    "near-food", "family-friendly", "near-transport", "quiet",
    "mature-estate", "good-schools", "green-spaces", "busy-peak-hours",
    "great-hawkers", "convenient",
]

OPENERS = [
    "Been living here for a few years and", "Grew up around this area and",
    "Moved in recently and", "Family aside, honestly", "As a long-time resident,",
    "After renting here for a while,", "My family settled here and",
    "Stayed here since my BTO days and", "Compared to my old estate,",
    "First impressions after moving in:",
]

BODIES = [
    "the food options are excellent — the hawker centre never disappoints.",
    "getting to the MRT is a breeze, which makes the daily commute painless.",
    "it's a proper mature estate, so amenities are all within walking distance.",
    "the neighbourhood is quiet in the evenings, great for families.",
    "there are plenty of parks and green spaces for morning walks.",
    "the wet market and supermarkets nearby make grocery runs easy.",
    "schools in the area are well-regarded, which was a big draw for us.",
    "bus connectivity is solid even to parts without a nearby MRT.",
    "it can get a little crowded during peak hours, but nothing unbearable.",
    "the coffee shops here are affordable and the kopi is legit.",
    "renovation-friendly older blocks with generous floor areas.",
    "community spirit is strong — neighbours actually know each other.",
    "there's a good mix of new and older developments.",
    "the town centre has everything from clinics to banks in one spot.",
    "parking can be tight on weekends, so plan around it.",
]

CLOSERS = [
    "Would recommend to anyone shortlisting here.",
    "Overall very happy with the choice.",
    "Value for money considering the location.",
    "Only downside is prices have crept up lately.",
    "Great for young families especially.",
    "Would happily stay here long term.",
    "Worth a look if you value convenience.",
    "Solid all-rounder of an estate.",
    "",
    "",
]


def make_comment():
    parts = [random.choice(OPENERS), random.choice(BODIES)]
    closer = random.choice(CLOSERS)
    if closer:
        parts.append(closer)
    return " ".join(parts)


def make_review(town):
    # Ratings skew positive but with some variety (2–5).
    rating = random.choices([2, 3, 4, 5], weights=[1, 3, 6, 5])[0]
    days_ago = random.randint(3, 720)
    return {
        "town": town,
        "rating": rating,
        "comment": make_comment(),
        "tags": random.sample(TAG_POOL, k=random.randint(1, 3)),
        "author": random.choice(AUTHORS),
        "created_at": datetime.utcnow() - timedelta(days=days_ago,
                                                    hours=random.randint(0, 23)),
        "seeded": True,   # marks generated reviews so they can be identified
    }


def main():
    total_added = 0
    print(f"Ensuring at least {MIN_PER_TOWN} reviews per town...\n")
    for town in TOWNS:
        existing = db.reviews.count_documents({"town": town})
        need = max(0, MIN_PER_TOWN - existing)
        if need == 0:
            print(f"  {town:<16} {existing} reviews — ok")
            continue
        docs = [make_review(town) for _ in range(need)]
        db.reviews.insert_many(docs)
        total_added += need
        print(f"  {town:<16} {existing} -> {existing + need} (+{need})")

    print(f"\nDone. Added {total_added} reviews.")
    print("Total reviews now:", db.reviews.count_documents({}))


if __name__ == "__main__":
    main()
