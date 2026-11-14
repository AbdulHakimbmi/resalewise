"""Value benchmarks: judge a sale against sales from ITS OWN period.

HDB prices rise over the decades, so a 1995 flat is always "cheaper" than the
current median. Comparing across years is meaningless, so every sale is
compared with the median for the same town + flat type in the same quarter.
If that quarter has too few sales we widen the comparison, and say so:
  1. same town + flat type, same quarter
  2. same town + flat type, same calendar year
  3. same flat type in ALL towns, same quarter
"""
from . import db
from .web import cache_get, cache_put

BENCH_MIN_N = 5
BENCH_TTL = 24 * 3600          # historical data never changes
_BENCH_LEVELS = (1, 2, 3)


def _quarter_start(d):
    return d.replace(month=3 * ((d.month - 1) // 3) + 1, day=1)


def _add_months(d, n):
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def norm_flat_type(value):
    """HDB spells one flat type two ways over the years ("MULTI GENERATION" and
    "MULTI-GENERATION"). Treat them as one so the site shows a single option and
    results/benchmarks include both spellings."""
    return value.replace("-", " ").upper() if isinstance(value, str) else value


def norm_flat_model(value):
    """Same idea for flat models, which also mix case and hyphens in the raw data."""
    return value.replace("-", " ").upper() if isinstance(value, str) else value


def verdict_for(pct):
    """One shared set of thresholds for every 'value' label on the site."""
    if pct is None:   return "No comparison available"
    if pct <= -8:     return "Great value"
    if pct < -2:      return "Good value"
    if pct <= 2:      return "Around market rate"
    if pct <= 8:      return "Above market rate"
    return "Premium"


def _bench_label(level, town, ftype, qs):
    q_ = "Q%d %d" % ((qs.month - 1) // 3 + 1, qs.year)
    if level == 1:
        return "%s %s, %s" % (town.title(), ftype.title(), q_)
    if level == 2:
        return "%s %s, %d" % (town.title(), ftype.title(), qs.year)
    return "%s in all towns, %s" % (ftype.title(), q_)


def _bench_rows(level, keys):
    """One query for every (town, type, quarter) still unresolved at `level`."""
    conds, params, seen = [], [], set()
    for town, ftype, qs in keys:
        if level == 2:
            lo, hi = qs.replace(month=1), qs.replace(year=qs.year + 1, month=1)
        else:
            lo, hi = qs, _add_months(qs, 3)
        ident = (town, ftype, lo) if level != 3 else (ftype, lo)
        if ident in seen:
            continue
        seen.add(ident)
        if level == 3:
            conds.append("(UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s AND t.month >= %s AND t.month < %s)")
            params += [ftype, lo, hi]
        else:
            conds.append("(tw.town_name = %s AND UPPER(REPLACE(ft.flat_type_name, '-', ' ')) = %s AND t.month >= %s AND t.month < %s)")
            params += [town, ftype, lo, hi]
    if not conds:
        return {}
    unit = "year" if level == 2 else "quarter"
    rows = db.q("""
      SELECT %s AS town_name, UPPER(REPLACE(ft.flat_type_name, '-', ' ')) AS flat_type_name,
             DATE_TRUNC('%s', t.month)::date AS period,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price))::int AS median_price,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (t.resale_price / NULLIF(t.floor_area_sqm, 0))))::int AS median_psm,
             ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.remaining_lease_months))::int AS median_lease_months,
             COUNT(*)::int AS n
      FROM resale_transactions t
      JOIN blocks b      ON b.block_id = t.block_id
      JOIN towns tw      ON tw.town_id = b.town_id
      JOIN flat_types ft ON ft.flat_type_id = t.flat_type_id
      WHERE %s
      GROUP BY 1, 2, 3
    """ % ("tw.town_name" if level != 3 else "NULL::text", unit, " OR ".join(conds)), params)
    out = {}
    for r in rows:
        ident = (r["town_name"], r["flat_type_name"], r["period"]) if level != 3 \
            else (r["flat_type_name"], r["period"])
        out[ident] = r
    return out


def benchmarks_for(items):
    """items: [(town_name, flat_type_name, month_date)] -> list of benchmark dicts
    (or None when there is nothing comparable), aligned with `items`."""
    keys = [(t, f, _quarter_start(m)) for t, f, m in items]
    result, todo = {}, []
    for k in dict.fromkeys(keys):
        found, val = cache_get("bench:%s|%s|%s" % (k[0], k[1], k[2].isoformat()), BENCH_TTL)
        if found:
            result[k] = val
        else:
            todo.append(k)
    pending = list(todo)
    for level in _BENCH_LEVELS:
        if not pending:
            break
        got = _bench_rows(level, pending)
        still = []
        for k in pending:
            town, ftype, qs = k
            if level == 1:    r = got.get((town, ftype, qs))
            elif level == 2:  r = got.get((town, ftype, qs.replace(month=1)))
            else:             r = got.get((ftype, qs))
            if r and r["n"] >= BENCH_MIN_N and r["median_price"]:
                result[k] = {"median_price": r["median_price"], "median_psm": r["median_psm"],
                             "median_lease_months": r.get("median_lease_months"),
                             "n": r["n"], "level": level,
                             "label": _bench_label(level, town, ftype, qs)}
            else:
                still.append(k)
        pending = still
    for k in todo:                              # remember misses too
        result.setdefault(k, None)
        cache_put("bench:%s|%s|%s" % (k[0], k[1], k[2].isoformat()), result[k])
    return [result[k] for k in keys]


def value_score(price_diff_pct, lease_years, peer_lease_years):
    """Explainable 0-100 score: 50 = exactly at the period's median. Every 1 %
    below the median adds 1.6 points; more remaining lease than the peers sold
    in that period adds up to 12 points (less takes up to 12 away). None when
    there is nothing comparable — we never invent a score."""
    if price_diff_pct is None:
        return None
    score = 50.0 - price_diff_pct * 1.6
    score += max(min((lease_years - peer_lease_years) * 0.4, 12), -12)
    return max(0, min(100, round(score)))
