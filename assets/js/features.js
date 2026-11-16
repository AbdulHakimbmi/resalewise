/*
  ResaleWise SG — feature layer (loaded last on every page).

  Progressive enhancement: every block checks that its data exists and leaves
  the page exactly as it was when it doesn't (e.g. before the geospatial
  pipeline has been run, the original map embed stays).

    RWFeatures.details(record)      map with nearest stations + "What goes with a higher price?"
    RWFeatures.nearby(record)       "What's nearby": parks, hawker centres, gyms, childcare, schools
    RWFeatures.matchPage()          "Match my priorities": rank blocks by the weights you set
    RWFeatures.townMap(town)        blocks coloured by price per sqm + stations
    RWFeatures.townPicker(el)       grid of towns when none is chosen
    RWFeatures.momentum(el)         window-function league table (insights page)
    RWFeatures.reviewInsights(el)   $facet summary on the reviews page
    RWFeatures.wireReviewOwner(...) edit / delete your own review
    RWFeatures.account()            activity (trigger audit trail) + delete account
    RWFeatures.about()              validation results + photo credits
    RWFeatures.benchmarkPage()      the "Under the hood" page
*/
(function () {
  "use strict";
  const RWF = (window.RWFeatures = window.RWFeatures || {});
  const $ = (sel, root) => (root || document).querySelector(sel);
  const esc = s => (typeof escapeHtml === "function" ? escapeHtml(s) : String(s == null ? "" : s));
  const tc = s => (typeof titleCase === "function" ? titleCase(s || "") : String(s || ""));
  const money = n => (n == null || isNaN(n) ? "–" : "$" + Math.round(Number(n)).toLocaleString("en-SG"));
  const kmoney = n => (n == null ? "–" : n >= 1e6 ? "$" + (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + "m" : "$" + Math.round(n / 1000) + "k");
  const signed = (n, d) => (n == null || isNaN(n) ? "–" : (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(d == null ? 1 : d) + "%");
  const dist = m => (m == null ? "–" : m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(2) + " km");
  const fmtMs = ms => (ms == null ? "–" : ms < 1 ? ms.toFixed(2) + " ms" : ms < 10 ? ms.toFixed(1) + " ms" : Math.round(ms) + " ms");

  function get(path, params) {
    return apiGet(path, params || {});
  }
  function send(method, path, body) {
    return apiRequest(path, { method, body });
  }
  function toast(msg) {
    if (typeof showToast === "function") return showToast(msg);
    const t = $(".toast");
    if (t) { t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 3200); }
  }
  function loggedIn() {
    return !!(window.Auth && Auth.isLoggedIn());
  }

  // ------------------------------------------------------------------
  // Leaflet (bundled in assets/vendor/leaflet; loaded only when needed)
  // ------------------------------------------------------------------
  let leafletP = null;
  function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    if (leafletP) return leafletP;
    leafletP = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = "assets/vendor/leaflet/leaflet.css";
      document.head.appendChild(css);
      const s = document.createElement("script");
      s.src = "assets/vendor/leaflet/leaflet.js";
      s.onload = () => resolve(window.L);
      s.onerror = () => reject(new Error("map library failed to load"));
      document.head.appendChild(s);
    });
    return leafletP;
  }

  function isLight() {
    return document.documentElement.getAttribute("data-theme") === "light";
  }

  // OneMap basemaps (Singapore Land Authority): free, no API key, block numbers shown.
  // "Night" suits the dark theme and "Grey" the light one. If OneMap's tiles ever fail to
  // load, fall back to OpenStreetMap's own tiles rather than showing an empty map.
  function addTiles(L, map) {
    const onemap = () => "https://www.onemap.gov.sg/maps/tiles/" + (isLight() ? "Grey" : "Night") + "/{z}/{x}/{y}.png";
    const attrOneMap = '<a href="https://www.onemap.gov.sg/" target="_blank" rel="noopener">OneMap</a> &copy; contributors &#124; ' +
      '<a href="https://www.sla.gov.sg/" target="_blank" rel="noopener">Singapore Land Authority</a>';
    const attrOsm = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
    let fallback = false, errors = 0;
    const layer = L.tileLayer(onemap(), { minZoom: 11, maxZoom: 19, attribution: attrOneMap }).addTo(map);
    layer.on("tileerror", () => {
      if (fallback || ++errors < 4) return;
      fallback = true;
      layer.options.attribution = attrOsm;
      map.attributionControl.removeAttribution(attrOneMap);
      map.attributionControl.addAttribution(attrOsm);
      layer.setUrl("https://tile.openstreetmap.org/{z}/{x}/{y}.png");
    });
    new MutationObserver(() => { if (!fallback) layer.setUrl(onemap()); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return layer;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#6f9bff";
  }

  function makeMapIn(wrap) {
    const iframe = wrap.querySelector("iframe");
    const div = document.createElement("div");
    div.className = "rwx-map";
    div.setAttribute("role", "region");
    div.setAttribute("aria-label", "Interactive map");
    if (iframe) iframe.replaceWith(div); else wrap.appendChild(div);
    return div;
  }

  const stationIcon = L => L.divIcon({ className: "", html: '<div class="rwx-station-icon"></div>', iconSize: [14, 14], iconAnchor: [7, 7] });
  const PIN_LETTER = { park: "P", hawker: "H", gym: "G", childcare: "C", primary_school: "1", secondary_school: "2" };
  const pinIcon = (L, kind) => L.divIcon({ className: "", html: `<div class="rwx-pin rwx-pin-${kind}">${PIN_LETTER[kind] || "·"}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] });
  const homeIcon = L => L.divIcon({ className: "", html: '<div class="rwx-home-icon"></div>', iconSize: [18, 18], iconAnchor: [9, 9] });

  // ------------------------------------------------------------------
  // Details page
  // ------------------------------------------------------------------
  // Fixed places for the panels under the price, so they always read in the same order
  // whichever request answers first (nearby places, then what drives the price).
  const SLOTS = ["nearby", "drivers"];
  function slot(card, name) {
    let el = card.querySelector(`[data-slot="${name}"]`);
    if (el) return el;
    el = document.createElement("div");
    el.dataset.slot = name;
    const later = SLOTS.slice(SLOTS.indexOf(name) + 1).map(n => card.querySelector(`[data-slot="${n}"]`)).find(Boolean);
    later ? card.insertBefore(el, later) : card.appendChild(el);
    return el;
  }
  const amCache = {};
  const amenitiesFor = id => amCache[id] || (amCache[id] = get("/api/amenities/block/" + id));

  RWF.details = function (r) {
    if (!r) return;
    const card = $("#propertyDetail");
    if (card) SLOTS.forEach(n => slot(card, n));
    blockMap(r).catch(e => console.info("[features] map stays as embed:", e.message));
    driversPanel(r).catch(e => console.info("[features] drivers panel unavailable:", e.message));
    nearbyPanel(r).catch(e => console.info("[features] nearby panel unavailable:", e.message));
  };

  async function blockMap(r) {
    if (!r.block_id) return;
    const g = await get("/api/geo/block/" + r.block_id);
    if (!g || !g.available || !g.located) return;
    const card = $(".map-card");
    const wrap = card && card.querySelector(".map-wrap");
    if (!wrap) return;
    const L = await loadLeaflet();
    const el = makeMapIn(wrap);
    const map = L.map(el, { scrollWheelZoom: false, zoomControl: true });
    addTiles(L, map);
    const home = [g.lat, g.lon];
    L.marker(home, { icon: homeIcon(L), title: "This block" }).addTo(map)
      .bindPopup(`<b>Blk ${esc(r.block_no)} ${esc(r.street_name)}</b><br>${esc(g.matched_address || "")}`);
    const pts = [home];
    (g.stations || []).forEach((s, i) => {
      const p = [s.lat, s.lon];
      pts.push(p);
      L.marker(p, { icon: stationIcon(L), title: s.name }).addTo(map)
        .bindPopup(`<b>${esc(s.name)}</b> ${esc((s.codes || []).join(" / "))}<br>${dist(s.distance_m)} to the nearest exit`);
      if (i === 0) L.polyline([home, p], { color: cssVar("--chart-2"), weight: 2, dashArray: "6 6", opacity: .9 }).addTo(map);
    });
    const am = await amenitiesFor(r.block_id).catch(() => null);
    const places = (am && am.available && am.places) || [];
    places.forEach(p => {
      L.marker([p.lat, p.lon], { icon: pinIcon(L, p.kind), title: `${placeName(p.name)} (${KIND_LABEL[p.kind] || p.kind})` }).addTo(map)
        .bindPopup(`<b>${esc(placeName(p.name))}</b><br>${esc(KIND_LABEL[p.kind] || p.kind)} · ${dist(p.distance_m)} · ${esc(walk(p.walk_min))}`);
      if (p.distance_m <= 1000) pts.push([p.lat, p.lon]);          // frame what is within a short walk
    });
    if (places.length) {
      const legend = L.control({ position: "bottomleft" });
      legend.onAdd = () => {
        const div = L.DomUtil.create("div", "rwx-legend-pins");
        const kinds = KIND_ORDER.filter(k => places.some(p => p.kind === k));
        div.innerHTML = kinds.map(k => `<span><i class="rwx-pin rwx-pin-${k}">${PIN_LETTER[k]}</i>${esc(KIND_LABEL[k])}</span>`).join("");
        return div;
      };
      legend.addTo(map);
    }
    map.fitBounds(L.latLngBounds(pts).pad(0.35), { maxZoom: 17 });
    const sub = $("#mapSub");
    if (sub) {
      sub.innerHTML = `${esc(r.block_no)} ${esc(r.street_name)} · located by OneMap (${esc(g.match_quality)} match)`;
    }
    if (g.stations && g.stations.length) {
      const ul = document.createElement("ul");
      ul.className = "rwx-stations";
      ul.innerHTML = g.stations.map(s =>
        `<li><span><b>${esc(s.name)}</b><span class="rwx-codes">${esc((s.codes || []).join(" / "))}</span></span><span class="rwx-num">${dist(s.distance_m)}</span></li>`).join("");
      card.appendChild(ul);
      const note = document.createElement("p");
      note.className = "rwx-muted";
      note.style.cssText = "font-size:.78rem;margin:10px 0 0";
      note.textContent = "Straight-line distance to the nearest station exit, computed by MongoDB ($geoNear) and cross-checked with PostGIS.";
      card.appendChild(note);
    }
  }

  function ciRow(e, lo, hi) {
    const span = hi - lo || 1;
    const x = v => ((v - lo) / span) * 100;
    return `<div class="rwx-ci" aria-hidden="true">
      <span class="zero" style="left:${x(0)}%"></span>
      <span class="range" style="left:${x(e.ci_low)}%;width:${Math.max(1, x(e.ci_high) - x(e.ci_low))}%"></span>
      <span class="pt" style="left:${x(e.effect_pct)}%"></span></div>`;
  }

  function appliesTo(e, applies) {
    if (e.driver === "storey") return applies.storey === "high (13+)";
    if (e.driver === "lease") return applies.lease === "80+ years";
    if (e.driver === "mrt") return (applies.mrt === "<=500m" && e.contrast.indexOf("<=500") === 0)
      || (applies.mrt === "500m-1km" && e.contrast.indexOf("500 m-1 km") === 0);
    return false;
  }

  const DRIVER_LABEL = { storey: "High floor (13+) vs low (1–6)", lease: "80+ years lease left vs 60–70", mrt: "" };

  async function driversPanel(r) {
    const card = $("#propertyDetail");
    if (!card || !r.txn_id) return;
    const d = await get("/api/drivers/" + r.txn_id);
    const p = d.peers || {};
    const here = d.flat && d.flat.price_per_sqm;
    const box = document.createElement("section");
    box.className = "rwx-drivers";
    box.setAttribute("aria-labelledby", "driversTitle");
    let html = `<div class="small-kicker">Matched comparison</div><h3 id="driversTitle">What goes with a higher price?</h3>
      <p class="rwx-muted" style="margin:4px 0 0">${esc(p.definition || "")} — <b>${(p.n || 0).toLocaleString("en-SG")}</b> sales.</p>`;
    if (p.n) {
      const pricier = p.pct_cheaper_than_this;
      html += `<div class="rwx-peer">
        <div><b class="rwx-num">${money(here)}</b><span>This flat, per sqm</span></div>
        <div><b class="rwx-num">${money(p.median_psm)}</b><span>Typical for its peers</span></div>
        <div><b class="rwx-num">${pricier == null ? "–" : pricier + "%"}</b><span>of peers sold for less per sqm</span>
          <div class="rwx-bar"><i style="width:${Math.max(0, Math.min(100, pricier || 0))}%"></i></div></div>
      </div>`;
      if (!p.enough) html += `<p class="rwx-note">Only ${p.n} comparable sales: treat this comparison as rough.</p>`;
    }
    const effects = d.effects || [];
    if (effects.length) {
      const lo = Math.min(0, ...effects.map(e => e.ci_low)) - 2;
      const hi = Math.max(0, ...effects.map(e => e.ci_high)) + 2;
      html += `<p class="rwx-muted" style="margin:20px 0 0;font-size:.88rem">Across the market (sales since 2017), holding size, lease, flat type and place fixed, these features go with a higher price per sqm. Highlighted rows apply to this flat.</p>
        <ul class="rwx-effects">${effects.map(e => `
          <li class="${appliesTo(e, d.applies || {}) ? "applies" : ""}">
            <span class="lbl">${esc(DRIVER_LABEL[e.driver] || ("Distance to MRT/LRT: " + e.contrast))}</span>
            <span class="val ${e.effect_pct >= 0 ? "rwx-up" : "rwx-down"}">${signed(e.effect_pct)}</span>
            ${ciRow(e, lo, hi)}
            <span class="meta">95% CI ${signed(e.ci_low)} to ${signed(e.ci_high)} · ${e.n_strata.toLocaleString("en-SG")} matched groups, ${e.n_sales.toLocaleString("en-SG")} sales${e.naive_pct != null ? " · unmatched gap " + signed(e.naive_pct) : ""}</span>
          </li>`).join("")}</ul>`;
    } else {
      html += `<p class="rwx-note">Market-wide comparisons appear here once the price-driver analysis has been run.</p>`;
    }
    if (d.mrt && d.mrt.m != null) {
      html += `<p class="rwx-muted" style="margin:12px 0 0;font-size:.86rem">This block is <b>${dist(d.mrt.m)}</b> from ${esc(d.mrt.station || "the nearest station")}.</p>`;
    }
    html += `<p class="rwx-note"><b>Association, not causation.</b> ${esc(d.caveat || "")} <a href="about.html#drivers">How this is calculated</a></p>`;
    box.innerHTML = html;
    slot(card, "drivers").appendChild(box);
  }


  // ------------------------------------------------------------------
  // What's nearby (details page) and Match my priorities (match.html)
  // ------------------------------------------------------------------
  const KIND_ORDER = ["mrt", "park", "hawker", "gym", "childcare", "primary_school", "secondary_school"];
  const KIND_LABEL = { mrt: "MRT/LRT station", park: "Park", hawker: "Hawker centre", gym: "Gym", childcare: "Childcare centre",
    primary_school: "Primary school", secondary_school: "Secondary school" };
  const walk = m => (m == null ? "–" : m + " min walk");
  // Names arrive in capitals; title-casing them turns "PCF" into "Pcf", so put the common acronyms back.
  const placeName = n => tc(n || "").replace(/\b(Pcf|Ntuc|Ymca|Ywca|Cdc|Moe|Safra|Ite|Hdb|Mcst)\b/g, m => m.toUpperCase());

  function scoreBar(v) {
    const w = Math.max(0, Math.min(100, Number(v) || 0));
    return `<div class="rwx-bar" role="img" aria-label="Closeness score ${Math.round(w)} out of 100"><i style="width:${w}%"></i></div>`;
  }

  async function nearbyPanel(r) {
    const card = $("#propertyDetail");
    if (!card || !r.block_id) return;
    const d = await amenitiesFor(r.block_id);
    if (!d || !d.available || !(d.items || []).length) return;
    const places = {};
    (d.places || []).forEach(p => (places[p.kind] = places[p.kind] || []).push(p));
    const box = document.createElement("section");
    box.className = "rwx-nearby";
    box.setAttribute("aria-labelledby", "nearbyTitle");
    let html = `<h3 id="nearbyTitle">What’s nearby?</h3>
      <p class="rwx-muted rwx-nearby-sub">The nearest of each kind within 2 km. The lettered squares on the map mark the same places.</p>
      <ul class="rwx-nearby-list">${d.items.map(i => {
        const list = places[i.kind] || [];
        return `
        <li>
          <span class="lbl"><b>${esc(i.label)}</b>${list.length ? "" : `<span class="rwx-muted">${esc(tc(i.nearest_name || ""))}</span>`}</span>
          <span class="val rwx-num">${dist(i.nearest_m)}<span class="rwx-muted"> · ${esc(walk(i.walk_min))}</span></span>
          ${scoreBar(i.score)}
          ${list.length ? `<ul class="rwx-places">${list.slice(0, 3).map(p =>
            `<li><span>${esc(placeName(p.name))}</span><span class="rwx-muted">${dist(p.distance_m)}</span></li>`).join("")}</ul>` : ""}
          <span class="meta">${i.within_1km == null ? "" : i.within_1km + " within 1 km"}${i.within_2km == null ? "" : " · " + i.within_2km + " within 2 km"}</span>
        </li>`;
      }).join("")}</ul>`;
    if (d.premium && d.premium.premium_pct != null) {
      const p = d.premium.premium_pct;
      html += `<p class="rwx-muted" style="margin:14px 0 0;font-size:.9rem">In past sales, the model links this block’s surroundings to prices
        <b class="${p >= 0 ? "rwx-up" : "rwx-down"}">${signed(p)}</b> ${p >= 0 ? "above" : "below"} a typical block
        (${d.premium.n_sales} sales). <a href="about.html#amenities">How this is calculated</a></p>`;
    }
    html += `<p class="rwx-note">${esc(d.note || "")} The bars show closeness, not quality: a park or school next door can be
      a convenience for one buyer and noise for another. <b>Association, not causation.</b></p>`;
    box.innerHTML = html;
    slot(card, "nearby").appendChild(box);
  }
  RWF.nearby = r => nearbyPanel(r);

  RWF.matchPage = async function () {
    const root = $("#rwxMatch");
    if (!root) return;
    const form = $("#matchForm", root), out = $("#matchOut", root);
    const flatSel = $("#mFlat", root), townSel = $("#mTown", root);
    await Promise.all([populateFlatTypes(flatSel, { includeAny: false }), populateTowns(townSel, { includeAny: true, anyLabel: "Any town" })]);
    for (const o of flatSel.options) if (o.textContent.trim().toUpperCase() === "4 ROOM") { flatSel.value = o.value; break; }

    const sliders = KIND_ORDER.map(k => ({ k, el: $("#w_" + k, root), out: $("#wv_" + k, root) }));
    const syncLabels = () => sliders.forEach(s => {
      s.out.textContent = s.el.value === "0" ? "ignore" : s.el.value;
      s.el.style.setProperty("--v", (100 * s.el.value / (s.el.max || 5)) + "%");   // fills the slider track
    });
    let timer = null, seq = 0;

    function params() {
      const p = { flat_type: flatSel.value, months: $("#mMonths", root).value, limit: 20 };
      if (townSel.value) p.town = townSel.value;
      const budget = parseInt($("#mBudget", root).value.replace(/[^0-9]/g, ""), 10);
      if (budget > 0) p.max_price = budget;
      sliders.forEach(s => { p["w_" + s.k] = s.el.value; });
      return p;
    }

    // One row per block. The columns are the kinds that were scored, each with its distance, walk time
    // and a closeness bar, so blocks can be compared down a column instead of by reading every chip.
    function row(r, i, cols) {
      const cells = cols.map(k => `<div class="rwx-cell"><span class="rwx-cell-label">${esc(KIND_LABEL[k])}</span>
          <b class="rwx-num">${dist(r.nearest_m[k])}</b><span class="rwx-muted">${esc(walk(r.walk_min[k]))}</span>${scoreBar(r.scores[k])}</div>`).join("");
      const prem = r.premium_pct == null ? "" :
        `<span class="rwx-prem ${r.premium_pct >= 0 ? "rwx-up" : "rwx-down"}" title="Model estimate from past sales: an association, not a cause">Location ${signed(r.premium_pct)} vs typical</span>`;
      const act = r.latest_txn_id ? `<a class="rwx-link" href="details.html?id=${encodeURIComponent(r.latest_txn_id)}">Latest sale &amp; map →</a>` : "";
      return `<li class="rwx-row">
        <span class="rwx-rank">${i + 1}</span>
        <div class="rwx-blk">
          <h3>Blk ${esc(r.block_no)} ${esc(tc(r.street_name))}</h3>
          <p class="rwx-muted">${esc(tc(r.town_name))} · typical ${money(r.median_price)} · ${money(r.median_psm)}/sqm · ${r.n_sales} sales</p>
          <p class="rwx-blk-act">${prem}${act}</p>
        </div>
        ${cells}
        <div class="rwx-score"><span class="rwx-cell-label">Match score</span><b class="rwx-num">${Math.round(r.score)}</b>${scoreBar(r.score)}</div>
      </li>`;
    }

    function table(d) {
      const have = Object.keys(d.results[0].scores || {});
      const cols = KIND_ORDER.filter(k => have.includes(k) && d.results[0].nearest_m && d.results[0].nearest_m[k] !== undefined);
      const head = `<div class="rwx-rank-head" aria-hidden="true"><span></span><span>Block</span>${cols.map(k => {
        const sc = d.scale[k];
        return `<span>${esc(KIND_LABEL[k])}${sc ? `<small>full score within ${sc.full_score_within_m} m</small>` : ""}</span>`;
      }).join("")}<span>Match score</span></div>`;
      return `<div class="rwx-rank-table" style="--cols:${cols.length}">${head}<ol class="rwx-rank-list">${d.results.map((r, i) => row(r, i, cols)).join("")}</ol></div>`;
    }

    async function run() {
      const mine = ++seq;
      out.setAttribute("aria-busy", "true");
      try {
        const d = await get("/api/amenities/rank", params());
        if (mine !== seq) return;
        if (d.available) {
          const off = new Set(d.not_loaded || []);
          sliders.forEach(s => {
            s.el.disabled = off.has(s.k);
            s.el.closest(".rwx-weight").title = off.has(s.k) ? "No data loaded for this yet" : "";
            if (off.has(s.k)) s.out.textContent = "no data";
          });
        }
        if (!d.available) {
          out.innerHTML = `<p class="rwx-note">Nearby-place data has not been loaded yet. Run <code>scripts/load_amenities.py</code> and
            <code>scripts/build_amenity_features.py</code> (see USER_MANUAL.md), then reload this page.</p>`;
          return;
        }
        const head = `<p class="rwx-summary">${d.scored.toLocaleString("en-SG")} blocks scored from ${d.candidates.toLocaleString("en-SG")} with enough recent ${esc(d.flat_type.toLowerCase())} sales`
          + (d.not_located ? ` (${d.not_located} could not be located on the map)` : "") + `. Showing the top ${d.results.length}.</p>`;
        out.innerHTML = d.results.length
          ? head + table(d) + `<p class="rwx-note">${esc(d.note)} Blocks with the same score are listed cheapest first. “Typical” is the median of the last ${d.months} months.
              Distances are straight-line; the score for each kind runs from 100 (very close) to 0 (far): ${
              Object.values(d.scale).map(s => `${esc(s.label.toLowerCase())} ${s.full_score_within_m} m → ${s.zero_score_at_m} m`).join(", ")}.</p>`
          : `<p class="rwx-note">No blocks match. Try a longer time window, a higher budget or a different town.</p>`;
      } catch (e) {
        if (mine === seq) out.innerHTML = `<p class="rwx-note">${esc(e.message || "Couldn’t load the ranking.")}</p>`;
      } finally {
        if (mine === seq) out.removeAttribute("aria-busy");
      }
    }

    const later = () => { clearTimeout(timer); timer = setTimeout(run, 350); };
    sliders.forEach(s => s.el.addEventListener("input", () => { syncLabels(); later(); }));
    form.addEventListener("change", later);
    form.addEventListener("submit", e => { e.preventDefault(); clearTimeout(timer); run(); });
    syncLabels();
    run();
  };

  // ------------------------------------------------------------------
  // Town page
  // ------------------------------------------------------------------
  const PSM_CLASSES = 5;
  function ramp(i) {   // light -> strong version of the primary colour
    const a = [0.25, 0.42, 0.6, 0.78, 1][i];
    return `color-mix(in srgb, ${cssVar("--chart-1")} ${Math.round(a * 100)}%, ${isLight() ? "#ffffff" : "#0e1013"})`;
  }

  RWF.townMap = async function (town) {
    if (!town) return;
    let g;
    try { g = await get("/api/geo/town/" + encodeURIComponent(town)); } catch (_) { return; }
    if (!g || !g.available || !g.blocks || !g.blocks.length) return;
    const frame = $("#townMap");
    const wrap = frame && frame.closest(".map-wrap");
    if (!wrap) return;
    const L = await loadLeaflet();
    const el = makeMapIn(wrap);
    const map = L.map(el, { scrollWheelZoom: false, preferCanvas: true });
    addTiles(L, map);
    const priced = g.blocks.filter(b => b.median_psm).map(b => b.median_psm).sort((a, b) => a - b);
    const cut = k => priced.length ? priced[Math.min(priced.length - 1, Math.floor(priced.length * k / PSM_CLASSES))] : 0;
    const breaks = [1, 2, 3, 4].map(cut);
    const cls = v => (v == null ? -1 : breaks.filter(b => v >= b).length);
    const pts = [];
    g.blocks.forEach(b => {
      const c = cls(b.median_psm);
      pts.push([b.lat, b.lon]);
      L.circleMarker([b.lat, b.lon], {
        radius: 5, weight: 1, color: isLight() ? "#ffffff" : "#0e1013",
        fillColor: c < 0 ? cssVar("--muted") : ramp(c), fillOpacity: c < 0 ? 0.35 : 0.95,
      }).addTo(map).bindPopup(
        `<b>Blk ${esc(b.block_no)} ${esc(b.street_name)}</b><br>` +
        (b.median_psm ? `${money(b.median_psm)} per sqm (median, ${b.n} sales in ${g.months} months)` : "No sales in the window") +
        (b.nearest_mrt_m != null ? `<br>${dist(b.nearest_mrt_m)} to ${esc(b.nearest_station_name)}` : "") +
        `<br><a href="search.html?town=${encodeURIComponent(town)}">See sales in ${esc(tc(town))} →</a>`);
    });
    (g.stations || []).forEach(s => {
      L.marker([s.lat, s.lon], { icon: stationIcon(L), title: s.name }).addTo(map)
        .bindTooltip(`${s.name} ${(s.codes || []).join(" / ")}`, { direction: "top", offset: [0, -8] });
    });
    map.fitBounds(L.latLngBounds(pts).pad(0.08));
    const legend = document.createElement("div");
    legend.className = "rwx-legend";
    legend.innerHTML = `<span>Median price per sqm, last ${g.months} months:</span>
      <span>${money(priced[0])}<span class="rwx-scale">${[0, 1, 2, 3, 4].map(i => `<span style="background:${ramp(i)}"></span>`).join("")}</span>${money(priced[priced.length - 1])}</span>
      <span><i class="sq" style="background:var(--chart-2)"></i>MRT / LRT station</span>`;
    wrap.parentElement.appendChild(legend);
    const sub = wrap.parentElement.querySelector("p");
    if (sub) sub.textContent = `${g.blocks.length} geocoded blocks. Click a dot for its recent prices.`;
  };

  RWF.townPicker = async function (el) {
    if (!el) return;
    el.innerHTML = `<div class="small-kicker">Pick a town</div><h3 style="margin:6px 0 0">All ${""}towns at a glance</h3>
      <p class="rwx-muted" style="margin:6px 0 0">Median 4-room resale price over the last 12 months.</p><div class="rwx-towns" id="rwxTowns">${RW.skeleton ? "" : ""}</div>`;
    const grid = $("#rwxTowns", el);
    try {
      const [opts, med] = await Promise.all([getOptions(), get("/api/insights/town-medians", { flat_type: "4 ROOM", months: 12 })]);
      const m = {};
      (med.rows || []).forEach(r => { m[r.town_name] = r; });
      grid.innerHTML = (opts.towns || []).map(t => {
        const r = m[t];
        return `<a class="rwx-town" href="town.html?town=${encodeURIComponent(t)}"><b>${esc(tc(t))}</b>
          <span>${r ? kmoney(r.median_price) : "–"}</span><small>${r ? r.txn_count.toLocaleString("en-SG") + " sales" : "too few 4-room sales"}</small></a>`;
      }).join("");
    } catch (e) {
      grid.innerHTML = `<p class="rwx-note">Couldn’t load the towns right now.</p>`;
    }
  };

  // ------------------------------------------------------------------
  // Insights page: momentum league table (window functions)
  // ------------------------------------------------------------------
  RWF.momentum = async function (el) {
    if (!el) return;
    el.innerHTML = `<div class="rwx-section-title"><div><div class="small-kicker">Window functions</div><h3>Which towns are moving?</h3></div>
      <label class="rwx-muted" style="font-size:.85rem">Flat type <select id="momType" style="margin-left:6px"></select></label></div>
      <p class="rwx-muted" style="margin:6px 0 0" id="momSub">Latest quarter, towns with at least 30 sales.</p>
      <div class="rwx-table-wrap"><table class="rwx-table"><thead><tr><th>Rank</th><th>Town</th><th>Median</th><th>vs last quarter</th><th>vs a year ago</th><th>Rank change (1y)</th><th>Sales</th></tr></thead><tbody id="momBody"><tr><td colspan="7">Loading…</td></tr></tbody></table></div>
      <p class="rwx-note">Computed in PostgreSQL with <code>RANK()</code>, <code>PERCENT_RANK()</code> and date-based <code>RANGE</code> window frames over a materialized view of quarterly medians, so a quarter with no sales is a gap, never a wrong comparison.</p>`;
    const sel = $("#momType", el);
    if (typeof populateFlatTypes === "function") await populateFlatTypes(sel, { selected: "4 Room" });
    const body = $("#momBody", el);
    async function load() {
      body.innerHTML = `<tr><td colspan="7">Loading…</td></tr>`;
      try {
        const m = await get("/api/insights/momentum", { flat_type: sel.value || "4 ROOM", min_n: 30 });
        $("#momSub", el).textContent = `${RW.formatQuarter ? RW.formatQuarter(m.quarter) : m.quarter} · towns with at least 30 sales that quarter.`;
        if (!m.rows.length) { body.innerHTML = `<tr><td colspan="7">Not enough sales for this flat type.</td></tr>`; return; }
        body.innerHTML = m.rows.map(r => {
          const rc = r.rank_change_1y;
          const arrow = rc == null ? "–" : rc > 0 ? `<span class="rwx-up">▲ ${rc}</span>` : rc < 0 ? `<span class="rwx-down">▼ ${-rc}</span>` : "=";
          const cls = v => (v == null ? "" : v >= 0 ? "rwx-up" : "rwx-down");
          return `<tr><td class="rwx-num">${r.price_rank}</td><td><a href="town.html?town=${encodeURIComponent(r.town_name)}">${esc(tc(r.town_name))}</a></td>
            <td class="rwx-num">${money(r.median_price)}</td><td class="rwx-num ${cls(r.qoq_pct)}">${signed(r.qoq_pct)}</td>
            <td class="rwx-num ${cls(r.yoy_pct)}">${signed(r.yoy_pct)}</td><td>${arrow}</td><td class="rwx-num">${r.txn_count}</td></tr>`;
        }).join("");
      } catch (e) {
        body.innerHTML = `<tr><td colspan="7">${esc(e.code === "feature_unavailable" ? "Run scripts/migrate.py to enable this table." : "Couldn’t load this table.")}</td></tr>`;
      }
    }
    sel.addEventListener("change", load);
    load();
  };

  // ------------------------------------------------------------------
  // Reviews page
  // ------------------------------------------------------------------
  RWF.reviewInsights = async function (el, town) {
    if (!el) return;
    try {
      const d = await get("/api/reviews/insights", town ? { town } : {});
      const total = (d.ratings || []).reduce((a, r) => a + r.count, 0);
      if (!total) { el.hidden = true; return; }
      const by = {};
      d.ratings.forEach(r => { by[r.stars] = r.count; });
      const hist = [5, 4, 3, 2, 1].map(s => `<div><span>${s}★</span><span class="t"><i style="width:${(100 * (by[s] || 0) / total).toFixed(1)}%"></i></span><span class="rwx-num">${by[s] || 0}</span></div>`).join("");
      const tags = (d.tags || []).map(t => `<span class="rwx-chip">${esc(t.tag)} <span class="rwx-muted">${t.count}</span></span>`).join("") || '<span class="rwx-muted">No tags yet</span>';
      el.hidden = false;
      el.innerHTML = `<div><div class="small-kicker">Ratings${town ? " in " + esc(tc(town)) : ""}</div><div class="rwx-hist" style="margin-top:10px">${hist}</div></div>
        <div><div class="small-kicker">What people mention</div><div class="rwx-tags" style="margin-top:10px">${tags}</div>
        <p class="rwx-muted" style="font-size:.74rem;margin:12px 0 0">One MongoDB aggregation ($facet) computes both panels.</p></div>`;
    } catch (_) { el.hidden = true; }
  };

  // Owner actions on review cards rendered with data-review-id + .rc-text.
  RWF.wireReviewOwner = function (listEl, reload) {
    if (!listEl || listEl.dataset.rwxWired) return;
    listEl.dataset.rwxWired = "1";
    listEl.addEventListener("click", async ev => {
      const btn = ev.target.closest("button[data-act]");
      if (!btn) return;
      const card = btn.closest("[data-review-id]");
      const id = card && card.dataset.reviewId;
      if (!id) return;
      const act = btn.dataset.act;
      if (act === "delete") {
        if (btn.dataset.armed !== "1") {
          btn.dataset.armed = "1";
          btn.textContent = "Tap again to delete";
          setTimeout(() => { btn.dataset.armed = ""; btn.textContent = "Delete"; }, 4000);
          return;
        }
        try { await send("DELETE", "/api/reviews/" + id); toast("Review deleted."); reload(); }
        catch (e) { toast(e.message || "Couldn’t delete that review."); }
      } else if (act === "edit") {
        const text = card.querySelector(".rc-text");
        const rating = Number(card.dataset.rating) || 5;
        const form = document.createElement("div");
        form.className = "rwx-edit";
        form.innerHTML = `<textarea maxlength="1000" aria-label="Edit your review">${esc(text ? text.textContent : "")}</textarea>
          <div class="rwx-owner"><select aria-label="Rating">${[5, 4, 3, 2, 1].map(s => `<option value="${s}" ${s === rating ? "selected" : ""}>${s} stars</option>`).join("")}</select>
          <button type="button" data-act="save">Save</button><button type="button" data-act="cancel">Cancel</button></div>`;
        card.querySelector(".rwx-owner").replaceWith(form);
        if (text) text.hidden = true;
        form.querySelector("textarea").focus();
      } else if (act === "cancel") {
        reload();
      } else if (act === "save") {
        const form = btn.closest(".rwx-edit");
        try {
          await send("PATCH", "/api/reviews/" + id, { comment: form.querySelector("textarea").value, rating: Number(form.querySelector("select").value) });
          toast("Review updated."); reload();
        } catch (e) { toast(e.message || "Couldn’t save your changes."); }
      }
    });
  };

  // ------------------------------------------------------------------
  // Account page: shortlist activity (trigger audit) + delete account
  // ------------------------------------------------------------------
  RWF.account = async function () {
    const host = $("#rwxAccountExtras");
    if (!host || !loggedIn()) return;
    host.innerHTML = `
      <article class="card" style="margin-top:18px"><div class="small-kicker">Recorded by a database trigger</div><h3 style="margin:6px 0 0">Recent shortlist activity</h3>
        <ul class="rwx-activity" id="rwxActivity"><li>Loading…</li></ul></article>
      <article class="card rwx-danger-zone" style="margin-top:18px"><div class="small-kicker">Danger zone</div><h3 style="margin:6px 0 0">Delete your account</h3>
        <p class="rwx-muted" style="margin:6px 0 0">This permanently removes your account and shortlist. Reviews you wrote stay up, credited to “Former member”.</p>
        <form id="rwxDelete" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;align-items:center">
          <input type="password" id="rwxDelPw" autocomplete="current-password" placeholder="Confirm with your password" aria-label="Your password, to confirm deleting the account" required
            style="flex:1;min-width:220px;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--ink)">
          <button class="rwx-danger" id="rwxDelBtn" type="submit">Delete account</button></form>
        <p class="rwx-muted" id="rwxDelMsg" role="status" style="margin:10px 0 0;font-size:.85rem"></p></article>`;
    try {
      const rows = await get("/api/me/activity");
      const ul = $("#rwxActivity");
      ul.innerHTML = rows.length ? rows.map(a => `<li><span><b>${esc(a.action[0].toUpperCase() + a.action.slice(1))}</b> ${esc(a.block_no ? "Blk " + a.block_no + " " + a.street_name : "#" + a.txn_id)}${a.town_name ? " · " + esc(tc(a.town_name)) : ""}</span>
        <span class="rwx-muted">${esc(new Date(a.changed_at).toLocaleString("en-SG", { dateStyle: "medium", timeStyle: "short" }))}</span></li>`).join("")
        : `<li class="rwx-muted">Nothing yet — save a flat to your shortlist and it shows up here.</li>`;
    } catch (_) { $("#rwxActivity").innerHTML = `<li class="rwx-muted">Activity isn’t available right now.</li>`; }
    const form = $("#rwxDelete"), btn = $("#rwxDelBtn"), msg = $("#rwxDelMsg");
    form.addEventListener("submit", async ev => {
      ev.preventDefault();
      if (!btn.classList.contains("armed")) {
        btn.classList.add("armed");
        btn.textContent = "Yes, delete it";
        msg.textContent = "Press again to confirm. This cannot be undone.";
        return;
      }
      btn.disabled = true;
      try {
        await send("DELETE", "/api/me", { password: $("#rwxDelPw").value });
        Auth.logout();
        location.href = "index.html";
      } catch (e) {
        btn.disabled = false; btn.classList.remove("armed"); btn.textContent = "Delete account";
        msg.textContent = e.message || "Couldn’t delete the account.";
      }
    });
  };

  // ------------------------------------------------------------------
  // About page + "Under the hood": published results
  // ------------------------------------------------------------------
  function sampleBadge(d) {
    return d && d.sample ? '<span class="rwx-badge sample" title="Bundled example output: run the scripts to replace it">Sample data</span>' : "";
  }

  function checkCard(c) {
    const m = c.metrics || {};
    let big = "";
    if (c.id === "V1") big = m.pct_blocks != null ? m.pct_blocks + "% of blocks located" : "";
    if (c.id === "V2") big = m.outside_bbox != null ? m.outside_bbox + " outside Singapore" : "";
    if (c.id === "V3") big = m.accuracy_pct != null ? m.accuracy_pct + "% correct by eye" : (m.checked || 0) + " of " + (m.sampled || 30) + " checked";
    if (c.id === "V4") big = m.agreement_pct_excl_ties != null ? m.agreement_pct_excl_ties + "% same nearest station" : "";
    if (c.id === "V5") big = m.probes != null ? (m.probes - m.failed) + " / " + m.probes + " probes behave" : "";
    if (c.id === "V6") big = c.status === "pass" ? "identical on rebuild" : "";
    if (c.id === "V7") big = m.counts_identical_same_sphere_pct != null ? m.counts_identical_same_sphere_pct + "% of amenity counts match" : "";
    return `<div class="rwx-check"><span class="rwx-badge ${esc(c.status)}">${esc(c.status)}</span><h4>${esc(c.id)} · ${esc(c.name)}</h4>
      <p>${esc(c.criterion)}</p><span class="big">${esc(big)}</span></div>`;
  }

  async function renderValidation(el) {
    if (!el) return;
    try {
      const d = await get("/api/results/geo-validation");
      const v4 = (d.checks || []).find(c => c.id === "V4");
      let extra = "";
      if (v4 && v4.metrics && v4.metrics.rel_diff_vs_postgis_spheroid) {
        const m = v4.metrics;
        extra = `<p class="rwx-note">MongoDB and PostGIS agree on the nearest station for <b>${m.agreement_pct_excl_ties}%</b> of blocks. Their distances differ by at most <b>${m.rel_diff_vs_postgis_sphere.max_pct}%</b> on a sphere (the two radii differ by ${m.radius_ratio_pct}%) and <b>${m.rel_diff_vs_postgis_spheroid.max_pct}%</b> against the WGS84 spheroid; ${m.one_km_band_flips} blocks change their “within 1 km” answer between the two earth models.</p>`;
      }
      el.innerHTML = `<div class="rwx-section-title"><h3>Map data checks ${sampleBadge(d)}</h3><span class="rwx-muted" style="font-size:.8rem">${esc(d.label || (d.generated_at ? "Run " + day(d.generated_at) : ""))}</span></div>
        <div class="rwx-checks">${(d.checks || []).map(checkCard).join("")}</div>${extra}`;
    } catch (_) {
      el.innerHTML = `<p class="rwx-note">The geospatial checks appear here once <code>scripts/validate_geo.py</code> has been run.</p>`;
    }
  }

  RWF.about = function () {
    renderValidation($("#rwxValidation"));
  };

  const CFG_LABEL = { postgres: "PostgreSQL (joins)", postgres_flat: "PostgreSQL (flat)", mongodb: "MongoDB" };
  const GEO_WORKLOADS = ["W4", "W5"];   // PostGIS queries: no joins
  const cfgLabel = (c, wid) => (c === "postgres" && GEO_WORKLOADS.includes(wid) ? "PostgreSQL (PostGIS)" : CFG_LABEL[c] || c);
  const day = iso => { const t = new Date(iso); return isNaN(t) ? "" : t.toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" }); };

  function workloadBlock(w) {
    const cfgs = w.configs.filter(c => w.latency[c]);
    const max = Math.max(...cfgs.map(c => w.latency[c].p95_ms)) * 1.05 || 1;
    const eq = w.equivalence || {};
    const ok = eq.checked && eq.passed === eq.checked;
    return `<div class="rwx-wl"><div class="rwx-wl-head"><span><span class="rwx-muted rwx-num">${esc(w.id)}</span> <b>${esc(w.title)}</b> <span class="rwx-muted">· ${esc(w.what)}</span></span>
      <span class="rwx-badge ${ok ? "pass" : "warn"}" title="Answers compared before timing">same answers ${eq.passed}/${eq.checked}</span></div>
      <div class="rwx-bars">${cfgs.map(c => {
        const s = w.latency[c];
        return `<div class="rwx-brow"><span class="name">${cfgLabel(c, w.id)}</span><span class="track">
          <span class="fill cfg-${c}" style="width:${(100 * s.p50_ms / max).toFixed(1)}%"></span>
          <span class="whisk" style="left:${(100 * s.p50_ci95[0] / max).toFixed(2)}%;width:${Math.max(0.6, 100 * (s.p50_ci95[1] - s.p50_ci95[0]) / max).toFixed(2)}%"></span></span>
          <span class="ms" title="p95 ${fmtMs(s.p95_ms)} · p99 ${fmtMs(s.p99_ms)}">${fmtMs(s.p50_ms)}</span></div>`;
      }).join("")}</div>
      <p class="rwx-muted" style="font-size:.76rem;margin:8px 0 0">Median of ${w.latency[cfgs[0]].n} runs (whisker: 95% CI) · p95: ${cfgs.map(c => cfgLabel(c, w.id) + " " + fmtMs(w.latency[c].p95_ms)).join(" · ")}</p></div>`;
  }

  // W7 single-row writes and the storage each data model takes
  function writesBlock(d) {
    const w = d.writes, st = d.storage;
    if (!w || !w.modes) return "";
    const p50 = (mode, op, c) => (((w.modes[mode] || {})[op] || {})[c] || {}).p50_ms;
    const rows = ["durable", "fast"].flatMap(mode => w.ops.map(op => `<tr><td>${esc(op)}</td><td>${mode === "durable" ? "yes" : "no"}</td>
      <td>${fmtMs(p50(mode, op, "postgres"))}</td><td>${fmtMs(p50(mode, op, "mongodb"))}</td></tr>`)).join("");
    const mb = x => (x == null ? "–" : x.toLocaleString("en-SG") + " MB");
    const kept = { postgres: "5 tables, uncompressed", postgres_flat: "1 table, uncompressed", mongodb: "1 collection, compressed" };
    const strows = st ? ["postgres", "postgres_flat", "mongodb"].map(c => st[c] ? `<tr><td>${CFG_LABEL[c] || c}</td><td>${kept[c]}</td>
      <td>${mb(st[c].data_mb)}</td><td>${mb(st[c].index_mb)}</td></tr>` : "").join("") : "";
    return `<div class="small-kicker" style="margin-top:22px">W7 · one sale inserted, updated and deleted, median of each, committed</div>
      <div class="rwx-table-wrap"><table class="rwx-table"><thead><tr><th>Write</th><th>Waits for disk</th><th>PostgreSQL</th><th>MongoDB</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="rwx-note">Same answers after every step in ${w.equivalence ? w.equivalence.passed + " of " + w.equivalence.checked : "–"} checks. ${esc(w.note || "")}</p>
      ${strows ? `<div class="small-kicker" style="margin-top:18px">Storage on disk</div>
      <div class="rwx-table-wrap"><table class="rwx-table"><thead><tr><th>Data model</th><th>Stored as</th><th>Data</th><th>Indexes</th></tr></thead>
      <tbody>${strows}</tbody></table></div><p class="rwx-note">${esc(st.note || "")}</p>` : ""}`;
  }

  // operations per second at each thread count, one row per workload and set-up
  function throughputTable(d) {
    const rows = {}, threads = [...new Set((d.concurrency || []).map(c => c.threads))].sort((a, b) => a - b);
    (d.concurrency || []).forEach(c => { (rows[c.workload + "|" + c.config] = rows[c.workload + "|" + c.config] || {})[c.threads] = c; });
    if (!threads.length) return "";
    const body = Object.entries(rows).map(([k, by]) => {
      const [wid, cfg] = k.split("|");
      return `<tr><td>${esc(wid)}</td><td>${cfgLabel(cfg, wid)}</td>${threads.map(n => `<td>${by[n] ? Math.round(by[n].ops_per_s).toLocaleString("en-SG") : "–"}</td>`).join("")}</tr>`;
    }).join("");
    return `<div class="small-kicker" style="margin-top:22px">Throughput · operations per second, 10 s per step</div>
      <div class="rwx-table-wrap"><table class="rwx-table"><thead><tr><th>Workload</th><th>Set-up</th>${threads.map(n => `<th>${n} thread${n > 1 ? "s" : ""}</th>`).join("")}</tr></thead>
      <tbody>${body}</tbody></table></div>`;
  }

  RWF.benchmarkPage = async function () {
    const res = $("#rwxBench");
    if (res) {
      try {
        const d = await get("/api/results/benchmark");
        const env = d.environment || {};
        const bl = d.bulk_load || {};
        const ms = d.median_semantics || {};
        const box = ((env.containers || {})["resalewise-postgres"] || {}).cpus;
        res.innerHTML = `<div class="rwx-section-title"><div><div class="small-kicker">Measured</div><h3>Latency per query ${sampleBadge(d)}</h3></div>
            <span class="rwx-muted" style="font-size:.8rem">${esc(d.label || "")}</span></div>
          <p class="rwx-muted" style="margin:6px 0 0;font-size:.85rem">${esc(env.postgres || "")}${env.postgis ? " + PostGIS " + esc(env.postgis) : ""} · MongoDB ${esc(env.mongodb || "")} · ${esc(String(env.rows_postgres || "").replace(/\B(?=(\d{3})+(?!\d))/g, ","))} rows · ${box ? esc(box) + " CPUs per database" : esc(env.cpu_count || "") + " CPUs"} · run ${esc(day(d.generated_at))}</p>
          <div class="rwx-legend"><span><i class="sq cfg-postgres"></i>PostgreSQL, normalised (the app)</span><span><i class="sq cfg-postgres_flat"></i>PostgreSQL, one flat table</span><span><i class="sq cfg-mongodb"></i>MongoDB documents</span></div>
          ${(d.workloads || []).map(workloadBlock).join("")}
          <div class="rwx-grid2" style="margin-top:10px">
            <div class="rwx-check"><div class="small-kicker">W6 · bulk load</div><span class="big">${(bl.postgres_copy || {}).rows_per_s_median ? bl.postgres_copy.rows_per_s_median.toLocaleString("en-SG") : "–"} rows/s</span><p>PostgreSQL COPY</p>
              <span class="big">${(bl.mongodb_insert_many || {}).rows_per_s_median ? bl.mongodb_insert_many.rows_per_s_median.toLocaleString("en-SG") : "–"} docs/s</span><p>MongoDB insert_many (${(bl.rows || 0).toLocaleString("en-SG")} rows, median of ${((bl.postgres_copy || {}).runs || []).length} runs)</p></div>
          </div>
          ${throughputTable(d)}
          ${box ? `<p class="rwx-note">Each database runs in its own container limited to <b>${box} CPUs</b>, so with more than ${box} threads the queries queue for CPU time: throughput then shows that limit, not the engine.</p>` : ""}
          ${writesBlock(d)}
          ${ms.groups ? `<p class="rwx-note"><b>Two kinds of median.</b> MongoDB's <code>$median</code> returned exactly PostgreSQL's <code>PERCENTILE_DISC(0.5)</code> (an observed price) in every check, so the timings compare like with like. The app itself uses <code>PERCENTILE_CONT</code>, which averages the two middle prices; that gives a different number in ${ms.pct_groups}% of ${ms.groups.toLocaleString("en-SG")} town × type × quarter groups (up to ${ms.max_rel_diff_pct}%).</p>` : ""}`;
      } catch (_) {
        res.innerHTML = `<p class="rwx-note">Benchmark results appear here once <code>python bench/run_bench.py</code> has been run.</p>`;
      }
    }
    renderValidation($("#rwxValidation"));

    const form = $("#rwxEngineForm");
    if (form) {
      if (typeof populateTowns === "function") populateTowns($("#egTown"), { includeAny: true, anyLabel: "Any town", selected: "TAMPINES" });
      if (typeof populateFlatTypes === "function") populateFlatTypes($("#egType"), { includeAny: true, anyLabel: "Any flat type", selected: "4 Room" });
      form.addEventListener("submit", async ev => {
        ev.preventDefault();
        const out = $("#rwxEngineOut");
        out.innerHTML = `<div class="rwx-muted">Running the same search on both databases…</div>`;
        try {
          const d = await get("/api/engines/search", {
            town: $("#egTown").value, flat_type: $("#egType").value,
            budget: ($("#egBudget").value || "").replace(/[^0-9]/g, ""), min_lease: $("#egLease").value,
          });
          const same = d.same_total && d.same_rows;
          out.innerHTML = `<div><div class="small-kicker">PostgreSQL · ${esc(d.postgres.shape)}</div><strong class="rwx-num">${fmtMs(d.postgres.ms)}</strong><span class="rwx-muted">${d.postgres.total.toLocaleString("en-SG")} matching sales</span></div>
            <div><div class="small-kicker">MongoDB · ${esc(d.mongodb.shape)}</div><strong class="rwx-num">${fmtMs(d.mongodb.ms)}</strong><span class="rwx-muted">${d.mongodb.total.toLocaleString("en-SG")} matching sales</span></div>
            <p class="rwx-note" style="grid-column:1/-1"><span class="rwx-badge ${same ? "pass" : "fail"}">${same ? "identical results" : "results differ"}</span> Same count and the same newest ${d.postgres.ids.length} sales in the same order. MongoDB filter: <code>${esc(JSON.stringify(d.mongodb.filter))}</code>. ${esc(d.note)}</p>`;
        } catch (e) {
          out.innerHTML = `<p class="rwx-note">${esc(e.code === "feature_unavailable" ? "Load the MongoDB copy first: python scripts/load_mongo_transactions.py" : (e.message || "Couldn’t run the comparison."))}</p>`;
        }
      });
    }
  };

  // ------------------------------------------------------------------
  // Auto-wiring for pages that need no page-specific data
  // ------------------------------------------------------------------
  function boot() {
    if ($("#rwxMomentum")) RWF.momentum($("#rwxMomentum"));
    if ($("#rwxMatch")) RWF.matchPage();
    if ($("#rwxAccountExtras")) RWF.account();
    if ($("#rwxValidation") && !$("#rwxBench")) RWF.about();
    if ($("#rwxBench")) RWF.benchmarkPage();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
