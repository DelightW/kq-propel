/* Loads the real admin and chat pages in a DOM and asserts the authentication
   gate behaves correctly end to end against the live server. */
const { JSDOM } = require("jsdom");

const BASE = "http://127.0.0.1:8000";
const PASSWORD = "Frankaren123.";
let pass = 0, fail = 0;

function check(name, cond, detail) {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (detail ? " :: " + detail : "")); }
}

function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

async function load(path) {
  /* jsdom provides no fetch, and the page scripts call it on load, so it must
     be installed via beforeParse. Cookies are tracked per window so the
     HttpOnly session cookie behaves as it would in a browser. */
  const cookies = new Map();

  function cookieHeader() {
    return [...cookies.entries()].map(([k, v]) => k + "=" + v).join("; ");
  }

  function absorb(res) {
    const setCookies = typeof res.headers.getSetCookie === "function"
      ? res.headers.getSetCookie()
      : (res.headers.get("set-cookie") ? [res.headers.get("set-cookie")] : []);
    for (const line of setCookies) {
      const [pair] = line.split(";");
      const idx = pair.indexOf("=");
      const name = pair.slice(0, idx).trim();
      const value = pair.slice(idx + 1).trim();
      if (/Max-Age=0|Expires=Thu, 01 Jan 1970/i.test(line) || value === "") {
        cookies.delete(name);
      } else {
        cookies.set(name, value);
      }
    }
  }

  const dom = await JSDOM.fromURL(BASE + path, {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = async (url, options = {}) => {
        const target = url.startsWith("http") ? url : BASE + url;
        const headers = Object.assign({}, options.headers || {});
        const jarred = cookieHeader();
        if (jarred) { headers.Cookie = jarred; }
        const res = await fetch(target, {
          method: options.method || "GET",
          headers,
          body: options.body,
          redirect: "follow",
        });
        absorb(res);
        return res;
      };
      w.matchMedia = q => ({
        matches: /min-width:\s*1024px/.test(q), media: q,
        addListener() {}, removeListener() {},
        addEventListener() {}, removeEventListener() {},
        onchange: null, dispatchEvent() { return false; },
      });
    },
  });
  return { dom, w: dom.window, cookies };
}

(async () => {
  console.log("\n=== Admin page: unauthenticated ===");
  const a = await load("/admin");
  await wait(2500);
  let doc = a.w.document;
  const login = doc.getElementById("login-screen");
  const dash = doc.getElementById("dashboard");
  check("login screen is visible", login && !login.hidden);
  check("dashboard is hidden", dash && dash.hidden === true);
  check("no transaction rows rendered",
    doc.querySelectorAll("#tx-table tbody tr").length === 0,
    String(doc.querySelectorAll("#tx-table tbody tr").length));
  check("password field is type=password",
    doc.getElementById("login-pass").type === "password");
  check("username field present", !!doc.getElementById("login-user"));
  check("sign out button exists for later", !!doc.getElementById("logout"));

  console.log("\n=== Admin page: signing in through the form ===");
  doc.getElementById("login-user").value = "admin";
  doc.getElementById("login-pass").value = PASSWORD;
  doc.getElementById("login-form").dispatchEvent(
    new a.w.Event("submit", { bubbles: true, cancelable: true }));
  await wait(6000);

  check("dashboard revealed after sign-in", dash && dash.hidden === false,
    "login error said: " + (doc.getElementById("login-error").textContent || "(none)"));
  check("login screen hidden after sign-in", login && login.hidden === true);
  check("operator identity shown",
    (doc.getElementById("who").textContent || "").indexOf("admin") !== -1,
    doc.getElementById("who").textContent);
  check("KPI cards rendered", doc.querySelectorAll("#kpis .card").length > 0,
    String(doc.querySelectorAll("#kpis .card").length));
  check("password field cleared after use",
    doc.getElementById("login-pass").value === "");
  check("access log populated",
    doc.querySelectorAll("#audit-table tbody tr").length > 0,
    String(doc.querySelectorAll("#audit-table tbody tr").length));

  console.log("\n=== Admin page: wrong password ===");
  const b = await load("/admin");
  await wait(2000);
  const bdoc = b.w.document;
  bdoc.getElementById("login-user").value = "admin";
  bdoc.getElementById("login-pass").value = "definitely-wrong";
  bdoc.getElementById("login-form").dispatchEvent(
    new b.w.Event("submit", { bubbles: true, cancelable: true }));
  await wait(3500);
  check("error message shown",
    (bdoc.getElementById("login-error").textContent || "").length > 0,
    bdoc.getElementById("login-error").textContent);
  check("dashboard still hidden",
    bdoc.getElementById("dashboard").hidden === true);

  console.log("\n=== Chat page: session bootstrap ===");
  const c = await load("/");
  await wait(3000);
  const cdoc = c.w.document;
  const bubbles = cdoc.querySelectorAll(".bubble, .msg, .message");
  check("greeting rendered", bubbles.length > 0, String(bubbles.length));
  const input = cdoc.querySelector("#input, input[type=text], textarea");
  check("composer enabled after session starts",
    input && input.disabled === false,
    input ? "disabled=" + input.disabled : "no input found");

  console.log("\n=== Chat page: mobile viewport ===");
  check("viewport meta present",
    !!cdoc.querySelector('meta[name="viewport"]'));

  /* An author rule that sets `display` beats the browser's built-in
     [hidden] { display: none }, because author styles outrank the user-agent
     stylesheet whatever the specificity. When that happened to .login-screen
     the overlay stayed painted over the dashboard after a successful
     sign-in. jsdom does not model the user-agent stylesheet, so this is
     asserted against the stylesheet text rather than the rendered result. */
  console.log("\n=== Stylesheets: hidden attribute stays authoritative ===");
  const guard = /\[hidden\][^{]*\{[^}]*display\s*:\s*none\s*!important/;
  for (const sheet of ["admin.css", "chat.css"]) {
    const css = await (await fetch(BASE + "/static/" + sheet)).text();
    const setsDisplay = /\.(login-screen|scrim)[^{]*\{[^}]*display\s*:/.test(css);
    check(sheet + " keeps [hidden] winning over class display rules",
      guard.test(css) || !setsDisplay,
      setsDisplay ? "sets display on an element toggled via .hidden but has no [hidden] override"
                  : "no overlay rule sets display");
  }

  console.log("\n" + "=".repeat(56));
  console.log("PASSED " + pass + "   FAILED " + fail);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error("harness error:", e); process.exit(2); });
