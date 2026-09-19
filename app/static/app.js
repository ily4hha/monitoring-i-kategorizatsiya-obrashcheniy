const state = {
  dataset: null,
  integrations: null,
  integrationsRequest: 0,
  offset: 0,
  limit: 20,
  total: 0,
  query: "",
  recordsRequest: 0,
  recordsAbort: null,
  recordsLoading: false,
  analyticsOptionsInitialized: false,
  analyticsRequest: 0,
};

const reservedColumns = new Set(["_record_id", "record_id", "_sheet", "_source_row"]);
const binaryFields = new Set(["is_overdue", "is_multiline", "is_high_clarifications", "has_datetime_anomaly", "lines_count_changed"]);
const dateFields = new Set(["reg_dt", "plan_dt", "deadline_dt", "fact_dt", "status_entry_dt", "last_modified_dt", "Дата регистрации"]);
const fieldLabels = {
  ticket_id: "Номер обращения", reg_dt: "Дата регистрации", plan_dt: "Плановый срок",
  deadline_dt: "Крайний срок", fact_dt: "Дата завершения", status_entry_dt: "Вход в текущий статус",
  last_modified_dt: "Последнее изменение", status: "Статус", user_type: "Пользователь",
  service: "Услуга", service_component: "Компонент услуги", category_original: "Вид обращения",
  category_grouped: "Группа обращений", request_type: "Тип обращения", criticality: "Критичность",
  urgency: "Срочность", priority: "Приоритет", service_class: "Класс обслуживания",
  timezone: "Часовой пояс", text_clean: "Описание", norm_sla_h: "Норматив SLA, ч",
  fact_sla_h: "Фактический SLA, ч", fact_sla_source: "Источник расчёта SLA",
  line_1_react_h: "Реакция 1-й линии, ч", line_1_work_h: "Работа 1-й линии, ч",
  line_2_react_h: "Реакция 2-й линии, ч", line_2_work_h: "Работа 2-й линии, ч",
  line_3_react_h: "Реакция 3-й линии, ч", line_3_work_h: "Работа 3-й линии, ч",
  line_4_react_h: "Реакция 4-й линии, ч", line_4_work_h: "Работа 4-й линии, ч",
  clarification_h: "Время уточнений, ч", resolution: "Результат работ", resolved_line: "Линия решения",
  clarifications_cnt: "Количество уточнений", total_work_h: "Общее время работы, ч",
  total_react_h: "Общее время реакции, ч", lines_count: "Задействовано линий",
  is_multiline: "Несколько линий", lines_count_changed: "Пересчёт количества линий",
  is_overdue: "Просрочено", is_high_clarifications: "Много уточнений",
  total_lifecycle_h: "Полный цикл, ч", has_datetime_anomaly: "Требуется проверка дат",
  "Номер запроса": "Номер обращения", "Описание 2": "Описание", "Услуга": "Услуга",
  "Компонент услуги 1 уровня": "Компонент услуги", "Вид запроса": "Вид обращения",
  "Кем решен (группа)": "Линия решения", "Результат работ": "Результат работ",
  "Дата регистрации": "Дата регистрации", "Приоритет": "Приоритет", "Тема": "Тема", Text: "Текст",
};
const preferredHistoryColumns = [
  "Номер запроса", "ticket_id", "Тема", "subject", "Дата регистрации", "reg_dt",
  "Услуга", "service", "Вид запроса", "category_original", "Приоритет", "priority",
  "Кем решен (группа)", "resolved_line", "Результат работ", "resolution",
];
const $ = (selector) => document.querySelector(selector);

function showNotice(message, type = "info") {
  const notice = $("#notice");
  notice.textContent = friendlyText(message);
  notice.className = `notice${type === "error" ? " error" : ""}`;
  clearTimeout(showNotice.timeout);
  showNotice.timeout = setTimeout(() => notice.classList.add("hidden"), 6000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = response.status >= 500 ? "Сервис временно недоступен. Попробуйте ещё раз." : "Не удалось выполнить запрос.";
    try { message = formatApiDetail((await response.json()).detail) || message; } catch (_) {}
    throw new Error(friendlyText(message));
  }
  return response.json();
}

