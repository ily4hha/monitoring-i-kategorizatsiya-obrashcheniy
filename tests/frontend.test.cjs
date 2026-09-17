const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() {
    this.classes = new Set(); this.children = []; this.listeners = {}; this.style = {}; this.dataset = {};
    this.textContent = ''; this.innerHTML = ''; this.value = ''; this.disabled = false;
    this.classList = {
      add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x),
      toggle: (x, force) => force ? this.classes.add(x) : this.classes.delete(x),
    };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.innerHTML = ''; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

async function harness() {
  const elements = new Map(), timers = new Map(), requests = [];
  let timerId = 0;
  const el = key => { if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key); };
  const context = vm.createContext({
    document: { querySelector: el, querySelectorAll: () => [], createElement: () => new Element() },
    AbortController, URLSearchParams, console,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
    FormData: class { entries() { return [['subject', 'Subject'], ['description', 'Description']]; } },
    fetch: async url => ({ ok: true, json: async () => url === '/api/datasets/current' ? null : { total_appeals: 0 } }),
  });
  const run = code => vm.runInContext(code, context);
  run(source);
  await flush();
  // Intentionally ignore AbortSignal so the request-generation guard is exercised.
  context.fetch = (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject }));
  run('state.dataset = {dataset_id: "D", columns: ["Text"]}; state.total = 21; renderPagination();');
  const reply = (index, page) => requests[index].resolve({ ok: true, json: async () => page });
  const type = text => {
    el('#record-search').value = text;
    el('#record-search').listeners.input({ target: el('#record-search') });
  };
  const debounce = () => {
    const [id, timer] = [...timers].find(([, timer]) => timer.delay === 300);
    timers.delete(id); timer.fn();
  };
  return { context, run, el, requests, reply, type, debounce };
}

test('two immediate Next clicks stop on the last page and preserve total=21', async () => {
  const h = await harness();
  h.el('#next-page').listeners.click();
  assert.equal(h.el('#next-page').disabled, true);
  h.el('#next-page').listeners.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.run('state.offset'), 20);
  h.reply(0, { items: [{ _record_id: 'last', Text: 'Last' }], total: 21 });
  await flush();
  assert.equal(h.el('#page-label').textContent, '21–21 из 21');
  assert.equal(h.run('state.total'), 21);
  assert.equal(h.el('#next-page').disabled, true);
  h.el('#prev-page').listeners.click();
  h.el('#prev-page').listeners.click();
  assert.equal(h.requests.length, 2);
  assert.equal(h.run('state.offset'), 0);
});

test('an empty out-of-range response reloads the last valid page without erasing total', async () => {
  const h = await harness();
  const pending = h.run('state.offset = 40; loadRecords()');
  h.reply(0, { items: [], total: 21 });
  await flush();
  assert.equal(h.run('state.total'), 21);
  assert.equal(h.requests.length, 2);
  assert.match(h.requests[1].url, /offset=20/);
  h.reply(1, { items: [{ _record_id: 'last', Text: 'Last' }], total: 21 });
  await pending;
  assert.equal(h.el('#page-label').textContent, '21–21 из 21');
});

test('typing invalidates a pending request immediately, including the debounce window', async () => {
  const h = await harness();
  const old = h.run('state.query = "old"; loadRecords()');
  h.type('new');
  assert.equal(h.requests[0].options.signal.aborted, true);
  assert.equal(h.run('state.query'), 'new');
  assert.equal(h.el('#next-page').disabled, true);
  const waiting = h.el('#table-wrap').innerHTML;
  h.reply(0, { items: [{ _record_id: 'old', Text: 'Old result' }], total: 999 });
  await old;
  assert.equal(h.el('#table-wrap').innerHTML, waiting);
  assert.notEqual(h.run('state.total'), 999);
  assert.equal(h.run('state.recordsLoading'), true);
  h.debounce();
  assert.match(h.requests[1].url, /q=new/);
  h.reply(1, { items: [{ _record_id: 'new', Text: 'New result' }], total: 1 });
  await flush();
  assert.equal(h.el('#page-label').textContent, '1–1 из 1');
});

test('a late older response cannot replace a newer completed search', async () => {
  const h = await harness();
  const old = h.run('state.query = "old"; loadRecords()');
  h.type('_'); h.debounce();
  assert.match(h.requests[1].url, /q=_/);
  h.reply(1, { items: [], total: 0 });
  await flush();
  const current = h.el('#table-wrap').innerHTML;
  h.reply(0, { items: [{ _record_id: 'old', Text: 'Old' }], total: 99 });
  await old;
  assert.equal(h.el('#table-wrap').innerHTML, current);
  assert.equal(h.run('state.total'), 0);
  assert.equal(h.el('#page-label').textContent, '0–0 из 0');
});

test('an old request error cannot replace the new search loading state', async () => {
  const h = await harness();
  const old = h.run('loadRecords()');
  h.type('new');
  const waiting = h.el('#table-wrap').innerHTML;
  h.requests[0].reject(new Error('Old server error'));
  await old;
  assert.equal(h.el('#table-wrap').innerHTML, waiting);
  assert.equal(h.run('state.recordsLoading'), true);
});

test('record dialog keeps underscore and star user fields, hides only actual metadata', async () => {
  const h = await harness();
  const pending = h.run('openRecord("id")');
  h.reply(0, { _record_id: 'id', _sheet: 'Sheet', _source_row: 2, _customer: 'User', '*custom': 'Star', Text: 'Value' });
  await pending;
  assert.deepEqual(h.el('#record-details').children.map(child => child.textContent), ['_customer', 'User', '*custom', 'Star', 'Text', 'Value']);
  assert.equal(h.el('#record-dialog').open, true);
});

test('structured API errors are rendered as readable text', async () => {
  const h = await harness();
  for (const detail of ['text', { message: 'Object error' }, [{ msg: 'Validation error' }]]) {
    h.context.detail = detail;
    const value = h.run('formatApiDetail(detail)');
    assert.ok(value && !value.includes('[object Object]'));
  }
});

test('a failed analysis hides previous results and similar cards', async () => {
  const h = await harness();
  h.run('renderAnalysis({category: {category: "A", confidence: .9, explanation: "A"}, routing: {support_line: "L1", confidence: .9, explanation: "L1"}, similar_appeals: [], manual_review_required: true})');
  const button = new Element();
  h.context.event = { preventDefault() {}, currentTarget: { querySelector: () => button } };
  const pending = h.run('submitAppeal(event)');
  h.requests[0].resolve({ ok: false, status: 503, json: async () => ({ detail: 'Unavailable' }) });
  await pending;
  for (const id of ['#result-grid', '#similar-section', '#manual-review']) assert.equal(h.el(id).classList.contains('hidden'), true);
  assert.match(h.el('#result-placeholder').textContent, /Unavailable/);
  assert.equal(button.disabled, false);
});
