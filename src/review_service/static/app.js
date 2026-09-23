import {createDiffEditor} from './review_editor.bundle.js';

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const el = (tag, cls = '', text = '') => { const n = document.createElement(tag); n.className = cls; n.textContent = text; return n; };
const button = (text, cls, fn) => { const b = el('button', cls, text); b.type = 'button'; b.addEventListener('click', () => guard(fn)); return b; };
const tag = (text, cls = '') => el('span', `tag ${cls}`, text);
const key = () => crypto.randomUUID();
let token = sessionStorage.getItem('review-token') || '';
let state, displayed, displayedReport, view = 'overview', streamController, selected = new Map();
let sourceCache = [], chosenSessionList = [], chatItem = null, chatSnapshot = null, chatReport = null;
let unseen = false, onlyProblems = false, toastTimeout, liveMessages = new Map();
let reportSelection = sessionStorage.getItem('review-report-selection') || '';
let loadRevision = 0;
let modelDirty = false, modelSaving = false, modelCatalogRequested = false, modelCatalogLoading = false, modelCatalogRevision = 0;
let modelCatalog = [], modelActiveIndex = -1;
let overviewRoot, overviewRootKey = '', overviewTarget = {kind: 'summary', id: null};
let overviewFile = null, overviewLayer = null, overviewMode = 'frozen', overviewEditor = null, overviewEditorKey = '';
let overviewInitialContent = '', overviewUnsaved = false, overviewDiffRevision = 0;
let overviewSelection = [], overviewLineIds = [], chatTargetKind = 'all', chatTargetId = null;
let chatExpanded = false, uiRepo = '', overviewSplitRatio = .5, overviewResizeObserver, overviewEditorResizeObserver;
const draftLabel = draft => draft.status === 'running' ? 'Генерируется' : 'Не завершён';
function rememberReport(value) { reportSelection = value; sessionStorage.setItem('review-report-selection', value); }
function uiKey(name) { return `review-ui:${state.repo}:${name}`; }
function loadUiPreference(name) { try { return localStorage.getItem(uiKey(name)); } catch { return null; } }
function saveUiPreference(name, value) { try { localStorage.setItem(uiKey(name), String(value)); } catch { /* Storage may be unavailable. */ } }
function restoreUiPreferences() {
  if (uiRepo === state.repo) return;
  uiRepo = state.repo;
  chatExpanded = loadUiPreference('chat-expanded') === 'true';
  const savedRatio = loadUiPreference('detail-ratio');
  const ratio = savedRatio === null ? NaN : Number(savedRatio);
  overviewSplitRatio = Number.isFinite(ratio) && ratio >= 0 && ratio <= 1 ? ratio : .5;
  updateChatExpansion();
}
function updateChatExpansion() {
  const panel = $('#chat-panel') || $('#chat-panel', overviewRoot);
  if (!panel) return;
  panel.classList.toggle('collapsed', !chatExpanded);
  $('#chat-body', panel).inert = !chatExpanded;
  const toggle = $('#chat-close', panel);
  toggle.textContent = view === 'overview' ? (chatExpanded ? '⌄' : '⌃') : '✕';
  toggle.setAttribute('aria-label', view === 'overview' ? (chatExpanded ? 'Свернуть вопросы' : 'Развернуть вопросы') : 'Закрыть вопросы');
  toggle.setAttribute('aria-expanded', String(chatExpanded));
  $('.overview-center', overviewRoot)?.classList.toggle('chat-collapsed', !chatExpanded);
  requestAnimationFrame(updateOverviewSplit);
}
function setChatExpanded(expanded) {
  chatExpanded = expanded;
  if (state) saveUiPreference('chat-expanded', expanded);
  updateChatExpansion();
}

async function resolveReport() {
  if (!reportSelection) {
    const draft = state.report_drafts.find(d => d.id === state.active_report_draft_id);
    const latestDraft = state.report_drafts.at(-1);
    rememberReport(draft ? 'draft:' + draft.id : state.report ? 'report:' + state.report.id : latestDraft ? 'draft:' + latestDraft.id : '');
  }
  const choice = reportSelection;
  const [kind, id] = choice.split(':');
  if (!id) return null;
  let report;
  if (kind === 'draft') {
    const job = state.jobs.find(j => j.id === id);
    // A running job can still be preparing its snapshot, and a failed job may
    // never have created a draft. Only fetch drafts advertised by /state.
    if (!state.report_drafts.some(d => d.id === id)) {
      const reportId = job?.result?.report_id;
      if (job?.status === 'completed' && state.reports.some(r => r.id === reportId)) {
        rememberReport('report:' + reportId);
        return resolveReport();
      }
      if (job && ['running', 'queued'].includes(job.status)) return null;
      rememberReport('');
      return resolveReport();
    }
    report = await api('/report-drafts/' + id).catch(error => {
      if (error.status === 404) {
        state.report_drafts = state.report_drafts.filter(d => d.id !== id);
        if (state.jobs.some(j => j.id === id && ['running', 'queued'].includes(j.status))) return null;
        if (reportSelection === choice) rememberReport('');
        return resolveReport();
      }
      throw error;
    });
    if (report?.status === 'completed') {
      report = await api('/reports/' + report.report_id);
      if (reportSelection === choice) rememberReport('report:' + report.id);
    }
  } else if (state.reports.some(r => r.id === id)) report = await api('/reports/' + id);
  else { rememberReport(''); return resolveReport(); }
  if (report?.is_draft && displayedReport?.id === report.id && displayedReport.revision > report.revision) return displayedReport;
  return report;
}

async function api(path, method = 'GET', body) {
  const response = await fetch('/api/v1' + path, { method, headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) { if (response.status === 401) logout(); const error = new Error(data.error || 'Ошибка запроса'); error.status = response.status; throw error; }
  return data;
}
async function guard(fn) { try { await fn(); } catch (e) { toast(e.message); } }
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimeout); toastTimeout = setTimeout(() => $('#toast').hidden = true, 6500); }
function modal(title, content, actions = []) {
  $('#dialog-title').textContent = title; $('#dialog-body').replaceChildren(content);
  $('#dialog-actions').replaceChildren(...actions); if (!$('#dialog').open) $('#dialog').showModal();
}
$('#dialog-close').onclick = () => $('#dialog').close();
function empty(title, text) { const box = el('div', 'empty'); box.append(el('div', 'empty-icon', '◫'), el('h2', '', title), el('p', '', text)); return box; }
function logout() { token = ''; sessionStorage.removeItem('review-token'); streamController?.abort(); modelCatalogRevision++; modelCatalogRequested = false; modelCatalogLoading = false; modelDirty = false; modelCatalog = []; $('#model-options').replaceChildren(); setModelPopup(false); $('#workspace').hidden = true; $('#login').hidden = false; }
$('#logout').onclick = logout;
$('#login-form').onsubmit = async event => { event.preventDefault(); token = $('#token').value.trim(); try { await load(true); sessionStorage.setItem('review-token', token); $('#token').value = ''; connect(); } catch (e) { $('#login-error').textContent = e.message; } };