function formatApiDetail(detail) {
  if (typeof detail === "string") return friendlyText(detail);
  if (Array.isArray(detail)) return friendlyText(detail.map((item) => item?.msg || item?.message || "Некорректное значение").join("; "));
  if (detail && typeof detail === "object") return friendlyText(detail.msg || detail.message || "Проверьте введённые данные.");
  return "";
}

function friendlyText(value) {
  return String(value ?? "")
    .replace(/partial-data/gi, "доступны не все показатели")
    .replace(/model-incompatible/gi, "компонент анализа требует настройки")
    .replace(/model-missing/gi, "автоматическая рекомендация пока недоступна")
    .replace(/inference-error/gi, "ошибка расчёта")
    .replace(/fact_sla_h/gi, "фактический SLA")
    .replace(/is_overdue/gi, "признак просрочки")
    .replace(/category_grouped/gi, "группа обращений")
    .replace(/category_original/gi, "вид обращения")
    .replace(/resolved_line/gi, "линия решения")
    .replace(/is_multiline/gi, "несколько линий")
    .replace(/is_high_clarifications/gi, "много уточнений")
    .replace(/total_work_h/gi, "общее время работы")
    .replace(/total_react_h/gi, "общее время реакции")
    .replace(/reg_dt/gi, "дата регистрации");
}

function setActiveView(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
  $(`#${name}-view`).classList.add("active");
}

async function loadCurrentDataset() {
  try {
    state.dataset = await api("/api/datasets/current");
  } catch (error) {
    state.dataset = null;
    state.total = 0;
    renderDatasetStatus();
    renderAnalyticsError(error.message);
    renderEmptyTable(`Не удалось загрузить историю обращений: ${error.message}`, true);
    showNotice(`Не удалось загрузить историю обращений: ${error.message}`, "error");
    return;
  }
  renderDatasetStatus();
  state.analyticsOptionsInitialized = false;
  await Promise.all([loadAnalytics(true), loadRecords()]);
}

async function loadIntegrations(showErrorNotice = true) {
  const requestId = ++state.integrationsRequest;
  try {
    const integrations = await api("/api/integrations");
    if (requestId !== state.integrationsRequest) return state.integrations;
    state.integrations = integrations;
  } catch (error) {
    if (requestId !== state.integrationsRequest) return state.integrations;
    state.integrations = { classification: "status-error", routing: "status-error", similarity: "status-error", analytics: "status-error" };
    if (showErrorNotice) showNotice("Не удалось проверить доступность рекомендаций.", "error");
  }
  renderIntegrationStatuses();
  return state.integrations;
}

function integrationCode(status) {
  return String(status || "status-error").replace(/^unavailable:/, "");
}

function renderIntegrationStatus(selector, kind, status) {
  const code = integrationCode(status);
  const ready = { classification: "Категоризация доступна", routing: "Маршрут доступен", similarity: "Поиск доступен" };
  const waiting = { classification: "Категоризация запускается", routing: "Маршрут запускается", similarity: "Поиск запускается" };
  const unavailable = { classification: "Нужна ручная проверка", routing: "Нужна ручная проверка", similarity: "Поиск временно недоступен" };
  const badge = $(selector);
  badge.textContent = code === "ready" ? ready[kind] : ["pending", "uninitialized"].includes(code) ? waiting[kind] : unavailable[kind];
  const tone = code === "ready" ? "ready" : ["pending", "uninitialized"].includes(code) ? "loading" : "attention";
  badge.className = `model-status ${tone}`;
}

function renderIntegrationStatuses() {
  const integrations = state.integrations || {};
  renderIntegrationStatus("#classification-status", "classification", integrations.classification);
  renderIntegrationStatus("#routing-status", "routing", integrations.routing);
  renderIntegrationStatus("#similarity-status", "similarity", integrations.similarity);
}

function renderClassificationStatus(status) {
  renderIntegrationStatus("#classification-status", "classification", status);
}

function renderDatasetStatus() {
  const status = $("#dataset-status");
  status.classList.toggle("ready", Boolean(state.dataset));
  status.innerHTML = state.dataset
    ? `<i aria-hidden="true"></i> История загружена · ${Number(state.dataset.row_count).toLocaleString("ru-RU")} записей`
    : '<i aria-hidden="true"></i> История не загружена';
}

