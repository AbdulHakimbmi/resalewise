// ResaleWise SG — shared API helper
const API_BASE = "http://localhost:5000";

async function apiGet(path, params = {}) {
  const cleaned = { ...params };

  // Normalise enum-like params to what the DB stores (UPPERCASE)
  if (cleaned.town)      cleaned.town      = String(cleaned.town).toUpperCase();
  if (cleaned.flat_type) cleaned.flat_type = String(cleaned.flat_type).toUpperCase();

  const clean = Object.fromEntries(
    Object.entries(cleaned).filter(([, v]) =>
      v !== "" && v != null && v !== "ANY TOWN" && v !== "ANY FLAT TYPE"
    )
  );

  const qs = new URLSearchParams(clean).toString();
  const url = API_BASE + path + (qs ? "?" + qs : "");
  const res = await fetch(url);
  if (!res.ok) throw new Error("API " + res.status + " " + res.statusText);
  return res.json();
}

function titleCase(s) {
  return String(s).toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

// Cache /api/options for the page so multiple dropdowns share one request.
let _optionsPromise = null;
function getOptions() {
  if (!_optionsPromise) _optionsPromise = apiGet("/api/options");
  return _optionsPromise;
}

/*
  Fill a <select> with the full town list from the database, so every page
  shows a consistent, complete set of towns. Options are shown Title Cased.

  opts:
    includeAny  - if truthy, prepend an "Any town"/placeholder option
    anyLabel    - text for that first option (default "Any town")
    selected    - value to pre-select (matched case-insensitively)
  Returns a promise. On failure it leaves any existing <option>s in place.
*/
async function populateTowns(select, opts = {}) {
  if (!select) return;
  const { includeAny = true, anyLabel = "Any town", selected = "" } = opts;
  try {
    const data = await getOptions();
    const towns = (data && data.towns) || [];
    if (!towns.length) return;

    select.innerHTML = "";
    if (includeAny) {
      const a = document.createElement("option");
      a.value = "";
      a.textContent = anyLabel;
      select.appendChild(a);
    }
    towns.forEach(t => {
      const o = document.createElement("option");
      o.textContent = titleCase(t);   // display Title Case; api.js re-uppercases on send
      select.appendChild(o);
    });
    if (selected) {
      const want = String(selected).trim().toUpperCase();
      for (const o of select.options) {
        if (o.textContent.trim().toUpperCase() === want) { select.value = o.value; break; }
      }
    }
  } catch (e) {
    console.warn("[populateTowns] could not load towns:", e.message);
  }
}

// Fill a <select> with all flat models from the database (Title Cased).
async function populateFlatModels(select, opts = {}) {
  if (!select) return;
  const { includeAny = true, anyLabel = "Any model", selected = "" } = opts;
  try {
    const data = await getOptions();
    const models = (data && data.flat_models) || [];
    if (!models.length) return;

    select.innerHTML = "";
    if (includeAny) {
      const a = document.createElement("option");
      a.value = "";
      a.textContent = anyLabel;
      select.appendChild(a);
    }
    models.forEach(mn => {
      const o = document.createElement("option");
      o.textContent = titleCase(mn);
      select.appendChild(o);
    });
    if (selected) {
      const want = String(selected).trim().toUpperCase();
      for (const o of select.options) {
        if (o.textContent.trim().toUpperCase() === want) { select.value = o.value; break; }
      }
    }
  } catch (e) {
    console.warn("[populateFlatModels] could not load flat models:", e.message);
  }
}

// Fill a <select> with all flat types from the database (Title Cased).
async function populateFlatTypes(select, opts = {}) {
  if (!select) return;
  const { includeAny = false, anyLabel = "Any flat type", selected = "" } = opts;
  try {
    const data = await getOptions();
    const types = (data && data.flat_types) || [];
    if (!types.length) return;

    select.innerHTML = "";
    if (includeAny) {
      const a = document.createElement("option");
      a.value = "";
      a.textContent = anyLabel;
      select.appendChild(a);
    }
    types.forEach(ft => {
      const o = document.createElement("option");
      o.textContent = titleCase(ft);
      select.appendChild(o);
    });
    if (selected) {
      const want = String(selected).trim().toUpperCase();
      for (const o of select.options) {
        if (o.textContent.trim().toUpperCase() === want) { select.value = o.value; break; }
      }
    }
  } catch (e) {
    console.warn("[populateFlatTypes] could not load flat types:", e.message);
  }
}