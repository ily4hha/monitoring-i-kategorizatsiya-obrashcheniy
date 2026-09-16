const state = { dataset: null, offset: 0, limit: 20, total: 0, query: "" };
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
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function setActiveView(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
  $(`#${name}-view`).classList.add("active");
}

async function loadCurrentDataset() {
  state.dataset = await api("/api/datasets/current");
  renderDatasetStatus();
  await Promise.all([loadAnalytics(), loadRecords()]);
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
  payload.priority ||= null;
  button.disabled = true;
  button.textContent = "Анализируем…";
  try {
    const result = await api("/api/appeals/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderAnalysis(result);
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Проанализировать";
  }
}

function renderAnalysis(result) {
  $("#result-placeholder").classList.add("hidden");
  $("#result-grid").classList.remove("hidden");
  $("#manual-review").classList.toggle("hidden", !result.manual_review_required);
  $("#category-value").textContent = result.category.category || "Не определена";
  $("#category-confidence").style.width = `${Math.round(result.category.confidence * 100)}%`;
  $("#category-explanation").textContent = `${result.category.explanation} Уверенность: ${Math.round(result.category.confidence * 100)}%.`;
  $("#line-value").textContent = result.routing.support_line || "Не определена";
  $("#line-confidence").style.width = `${Math.round(result.routing.confidence * 100)}%`;
  $("#line-explanation").textContent = `${result.routing.explanation} Уверенность: ${Math.round(result.routing.confidence * 100)}%.`;

  const section = $("#similar-section");
  const list = $("#similar-list");
  list.replaceChildren();
  section.classList.remove("hidden");
  if (!result.similar_appeals.length) {
    list.innerHTML = '<div class="empty-state">Модуль поиска похожих обращений пока не подключён.</div>';
    return;
  }
  result.similar_appeals.forEach((item) => {
    const card = document.createElement("article");
    card.className = "similar-item";
    card.innerHTML = `<strong>${escapeHtml(item.category || "Без категории")}</strong><p>${escapeHtml(item.resolution || "Результат не указан")}</p>`;
    list.append(card);
  });
}

async function loadAnalytics() {
  const overview = await api("/api/analytics/overview");
  $("#kpi-total").textContent = overview.total_appeals.toLocaleString("ru-RU");
  $("#kpi-overdue").textContent = overview.overdue_share == null ? "—" : `${Math.round(overview.overdue_share * 100)}%`;
  $("#kpi-status").textContent = overview.status === "integration-pending" ? "Ожидает модуль" : "Нет данных";
}

async function loadRecords() {
  if (!state.dataset) return renderEmptyTable("Загрузите Excel, чтобы увидеть историю.");
  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.query) params.set("q", state.query);
  try {
    const page = await api(`/api/datasets/${state.dataset.dataset_id}/records?${params}`);
    state.total = page.total;
    renderRecords(page.items);
    renderPagination();
  } catch (error) {
    renderEmptyTable(error.message);
  }
}

function renderRecords(items) {
  if (!items.length) return renderEmptyTable("По вашему запросу ничего не найдено.");
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

function renderEmptyTable(message) {
  $("#table-wrap").innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
  state.total = 0;
  renderPagination();
}

function renderPagination() {
  const start = state.total ? state.offset + 1 : 0;
  const end = Math.min(state.offset + state.limit, state.total);
  $("#page-label").textContent = `${start}–${end} из ${state.total}`;
  $("#prev-page").disabled = state.offset === 0;
  $("#next-page").disabled = state.offset + state.limit >= state.total;
}

async function openRecord(id) {
  try {
    const item = await api(`/api/records/${id}`);
    const details = $("#record-details");
    details.replaceChildren();
    Object.entries(item).filter(([key]) => !key.startsWith("_")).forEach(([key, value]) => {
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
$("#prev-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadRecords(); });
$("#next-page").addEventListener("click", () => { state.offset += state.limit; loadRecords(); });
$("#record-search").addEventListener("input", (event) => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => { state.query = event.target.value.trim(); state.offset = 0; loadRecords(); }, 300);
});
$("#close-dialog").addEventListener("click", () => $("#record-dialog").close());
loadCurrentDataset().catch((error) => showNotice(error.message, "error"));

