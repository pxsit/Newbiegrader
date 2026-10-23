/* Retired global ranking: navigation must not fetch or render the old board. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");

test("global leaderboard has no menu, view, renderer or API request", () => {
  assert.doesNotMatch(html, /#\/leaderboard|view-leaderboard|leader-body/);
  assert.doesNotMatch(source, /renderLeaderboard|\/api\/leaderboard/);
});

test("old leaderboard bookmarks replace their entry with the problem list", () => {
  const calls = [];
  const context = vm.createContext({
    location: { hash: "#/leaderboard" }, _pendingNav: null,
    state: { user: { email: 'synthetic@example.invalid' }, authReady: true },
    publicRoute: () => false,
    _syncHistoryEntry() {}, stopContestTicker() {},
    _nav: (...args) => calls.push(args),
  });
  vm.runInContext(source.slice(source.indexOf("function router()"),
    source.indexOf('window.addEventListener("hashchange", router)')), context);
  context.router();
  assert.deepEqual(calls, [["#/tasks", "replace"]]);
});

test("contest navigation remains available", () => {
  assert.match(html, /data-route="contests" href="#\/contests"/);
  const calls = [];
  const context = vm.createContext({
    location: { hash: "#/contests" },
    renderContests: () => calls.push("contests"),
  });
  vm.runInContext(source.slice(source.indexOf("function currentRouteRender()"),
    source.indexOf("function router()")), context);
  context.currentRouteRender()();
  assert.deepEqual(calls, ["contests"]);
});