async function uploadDataset(file) {
  const body = new FormData();
  body.append("file", file);
  $("#dataset-status").innerHTML = '<i aria-hidden="true"></i> Загружаем историю…';
  try {
    state.dataset = await api("/api/datasets", { method: "POST", body });
    state.offset = 0;
    state.query = "";
    state.analyticsOptionsInitialized = false;
    $("#analytics-filters").reset();
    $("#record-search").value = "";
    renderDatasetStatus();
    showNotice(`История загружена: ${Number(state.dataset.row_count).toLocaleString("ru-RU")} записей.`);
    await Promise.all([loadAnalytics(true), loadRecords()]);
  } catch (error) {
    renderDatasetStatus();
    showNotice(`Не удалось загрузить историю: ${error.message}`, "error");
  } finally {
    $("#dataset-file").value = "";
  }
}

async function submitAppeal(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  payload.service ||= null;
  payload.component ||= null;
  payload.priority ||= null;
  button.disabled = true;
  button.textContent = "Анализируем…";
  $(".result-panel").setAttribute("aria-busy", "true");
  resetAnalysis("Анализируем обращение…");
  try {
    const result = await api("/api/appeals/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadIntegrations(false);
    renderAnalysis(result);
  } catch (error) {
    resetAnalysis(`Не удалось получить рекомендацию: ${error.message}`);
    showNotice(error.message, "error");
  } finally {
    $(".result-panel").setAttribute("aria-busy", "false");
    button.disabled = false;
    button.textContent = "Проанализировать";
  }
}

function resetAnalysis(message) {
  $("#result-grid").classList.add("hidden");
  $("#similar-section").classList.add("hidden");
  $("#manual-review").classList.add("hidden");
  $("#result-placeholder").textContent = friendlyText(message);
  $("#result-placeholder").classList.remove("hidden");
}

function renderAnalysis(result) {
  $("#result-placeholder").classList.add("hidden");
  $("#result-grid").classList.remove("hidden");
  $("#manual-review").classList.toggle("hidden", !result.manual_review_required);
  const reviewReasons = [];
  if (result.category.needs_manual_review || !result.category.category || result.category.confidence < 0.5) reviewReasons.push("категорию");
  if (result.routing.needs_manual_review || !result.routing.support_line || result.routing.confidence < 0.5) reviewReasons.push("маршрут");
  $("#manual-review-reason").textContent = result.manual_review_required
    ? `Перед назначением проверьте ${reviewReasons.join(" и ") || "результат анализа"}.`
    : "";
  $("#category-value").textContent = result.category.category || "Не определена";
  $("#category-confidence").style.width = `${Math.round(result.category.confidence * 100)}%`;
  $("#category-confidence-label").textContent = `Оценка модели: ${Math.round(result.category.confidence * 100)}%`;
  $("#category-explanation").textContent = friendlyText(result.category.explanation);
  $("#category-limitation").textContent = friendlyText(result.category.limitation || "");
  $("#category-limitation").classList.toggle("hidden", !result.category.limitation);
  $("#category-review").classList.toggle("hidden", !result.category.needs_manual_review);
  $("#line-value").textContent = result.routing.support_line || "Не определена";
  $("#line-confidence").style.width = `${Math.round(result.routing.confidence * 100)}%`;
  $("#line-confidence-label").textContent = `Оценка маршрута: ${Math.round(result.routing.confidence * 100)}%`;
  $("#line-explanation").textContent = friendlyText(result.routing.explanation);
  $("#line-review").classList.toggle("hidden", !result.routing.needs_manual_review);

  const list = $("#similar-list");
  list.replaceChildren();
  $("#similar-section").classList.remove("hidden");
  if (!result.similar_appeals.length) {
    const available = integrationCode(state.integrations?.similarity) === "ready";
    list.innerHTML = `<div class="empty-state compact${available ? "" : " error-state"}">${available ? "Похожих обращений не найдено." : "Поиск похожих обращений временно недоступен. Проверьте рекомендацию вручную."}</div>`;
    return;
  }
  result.similar_appeals.forEach((item) => {
    const card = document.createElement("article");
    card.className = "similar-item";
    card.innerHTML = `
      <div class="similar-number"><span>Номер</span><strong>${escapeHtml(item.appeal_number || "Без номера")}</strong></div>
      <dl><div><dt>Категория</dt><dd>${escapeHtml(item.category || "Не указана")}</dd></div><div><dt>Линия</dt><dd>${escapeHtml(item.support_line || "Не указана")}</dd></div><div class="resolution"><dt>Результат работ</dt><dd>${escapeHtml(item.resolution || "Не указан")}</dd></div></dl>
      <div class="similar-actions"><span class="similarity-score" title="${escapeHtml(item.score_description || "")}">Сходство ${Math.round(item.score * 100)}%</span><button class="secondary open-record" type="button" data-record-id="${escapeHtml(item.record_id)}">Открыть исходную запись</button></div>
      <div class="similar-explanation">${item.score_description ? `<p>${escapeHtml(item.score_description)}</p>` : ""}
      ${item.explanation ? `<p>${escapeHtml(item.explanation)}</p>` : ""}</div>`;
    list.append(card);
  });
}

function analyticsParams() {
  const params = new URLSearchParams();
  for (const name of ["date_from", "date_to", "service", "category", "priority", "line"]) {
    const value = $(`#analytics-filters [name="${name}"]`).value.trim();
    if (value) params.set(name, value);
  }
  return params;
}

async function loadAnalytics(captureOptions = false) {
  const requestId = ++state.analyticsRequest;
  setAnalyticsLoading();
  try {
    const params = analyticsParams();
    const overview = await api(`/api/analytics/overview${params.toString() ? `?${params}` : ""}`);
    if (requestId !== state.analyticsRequest) return;
    if (captureOptions && !state.analyticsOptionsInitialized) populateAnalyticsFilters(overview);
    renderAnalytics(overview);
  } catch (error) {
    if (requestId !== state.analyticsRequest) return;
    renderAnalyticsError(error.message);
    showNotice(`Не удалось загрузить аналитику: ${error.message}`, "error");
  }
}

function populateAnalyticsFilters(overview) {
  const breakdowns = overview.sla_breakdowns || {};
  fillSelect("#service-filter", breakdowns.services, "Все услуги");
  fillSelect("#category-filter", breakdowns.categories?.length ? breakdowns.categories : breakdowns.category_groups, "Все виды");
  fillSelect("#priority-filter", breakdowns.priorities, "Все приоритеты");
  fillSelect("#line-filter", breakdowns.lines, "Все линии");
  state.analyticsOptionsInitialized = true;
}

function fillSelect(selector, items = [], allLabel) {
  const select = $(selector);
  const current = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = allLabel;
  select.append(all);
  [...new Set(items.map((item) => item.value).filter((value) => value && value !== "Не указано"))].sort((a, b) => a.localeCompare(b, "ru")).forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
  select.value = current;
}

function setAnalyticsLoading() {
  for (const selector of ["#kpi-total", "#kpi-overdue", "#kpi-mean-sla", "#kpi-median-sla", "#kpi-multiline", "#kpi-clarifications"]) $(selector).textContent = "—";
  $("#kpi-status").textContent = "Обновляем…";
  $("#analytics-message").textContent = "Считаем показатели…";
  renderDistribution("#category-distribution", [], "Загрузка…");
  renderDistribution("#line-distribution", [], "Загрузка…");
  for (const selector of ["#sla-services", "#sla-categories", "#sla-priorities", "#sla-lines", "#risk-categories", "#risk-lines"]) $(selector).innerHTML = '<div class="empty-state compact">Загрузка…</div>';
}

function renderAnalytics(overview) {
  const noData = overview.status === "no-data";
  $("#kpi-total").textContent = Number(overview.total_appeals || 0).toLocaleString("ru-RU");
  $("#kpi-overdue").textContent = overview.overdue_share == null ? "—" : `${overview.overdue_count ?? "—"} · ${formatPercent(overview.overdue_share)}`;
  $("#kpi-mean-sla").textContent = formatHours(overview.mean_sla_h);
  $("#kpi-median-sla").textContent = formatHours(overview.median_sla_h);
  $("#kpi-multiline").textContent = formatCount(overview.multiline_count);
  $("#kpi-clarifications").textContent = formatCount(overview.high_clarifications_count);
  $("#kpi-status").textContent = noData ? "Нет данных" : overview.status === "ready" ? "Данные полные" : "Часть показателей недоступна";
  $("#analytics-message").textContent = noData
    ? (state.dataset ? "По выбранным фильтрам обращений не найдено." : "Загрузите историю обращений, чтобы построить аналитику.")
    : "Показатели рассчитаны по выбранной исторической выборке.";
  const emptyMessage = noData ? "Нет данных для выбранных условий." : "Подходящих значений нет.";
  renderDistribution("#category-distribution", overview.category_distribution || [], emptyMessage);
  renderDistribution("#line-distribution", overview.line_distribution || [], emptyMessage);
  const breakdowns = overview.sla_breakdowns || {};
  renderBreakdown("#sla-services", breakdowns.services || [], emptyMessage);
  renderBreakdown("#sla-categories", breakdowns.categories?.length ? breakdowns.categories : breakdowns.category_groups || [], emptyMessage);
  renderBreakdown("#sla-priorities", breakdowns.priorities || [], emptyMessage);
  renderBreakdown("#sla-lines", breakdowns.lines || [], emptyMessage);
  const risk = overview.historical_sla_risk || {};
  $("#risk-note").textContent = "Риск рассчитан по прошлым обращениям и не является прогнозом будущих нарушений.";
  renderRisk("#risk-categories", risk.categories || [], emptyMessage);
  renderRisk("#risk-lines", risk.lines || [], emptyMessage);
}

function renderAnalyticsError(message) {
  for (const selector of ["#kpi-total", "#kpi-overdue", "#kpi-mean-sla", "#kpi-median-sla", "#kpi-multiline", "#kpi-clarifications"]) $(selector).textContent = "—";
  $("#kpi-status").textContent = "Не удалось обновить";
  $("#analytics-message").textContent = `Аналитика временно недоступна: ${friendlyText(message)}`;
  renderDistribution("#category-distribution", [], "Не удалось загрузить данные.", true);
  renderDistribution("#line-distribution", [], "Не удалось загрузить данные.", true);
  for (const selector of ["#sla-services", "#sla-categories", "#sla-priorities", "#sla-lines", "#risk-categories", "#risk-lines"]) $(selector).innerHTML = '<div class="empty-state compact error-state">Не удалось загрузить данные.</div>';
}

function renderDistribution(selector, items, emptyMessage, isError = false) {
  const container = $(selector);
  if (!items.length) {
    container.innerHTML = `<div class="empty-state compact${isError ? " error-state" : ""}">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  container.innerHTML = items.slice(0, 10).map((item) => {
    const percent = Math.round(item.share * 100);
    return `<div class="distribution-item"><span title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</span><strong>${Number(item.count).toLocaleString("ru-RU")} · ${percent}%</strong><div class="distribution-bar" aria-hidden="true"><i style="width:${percent}%"></i></div></div>`;
  }).join("");
}

function renderBreakdown(selector, items, emptyMessage) {
  const container = $(selector);
  if (!items.length) {
    container.innerHTML = `<div class="empty-state compact">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  container.innerHTML = `<div class="metric-row metric-head"><span>Группа</span><span>Обращения</span><span>Средний SLA</span><span>Просрочено</span></div>${items.slice(0, 8).map((item) => `<div class="metric-row"><strong title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</strong><span>${Number(item.total_appeals ?? item.count ?? 0).toLocaleString("ru-RU")}</span><span>${formatHours(item.mean_sla_h)}</span><span>${formatPercent(item.overdue_share)}</span></div>`).join("")}`;
}

function renderRisk(selector, items, emptyMessage) {
  const elevated = items.filter((item) => item.is_elevated_historical_risk);
  const container = $(selector);
  if (!elevated.length) {
    container.innerHTML = `<div class="empty-state compact">${items.length ? "Повышенный риск в достаточных по размеру группах не выявлен." : escapeHtml(emptyMessage)}</div>`;
    return;
  }
  container.innerHTML = elevated.slice(0, 6).map((item) => `<div class="risk-item"><div><strong>${escapeHtml(item.value)}</strong><span>${Number(item.overdue_sample_size).toLocaleString("ru-RU")} обращений в расчёте</span></div><div class="risk-value"><strong>${formatPercent(item.overdue_share)}</strong><span>просрочено</span></div></div>`).join("");
}

function formatHours(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ч`;
}

function formatPercent(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${Math.round(Number(value) * 100)}%`;
}

function formatCount(value) {
  return value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("ru-RU");
}

async function loadRecords() {
  clearTimeout(state.searchTimer);
  if (!state.dataset) {
    state.total = 0;
    $("#records-summary").textContent = "Загрузите историю обращений, чтобы начать поиск.";
    return renderEmptyTable("История обращений пока не загружена.");
  }
  state.recordsAbort?.abort();
  const controller = new AbortController();
  state.recordsAbort = controller;
  const requestId = ++state.recordsRequest;
  state.recordsLoading = true;
  renderEmptyTable("Загружаем обращения…");
  renderPagination();
  const expected = { datasetId: state.dataset.dataset_id, query: state.query, offset: state.offset };
  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.query) params.set("q", state.query);
  try {
    const page = await api(`/api/datasets/${state.dataset.dataset_id}/records?${params}`, { signal: controller.signal });
    if (requestId !== state.recordsRequest || !state.dataset || state.dataset.dataset_id !== expected.datasetId || state.query !== expected.query || state.offset !== expected.offset) return;
    state.total = page.total;
    const lastOffset = Math.max(0, Math.floor((page.total - 1) / state.limit) * state.limit);
    if (state.offset > lastOffset) {
      state.offset = lastOffset;
      return loadRecords();
    }
    $("#records-summary").textContent = state.query ? `Найдено: ${Number(page.total).toLocaleString("ru-RU")}` : `Всего записей: ${Number(page.total).toLocaleString("ru-RU")}`;
    renderRecords(page.items);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId === state.recordsRequest) renderEmptyTable(`Не удалось загрузить обращения: ${error.message}`, true);
  } finally {
    if (requestId === state.recordsRequest) {
      state.recordsLoading = false;
      renderPagination();
    }
  }
}

