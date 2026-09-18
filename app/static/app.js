const state = { dataset: null, integrations: null, integrationsRequest: 0, offset: 0, limit: 20, total: 0, query: "", recordsRequest: 0, recordsAbort: null, recordsLoading: false };
const reservedColumns = new Set(["_record_id", "_sheet", "_source_row"]);
const $ = (selector) => document.querySelector(selector);

function showNotice(message, type = "info") {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = `notice${type === "error" ? " error" : ""}`;
  clearTimeout(showNotice.timeout);
  showNotice.timeout = setTimeout(() => notice.classList.add("hidden"), 6000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try { message = formatApiDetail((await response.json()).detail) || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function formatApiDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item?.msg || item?.message || JSON.stringify(item)).join("; ");
  if (detail && typeof detail === "object") return detail.msg || detail.message || JSON.stringify(detail);
  return "";
}

function setActiveView(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
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
    renderEmptyTable(`Не удалось загрузить текущий набор данных: ${error.message}`, true);
    showNotice(`Не удалось загрузить текущий набор данных: ${error.message}`, "error");
    return;
  }
  renderDatasetStatus();
  await Promise.all([loadAnalytics(), loadRecords()]);
}

async function loadIntegrations(showErrorNotice = true) {
  const requestId = ++state.integrationsRequest;
  try {
    const integrations = await api("/api/integrations");
    if (requestId !== state.integrationsRequest) return state.integrations;
    state.integrations = integrations;
  } catch (error) {
    if (requestId !== state.integrationsRequest) return state.integrations;
    state.integrations = {
      classification: "status-error",
      routing: "status-error",
      similarity: "status-error",
      analytics: "status-error",
    };
    if (showErrorNotice) showNotice(`Не удалось получить статус интеграций: ${error.message}`, "error");
  }
  renderIntegrationStatuses();
  return state.integrations;
}

function integrationCode(status) {
  return String(status || "status-error").replace(/^unavailable:/, "");
}

function renderIntegrationStatus(selector, kind, status) {
  const code = integrationCode(status);
  const labels = {
    classification: {
      ready: "Статус модели: готова", "model-missing": "Статус модели: отсутствует",
      "metadata-missing": "Статус модели: нет метаданных",
      uninitialized: "Статус модели: ожидает проверки", pending: "Статус модели: ожидает запуска",
      "model-incompatible": "Статус модели: несовместима", "embedder-unavailable": "Статус модели: нет локальных весов",
      "inference-error": "Статус модели: ошибка вычислений", "status-error": "Статус модели: недоступен",
    },
    routing: {
      ready: "Маршрутизация: готова", "model-missing": "Маршрутизация: модель отсутствует",
      uninitialized: "Маршрутизация: ожидает проверки", pending: "Маршрутизация: ожидает запуска",
      "model-incompatible": "Маршрутизация: модель несовместима", "embedder-unavailable": "Маршрутизация: веса недоступны",
      "inference-error": "Маршрутизация: ошибка вычислений", "status-error": "Маршрутизация: недоступна",
    },
    similarity: {
      ready: "Поиск: доступен", "model-missing": "Поиск: модель отсутствует",
      uninitialized: "Поиск: ожидает проверки", pending: "Поиск: ожидает запуска",
      "model-incompatible": "Поиск: модель несовместима", "embedder-unavailable": "Поиск: веса недоступны",
      "inference-error": "Поиск: ошибка вычислений", "status-error": "Поиск: недоступен",
    },
  };
  const badge = $(selector);
  badge.textContent = labels[kind][code] || `${kind === "classification" ? "Статус модели" : kind === "routing" ? "Маршрутизация" : "Поиск"}: ${code}`;
  const tone = code === "ready" ? "ready" : ["pending", "uninitialized"].includes(code) ? "loading" : code === "model-missing" ? "missing" : "error";
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
  $("#dataset-status").textContent = state.dataset
    ? `${state.dataset.filename} · ${state.dataset.row_count} строк`
    : "Данные не загружены";
}