function setModelPopup(open) {
  $('#model-popup').hidden = !open;
  $('#model-input').setAttribute('aria-expanded', String(open));
  $('#model-open').setAttribute('aria-expanded', String(open));
  if (!open) { modelActiveIndex = -1; $('#model-input').removeAttribute('aria-activedescendant'); }
}
function renderModelOptions() {
  const query = modelDirty ? $('#model-input').value.trim().toLowerCase() : '';
  const models = modelCatalog.filter(id => id.toLowerCase().includes(query));
  const box = $('#model-options');
  box.replaceChildren(...models.map((id, index) => {
    const option = button(id, 'model-option', () => chooseModelOption(id));
    option.id = `model-option-${index}`;
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', String(id === $('#model-input').value));
    return option;
  }));
  if (!models.length) box.append(el('p', 'model-empty', modelCatalog.length ? 'Совпадений нет. Введите ID вручную.' : 'Введите ID модели вручную.'));
  modelActiveIndex = -1;
  $('#model-input').removeAttribute('aria-activedescendant');
}
function chooseModelOption(id) {
  $('#model-input').value = id;
  modelDirty = true;
  renderModelOptions(); renderModelSettings();
  $('#model-input').focus();
}
function moveModelOption(direction) {
  const options = $$('.model-option', $('#model-options'));
  if (!options.length) return;
  modelActiveIndex = (modelActiveIndex + direction + options.length) % options.length;
  $('#model-input').setAttribute('aria-activedescendant', options[modelActiveIndex].id);
  options[modelActiveIndex].scrollIntoView({block: 'nearest'});
}
function renderModelSettings() {
  if (!state) return;
  if (!modelDirty) $('#model-input').value = state.llm_model || '';
  $('#model-current').textContent = state.llm_configured
    ? `Сейчас: ${state.llm_model || 'модель не выбрана'}. Выбор применяется к новым отчётам и вопросам.`
    : 'Настройте OPENAI_API_KEY и OPENAI_BASE_URL на сервере.';
  $('#model-input').disabled = modelSaving || !state.llm_configured;
  $('#model-open').disabled = modelSaving || !state.llm_configured;
  $('#model-apply').disabled = modelSaving || !state.llm_configured || !$('#model-input').value.trim();
  $('#model-reset').disabled = modelSaving || !state.llm_configured;
  $('#model-reset').title = state.llm_default_model ? `REVIEW_MODEL: ${state.llm_default_model}` : 'REVIEW_MODEL не задана';
  $('#model-refresh').disabled = modelCatalogLoading || !state.llm_configured;
  $('#generate').disabled = modelSaving || !state.llm_available || state.jobs.some(j => j.kind === 'report' && ['queued', 'running'].includes(j.status));
  $('#generate').title = state.llm_available ? '' : 'Настройте OPENAI_API_KEY и выберите модель';
  $('#chat-form button[type="submit"]').disabled = modelSaving || !state.llm_available || (view === 'overview' && Boolean(displayedReport?.is_draft));
}
async function refreshModels() {
  const revision = ++modelCatalogRevision;
  modelCatalogRequested = true; modelCatalogLoading = true;
  $('#model-list-status').textContent = 'Загружаем список моделей…'; renderModelSettings();
  try {
    const data = await api('/models');
    if (revision !== modelCatalogRevision) return;
    modelCatalog = data.models; renderModelOptions();
    $('#model-list-status').textContent = data.models.length ? '' : 'Список пуст. Введите ID модели вручную.';
  } catch (error) {
    if (revision === modelCatalogRevision) $('#model-list-status').textContent = error.message;
  } finally {
    if (revision === modelCatalogRevision) { modelCatalogLoading = false; renderModelSettings(); }
  }
}
async function saveModel(model) {
  if (modelSaving) return;
  modelSaving = true; renderModelSettings();
  try {
    const result = await api('/model', 'PUT', {model});
    state.llm_model = result.model; modelDirty = false; setModelPopup(false);
    await load();
  } finally { modelSaving = false; renderModelSettings(); }
}
$('#model-input').onfocus = () => setModelPopup(true);
$('#model-input').oninput = () => { modelDirty = true; setModelPopup(true); renderModelOptions(); renderModelSettings(); };
$('#model-input').onkeydown = event => {
  if (event.key === 'Escape') { setModelPopup(false); $('#model-input').blur(); event.preventDefault(); }
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    setModelPopup(true); moveModelOption(event.key === 'ArrowDown' ? 1 : -1); event.preventDefault();
  } else if (event.key === 'Enter' && modelActiveIndex >= 0) {
    const option = $$('.model-option', $('#model-options'))[modelActiveIndex];
    if (option) { chooseModelOption(option.textContent); event.preventDefault(); }
  }
};
$('#model-open').onclick = () => { const open = $('#model-popup').hidden; setModelPopup(open); if (open) $('#model-input').focus(); };
$('#model-form').onsubmit = event => { event.preventDefault(); guard(() => saveModel($('#model-input').value.trim())); };
$('#model-reset').onclick = () => guard(() => saveModel(null));
$('#model-refresh').onclick = () => refreshModels();
$('#model-form').onkeydown = event => { if (event.key === 'Escape') { setModelPopup(false); event.preventDefault(); event.stopPropagation(); } };
document.addEventListener('pointerdown', event => { if (!$('#model-form').contains(event.target)) setModelPopup(false); });
const chromeSizeObserver = new ResizeObserver(() => {
  $('#workspace').style.setProperty('--header-height', `${$('.topbar').offsetHeight}px`);
  $('#workspace').style.setProperty('--sidebar-height', `${$('.sidebar').offsetHeight}px`);
});
chromeSizeObserver.observe($('.topbar')); chromeSizeObserver.observe($('.sidebar'));

async function load(adopt = false) {
  const revision = ++loadRevision;
  const nextState = await api('/state');
  if (revision !== loadRevision) return;
  state = nextState;
  restoreUiPreferences();
  $('#workspace').hidden = false; $('#login').hidden = true;
  $('#repo-name').textContent = state.repo.split('/').pop(); $('#repo-name').title = state.repo;
  if (adopt || !displayed) {
    displayed = state.snapshot; displayedReport = state.report; selected.clear(); unseen = false;
  }
  if (view === 'overview') {
    const report = await resolveReport();
    if (revision !== loadRevision) return;
    const snapshot = report && displayed.id !== report.snapshot_id ? await api('/snapshots/' + report.snapshot_id) : displayed;
    if (revision !== loadRevision) return;
    displayedReport = report;
    displayed = snapshot;
  }
  $('#snapshot-label').textContent = displayed.version.slice(0, 12);
  $('#review-count').textContent = String(state.report?.items.length || 0);
  $('#change-count').textContent = String(new Set(state.snapshot.fragments.map(f => f.path)).size);
  $('#generate').textContent = state.report ? 'Обновить отчёт ↗' : 'Создать отчёт ↗';
  renderModelSettings();
  if (state.llm_configured && !modelCatalogRequested) void refreshModels();
  overviewProgress(); renderBanner(); renderJobs(); render(Boolean(displayedReport?.is_draft));
}
function renderBanner() {
  const banner = $('#banner'); banner.replaceChildren();
  if (view === 'overview') { banner.hidden = true; return; }
  if (unseen || (displayed && displayed.version !== state.snapshot.version)) {
    banner.append(el('span', '', 'Есть новая версия изменений. Вы просматриваете сохранённый снимок.'), button('Открыть актуальный diff', '', async () => { view = 'changes'; await load(true); updateNav(); }));
    banner.hidden = false;
  } else if (view === 'overview' && !displayedReport?.is_draft && state.report_sources_stale) {
    banner.append(el('span', '', 'Источники обновились после создания отчёта. Обновите отчёт, чтобы учесть новые решения.')); banner.hidden = false;
  } else { banner.hidden = true; }
}
function renderJobs() {
  const running = state.jobs.filter(j => ['queued', 'running'].includes(j.status));
  const node = $('#job-status'); node.replaceChildren(); node.hidden = !running.length;
  for (const job of running) node.append(el('span', '', (job.kind === 'report' ? 'Готовим отчёт…' : 'Готовим ответ…') + (job.model ? ` · ${job.model}` : '')), button('Отменить', 'quiet', () => api('/jobs/' + job.id + '/cancel', 'POST')));
}
function updateNav() { $$('.nav-button').forEach(b => b.classList.toggle('active', b.dataset.view === view)); }
$$('.nav-button').forEach(b => b.onclick = () => guard(async () => { view = b.dataset.view; updateNav(); await load(true); if (view === 'sources') await refreshSources(); }));
$('#refresh').onclick = () => guard(async () => { $('#refresh').disabled = true; try { await api('/sync', 'POST'); await load(true); } finally { $('#refresh').disabled = false; } });
$('#generate').onclick = () => guard(async () => {
  const job = await api('/reports', 'POST'); rememberReport('draft:' + job.id);
  view = 'overview'; updateNav(); await load(true);
});
$('#baseline').onclick = () => {
  modal('Начать новую работу', el('p', 'prose', 'Текущее состояние станет исходным. Все уже существующие изменения будут отмечены отдельно от последующих правок агента. Сохранённые отчёты останутся доступны.'), [button('Зафиксировать начало', 'primary', async () => { await api('/reviews', 'POST', {from_start: true}); $('#dialog').close(); await load(true); })]);
};

