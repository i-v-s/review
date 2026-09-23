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
const draftLabel = draft => draft.status === 'running' ? 'Генерируется' : 'Не завершён';
function rememberReport(value) { reportSelection = value; sessionStorage.setItem('review-report-selection', value); }

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
function logout() { token = ''; sessionStorage.removeItem('review-token'); streamController?.abort(); modelCatalogRevision++; modelCatalogRequested = false; modelCatalogLoading = false; modelDirty = false; $('#model-options').replaceChildren(); $('#workspace').hidden = true; $('#login').hidden = false; }
$('#logout').onclick = logout;
$('#login-form').onsubmit = async event => { event.preventDefault(); token = $('#token').value.trim(); try { await load(true); sessionStorage.setItem('review-token', token); $('#token').value = ''; connect(); } catch (e) { $('#login-error').textContent = e.message; } };

function renderModelSettings() {
  if (!state) return;
  if (!modelDirty) $('#model-input').value = state.llm_model || '';
  $('#model-current').textContent = state.llm_configured
    ? `Сейчас: ${state.llm_model || 'модель не выбрана'}. Выбор применяется к новым отчётам и вопросам.`
    : 'Настройте OPENAI_API_KEY и OPENAI_BASE_URL на сервере.';
  $('#model-input').disabled = modelSaving || !state.llm_configured;
  $('#model-apply').disabled = modelSaving || !state.llm_configured || !$('#model-input').value.trim();
  $('#model-reset').disabled = modelSaving || !state.llm_configured;
  $('#model-reset').title = state.llm_default_model ? `REVIEW_MODEL: ${state.llm_default_model}` : 'REVIEW_MODEL не задана';
  $('#model-refresh').disabled = modelCatalogLoading || !state.llm_configured;
  $('#generate').disabled = modelSaving || !state.llm_available || state.jobs.some(j => j.kind === 'report' && ['queued', 'running'].includes(j.status));
  $('#generate').title = state.llm_available ? '' : 'Настройте OPENAI_API_KEY и выберите модель';
  $('#chat-form button[type="submit"]').disabled = modelSaving || !state.llm_available;
}
async function refreshModels() {
  const revision = ++modelCatalogRevision;
  modelCatalogRequested = true; modelCatalogLoading = true;
  $('#model-list-status').textContent = 'Загружаем список моделей…'; renderModelSettings();
  try {
    const data = await api('/models');
    if (revision !== modelCatalogRevision) return;
    $('#model-options').replaceChildren(...data.models.map(id => { const option = el('option'); option.value = id; return option; }));
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
    state.llm_model = result.model; modelDirty = false;
    await load();
  } finally { modelSaving = false; renderModelSettings(); }
}
$('#model-input').oninput = () => { modelDirty = true; renderModelSettings(); };
$('#model-form').onsubmit = event => { event.preventDefault(); guard(() => saveModel($('#model-input').value.trim())); };
$('#model-reset').onclick = () => guard(() => saveModel(null));
$('#model-refresh').onclick = () => refreshModels();

async function load(adopt = false) {
  const revision = ++loadRevision;
  const nextState = await api('/state');
  if (revision !== loadRevision) return;
  state = nextState;
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
  renderBanner(); renderJobs(); render(Boolean(displayedReport?.is_draft));
}
function renderBanner() {
  const banner = $('#banner'); banner.replaceChildren();
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
  if (view === 'overview') renderOverview(content);
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
  $('#chat-toggle').disabled = view === 'overview' && Boolean(displayedReport?.is_draft);
}
function stats(container) {
  const rows = displayed.fragments.flatMap(f => f.rows);
  const data = [[new Set(displayed.fragments.map(f => f.path)).size, 'файлов в снимке', ''], ['+' + rows.filter(r => r.kind === 'add').length, 'добавленных строк', 'green'], ['−' + rows.filter(r => r.kind === 'delete').length, 'удалённых строк', 'red'], [displayedReport?.items.filter(i => i.reviewed).length || 0, 'решений просмотрено', '']];
  const box = el('div', 'stats');
  data.forEach(([value, label, color]) => { const node = el('div', 'stat'); node.append(el('span', 'stat-value ' + color, String(value)), el('span', 'stat-label', label)); box.append(node); }); container.append(box);
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
  chatItem = item; chatSnapshot = displayed.id; chatReport = item ? displayedReport?.id : null; liveMessages.clear();
  $('#chat-panel').hidden = false; $('#chat-context').textContent = `${item?.title || 'Все изменения'} · снимок ${displayed.version.slice(0, 8)}`;
  await loadMessages(); $('#question').focus();
}
async function loadMessages() {
  const thread = chatSnapshot + ':' + (chatItem?.id || 'all');
  const data = await api('/messages?thread=' + encodeURIComponent(thread));
  const box = $('#chat-messages'); box.replaceChildren();
  if (!data.messages.length) box.append(el('p', 'footer-note', 'Спросите о причинах решения, граничных случаях или последствиях отмены фрагмента.'));
  data.messages.forEach(message => { const node = el('div', 'message ' + message.role); node.append(el('span', 'role', message.role === 'user' ? 'ВЫ' : 'REVIEW ASSISTANT' + (message.model ? ` · ${message.model}` : ''))); const text = el('span', '', message.content + (message.complete === false ? '\n[Ответ не завершён]' : '')); node.append(text); box.append(node); });
  box.scrollTop = box.scrollHeight;
}
$('#chat-toggle').onclick = () => guard(() => openChat());
$('#chat-close').onclick = () => $('#chat-panel').hidden = true;
$('#chat-form').onsubmit = event => { event.preventDefault(); guard(async () => {
  const question = $('#question').value.trim(); if (!question) return;
  await api('/questions', 'POST', {question, snapshot_id: chatSnapshot, report_id: chatReport, item_id: chatItem?.id || null});
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
  if (event.type === 'chat_delta' && event.thread === chatSnapshot + ':' + (chatItem?.id || 'all')) {
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
