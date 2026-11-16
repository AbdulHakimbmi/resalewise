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
      var name = user.is_admin ? "Account settings" : user.name || (user.email || "Account").split("@")[0];
      actions.innerHTML =
        (user.is_admin ? '<a class="text-btn" href="admin.html">Database</a>' : '') +
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

  // eye button on every password box: show what is being typed, off by default
  var EYE = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_OFF = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.9 17.9A10.9 10.9 0 0 1 12 19C5 19 1 12 1 12a18.5 18.5 0 0 1 5.1-5.9M9.9 5.2A10.4 10.4 0 0 1 12 5c7 0 11 7 11 7a18.6 18.6 0 0 1-2.2 3.2M1 1l22 22"/><path d="M14.1 14.1a3 3 0 1 1-4.2-4.2"/></svg>';
  function addPasswordToggles() {
    var boxes = document.querySelectorAll('input[type="password"]');
    Array.prototype.forEach.call(boxes, function (input) {
      var wrap = document.createElement("div");
      wrap.className = "pw-wrap";
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pw-toggle";
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", "Show password");
      btn.innerHTML = EYE;
      btn.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-pressed", String(show));
        btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
        btn.innerHTML = show ? EYE_OFF : EYE;
        input.focus();
      });
      wrap.appendChild(btn);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", addPasswordToggles);
  } else {
    addPasswordToggles();
  }
})();
