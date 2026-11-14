/*
  ResaleWise SG — homepage only: hero photo crossfade and the live price check.
  Reveals, the town rotator, hero search, scrollytelling and the live band
  stay in ui-polish.js. Load after api.js.
*/
(function () {
  "use strict";

  var still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return [].slice.call((el || document).querySelectorAll(s)); };
  var money = function (n) { return "$" + Math.round(n).toLocaleString("en-SG"); };
  var month = function (iso) { return RW.formatMonth(iso); };

  /* ---------- hero: slow crossfade between three estates ---------- */
  (function crossfade() {
    var fade = $("[data-fade]");
    if (!fade || still) return;
    var imgs = $$("img", fade), cap = $("[data-fade-caption]", fade);
    var names = ["Tampines", "Tanglin Halt, Queenstown", "Toa Payoh"];
    var at = 0;
    setInterval(function () {
      if (document.hidden) return;
      imgs[at].classList.remove("is-on");
      at = (at + 1) % imgs.length;
      imgs[at].classList.add("is-on");
      cap.textContent = names[at];
    }, 6500);
  })();

  /* ---------- the live check: every window is one sale, cheapest on the ground floor ---------- */
  var form = $("[data-check-form]");
  if (!form) return;
  var face = $("[data-face]"), block = $("[data-block]"), line = $("[data-line]");
  var result = $("[data-result]"), near = $("[data-near]"), hover = $("[data-hover]");
  var COLS = 10;
  var rows = [];

  function parsePrice(v) { return Number(String(v).replace(/[^0-9]/g, "")); }

  function describe(r) {
    var st = String(r.storey_low).padStart(2, "0") + " to " + String(r.storey_high).padStart(2, "0");
    return "Blk " + r.block_no + " " + titleCase(r.street_name) + " · storey " + st + " · " +
      Math.round(r.floor_area_sqm) + " sqm · " + money(r.resale_price) + " · " + month(r.month);
  }

  function draw(price) {
    var n = rows.length, levels = Math.ceil(n / COLS);
    var below = rows.filter(function (r) { return r.resale_price < price; }).length;
    face.innerHTML = "";
    rows.forEach(function (r, i) {
      var w = document.createElement("span");
      w.style.gridRow = levels - Math.floor(i / COLS);
      w.style.gridColumn = (i % COLS) + 1;
      if (r.resale_price < price) w.className = "below";
      w.dataset.i = i;
      if (!still) { w.classList.add("enter"); w.style.transitionDelay = (i * 4) + "ms"; }
      face.appendChild(w);
    });
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        $$("span", face).forEach(function (w) { w.classList.remove("enter"); });
        setTimeout(function () { $$("span", face).forEach(function (w) { w.style.transitionDelay = ""; }); }, 900);
      });
    });

    // the pink line sits on top of the last cheaper window's floor
    var b = block.getBoundingClientRect(), f = face.getBoundingClientRect(), bottom;
    if (below === 0) bottom = b.bottom - f.bottom;
    else if (below === n) bottom = b.bottom - f.top - 6;
    else bottom = b.bottom - face.children[below - 1].getBoundingClientRect().top + 1;
    line.hidden = false;
    line.style.bottom = bottom + "px";
    $("[data-line-label]").textContent = money(price);

    var prices = rows.map(function (r) { return r.resale_price; });
    var median = n % 2 ? prices[(n - 1) / 2] : (prices[n / 2 - 1] + prices[n / 2]) / 2;
    var months = rows.map(function (r) { return r.month; }).sort();
    var share = below / n;
    var verdict = share < 0.25 ? "That is cheaper than three in four of them."
      : share > 0.75 ? "That is dearer than three in four of them."
      : "That sits in the middle half of recent sales.";
    var type = form.flat_type.value.toLowerCase(), town = form.town.value;
    result.innerHTML = "<strong>" + money(price) + "</strong> is more than <strong>" + below + " of the last " + n +
      "</strong> " + escapeHtml(type) + " sales in " + escapeHtml(town) + " (" + month(months[0]) + " to " + month(months[n - 1]) +
      "). Their median was <strong>" + money(median) + "</strong>. " + verdict;

    near.innerHTML = "";
    rows.slice().sort(function (a, c) { return Math.abs(a.resale_price - price) - Math.abs(c.resale_price - price); })
      .slice(0, 3).forEach(function (r) {
        var li = document.createElement("li");
        li.innerHTML = '<a href="details.html?id=' + Number(r.txn_id) + '"><span>Blk ' + escapeHtml(r.block_no) + " " +
          escapeHtml(titleCase(r.street_name)) + ", " + month(r.month) + "</span><b>" + money(r.resale_price) + "</b></a>";
        near.appendChild(li);
      });
  }

  function check() {
    var price = parsePrice(form.budget.value);
    if (!price) { result.textContent = "Type the asking price in dollars, for example 650,000."; form.budget.focus(); return; }
    form.budget.value = price.toLocaleString("en-SG");
    result.textContent = "Loading recent " + form.flat_type.value.toLowerCase() + " sales in " + form.town.value + "…";
    apiGet("/api/search", { town: form.town.value, flat_type: form.flat_type.value, limit: 120 })
      .then(function (data) {
        rows = (data.rows || []).sort(function (a, c) { return a.resale_price - c.resale_price; });
        if (!rows.length) {
          face.innerHTML = ""; line.hidden = true; near.innerHTML = "";
          result.textContent = "No " + form.flat_type.value.toLowerCase() + " sales are recorded in " + form.town.value + ". Try another flat type.";
          return;
        }
        draw(price);
      })
      .catch(function (err) {
        var d = RW.describeError(err, "recent sales");
        result.innerHTML = escapeHtml(d.title + ". " + d.text) + ' You can still browse past sales on the <a class="link" href="search.html">search page</a>.';
      });
  }

  form.addEventListener("submit", function (e) { e.preventDefault(); check(); });
  face.addEventListener("pointerover", function (e) {
    var i = e.target.dataset && e.target.dataset.i;
    if (i != null) hover.textContent = describe(rows[i]);
  });
  var t;
  window.addEventListener("resize", function () {
    clearTimeout(t);
    t = setTimeout(function () { if (rows.length) draw(parsePrice(form.budget.value)); }, 200);
  });

  check();
})();
