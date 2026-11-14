/*
  ResaleWise SG — shared authentication helper
  --------------------------------------------------------------
  Token-based auth. On login/register the API returns a signed token,
  which we keep in localStorage and send as "Authorization: Bearer <token>".

  Depends on api.js for API_BASE. Load auth.js AFTER api.js.
*/
(function () {
  "use strict";

  var TOKEN_KEY = "rw_token";
  var USER_KEY  = "rw_user";
  var BASE = (typeof API_BASE !== "undefined") ? API_BASE : "http://localhost:5000";

  var Auth = {
    token: function () { return localStorage.getItem(TOKEN_KEY); },
    user: function () {
      try { return JSON.parse(localStorage.getItem(USER_KEY) || "null"); }
      catch (e) { return null; }
    },
    isLoggedIn: function () { return !!this.token(); },

    save: function (token, user) {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(user || {}));
    },
    logout: function () {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    },

    // POST /api/login
    login: async function (email, password) {
      var data = await postJson("/api/login", { email: email, password: password });
      this.save(data.token, data.user);
      return data.user;
    },

    // POST /api/register
    register: async function (email, password, name) {
      var data = await postJson("/api/register", { email: email, password: password, name: name });
      this.save(data.token, data.user);
      return data.user;
    }
  };

  // POST JSON and return the parsed body. Failures throw an Error whose
  // message is safe to show on screen (server-provided text, or a plain
  // sentence when the server can't be reached) — never "Failed to fetch".
  async function postJson(path, payload) {
    var res;
    try {
      res = await fetch(BASE + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    } catch (e) {
      throw new Error("Can’t reach the server. Check your connection and try again.");
    }
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      var err = new Error(data.error || ("Something went wrong (" + res.status + "). Please try again."));
      err.code = data.code; err.status = res.status; err.field = data.field;
      throw err;
    }
    return data;
  }

  // fetch() wrapper that attaches the bearer token. Use for protected calls.
  window.authFetch = function (path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    var t = Auth.token();
    if (t) opts.headers["Authorization"] = "Bearer " + t;
    var url = path.indexOf("http") === 0 ? path : (BASE + path);
    return fetch(url, opts);
  };

  // Redirect to login if not authenticated (call at the top of protected pages).
  window.requireAuth = function (returnTo) {
    if (!Auth.isLoggedIn()) {
      var back = encodeURIComponent(returnTo || (location.pathname.split("/").pop() + location.search));
      location.href = "login.html?next=" + back;
      return false;
    }
    return true;
  };

  // Render the nav's account area: "Log in" when out, or the user's name +
  // "Log out" when in. Runs automatically on DOMContentLoaded.
  function renderNav() {
    var actions = document.querySelector(".nav-actions");
    if (!actions) return;
    var user = Auth.user();
    if (Auth.isLoggedIn() && user) {
      var name = user.name || (user.email || "Account").split("@")[0];
      actions.innerHTML =
        '<a class="text-btn" href="compare.html">Shortlist</a>' +
        '<a class="nav-user" href="account.html" title="Account settings">' + escapeHtml(name) + '</a>' +
        '<button class="primary-btn" id="logoutBtn" type="button">Log out</button>';
      var lo = document.getElementById("logoutBtn");
      if (lo) lo.addEventListener("click", function () {
        Auth.logout();
        location.href = "index.html";
      });
    } else {
      actions.innerHTML =
        '<a class="text-btn" href="login.html">Log in</a>' +
        '<a class="primary-btn" href="register.html">Sign up →</a>';
    }
  }

  window.Auth = Auth;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderNav);
  } else {
    renderNav();
  }
  window.renderAuthNav = renderNav;
})();
