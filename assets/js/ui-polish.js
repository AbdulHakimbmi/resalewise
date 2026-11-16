/*
  ResaleWise SG — homepage UI behaviour
  Rotating hero town name, scroll reveals, count-up stats, sticky nav,
  hero search, mobile menu, theme toggle and the scrollytelling panel.
  Honours prefers-reduced-motion.
*/
(function () {
  "use strict";

  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Count-up helper (fires when the number scrolls into view); the live band and the nearby stats share it.
  function animateNumber(el, to, fmt) {
    var done = false;
    function run() {
      if (done) return; done = true;
      if (reduceMotion) { el.textContent = fmt(to); return; }
      var start = null, DUR = 1200;
      function frame(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / DUR, 1);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = fmt(to * eased);
        if (p < 1) requestAnimationFrame(frame); else el.textContent = fmt(to);
      }
      requestAnimationFrame(frame);
    }
    if (!("IntersectionObserver" in window)) { run(); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) { run(); io.disconnect(); } });
    }, { threshold: 0.4 });
    io.observe(el);
  }

  // Full town list (matches the coverflow + DB). Title-cased for display.
  var TOWNS = [
    "Ang Mo Kio", "Bedok", "Bishan", "Bukit Batok", "Bukit Merah",
    "Bukit Panjang", "Bukit Timah", "Central Area", "Choa Chu Kang",
    "Clementi", "Geylang", "Hougang", "Jurong East", "Jurong West",
    "Kallang/Whampoa", "Marine Parade", "Pasir Ris", "Punggol",
    "Queenstown", "Sembawang", "Sengkang", "Serangoon", "Tampines",
    "Toa Payoh", "Woodlands", "Yishun"
  ];

  /* ---------- 1. Rotating town name in the hero ---------- */
  (function rotatingTown() {
    var el = document.querySelector("[data-town-rotator]");
    if (!el) return;

    // A curated order that reads nicely; falls back to full list.
    var order = [
      "Tampines", "Punggol", "Bedok", "Sengkang", "Queenstown",
      "Ang Mo Kio", "Woodlands", "Bishan", "Clementi", "Toa Payoh"
    ].filter(function (t) { return TOWNS.indexOf(t) !== -1; });
    if (!order.length) order = TOWNS.slice();

    var i = 0;
    el.textContent = order[0];

    if (reduceMotion) return; // show a single static town, no cycling

    el.dataset.next = order[1]; // so home.js can preload the next photo
    var HOLD = 4500; // ms each town stays visible
    function next() {
      if (document.hidden) return;
      el.classList.add("is-swapping"); // fade/slide out (CSS)
      setTimeout(function () {
        i = (i + 1) % order.length;
        el.textContent = order[i];
        el.dataset.next = order[(i + 1) % order.length];
        el.classList.remove("is-swapping"); // fade/slide back in
        // home.js swaps the hero photo on this
        document.dispatchEvent(new CustomEvent("rw:town", { detail: order[i] }));
      }, 420); // must match CSS transition duration
    }
    setInterval(next, HOLD);
  })();

  /* ---------- 2. Scroll-reveal ---------- */
  (function scrollReveal() {
    var items = [].slice.call(document.querySelectorAll("[data-reveal]"));
    if (!items.length) return;

    if (reduceMotion || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("reveal-in"); });
      return;
    }

    items.forEach(function (el) { el.classList.add("reveal-init"); });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("reveal-in");
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.14, rootMargin: "0px 0px -8% 0px" });

    items.forEach(function (el) { io.observe(el); });
  })();

  /* ---------- 3. Count-up numbers ---------- */
  (function countUp() {
    var nums = [].slice.call(document.querySelectorAll("[data-count-to]"));
    if (!nums.length) return;

    function format(value, el) {
      var decimals = parseInt(el.getAttribute("data-count-decimals") || "0", 10);
      var group = el.getAttribute("data-count-group") === "1";
      var prefix = el.getAttribute("data-count-prefix") || "";
      var suffix = el.getAttribute("data-count-suffix") || "";
      var fixed = value.toFixed(decimals);
      if (group) {
        var parts = fixed.split(".");
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        fixed = parts.join(".");
      }
      // Preserve a proper minus sign to match the original design (−).
      fixed = fixed.replace(/^-/, "\u2212");
      return prefix + fixed + suffix;
    }

    function run(el) {
      var to = parseFloat(el.getAttribute("data-count-to"));
      if (isNaN(to)) return;
      if (reduceMotion) { el.textContent = format(to, el); return; }

      var DURATION = 1100;
      var start = null;
      function frame(ts) {
        if (start === null) start = ts;
        var p = Math.min((ts - start) / DURATION, 1);
        // easeOutCubic
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = format(to * eased, el);
        if (p < 1) requestAnimationFrame(frame);
        else el.textContent = format(to, el);
      }
      requestAnimationFrame(frame);
    }

    if (!("IntersectionObserver" in window)) {
      nums.forEach(run);
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          run(entry.target);
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0.5 });
    nums.forEach(function (el) { io.observe(el); });
  })();

  /* ---------- 4. Sticky / condensing nav ---------- */
  (function stickyNav() {
    var nav = document.querySelector(".nav");
    if (!nav) return;
    var ticking = false;
    function update() {
      nav.classList.toggle("nav-scrolled", window.scrollY > 24);
      ticking = false;
    }
    window.addEventListener("scroll", function () {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  })();

  /* ---------- 5. Hero search form ---------- */
  (function heroSearch() {
    var form = document.getElementById("heroSearch");
    if (!form) return;

    var townSel = form.querySelector("#heroTown");

    // Fill immediately with the built-in full list so the dropdown is never
    // empty even before/without the API...
    if (townSel) {
      var current = townSel.value;
      townSel.innerHTML = '<option value="">Any town</option>';
      TOWNS.forEach(function (t) {
        var o = document.createElement("option");
        o.textContent = t;
        townSel.appendChild(o);
      });
      townSel.value = current;
    }

    // ...then, if api.js is present, refresh from the live DB via the shared
    // helper so the homepage stays consistent with every other page.
    if (typeof populateTowns === "function") {
      populateTowns(townSel, { includeAny: true, anyLabel: "Any town" });
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var params = new URLSearchParams();

      var town = (form.querySelector("#heroTown") || {}).value || "";
      var type = (form.querySelector("#heroType") || {}).value || "";
      var budgetRaw = (form.querySelector("#heroBudget") || {}).value || "";
      var lease = (form.querySelector("#heroLease") || {}).value || "";
      var area = (form.querySelector("#heroArea") || {}).value || "";

      // Strip commas / spaces / $ from the budget so "600,000" works.
      var budget = budgetRaw.replace(/[^0-9.]/g, "");

      if (town) params.set("town", town);
      if (type) params.set("flat_type", type);
      if (budget) params.set("budget", budget);
      if (lease) params.set("min_lease", lease);
      if (area) params.set("min_area", area);

      var qs = params.toString();
      window.location.href = "search.html" + (qs ? "?" + qs : "");
    });
  })();

  /* ---------- 6. Mobile menu toggle ---------- */
  (function mobileMenu() {
    var btn = document.querySelector("[data-menu-toggle]");
    var links = document.querySelector(".nav-links");
    if (!btn || !links) return;
    btn.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  })();

  /* ---------- 6b. Light / dark theme toggle (sun & moon) ---------- */
  (function themeToggle() {
    var SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
    var MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

    function apply(theme) {
      if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
      else document.documentElement.removeAttribute("data-theme");
      // Keep the browser UI colour (mobile address bar) in step with the theme.
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute("content", theme === "light" ? "#f4f2ec" : "#0e1013");
    }
    // Applied ASAP (also set very early below to avoid a flash).
    var saved = localStorage.getItem("rw_theme") || "dark";
    apply(saved);

    function mountButton() {
      var nav = document.querySelector(".nav");
      if (!nav || document.getElementById("themeToggle")) return;
      var btn = document.createElement("button");
      btn.id = "themeToggle";
      btn.className = "theme-toggle";
      btn.type = "button";
      btn.setAttribute("aria-label", "Toggle light or dark mode");
      function paint() {
        var t = localStorage.getItem("rw_theme") || "dark";
        btn.innerHTML = t === "light" ? MOON : SUN;   // show the icon you'd switch TO
        btn.title = t === "light" ? "Switch to dark mode" : "Switch to light mode";
      }
      btn.addEventListener("click", function () {
        var t = (localStorage.getItem("rw_theme") || "dark") === "light" ? "dark" : "light";
        localStorage.setItem("rw_theme", t);
        apply(t);
        paint();
      });
      // Insert just before the mobile menu button (a direct nav child). The
      // nav layout (below) uses margin-left:auto on .nav-links to keep the
      // right-hand cluster tight, so this won't create a space-between gap.
      var menu = nav.querySelector("[data-menu-toggle]");
      if (menu) nav.insertBefore(btn, menu); else nav.appendChild(btn);
      paint();
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mountButton);
    } else { mountButton(); }
  })();

  /* ---------- 7. Scroll progress bar ---------- */
  (function scrollProgress() {
    var bar = document.createElement("div");
    bar.className = "scroll-progress";
    document.body.appendChild(bar);
    var ticking = false;
    function update() {
      var h = document.documentElement;
      var max = h.scrollHeight - h.clientHeight;
      var p = max > 0 ? (h.scrollTop || window.scrollY) / max : 0;
      bar.style.transform = "scaleX(" + Math.min(Math.max(p, 0), 1) + ")";
      ticking = false;
    }
    window.addEventListener("scroll", function () {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  })();

  /* ---------- 8. Scrollytelling: steps drive the sticky visual ---------- */
  (function scrolly() {
    var steps = [].slice.call(document.querySelectorAll(".scrolly-step"));
    var visuals = [].slice.call(document.querySelectorAll(".scrolly-visual"));
    if (!steps.length || !visuals.length) return;

    function activate(idx) {
      steps.forEach(function (s, i) { s.classList.toggle("active", i === idx); });
      visuals.forEach(function (v, i) { v.classList.toggle("active", i === idx); });
    }
    activate(0);

    if (!("IntersectionObserver" in window)) { return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          var idx = parseInt(e.target.getAttribute("data-step"), 10) || 0;
          activate(idx);
        }
      });
    }, { rootMargin: "-45% 0px -45% 0px", threshold: 0 });
    steps.forEach(function (s) { io.observe(s); });
  })();

  /* ---------- 8b. Live mini-search inside the scrollytelling panel ---------- */
  (function scrollySearch() {
    var input = document.getElementById("scrollySearch");
    var menu  = document.getElementById("scrollyMenu");
    var go    = document.getElementById("scrollyGo");
    if (!input || !menu) return;

    function toTitle(s) {
      return String(s).toLowerCase().replace(/\b\w/g, function (c) { return c.toUpperCase(); });
    }
    var towns = TOWNS.slice();      // built-in full list (also refreshed below)
    var active = -1;

    // Refresh from the live DB if reachable, so it always matches search.html.
    // (API_BASE comes from api.js, which is loaded before this file.)
    if (window.fetch) {
      fetch(API_BASE + "/api/options")
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.towns && d.towns.length) towns = d.towns.map(toTitle); })
        .catch(function () {});
    }

    function matches() {
      var qv = input.value.trim().toLowerCase();
      var base = qv ? towns.filter(function (n) { return n.toLowerCase().indexOf(qv) !== -1; }) : towns;
      return base.slice(0, 6);
    }
    function open(items) {
      if (!items.length) { close(); return; }
      menu.innerHTML = items.map(function (n, i) {
        return '<li role="option" class="combo-item' + (i === active ? ' active' : '') + '" data-name="' + n + '">' + n + '</li>';
      }).join("");
      menu.hidden = false;
    }
    function close() { menu.hidden = true; menu.innerHTML = ""; active = -1; }
    function go2(town) {
      var t = (town || input.value || "").trim();
      var qs = t ? ("?town=" + encodeURIComponent(t.toUpperCase())) : "";
      window.location.href = "search.html" + qs;
    }

    input.addEventListener("input", function () { active = -1; open(matches()); });
    input.addEventListener("focus", function () { open(matches()); });
    input.addEventListener("keydown", function (e) {
      var items = matches();
      if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); open(items); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); open(items); }
      else if (e.key === "Enter") {
        e.preventDefault();
        if (active >= 0 && items[active]) go2(items[active]);
        else go2();   // free-text or empty → search page
      } else if (e.key === "Escape") { close(); }
    });
    menu.addEventListener("mousedown", function (e) {
      var li = e.target.closest(".combo-item");
      if (li) { e.preventDefault(); go2(li.getAttribute("data-name")); }
    });
    if (go) go.addEventListener("click", function () { go2(); });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".sv-search-live")) close();
    });
  })();

  /* ---------- 9. Subtle hero parallax ---------- */
  (function heroParallax() {
    var art = document.querySelector(".home-hero .art");
    if (!art || reduceMotion) return;
    var ticking = false;
    function update() {
      var y = window.scrollY || 0;
      // move the art slightly slower than scroll for depth
      art.style.transform = "translateY(" + (y * 0.06) + "px)";
      ticking = false;
    }
    window.addEventListener("scroll", function () {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  })();

  /* ---------- 9b. What's around: live counts and an example block, from the amenities API ---------- */
  (function nearbyHome() {
    var step = document.getElementById("nearStep");
    if (!step || !window.fetch) return;                      // only on the homepage
    var visual = document.getElementById("nearVisual");
    var hide = function () {                                 // no nearby data loaded: leave the step out rather than show nothing
      if (step) step.hidden = true;
      if (visual) visual.hidden = true;
    };
    var fmt = function (m) { return m < 1000 ? Math.round(m) + " m" : (m / 1000).toFixed(2).replace(/0$/, "") + " km"; };
    var title = function (s) { return String(s || "").toLowerCase().replace(/\b[a-z]/g, function (c) { return c.toUpperCase(); }); };

    fetch(API_BASE + "/api/amenities/status")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.available || !d.amenities) { hide(); return null; }
        var total = Object.keys(d.amenities).reduce(function (n, k) { return n + d.amenities[k]; }, 0);
        var placesEl = document.getElementById("lsPlaces"), blocksEl = document.getElementById("lsBlocks");
        if (placesEl && total) {
          document.getElementById("lsPlacesStat").hidden = false;
          animateNumber(placesEl, total, function (v) { return Math.round(v).toLocaleString("en-SG"); });
        }
        if (blocksEl && d.blocks_with_features) {
          document.getElementById("lsBlocksStat").hidden = false;
          animateNumber(blocksEl, d.blocks_with_features, function (v) { return Math.round(v).toLocaleString("en-SG"); });
        }
        return fetch(API_BASE + "/api/amenities/rank?flat_type=4+ROOM&months=24&limit=1").then(function (r) { return r.ok ? r.json() : null; });
      })
      .then(function (d) {
        if (d === null || d === undefined) return;
        var b = d.available && d.results && d.results[0];
        if (!b) { hide(); return; }
        var scale = d.scale || {};
        document.getElementById("nearTitle").textContent = "Blk " + b.block_no + " " + title(b.street_name) + " · " + title(b.town_name);
        var order = ["mrt", "park", "hawker", "gym", "childcare", "primary_school", "secondary_school"];
        document.getElementById("nearList").innerHTML = order.filter(function (k) { return b.nearest_m && b.nearest_m[k] != null; }).map(function (k) {
          var label = k === "mrt" ? "MRT/LRT station" : (scale[k] && scale[k].label) || k;
          return "<li><span>" + label + "</span><b>" + fmt(b.nearest_m[k]) + "</b></li>";
        }).join("");
      })
      .catch(hide);
  })();

  /* ---------- 10. Live data band (real numbers from the API) ---------- */
  (function liveBand() {
    var recordsEl = document.getElementById("lsRecords");
    if (!recordsEl) return;   // only on the homepage
    var townsEl = document.getElementById("lsTowns");
    var medianEl = document.getElementById("lsMedian");
    var typesEl = document.getElementById("lsTypes");

    // Static, known figure for records (avoids a heavy count query).
    animateNumber(recordsEl, 985219, function (v) {
      return Math.round(v).toLocaleString("en-SG");
    });

    if (!window.fetch) return;

    fetch(API_BASE + "/api/options")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (townsEl && d.towns) animateNumber(townsEl, d.towns.length, function (v) { return Math.round(v); });
        if (typesEl && d.flat_types) animateNumber(typesEl, d.flat_types.length, function (v) { return Math.round(v); });
      })
      .catch(function () {});

    fetch(API_BASE + "/api/insights/summary?flat_type=4%20ROOM&months=24")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.summary && d.summary.median_price && medianEl) {
          animateNumber(medianEl, d.summary.median_price, function (v) {
            return "$" + Math.round(v / 1000) + "k";
          });
        } else if (medianEl) { medianEl.textContent = "—"; }
      })
      .catch(function () { if (medianEl) medianEl.textContent = "—"; });
  })();

})();
