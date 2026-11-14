// ResaleWise SG — shared API helper (config.js sets window.RW_API_BASE first)
const API_BASE = (function () {
  if (window.RW_API_BASE) return String(window.RW_API_BASE).replace(/\/+$/, "");
  const h = location.hostname;
  const local = location.protocol === "file:" || h === "localhost" || h === "127.0.0.1" || h === "[::1]";
  return local ? "http://localhost:5000" : "";     // "" = same origin as the page
})();

/*
  Every failed API call throws an ApiError, so pages can show something useful
  instead of a raw "Failed to fetch":
    err.code    - "network_error" | "timeout" | whatever the server sent
                  ("invalid_parameter", "not_found", "rate_limited", "db_unavailable", ...)
    err.status  - HTTP status, or 0 when the server was never reached
    err.message - a sentence that is safe to show to people
    err.retryAfter - seconds, when the server rate-limited us
*/
class ApiError extends Error {
  constructor(message, { status = 0, code = "network_error", retryAfter = null, field = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.field = field;
  }
}

// Shared fetch core: timeout, JSON parsing and the {error, code} contract.
async function apiRequest(path, { method = "GET", body, headers = {}, timeoutMs = 20000 } = {}) {
  // Send the login token when there is one, so endpoints that behave
  // differently for a signed-in user (e.g. "your review" can be edited) know.
  try {
    const t = localStorage.getItem("rw_token");
    if (t && !headers.Authorization) headers = { ...headers, Authorization: "Bearer " + t };
  } catch (_) { /* storage blocked: browse anonymously */ }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new ApiError("The server took too long to respond.", { code: "timeout" });
    }
    throw new ApiError("Can't reach the server.", { code: "network_error" });
  } finally {
    clearTimeout(timer);
  }

  let data = null;
  try { data = await res.json(); } catch (_) { /* empty or non-JSON body */ }

  if (!res.ok) {
    throw new ApiError(
      (data && data.error) || ("Request failed (" + res.status + ")"),
      {
        status: res.status,
        code: (data && data.code) || ("http_" + res.status),
        retryAfter: parseInt(res.headers.get("Retry-After") || "", 10) || (data && data.retry_after) || null,
        field: (data && data.field) || null,
      }
    );
  }
  return data;
}

// Build "/api/path?x=1&y=2" (no host) the same way for every caller, so a
// download link and the fetch behind a page always use identical filters.
function apiUrl(path, params = {}) {
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
  return path + (qs ? "?" + qs : "");
}

async function apiGet(path, params = {}) {
  return apiRequest(apiUrl(path, params));
}

function apiPost(path, body) {
  return apiRequest(path, { method: "POST", body });
}

function titleCase(s) {
  return String(s).toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

// Anything that came from a user (review text, names) or the database must be
// escaped before it goes into innerHTML — otherwise it can inject markup/script.
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Cache /api/options for the page so multiple dropdowns share one request —
// but never cache a FAILURE, or one blip would break every dropdown until reload.
let _optionsPromise = null;
function getOptions() {
  if (!_optionsPromise) {
    _optionsPromise = apiGet("/api/options").catch(err => { _optionsPromise = null; throw err; });
  }
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