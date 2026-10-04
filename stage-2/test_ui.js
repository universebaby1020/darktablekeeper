// State-flow checks with an in-memory DOM and fetch. This is not a browser/visual test.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor(tag = 'div') { this.tag = tag; this.attributes = {}; this.children = []; this.parent = null; this.value = ''; this.disabled = false; this.text = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'value') this.value = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const node of this.children) node.parent = null; this.children = []; this.text = ''; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); this.parent = null; }
  set textContent(value) { this.replaceChildren(); this.text = String(value); }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(''); }
  set className(value) { this.attributes.class = value; }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
  matches(selector) {
    const attr = selector.match(/^\[([^=]+)="([^"]*)"\]$/);
    if (attr) return this.attributes[attr[1]] === attr[2];
    return this.tag === selector;
  }
  querySelectorAll(selector) { return this.children.flatMap(node => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  set innerHTML(value) {
    this.replaceChildren();
    const stack = [this];
    for (const token of value.match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (token.startsWith('<')) {
        const tag = token.match(/^<([\w-]+)/)?.[1];
        if (!tag) continue;
        const node = new Node(tag);
        for (const attr of token.matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(attr[1], attr[2]);
        stack.at(-1).append(node);
        if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(tag)) stack.push(node);
      } else {
        const text = new Node('#text'); text.text = token; stack.at(-1).append(text);
      }
    }
  }
}

function environment(fetcher) {
  const root = new Node('body');
  for (const id of ['account-nav', 'app']) { const node = new Node(); node.setAttribute('id', id); root.append(node); }
  const document = {
    createElement: tag => new Node(tag),
    getElementById: id => root.querySelector(`[id="${id}"]`),
    querySelector: selector => root.querySelector(selector),
    querySelectorAll: selector => root.querySelectorAll(selector),
  };
  let keyNumber = 0;
  const sandbox = { document, fetch: fetcher, URLSearchParams, Uint8Array, Intl, Date, console,
    crypto: { randomUUID: () => 'request-' + (++keyNumber) },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    location: { pathname: '/', search: '', assign: () => {} } };
  vm.createContext(sandbox);
  const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
  vm.runInContext(source.slice(0, source.lastIndexOf('\nrenderAccount();')), sandbox);
  vm.runInContext('session = { token: "local-test-session", display_name: "Ada" }; renderAccount(); renderSearch();', sandbox);
  return { sandbox, document, run: expression => vm.runInContext(expression, sandbox) };
}

const restaurant = (id = 'r') => ({ id, name: 'Restaurant ' + id, timezone: 'UTC',
  tables: [{ id: id + '1', label: 'Window ' + id, capacity: 4 }, { id: id + '2', label: 'Garden ' + id, capacity: 4 }], combinable: [[id + '2', id + '1']] });
const availability = (id = 'r', taken = false) => ({ restaurant_id: id, date: '2090-01-05', slots: [{ starts_at_local: '2090-01-05T19:00',
  available_table_ids: taken ? [id + '2'] : [id + '1', id + '2'],
  available_options: taken ? [] : [{ table_ids: [id + '2', id + '1'], capacity: 8 }] }] });
const response = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
const flush = () => new Promise(resolve => setImmediate(resolve));
const query = id => JSON.stringify({ restaurant_id: id, date: '2090-01-05', party_size: 2 });
const ordinaryFetcher = async path => {
  if (path === '/restaurants') return response({ restaurants: [restaurant()] });
  if (path.startsWith('/restaurants/')) return response(restaurant(path.split('/').at(-1)));
  const id = new URLSearchParams(path.split('?')[1]).get('restaurant_id');
  return response(availability(id));
};

async function lateSearchCannotRestoreEarlierGridOrBooking() {
  let finishA;
  const env = environment(async path => {
    if (path.startsWith('/availability?') && path.includes('restaurant_id=A')) return new Promise(resolve => { finishA = () => resolve(response(availability('A'))); });
    return ordinaryFetcher(path);
  });
  await flush();
  const first = env.run('runSearch(' + query('A') + ')');
  await env.run('runSearch(' + query('B') + ')');
  const button = env.document.querySelector('[data-testid="slot-B1-19:00"]');
  assert(button.textContent.includes('Window B'));
  button.onclick();
  assert.equal(env.run('selection.body.restaurant_id'), 'B');
  assert(env.document.querySelector('[data-testid="booking-submit"]').focused);
  assert(env.document.querySelector('[data-testid="booking-submit"]').scrolled);
  finishA(); await first;
  assert.equal(env.run('searchContext.restaurant.id'), 'B');
  assert.equal(env.run('selection.body.restaurant_id'), 'B');
  assert.equal(env.document.querySelector('[data-testid="slot-A1-19:00"]'), null);
  assert(env.document.querySelector('[data-testid="booking-summary"]').textContent.includes('Window B'));
}

async function uncertainPairRetryUsesOriginalIdentityAndClearsFeedback() {
  const requests = [];
  const env = environment(async (path, options) => {
    if (path === '/reservations') {
      requests.push({ key: options.headers['Idempotency-Key'], body: options.body });
      if (requests.length === 1) throw new Error('Response connection lost after submission');
      const body = JSON.parse(options.body);
      return response({ ...body, reference: 'ORIGIN01', status: 'confirmed' }, 200);
    }
    return ordinaryFetcher(path);
  });
  await flush(); await env.run('runSearch(' + query('r') + ')');
  env.document.querySelector('[data-testid="slot-r2+r1-19:00"]').onclick();
  assert(env.document.querySelector('[data-testid="booking-summary"]').textContent.includes('Garden r & Window r'));
  const party = env.document.querySelector('[data-testid="booking-party-size"]');
  party.value = '7'; party.oninput();
  await env.run('submitBooking({preventDefault(){}})');
  assert(env.document.querySelector('[data-testid="booking-uncertain"]').textContent.length > 0);
  assert.equal(env.document.querySelector('[data-testid="booking-error"]'), null);
  assert.equal(env.document.querySelector('[data-testid="confirmation"]'), null);
  assert.equal(party.value, '7');
  await env.run('submitBooking({preventDefault(){}})');
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(env.document.querySelector('[data-testid="confirmation-reference"]').textContent, 'ORIGIN01');
  assert.equal(env.document.querySelector('[data-testid="booking-uncertain"]'), null);
  assert.equal(env.document.querySelector('[data-testid="booking-error"]'), null);
  assert(env.document.querySelector('[data-testid="confirmation-tables"]').textContent.includes('Garden r & Window r'));
  await env.run('submitBooking({preventDefault(){}})');
  assert.deepEqual(requests[0], requests[2]);
  assert(env.document.querySelector('[data-testid="booking-form"]'));
  party.value = '6'; party.oninput();
  await env.run('submitBooking({preventDefault(){}})');
  assert.notEqual(requests[0].key, requests[3].key);
  assert.notEqual(requests[0].body, requests[3].body);
}

async function confirmedConflictRefreshPreservesFormAndInputs() {
  let conflict = false;
  const env = environment(async path => {
    if (path === '/reservations') { conflict = true; return response({ error: { code: 'table_unavailable', message: 'Table is taken' } }, 409); }
    if (path.startsWith('/availability?')) return response(availability('r', conflict));
    return ordinaryFetcher(path);
  });
  await flush(); await env.run('runSearch(' + query('r') + ')');
  env.document.querySelector('[data-testid="slot-r1-19:00"]').onclick();
  const form = env.document.querySelector('[data-testid="booking-form"]');
  const input = env.document.querySelector('[data-testid="booking-party-size"]');
  input.value = '3'; input.oninput();
  await env.run('submitBooking({preventDefault(){}})');
  assert.equal(env.document.querySelector('[data-testid="booking-form"]'), form);
  assert.equal(input.value, '3');
  assert(env.document.querySelector('[data-testid="booking-error"]'));
  assert.equal(env.document.querySelector('[data-testid="booking-uncertain"]'), null);
  assert.equal(env.document.querySelector('[data-testid="confirmation"]'), null);
  assert.equal(env.document.querySelector('[data-testid="slot-r1-19:00"]').getAttribute('data-available'), 'false');
  assert.equal(env.document.querySelector('[data-testid="slot-r2+r1-19:00"]'), null);
}

async function emptySlotsAndSignedOutSelectionHaveExplicitStates() {
  const env = environment(ordinaryFetcher);
  await flush(); await env.run('runSearch(' + query('r') + ')');
  env.run('session=null; renderAccount();');
  env.document.querySelector('[data-testid="slot-r1-19:00"]').onclick();
  assert(env.document.querySelector('[data-testid="auth-error"]'));
  assert.equal(env.document.querySelector('[data-testid="booking-form"]'), null);
  env.run('searchContext.availability.slots=[]; renderGrid(searchContext);');
  assert(env.document.querySelector('[data-testid="no-slots"]'));
  assert.equal(env.document.querySelector('[data-testid="availability-grid"]'), null);
  assert.equal(env.document.querySelector('[data-testid="current-user"]'), null);
}

async function lookupCancellationShowsBothLabelsAndFinalStatus() {
  const record = { restaurant_id: 'r', table_ids: ['r2', 'r1'], starts_at_local: '2090-01-05T19:00', party_size: 6, reference: 'BOOK001', status: 'confirmed' };
  const env = environment(async path => {
    if (path.endsWith('/cancel')) return response({ ...record, status: 'cancelled' });
    if (path.startsWith('/reservations/')) return response(record);
    return ordinaryFetcher(path);
  });
  await flush(); env.run('renderLookup();');
  env.document.getElementById('lookup-reference').value = 'BOOK001';
  await env.document.getElementById('lookup-form').onsubmit({ preventDefault() {} });
  assert.equal(env.document.querySelector('[data-testid="reservation-status"]').textContent, 'confirmed');
  assert(env.document.querySelector('[data-testid="reservation-tables"]').textContent.includes('Garden r & Window r'));
  await env.document.querySelector('[data-testid="reservation-cancel-button"]').onclick();
  assert.equal(env.document.querySelector('[data-testid="reservation-status"]').textContent, 'cancelled');
  assert.equal(env.document.querySelector('[data-testid="reservation-cancel-button"]'), null);
}

async function authScreensSubmitFieldsAndKeepAccountNavigationConsistent() {
  const env = environment(async (path, options) => {
    if (path === '/auth/signup') {
      const body = JSON.parse(options.body);
      assert.equal(body.email, 'local@example.com');
      assert.equal(body.display_name, 'Diner');
      return response({ user_id: 'local-user', display_name: 'Diner', token: 'local-test-session' }, 201);
    }
    if (path === '/auth/login') return response({ error: { code: 'unauthenticated', message: 'Check your email and password.' } }, 401);
    return ordinaryFetcher(path);
  });
  await flush();
  let destination = null;
  env.sandbox.location.assign = path => { destination = path; };
  env.run('session=null; renderAccount(); renderAuth(true);');
  env.document.getElementById('auth-name').value = 'Diner';
  env.document.getElementById('auth-email').value = 'local@example.com';
  env.document.getElementById('auth-password').value = 'local test password';
  await env.document.getElementById('auth-form').onsubmit({ preventDefault() {} });
  assert.equal(destination, '/');
  assert.equal(env.document.querySelector('[data-testid="current-user"]').textContent, 'Diner');
  for (const render of ['renderAuth(false)', 'renderLookup()', 'renderSearch()']) {
    env.run(render);
    await flush();
    assert.equal(env.document.querySelector('[data-testid="current-user"]').textContent, 'Diner');
  }
  env.run('session=null; renderAccount(); renderAuth(false);');
  env.document.getElementById('auth-email').value = 'local@example.com';
  env.document.getElementById('auth-password').value = 'incorrect local password';
  await env.document.getElementById('auth-form').onsubmit({ preventDefault() {} });
  assert(env.document.querySelector('[data-testid="auth-error"]').textContent.length > 0);
  assert.equal(env.document.querySelector('[data-testid="current-user"]'), null);
}

(async () => {
  for (const test of [lateSearchCannotRestoreEarlierGridOrBooking, uncertainPairRetryUsesOriginalIdentityAndClearsFeedback,
    confirmedConflictRefreshPreservesFormAndInputs, emptySlotsAndSignedOutSelectionHaveExplicitStates,
    lookupCancellationShowsBothLabelsAndFinalStatus, authScreensSubmitFieldsAndKeepAccountNavigationConsistent]) {
    await test(); console.log('PASS ' + test.name);
  }
  console.log('6 state-flow checks passed; direct browser and visual review NOT_PERFORMED.');
})().catch(error => { console.error(error); process.exitCode = 1; });
