/* Synthetic rendering, essential-cookie notice and shared-device regressions. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

function harness(storage = new Map(), blocked = false) {
  const elements = new Map(), listeners = {}, messages = [], calls = [];
  const context = vm.createContext({
    state: { user: null, myBest: {}, myBestLoaded: true, current: "synthetic", file: {}, jobResults: {} },
    document: { body: { style: {} }, querySelector: () => null },
    window: { addEventListener: (key, fn) => { listeners[key] = fn; } },
    localStorage: {
      getItem: (key) => { if (blocked) throw new Error("disabled"); return storage.get(key) || null; },
      setItem: (key, value) => { if (blocked) throw new Error("disabled"); storage.set(key, value); },
      removeItem: (key) => storage.delete(key),
    },
    location: { hash: "#/task/synthetic" },
    fi: { value: "synthetic.py" }, PROBLEMS_CACHE_KEY: "synthetic-cache",
    icon: () => "<svg></svg>", scoreCls: () => "s0",
    toast: (value) => messages.push(value),
    apiPostJson: async () => ({}), clearGetCache: () => calls.push("cache"),
    clearLocalWorkspace: () => calls.push("workspace"),
    setDraftHint: () => {}, refreshMe: async () => {}, renderTasksTable: () => calls.push("render"),
    loadPublicHome: async () => calls.push("public-home"), renderHome: () => {},
    $: (key) => {
      if (!elements.has(key)) {
        const classes = new Set(["hidden"]);
        elements.set(key, {
          value: "SYNTHETIC_PRIVATE_DRAFT", style: {},
          classList: { contains: (x) => classes.has(x), add: (x) => classes.add(x), remove: (x) => classes.delete(x),
            toggle: (x, force) => { const on = force === undefined ? !classes.has(x) : force; if (on) classes.add(x); else classes.delete(x); return on; } },
          getBoundingClientRect: () => ({ height: 180 }), setAttribute() {}, removeAttribute(key) { delete this[key]; }, focus() {},
        });
      }
      return elements.get(key);
    },
  });
  vm.runInContext(source.slice(source.indexOf("/* ---------------- editor keys"),
    source.indexOf("/* ---------------- task tabs")), context);
  vm.runInContext(source.slice(source.indexOf("function esc("), source.indexOf("function inlineMd(")), context);
  vm.runInContext(source.slice(source.indexOf("function problemNameHtml("), source.indexOf("function byPidOf(")), context);
  vm.runInContext(source.slice(source.indexOf("function taskScoreHtml("), source.indexOf("function renderTasksTable(")), context);
  vm.runInContext(source.slice(source.indexOf("const COOKIE_NOTICE_KEY"), source.indexOf("/* ---------------- language + file")), context);
  vm.runInContext(source.slice(source.indexOf("async function signOut("), source.indexOf("/* ---------------- admin overview")), context);
  return { context, elements, listeners, messages, calls, storage };
}

test("problem names separate languages and escape every untrusted title", () => {
  const h = harness();
  assert.match(h.context.problemNameHtml("โจทย์ (English)"), /problem-english.*English/);
  const html = h.context.problemNameHtml("<img src=x onerror=alert(1)> (<script>)");
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script>"));
  assert.match(h.context.problemNameHtml("ชื่อที่ไม่มีวงเล็บ"), /problem-name/);
});

test("an untried problem is different from an attempted zero score", () => {
  const h = harness();
  assert.match(h.context.taskScoreHtml("p"), /ยังไม่ได้ลอง/);
  h.context.state.myBest.p = 0;
  assert.match(h.context.taskScoreHtml("p"), /0 .*100/);
  h.context.state.myBest.p = 100;
  assert.match(h.context.taskScoreHtml("p"), /ผ่านแล้ว/);
});

test("PDF is the default statement and markdown is a fallback for packages without PDF", () => {
  const h = harness();
  h.context.API = "";
  h.context.setTitle = () => {};
  h.context.diffBadge = () => "easy";
  h.context.renderSampleButtons = () => {};
  h.context.prefetchSamples = () => {};
  h.context.renderMd = (text) => "RENDERED: " + text;
  h.context.bindCopyButtons = () => {};
  h.context.document.getElementById = h.context.$;
  h.context.showPdfFrame = (url) => {
    h.context.$("pdf-frame").src = url;
    h.context.$("pdf-frame").classList.remove("hidden");
  };
  vm.runInContext(source.slice(source.indexOf("function fillTaskDetail("),
    source.indexOf("/* ---------------- public sample viewer")), h.context);
  const problem = { id: "synthetic", title: "โจทย์ (Example)", no: 1,
    time_limit: 1, memory_limit: 128, tests: 3, samples: ["01"], has_pdf: true,
    statement: "SYNTHETIC_PUBLIC_STATEMENT" };
  h.context.fillTaskDetail(problem);
  assert.equal(h.context.$("task-body").classList.contains("hidden"), true);
  assert.equal(h.context.$("pdf-frame").classList.contains("hidden"), false);
  assert.equal(h.context.$("pdf-frame").src, "/api/problems/synthetic/pdf");
  assert.equal(h.context.$("statement-open").href, "/api/problems/synthetic/pdf");
  h.context.fillTaskDetail({ ...problem, has_pdf: false });
  assert.equal(h.context.$("task-body").classList.contains("hidden"), false);
  assert.match(h.context.$("task-body").innerHTML, /SYNTHETIC_PUBLIC_STATEMENT/);
  assert.equal(h.context.$("pdf-frame").classList.contains("hidden"), true);
  assert.equal(h.context.$("statement-open").classList.contains("hidden"), true);
});

test("same PDF reuses its frame and a different problem replaces the frame for Back history", () => {
  const h = harness();
  let current = null, created = 0;
  h.context.document.getElementById = () => current;
  h.context.document.createElement = (tag) => {
    assert.equal(tag, "iframe");
    created++;
    return { dataset: {}, classList: { remove() {} }, replaceWith(frame) { current = frame; } };
  };
  h.context.$("pane-statement").appendChild = (frame) => { current = frame; };
  vm.runInContext(source.slice(source.indexOf("function showPdfFrame("), source.indexOf("function fillTaskDetail(")), h.context);
  h.context.showPdfFrame("/api/problems/first/pdf");
  const first = current;
  h.context.showPdfFrame("/api/problems/first/pdf");
  assert.equal(current, first);
  assert.equal(created, 1);
  h.context.showPdfFrame("/api/problems/second/pdf");
  assert.notEqual(current, first);
  assert.equal(current.src, "/api/problems/second/pdf");
  assert.equal(created, 2);
});

test("statement renderer preserves nested rules, ordered lists, samples and safe Thai headings", () => {
  const h = harness();
  vm.runInContext(source.slice(source.indexOf("function inlineMd("), source.indexOf("function badge(")), h.context);
  const html = h.context.renderMd("## Input\n- rule\n  - nested\n- another\n\n1. first\n2. second\n\n```\n0 1 0\n```\n<script>alert(1)</script>");
  assert.match(html, /<h2>ข้อมูลนำเข้า<\/h2>/);
  assert.match(html, /<li>rule\n<ul>\n<li>nested/);
  assert.match(html, /<ol>\n<li>first\n<\/li>\n<li>second/);
  assert.match(html, /<code>0 1 0<\/code>/);
  assert.ok(!html.includes("<script>"));
  assert.equal((html.match(/<ul>/g) || []).length, (html.match(/<\/ul>/g) || []).length);
});

test("essential-cookie notice persists acknowledgement without gating authentication", () => {
  const h = harness();
  h.context.maybeShowCookieBanner();
  assert.equal(h.context.$("cookie-banner").classList.contains("hidden"), false);
  h.context.$("cookie-ack").onclick();
  assert.equal(h.context.$("cookie-banner").classList.contains("hidden"), true);
  assert.equal(h.context.document.body.style.paddingBottom, "");
  const next = harness(h.storage);
  next.context.maybeShowCookieBanner();
  assert.equal(next.context.$("cookie-banner").classList.contains("hidden"), true);
});

test("blocked storage still allows notice dismissal for the current page", () => {
  const h = harness(new Map(), true);
  h.context.maybeShowCookieBanner();
  h.context.acknowledgeCookieNotice();
  h.context.maybeShowCookieBanner();
  assert.equal(h.context.$("cookie-banner").classList.contains("hidden"), true);
});

test("notice details can reopen and acknowledgements synchronize between tabs", () => {
  const h = harness();
  h.context.acknowledgeCookieNotice();
  h.context.maybeShowCookieBanner(true);
  assert.equal(h.context.$("cookie-banner").classList.contains("hidden"), false);
  h.context.$("cookie-more").onclick();
  assert.equal(h.context.$("cookie-details").classList.contains("hidden"), false);
  h.listeners.storage({ key: "ng_cookie_notice_v2", newValue: "acknowledged" });
  assert.equal(h.context.$("cookie-banner").classList.contains("hidden"), true);
});

test("failed logout keeps the workspace and reports failure", async () => {
  const h = harness();
  h.context.apiPostJson = async () => { throw new Error("synthetic network failure"); };
  await h.context.signOut();
  assert.equal(h.context.$("editor").value, "SYNTHETIC_PRIVATE_DRAFT");
  assert.equal(h.context.state.current, "synthetic");
  assert.equal(h.context.$("logout-btn").disabled, false);
  assert.equal(h.calls.length, 0);
  assert.match(h.messages[0], /ไม่สำเร็จ/);
});

test("successful logout clears editor and current problem so unload cannot resave a private draft", async () => {
  const h = harness();
  await h.context.signOut();
  assert.equal(h.context.$("editor").value, "");
  assert.equal(h.context.$("run-input").value, "");
  assert.equal(h.context.state.current, null);
  assert.equal(h.context.state.file, null);
  assert.ok(h.calls.includes("workspace"));
  assert.equal(h.context.location.hash, "#/");
});

test("light is the default and long-lived login is not preselected", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  assert.match(source, /getItem\("ng_theme"\) \|\| "light"/);
  assert.doesNotMatch(html.match(/<input id="auth-remember"[^>]*>/)[0], /checked/);
  assert.doesNotMatch(html, /cookie-decline|cookie-accept/);
  assert.doesNotMatch(html, /brand-dot/);
});
