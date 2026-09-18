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

test('current dataset loading failure renders analytics and records error states', async () => {
  const h = await harness();
  const pending = h.run('loadCurrentDataset()');
  h.requests[0].reject(new Error('Connection lost'));
  await pending;
  assert.equal(h.el('#kpi-status').textContent, 'Ошибка');
  assert.match(h.el('#analytics-message').textContent, /Connection lost/);
  assert.match(h.el('#table-wrap').innerHTML, /Не удалось загрузить текущий набор данных/);
  assert.match(h.el('#table-wrap').innerHTML, /error-state/);
  assert.equal(h.el('#page-label').textContent, '0–0 из 0');
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

test('analysis renders honest categorization fallback, confidence, route, and manual review', async () => {
  const h = await harness();
  h.run(`state.integrations = {classification: "model-missing", routing: "ready", similarity: "ready", analytics: "ready"};
    renderIntegrationStatuses(); renderAnalysis({
    category: {category: null, confidence: 0, explanation: "Модель отсутствует.", limitation: "Категоризация недоступна: model-missing", needs_manual_review: true},
    routing: {support_line: "2 линия", confidence: .82, explanation: "Похожий профиль обращения.", needs_manual_review: false},
    similar_appeals: [{record_id: "model-17", score: .74, category: "Почта", support_line: "2 линия", resolution: "Проверить очередь"}],
    manual_review_required: true
  })`);
  assert.equal(h.el('#classification-status').textContent, 'Статус модели: отсутствует');
  assert.equal(h.el('#category-value').textContent, 'Не определена');
  assert.equal(h.el('#category-confidence-label').textContent, 'Уверенность: 0%');
  assert.equal(h.el('#category-limitation').classList.contains('hidden'), false);
  assert.equal(h.el('#category-review').classList.contains('hidden'), false);
  assert.equal(h.el('#routing-status').textContent, 'Маршрутизация: готова');
  assert.equal(h.el('#line-value').textContent, '2 линия');
  assert.equal(h.el('#line-confidence-label').textContent, 'Уверенность: 82%');
  assert.equal(h.el('#manual-review').classList.contains('hidden'), false);
  assert.equal(h.el('#similarity-status').textContent, 'Поиск: доступен');
  assert.equal(h.el('#similar-list').children.length, 1);
  assert.match(h.el('#similar-list').children[0].innerHTML, /ID в индексе модели: model-17/);
  assert.doesNotMatch(h.el('#similar-list').children[0].innerHTML, /Открыть исходную запись/);
});

test('analysis refreshes lazy integration statuses before showing the result', async () => {
  const h = await harness();
  const button = new Element();
  h.context.event = { preventDefault() {}, currentTarget: { querySelector: () => button } };
  const pending = h.run('submitAppeal(event)');
  h.reply(0, {
    category: { category: null, confidence: 0, explanation: 'Модель отсутствует.', limitation: 'model-missing', needs_manual_review: true },
    routing: { support_line: '3 линия', confidence: .76, explanation: 'Маршрут найден.', needs_manual_review: false },
    similar_appeals: [], manual_review_required: true,
  });
  await flush();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].url, '/api/integrations');
  h.reply(1, { classification: 'model-missing', routing: 'ready', similarity: 'unavailable:inference-error', analytics: 'ready' });
  await pending;
  assert.equal(h.el('#classification-status').textContent, 'Статус модели: отсутствует');
  assert.equal(h.el('#routing-status').textContent, 'Маршрутизация: готова');
  assert.equal(h.el('#similarity-status').textContent, 'Поиск: ошибка вычислений');
  assert.match(h.el('#similar-list').innerHTML, /Поиск похожих обращений недоступен/);
  assert.match(h.el('#similar-list').innerHTML, /error-state/);
  assert.equal(button.disabled, false);
});

test('a late integration response cannot overwrite a newer post-analysis status', async () => {
  const h = await harness();
  const old = h.run('loadIntegrations()');
  const current = h.run('loadIntegrations()');
  h.reply(1, { classification: 'model-missing', routing: 'ready', similarity: 'ready', analytics: 'ready' });
  await current;
  h.reply(0, { classification: 'uninitialized', routing: 'pending', similarity: 'pending', analytics: 'ready' });
  await old;
  assert.equal(h.el('#classification-status').textContent, 'Статус модели: отсутствует');
  assert.equal(h.el('#routing-status').textContent, 'Маршрутизация: готова');
  assert.equal(h.el('#similarity-status').textContent, 'Поиск: доступен');
});

test('ready similarity search distinguishes a real empty result from an unavailable search', async () => {
  const h = await harness();
  h.run(`state.integrations = {classification: "ready", routing: "ready", similarity: "ready", analytics: "ready"};
    renderIntegrationStatuses(); renderAnalysis({
      category: {category: "A", confidence: .91, explanation: "Категория найдена.", needs_manual_review: false},
      routing: {support_line: "1 линия", confidence: .88, explanation: "Маршрут найден.", needs_manual_review: false},
      similar_appeals: [], manual_review_required: false
    })`);
  assert.match(h.el('#similar-list').innerHTML, /Совпадений не найдено/);
  assert.doesNotMatch(h.el('#similar-list').innerHTML, /error-state/);
  assert.equal(h.el('#manual-review').classList.contains('hidden'), true);
});

test('SLA analytics renders metrics, distributions, empty, loading, and error states', async () => {
  const h = await harness();
  h.run(`renderAnalytics({
    status: "ready", total_appeals: 4, overdue_count: 2, overdue_share: .5,
    mean_sla_h: 4.25, median_sla_h: 3,
    category_distribution: [{value: "Категория A", count: 3, share: .75}],
    line_distribution: [{value: "2 линия", count: 2, share: .5}],
    message: "Расчёт готов."
  })`);
  assert.equal(h.el('#kpi-total').textContent, '4');
  assert.equal(h.el('#kpi-overdue').textContent, '2 · 50%');
  assert.match(h.el('#kpi-mean-sla').textContent, /^4[,.]3 ч$/);
  assert.equal(h.el('#kpi-median-sla').textContent, '3 ч');
  assert.match(h.el('#category-distribution').innerHTML, /Категория A/);
  assert.match(h.el('#line-distribution').innerHTML, /2 линия/);

  h.run('setAnalyticsLoading()');
  assert.equal(h.el('#kpi-status').textContent, 'Загрузка…');
  assert.equal(h.el('#kpi-total').textContent, '—');
  assert.equal(h.el('#kpi-overdue').textContent, '—');
  assert.match(h.el('#category-distribution').innerHTML, /Загрузка/);

  h.run('renderAnalytics({status: "no-data", total_appeals: 0, overdue_share: null, message: "Загрузите Excel."})');
  assert.equal(h.el('#kpi-status').textContent, 'Нет данных');
  assert.match(h.el('#category-distribution').innerHTML, /Загрузите данные/);

  h.run('renderAnalyticsError("Server unavailable")');
  assert.equal(h.el('#kpi-status').textContent, 'Ошибка');
  assert.match(h.el('#analytics-message').textContent, /Server unavailable/);
  assert.match(h.el('#category-distribution').innerHTML, /error-state/);
});
