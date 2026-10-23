/* Synthetic browser primitives and identities only; no real sessions or API. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness({ me, restore = async () => ({ restored: false }), stats = async () => {}, route = '#/admin', healthFails = false }) {
  const nodes = new Map(), events = new Map(), queued = [], calls = [], replaced = [];
  let hash = route, adminLoads = 0;
  function node(id) {
    if (!nodes.has(id)) {
      const classes = new Set(id === 'admin-content' || id.endsWith('-card') ? ['hidden'] : []);
      nodes.set(id, { id, value: '', textContent: '', innerHTML: '', dataset: {},
        classList: {
          add: (v) => classes.add(v), remove: (v) => classes.delete(v),
          contains: (v) => classes.has(v),
          toggle(v, on = !classes.has(v)) { if (on) classes.add(v); else classes.delete(v); },
        }, setAttribute() {}, removeAttribute() {}, hasAttribute: () => false,
        querySelectorAll: () => [], querySelector: () => null,
        insertAdjacentHTML() {}, focus() {},
      });
    }
    return nodes.get(id);
  }
  const views = ['home', 'admin', 'tasks', 'login'].map((v) => node('view-' + v));
  const location = {
    get hash() { return hash; },
    set hash(value) { hash = value; queued.push(() => events.get('hashchange')()); },
    get href() { return 'https://synthetic.invalid/' + hash; },
    replace(value) { replaced.push(value); this.hash = value; },
  };
  const context = vm.createContext({
    $: node, location, history: { state: null, replaceState() {} },
    document: { querySelectorAll: (q) => q === '.view' ? views : [],
      querySelector: () => null, addEventListener() {} },
    window: { scrollY: 0, scrollTo() {}, addEventListener: (name, fn) => events.set(name, fn),
      matchMedia: () => ({ matches: true, addEventListener() {} }) },
    localStorage: { getItem: () => null },
    apiGet: async (url) => {
      calls.push(url);
      if (url === '/api/auth/me') return me();
      if (url === '/api/auth/restore') return restore();
      if (url === '/api/health') { if (healthFails) throw new Error('synthetic outage'); return { ok: true }; }
      if (url === '/api/problems') return { problems: [] };
      if (url === '/api/home') return { problems: [], configured: true, summary: {} };
      throw new Error('Unexpected API request: ' + url);
    },
    _getGen: 0, clearGetCache() { context._getGen++; }, problemRowsForIdentity: (rows) => rows,
    setLang() {}, lastLang: () => null, bindEditorKeys() {}, saveDraft() {},
    openPalette() {}, maybeShowCookieBanner() {}, loadProblemsCache: () => false,
    problemsCacheAgeMs: () => Infinity, saveProblemsCache() {}, buildFilterOptions() {},
    stopContestTicker() {}, renderHome() {}, renderTasksTable() {}, setAuthMode() {},
    loadHistory() {}, loadAdminReports() {}, loadAdmins() {}, addAdminByEmail() {}, loadAdminHistory() {},
    fetchStats: () => { calls.push('/api/stats'); return stats(); }, buildProblemSelect() {}, renderContestProblemPicker() {},
    loadAdminContests() { adminLoads++; }, loadOverview() { adminLoads++; },
  });
  function load(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
    assert.ok(from >= 0 && to > from, `invalid source boundaries: ${start} -> ${end}`);
    vm.runInContext(source.slice(from, to), context);
  }
  load('const state =', 'const _sampleInflight');
  load('function show(view)', '/* ---------------- home');
  load('async function refreshMe()', '$("login-btn").onclick');
  load('async function refreshProblems()', '/* Canonical tag taxonomy');
  load('const ADMIN_TABS =', 'function setAdminsStatus(');
  load('async function init()', '\ninit();');
  return { context, state: vm.runInContext('state', context), node, location, calls, replaced,
    loads: () => adminLoads,
    start: () => context.init(),
    async flush() {
      await new Promise(setImmediate);
      while (queued.length) { queued.shift()(); await new Promise(setImmediate); }
    },
  };
}

const admin = { user: { email: 'admin@example.invalid', name: 'Synthetic Admin' }, is_admin: true, profile: {} };
const regular = { user: { email: 'student@example.invalid' }, is_admin: false, profile: {} };
const anonymous = { user: null, is_admin: false };

test('explicitly absent remember cookie skips restoration without granting an identity', async () => {
  const h = harness({ me: async () => ({...anonymous, restore_available: false}) });
  await h.start(); await h.flush();
  assert.equal(h.calls.includes('/api/auth/restore'), false);
  assert.equal(h.state.authReady, true);
  assert.equal(h.state.user, null);
  assert.equal(h.loads(), 0);
});

test('public landing starts while identity is pending without requesting protected statistics', async () => {
  const identity = deferred(), stats = deferred();
  const h = harness({ me: () => identity.promise, stats: () => stats.promise, route: '#/' });
  const boot = h.start();
  assert.equal(h.calls.filter(url => url === '/api/home').length, 1);
  assert.equal(h.calls.includes('/api/stats'), false);
  assert.equal(h.state.authReady, false);
  identity.resolve({...anonymous, restore_available: false});
  await boot; await h.flush();
  assert.equal(h.state.authReady, true);
  stats.resolve(); await h.flush();
});

test('present remember cookie still restores and revalidates before opening admin', async () => {
  let reads = 0;
  const h = harness({ me: async () => ++reads === 1 ? {...anonymous, restore_available: true} : admin,
    restore: async () => ({restored: true}) });
  await h.start(); await h.flush();
  assert.equal(reads, 2);
  assert.equal(h.calls.filter(url => url === '/api/auth/restore').length, 1);
  assert.equal(h.node('admin-content').classList.contains('hidden'), false);
});

test('reload retains admin URL and hides controls until the actual identity resolves', async () => {
  const pending = deferred(), h = harness({ me: () => pending.promise });
  const boot = h.start();
  assert.equal(h.state.authReady, false);
  assert.equal(h.location.hash, '#/admin');
  assert.equal(h.node('view-admin').classList.contains('active'), false);
  assert.equal(h.node('view-home').classList.contains('active'), true);
  assert.equal(h.node('admin-content').classList.contains('hidden'), true);
  assert.equal(h.loads(), 0);
  pending.resolve(admin);
  await boot; await h.flush();
  assert.equal(h.location.hash, '#/admin');
  assert.equal(h.state.authReady, true);
  assert.equal(h.node('admin-content').classList.contains('hidden'), false);
  assert.equal(h.node('admin-session-status').classList.contains('hidden'), true);
  assert.equal(h.loads(), 2);
  assert.equal(h.calls.includes('/api/auth/restore'), false);
});

test('missing session waits for remember-me restoration before rejecting admin', async () => {
  const pending = deferred(); let reads = 0;
  const h = harness({ me: async () => ++reads === 1 ? anonymous : admin, restore: () => pending.promise });
  const boot = h.start(); await h.flush();
  assert.equal(h.state.authReady, false);
  assert.equal(h.location.hash, '#/admin');
  assert.equal(h.loads(), 0);
  pending.resolve({ restored: true });
  await boot; await h.flush();
  assert.equal(reads, 2);
  assert.equal(h.location.hash, '#/admin');
  assert.equal(h.node('admin-content').classList.contains('hidden'), false);
});

for (const [name, identity] of [['regular user', regular], ['anonymous', anonymous]]) {
  test(`${name} is denied after identity and restoration complete, with no admin data load`, async () => {
    const h = harness({ me: async () => identity });
    await h.start(); await h.flush();
    assert.equal(h.location.hash, name === 'anonymous' ? '#/login' : '#/');
    assert.deepEqual(h.replaced, [name === 'anonymous' ? '#/login' : '#/']);
    assert.equal(h.loads(), 0);
    assert.equal(h.node('admin-content').classList.contains('hidden'), true);
  });
}

test('identity or restoration outage never paints protected content and offers login retry', async () => {
  for (const identityFails of [true, false]) {
    const h = harness({ me: async () => {
      if (identityFails) throw new Error('synthetic identity outage');
      return anonymous;
    }, restore: async () => { throw new Error('synthetic restore outage'); } });
    await h.start(); await h.flush();
    assert.equal(h.location.hash, '#/login');
    assert.equal(h.state.authError, true);
    assert.match(h.node('auth-err').textContent, /ไม่สำเร็จ/);
    assert.equal(h.node('admin-content').classList.contains('hidden'), true);
    assert.equal(h.loads(), 0);
  }
});

test('late identity response does not pull a visitor back after leaving admin', async () => {
  const pending = deferred(), h = harness({ me: () => pending.promise });
  const boot = h.start();
  h.location.hash = '#/tasks'; await h.flush();
  pending.resolve(admin); await boot; await h.flush();
  assert.equal(h.location.hash, '#/tasks');
  assert.equal(h.node('view-tasks').classList.contains('active'), true);
  assert.equal(h.loads(), 0);
});

test('unavailable health endpoint does not send a cold admin reload to home', async () => {
  const h = harness({ me: async () => admin, healthFails: true });
  await h.start(); await h.flush();
  assert.equal(h.location.hash, '#/admin');
  assert.equal(h.node('view-admin').classList.contains('active'), true);
  assert.equal(h.node('admin-content').classList.contains('hidden'), false);
});

test('revoked admin identity hides existing controls and replaces the protected route', async () => {
  let identity = admin;
  const h = harness({ me: async () => identity });
  await h.start(); await h.flush();
  identity = regular;
  await h.context.refreshMe(); await h.flush();
  assert.equal(h.location.hash, '#/');
  assert.equal(h.node('admin-content').classList.contains('hidden'), true);
  assert.equal(h.loads(), 2);
});

test('public route paints immediately while identity is pending', async () => {
  const pending = deferred(), h = harness({ me: () => pending.promise, route: '#/' });
  const boot = h.start();
  assert.equal(h.node('view-home').classList.contains('active'), true);
  assert.equal(h.state.authReady, false);
  pending.resolve(regular); await boot; await h.flush();
  assert.equal(h.location.hash, '#/');
  assert.equal(h.loads(), 0);
});

for (const route of ['#/tasks', '#/archive', '#/task/synthetic', '#/submission/synthetic',
  '#/history', '#/settings', '#/contests', '#/contests/synthetic']) {
  test(`anonymous direct navigation to ${route} opens login without loading protected data`, async () => {
    const h = harness({ me: async () => ({...anonymous, restore_available: false}), route });
    await h.start(); await h.flush();
    assert.equal(h.location.hash, '#/login');
    assert.equal(h.state.authReturnTo, route);
    assert.equal(h.context.loginDestination(), route);
    assert.equal(h.node('view-login').classList.contains('active'), true);
    assert.equal(h.calls.includes('/api/problems'), false);
    assert.equal(h.calls.includes('/api/stats'), false);
  });
}

test('login destination only accepts internal protected application routes', () => {
  const h = harness({ me: async () => anonymous });
  for (const route of ['https://evil.invalid', '//evil.invalid', '#/login', '#/task/x?next=evil']) {
    h.state.authReturnTo = route;
    assert.equal(h.context.loginDestination(), '#/tasks');
  }
});


test('late member catalog cannot repopulate state after logout', async () => {
  const h = harness({ me: async () => regular });
  const pending = deferred();
  h.state.user = regular.user;
  h.context.apiGet = () => pending.promise;
  const loading = h.context.refreshProblems();
  h.context.clearGetCache();
  h.state.user = null;
  h.state.problems = [];
  pending.resolve({problems: [{id: 'private-catalog-canary'}]});
  assert.equal(await loading, false);
  assert.equal(h.state.problems.length, 0);
});
