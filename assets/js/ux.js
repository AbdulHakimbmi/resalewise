/*
  ResaleWise SG — shared UX helpers (load after api.js)

    RW.skeleton.*        shimmer placeholders so a page never looks "broken" while data loads
    RW.renderEmpty()     a useful "nothing here" state, with next-step buttons
    RW.renderError()     a friendly inline failure with a Retry button (never a dead end)
    RW.url.sync()        keep the address bar in step with page state (shareable links)
    RW.sparkline()       tiny inline-SVG trend line
    RW.downloadCsv()     client-side CSV download
    footer data line     "Data: HDB Resale Flat Prices, data.gov.sg — transactions up to Sep 2026"
*/
(function () {
  "use strict";

  var RW = (window.RW = window.RW || {});
  var esc = window.escapeHtml;

  /* ------------------------------------------------------------------ */
  /*  Loading skeletons                                                  */
  /* ------------------------------------------------------------------ */
  function line(width, cls) {
    return '<span class="skel skel-line' + (cls ? " " + cls : "") + '" style="width:' + width + '"></span>';
  }
  function repeat(str, n) { return new Array(n + 1).join(str); }

  RW.skeleton = {
    // Same grid as a real result card, so nothing jumps when data arrives.
    resultCards: function (n) {
      var one =
        '<article class="card transaction skel-card" aria-hidden="true">' +
          '<div>' + line("38%", "sm") + line("72%", "title") + line("88%") + '<span class="skel skel-pill"></span></div>' +
          '<div class="details">' + repeat('<div>' + line("64px", "lg") + line("52px", "sm") + '</div>', 3) + '</div>' +
          '<div class="price"><span class="skel skel-price"></span><span class="skel skel-btn"></span></div>' +
        '</article>';
      return repeat(one, n || 5);
    },
    reviews: function (n) {
      var one =
        '<div class="review-card skel-card" aria-hidden="true">' +
          '<span class="skel skel-avatar"></span>' +
          '<div class="rc-body" style="flex:1">' + line("42%") + line("22%", "sm") + line("96%") + line("80%") + '</div>' +
        '</div>';
      return repeat(one, n || 3);
    },
    // A row of bars of varying height — stands in for a bar chart / histogram.
    bars: function (n) {
      var heights = [38, 62, 80, 55, 92, 70, 48, 66, 34, 58, 76, 44];
      var out = "";
      for (var i = 0; i < (n || 12); i++) {
        out += '<span class="skel skel-bar" style="height:' + heights[i % heights.length] + '%"></span>';
      }
      return '<div class="skel-bars" aria-hidden="true">' + out + '</div>';
    },
    // Horizontal rows — stands in for a ranked list / bar rows.
    rows: function (n) {
      var widths = ["92%", "78%", "70%", "58%", "46%", "38%"];
      var out = "";
      for (var i = 0; i < (n || 6); i++) {
        out += '<div class="skel-row" aria-hidden="true">' + line("22%") + line(widths[i % widths.length], "bar") + line("12%") + '</div>';
      }
      return out;
    },
    block: function (height) {
      return '<span class="skel" aria-hidden="true" style="display:block;width:100%;height:' + (height || 120) + 'px"></span>';
    },
    metric: function () { return '<span class="skel skel-metric" aria-hidden="true"></span>'; },
    // Visually-hidden text so screen readers hear "Loading…" for the region.
    label: function (text) { return '<span class="visually-hidden" role="status">' + esc(text || "Loading…") + '</span>'; },
  };

  // Mark a region as busy for assistive tech while it loads.
  RW.setBusy = function (el, busy) {
    if (el) el.setAttribute("aria-busy", busy ? "true" : "false");
  };

  /* ------------------------------------------------------------------ */
  /*  Empty + error states                                               */
  /* ------------------------------------------------------------------ */
  function actionsHtml(actions) {
    if (!actions || !actions.length) return "";
    return '<div class="sb-actions">' + actions.map(function (a, i) {
      var cls = a.primary ? "primary-btn" : "ghost-btn";
      if (a.href) return '<a class="' + cls + '" href="' + esc(a.href) + '">' + esc(a.label) + '</a>';
      return '<button type="button" class="' + cls + '" data-sb-action="' + i + '">' + esc(a.label) + '</button>';
    }).join("") + '</div>';
  }
  function bindActions(el, actions) {
    [].forEach.call(el.querySelectorAll("[data-sb-action]"), function (btn) {
      btn.addEventListener("click", function () { actions[Number(btn.getAttribute("data-sb-action"))].onClick(btn); });
    });
  }

  /*
    RW.renderEmpty(el, { icon, title, text, hints:[...], actions:[{label, onClick|href, primary}], compact })
    All text is escaped for you.
  */
  RW.renderEmpty = function (el, o) {
    if (!el) return;
    var hints = (o.hints && o.hints.length)
      ? '<ul class="sb-hints">' + o.hints.map(function (h) { return '<li>' + esc(h) + '</li>'; }).join("") + '</ul>'
      : "";
    el.innerHTML =
      '<div class="state-block is-empty' + (o.compact ? " compact" : "") + '" role="status">' +
        '<div class="sb-ico" aria-hidden="true">' + (o.icon || "🔎") + '</div>' +
        '<h4>' + esc(o.title) + '</h4>' +
        (o.text ? '<p>' + esc(o.text) + '</p>' : "") + hints +
        actionsHtml(o.actions) +
      '</div>';
    bindActions(el, o.actions || []);
  };

  // Turn an ApiError (or anything thrown) into a title + sentence for people.
  RW.describeError = function (err, what) {
    var code = err && err.code, status = err && err.status;
    var subject = what || "this";
    var local = typeof API_BASE !== "undefined" && /localhost|127\.0\.0\.1/.test(API_BASE);

    if (code === "network_error") {
      return {
        title: "Can’t reach the server",
        text: "Check your internet connection and try again." +
              (local ? " Running locally? Make sure the API is started (python app.py)." : "")
      };
    }
    if (code === "timeout") {
      return { title: "That’s taking too long",
               text: "The server didn’t answer in time. It may be waking up — give it a few seconds and try again." };
    }
    if (code === "rate_limited" || status === 429) {
      var wait = err.retryAfter ? " Please wait about " + err.retryAfter + " seconds and try again." : " Please wait a moment and try again.";
      return { title: "Too many requests", text: "You’re going a little fast." + wait };
    }
    if (code === "db_unavailable" || status === 503) {
      return { title: "Data is temporarily unavailable",
               text: "Our database isn’t responding right now. Please try again in a moment." };
    }
    if (status === 404 || code === "not_found") {
      return { title: "We couldn’t find " + subject, text: (err && err.message) || "It may have been removed." };
    }
    if (status === 400 || code === "invalid_parameter" || code === "validation_error") {
      return { title: "Something about that request isn’t right", text: (err && err.message) || "Please check the filters and try again." };
    }
    if (status >= 500) {
      return { title: "Something went wrong on our side", text: "It’s not you. Please try again in a moment." };
    }
    return { title: "Couldn’t load " + subject, text: (err && err.message) || "Please try again." };
  };

  /*
    RW.renderError(el, err, { what:"results", onRetry:fn, compact:true, actions:[...] })
    Always offers a way forward: a Retry button when onRetry is given.
  */
  RW.renderError = function (el, err, o) {
    if (!el) return;
    o = o || {};
    if (window.console && console.warn) console.warn("[ResaleWise]", err);
    var d = RW.describeError(err, o.what);
    var actions = [];
    if (o.onRetry) {
      actions.push({ label: "Try again", primary: true, onClick: function (btn) {
        btn.disabled = true; btn.textContent = "Retrying…";
        o.onRetry();
      } });
    }
    (o.actions || []).forEach(function (a) { actions.push(a); });

    var detail = err && (err.code || err.status)
      ? '<span class="sb-code">' + esc([err.code, err.status ? "HTTP " + err.status : ""].filter(Boolean).join(" · ")) + '</span>'
      : "";
    el.innerHTML =
      '<div class="state-block is-error' + (o.compact ? " compact" : "") + '" role="alert">' +
        '<h4>' + esc(d.title) + '</h4>' +
        '<p>' + esc(d.text) + '</p>' +
        actionsHtml(actions) + detail +
      '</div>';
    bindActions(el, actions);
  };

  /* ------------------------------------------------------------------ */
  /*  URL state (shareable links)                                        */
  /* ------------------------------------------------------------------ */
  RW.url = {
    params: function () { return new URLSearchParams(location.search); },

    // Make the address bar reflect `state` (empty values are dropped). Uses
    // pushState so Back/Forward step through searches; pass {replace:true}
    // for the initial load. Returns true if the URL actually changed.
    sync: function (state, opts) {
      var p = new URLSearchParams();
      Object.keys(state).forEach(function (k) {
        var v = state[k];
        if (v !== "" && v != null && v !== false) p.set(k, v);
      });
      var qs = p.toString();
      var next = location.pathname + (qs ? "?" + qs : "") + location.hash;
      var cur = location.pathname + location.search + location.hash;
      if (next === cur) return false;
      try {
        history[opts && opts.replace ? "replaceState" : "pushState"](null, "", next);
      } catch (e) { /* e.g. file:// pages — the page still works, it just isn't shareable */ }
      return true;
    },

    onChange: function (fn) { window.addEventListener("popstate", fn); },
  };

  /* ------------------------------------------------------------------ */
  /*  Pager:  ‹ Previous   1 … 4 5 [6] 7 8 … 500   Next ›                */
  /* ------------------------------------------------------------------ */
  // Draws the page buttons into `el` (a <nav>). o = { page, pages, onPage(n) }.
  // Always shows the first page, the last page and two pages either side of
  // the current one, with "…" for the gaps, so 500 pages still fit one row.
  // On phones the CSS swaps the numbers for "Page 6 of 500".
  RW.pager = function (el, o) {
    if (!el) return;
    var page = o.page, pages = o.pages;
    if (!pages || pages < 2) { el.innerHTML = ""; el.hidden = true; return; }
    el.hidden = false;
    var nf = function (n) { return Number(n).toLocaleString("en-SG"); };
    var want = {};
    want[1] = 1; want[pages] = 1;
    for (var i = page - 2; i <= page + 2; i++) if (i >= 1 && i <= pages) want[i] = 1;
    var list = Object.keys(want).map(Number).sort(function (a, b) { return a - b; });
    var html = '<button type="button" class="pg pg-step" data-page="' + (page - 1) + '"' +
      (page <= 1 ? " disabled" : "") + ' aria-label="Previous page">&lsaquo; Previous</button>' +
      '<span class="pg-status">Page ' + nf(page) + " of " + nf(pages) + "</span>";
    var last = 0;
    list.forEach(function (n) {
      if (last && n - last > 1) html += '<span class="pg-gap" aria-hidden="true">&hellip;</span>';
      html += n === page
        ? '<button type="button" class="pg pg-num is-current" data-page="' + n + '" aria-current="page" aria-label="Page ' + nf(n) + ', current page">' + nf(n) + "</button>"
        : '<button type="button" class="pg pg-num" data-page="' + n + '" aria-label="Page ' + nf(n) + '">' + nf(n) + "</button>";
      last = n;
    });
    html += '<button type="button" class="pg pg-step" data-page="' + (page + 1) + '"' +
      (page >= pages ? " disabled" : "") + ' aria-label="Next page">Next &rsaquo;</button>';
    el.innerHTML = html;
    el.onclick = function (e) {
      var b = e.target.closest ? e.target.closest("button[data-page]") : null;
      if (!b || b.disabled) return;
      var n = Number(b.getAttribute("data-page"));
      if (n >= 1 && n <= pages && n !== page) o.onPage(n);
    };
  };

  /* ------------------------------------------------------------------ */
  /*  Small utilities                                                    */
  /* ------------------------------------------------------------------ */
  // "2026-09-01" -> "Sep 2026". Parsed from the string, never via Date: a
  // date-only string is read as UTC, which shows the PREVIOUS month to anyone
  // west of Greenwich.
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  RW.formatMonth = function (iso, long) {
    var m = /^(\d{4})-(\d{2})/.exec(String(iso));
    if (!m || +m[2] < 1 || +m[2] > 12) return "";
    return (long ? MONTHS_LONG : MONTHS)[+m[2] - 1] + " " + m[1];
  };
  // "2025-07-01" -> "Q3 2025"
  RW.formatQuarter = function (iso) {
    var m = /^(\d{4})-(\d{2})/.exec(String(iso));
    return m ? "Q" + (Math.floor((+m[2] - 1) / 3) + 1) + " " + m[1] : "";
  };

  // Client-side CSV download. rows = array of arrays. Opens cleanly in Excel (UTF-8 BOM)
  // and neutralises spreadsheet-formula injection (cells starting = + - @).
  RW.downloadCsv = function (filename, rows) {
    function cell(v) {
      var s = v == null ? "" : String(v);
      if (/^[=+\-@]/.test(s)) s = "'" + s;
      return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    var csv = rows.map(function (r) { return r.map(cell).join(","); }).join("\r\n");
    var blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  };

  /* ------------------------------------------------------------------ */
  /*  Sparkline — tiny inline SVG of a median-price series               */
  /* ------------------------------------------------------------------ */
  // series: [{quarter, median_price}, ...] oldest -> newest. Returns an HTML
  // string ("" when there isn't enough data to draw a line).
  RW.sparkline = function (series, label) {
    if (!series || series.length < 2) return "";
    var W = 96, H = 28, pad = 3;
    var vals = series.map(function (p) { return p.median_price; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), span = Math.max(hi - lo, 1);
    var pts = vals.map(function (v, i) {
      return [pad + i * (W - 2 * pad) / (vals.length - 1), pad + (1 - (v - lo) / span) * (H - 2 * pad)];
    });
    var path = pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" ");
    var area = path + " L" + pts[pts.length - 1][0].toFixed(1) + " " + (H - pad) + " L" + pts[0][0].toFixed(1) + " " + (H - pad) + " Z";
    var last = pts[pts.length - 1];
    var pct = (vals[vals.length - 1] - vals[0]) / vals[0] * 100;
    var up = pct >= 0;
    var years = Math.max(1, Math.round(series.length / 4));
    var aria = (label ? label + ": " : "Median price: ") + (up ? "up " : "down ") + Math.abs(pct).toFixed(1) + "% over about " + years + (years === 1 ? " year" : " years");
    return '<div class="spark-wrap">' +
      '<svg class="spark" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="' + esc(aria) + '">' +
        '<path class="spark-area" d="' + area + '"/><path class="spark-line" d="' + path + '"/>' +
        '<circle class="spark-dot" cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2.4"/>' +
      '</svg>' +
      '<span class="spark-delta ' + (up ? "up" : "down") + '" aria-hidden="true">' + (up ? "▲ " : "▼ ") + Math.abs(pct).toFixed(1) + '%</span>' +
      '<span class="spark-cap" aria-hidden="true">' + years + 'y town median</span>' +
    '</div>';
  };

  /* ------------------------------------------------------------------ */
  /*  Footer: data source + freshness                                    */
  /* ------------------------------------------------------------------ */
  var META_KEY = "rw_meta_v1", META_TTL = 60 * 60 * 1000;

  RW.loadMeta = function () {
    try {
      var saved = JSON.parse(sessionStorage.getItem(META_KEY) || "null");
      if (saved && Date.now() - saved.t < META_TTL) return Promise.resolve(saved.data);
    } catch (e) { /* storage unavailable */ }
    return apiGet("/api/meta").then(function (data) {
      try { sessionStorage.setItem(META_KEY, JSON.stringify({ t: Date.now(), data: data })); } catch (e) { /* ignore */ }
      return data;
    });
  };

  function fillFreshness() {
    var els = document.querySelectorAll("[data-freshness]");
    if (!els.length || typeof apiGet !== "function") return;
    RW.loadMeta().then(function (m) {
      var when = m && m.latest_month ? RW.formatMonth(m.latest_month) : "";
      if (!when) return;
      [].forEach.call(els, function (el) {
        el.textContent = " — transactions up to " + when;
        el.hidden = false;
      });
    }).catch(function () { /* footer simply stays without a date */ });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fillFreshness);
  else fillFreshness();

  // Town photos live at assets/images/towns/<slug>.jpg (KALLANG/WHAMPOA -> kallang-whampoa).
  // A town without one gets the drawn block (.no-image) instead of a broken picture.
  RW.townSlug = function (town) {
    return String(town).toLowerCase().replace(/\//g, "-").replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "").replace(/-+/g, "-");
  };
  RW.townPhoto = function (figure, town) {
    if (!figure || !town) return;
    var img = figure.querySelector("img"), cap = figure.querySelector("figcaption");
    figure.classList.remove("no-image");
    img.onerror = function () { figure.classList.add("no-image"); };
    img.alt = "HDB blocks in " + titleCase(town);
    img.src = "assets/images/towns/" + RW.townSlug(town) + ".jpg";
    if (cap) cap.textContent = titleCase(town);
  };

  // Stamp printouts with today's date.
  function stampPrintDate() {
    var d = new Date().toLocaleDateString("en-SG", { day: "numeric", month: "long", year: "numeric" });
    [].forEach.call(document.querySelectorAll("[data-print-date]"), function (el) { el.textContent = d; });
  }
  window.addEventListener("beforeprint", stampPrintDate);
})();