function render(preserve = false) {
  if (!state) return;
  const content = el('div');
  const x = window.scrollX, y = window.scrollY;
  $('#page-title').textContent = {overview: 'Обзор изменений', changes: 'Изменения кода', sources: 'Контекст и источники', history: 'История действий'}[view];
  $('#page-subtitle').textContent = {overview: 'Проверьте решения. Соберите следующий коммит.', changes: 'Каждая строка — под вашим контролем.', sources: 'Свяжите реализацию с решениями из сессии.', history: 'Применённые изменения и точки восстановления.'}[view];
  $('#main').classList.toggle('overview-main', view === 'overview');
  if (view === 'overview') renderOverviewWorkspace(content);
  else {
    if ($('#chat-panel').closest('.overview-workspace')) { $('.layout').append($('#chat-panel')); $('#chat-panel').hidden = true; }
    $('.page-heading').hidden = false;
  }
  if (view === 'changes') renderChanges(content);
  if (view === 'sources') renderSources(content);
  if (view === 'history') renderHistory(content);
  const target = $('#content');
  if (preserve) {
    // Keep unchanged cards and their diff scroll positions/focus during streaming.
    const old = new Map([...target.children].map((node, index) => [node.dataset.liveKey || 'position:' + index, node]));
    let cursor = target.firstChild;
    [...content.children].forEach((node, index) => {
      const previous = old.get(node.dataset.liveKey || 'position:' + index);
      if (previous?.isEqualNode(node)) node = previous;
      else if (previous && node.dataset.liveKey === 'summary') {
        $('.summary-text', previous).textContent = $('.summary-text', node).textContent;
        if (previous.isEqualNode(node)) node = previous;
      }
      if (node === cursor) cursor = cursor.nextSibling;
      else target.insertBefore(node, cursor);
    });
    while (cursor) { const next = cursor.nextSibling; cursor.remove(); cursor = next; }
    window.scrollTo(x, y);
  } else target.replaceChildren(...content.children);
  $('#chat-toggle').disabled = false;
  if (view === 'overview') {
    const expectedReport = displayedReport?.is_draft ? null : displayedReport?.id || null;
    const expectedKind = expectedReport ? overviewTarget.kind : 'all';
    const expectedId = expectedReport ? overviewTarget.id : null;
    if (!chatSnapshot || chatSnapshot !== displayed.id || chatReport !== expectedReport || chatTargetKind !== expectedKind || chatTargetId !== expectedId) {
      void guard(() => openOverviewChat(false));
    }
  }
}
function stats(container) {
  const rows = displayed.fragments.flatMap(f => f.rows);
  const data = [[new Set(displayed.fragments.map(f => f.path)).size, 'файлов в снимке', ''], ['+' + rows.filter(r => r.kind === 'add').length, 'добавленных строк', 'green'], ['−' + rows.filter(r => r.kind === 'delete').length, 'удалённых строк', 'red'], [displayedReport?.items.filter(i => i.reviewed).length || 0, 'решений просмотрено', '']];
  const box = el('div', 'stats');
  data.forEach(([value, label, color]) => { const node = el('div', 'stat'); node.append(el('span', 'stat-value ' + color, String(value)), el('span', 'stat-label', label)); box.append(node); }); container.append(box);
}
function overviewFragmentsFor(path = overviewFile, layer = overviewLayer, snapshot = displayed) {
  return (snapshot?.fragments || []).filter(f => f.path === path && f.layer === layer);
}
function overviewTargetObject() {
  if (overviewTarget.kind === 'item') return displayedReport?.items?.find(i => i.id === overviewTarget.id);
  if (overviewTarget.kind === 'finding') return displayedReport?.findings?.find(f => f.id === overviewTarget.id);
  return displayedReport;
}
function overviewFiles(target) {
  const ids = target?.fragment_ids ? new Set(target.fragment_ids) : null;
  return [...new Map(displayed.fragments.filter(f => !ids || ids.has(f.id)).map(f => [f.path + ':' + f.layer, f])).values()];
}
function overviewChoose(kind, id = null, file = null, layer = null) {
  if (overviewUnsaved && !confirm('В редакторе есть несохранённые изменения. Перейти без сохранения?')) return;
  overviewTarget = {kind, id}; overviewSelection = []; overviewLineIds = [];
  overviewFile = file; overviewLayer = layer; overviewMode = 'frozen';
  render(); updateChatSelections();
}
function overviewTreeButton(label, kind, id, file = null, layer = null, level = 0, badge = '') {
  const b = button(label, 'tree-entry level-' + level, () => overviewChoose(kind, id, file, layer));
  b.classList.toggle('active', overviewTarget.kind === kind && overviewTarget.id === id &&
    (file ? overviewFile === file && overviewLayer === layer : !overviewFile));
  if (badge) b.append(el('span', 'tree-badge', badge));
  b.title = file || label;
  return b;
}
function overviewTreeGroup(tree, label, kind, id, target, level = 0) {
  const wrap = el('div', 'tree-group');
  wrap.append(overviewTreeButton(label, kind, id, null, null, level, target?.reviewed ? '✓' : ''));
  for (const f of overviewFiles(target)) wrap.append(overviewTreeButton(
    `${f.path} · ${f.layer === 'staged' ? 'staged' : 'working'}`, kind, id, f.path, f.layer, level + 1));
  tree.append(wrap);
}
function overviewProgress() {
  const total = (displayedReport?.items?.length || 0) + (displayedReport?.findings?.length || 0);
  const done = [...(displayedReport?.items || []), ...(displayedReport?.findings || [])].filter(x => x.reviewed).length;
  const rows = state.snapshot.fragments.flatMap(f => f.rows.map(row => ({...row, layer: f.layer})));
  const staged = rows.filter(r => r.layer === 'staged' && r.id).length;
  const unstaged = rows.filter(r => r.layer === 'unstaged' && r.id).length;
  const files = new Set(state.snapshot.fragments.map(f => f.path)).size;
  const bar = $('#overview-metrics'); bar.replaceChildren();
  bar.append(el('span', '', `${files} файлов`), el('span', '', `Review ${done}/${total}`),
    el('span', '', `Staged ${staged}/${staged + unstaged}`));
  const progress = el('progress'); progress.max = Math.max(1, total); progress.value = done;
  progress.setAttribute('aria-label', 'Прогресс review'); bar.append(progress);
  const stagedProgress = el('progress', 'staged-progress'); stagedProgress.max = Math.max(1, staged + unstaged); stagedProgress.value = staged;
  stagedProgress.setAttribute('aria-label', 'Доля подготовленных строк'); bar.append(stagedProgress);
}
function splitSpace() {
  const center = $('.overview-center', overviewRoot);
  if (!center?.isConnected) return null;
  const splitter = $('.overview-splitter', center);
  const chat = $('.overview-chat-slot', center);
  return {center, splitter, available: center.clientHeight - splitter.offsetHeight - chat.offsetHeight};
}
function updateOverviewSplit() {
  const space = splitSpace(); if (!space) return;
  const {center, splitter, available} = space;
  const overflow = available < 290;
  center.classList.toggle('split-overflow', overflow);
  const detailHeight = overflow ? 130 : Math.max(130, Math.min(available - 160, available * overviewSplitRatio));
  center.style.setProperty('--detail-height', `${detailHeight}px`);
  splitter.setAttribute('aria-valuemin', String(overflow ? 0 : Math.round(13000 / available)));
  splitter.setAttribute('aria-valuemax', String(overflow ? 100 : Math.round(100 - 16000 / available)));
  splitter.setAttribute('aria-valuenow', String(Math.round(100 * (overflow ? .5 : detailHeight / available))));
  splitter.setAttribute('aria-disabled', String(overflow));
  overviewEditor?.requestMeasure();
}
function setOverviewSplit(height, persist = false) {
  const space = splitSpace(); if (!space || space.available < 290) return;
  const clamped = Math.max(130, Math.min(space.available - 160, height));
  overviewSplitRatio = clamped / space.available;
  updateOverviewSplit();
  if (persist) saveUiPreference('detail-ratio', overviewSplitRatio);
}
function setupOverviewSplitter(splitter) {
  splitter.onpointerdown = event => {
    if (event.button !== 0 || splitSpace()?.available < 290) return;
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add('dragging');
    event.preventDefault();
  };
  splitter.onpointermove = event => {
    if (!splitter.hasPointerCapture(event.pointerId)) return;
    setOverviewSplit(event.clientY - splitter.parentElement.getBoundingClientRect().top);
  };
  const finish = event => {
    if (!splitter.hasPointerCapture(event.pointerId)) return;
    setOverviewSplit(event.clientY - splitter.parentElement.getBoundingClientRect().top, true);
    splitter.classList.remove('dragging');
    splitter.releasePointerCapture(event.pointerId);
  };
  splitter.onpointerup = finish;
  splitter.onpointercancel = event => { splitter.classList.remove('dragging'); if (splitter.hasPointerCapture(event.pointerId)) splitter.releasePointerCapture(event.pointerId); };
  splitter.onkeydown = event => {
    const space = splitSpace(); if (!space || space.available < 290) return;
    const current = Math.max(130, Math.min(space.available - 160, space.available * overviewSplitRatio));
    const step = event.shiftKey ? 50 : 10;
    const target = event.key === 'ArrowUp' ? current - step : event.key === 'ArrowDown' ? current + step :
      event.key === 'Home' ? 130 : event.key === 'End' ? space.available - 160 : null;
    if (target === null) return;
    event.preventDefault(); setOverviewSplit(target, true);
  };
}
function setupDiffScroll(host) {
  host.tabIndex = 0;
  host.setAttribute('aria-label', 'Прокрутка диффа');
  host.onkeydown = event => {
    if (event.target !== host) return;
    const scroller = $('.cm-mergeView', host) || $('.cm-scroller', host);
    if (!scroller) return;
    const vertical = event.key === 'ArrowDown' ? 40 : event.key === 'ArrowUp' ? -40 :
      event.key === 'PageDown' ? scroller.clientHeight : event.key === 'PageUp' ? -scroller.clientHeight : null;
    if (vertical !== null) { scroller.scrollTop += vertical; event.preventDefault(); return; }
    if (event.key === 'Home' || event.key === 'End') { scroller.scrollTop = event.key === 'Home' ? 0 : scroller.scrollHeight; event.preventDefault(); return; }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      scroller.scrollLeft += event.key === 'ArrowLeft' ? -40 : 40; event.preventDefault();
    }
  };
}
function renderOverviewWorkspace(container) {
  const rootKey = `${reportSelection}:${displayed.id}`;
  if (!overviewRoot || overviewRootKey !== rootKey) {
    overviewResizeObserver?.disconnect(); overviewEditorResizeObserver?.disconnect(); overviewEditor?.destroy(); overviewEditor = null; overviewEditorKey = ''; overviewRootKey = rootKey;
    overviewTarget = {kind: 'summary', id: null}; overviewFile = null; overviewLayer = null;
    overviewMode = 'frozen'; overviewUnsaved = false; overviewSelection = [];
    overviewRoot = el('div', 'overview-workspace');
    const grid = el('div', 'overview-grid');
    const tree = el('nav', 'overview-tree'); tree.setAttribute('aria-label', 'Дерево отчёта');
    const center = el('div', 'overview-center');
    const detail = el('section', 'overview-detail'); detail.id = 'overview-detail';
    const diff = el('section', 'overview-diff'); diff.id = 'overview-diff';
    const splitter = el('div', 'overview-splitter'); splitter.tabIndex = 0;
    splitter.setAttribute('role', 'separator'); splitter.setAttribute('aria-label', 'Разделитель текста и диффа');
    splitter.setAttribute('aria-orientation', 'horizontal'); splitter.setAttribute('aria-controls', 'overview-detail overview-diff');
    splitter.setAttribute('aria-valuemin', '0'); splitter.setAttribute('aria-valuemax', '100');
    setupOverviewSplitter(splitter);
    center.append(detail, splitter, diff, el('div', 'overview-chat-slot'));
    grid.append(tree, center); overviewRoot.append(grid);
    overviewResizeObserver = new ResizeObserver(updateOverviewSplit); overviewResizeObserver.observe(center);
  }
  container.append(overviewRoot);
  $('.page-heading').hidden = true;
  const chatPanel = $('#chat-panel') || $('#chat-panel', overviewRoot);
  $('.overview-chat-slot', overviewRoot).append(chatPanel);
  chatPanel.hidden = false;
  updateChatExpansion(); overviewProgress();
  const tree = $('.overview-tree', overviewRoot); tree.replaceChildren();
  if (state.reports.length || state.report_drafts.length) {
    const select = el('select', 'overview-report-select'); select.setAttribute('aria-label', 'Версия отчёта');
    [...state.report_drafts].reverse().forEach(d => { const o = el('option', '', `${draftLabel(d)} · ${new Date(d.created_at).toLocaleString('ru')}`); o.value = 'draft:' + d.id; select.append(o); });
    [...state.reports].reverse().forEach((r, i) => { const o = el('option', '', `${i === 0 ? 'Последний · ' : ''}${new Date(r.created_at).toLocaleString('ru')}`); o.value = 'report:' + r.id; select.append(o); });
    select.value = reportSelection;
    select.onchange = () => guard(async () => { if (overviewUnsaved && !confirm('Сбросить несохранённые правки?')) { select.value = reportSelection; return; } rememberReport(select.value); await load(true); });
    tree.append(select);
  }
  tree.append(el('p', 'eyebrow', 'НАВИГАЦИЯ ПО ОТЧЁТУ'));
  overviewTreeGroup(tree, 'Описание', 'summary', null, null);
  if (displayedReport?.findings?.length) {
    tree.append(el('p', 'tree-heading', `Проблемы · ${displayedReport.findings.length}`));
    displayedReport.findings.forEach(f => overviewTreeGroup(tree, f.title, 'finding', f.id, f, 0));
  }
  if (displayedReport?.items?.length) {
    let section = '';
    displayedReport.items.forEach(item => {
      if (section !== item.section) { section = item.section; tree.append(el('p', 'tree-heading', section)); }
      overviewTreeGroup(tree, item.title, 'item', item.id, item);
    });
  }
  renderOverviewDetail();
  void renderOverviewDiff();
  requestAnimationFrame(updateOverviewSplit);
}
function overviewText(parent, label, field, value) {
  if (!value) return;
  if (label) parent.append(el('p', 'detail-label', label));
  const p = el('p', 'prose overview-selectable' + (field === 'summary' ? ' summary-text' : ''), value); p.dataset.reportField = field;
  p.onmouseup = p.onkeyup = () => {
    const quote = window.getSelection()?.toString().trim();
    if (!quote || !value.includes(quote)) return;
    overviewSelection = overviewSelection.filter(s => s.kind !== 'report');
    overviewSelection.push({kind: 'report', field, text: quote}); updateChatSelections();
  };
  parent.append(p);
}
function renderOverviewDetail() {
  const pane = $('.overview-detail', overviewRoot); pane.replaceChildren();
  const report = displayedReport, target = overviewTargetObject();
  const header = el('div', 'overview-pane-header');
  const title = overviewTarget.kind === 'summary' ? 'Описание' : target?.title || 'Пункт отчёта';
  header.append(el('h2', '', title));
  if (overviewTarget.kind !== 'summary' && target && !report?.is_draft) {
    const label = el('label', 'checkbox-label'); const check = document.createElement('input'); check.type = 'checkbox';
    check.checked = Boolean(target.reviewed); check.setAttribute('aria-label', 'Просмотрено');
    check.onchange = () => guard(async () => {
      const path = overviewTarget.kind === 'finding' ? 'findings' : 'items';
      await api(`/reports/${report.id}/${path}/${target.id}`, 'PATCH', {reviewed: check.checked});
      target.reviewed = check.checked; render();
    });
    label.append(check, document.createTextNode('Просмотрено')); header.append(label);
  }
  pane.append(header);
  if (!report) { pane.append(empty('Отчёт ещё не создан', 'Создайте отчёт или выберите файл слева, чтобы изучить diff.')); return; }
  if (report.is_draft) pane.append(el('p', 'overview-notice draft-status', `${draftLabel(report)} · ${report.error || 'Предварительный текст. Отметки станут доступны после завершения.'}`));
  if (!report.is_draft && displayed.version !== state.snapshot.version) pane.append(el('p', 'overview-notice', 'Отчёт относится к прежнему снимку. Текущий рабочий файл может отличаться.'));
  if (!report.is_draft && state.report_sources_stale) pane.append(el('p', 'overview-notice', 'Источники изменились после создания отчёта. Обновите отчёт для новых данных.'));
  if (overviewTarget.kind === 'summary') {
    overviewText(pane, '', 'summary', report.summary || 'Ожидаем текст от модели…');
    if (report.model) pane.append(el('p', 'footer-note report-model', `Модель: ${report.model}`));
  } else if (target) {
    if (overviewTarget.kind === 'item') {
      pane.append(tag({recorded: 'Из истории сессии', reconstructed: 'Реконструкция', unknown: 'Причина не зафиксирована'}[target.rationale_kind]));
      overviewText(pane, 'Объяснение', 'explanation', target.explanation);
      overviewText(pane, 'Обоснование и проверки', 'argument', target.argument);
      if (target.limitations?.length) pane.append(el('p', 'footer-note', target.limitations.join(' · ')));
    } else {
      pane.append(tag(target.severity, target.severity));
      overviewText(pane, 'Проблема', 'description', target.description);
      overviewText(pane, 'Предложение', 'suggestion', target.suggestion);
    }
    const links = el('div', 'sources-links'); target.source_ids?.forEach((id, i) => links.append(button(`Источник ${i + 1} ↗`, '', () => showSource(id)))); pane.append(links);
  }
  const ask = button('Спросить об этом ↗', 'text-button overview-ask', () => openOverviewChat(true));
  ask.disabled = Boolean(report.is_draft); pane.append(ask);
}
function overviewChangedLines() {
  return overviewFragmentsFor(overviewFile, overviewLayer, overviewMode === 'live' ? state.snapshot : displayed)
    .flatMap(f => f.rows).filter(r => r.id);
}
function overviewSelectCode(selection) {
  const snapshot = overviewMode === 'live' ? state.snapshot : displayed;
  const side = selection.side === 'a' ? (overviewLayer === 'staged' ? 'head' : 'index') : (overviewLayer === 'staged' ? 'index' : 'work');
  overviewSelection = overviewSelection.filter(s => s.kind !== 'code' && s.kind !== 'unsaved');
  if (selection.unsaved) overviewSelection.push({kind: 'unsaved', path: overviewFile, text: selection.text.slice(0, 2000)});
  else overviewSelection.push({kind: 'code', snapshot_id: snapshot.id, path: overviewFile, side,
                               start: selection.start, end: selection.end});
  const field = selection.side === 'a' ? 'old' : 'new';
  overviewLineIds = overviewChangedLines().filter(r => r[field] >= selection.start && r[field] <= selection.end).map(r => r.id);
  updateChatSelections();
  const count = $('.overview-line-count', overviewRoot); if (count) count.textContent = `${overviewLineIds.length} строк diff`;
  overviewUpdateSave();
}
function overviewUpdateSave() {
  const save = $('.overview-save', overviewRoot); if (save) save.disabled = !overviewUnsaved;
  const stage = $('.overview-stage-lines', overviewRoot); if (stage) stage.disabled = overviewMode !== 'live' || overviewUnsaved || !overviewLineIds.length;
  const whole = $('.overview-stage-all', overviewRoot); if (whole) whole.disabled = overviewMode !== 'live' || overviewUnsaved;
}
async function overviewGitAction(whole) {
  if (overviewMode !== 'live' || overviewUnsaved) throw new Error('Сначала сохраните текущий файл.');
  const fragment = overviewFragmentsFor(overviewFile, overviewLayer, state.snapshot)[0];
  if (!fragment) throw new Error('Для файла нет изменений в выбранном слое.');
  const action = overviewLayer === 'staged' ? 'unstage' : 'stage';
  const op = await api('/operations', 'POST', {snapshot_id: state.snapshot.id, expected_version: state.snapshot.version,
    path: overviewFile, action, line_ids: whole ? [] : overviewLineIds, whole_file: whole, key: key()});
  if (op.status !== 'completed') throw new Error(op.error || 'Операция не завершена');
  overviewLineIds = []; overviewSelection = overviewSelection.filter(s => s.kind === 'report');
  overviewEditorKey = ''; await load(); toast('Готово. Восстановление доступно в истории действий.');
}
async function overviewSave() {
  if (!overviewEditor || !overviewUnsaved || overviewMode !== 'live') return;
  const op = await api('/operations/edit', 'POST', {snapshot_id: state.snapshot.id,
    expected_version: state.snapshot.version, path: overviewFile, content: overviewEditor.content, key: key()});
  if (op.status !== 'completed') throw new Error(op.error || 'Файл не сохранён');
  overviewUnsaved = false; overviewEditorKey = ''; overviewSelection = overviewSelection.filter(s => s.kind === 'report');
  await load(); toast('Рабочий файл сохранён. Отчёт относится к прежнему снимку.');
}
async function overviewEditCurrent() {
  if (!overviewFile) return;
  state = await api('/sync', 'POST', {paths: [overviewFile]});
  overviewMode = 'live'; overviewEditorKey = ''; overviewLineIds = [];
  render();
}
function updateChatSelections() {
  const box = $('#chat-selections'); if (!box) return; box.replaceChildren();
  overviewSelection.forEach((s, index) => {
    const label = s.kind === 'report' ? `Текст отчёта: ${s.text.slice(0, 45)}` :
      s.kind === 'unsaved' ? `Несохранённый код: ${s.text.slice(0, 45)}` :
      `${s.path}:${s.start}–${s.end} · ${s.side}`;
    box.append(button(label + ' ×', 'context-chip', () => { overviewSelection.splice(index, 1); updateChatSelections(); }));
  });
}
async function renderOverviewDiff() {
  const pane = $('.overview-diff', overviewRoot); if (!pane) return;
  const candidates = overviewFiles(overviewTargetObject());
  if (!overviewFile && candidates.length) { overviewFile = candidates[0].path; overviewLayer = candidates[0].layer; }
  const snapshot = overviewMode === 'live' ? state.snapshot : displayed;
  const editorKey = `${snapshot.id}:${overviewFile}:${overviewLayer}:${overviewMode}`;
  const header = el('div', 'overview-pane-header overview-diff-header');
  header.append(el('h2', '', overviewFile || 'Diff файла'));
  if (overviewFile) header.append(tag(overviewMode === 'frozen' ? 'Снимок отчёта' : 'Текущий файл', overviewMode === 'frozen' ? '' : 'warning'));
  const actions = el('div', 'overview-diff-actions');
  if (overviewFile) {
    if (overviewMode === 'frozen') actions.append(button('Редактировать текущий файл ↗', 'outline', overviewEditCurrent));
    else {
      actions.append(button('Вернуться к снимку', 'quiet', () => { if (overviewUnsaved && !confirm('Сбросить несохранённые правки?')) return; overviewMode = 'frozen'; overviewUnsaved = false; overviewEditorKey = ''; render(); }));
      actions.append(button('Сохранить файл', 'primary overview-save', overviewSave));
      const action = overviewLayer === 'staged' ? 'Unstage' : 'Stage';
      actions.append(button(`${action} строк`, 'outline overview-stage-lines', () => overviewGitAction(false)));
      actions.append(button(`${action} файла`, 'outline overview-stage-all', () => overviewGitAction(true)));
      actions.append(el('span', 'overview-line-count', `${overviewLineIds.length} строк diff`));
    }
  }
  const oldHeader = $('.overview-diff-header', pane);
  if (oldHeader) oldHeader.replaceWith(header); else pane.prepend(header);
  const oldActions = $('.overview-diff-actions', pane);
  if (oldActions) oldActions.replaceWith(actions); else header.after(actions);
  overviewUpdateSave();
  let host = $('.overview-editor-host', pane); if (!host) { host = el('div', 'overview-editor-host'); setupDiffScroll(host); pane.append(host); }
  if (!overviewFile) {
    overviewEditorResizeObserver?.disconnect(); overviewEditor?.destroy(); overviewEditor = null; overviewEditorKey = '';
    host.replaceChildren(empty('Выберите файл', 'Файлы находятся в дереве слева.'));
    return;
  }
  if (overviewEditor && editorKey === overviewEditorKey) return;
  const revision = ++overviewDiffRevision;
  const sideA = overviewLayer === 'staged' ? 'head' : 'index';
  const sideB = overviewLayer === 'staged' ? 'index' : 'work';
  const url = side => `/snapshots/${snapshot.id}/file?path=${encodeURIComponent(overviewFile)}&side=${side}`;
  const [before, after] = await Promise.all([api(url(sideA)), api(url(sideB))]);
  if (revision !== overviewDiffRevision || view !== 'overview') return;
  overviewEditorResizeObserver?.disconnect(); overviewEditor?.destroy(); overviewEditor = null; host.replaceChildren();
  if (before.unsupported || after.unsupported || before.omitted || after.omitted) {
    host.append(empty('Файл недоступен для редактора', before.unsupported || after.unsupported || 'Содержимое файла не включено в снимок.'));
    overviewEditorKey = editorKey; return;
  }
  overviewInitialContent = after.content; overviewUnsaved = false; overviewEditorKey = editorKey;
  overviewEditor = createDiffEditor(host, {before: before.content, after: after.content,
    editable: overviewMode === 'live' && overviewLayer === 'unstaged' && after.exists,
    compact: matchMedia('(max-width: 800px)').matches,
    nonce: $('meta[name="csp-nonce"]').content,
    onChange: content => { overviewUnsaved = content !== overviewInitialContent; overviewUpdateSave(); },
    onSelection: overviewSelectCode});
  overviewEditorResizeObserver = new ResizeObserver(() => overviewEditor?.requestMeasure());
  overviewEditorResizeObserver.observe(host);
  if (overviewMode === 'live' && overviewLayer === 'staged') host.append(el('p', 'footer-note', 'Для правки переключитесь на working-слой файла. Staged-слой меняется через Stage/Unstage.'));
  overviewUpdateSave();
}
function renderOverview(container) {
  stats(container);
  const toolbar = el('div', 'toolbar');
  if (state.reports.length || state.report_drafts.length) {
    const select = el('select'); select.setAttribute('aria-label', 'Версия отчёта');
    [...state.report_drafts].reverse().forEach(draft => { const opt = el('option', '', `${draftLabel(draft)} · ${new Date(draft.created_at).toLocaleString('ru')}`); opt.value = 'draft:' + draft.id; opt.selected = opt.value === reportSelection; select.append(opt); });
    [...state.reports].reverse().forEach((report, i) => { const opt = el('option', '', `${i === 0 ? 'Последний · ' : ''}${new Date(report.created_at).toLocaleString('ru')}`); opt.value = 'report:' + report.id; opt.selected = opt.value === reportSelection; select.append(opt); });
    select.onchange = () => guard(async () => { rememberReport(select.value); await load(true); }); toolbar.append(select);
  }
  const filter = el('label', 'checkbox-label'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = onlyProblems; checkbox.onchange = () => { onlyProblems = checkbox.checked; render(); }; filter.append(checkbox, document.createTextNode('Только пункты с проблемами')); toolbar.append(filter); container.append(toolbar);
  if (!displayedReport) {
    container.append(empty('Сначала — общая картина', state.llm_available ? 'Подключите сессии во вкладке «Источники» и создайте отчёт. Изменения уже доступны для просмотра и stage.' : 'Diff и Git-действия уже доступны. Для объяснений и вопросов настройте подключение LLM и выберите модель выше.'));
    const heading = el('div', 'section-heading'); heading.append(el('h2', '', 'Изменения в рабочем дереве'), button('Смотреть diff →', 'text-button', async () => { view = 'changes'; updateNav(); await load(true); })); container.append(heading);
    displayed.fragments.slice(0, 3).forEach(f => container.append(fragmentView(f)));
    return;
  }
  if (displayedReport.is_draft) {
    const notice = el('div', 'banner draft-status'); notice.dataset.liveKey = 'draft-status';
    const progress = displayedReport.batch_count ? ` · порция ${displayedReport.batch_number}/${displayedReport.batch_count}` : '';
    notice.append(el('strong', '', `${draftLabel(displayedReport)} · Предпросмотр${progress}`), el('p', '', displayedReport.error || 'Текст предварительный. Отметки просмотра, вопросы и Git-действия доступны в готовом отчёте.'));
    if (displayedReport.attempt > 1 && displayedReport.status === 'running') notice.append(el('p', '', 'Модель исправляет ответ текущей порции.'));
    container.append(notice);
  }
  const summary = el('section', 'panel'); summary.dataset.liveKey = 'summary'; const body = el('div', 'panel-body'); const title = el('div', 'panel-title'); title.append(el('h2', '', 'Что изменилось'), tag(displayedReport.retrospective ? 'Ретроспективный отчёт' : 'С начала работы')); body.append(title, el('p', 'summary-text', displayedReport.summary || 'Ожидаем текст от модели…'));
  if (displayedReport.model) body.append(el('p', 'footer-note report-model', 'Модель: ' + displayedReport.model));
  if (displayedReport.evidence_incomplete) body.append(el('p', 'footer-note', 'Часть истории не вошла в контекст LLM. Выводы ограничены доступными источниками.'));
  summary.append(body); container.append(summary);
  if (displayedReport.preexisting_paths?.length) container.append(el('p', 'preexisting', `До начала review уже были изменения: ${displayedReport.preexisting_paths.join(', ')}`));
  displayedReport.findings.forEach(finding => {
    const box = el('div', 'finding ' + finding.severity); box.dataset.liveKey = 'finding:' + finding.id; box.append(el('h3', '', finding.title), el('p', 'prose', finding.description));
    if (finding.suggestion) box.append(el('p', 'prose spaced', 'Предложение: ' + finding.suggestion));
    const links = el('div', 'sources-links'); finding.source_ids.forEach(id => links.append(button('Источник ↗', '', () => showSource(id)))); box.append(links); container.append(box);
  });
  let section;
  for (const item of displayedReport.items) {
    if (onlyProblems && !displayedReport.findings.some(f => f.fragment_ids.some(id => item.fragment_ids.includes(id)))) continue;
    if (section !== item.section) { section = item.section; const heading = el('div', 'section-heading'); heading.append(el('h2', '', section), el('span', 'eyebrow', 'DECISIONS & EVIDENCE')); container.append(heading); }
    const card = el('article', 'review-card'); card.dataset.liveKey = 'item:' + item.id;
    const header = el('div', 'card-heading'); const text = el('div'); text.append(el('h3', '', item.title), tag({recorded: 'Из истории сессии', reconstructed: 'Реконструкция', unknown: 'Причина не зафиксирована'}[item.rationale_kind]));
    if (item.fragment_ids.some(id => !state.snapshot.fragments.some(f => f.id === id))) text.append(tag('Требует повторного review', 'warning'));
    const ask = button('Спросить ↗', 'text-button', () => openChat(item)); ask.disabled = Boolean(displayedReport.is_draft); header.append(text, ask); card.append(header);
    const body = el('div', 'card-body'); body.append(el('p', 'prose', item.explanation));
    if (item.argument) body.append(el('p', 'detail-label', 'Обоснование и проверки'), el('p', 'prose', item.argument));
    if (item.limitations.length) body.append(el('p', 'detail-label', 'Ограничения'), el('p', 'prose', item.limitations.map(s => '• ' + s).join('\n')));
    const links = el('div', 'sources-links'); item.source_ids.forEach((id, n) => links.append(button(`Источник ${n + 1} ↗`, '', () => showSource(id)))); body.append(links);
    if (item.dependencies.length) body.append(el('p', 'footer-note', `${item.dependencies.length} связанных фрагментов учитываются при вопросах.`));
    item.fragment_ids.forEach(id => { const f = displayed.fragments.find(f => f.id === id); if (f) body.append(fragmentView(f, Boolean(displayedReport.is_draft))); }); card.append(body);
    const footer = el('div', 'card-footer'); const label = el('label'); const check = document.createElement('input'); check.type = 'checkbox'; check.checked = item.reviewed; check.setAttribute('aria-label', 'Решение просмотрено'); check.onchange = () => guard(async () => { await api(`/reports/${displayedReport.id}/items/${item.id}`, 'PATCH', {reviewed: check.checked}); item.reviewed = check.checked; }); label.append(check, document.createTextNode('Просмотрено')); footer.append(label, tag(item.fragment_ids.length + ' фрагм.')); card.append(footer); container.append(card);
    check.disabled = Boolean(displayedReport.is_draft);
  }
}

function renderChanges(container) {
  stats(container);
  if (state.snapshot.untracked.length) {
    const box = el('div', 'panel'); const body = el('div', 'panel-body'); body.append(el('h3', '', 'Новые файлы вне отчёта'), el('p', 'footer-note', 'Выберите файлы для включения. Их содержимое станет доступно анализу LLM.'));
    state.snapshot.untracked.forEach(path => { const row = el('div', 'session'); row.append(el('code', 'session-title', path), button('Включить', 'outline', async () => { await api('/untracked', 'POST', {paths: [path]}); await load(true); })); body.append(row); }); box.append(body); container.append(box);
  }
  if (!displayed.fragments.length) container.append(empty('Все изменения разобраны', 'В выбранном снимке нет изменений отслеживаемых файлов. Новые файлы можно включить отдельно.'));
  for (const layer of ['unstaged', 'staged']) {
    const fragments = displayed.fragments.filter(f => f.layer === layer); if (!fragments.length) continue;
    const heading = el('div', 'section-heading'); heading.append(el('h2', '', layer === 'unstaged' ? 'Рабочие изменения' : 'Подготовлено к коммиту'), tag(layer === 'unstaged' ? 'Index → Working tree' : 'HEAD → Index')); container.append(heading);
    fragments.forEach(f => container.append(fragmentView(f)));
  }
}
function fragmentView(fragment, readOnly = false) {
  const box = el('section', 'diff'); box.dataset.fragment = fragment.id;
  const heading = el('div', 'diff-header'); heading.append(el('code', 'diff-path', fragment.path));
  const tags = el('div', 'diff-tags'); tags.append(tag(fragment.layer === 'staged' ? 'staged' : 'unstaged'));
  if (fragment.parts > 1) tags.append(tag(`${fragment.part}/${fragment.parts}`)); heading.append(tags); box.append(heading);
  if (fragment.unsupported) { box.append(el('p', 'panel-body muted', fragment.unsupported)); return box; }
  const selectionKey = fragment.path + ':' + fragment.layer;
  if (!selected.has(selectionKey)) selected.set(selectionKey, new Set());
  const selection = selected.get(selectionKey); const stale = readOnly || displayed.version !== state.snapshot.version;
  const lines = el('div', 'diff-scroll'); let group = null;
  for (const row of fragment.rows) {
    if (group !== null && group !== row.group) lines.append(el('div', 'diff-gap', '···'));
    group = row.group;
    const line = el('div', 'diff-row ' + row.kind); line.classList.toggle('selected', selection.has(row.id));
    if (row.id) {
      const input = document.createElement('input'); input.type = 'checkbox'; input.disabled = stale; input.checked = selection.has(row.id); input.dataset.line = row.id;
      input.setAttribute('aria-label', `${row.kind === 'add' ? 'Добавленная' : 'Удалённая'} строка ${row.new ?? row.old}`);
      input.onchange = () => { input.checked ? selection.add(row.id) : selection.delete(row.id); updateSelection(selectionKey); }; line.append(input);
    } else line.append(el('span'));
    line.append(el('span', 'line-number', String(row.old ?? '')), el('span', 'line-number', String(row.new ?? '')), el('span', '', row.kind === 'add' ? '+' : row.kind === 'delete' ? '−' : ''), el('code', 'line-text', row.text)); lines.append(line);
  }
  box.append(lines);
  if (!fragment.rows.length) box.append(el('p', 'panel-body muted', fragment.new_exists ? 'Пустой файл' : 'Удаление пустого файла'));
  if (fragment.old_no_newline || fragment.new_no_newline) box.append(el('div', 'diff-gap', 'Нет завершающего перевода строки — исходные байты сохраняются'));
  const actions = el('div', 'diff-actions'); actions.dataset.selection = selectionKey;
  const counter = el('span', 'selection-label', `Выбрано строк: ${selection.size}`); actions.append(counter);
  const action = fragment.layer === 'staged' ? 'unstage' : 'stage';
  const selectedButton = button(action === 'stage' ? 'Stage строк' : 'Unstage строк', 'primary selected-action', () => act(fragment, action, false)); selectedButton.disabled = stale || !selection.size; actions.append(selectedButton);
  if (fragment.layer !== 'staged') { const discard = button('Отменить строки', 'outline danger selected-action', () => act(fragment, 'discard', false)); discard.disabled = stale || !selection.size; actions.append(discard); }
  const whole = button(action === 'stage' ? 'Stage файла' : 'Unstage файла', 'outline', () => act(fragment, action, true)); whole.disabled = stale; actions.append(whole);
  if (fragment.layer !== 'staged') { const discard = button('Отменить файл', 'quiet danger', () => act(fragment, 'discard', true)); discard.disabled = stale; actions.append(discard); }
  actions.append(button('Контекст ↗', 'quiet', () => showFile(fragment))); box.append(actions); return box;
}
function updateSelection(selectionKey) {
  const selection = selected.get(selectionKey);
  $$('.diff-actions').filter(n => n.dataset.selection === selectionKey).forEach(actions => {
    $('.selection-label', actions).textContent = `Выбрано строк: ${selection.size}`;
    $$('.selected-action', actions).forEach(b => b.disabled = !selection.size || displayed.version !== state.snapshot.version);
    $$('input[data-line]', actions.parentElement).forEach(input => { input.checked = selection.has(input.dataset.line); input.parentElement.classList.toggle('selected', input.checked); });
  });
}
async function act(fragment, action, whole) {
  const request = {snapshot_id: displayed.id, expected_version: displayed.version, path: fragment.path, action, line_ids: [...selected.get(fragment.path + ':' + fragment.layer)], whole_file: whole, key: key()};
  const apply = async () => { const op = await api('/operations', 'POST', request); if (op.status !== 'completed') throw new Error(op.error || 'Операция не завершена'); $('#dialog').close(); view = 'changes'; updateNav(); await load(true); toast('Готово. Восстановление доступно в истории действий.'); };
  if (action === 'discard') {
    const preview = await api('/operations/preview', 'POST', request); const content = el('div'); content.append(el('p', 'muted', preview.exists ? 'Так будет выглядеть рабочий файл после отмены. Индекс не изменится.' : 'Рабочий файл будет удалён. Его содержимое сохранится в истории восстановления.'), el('pre', '', preview.content));
    modal('Отменить изменения: ' + fragment.path, content, [button('Оставить', 'outline', () => $('#dialog').close()), button('Отменить изменения', 'primary', apply)]);
  } else await apply();
}
async function showFile(fragment) {
  const side = fragment.layer === 'staged' ? 'index' : 'work';
  const file = await api(`/snapshots/${displayed.id}/file?path=${encodeURIComponent(fragment.path)}&side=${side}`);
  modal(fragment.path + ' · ' + side, el('pre', '', file.exists ? file.content : 'Файл отсутствует в этой версии'));
}
async function showSource(id) {
  // The endpoint includes historical evidence retained for old reports.
  const data = await api(displayedReport && view === 'overview' ? '/sources?' + (displayedReport.is_draft ? 'draft_id=' : 'report_id=') + displayedReport.id : '/sources?all=1'); const event = data.events.find(e => e.id === id);
  modal('Источник решения', el('pre', '', event ? `${event.provider} / ${event.session_id}\n${event.role} · ${event.timestamp || ''}\n\n${event.text}` : 'Источник недоступен: ' + id));
}

async function refreshSources() {
  const [sessions, sources] = await Promise.all([api('/sessions'), api('/sources')]);
  chosenSessionList = sessions.sessions; sourceCache = sources.events; state.source_status = sessions.status;
  if (view === 'sources') render();
}
function renderSources(container) {
  const panel = el('div', 'panel'); const body = el('div', 'panel-body'); const title = el('div', 'panel-title'); title.append(el('h2', '', 'Сессии разработки'), button('Найти сессии ↻', 'outline', refreshSources)); body.append(title);
  Object.entries(state.source_status).forEach(([provider, status]) => body.append(el('p', 'footer-note', `${provider}: ${status.message}`)));
  const choices = new Map((state.selected_sessions || []).map(s => [s.provider + ':' + s.id, s]));
  chosenSessionList.forEach(session => { const row = el('div', 'session'); const label = el('label'); const input = document.createElement('input'); input.type = 'checkbox'; input.checked = choices.has(session.provider + ':' + session.id); input.onchange = () => { input.checked ? choices.set(session.provider + ':' + session.id, session) : choices.delete(session.provider + ':' + session.id); }; label.append(input, el('span', 'session-title', session.title)); row.append(label, tag(session.provider)); body.append(row); });
  if (chosenSessionList.length) body.append(button('Синхронизировать выбранные', 'primary spaced', async () => { await api('/sessions', 'PUT', {sessions: [...choices.values()]}); await load(); await refreshSources(); toast('История синхронизирована'); }));
  const file = document.createElement('input'); file.type = 'file'; file.accept = '.json,application/json'; file.setAttribute('aria-label', 'Импорт OpenCode JSON'); file.hidden = true; file.onchange = () => guard(async () => { if (file.files[0]) { await api('/import', 'POST', JSON.parse(await file.files[0].text())); await load(); await refreshSources(); } });
  body.append(el('p', 'detail-label', 'Сохранённая история'), button('Импортировать OpenCode JSON', 'outline', () => file.click()), file); panel.append(body); container.append(panel);
  const record = el('div', 'panel'); const form = el('form', 'panel-body'); form.append(el('h3', '', 'Записать решение')); const text = el('textarea', 'spaced'); text.placeholder = 'Выбранный подход, причина, ограничения и связанные файлы…'; text.required = true; text.maxLength = 20000; text.setAttribute('aria-label', 'Текст решения'); const submit = el('button', 'primary spaced', 'Сохранить решение'); submit.type = 'submit'; form.append(text, submit); form.onsubmit = e => { e.preventDefault(); guard(async () => { await api('/decisions', 'POST', {text: text.value}); sourceCache = (await api('/sources')).events; render(); toast('Решение записано'); }); }; record.append(form); container.append(record);
  const events = el('div', 'panel'); const eventsBody = el('div', 'panel-body'); eventsBody.append(el('h3', '', `Источники · ${sourceCache.length}`));
  sourceCache.slice().reverse().forEach(event => { const row = el('details', 'source-entry'); row.append(el('summary', 'session-title', `${event.provider} · ${event.role} · ${event.text.slice(0, 80)}`), el('p', 'prose', event.text)); eventsBody.append(row); }); events.append(eventsBody); container.append(events);
}
function renderHistory(container) {
  if (!state.operations.length) { container.append(empty('История начинается с первого действия', 'Stage, unstage и отмена сохраняются вместе с данными для восстановления.')); return; }
  const box = el('div', 'panel'); const body = el('div', 'panel-body');
  [...state.operations].reverse().forEach(op => {
    const row = el('div', 'history-row'); const description = el('div'); description.append(el('code', '', op.path), el('p', '', `${op.action} · ${new Date(op.created_at).toLocaleString('ru')} · ${op.status}`)); if (op.error) description.append(el('p', 'error', op.error)); row.append(description);
    const b = button('Восстановить ↶', 'outline', async () => { await api('/operations/' + op.id + '/undo', 'POST', {key: key()}); await load(true); toast('Предыдущее состояние восстановлено'); });
    b.disabled = op.status !== 'completed' || op.after_version !== state.snapshot.version; if (b.disabled) b.title = 'После операции изменилось состояние репозитория'; row.append(b); body.append(row);
  }); box.append(body); container.append(box);
}

async function openChat(item = null) {
  chatItem = item; chatSnapshot = displayed.id; chatReport = item ? displayedReport?.id : null;
  chatTargetKind = item ? 'item' : 'all'; chatTargetId = item?.id || null; liveMessages.clear();
  $('#chat-panel').hidden = false; setChatExpanded(true); $('#chat-context').textContent = `${item?.title || 'Все изменения'} · снимок ${displayed.version.slice(0, 8)}`;
  await loadMessages(); $('#question').focus();
}
function chatThread() {
  return chatSnapshot + ':' + (chatTargetKind === 'item' ? chatTargetId : chatTargetKind === 'all' ? 'all' : chatTargetKind + ':' + (chatTargetId || ''));
}
async function openOverviewChat(focus = false) {
  chatItem = overviewTarget.kind === 'item' ? overviewTargetObject() : null;
  chatSnapshot = displayed.id;
  chatReport = displayedReport && !displayedReport.is_draft ? displayedReport.id : null;
  chatTargetKind = chatReport ? overviewTarget.kind : 'all';
  chatTargetId = chatReport ? overviewTarget.id : null;
  liveMessages.clear();
  $('#chat-panel').hidden = false;
  if (focus) setChatExpanded(true);
  $('#chat-context').textContent = `${overviewTarget.kind === 'summary' ? 'Описание' : overviewTargetObject()?.title || 'Все изменения'} · снимок отчёта ${displayed.version.slice(0, 8)}`;
  updateChatSelections();
  await loadMessages(); if (focus) $('#question').focus();
}
async function loadMessages() {
  const thread = chatThread();
  const data = await api('/messages?thread=' + encodeURIComponent(thread));
  const box = $('#chat-messages'); box.replaceChildren();
  if (!data.messages.length) box.append(el('p', 'footer-note', 'Спросите о причинах решения, граничных случаях или последствиях отмены фрагмента.'));
  data.messages.forEach(message => { const node = el('div', 'message ' + message.role); node.append(el('span', 'role', message.role === 'user' ? 'ВЫ' : 'REVIEW ASSISTANT' + (message.model ? ` · ${message.model}` : ''))); const text = el('span', '', message.content + (message.complete === false ? '\n[Ответ не завершён]' : '')); node.append(text); box.append(node); });
  box.scrollTop = box.scrollHeight;
}
$('#chat-toggle').onclick = () => guard(() => view === 'overview' ? openOverviewChat(true) : openChat());
$('#chat-close').onclick = () => { if (view !== 'overview') $('#chat-panel').hidden = true; else setChatExpanded(!chatExpanded); };
$('#chat-form').onsubmit = event => { event.preventDefault(); guard(async () => {
  const question = $('#question').value.trim(); if (!question) return;
  await api('/questions', 'POST', {question, snapshot_id: chatSnapshot, report_id: chatReport,
    item_id: chatItem?.id || null, target_kind: chatTargetKind, target_id: chatTargetId,
    selections: view === 'overview' ? overviewSelection : []});
  $('#question').value = ''; const user = el('div', 'message user'); user.append(el('span', 'role', 'ВЫ'), el('span', '', question)); $('#chat-messages').append(user); await load();
}); };

async function receive(event) {
  if (event.type === 'connected') { $('#connection').textContent = 'Подключено'; await load(); return; }
  if (event.type === 'model_changed') { await load(); return; }
  if (event.type === 'report_preview') {
    const draft = event.draft;
    const known = state.report_drafts.find(d => d.id === draft.id);
    if (known && known.revision >= event.revision) return;
    state.report_drafts = state.report_drafts.filter(d => d.id !== draft.id);
    if (draft.status !== 'completed') state.report_drafts.push(draft);
    if (view === 'overview' && reportSelection === 'draft:' + draft.id) {
      if (draft.status === 'completed') { rememberReport('report:' + draft.report_id); await load(); return; }
      displayedReport = draft;
      if (displayed.id !== draft.snapshot_id) { displayed = await api('/snapshots/' + draft.snapshot_id); selected.clear(); }
      $('#snapshot-label').textContent = displayed.version.slice(0, 12);
      renderBanner(); render(true);
    }
    return;
  }
  if (event.type === 'snapshot') {
    if (displayed && displayed.version !== event.version) {
      unseen = true; state = await api('/state'); renderBanner();
      $$('.diff-actions button, .diff input').forEach(b => { if (!b.textContent.includes('Контекст')) b.disabled = true; });
    }
  }
  if (event.type === 'progress') { $('#job-status').hidden = false; const span = $('#job-status span'); if (span) span.textContent = event.message; }
  if (event.type === 'warning') toast(event.message);
  if (event.type === 'job') {
    if (event.job.status === 'failed') toast(event.job.error);
    if (event.job.kind === 'report' && event.job.status === 'completed') {
      toast('Новая версия отчёта готова');
      if (reportSelection === 'draft:' + event.job.id || reportSelection === 'report:' + event.job.result.report_id) await load();
      else { unseen = true; await load(); }
    } else await load();
    if (event.job.kind === 'chat' && !['running', 'queued'].includes(event.job.status) && chatSnapshot) { liveMessages.clear(); await loadMessages(); }
  }
  if (event.type === 'resync') { await load(); if (chatSnapshot) await loadMessages(); }
  if (event.type === 'chat_delta' && event.thread === chatThread()) {
    let text = liveMessages.get(event.message_id);
    if (!text) { const node = el('div', 'message assistant'); node.append(el('span', 'role', 'REVIEW ASSISTANT' + (event.model ? ` · ${event.model}` : ''))); text = el('span'); node.append(text); $('#chat-messages').append(node); liveMessages.set(event.message_id, text); }
    text.textContent += event.delta; $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
  }
}
async function connect() {
  streamController?.abort(); const controller = new AbortController(); streamController = controller;
  while (token && !controller.signal.aborted) {
    try {
      const response = await fetch('/api/v1/events', {headers: {Authorization: 'Bearer ' + token}, signal: controller.signal});
      if (!response.ok) throw new Error('Stream unavailable');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const {done, value} = await reader.read(); if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let end;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (block.startsWith('data: ')) await receive(JSON.parse(block.slice(6)));
        }
      }
    } catch (e) { if (controller.signal.aborted) return; }
    $('#connection').textContent = 'Переподключение…';
    await new Promise(resolve => setTimeout(resolve, 2000));
    if (!controller.signal.aborted && token) await guard(() => load());
  }
}
if (token) guard(async () => { await load(true); connect(); });