function historyColumns() {
  const available = state.dataset?.columns || [];
  const selected = [];
  const labels = new Set();
  for (const name of [...preferredHistoryColumns, ...available]) {
    if (!available.includes(name) || reservedColumns.has(name)) continue;
    const label = fieldLabel(name);
    if (labels.has(label)) continue;
    selected.push(name);
    labels.add(label);
    if (selected.length === 6) break;
  }
  return selected;
}

function renderRecords(items) {
  if (!items.length) return renderEmptyTable("По вашему запросу ничего не найдено.");
  const columns = historyColumns();
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr>${columns.map((name) => `<th>${escapeHtml(fieldLabel(name))}</th>`).join("")}<th><span class="visually-hidden">Действие</span></th></tr></thead>`;
  const body = document.createElement("tbody");
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = `${columns.map((name) => `<td title="${escapeHtml(formatRecordValue(name, item[name]))}">${escapeHtml(formatRecordValue(name, item[name]))}</td>`).join("")}<td><button class="record-link" type="button" data-id="${escapeHtml(item._record_id)}">Открыть</button></td>`;
    body.append(row);
  });
  table.append(body);
  const wrap = $("#table-wrap");
  wrap.replaceChildren(table);
  wrap.querySelectorAll(".record-link").forEach((button) => button.addEventListener("click", () => openRecord(button.dataset.id)));
}

function renderEmptyTable(message, isError = false) {
  $("#table-wrap").innerHTML = `<div class="empty-state${isError ? " error-state" : ""}">${escapeHtml(friendlyText(message))}</div>`;
  renderPagination();
}

function renderPagination() {
  const start = state.total ? state.offset + 1 : 0;
  const end = Math.min(state.offset + state.limit, state.total);
  $("#page-label").textContent = state.recordsLoading ? "Загрузка…" : `${start}–${end} из ${state.total}`;
  $("#prev-page").disabled = state.recordsLoading || state.offset === 0;
  $("#next-page").disabled = state.recordsLoading || state.offset + state.limit >= state.total;
}

function changePage(direction) {
  if (state.recordsLoading || !state.dataset) return;
  const lastOffset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit);
  const offset = Math.max(0, Math.min(lastOffset, state.offset + direction * state.limit));
  if (offset === state.offset) return;
  state.offset = offset;
  loadRecords();
}

async function openRecord(id) {
  try {
    const item = await api(`/api/records/${id}`);
    const details = $("#record-details");
    details.replaceChildren();
    const shownLabels = new Set();
    Object.entries(item).filter(([key, value]) => {
      if (reservedColumns.has(key)) return false;
      if (/^Unnamed:\s*\d+$/i.test(key) && (value == null || value === "")) return false;
      const label = fieldLabel(key);
      if (shownLabels.has(label)) return false;
      shownLabels.add(label);
      return true;
    }).forEach(([key, value]) => {
      const term = document.createElement("dt");
      term.textContent = fieldLabel(key);
      const description = document.createElement("dd");
      description.textContent = formatRecordValue(key, value);
      details.append(term, description);
    });
    const number = item["Номер запроса"] || item.ticket_id;
    $("#record-dialog-title").textContent = number ? `Обращение ${number}` : "Исходная запись";
    $("#record-dialog").showModal();
  } catch (error) {
    showNotice(`Не удалось открыть обращение: ${error.message}`, "error");
  }
}

function fieldLabel(key) {
  if (fieldLabels[key]) return fieldLabels[key];
  const unnamed = String(key).match(/^Unnamed:\s*(\d+)$/i);
  if (unnamed) return `Дополнительное поле ${unnamed[1]}`;
  const cleaned = String(key).replace(/^[_*]+/, "").replaceAll("_", " ").trim();
  return cleaned ? cleaned.charAt(0).toUpperCase() + cleaned.slice(1) : "Дополнительное поле";
}

function formatRecordValue(key, value) {
  if (value == null || value === "") return "—";
  if (binaryFields.has(key) && [0, 1, "0", "1"].includes(value)) return Number(value) === 1 ? "Да" : "Нет";
  if (dateFields.has(key)) {
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) return parsed.toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
  }
  if (key === "fact_sla_source") {
    return { source: "Из исходной записи", reconstructed_from_lines: "Рассчитано по времени линий", missing: "Нет данных" }[value] || "Не указано";
  }
  return typeof value === "object" ? JSON.stringify(value) : friendlyText(value);
}

function formatValue(value) {
  return formatRecordValue("", value);
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setActiveView(tab.dataset.view));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const tabs = [...document.querySelectorAll(".tab")];
    const next = (tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].click();
    tabs[next].focus();
  });
});
$(".brand").addEventListener("click", () => setActiveView("operator"));
$("#dataset-file").addEventListener("change", (event) => event.target.files[0] && uploadDataset(event.target.files[0]));
$("#appeal-form").addEventListener("submit", submitAppeal);
$("#prev-page").addEventListener("click", () => changePage(-1));
$("#next-page").addEventListener("click", () => changePage(1));
$("#record-search").addEventListener("input", (event) => {
  state.recordsAbort?.abort();
  state.recordsRequest++;
  clearTimeout(state.searchTimer);
  state.query = event.target.value.trim();
  state.offset = 0;
  state.recordsLoading = Boolean(state.dataset);
  renderEmptyTable(state.dataset ? "Ищем обращения…" : "История обращений пока не загружена.");
  state.searchTimer = setTimeout(() => loadRecords(), 300);
});
$("#similar-list").addEventListener("click", (event) => {
  const button = event.target.closest?.(".open-record");
  if (button) openRecord(button.dataset.recordId);
});
$("#analytics-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  const from = $("#date-from").value;
  const to = $("#date-to").value;
  if (from && to && from > to) {
    showNotice("Дата начала периода не может быть позже даты окончания.", "error");
    return;
  }
  loadAnalytics(false);
});
$("#reset-analytics-filters").addEventListener("click", () => {
  $("#analytics-filters").reset();
  loadAnalytics(false);
});
$("#close-dialog").addEventListener("click", () => $("#record-dialog").close());
$("#record-dialog").addEventListener("click", (event) => {
  if (event.target === $("#record-dialog")) $("#record-dialog").close();
});
Promise.all([loadCurrentDataset(), loadIntegrations()]).catch(() => showNotice("Не удалось загрузить данные сервиса.", "error"));