async function uploadDataset(file) {
  const body = new FormData();
  body.append("file", file);
  $("#dataset-status").textContent = "Загрузка…";
  try {
    state.dataset = await api("/api/datasets", { method: "POST", body });
    state.offset = 0;
    renderDatasetStatus();
    showNotice(`Загружено: ${state.dataset.row_count} строк, лист «${state.dataset.active_sheet}».`);
    await Promise.all([loadAnalytics(), loadRecords()]);
  } catch (error) {
    renderDatasetStatus();
    showNotice(error.message, "error");
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
    resetAnalysis(`Не удалось выполнить анализ: ${error.message}`);
    showNotice(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Проанализировать";
  }
}

function resetAnalysis(message) {
  $("#result-grid").classList.add("hidden");
  $("#similar-section").classList.add("hidden");
  $("#manual-review").classList.add("hidden");
  $("#result-placeholder").textContent = message;
  $("#result-placeholder").classList.remove("hidden");
}

function renderAnalysis(result) {
  $("#result-placeholder").classList.add("hidden");
  $("#result-grid").classList.remove("hidden");
  $("#manual-review").classList.toggle("hidden", !result.manual_review_required);
  const reviewReasons = [];
  if (result.category.needs_manual_review || !result.category.category || result.category.confidence < 0.5) reviewReasons.push("категория не подтверждена");
  if (result.routing.needs_manual_review || !result.routing.support_line || result.routing.confidence < 0.5) reviewReasons.push("маршрут не подтверждён");
  $("#manual-review").textContent = result.manual_review_required
    ? `Нужна ручная проверка: ${reviewReasons.join("; ") || "результат требует подтверждения"}.`
    : "";
  $("#category-value").textContent = result.category.category || "Не определена";
  $("#category-confidence").style.width = `${Math.round(result.category.confidence * 100)}%`;
  $("#category-confidence-label").textContent = `Уверенность: ${Math.round(result.category.confidence * 100)}%`;
  $("#category-explanation").textContent = result.category.explanation;
  $("#category-limitation").textContent = result.category.limitation || "";
  $("#category-limitation").classList.toggle("hidden", !result.category.limitation);
  $("#category-review").classList.toggle("hidden", !result.category.needs_manual_review);
  $("#line-value").textContent = result.routing.support_line || "Не определена";
  $("#line-confidence").style.width = `${Math.round(result.routing.confidence * 100)}%`;
  $("#line-confidence-label").textContent = `Уверенность: ${Math.round(result.routing.confidence * 100)}%`;
  $("#line-explanation").textContent = result.routing.explanation;
  $("#line-review").classList.toggle("hidden", !result.routing.needs_manual_review);

  const section = $("#similar-section");
  const list = $("#similar-list");
  list.replaceChildren();
  section.classList.remove("hidden");
  if (!result.similar_appeals.length) {
    const similarityCode = integrationCode(state.integrations?.similarity);
    const message = similarityCode === "ready"
      ? "Совпадений не найдено."
      : "Поиск похожих обращений недоступен; результат требует ручной проверки.";
    list.innerHTML = `<div class="empty-state${similarityCode === "ready" ? "" : " error-state"}">${message}</div>`;
    return;
  }
  result.similar_appeals.forEach((item) => {
    const card = document.createElement("article");
    card.className = "similar-item";
    card.innerHTML = `<strong>${escapeHtml(item.category || "Без категории")}</strong><p>${escapeHtml(item.resolution || "Решение не указано")}</p><p>Близость: ${Math.round(item.score * 100)}% · Линия: ${escapeHtml(item.support_line || "не указана")}</p><p class="source-reference">ID в индексе модели: ${escapeHtml(item.record_id)}</p>`;
    list.append(card);
  });
}

async function loadAnalytics() {
  setAnalyticsLoading();
  try {
    const overview = await api("/api/analytics/overview");
    renderAnalytics(overview);
  } catch (error) {
    renderAnalyticsError(error.message);
    showNotice(`Не удалось загрузить аналитику: ${error.message}`, "error");
  }
}

function setAnalyticsLoading() {
  for (const selector of ["#kpi-total", "#kpi-overdue", "#kpi-mean-sla", "#kpi-median-sla"]) $(selector).textContent = "—";
  $("#kpi-status").textContent = "Загрузка…";
  $("#analytics-message").textContent = "Рассчитываем SLA-метрики…";
  renderDistribution("#category-distribution", [], "Загрузка…");
  renderDistribution("#line-distribution", [], "Загрузка…");
}

function renderAnalytics(overview) {
  $("#kpi-total").textContent = overview.total_appeals.toLocaleString("ru-RU");
  $("#kpi-overdue").textContent = overview.overdue_share == null
    ? "—"
    : `${overview.overdue_count ?? "—"} · ${Math.round(overview.overdue_share * 100)}%`;
  $("#kpi-mean-sla").textContent = formatHours(overview.mean_sla_h);
  $("#kpi-median-sla").textContent = formatHours(overview.median_sla_h);
  $("#kpi-status").textContent = overview.status === "ready" ? "Готово" : overview.status === "partial-data" ? "Неполные данные" : "Нет данных";
  $("#analytics-message").textContent = overview.message || "Аналитика рассчитана по загруженному датасету.";
  const emptyMessage = overview.status === "no-data" ? "Загрузите данные для расчёта." : "В датасете нет подходящих значений.";
  renderDistribution("#category-distribution", overview.category_distribution || [], emptyMessage);
  renderDistribution("#line-distribution", overview.line_distribution || [], emptyMessage);
}

function renderAnalyticsError(message) {
  for (const selector of ["#kpi-total", "#kpi-overdue", "#kpi-mean-sla", "#kpi-median-sla"]) $(selector).textContent = "—";
  $("#kpi-status").textContent = "Ошибка";
  $("#analytics-message").textContent = `Не удалось загрузить SLA-аналитику: ${message}`;
  renderDistribution("#category-distribution", [], "Ошибка загрузки.", true);
  renderDistribution("#line-distribution", [], "Ошибка загрузки.", true);
}

function renderDistribution(selector, items, emptyMessage, isError = false) {
  const container = $(selector);
  if (!items.length) {
    container.innerHTML = `<div class="empty-state compact${isError ? " error-state" : ""}">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  container.innerHTML = items.map((item) => {
    const percent = Math.round(item.share * 100);
    return `<div class="distribution-item"><span title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</span><strong>${item.count} · ${percent}%</strong><div class="distribution-bar"><i style="width:${percent}%"></i></div></div>`;
  }).join("");
}

function formatHours(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} ч`;
}

async function loadRecords() {
  clearTimeout(state.searchTimer);
  if (!state.dataset) return renderEmptyTable("Загрузите Excel, чтобы увидеть историю.");
  state.recordsAbort?.abort();
  const controller = new AbortController();
  state.recordsAbort = controller;
  const requestId = ++state.recordsRequest;
  state.recordsLoading = true;
  renderEmptyTable("Загрузка записей…");
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
    renderRecords(page.items);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId === state.recordsRequest) renderEmptyTable(`Не удалось загрузить записи: ${error.message}`, true);
  } finally {
    if (requestId === state.recordsRequest) {
      state.recordsLoading = false;
      renderPagination();
    }
  }
}

