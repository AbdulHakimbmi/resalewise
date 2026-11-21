"""Print the results section of bench/README.md from the saved result files, so the
README always shows the numbers the scripts produced.

    python bench/summary.py > results.md
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "data", "results")


def load(name):
    path = os.path.join(RES, name)
    return json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else None


def ms(x):
    return "–" if x is None else f"{x:.2f}" if x < 10 else f"{x:.1f}"


def main():
    b, b1 = load("benchmark.json"), load("benchmark_run1_2026-10-26.json")
    ni, pg = load("benchmark_noindex.json"), load("paging.json")
    env = b["environment"]
    box = (env.get("containers") or {}).get("resalewise-postgres") or {}
    out = [f"## Results ({b['generated_at'][:10]}, {b['label']})", "",
           f"{env['postgres']} + PostGIS {env['postgis']}, MongoDB {env['mongodb']}, "
           f"{env['rows_postgres']:,} sales, {b['protocol']['runs']} measured runs after "
           f"{b['protocol']['warmup']} warm-up runs. Each database container is limited to "
           f"{box.get('cpus', '?')} CPUs and {box.get('memory_gb', '?')} GB.", "",
           "Median latency in ms (95 % confidence interval of the median):", "",
           "| Workload | Same answers | PostgreSQL (joins) | PostgreSQL (flat) | MongoDB |", "|---|---|---|---|---|"]
    for w in b["workloads"]:
        cell = lambda c: (f"{ms(w['latency'][c]['p50_ms'])} ({ms(w['latency'][c]['p50_ci95'][0])}–"
                          f"{ms(w['latency'][c]['p50_ci95'][1])})" if c in w["latency"] else "n/a")
        eq = w["equivalence"]
        out.append(f"| {w['id']} {w['title']} | {eq['passed']}/{eq['checked']} | {cell('postgres')} | "
                   f"{cell('postgres_flat')} | {cell('mongodb')} |")

    wr = b.get("writes")
    if wr:
        out += ["", f"W7 single-row writes, median ms (same answers after every step in "
                    f"{wr['equivalence']['passed']}/{wr['equivalence']['checked']} checks):", "",
                "| Write | Commit waits for disk | PostgreSQL | MongoDB |", "|---|---|---|---|"]
        for mode, label in (("durable", "yes"), ("fast", "no")):
            for op in wr["ops"]:
                m = wr["modes"][mode][op]
                out.append(f"| {op} | {label} | {ms(m['postgres']['p50_ms'])} | {ms(m['mongodb']['p50_ms'])} |")

    st = b.get("storage")
    if st:
        out += ["", "Storage on disk (MB):", "", "| Data model | Data | Indexes |", "|---|---|---|"]
        for c, label in (("postgres", "PostgreSQL, 5 normalised tables"), ("postgres_flat", "PostgreSQL, one flat table"),
                         ("mongodb", "MongoDB, one document per sale (compressed)")):
            out.append(f"| {label} | {st[c]['data_mb']} | {st[c]['index_mb']} |")
        out.append(f"\nMongoDB holds {st['mongodb']['uncompressed_data_mb']} MB of BSON before compression.")

    out += ["", "Throughput, operations per second:", "", "| Workload | Configuration | 1 | 2 | 4 | 8 threads |", "|---|---|---|---|---|---|"]
    rows = {}
    for c in b["concurrency"]:
        rows.setdefault((c["workload"], c["config"]), {})[c["threads"]] = c["ops_per_s"]
    for (wid, cfg), by in rows.items():
        out.append(f"| {wid} | {cfg} | " + " | ".join(f"{by.get(n, '–'):,}" if n in by else "–" for n in (1, 2, 4, 8)) + " |")

    bl = b["bulk_load"]
    out += ["", f"Bulk load of {bl['rows']:,} rows: COPY {bl['postgres_copy']['rows_per_s_median']:,} rows/s, "
                f"insert_many {bl['mongodb_insert_many']['rows_per_s_median']:,} docs/s."]
    if ni:
        out += ["", "Without secondary indexes (`--no-index`), times slower than with them:", "",
                "| Workload | PostgreSQL (flat) | MongoDB |", "|---|---|---|"]
        for wid, f in ni["index_effect"]["slowdown_x"].items():
            out.append(f"| {wid} | {f['postgres_flat']}× | {f['mongodb']}× |")
    if pg:
        deep = [c for c in pg["cases"] if c["filter"] == "all sales"][-1]
        out += ["", f"Deep paging, all sales, {deep['depth']:,} rows in: PostgreSQL OFFSET "
                    f"{ms(deep['postgres']['offset']['p50_ms'])} ms vs keyset {ms(deep['postgres']['keyset']['p50_ms'])} ms; "
                    f"MongoDB skip {ms(deep['mongodb']['skip']['p50_ms'])} ms vs keyset {ms(deep['mongodb']['keyset']['p50_ms'])} ms."]

    if b1:
        out += ["", f"### Run 1 against run 2", "",
                f"Run 1 ({b1['generated_at'][:10]}) recorded no container limits. The median ratios below are what the "
                "comparison rests on; if they hold across runs, the conclusions do.", "",
                "| Workload | joins ÷ flat, run 1 | run 2 | MongoDB ÷ flat, run 1 | run 2 |", "|---|---|---|---|---|"]
        one = {w["id"]: w for w in b1["workloads"]}
        for w in b["workloads"]:
            o = one.get(w["id"])
            if not o or "postgres_flat" not in w["latency"]:
                continue
            r = lambda x, c: x["latency"][c]["p50_ms"] / x["latency"]["postgres_flat"]["p50_ms"]
            out.append(f"| {w['id']} | {r(o, 'postgres'):.1f}× | {r(w, 'postgres'):.1f}× | {r(o, 'mongodb'):.1f}× | {r(w, 'mongodb'):.1f}× |")
    print("\n".join(out))


if __name__ == "__main__":
    main()
