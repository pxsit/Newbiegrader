const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
function harness() {
  let respond, redirects = 0;
  const state = {user: {email: 'synthetic@example.invalid'}, problems: [{id: 'private'}]};
  const c = vm.createContext({state, API: '', PROBLEMS_CACHE_KEY: 'synthetic',
    fetch: () => new Promise(r => {respond = r;}), clearGetCache() {},
    localStorage: {removeItem() {}}, router() {redirects++;}, refreshMe: async () => {}});
  vm.runInContext(source.slice(source.indexOf('async function apiFetch('), source.indexOf('function apiGet(')), c);
  return {c, state, respond: () => respond({ok: false, status: 401,
    json: async () => ({detail: 'login required'}), headers: {get: () => null}}),
    redirects: () => redirects};
}
test('expired member request clears identity and redirects to login', async () => {
  const h = harness(), request = h.c.apiFetch('/api/problems', {});
  h.respond();
  await assert.rejects(request, /login required/);
  assert.equal(h.state.user, null);
  assert.equal(h.state.problems.length, 0);
  assert.equal(h.redirects(), 1);
});
test('late rejection from a previous login does not log out the current identity', async () => {
  const h = harness(), request = h.c.apiFetch('/api/problems', {});
  const current = h.state.user = {email: 'new@example.invalid'};
  h.respond();
  await assert.rejects(request, /login required/);
  assert.equal(h.state.user, current);
  assert.equal(h.redirects(), 0);
});
