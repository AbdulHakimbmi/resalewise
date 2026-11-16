/*
  ResaleWise SG: the town archive.
  Every HDB town stands on a shelf that curves away into depth, like tapes in a rack. The one in the
  middle turns to show its photograph; tapping it grows a sheet of paper out of the cover with the
  town's prices, and a link on to the full town page.

  Motion is one spring on one number (`s`, the shelf position) painted by a requestAnimationFrame
  loop that only runs while something is moving and the section is on screen. The 3D is plain CSS
  (perspective + translate3d), so the work per frame is a transform per tape.

  The town list is in the page (.archive-slide), so it reads without JavaScript; once the API answers
  it is rebuilt from the database, with each town's live 4-room median.
  Photographs: assets/images/towns/<town>.jpg, credited in credits.json next to them.
*/
(function () {
    "use strict";
    const root = document.querySelector("[data-archive]");
    if (!root) return;

    const reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    const finePointer = !!(window.matchMedia && matchMedia("(pointer: fine)").matches);
    const EASE = "cubic-bezier(.2,.8,.2,1)";
    const SHEET_EASE = "cubic-bezier(.76,0,.18,1)";
    const P = 1400;                                            // the CSS perspective, in px

    const money = (n) => "$" + Math.round(Number(n)).toLocaleString("en-SG");
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const title = (s) => s.toLowerCase().replace(/(^|[\s/])(\w)/g, (m, a, b) => a + b.toUpperCase());
    const slug = (s) => s.toLowerCase().replace(/\//g, "-").replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/-+/g, "-");
    const photoFile = (name) => "assets/images/towns/" + slug(name) + ".jpg";
    const smooth01 = (x) => { const t = Math.min(1, Math.max(0, x)); return t * t * (3 - 2 * t); };
    const townUrl = (name) => "town.html?town=" + encodeURIComponent(name);

    /* where the picture is cropped when a landscape photograph is cut into a tall cover (x% y%) */
    const FOCUS = { "QUEENSTOWN": "32% 50%", "PUNGGOL": "8% 50%", "YISHUN": "88% 50%" };

    /* ---------------------------------------------------------------- data */
    let towns = [...root.querySelectorAll(".archive-slide")].map((li) => ({
        name: li.dataset.town,
        median: Number(li.dataset.median) || null,
        count: Number(li.dataset.count) || 0,
        psm: null,
    }));
    if (!towns.length) return;

    /* ---------------------------------------------------------------- skeleton */
    root.classList.add("is-live");
    root.insertAdjacentHTML("beforeend", `
        <span class="archive-word archive-word-l" aria-hidden="true">HDB</span>
        <span class="archive-word archive-word-r" aria-hidden="true">TOWNS</span>
        <div class="archive-floor" aria-hidden="true"></div>
        <div class="archive-shelf" tabindex="0" role="region" aria-label="HDB towns, one on each tape. Use the left and right arrow keys to move along, Enter to open a town.">
            <div class="archive-stage"></div>
        </div>
        <button class="archive-hint" type="button" tabindex="-1"><span></span>Open</button>
        <p class="archive-sr" aria-live="polite"></p>
        <div class="archive-caption">
            <button class="archive-arrow archive-prev" type="button" aria-label="Previous town"><svg width="24" height="12" viewBox="0 0 24 12" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M6 1L1 6l5 5M1 6h23"/></svg></button>
            <div class="archive-mask archive-name"></div>
            <div class="archive-mask archive-sub"></div>
            <span class="archive-rule" aria-hidden="true"></span>
            <div class="archive-mask archive-meta"></div>
            <button class="archive-arrow archive-next" type="button" aria-label="Next town"><svg width="24" height="12" viewBox="0 0 24 12" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M18 1l5 5-5 5M23 6H0"/></svg></button>
        </div>
        <div class="archive-ruler" role="presentation"><div class="archive-ruler-line"></div><div class="archive-ruler-ticks"></div><i class="archive-ruler-dot"></i></div>`);
    const shelf = root.querySelector(".archive-shelf");
    const stage = root.querySelector(".archive-stage");
    const hint = root.querySelector(".archive-hint");
    const live = root.querySelector(".archive-sr");
    const maskName = root.querySelector(".archive-name");
    const maskSub = root.querySelector(".archive-sub");
    const maskMeta = root.querySelector(".archive-meta");
    const ruler = root.querySelector(".archive-ruler");
    const rulerTicks = root.querySelector(".archive-ruler-ticks");
    const rulerDot = root.querySelector(".archive-ruler-dot");
    const prevBtn = root.querySelector(".archive-prev");
    const nextBtn = root.querySelector(".archive-next");
    const jump = document.getElementById("townJump");

    /* ---------------------------------------------------------------- the pop-up sheet */
    const sheet = document.createElement("div");
    sheet.className = "archive-sheet";
    sheet.hidden = true;
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.setAttribute("aria-labelledby", "archiveSheetTitle");
    sheet.innerHTML = `
        <button class="archive-close" type="button">Close</button>
        <div class="archive-sheet-copy">
            <div class="archive-line"><p class="archive-label" data-s="label"></p></div>
            <div class="archive-line"><h3 id="archiveSheetTitle" data-s="name"></h3></div>
            <div class="archive-line"><p class="archive-price" data-s="price"></p></div>
            <div class="archive-line"><p class="archive-facts" data-s="facts"></p></div>
            <div class="archive-line archive-trend" data-s="trend"></div>
            <div class="archive-line"><a class="archive-go" data-s="go" href="town.html">Open the town page <span aria-hidden="true">→</span></a></div>
        </div>
        <figure class="archive-poster"><span class="archive-poster-img" data-s="poster"></span><figcaption data-s="credit"></figcaption></figure>`;
    document.body.appendChild(sheet);
    const S = (k) => sheet.querySelector('[data-s="' + k + '"]');
    const closeBtn = sheet.querySelector(".archive-close");

    /* ---------------------------------------------------------------- state */
    let tapes = [];
    let n = 0;
    let s = 0;                       // where the shelf is (tapes), eased toward T
    let T = 0;                       // where it is going
    let active = -1;
    let hover = -1;
    let tiltX = 0, tiltY = 0;
    let H = 0, ws = 0, d = 0, gap = 0, U = 0, XS = [0];
    let raf = 0, last = 0, visible = true;
    let drag = null;
    let wheelTimer = 0;
    let sheetOpen = false;
    let ptr = { x: 0, y: 0, in: false };
    let lo = 0, hi = 1, dotPos = null;
    let swapDir = 1;

    /* ---------------------------------------------------------------- geometry (the arc) */
    function measure() {
        const w = root.clientWidth || 800;
        const phone = w < 768;
        H = phone ? Math.min(w * 0.68, innerHeight * 0.4) : Math.min(innerHeight * 0.44, w * 0.3, 440);
        H = Math.max(210, H);
        ws = (H * 142) / 840;
        d = (H * 474) / 900;
        gap = H * 0.035;
        U = H * (phone ? 1.05 : 1.3);
        XS = [0];
        for (let u = 0, acc = 0; u < 3 * U; u += 2) { acc += 2 / (1 + Math.pow(u / U, 1.6)); XS.push(acc); }
        root.style.setProperty("--H", H.toFixed(1) + "px");
        root.style.setProperty("--ws", ws.toFixed(1) + "px");
        root.style.setProperty("--d", d.toFixed(1) + "px");
    }
    const rel = (v, pos) => { const r = (((v - pos) % n) + n) % n; return r >= n / 2 ? r - n : r; };
    const B = (r) => (Math.abs(r) < 1 ? (r + Math.sin(Math.PI * r) / Math.PI) / 2 : Math.sign(r) * 0.5);
    const F = (r) => r * (ws + gap) + (d - ws + 1.4 * gap) * B(r);              // arc length from the centre; the open cover pushes its neighbours apart
    const scl = (u) => 1 / (1 + Math.pow(Math.abs(u) / U, 1.6));               // how small it looks at that distance
    const xsc = (u) => {                                                       // screen x: far tapes pack tighter
        const a = Math.abs(u) / 2;
        const i = Math.min(XS.length - 2, Math.floor(a));
        return Math.sign(u) * (XS[i] + (XS[i + 1] - XS[i]) * (a - i));
    };

    /* ---------------------------------------------------------------- build the tapes */
    function build() {
        stage.textContent = "";
        n = towns.length;
        tapes = towns.map((t, i) => {
            const el = document.createElement("div");
            el.className = "archive-tape is-bare";
            el.dataset.i = i;
            const name = esc(title(t.name));
            el.innerHTML = `
                <div class="archive-sp"><span class="archive-sp-img"></span><span class="archive-sp-name">${name}</span><span class="archive-tag">HDB</span></div>
                <div class="archive-cv"><span class="archive-cv-img"></span><span class="archive-cv-top">ResaleWise SG</span><span class="archive-cv-name">${name}</span></div>`;
            if (FOCUS[t.name]) el.style.setProperty("--focus", FOCUS[t.name]);
            stage.appendChild(el);
            const img = new Image();                                           // fetched and decoded before it is shown
            // an absolute address: a relative url() in a custom property is resolved against the stylesheet, not the page
            img.onload = () => { el.style.setProperty("--photo", `url("${img.src}")`); el.classList.remove("is-bare"); wake(); };
            img.src = photoFile(t.name);
            if (img.decode) img.decode().catch(() => {});
            el.addEventListener("pointerenter", () => { hover = i; wake(); });
            el.addEventListener("pointerleave", () => { if (hover === i) hover = -1; wake(); });
            return { el, cv: el.querySelector(".archive-cv"), off: false, lift: 0 };
        });
        measureRuler();
        active = -1;
    }

    /* ---------------------------------------------------------------- the price ruler */
    function measureRuler() {
        const ms = towns.map((t) => t.median).filter(Boolean);
        if (!ms.length) { ruler.hidden = true; return; }
        ruler.hidden = false;
        lo = Math.floor(Math.min(...ms) / 100000) * 100000;
        hi = Math.max(lo + 200000, Math.ceil(Math.max(...ms) / 100000) * 100000);
        let html = "";
        for (let v = lo; v <= hi; v += 50000) {
            const major = v % 100000 === 0;
            const label = major && ((v - lo) % 200000 === 0 || v === hi) ? `<em>$${(v / 1e6).toFixed(1)}m</em>` : "";
            html += `<span class="${major ? "maj" : "min"}" style="left:${(((v - lo) / (hi - lo)) * 100).toFixed(2)}%">${label}</span>`;
        }
        rulerTicks.innerHTML = html;
        dotPos = null;
    }
    function medianAt(pos) {                                                    // the median under the shelf, blended between two towns
        const base = Math.floor(pos);
        const f = pos - base;
        const at = (k) => { for (let j = 0; j < n; j++) { const t = towns[(((k + j) % n) + n) % n]; if (t.median) return t.median; } return null; };
        const a = at(base);
        const b = at(base + 1);
        return a == null ? null : a + ((b == null ? a : b) - a) * f;
    }

    /* ---------------------------------------------------------------- the caption (a mask slide) */
    function swap(mask, html, dir) {
        const old = [...mask.children];
        const el = document.createElement("span");
        el.innerHTML = html;
        mask.appendChild(el);
        const opts = { duration: reduce ? 1 : 650, easing: EASE, fill: "both" };
        el.animate([{ transform: `translateY(${dir * 105}%)` }, { transform: "translateY(0)" }], opts);
        old.forEach((o) => {
            const m = getComputedStyle(o).transform;
            const a = o.animate([{ transform: m === "none" ? "translateY(0)" : m }, { transform: `translateY(${-dir * 105}%)` }], opts);
            a.onfinish = () => o.remove();
        });
    }
    function setActive(i, dir) {
        active = i;
        const t = towns[i];
        const name = esc(title(t.name));
        swap(maskName, name, dir);
        swap(maskSub, t.median ? "4-room median " + money(t.median) : "Too few 4-room sales for a median", dir);
        swap(maskMeta, t.count ? t.count.toLocaleString("en-SG") + " sales in the last 12 months" : "", dir);
        live.textContent = title(t.name) + (t.median ? ", 4-room median " + money(t.median) : "");
        hint.setAttribute("aria-label", "Open " + title(t.name));
        if (jump && jump.value !== t.name) jump.value = t.name;
    }

    /* ---------------------------------------------------------------- the frame */
    function wake() { if (!raf && visible) { last = performance.now(); raf = requestAnimationFrame(frame); } }

    function frame(now) {
        raf = 0;
        const dt = Math.min(0.05, Math.max(0.001, (now - last) / 1000));
        last = now;
        let moving = false;

        const k = reduce ? 1000 : drag && drag.moved ? 18 : 5.2;
        s += (T - s) * (1 - Math.exp(-k * dt));
        if (Math.abs(T - s) < 0.0004) s = T; else moving = true;
        if (Math.abs(s) > n * 60) { const sh = Math.round(s / n) * n; s -= sh; T -= sh; }

        // the cover leans toward the pointer
        const box = shelf.getBoundingClientRect();
        const cx = box.left + box.width / 2;
        const cy = box.top + box.height / 2;
        const settled = Math.abs(T - s) < 0.3;
        const onCover = ptr.in && finePointer && !sheetOpen && settled && Math.abs(ptr.x - cx) < 0.55 * d && Math.abs(ptr.y - cy) < 0.55 * H;
        const tyT = reduce ? 0 : onCover ? ((ptr.x - cx) / (0.55 * d)) * 8 : ptr.in && finePointer ? ((ptr.x - cx) / box.width) * 4 : 0;
        const txT = reduce || !onCover ? 0 : (-(ptr.y - cy) / (H / 2)) * 5;
        const ke = 1 - Math.exp(-7 * dt);
        tiltY += (tyT - tiltY) * ke;
        tiltX += (txT - tiltX) * ke;
        if (Math.abs(tyT - tiltY) > 0.02 || Math.abs(txT - tiltX) > 0.02) moving = true;

        const lk = 1 - Math.exp(-12 * dt);
        for (let i = 0; i < n; i++) {
            const t = tapes[i];
            const r = rel(i, s);
            const ar = Math.abs(r);
            const o = smooth01(Math.max(0, 1 - ar));
            const u = F(r);
            if (Math.abs(u) > U * 1.95) {
                if (!t.off) { t.el.style.display = "none"; t.off = true; }
                continue;
            }
            if (t.off) { t.el.style.display = ""; t.off = false; }
            const sc = scl(u);
            const kk = 1 / sc;
            const want = hover === i && ar > 0.5 && !drag ? 1 : 0;
            t.lift += (want - t.lift) * lk;
            if (Math.abs(want - t.lift) > 0.003) moving = true; else t.lift = want;
            const z = P * (1 - kk) + o * H * 0.1;
            const x = xsc(u) * kk;
            const y = (H * 0.5 * (1 - sc) * 0.42 + o * H * 0.012 - t.lift * H * 0.05) * kk;     // far tapes sit lower: the shelf is a shallow bowl
            const ry = Math.sign(u) * Math.min(1, Math.abs(u) / U) * 26 * (1 - o) - 90 * o + tiltY * o;
            t.el.style.transform = `translate3d(${x.toFixed(2)}px,${y.toFixed(2)}px,${z.toFixed(2)}px) rotateX(${(tiltX * o).toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg)`;
            t.el.style.zIndex = Math.round(3000 + z);
            t.el.style.setProperty("--sp", Math.min(0.6, o * 0.55 + (1 - sc) * 0.32).toFixed(3));
            t.el.style.setProperty("--cv", ((1 - o) * 0.82).toFixed(3));
        }

        const a = ((Math.round(s) % n) + n) % n;
        if (a !== active) setActive(a, Math.sign(T - s) || swapDir);
        if (T !== s) swapDir = Math.sign(T - s) || swapDir;

        // the "open" tag sits on the cover's corner once the shelf has settled
        const on = sheetOpen ? 0 : smooth01(Math.max(0, 1 - Math.abs(T - s) * 3));
        hint.style.setProperty("--on", on.toFixed(3));
        hint.style.visibility = on < 0.02 ? "hidden" : "visible";
        hint.tabIndex = on < 0.9 ? -1 : 0;
        hint.style.left = `calc(50% + ${(d / 2 + 6).toFixed(1)}px)`;                // the cover's lower-right corner

        // the dot on the ruler
        const m = medianAt(s);
        if (m != null) {
            if (dotPos == null || Math.abs(m - dotPos) > (hi - lo) * 0.45 || reduce) dotPos = m;
            else dotPos += (m - dotPos) * (1 - Math.exp(-14 * dt));
            if (Math.abs(m - dotPos) > 1000) moving = true;
            rulerDot.style.left = (((dotPos - lo) / (hi - lo)) * 100).toFixed(2) + "%";
            rulerDot.style.transform = "translate(-50%,-50%) scale(1)";
        } else {
            rulerDot.style.transform = "translate(-50%,-50%) scale(0)";
        }

        if (moving || drag) wake();
    }

    /* ---------------------------------------------------------------- moving the shelf */
    function glideBy(delta) { T = Math.round(T) + delta; wake(); }
    function goTo(i) { T = Math.round(T) + Math.round(rel(i, Math.round(T))); wake(); }

    shelf.addEventListener("pointerdown", (e) => {
        if (e.button) return;
        shelf.setPointerCapture(e.pointerId);
        drag = { id: e.pointerId, x0: e.clientX, T0: T, moved: false, tape: e.target.closest ? e.target.closest(".archive-tape") : null };
        wake();
    });
    root.addEventListener("pointermove", (e) => {
        ptr.x = e.clientX; ptr.y = e.clientY; ptr.in = true;
        if (drag && drag.id === e.pointerId) {
            const dx = e.clientX - drag.x0;
            if (!drag.moved && Math.abs(dx) > 5) drag.moved = true;
            if (drag.moved) T = drag.T0 - dx / (ws + gap);
        }
        wake();
    });
    root.addEventListener("pointerleave", () => { ptr.in = false; wake(); });
    function overFrontCover(x, y) {
        if (active < 0 || !tapes[active]) return false;
        const b = tapes[active].cv.getBoundingClientRect();
        return x >= b.left && x <= b.right && y >= b.top && y <= b.bottom;
    }
    function endDrag(e) {
        if (!drag || drag.id !== e.pointerId) return;
        const { moved, tape } = drag;
        drag = null;
        if (moved) { T = Math.round(T); }
        else if (Math.abs(T - s) < 0.3 && overFrontCover(e.clientX, e.clientY)) openSheet();      // by geometry, so it works however the browser hit-tests the turned face
        else if (tape) glideBy(Math.round(rel(Number(tape.dataset.i), Math.round(s))));
        wake();
    }
    shelf.addEventListener("pointerup", endDrag);
    shelf.addEventListener("pointercancel", endDrag);

    /* a sideways swipe or shift+wheel moves the shelf; plain vertical scrolling is left to the page */
    shelf.addEventListener("wheel", (e) => {
        if (sheetOpen) return;
        const sideways = Math.abs(e.deltaX) > Math.abs(e.deltaY) || (e.shiftKey && e.deltaY);
        if (!sideways) return;
        e.preventDefault();
        const amount = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
        T += (amount / (ws + gap)) * 0.5;
        clearTimeout(wheelTimer);
        wheelTimer = setTimeout(() => { T = Math.round(T); wake(); }, 160);
        wake();
    }, { passive: false });

    shelf.addEventListener("keydown", (e) => {
        if (e.key === "ArrowLeft") { e.preventDefault(); glideBy(-1); }
        else if (e.key === "ArrowRight") { e.preventDefault(); glideBy(1); }
        else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSheet(); }
    });
    prevBtn.addEventListener("click", () => glideBy(-1));
    nextBtn.addEventListener("click", () => glideBy(1));
    hint.addEventListener("click", () => openSheet());
    if (jump) jump.addEventListener("change", () => { const i = towns.findIndex((t) => t.name === jump.value); if (i >= 0) goTo(i); });
    ruler.addEventListener("click", (e) => {                                   // jump to the town whose median is nearest the price you clicked
        const b = ruler.getBoundingClientRect();
        const price = lo + ((e.clientX - b.left) / b.width) * (hi - lo);
        let best = -1, gapMin = Infinity;
        towns.forEach((t, i) => { if (t.median && Math.abs(t.median - price) < gapMin) { gapMin = Math.abs(t.median - price); best = i; } });
        if (best >= 0) goTo(best);
    });

    /* ---------------------------------------------------------------- the sheet */
    const credits = { map: null };
    function loadCredits() {
        if (credits.map) return Promise.resolve(credits.map);
        return fetch("assets/images/towns/credits.json", { cache: "no-cache" }).then((r) => r.json()).then((rows) => {
            credits.map = {}; rows.forEach((row) => { credits.map[row.file] = row.credit; }); return credits.map;
        }).catch(() => (credits.map = {}));
    }
    const ordinal = (k) => k + (["th", "st", "nd", "rd"][(k % 100 > 10 && k % 100 < 14) ? 0 : (k % 10 < 4 ? k % 10 : 0)]);
    const quarterLabel = (q) => { const dt = new Date(q); return isNaN(dt) ? "" : dt.getUTCFullYear() + " Q" + (Math.floor(dt.getUTCMonth() / 3) + 1); };

    function fillSheet(t) {
        const name = title(t.name);
        S("label").textContent = "4-room flats, last 12 months";
        S("name").textContent = name;
        S("go").innerHTML = "Open " + esc(name) + " <span aria-hidden=\"true\">→</span>";
        S("go").href = townUrl(t.name);
        const ranked = towns.filter((x) => x.median).sort((a, b) => b.median - a.median);
        const pos = ranked.findIndex((x) => x.name === t.name) + 1;
        S("price").textContent = t.median ? money(t.median) + " median" : "Too few 4-room sales for a median";
        const facts = [];
        if (t.count) facts.push(t.count.toLocaleString("en-SG") + " sales");
        if (t.psm) facts.push(money(t.psm) + " per sqm");
        if (t.median && pos) facts.push((pos === 1 ? "Dearest" : ordinal(pos) + " dearest") + " of " + ranked.length + " towns");
        S("facts").textContent = facts.join("  ·  ");
        S("trend").innerHTML = "";
        const img = S("poster");
        img.style.backgroundImage = `url("${photoFile(t.name)}")`;
        img.style.backgroundPosition = FOCUS[t.name] || "50% 50%";
        S("credit").textContent = "";
        loadCredits().then((map) => { if (active >= 0 && towns[active] === t) S("credit").textContent = map["assets/images/towns/" + slug(t.name) + ".jpg"] || ""; });
        if (typeof apiGet === "function" && t.median) {
            apiGet("/api/insights/trend", { flat_type: "4 ROOM", town: t.name, months: 24 }).then((res) => {
                const rows = (res.rows || []).filter((r) => r.median_price);
                if (rows.length < 3 || towns[active] !== t || !sheetOpen) return;
                const vals = rows.map((r) => Number(r.median_price));
                const min = Math.min(...vals), max = Math.max(...vals);
                const W = 340, Hh = 80, pad = 6;
                const pts = vals.map((v, i) => [pad + (i / (vals.length - 1)) * (W - 2 * pad), pad + (1 - (max === min ? 0.5 : (v - min) / (max - min))) * (Hh - 2 * pad)]);
                const change = Math.round(((vals[vals.length - 1] - vals[0]) / vals[0]) * 100);
                S("trend").innerHTML =
                    `<svg viewBox="0 0 ${W} ${Hh}" role="img" aria-label="Quarterly 4-room median price, ${esc(quarterLabel(rows[0].quarter))} to ${esc(quarterLabel(rows[rows.length - 1].quarter))}"><polyline points="${pts.map((p) => p.map((c) => c.toFixed(1)).join(",")).join(" ")}" /><rect x="${(pts[pts.length - 1][0] - 4).toFixed(1)}" y="${(pts[pts.length - 1][1] - 4).toFixed(1)}" width="8" height="8" /></svg>` +
                    `<p>${esc(quarterLabel(rows[0].quarter))} to ${esc(quarterLabel(rows[rows.length - 1].quarter))}: ${change >= 0 ? "up" : "down"} ${Math.abs(change)}%</p>`;
            }).catch(() => {});
        }
    }

    function rise(el, delay) {
        return el.animate([{ transform: "translateY(110%)" }, { transform: "translateY(0)" }], { duration: reduce ? 1 : 900, delay: reduce ? 0 : delay, easing: EASE, fill: "backwards" });
    }
    let sheetAnim = null;
    let lastFocus = null;
    function coverInset() {
        const r = tapes[active].cv.getBoundingClientRect();
        return `inset(${Math.max(0, r.top)}px ${Math.max(0, innerWidth - r.right)}px ${Math.max(0, innerHeight - r.bottom)}px ${Math.max(0, r.left)}px)`;
    }
    function openSheet() {
        if (sheetOpen || active < 0) return;
        sheetOpen = true;
        lastFocus = document.activeElement;
        fillSheet(towns[active]);
        sheet.hidden = false;
        document.documentElement.style.overflow = "hidden";                      // the page behind does not scroll
        if (sheetAnim) sheetAnim.cancel();
        sheetAnim = sheet.animate([{ clipPath: coverInset() }, { clipPath: "inset(0px 0px 0px 0px)" }], { duration: reduce ? 1 : 1050, easing: SHEET_EASE, fill: "both" });
        sheet.querySelectorAll(".archive-line").forEach((el, i) => rise(el.firstElementChild || el, 620 + i * 75));
        S("poster").animate([{ clipPath: "inset(0 0 100% 0)" }, { clipPath: "inset(0 0 0% 0)" }], { duration: reduce ? 1 : 1100, delay: reduce ? 0 : 450, easing: SHEET_EASE, fill: "backwards" });
        closeBtn.focus({ preventScroll: true });
        wake();
    }
    function closeSheet() {
        if (!sheetOpen) return;
        sheetOpen = false;
        const done = () => {
            sheet.hidden = true;
            if (sheetAnim) { sheetAnim.cancel(); sheetAnim = null; }
            document.documentElement.style.overflow = "";
            const back = lastFocus && document.contains(lastFocus) ? lastFocus : shelf;
            back.focus({ preventScroll: true });
            wake();
        };
        if (sheetAnim) sheetAnim.cancel();
        const a = sheet.animate([{ clipPath: "inset(0px 0px 0px 0px)" }, { clipPath: coverInset() }], { duration: reduce ? 1 : 850, easing: SHEET_EASE, fill: "both" });
        sheetAnim = a;
        a.onfinish = done;
        wake();
    }
    closeBtn.addEventListener("click", closeSheet);
    document.addEventListener("keydown", (e) => {
        if (!sheetOpen) return;
        if (e.key === "Escape") { e.preventDefault(); closeSheet(); }
        else if (e.key === "Tab") {                                              // keep focus inside the dialog
            const items = [closeBtn, S("go")];
            const at = items.indexOf(document.activeElement);
            const next = e.shiftKey ? items[(at <= 0 ? items.length : at) - 1] : items[(at + 1) % items.length];
            e.preventDefault();
            next.focus();
        }
    });

    /* ---------------------------------------------------------------- start */
    measure();
    build();
    s = T = Math.max(0, towns.findIndex((t) => t.name === "QUEENSTOWN"));       // opens on the town the hero shows
    wake();
    if ("IntersectionObserver" in window) {
        new IntersectionObserver((entries) => { visible = entries[0].isIntersecting; if (visible) wake(); }, { rootMargin: "200px" }).observe(root);
    }
    let resizeTimer = 0;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { measure(); wake(); }, 80); }, { passive: true });

    /* once the API answers, rebuild from the database: every town in the data, with its live median */
    if (typeof getOptions === "function" && typeof apiGet === "function") {
        Promise.all([getOptions(), apiGet("/api/insights/town-medians", { flat_type: "4 ROOM", months: 12 })]).then((res) => {
            const names = res[0].towns || [];
            if (!names.length) return;
            const byTown = {};
            (res[1].rows || []).forEach((row) => { byTown[row.town_name] = row; });
            const here = towns[active] ? towns[active].name : null;
            towns = names.map((name) => {
                const m = byTown[name];
                return { name, median: m ? Number(m.median_price) : null, count: m ? Number(m.txn_count) : 0, psm: m && m.median_psm ? Number(m.median_psm) : null };
            });
            build();
            const keep = towns.findIndex((t) => t.name === here);
            s = T = keep >= 0 ? keep : Math.floor(n / 4);
            wake();
        }).catch((err) => console.warn("[town-archive] keeping the built-in towns:", err.message));
    }
})();
