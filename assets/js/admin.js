/*
  Admin page (admin.html). One tab per tool, each loaded the first time it is opened:
    overview, console (SQL or MongoDB), cube, index, concurrency
  Each tool also shows the same thing as terminal commands to copy (renderTerm).
  The server decides who is an admin on every request, this page only shows what it gets.
  Needs api.js (escapeHtml) and auth.js (Auth, authFetch).
*/
(function () {
    "use strict";
    const $ = (id) => document.getElementById(id);
    const esc = escapeHtml;
    const nf = new Intl.NumberFormat("en-SG", { maximumFractionDigits: 2 });
    // no separators on years and small ids, big numbers get them
    const num = (n) => (Number.isInteger(n) && Math.abs(n) < 10000 ? String(n) : nf.format(n));
    const money = (n) => (n == null ? "—" : "$" + nf.format(Math.round(n)));
    const ms = (n) => (n < 1 ? n.toFixed(3) : n < 100 ? n.toFixed(1) : nf.format(Math.round(n))) + " ms";

    // who may be here
    function gate(title, text, login) {
        $("admApp").hidden = true;
        $("admGate").hidden = false;
        $("admGateTitle").textContent = title;
        $("admGateText").textContent = text;
        $("admGateLink").hidden = !login;
    }
    if (!Auth.isLoggedIn()) {
        gate("Administrators only", "Sign in with the administrator account to use this page.", true);
        return;
    }

    async function api(path, body) {
        let res;
        try {
            res = await authFetch(path, body === undefined ? {} : {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        } catch (e) {
            throw new Error("Can’t reach the server. Check that it is running and try again.");
        }
        const data = await res.json().catch(() => ({}));
        if (res.status === 401) {
            Auth.logout();
            gate("Your session has ended", "Log in again to carry on.", true);
            throw new Error("Please log in again.");
        }
        if (res.status === 403) {
            gate("Administrators only", "You are signed in, but this account is not an administrator.", false);
            throw new Error(data.error || "Administrators only.");
        }
        if (!res.ok) throw new Error(data.error || "Something went wrong (" + res.status + "). Please try again.");
        return data;
    }
    const fail = (el, e) => { el.innerHTML = '<p class="adm-error" role="alert">' + esc(e.message) + "</p>"; };

    // terminal panels: the same thing as commands to copy and repeat by hand
    let term = { postgres: "psql", mongo: "mongosh", local: false };          // the real commands come with /api/admin/overview
    const asSql = (sql) => String(sql).trim().replace(/;\s*$/, "") + ";";
    const openStep = (which) => ({
        label: which === "mongo" ? "Open the MongoDB shell" : "Open psql",
        code: term[which],
        note: term.local ? "Docker must be running." : "It asks for the database password; it is in your .env file.",
    });
    function renderTerm(id, intro, steps) {
        const box = $(id);
        if (!box) return;
        const old = box.querySelector("details");
        box.innerHTML = '<details class="adm-term-box"' + (old && old.open ? " open" : "") + "><summary>Terminal commands</summary>" +
            (intro ? '<p class="adm-term-intro">' + esc(intro) + "</p>" : "") +
            (steps.length ? '<ol class="adm-term-steps">' + steps.map((st) =>
                "<li>" + '<div class="adm-term-label"><span>' + (st.session ? '<b class="adm-who ' + esc(st.session.toLowerCase()) + '">' + esc(st.session) + "</b> " : "") + esc(st.label) +
                '</span><button type="button" class="adm-copy" aria-label="Copy: ' + esc(st.label) + '">Copy</button></div>' +
                "<pre><code>" + esc(st.code) + "</code></pre>" +
                (st.note ? '<p class="adm-term-note' + (st.failed ? " bad" : "") + '">' + esc(st.note) + "</p>" : "") + "</li>").join("") + "</ol>" : "") + "</details>";
    }
    document.addEventListener("click", async (e) => {
        const b = e.target.closest(".adm-copy");
        if (!b) return;
        const text = b.closest("li").querySelector("code").textContent;
        try { await navigator.clipboard.writeText(text); }
        catch (err) {
            const t = document.createElement("textarea");
            t.value = text; document.body.appendChild(t); t.select();
            try { document.execCommand("copy"); } catch (e2) { /* nothing more to do, the code can still be selected */ }
            t.remove();
        }
        b.textContent = "Copied";
        setTimeout(() => { b.textContent = "Copy"; }, 1500);
    });

    // small renderers
    function cell(v) {
        if (v === null || v === undefined) return '<span class="rwx-muted">null</span>';
        if (typeof v === "number") return num(v);
        if (typeof v === "object") return esc(JSON.stringify(v));
        return esc(String(v));
    }
    function table(columns, rows, extra) {
        const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || typeof r[i] === "number"));
        const cls = (i) => (numeric[i] ? ' class="num"' : "");
        return '<div class="rwx-table-wrap"><table class="rwx-table adm-table ' + (extra || "") + '"><thead><tr>' +
            columns.map((c, i) => '<th scope="col"' + cls(i) + ">" + esc(c) + "</th>").join("") + "</tr></thead><tbody>" +
            rows.map((r) => "<tr>" + r.map((v, i) => "<td" + cls(i) + ">" + cell(v) + "</td>").join("") + "</tr>").join("") +
            "</tbody></table></div>";
    }

    // tabs
    const tabs = [...document.querySelectorAll('.adm-tabs [role="tab"]')];
    const firstOpen = {};        // tab id -> set-up to run the first time it is shown
    const everyOpen = {};        // tab id -> refresh to run every time it is shown
    function openTab(tab, focus) {
        tabs.forEach((t) => {
            const on = t === tab;
            t.setAttribute("aria-selected", on ? "true" : "false");
            t.tabIndex = on ? 0 : -1;
            $(t.getAttribute("aria-controls")).hidden = !on;
        });
        if (focus) tab.focus();
        try { history.replaceState(null, "", "#" + tab.id.slice(2)); } catch (e) { /* file:// */ }
        if (firstOpen[tab.id]) { const f = firstOpen[tab.id]; firstOpen[tab.id] = null; f(); }
        else if (everyOpen[tab.id]) everyOpen[tab.id]();
    }
    tabs.forEach((t, i) => {
        t.addEventListener("click", () => openTab(t));
        t.addEventListener("keydown", (e) => {
            const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
            if (to === undefined) return;
            e.preventDefault();
            openTab(tabs[(to + tabs.length) % tabs.length], true);
        });
    });

    // overview
    let overview = null;
    function renderOverview(o) {
        const size = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB");
        $("admOverview").innerHTML =
            '<div class="adm-two"><div><h3>PostgreSQL ' + esc(o.postgres.version || "") + "</h3>" +
            table(["Table", "Kind", "Rows", "Size", "Indexes"], o.postgres.tables.map((t) => [t.name, t.kind, t.rows, size(t.bytes), t.indexes])) +
            '</div><div><h3>MongoDB ' + esc(o.mongo.version || "(not reachable)") + "</h3>" +
            table(["Collection", "Documents", "Indexes"], o.mongo.collections.map((c) => [c.name, c.docs, c.indexes])) +
            "</div></div>" +
            '<p class="rwx-note">Signed in as <b>' + esc(o.admin.name || o.admin.email) + "</b>. SQL console guard: " +
            (o.postgres.readonly_role
                ? "<b>read-only role</b> (the database itself refuses writes and hides password hashes)."
                : "<b>read-only transaction</b> (this server may not create the read-only role).") + "</p>";
    }

    function overviewTerm() {
        renderTerm("ovTerm", "Look around both databases by hand.", [
            openStep("postgres"),
            { label: "Paste at the psql prompt", code: "-- every table, with its size\n\\dt+\n-- the columns, keys and indexes of the sales table\n\\d resale_transactions\n-- every index, with its size\n\\di+" },
            openStep("mongo"),
            { label: "Paste at the mongosh prompt", code: "show collections\ndb.reviews.getIndexes()\ndb.reviews.countDocuments()\ndb.transactions.findOne()" },
        ]);
    }

    // console
    const SQL_EXAMPLES = [
        ["GROUP BY: sales and average price per town, 2025",
         "SELECT town_name, COUNT(*) AS sales, ROUND(AVG(resale_price)) AS average_price\nFROM v_transactions\nWHERE sale_year = 2025\nGROUP BY town_name\nORDER BY sales DESC"],
        ["GROUP BY + HAVING: only the busy town and flat-type pairs",
         "SELECT town_name, flat_type_name, COUNT(*) AS sales\nFROM v_transactions\nWHERE sale_year = 2025\nGROUP BY town_name, flat_type_name\nHAVING COUNT(*) > 500\nORDER BY sales DESC"],
        ["GROUPING SETS: detail, subtotals and a grand total in one query",
         "SELECT town_name, flat_type_name, COUNT(*) AS sales\nFROM v_transactions\nWHERE sale_year = 2025 AND town_name IN ('BISHAN', 'YISHUN')\nGROUP BY GROUPING SETS ((town_name, flat_type_name), (town_name), ())\nORDER BY town_name, flat_type_name"],
        ["Window function: each town's rank by sales, per year",
         "SELECT sale_year, town_name, COUNT(*) AS sales,\n       RANK() OVER (PARTITION BY sale_year ORDER BY COUNT(*) DESC) AS rank_in_year\nFROM v_transactions\nWHERE sale_year >= 2023\nGROUP BY sale_year, town_name\nORDER BY sale_year DESC, rank_in_year"],
        ["CTE + LAG: year-on-year change in the median 4-room price",
         "WITH yearly AS (\n  SELECT sale_year,\n         PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resale_price) AS median_price\n  FROM v_transactions\n  WHERE flat_type_name = '4 ROOM'\n  GROUP BY sale_year\n)\nSELECT sale_year, median_price,\n       ROUND((100 * (median_price / LAG(median_price) OVER (ORDER BY sale_year) - 1))::numeric, 1) AS change_pct\nFROM yearly\nORDER BY sale_year DESC"],
        ["Subquery: the dearest sale in each town, 2025",
         "SELECT town_name, block_no, street_name, flat_type_name, resale_price\nFROM v_transactions\nWHERE sale_year = 2025\n  AND (town_name, resale_price) IN (\n        SELECT town_name, MAX(resale_price)\n        FROM v_transactions\n        WHERE sale_year = 2025\n        GROUP BY town_name)\nORDER BY resale_price DESC"],
        ["Four-table JOIN + GROUP BY: median price by town and flat type, 2025",
         "SELECT tw.town_name, ft.flat_type_name, COUNT(*) AS sales,\n       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.resale_price) AS median_price\nFROM resale_transactions t\nJOIN blocks bl ON bl.block_id = t.block_id\nJOIN towns tw ON tw.town_id = bl.town_id\nJOIN flat_types ft ON ft.flat_type_id = t.flat_type_id\nWHERE t.month >= DATE '2025-01-01'\nGROUP BY tw.town_name, ft.flat_type_name\nHAVING COUNT(*) >= 50\nORDER BY median_price DESC\nLIMIT 15"],
        ["EXPLAIN ANALYZE: how the database finds one sale",
         "EXPLAIN ANALYZE\nSELECT * FROM resale_transactions WHERE txn_id = 500000"],
        ["Refused: reading every column of users (it holds password hashes)",
         "SELECT * FROM users"],
        ["Refused: changing data",
         "EXPLAIN ANALYZE\nUPDATE resale_transactions SET resale_price = 1"],
    ];
    const since2025 = { $match: { month: { $gte: { $date: "2025-01-01T00:00:00Z" } } } };
    const MONGO_EXAMPLES = [
        ["$group: reviews and average rating per town", "reviews",
         [{ $group: { _id: "$town", reviews: { $sum: 1 }, average_rating: { $avg: "$rating" } } }, { $sort: { average_rating: -1 } }]],
        ["$unwind + $group: the most used review tags", "reviews",
         [{ $unwind: "$tags" }, { $group: { _id: "$tags", uses: { $sum: 1 } } }, { $sort: { uses: -1 } }]],
        ["$match + $group: 2025 sales per town (a GROUP BY, as a pipeline)", "transactions",
         [since2025, { $group: { _id: "$town", sales: { $sum: 1 }, average_price: { $avg: "$price" } } }, { $sort: { sales: -1 } }]],
        ["$bucket: 2025 sales by price band", "transactions",
         [since2025, { $bucket: { groupBy: "$price", boundaries: [0, 400000, 600000, 800000, 1000000, 2000000], default: "2,000,000 and up", output: { sales: { $sum: 1 } } } }]],
        ["$group + $push: stations opened each year", "places",
         [{ $match: { kind: "mrt" } }, { $group: { _id: "$opened_year", stations: { $sum: 1 }, names: { $push: "$name" } } }, { $sort: { _id: -1 } }, { $limit: 12 }]],
        ["$indexStats: how often each index has been used", "transactions",
         [{ $indexStats: {} }, { $project: { _id: 0, name: 1, key: 1, uses: "$accesses.ops" } }]],
        ["Refused: writing a collection with $out", "reviews",
         [{ $match: { rating: 5 } }, { $out: "five_star_copy" }]],
    ];
    let engine = "sql";
    function setEngine(next) {
        engine = next;
        $("engSql").setAttribute("aria-pressed", next === "sql");
        $("engMongo").setAttribute("aria-pressed", next === "mongo");
        $("mongoCollWrap").hidden = next !== "mongo";
        $("consoleLabel").textContent = next === "sql" ? "SQL (one read-only statement)" : "Aggregation pipeline (JSON)";
        const list = next === "sql" ? SQL_EXAMPLES : MONGO_EXAMPLES;
        $("consoleExample").innerHTML = list.map((x, i) => '<option value="' + i + '">' + esc(x[0]) + "</option>").join("");
        loadExample();
        $("consoleOut").innerHTML = "";
    }
    function loadExample() {
        const i = Number($("consoleExample").value) || 0;
        if (engine === "sql") {
            $("consoleInput").value = SQL_EXAMPLES[i][1];
        } else {
            const ex = MONGO_EXAMPLES[i];
            $("mongoColl").value = ex[1];
            $("consoleInput").value = "[\n" + ex[2].map((s) => "  " + JSON.stringify(s)).join(",\n") + "\n]";
        }
        consoleTerm();
    }
    // the pipeline in the box as mongosh code ({"$date": ...} becomes ISODate(...))
    function mongoShell(coll, text) {
        let pipeline;
        try { pipeline = JSON.parse(text); } catch (e) { return null; }
        const body = JSON.stringify(pipeline, null, 2).replace(/\{\s*"\$date"\s*:\s*"([^"]+)"\s*\}/g, 'ISODate("$1")');
        return "db." + coll + ".aggregate(" + body + ")";
    }
    function consoleTerm() {
        if (engine === "sql") {
            renderTerm("consoleTerm", "The query in the box, as you would run it by hand.", [openStep("postgres"),
                { label: "Paste at the psql prompt", code: "\\timing on\n" + asSql($("consoleInput").value), note: "\\timing on makes psql print how long the query took." }]);
        } else {
            const code = mongoShell($("mongoColl").value, $("consoleInput").value);
            renderTerm("consoleTerm", "The pipeline in the box, as you would run it by hand.", [openStep("mongo"),
                { label: "Paste at the mongosh prompt", code: code || "// the pipeline is not valid JSON yet" }]);
        }
    }
    async function runConsole() {
        const out = $("consoleOut"), btn = $("consoleRun"), text = $("consoleInput").value;
        btn.disabled = true; btn.textContent = "Running…";
        try {
            if (engine === "sql") {
                const d = await api("/api/admin/sql", { sql: text });
                out.innerHTML = '<div class="adm-out"><p class="adm-meta"><span><b>' + d.rows.length + "</b> row" + (d.rows.length === 1 ? "" : "s") +
                    (d.truncated ? " (the first 200; there are more)" : "") + "</span><span><b>" + ms(d.ms) + "</b></span><span>guard: " +
                    (d.sandbox === "role" ? "read-only role" : "read-only transaction") + "</span></p>" +
                    table(d.columns, d.rows) +
                    (d.plan ? "<details><summary>How PostgreSQL plans to run it (EXPLAIN)</summary><pre class=\"adm-code\">" + esc(d.plan.join("\n")) + "</pre></details>" : "") + "</div>";
            } else {
                let pipeline;
                try { pipeline = JSON.parse(text); } catch (e) { throw new Error("The pipeline is not valid JSON: " + e.message); }
                const d = await api("/api/admin/mongo", { collection: $("mongoColl").value, pipeline: pipeline });
                const cols = [];
                d.docs.forEach((doc) => Object.keys(doc).forEach((k) => { if (!cols.includes(k)) cols.push(k); }));
                out.innerHTML = '<div class="adm-out"><p class="adm-meta"><span><b>' + d.docs.length + "</b> document" + (d.docs.length === 1 ? "" : "s") +
                    (d.truncated ? " (the first 200; there are more)" : "") + "</span><span><b>" + ms(d.ms) + "</b></span></p>" +
                    table(cols, d.docs.map((doc) => cols.map((c) => (c in doc ? doc[c] : null)))) + "</div>";
            }
        } catch (e) { fail(out, e); }
        btn.disabled = false; btn.textContent = "Run →";
    }
    firstOpen["t-console"] = function () {
        $("mongoColl").innerHTML = ((overview && overview.mongo.collections) || []).map((c) => "<option>" + esc(c.name) + "</option>").join("");
        $("engSql").addEventListener("click", () => setEngine("sql"));
        $("engMongo").addEventListener("click", () => setEngine("mongo"));
        $("consoleExample").addEventListener("change", loadExample);
        $("consoleInput").addEventListener("input", consoleTerm);
        $("mongoColl").addEventListener("change", consoleTerm);
        $("consoleForm").addEventListener("submit", (e) => { e.preventDefault(); runConsole(); });
        $("consoleInput").addEventListener("keydown", (e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runConsole(); }
        });
        setEngine("sql");
    };

    // OLAP cube
    function cubeTerm(d) {
        renderTerm("cubeTerm", "This view's statement, as you would run it by hand.", [openStep("postgres"),
            { label: "Paste at the psql prompt", code: "\\timing on\n" + asSql(d.sql),
              note: d.totals === "none" ? "" : "In the level column, 0 is a detail row; higher numbers are subtotals and the grand total." }]);
    }
    const cube = { meta: null, rows: "year", cols: "flat_type", measure: "median_price", totals: "rollup", filters: {}, op: "" };
    const dimLabel = (k) => (cube.meta.dims.find((d) => d.key === k) || {}).label || k;
    const levelNext = (k, step) => {
        for (const h of Object.values(cube.meta.hierarchies)) {
            const i = h.indexOf(k);
            if (i >= 0) return h[i + step] || null;
        }
        return null;
    };
    function cubeControls() {
        $("cubeRows").value = cube.rows;
        $("cubeCols").value = cube.cols;
        $("cubeMeasure").value = cube.measure;
        $("cubeTotals").value = cube.totals;
        $("cubePivot").disabled = !cube.cols;
        $("cubeUpRows").disabled = !levelNext(cube.rows, -1);
        $("cubeUpCols").disabled = !cube.cols || !levelNext(cube.cols, -1);
        const keys = Object.keys(cube.filters);
        $("cubeFilters").innerHTML = keys.map((k) => {
            const v = cube.filters[k];
            return '<span class="adm-chip">' + esc(dimLabel(k)) + (Array.isArray(v) ? " in " + esc(v.join(", ")) : " = " + esc(String(v))) +
                '<button type="button" data-unfilter="' + esc(k) + '" aria-label="Remove the filter on ' + esc(dimLabel(k)) + '">×</button></span>';
        }).join("");
        $("cubeOp").innerHTML = cube.op ? "Last operation: <span>" + esc(cube.op) + "</span>" : "";
    }
    async function runCube() {
        cubeControls();
        const out = $("cubeOut");
        out.innerHTML = '<p class="adm-loading">Asking the database</p>';
        try {
            const d = await api("/api/admin/cube", {
                dims: cube.cols ? [cube.rows, cube.cols] : [cube.rows], measure: cube.measure, totals: cube.totals, filters: cube.filters });
            out.innerHTML = renderCube(d);
            cubeTerm(d);
        } catch (e) { fail(out, e); }
    }
    function drillButton(axis, dim, v) {
        if (v === null || !levelNext(dim, 1) || levelNext(dim, 1) === (axis === "rows" ? cube.cols : cube.rows)) return esc(String(v));
        return '<button type="button" class="adm-drill" data-axis="' + axis + '" data-value="' + esc(JSON.stringify(v)) +
            '" title="Drill down into ' + esc(String(v)) + '">' + esc(String(v)) + "</button>";
    }
    function renderCube(d) {
        const show = cube.measure === "sales" ? (v) => (v == null ? "—" : nf.format(v)) : money;
        const why = { rollup: "ROLLUP adds a subtotal for each row and a grand total.",
                      cube: "CUBE adds subtotals in both directions and a grand total.", none: "No subtotals." }[d.totals];
        const foot = '<p class="adm-meta" style="margin-top:12px"><span><b>' + ms(d.ms) + "</b> in PostgreSQL</span><span>" + esc(why) + "</span></p>" +
            '<pre class="adm-code">' + esc(d.sql) + "</pre>";
        if (!d.rows.length) return '<p class="adm-empty">No sales match that slice.</p>' + foot;

        if (d.dims.length === 1) {
            const body = d.rows.map((r) => "<tr" + (r[3] ? ' class="tot"' : "") + "><td>" + (r[3] ? "All" : drillButton("rows", cube.rows, r[0])) +
                "</td><td>" + show(r[1]) + "</td><td>" + nf.format(r[2]) + "</td></tr>").join("");
            return '<div class="rwx-table-wrap"><table class="rwx-table adm-cross"><thead><tr><th scope="col">' + esc(d.dims[0].label) +
                '</th><th scope="col">' + esc(d.measure.label) + '</th><th scope="col">Sales</th></tr></thead><tbody>' + body + "</tbody></table></div>" + foot;
        }

        const rowVals = [], colVals = [], cells = new Map(), rowTot = new Map(), colTot = new Map();
        let grand = null, max = 0;
        const key = (a, b) => JSON.stringify([a, b]);
        d.rows.forEach((r) => {
            const a = r[0], b = r[1], v = r[2], level = r[4];
            if (level === 0) {
                if (!rowVals.includes(a)) rowVals.push(a);
                if (!colVals.includes(b)) colVals.push(b);
                cells.set(key(a, b), v);
                if (v > max) max = v;
            } else if (level === 1) rowTot.set(a, v);
            else if (level === 2) colTot.set(b, v);
            else grand = v;
        });
        colVals.sort((x, y) => (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))));
        if (colVals.length > 40) {
            return '<p class="adm-error">' + colVals.length + " columns is too wide to read. Pivot it, or slice to fewer " +
                esc(d.dims[1].label.toLowerCase()) + " values.</p>" + foot;
        }
        const hasRowTot = rowTot.size > 0, hasColTot = colTot.size > 0;
        let html = '<div class="rwx-table-wrap"><table class="rwx-table adm-cross"><thead><tr><th scope="col">' + esc(d.dims[0].label) + " \\ " + esc(d.dims[1].label) + "</th>" +
            colVals.map((c) => '<th scope="col">' + drillButton("cols", cube.cols, c) + "</th>").join("") +
            (hasRowTot ? '<th scope="col" class="tot">All</th>' : "") + "</tr></thead><tbody>";
        rowVals.forEach((a) => {
            html += '<tr><th scope="row">' + drillButton("rows", cube.rows, a) + "</th>" + colVals.map((b) => {
                const v = cells.get(key(a, b));
                if (v === undefined) return "<td>—</td>";
                // bigger value, darker cell
                return '<td style="background:color-mix(in srgb, var(--accent) ' + Math.round((v / (max || 1)) * 34) + '%, transparent)">' + show(v) + "</td>";
            }).join("") + (hasRowTot ? '<td class="tot">' + show(rowTot.get(a)) + "</td>" : "") + "</tr>";
        });
        if (hasColTot || grand !== null) {
            html += '<tr class="tot"><th scope="row">All</th>' + colVals.map((b) => "<td>" + (hasColTot ? show(colTot.get(b)) : "—") + "</td>").join("") +
                (hasRowTot ? "<td>" + show(grand) + "</td>" : "") + "</tr>";
        }
        return html + "</tbody></table></div>" + foot;
    }
    firstOpen["t-cube"] = async function () {
        renderTerm("cubeTerm", "Each view's statement appears here once it has loaded.", []);
        try {
            cube.meta = await api("/api/admin/cube/meta");
        } catch (e) { fail($("cubeOut"), e); return; }
        const opts = cube.meta.dims.map((d) => '<option value="' + d.key + '">' + esc(d.label) + "</option>").join("");
        $("cubeRows").innerHTML = opts;
        $("cubeCols").innerHTML = '<option value="">(none)</option>' + opts;
        $("sliceDim").innerHTML = opts;
        $("cubeMeasure").innerHTML = cube.meta.measures.map((m) => '<option value="' + m.key + '">' + esc(m.label) + "</option>").join("");
        const changed = (id, field) => $(id).addEventListener("change", () => {
            cube[field] = $(id).value;
            if (cube.rows === cube.cols) cube[field === "rows" ? "cols" : "rows"] = field === "rows" ? "" : levelNext(cube.cols, 1) || "town";
            if (cube.rows === cube.cols) cube.cols = "";
            cube.op = "";
            runCube();
        });
        changed("cubeRows", "rows"); changed("cubeCols", "cols"); changed("cubeMeasure", "measure"); changed("cubeTotals", "totals");
        $("cubePivot").addEventListener("click", () => {
            [cube.rows, cube.cols] = [cube.cols, cube.rows];
            cube.op = "Pivot: rows and columns swapped";
            runCube();
        });
        const rollUp = (axis) => {
            const dim = cube[axis], parent = levelNext(dim, -1);
            if (!parent) return;
            delete cube.filters[parent];                               // the slice a drill-down added
            if (parent === cube[axis === "rows" ? "cols" : "rows"]) cube[axis === "rows" ? "cols" : "rows"] = "";
            cube[axis] = parent;
            cube.op = "Roll up: " + dimLabel(dim) + " → " + dimLabel(parent);
            runCube();
        };
        $("cubeUpRows").addEventListener("click", () => rollUp("rows"));
        $("cubeUpCols").addEventListener("click", () => rollUp("cols"));
        $("cubeOut").addEventListener("click", (e) => {
            const b = e.target.closest(".adm-drill");
            if (!b) return;
            const axis = b.dataset.axis, dim = cube[axis], child = levelNext(dim, 1), value = JSON.parse(b.dataset.value);
            cube.filters[dim] = value;
            cube[axis] = child;
            cube.op = "Drill down: " + dimLabel(dim) + " " + value + " → " + dimLabel(child);
            runCube();
        });
        $("cubeFilters").addEventListener("click", (e) => {
            const k = e.target.dataset && e.target.dataset.unfilter;
            if (!k) return;
            delete cube.filters[k];
            cube.op = "Filter on " + dimLabel(k) + " removed";
            runCube();
        });
        $("cubeSliceForm").addEventListener("submit", (e) => {
            e.preventDefault();
            const dim = $("sliceDim").value, isNum = cube.meta.dims.find((d) => d.key === dim).numeric;
            const values = $("sliceValue").value.split(",").map((s) => s.trim()).filter(Boolean)
                .map((s) => (isNum ? Number(s) : s.toUpperCase().replace(/^(\d{4})\s*Q([1-4])$/, "$1 Q$2")));
            if (!values.length || values.some((v) => isNum && !Number.isInteger(v))) {
                fail($("cubeOut"), new Error(isNum ? dimLabel(dim) + " takes whole numbers, such as 2024." : "Type a value to slice by."));
                return;
            }
            cube.filters[dim] = values.length === 1 ? values[0] : values;
            cube.op = (values.length === 1 ? "Slice: " : "Dice: ") + dimLabel(dim) + (values.length === 1 ? " = " : " in ") + values.join(", ");
            $("sliceValue").value = "";
            runCube();
        });
        runCube();
    };

    // index analysis
    function planSide(title, p, most, win) {
        return '<div class="adm-side' + (win ? " win" : "") + '"><h3>' + esc(title) + '</h3><div class="adm-figs"><div><strong>' + ms(p.ms) +
            "</strong><span>to run</span></div><div><strong>" + nf.format(p.pages) + '</strong><span>pages read (8 KB each)</span></div></div>' +
            '<div class="adm-bar" aria-hidden="true"><i style="width:' + Math.max(0.3, (p.pages / (most || 1)) * 100).toFixed(1) + '%"></i></div>' +
            '<ul class="adm-plan">' + p.nodes.map((n) => '<li style="padding-left:' + n.depth * 18 + 'px">' + (n.depth ? "└ " : "") + "<b>" + esc(n.node) +
            "</b> · " + (n.loops > 1 ? "split across " + n.loops + " processes" : nf.format(n.rows || 0) + " row" + (n.rows === 1 ? "" : "s")) +
            " · " + ms(n.ms) + "</li>").join("") + "</ul></div>";
    }
    function indexFacts(f) {
        const levels = f.height
            ? '<div class="adm-tree" aria-hidden="true">' + Array.from({ length: f.height }, (_, i) =>
                "<div style=\"width:" + (30 + (70 * i) / Math.max(1, f.height - 1)) + '%">' +
                (i === 0 ? "root: 1 page" : i === f.height - 1 ? "leaves: every key, in order, linked left to right" : "internal pages") + "</div>").join("") + "</div>"
            : "";
        return '<div class="adm-side"><h3>' + esc(f.name) + "</h3><p class=\"rwx-muted\" style=\"margin-top:6px\">" +
            (f.method === "btree" ? "A B+ tree" : esc(f.method)) + (f.height ? " <b>" + f.height + " levels</b> deep" : "") + ": " + nf.format(f.entries) +
            " entries in " + nf.format(f.pages) + " pages (" + (f.bytes / 1048576).toFixed(1) + " MB)." +
            (f.height ? " A lookup reads one page per level, then the row itself." : "") + "</p>" + levels +
            '<pre class="adm-code">' + esc(f.definition) + "</pre></div>";
    }
    function renderIndex(d) {
        const w = d.with_index, n = d.without_index, most = Math.max(w.pages, n.pages), used = d.indexes.length > 0;
        const gain = used && n.pages > w.pages
            ? "The index cut the work from <b>" + nf.format(n.pages) + "</b> pages to <b>" + nf.format(w.pages) + "</b> (" +
              nf.format(Math.round(n.pages / Math.max(1, w.pages))) + " times fewer), and the time from " + ms(n.ms) + " to " + ms(w.ms) + "."
            : "The planner did not use an index here: both runs do the same work.";
        return '<div class="adm-out"><p class="adm-meta"><span><b>' + esc(d.kind) + "</b></span><span>" + esc(d.label) + '</span></p><pre class="adm-code">' + esc(d.sql) + "</pre>" +
            '<div class="adm-two">' + planSide("As normal", w, most, used && n.pages > w.pages) + planSide("Indexes switched off", n, most, false) + "</div>" +
            '<p class="adm-takeaway">' + gain + " " + esc(d.takeaway) + "</p>" +
            (d.indexes.length ? '<div class="adm-two">' + d.indexes.map(indexFacts).join("") + "</div>" : "") + "</div>";
    }
    function caseButtons(holder, items, label, run) {
        holder.innerHTML = items.map((c) => '<button type="button" class="adm-case" data-key="' + esc(c.key) + '" aria-pressed="false">' + label(c) + "</button>").join("");
        holder.addEventListener("click", async (e) => {
            const b = e.target.closest(".adm-case");
            if (!b || b.disabled) return;
            const all = [...holder.querySelectorAll(".adm-case")];
            all.forEach((x) => { x.disabled = true; x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
            await run(b.dataset.key);
            all.forEach((x) => { x.disabled = false; });
        });
    }
    function indexTerm(d) {
        const sql = asSql(d.sql);
        const steps = [openStep("postgres"),
            { label: "Run it as normal", code: "\\timing on\nEXPLAIN (ANALYZE, BUFFERS) " + sql,
              note: "Look for Index Scan, and for Buffers: shared hit=…, which is the number of pages read." },
            { label: "Run it with the indexes switched off", code: "SET enable_indexscan = off;\nSET enable_indexonlyscan = off;\nSET enable_bitmapscan = off;\nEXPLAIN (ANALYZE, BUFFERS) " + sql +
              "\nRESET enable_indexscan;\nRESET enable_indexonlyscan;\nRESET enable_bitmapscan;", note: "Now it is a Seq Scan that reads every page." }];
        if (d.indexes.length) {
            const name = d.indexes[0].name;
            steps.push({ label: "Look at the index", code: "\\di+ " + name + "\n\\d resale_transactions\nCREATE EXTENSION IF NOT EXISTS pageinspect;\nSELECT level + 1 AS tree_levels FROM bt_metap('" + name + "');",
                         note: "The last two lines need a superuser (the local Docker database is one)." });
        }
        renderTerm("indexTerm", "The same two runs, by hand.", steps);
    }
    firstOpen["t-index"] = async function () {
        const out = $("indexOut");
        renderTerm("indexTerm", "Pick a case and the commands for it appear here.", []);
        try {
            const cases = await api("/api/admin/indexes");
            caseButtons($("indexCases"), cases, (c) => "<b>" + esc(c.label) + "</b><small>" + esc(c.kind) + "</small>", async (key) => {
                out.innerHTML = '<p class="adm-loading">Running it both ways</p>';
                try {
                    const d = await api("/api/admin/indexes", { case: key });
                    out.innerHTML = renderIndex(d);
                    indexTerm(d);
                } catch (e) { fail(out, e); }
            });
        } catch (e) { fail(out, e); }
    };

    // concurrency
    function renderConcurrency(d) {
        const bal = (b) => "Alice <b>" + nf.format(b.Alice) + "</b> · Bob <b>" + nf.format(b.Bob) + "</b>";
        const serial = d.valid.map((v) => "Alice " + v.Alice + " and Bob " + v.Bob).join(", or ");
        const verdict = d.correct
            ? '<div class="adm-verdict good">Correct: ' + (d.valid.length > 1 ? "the same as running them one after the other (" + serial + ")." : "exactly what was intended (" + serial + ").") + "</div>"
            : '<div class="adm-verdict bad">Wrong: the right answer is ' + esc(serial) + ".</div>";
        const lane = (s, who) => (s.who === who && s.sql ? "<code>" + esc(s.sql) + "</code>" : "");
        const rows = d.steps.map((s) => (s.who
            ? '<tr class="' + esc(s.status) + '"><td class="ms">' + nf.format(s.ms) + ' ms</td><td class="lane">' + lane(s, "T1") + '</td><td class="lane">' + lane(s, "T2") +
              "</td><td>" + (s.who ? '<span class="adm-who ' + s.who.toLowerCase() + '">' + esc(s.who) + "</span> " : "") + esc(s.note.replace(new RegExp("^" + s.who + " (?=[a-z])"), "")) + "</td></tr>"
            : '<tr class="' + esc(s.status) + '"><td class="ms">' + nf.format(s.ms) + ' ms</td><td colspan="3">' + esc(s.note) + "</td></tr>")).join("");
        return '<div class="adm-out"><p class="adm-meta"><span><b>' + esc(d.idea) + "</b></span><span>" + esc(d.what) + "</span></p>" +
            '<div class="adm-balances"><div>Start: ' + bal(d.start) + "</div><div>End: " + bal(d.end) + " · total <b>" + nf.format(d.total) + "</b></div>" + verdict + "</div>" +
            '<div class="rwx-table-wrap"><table class="adm-timeline"><thead><tr><th scope="col">When</th><th scope="col">T1’s session</th><th scope="col">T2’s session</th><th scope="col">What happened</th></tr></thead><tbody>' +
            rows + "</tbody></table></div>" +
            '<p class="rwx-note">Every statement above was sent to PostgreSQL on its own session; the errors and the waits are the database’s own.</p></div>';
    }
    // the run's statements in the order they were sent, for two terminals
    function concurrencyTerm(d) {
        const t = d.terminal;
        const steps = [
            { label: "Open psql in both terminals", code: term.postgres, note: "Open two terminal windows and call them Terminal 1 and Terminal 2. " + (term.local ? "Docker must be running." : "It asks for the database password.") },
            { label: "Start from the same balances (either terminal)", code: asSql(t.setup) },
        ];
        t.steps.forEach((s, i) => steps.push({
            session: s.session, label: s.label || "Step " + (i + 1) + (s.session ? ": paste in Terminal " + s.session.slice(1) : ": paste in either terminal"),
            code: asSql(s.sql), note: s.expect, failed: s.failed }));
        steps.push({ label: "Check the balances", code: "SELECT * FROM concurrency_accounts ORDER BY name;",
                     note: t.end_note || "This run ended with Alice " + nf.format(d.end.Alice) + " and Bob " + nf.format(d.end.Bob) + "." });
        renderTerm("ccTerm", "The same run by hand. Paste each step in the terminal named, in order; the note says what you should see.", steps);
    }
    firstOpen["t-concurrency"] = async function () {
        const out = $("ccOut");
        renderTerm("ccTerm", "Pick a scenario and its statements appear here, to repeat by hand in two terminals.", []);
        try {
            const list = await api("/api/admin/concurrency");
            caseButtons($("ccCases"), list, (c) => "<b>" + esc(c.title) + "</b><small>" + esc(c.idea) + "</small>", async (key) => {
                out.innerHTML = '<p class="adm-loading">Opening three database sessions and running it</p>';
                try {
                    const d = await api("/api/admin/concurrency", { scenario: key });
                    out.innerHTML = renderConcurrency(d);
                    concurrencyTerm(d);
                } catch (e) { fail(out, e); }
            });
        } catch (e) { fail(out, e); }
    };

    // start
    api("/api/admin/overview").then((o) => {
        overview = o;
        term = o.terminal || term;
        $("admApp").hidden = false;
        renderOverview(o);
        overviewTerm();
        const wanted = tabs.find((t) => "#" + t.id.slice(2) === location.hash);
        if (wanted && wanted !== tabs[0]) openTab(wanted);
    }).catch((e) => {
        if ($("admGate").hidden) gate("The admin area could not load", e.message, false);
    });
})();
