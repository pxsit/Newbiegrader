/* Synthetic DOM and responses: never sends an email or uses a real account. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const code = source.slice(source.indexOf('let authMode = "login";'), source.indexOf('/* A versioned notice'));

function harness(replies) {
  const bot = { value: null, resets: 0, checks: 0, prepare() {},
    async token() { this.checks++; return this.value; },
    reset() { this.resets++; this.value = null; } };
  const nodes = new Map();
  const calls = [];
  let refreshed = 0;
  let focused = '';
  function node(id) {
    if (!nodes.has(id)) {
      const classes = new Set();
      const attributes = new Map();
      nodes.set(id, { value: '', textContent: '', style: {}, checked: false,
        dataset: {}, setAttribute: (k, v) => attributes.set(k, String(v)),
        getAttribute: (k) => attributes.get(k) ?? null,
        classList: { add: (v) => classes.add(v), remove: (v) => classes.delete(v),
          contains: (v) => classes.has(v),
          toggle: (v, on) => on ? classes.add(v) : classes.delete(v) }, focus() { focused = id; } });
    }
    return nodes.get(id);
  }
  const tabs = ['login', 'register'].map((mode) => {
    const tab = node('auth-tab-' + mode);
    tab.dataset.authtab = mode;
    return tab;
  });
  const context = vm.createContext({ $: node, document: {
    querySelectorAll: (selector) => selector === '[data-authtab]' ? tabs : [],
    querySelector: (selector) => tabs.find((tab) => selector === `[data-authtab="${tab.dataset.authtab}"]`),
  },
    API: '', location: { hash: '#/login' }, refreshMe: async () => { refreshed++; },
    loginDestination: () => '#/tasks',
    GraderBotVerification: bot,
    fetch: async (url, init) => {
      calls.push({ url, payload: JSON.parse(init.body), init });
      const next = replies.shift();
      if (typeof next === 'function') return next();
      return { ok: next.status < 400, status: next.status,
        headers: { get: (k) => (next.headers || {})[k] || null }, json: async () => next.body || {} };
    } });
  vm.runInContext(code, context);
  node('auth-email').value = '  PROOF@example.invalid  ';
  return { node, context, calls, bot, refreshed: () => refreshed, focused: () => focused };
}

test('email request submits a fresh bot token and resets it after success or failure', async () => {
  const h = harness([{ status: 503 }, { status: 202, body: { verification_required: true } }]);
  h.context.setAuthMode('register');
  h.bot.value = 'first-synthetic-token';
  await h.context.doPasswordAuth();
  assert.equal(h.calls[0].payload['cf-turnstile-response'], 'first-synthetic-token');
  assert.equal(h.bot.resets, 1);
  h.bot.value = 'second-synthetic-token';
  await h.context.doPasswordAuth();
  assert.equal(h.calls[1].payload['cf-turnstile-response'], 'second-synthetic-token');
  assert.equal(h.bot.resets, 2);
});

test('unavailable bot module fails closed before requesting an email', async () => {
  const h = harness([]);
  h.context.setAuthMode('register');
  h.context.GraderBotVerification = undefined;
  await h.context.doPasswordAuth();
  assert.equal(h.calls.length, 0);
  assert.match(h.node('auth-err').textContent, /โหลดหน้าเว็บใหม่/);
});

test('auth tabs expose the active panel and support keyboard navigation without a request', () => {
  const h = harness([]);
  h.context.setAuthMode('login');
  h.node('auth-tab-login').onkeydown({key: 'ArrowRight', preventDefault() {}});
  assert.equal(h.node('auth-tab-register').getAttribute('aria-selected'), 'true');
  assert.equal(h.node('auth-tab-login').tabIndex, -1);
  assert.equal(h.node('auth-fields').getAttribute('aria-labelledby'), 'auth-tab-register');
  assert.equal(h.node('auth-heading').textContent, 'สร้างบัญชี');
  assert.equal(h.focused(), 'auth-tab-register');
  h.node('auth-tab-register').onkeydown({key: 'Home', preventDefault() {}});
  assert.equal(h.node('auth-tab-login').getAttribute('aria-selected'), 'true');
  assert.equal(h.node('auth-fields').getAttribute('aria-labelledby'), 'auth-tab-login');
  assert.equal(h.calls.length, 0);
});

test('signup requests proof without sending or retaining a preselected password', async () => {
  const h = harness([{ status: 202, body: { verification_required: true } }]);
  h.context.setAuthMode('register');
  h.node('auth-pass').value = 'synthetic-preselected';
  await h.context.doPasswordAuth();
  assert.equal(h.calls[0].url, '/api/auth/email/request');
  assert.deepEqual(h.calls[0].payload, { email: 'proof@example.invalid' });
  assert.equal(h.node('auth-pass').value, '');
  assert.equal(h.node('auth-email').readOnly, true);
  assert.equal(h.node('row-auth-code').classList.contains('hidden'), false);
  assert.equal(h.node('auth-err').classList.contains('bad'), false);
  assert.equal(h.refreshed(), 0);
  assert.equal(h.context.location.hash, '#/login');
});

test('registration asks for the missing email without requesting a hidden password', async () => {
  const h = harness([]);
  h.context.setAuthMode('register');
  h.node('auth-email').value = '';
  await h.context.doPasswordAuth();
  assert.equal(h.node('auth-err').textContent, 'กรอกอีเมลของคุณ');
  assert.equal(h.node('row-auth-pass').classList.contains('hidden'), true);
  assert.equal(h.calls.length, 0);
});

test('verification posts code and newly chosen password; clears both after success', async () => {
  const h = harness([{ status: 202, body: { verification_required: true } }, { status: 200 }]);
  h.context.setAuthMode('register');
  await h.context.doPasswordAuth();
  h.node('auth-code').value = '12345678';
  h.node('auth-pass').value = 'synthetic-new-password';
  h.node('auth-remember').checked = true;
  await h.context.doPasswordAuth();
  assert.equal(h.calls[1].url, '/api/auth/email/verify');
  assert.equal(h.calls[1].payload.code, '12345678');
  assert.equal(h.calls[1].payload.password, 'synthetic-new-password');
  assert.equal(h.calls[1].payload.remember, true);
  assert.equal(h.calls[1].init.credentials, 'include');
  assert.equal(h.node('auth-code').value, '');
  assert.equal(h.node('auth-pass').value, '');
  assert.equal(h.refreshed(), 1);
  assert.equal(h.context.location.hash, '#/tasks');
});

test('legacy login moves to proof setup without sending an unsolicited email', async () => {
  const h = harness([{ status: 403, headers: { 'X-Email-Verification-Required': 'true' } }]);
  h.node('auth-pass').value = 'synthetic-old';
  await h.context.doPasswordAuth();
  assert.equal(h.calls.length, 1);
  assert.equal(h.node('auth-pass').value, '');
  assert.match(h.node('auth-go').textContent, /ส่งรหัส/);
  assert.equal(h.refreshed(), 0);
});

test('resend uses only the bound email and preserves the challenge on rate limit', async () => {
  const h = harness([{ status: 202, body: { verification_required: true } },
    { status: 429, headers: { 'Retry-After': '59' } }]);
  h.context.setAuthMode('register');
  await h.context.doPasswordAuth();
  h.node('auth-email').value = 'other@example.invalid';
  await h.context.doPasswordAuth(true);
  assert.deepEqual(h.calls[1].payload, { email: 'proof@example.invalid' });
  assert.match(h.node('auth-err').textContent, /59/);
  assert.equal(h.refreshed(), 0);
  assert.equal(h.node('auth-resend').disabled, false);
});

test('concurrent clicks issue one request and busy tabs cannot change the flow', async () => {
  let resolve;
  const pending = new Promise((r) => { resolve = r; });
  const h = harness([() => pending]);
  h.context.setAuthMode('register');
  const first = h.context.doPasswordAuth();
  h.context.setAuthMode('login');
  await h.context.doPasswordAuth();
  assert.equal(h.calls.length, 1);
  resolve({ ok: true, status: 202, json: async () => ({ verification_required: true }) });
  await first;
  assert.equal(h.node('auth-pass').autocomplete, 'new-password');
});

test('provider outage and malformed code do not navigate or authenticate', async () => {
  const h = harness([{ status: 503 }, { status: 202, body: { verification_required: true } }]);
  h.context.setAuthMode('register');
  await h.context.doPasswordAuth();
  assert.equal(h.refreshed(), 0);
  assert.equal(h.node('auth-go').disabled, false);
  await h.context.doPasswordAuth();
  h.node('auth-pass').value = 'synthetic-new-password';
  h.node('auth-code').value = '<script>';
  await h.context.doPasswordAuth();
  assert.equal(h.calls.length, 2);
  assert.match(h.node('auth-err').textContent, /8/);
});