function renderRecords(items) {
  if (!items.length) {
    return renderEmptyTable("По вашему запросу ничего не найдено.");
  }
  const columns = state.dataset.columns.slice(0, 6);
  const table = document.createElement("table");
  table.innerHTML = `<thead><tr><th>Запись</th>${columns.map((name) => `<th>${escapeHtml(name)}</th>`).join("")}</tr></thead>`;
  const body = document.createElement("tbody");
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.innerHTML = `<td><button class="record-link" data-id="${item._record_id}">Открыть</button></td>${columns.map((name) => `<td title="${escapeHtml(formatValue(item[name]))}">${escapeHtml(formatValue(item[name]))}</td>`).join("")}`;
    body.append(row);
  });
  table.append(body);
  const wrap = $("#table-wrap");
  wrap.replaceChildren(table);
  wrap.querySelectorAll(".record-link").forEach((button) => button.addEventListener("click", () => openRecord(button.dataset.id)));
}

function renderEmptyTable(message, isError = false) {
  $("#table-wrap").innerHTML = `<div class="empty-state${isError ? " error-state" : ""}">${escapeHtml(message)}</div>`;
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
    Object.entries(item).filter(([key]) => !reservedColumns.has(key)).forEach(([key, value]) => {
      const term = document.createElement("dt");
      term.textContent = key;
      const description = document.createElement("dd");
      description.textContent = formatValue(value);
      details.append(term, description);
    });
    $("#record-dialog").showModal();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function formatValue(value) {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => setActiveView(tab.dataset.view)));
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
  renderEmptyTable(state.dataset ? "Поиск…" : "Загрузите Excel, чтобы увидеть историю.");
  state.searchTimer = setTimeout(() => loadRecords(), 300);
});
$("#close-dialog").addEventListener("click", () => $("#record-dialog").close());
Promise.all([loadCurrentDataset(), loadIntegrations()]).catch((error) => showNotice(error.message, "error"));
