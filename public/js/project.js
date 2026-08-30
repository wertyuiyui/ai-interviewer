import {
  $, apiFetch, formatDate, formatSeconds, getClientId, setButtonBusy, showToast, toArray,
} from './common.js?v=20260830-profile-bank-v2';

const elements = {
  status: $('#projectProfileStatus'),
  name: $('#projectName'),
  files: $('#projectFiles'),
  githubUrl: $('#projectGithubUrl'),
  githubAdd: $('#projectGithubAdd'),
  list: $('#projectAssetList'),
  ready: $('#projectReady'),
  readyTitle: $('#projectReadyTitle'),
  readyCopy: $('#projectReadyCopy'),
  analyze: $('#projectAnalyzeButton'),
  loading: $('#projectAnalysisLoading'),
  analysis: $('#projectAnalysis'),
  analysisName: $('#analysisProjectName'),
  analysisMeta: $('#analysisProjectMeta'),
  analysisSummary: $('#analysisProjectSummary'),
  refresh: $('#projectRefreshAnalysis'),
  architecture: $('#projectArchitecture'),
  requestFlow: $('#projectRequestFlow'),
  technologyChoices: $('#projectTechnologyChoices'),
  risks: $('#projectRisks'),
  improvements: $('#projectImprovements'),
  questions: $('#projectQuestionList'),
};

const queryProjectId = String(new URLSearchParams(location.search).get('project') || '').trim();
let profile = { resumes: [], projects: [], selected_project_id: '' };
let selectedProject = null;
let querySelectionHandled = false;
const projectPracticeTimers = new Map();
const projectPracticeTicker = window.setInterval(() => {
  projectPracticeTimers.forEach((entry) => updatePracticeTimer(entry));
}, 1000);

function itemId(item) {
  return String(item?.id || item?.project_id || '');
}

function itemName(item, fallback = '未命名项目') {
  return String(item?.name || item?.title || fallback).trim();
}

function normalizeProfile(payload) {
  const source = payload?.profile && typeof payload.profile === 'object' ? payload.profile : payload || {};
  return {
    resumes: Array.isArray(source.resumes) ? source.resumes : [],
    projects: Array.isArray(source.projects) ? source.projects : [],
    selected_project_id: String(source.selected_project_id || ''),
  };
}

function setVisible(element, visible) {
  element?.classList.toggle('is-hidden', !visible);
}

function updateStatus(message = '') {
  elements.status.textContent = message || `${profile.projects.length} 个项目 · 当前设备`;
}

function sourceMeta(project) {
  if (project?.source_type === 'github') return 'GitHub 仓库';
  const count = Array.isArray(project?.files) ? project.files.length : 0;
  return `${count || '多'} 个项目文件`;
}

function renderReady() {
  const hasProject = Boolean(selectedProject);
  elements.readyTitle.textContent = hasProject ? itemName(selectedProject) : '先从左侧添加一个项目';
  elements.readyCopy.textContent = hasProject
    ? `${sourceMeta(selectedProject)}已就绪。开始后会整理架构、风险和可练习的项目追问。`
    : '选择项目后，这里会生成架构拆解、风险清单和面试追问。';
  elements.analyze.disabled = !hasProject;
  setVisible(elements.ready, true);
  setVisible(elements.loading, false);
  setVisible(elements.analysis, false);
}

function createEmptyProjectItem() {
  const item = document.createElement('li');
  item.className = 'project-list-empty';
  item.textContent = '还没有项目，先上传资料或添加 GitHub。';
  return item;
}

function renderProjects() {
  elements.list.replaceChildren();
  const none = document.createElement('li');
  none.className = `project-asset-item project-none-item${selectedProject ? '' : ' is-selected'}`;
  const noneLabel = document.createElement('label');
  const noneRadio = document.createElement('input');
  noneRadio.type = 'radio';
  noneRadio.name = 'selected_project';
  noneRadio.value = '';
  noneRadio.checked = !selectedProject;
  noneRadio.addEventListener('change', clearProjectSelection);
  const noneIcon = document.createElement('span');
  noneIcon.className = 'project-source-icon';
  noneIcon.textContent = '—';
  const noneCopy = document.createElement('span');
  noneCopy.className = 'project-asset-copy';
  const noneTitle = document.createElement('strong');
  noneTitle.textContent = '不使用项目';
  const noneMeta = document.createElement('small');
  noneMeta.textContent = '不附加到下一场完整面试';
  noneCopy.append(noneTitle, noneMeta);
  noneLabel.append(noneRadio, noneIcon, noneCopy);
  none.append(noneLabel);
  elements.list.append(none);
  if (!profile.projects.length) {
    elements.list.append(createEmptyProjectItem());
    return;
  }
  profile.projects.forEach((project) => {
    const id = itemId(project);
    const selected = id === itemId(selectedProject);
    const item = document.createElement('li');
    item.className = `project-asset-item${selected ? ' is-selected' : ''}`;

    const label = document.createElement('label');
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'selected_project';
    radio.value = id;
    radio.checked = selected;
    radio.addEventListener('change', () => selectProject(project));
    const icon = document.createElement('span');
    icon.className = 'project-source-icon';
    icon.textContent = project?.source_type === 'github' ? 'GH' : '项';
    const copy = document.createElement('span');
    copy.className = 'project-asset-copy';
    const title = document.createElement('strong');
    title.textContent = itemName(project);
    const meta = document.createElement('small');
    meta.textContent = `${sourceMeta(project)} · ${formatDate(project?.created_at, false)}`;
    copy.append(title, meta);
    label.append(radio, icon, copy);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'project-delete-button';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `删除项目 ${title.textContent}`);
    remove.addEventListener('click', () => deleteProject(project));
    item.append(label, remove);
    elements.list.append(item);
  });
}

function chooseLocalProject(project) {
  selectedProject = project || null;
  profile.selected_project_id = itemId(project);
  profile.projects = profile.projects.map((item) => ({ ...item, selected: itemId(item) === profile.selected_project_id }));
  renderProjects();
  renderReady();
}

async function selectProject(project, { quiet = false } = {}) {
  const id = itemId(project);
  if (!id) return;
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/selection`, {
      method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), selected: true },
    });
    const canonical = response?.project && typeof response.project === 'object'
      ? response.project
      : project;
    chooseLocalProject(canonical);
    if (!quiet) showToast(`已选择“${itemName(project)}”。`, 'success');
  } catch (error) {
    renderProjects();
    showToast(error?.message || '项目选择失败，请稍后重试。', 'error', 5200);
  }
}

async function clearProjectSelection() {
  const id = String(profile.selected_project_id || itemId(selectedProject));
  if (!id) {
    chooseLocalProject(null);
    return;
  }
  try {
    await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/selection`, {
      method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), selected: false },
    });
    chooseLocalProject(null);
    showToast('已选择“不使用项目”，下一场完整面试不会附加项目资料。', 'success');
  } catch (error) {
    renderProjects();
    showToast(error?.message || '无法清除项目选择。', 'error', 5200);
  }
}

async function loadProfile({ preferredProjectId = '' } = {}) {
  updateStatus('正在同步匿名 Profile…');
  try {
    const payload = await apiFetch('/api/profile', { timeout: 15_000 });
    profile = normalizeProfile(payload);
    const selectedId = profile.selected_project_id;
    selectedProject = profile.projects.find((item) => itemId(item) === selectedId)
      || profile.projects.find((item) => item?.selected === true)
      || null;
    renderProjects();
    renderReady();
    updateStatus();
    const requestedId = preferredProjectId || (!querySelectionHandled ? queryProjectId : '');
    querySelectionHandled = true;
    const requested = profile.projects.find((item) => itemId(item) === requestedId);
    if (requested && itemId(requested) !== itemId(selectedProject)) {
      await selectProject(requested, { quiet: true });
    }
  } catch (error) {
    profile = { resumes: [], projects: [], selected_project_id: '' };
    selectedProject = null;
    renderProjects();
    renderReady();
    updateStatus('同步失败，可稍后重试');
    showToast(error?.message || '匿名 Profile 暂时无法同步。', 'error', 5200);
  }
}

async function uploadProjectFiles() {
  const files = [...(elements.files.files || [])];
  if (!files.length) return;
  elements.files.disabled = true;
  updateStatus(`正在保存 ${files.length} 个项目文件…`);
  try {
    const data = new FormData();
    data.append('client_id', getClientId());
    data.append('name', elements.name.value.trim() || files[0].name.replace(/\.[^.]+$/, ''));
    files.forEach((file) => data.append('files', file, file.name));
    const response = await apiFetch('/api/profile/projects', { method: 'POST', body: data, timeout: 65_000 });
    const project = response?.project || response;
    if (!itemId(project)) throw new Error('服务端没有返回项目编号。');
    await loadProfile({ preferredProjectId: itemId(project) });
    elements.name.value = '';
    showToast('项目资料已保存，点击“开始项目解读”继续。', 'success');
  } catch (error) {
    showToast(error?.message || '项目资料保存失败，请稍后重试。', 'error', 5200);
    updateStatus();
  } finally {
    elements.files.disabled = false;
    elements.files.value = '';
  }
}

function validGithubUrl(value) {
  try {
    const url = new URL(value);
    const parts = url.pathname.split('/').filter(Boolean);
    const repository = (parts[1] || '').replace(/\.git$/i, '');
    return url.protocol === 'https:' && url.hostname.toLowerCase() === 'github.com'
      && parts.length === 2 && Boolean(parts[0] && repository) && !url.search && !url.hash;
  } catch {
    return false;
  }
}

async function addGithubProject() {
  const url = elements.githubUrl.value.trim();
  if (!validGithubUrl(url)) {
    showToast('请输入完整的 GitHub 仓库链接。', 'error');
    elements.githubUrl.focus();
    return;
  }
  const repositoryName = new URL(url).pathname.split('/').filter(Boolean)[1].replace(/\.git$/i, '');
  elements.githubAdd.disabled = true;
  updateStatus('正在添加 GitHub 项目…');
  try {
    const response = await apiFetch('/api/profile/projects/github', {
      method: 'POST', timeout: 35_000, json: {
        client_id: getClientId(), name: elements.name.value.trim() || repositoryName, url,
      },
    });
    const project = response?.project || response;
    if (!itemId(project)) throw new Error('服务端没有返回项目编号。');
    await loadProfile({ preferredProjectId: itemId(project) });
    elements.githubUrl.value = '';
    elements.name.value = '';
    showToast('GitHub 项目已添加，点击“开始项目解读”继续。', 'success');
  } catch (error) {
    showToast(error?.message || 'GitHub 项目添加失败，请稍后重试。', 'error', 5200);
    updateStatus();
  } finally {
    elements.githubAdd.disabled = false;
  }
}

async function deleteProject(project) {
  const id = itemId(project);
  if (!id || !window.confirm(`确定删除“${itemName(project)}”吗？项目资料与已有解读会一并移除。`)) return;
  try {
    await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}`, {
      method: 'DELETE', timeout: 15_000,
    });
    await loadProfile();
    showToast('项目已删除。', 'success');
  } catch (error) {
    showToast(error?.message || '项目删除失败，请稍后重试。', 'error', 5200);
  }
}

function textFromValue(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value).trim();
  if (Array.isArray(value)) return value.map(textFromValue).filter(Boolean).join('；');
  if (typeof value === 'object') {
    return Object.entries(value)
      .map(([key, item]) => {
        const text = textFromValue(item);
        return text ? `${key}：${text}` : '';
      })
      .filter(Boolean)
      .join('；');
  }
  return '';
}

function listItemText(item) {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return textFromValue(item);
  const title = item.title || item.name || item.technology || item.choice || item.risk || item.improvement || item.step || '';
  const detailParts = [
    item.component ? `组件 ${textFromValue(item.component)}` : '',
    textFromValue(item.action || item.responsibility || item.purpose || item.reason || item.description || item.detail || item.rationale),
    item.tradeoffs ? `取舍：${textFromValue(item.tradeoffs)}` : '',
    item.impact ? `影响：${textFromValue(item.impact)}` : '',
    item.mitigation ? `应对：${textFromValue(item.mitigation)}` : '',
  ].filter(Boolean);
  const detail = detailParts.join('；');
  if (title && detail) return `${textFromValue(title)}：${textFromValue(detail)}`;
  return textFromValue(title || detail || item);
}

function renderList(element, values, fallback) {
  element.replaceChildren();
  const items = toArray(values).map(listItemText).filter(Boolean);
  (items.length ? items : [fallback]).forEach((text) => {
    const item = document.createElement('li');
    item.textContent = text;
    element.append(item);
  });
}

function renderArchitecture(value) {
  elements.architecture.replaceChildren();
  if (Array.isArray(value)) {
    value.filter(Boolean).forEach((entry, index) => {
      const row = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = entry?.name || entry?.component || `模块 ${index + 1}`;
      const copy = document.createElement('p');
      copy.textContent = textFromValue(entry?.responsibility || entry?.description || entry) || '暂无说明';
      row.append(title, copy);
      elements.architecture.append(row);
    });
  } else if (value && typeof value === 'object') {
    Object.entries(value).forEach(([key, detail]) => {
      const row = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = key;
      const copy = document.createElement('p');
      copy.textContent = textFromValue(detail) || '暂无说明';
      row.append(title, copy);
      elements.architecture.append(row);
    });
  } else {
    const copy = document.createElement('p');
    copy.textContent = textFromValue(value) || '当前资料不足以形成完整架构概览，可补充 README 或设计文档后重新解读。';
    elements.architecture.append(copy);
  }
}

function practiceStorageKey() {
  return `mock_interview.project_practice.v1.${getClientId()}.${itemId(selectedProject)}`;
}

function readPracticeDrafts(storageKey = practiceStorageKey()) {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

function elapsedPracticeSeconds(entry) {
  const active = entry.startedAt ? (Date.now() - entry.startedAt) / 1000 : 0;
  return Math.max(0, Number(entry.elapsedBase) || 0) + active;
}

function updatePracticeTimer(entry) {
  if (!entry?.timeLabel) return;
  entry.timeLabel.textContent = `我的回答 · ${formatSeconds(elapsedPracticeSeconds(entry))}`;
}

function persistPracticeEntry(entry, drafts = null) {
  if (!entry) return;
  const values = drafts || readPracticeDrafts(entry.storageKey);
  values[entry.key] = {
    answer: entry.textarea.value,
    elapsed_seconds: elapsedPracticeSeconds(entry),
    updated_at: new Date().toISOString(),
  };
  try { localStorage.setItem(entry.storageKey, JSON.stringify(values)); } catch { /* storage unavailable */ }
}

function persistVisiblePracticeEntries() {
  if (!projectPracticeTimers.size || !selectedProject) return;
  const grouped = new Map();
  projectPracticeTimers.forEach((entry) => {
    if (!grouped.has(entry.storageKey)) grouped.set(entry.storageKey, readPracticeDrafts(entry.storageKey));
    persistPracticeEntry(entry, grouped.get(entry.storageKey));
  });
}

function renderQuestions(values) {
  persistVisiblePracticeEntries();
  projectPracticeTimers.clear();
  elements.questions.replaceChildren();
  const questions = toArray(values).filter(Boolean);
  if (!questions.length) {
    const empty = document.createElement('p');
    empty.className = 'project-question-empty';
    empty.textContent = '当前资料还不足以生成追问，可补充关键模块和技术决策后重新解读。';
    elements.questions.append(empty);
    return;
  }
  const drafts = readPracticeDrafts();
  questions.forEach((question, index) => {
    const source = typeof question === 'object' ? question : { question };
    const card = document.createElement('article');
    card.className = 'project-question-card';
    const heading = document.createElement('header');
    const number = document.createElement('span');
    number.textContent = String(index + 1).padStart(2, '0');
    const copy = document.createElement('div');
    const title = document.createElement('h4');
    title.textContent = textFromValue(source.question || source.prompt || source.title) || `项目追问 ${index + 1}`;
    const focus = document.createElement('p');
    focus.textContent = source.focus ? `考察重点：${textFromValue(source.focus)}` : '先讲结论，再用项目中的真实取舍与结果支撑。';
    copy.append(title, focus);
    heading.append(number, copy);

    const label = document.createElement('label');
    const labelCopy = document.createElement('span');
    const practiceKey = `${index}:${title.textContent.slice(0, 240)}`;
    const saved = drafts[practiceKey] && typeof drafts[practiceKey] === 'object'
      ? drafts[practiceKey]
      : {};
    const answer = document.createElement('textarea');
    answer.rows = 5;
    answer.maxLength = 5000;
    answer.placeholder = '先用 60–90 秒组织你的回答…';
    answer.value = String(saved.answer || '');
    const practiceEntry = {
      key: practiceKey,
      storageKey: practiceStorageKey(),
      textarea: answer,
      timeLabel: labelCopy,
      elapsedBase: Math.max(0, Number(saved.elapsed_seconds) || 0),
      startedAt: 0,
    };
    projectPracticeTimers.set(practiceKey, practiceEntry);
    updatePracticeTimer(practiceEntry);
    answer.addEventListener('input', () => {
      if (!practiceEntry.startedAt) practiceEntry.startedAt = Date.now();
      hint.textContent = '草稿已自动保存在当前浏览器';
      persistPracticeEntry(practiceEntry);
    });
    label.append(labelCopy, answer);

    const actions = document.createElement('div');
    actions.className = 'project-question-actions';
    const hint = document.createElement('small');
    hint.textContent = '参考思路不会自动替你作答';
    const reveal = document.createElement('button');
    reveal.type = 'button';
    reveal.textContent = '展开参考思路';
    const finish = document.createElement('button');
    finish.type = 'button';
    finish.textContent = '完成回答';
    finish.addEventListener('click', () => {
      if (!answer.value.trim()) {
        showToast('请先写下本题回答。', 'error');
        answer.focus();
        return;
      }
      practiceEntry.elapsedBase = elapsedPracticeSeconds(practiceEntry);
      practiceEntry.startedAt = 0;
      persistPracticeEntry(practiceEntry);
      updatePracticeTimer(practiceEntry);
      hint.textContent = `已保存 · 用时 ${formatSeconds(practiceEntry.elapsedBase)}`;
      showToast('本题回答已保存，可展开参考思路对照。', 'success');
    });
    actions.append(hint, finish, reveal);

    const suggested = document.createElement('div');
    suggested.className = 'project-suggested-answer is-hidden';
    const suggestedTitle = document.createElement('strong');
    suggestedTitle.textContent = '参考思路';
    const suggestedCopy = document.createElement('p');
    suggestedCopy.textContent = textFromValue(source.suggested_answer || source.answer || source.key_points)
      || '当前未返回参考答案。可以围绕背景、技术取舍、落地过程、量化结果和复盘改进五步组织。';
    suggested.append(suggestedTitle, suggestedCopy);
    reveal.addEventListener('click', () => {
      const hidden = suggested.classList.toggle('is-hidden');
      reveal.textContent = hidden ? '展开参考思路' : '收起参考思路';
      reveal.setAttribute('aria-expanded', String(!hidden));
    });

    card.append(heading, label, actions, suggested);
    elements.questions.append(card);
  });
}

function renderAnalysis(payload) {
  const analysis = payload?.analysis && typeof payload.analysis === 'object' ? payload.analysis : payload || {};
  elements.analysisName.textContent = itemName(selectedProject);
  elements.analysisMeta.textContent = `${sourceMeta(selectedProject)} · 匿名 Profile${payload?.cached ? ' · 已复用最近解读' : ''}`;
  elements.analysisSummary.textContent = textFromValue(analysis.project_summary)
    || '以下结论只基于当前保存的项目资料，可补充更多上下文后重新解读。';
  renderArchitecture(analysis.architecture);
  renderList(elements.requestFlow, analysis.request_flow, '当前资料不足以还原完整请求链路。');
  renderList(elements.technologyChoices, analysis.technology_choices, '当前资料未明确说明技术选型依据。');
  renderList(elements.risks, analysis.risks, '暂未识别到明确风险，仍建议准备容量与故障场景。');
  renderList(elements.improvements, analysis.improvements, '可补充压测数据、监控指标与复盘结论。');
  renderQuestions(analysis.interview_questions);
  setVisible(elements.ready, false);
  setVisible(elements.loading, false);
  setVisible(elements.analysis, true);
  window.scrollTo({ top: Math.max(0, elements.analysis.offsetTop - 90), behavior: 'smooth' });
}

async function analyzeProject({ refresh = false } = {}) {
  const id = itemId(selectedProject);
  if (!id) return;
  setButtonBusy(elements.analyze, true, '正在解读项目…');
  elements.refresh.disabled = true;
  setVisible(elements.ready, false);
  setVisible(elements.analysis, false);
  setVisible(elements.loading, true);
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/analysis`, {
      method: 'POST', timeout: 90_000, json: { client_id: getClientId(), refresh: Boolean(refresh) },
    });
    renderAnalysis(response);
  } catch (error) {
    renderReady();
    showToast(error?.message || '项目解读失败，请稍后重试。', 'error', 6000);
  } finally {
    setButtonBusy(elements.analyze, false);
    elements.refresh.disabled = false;
  }
}

elements.files.addEventListener('change', uploadProjectFiles);
elements.githubAdd.addEventListener('click', addGithubProject);
elements.githubUrl.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addGithubProject();
});
elements.analyze.addEventListener('click', () => analyzeProject());
elements.refresh.addEventListener('click', () => analyzeProject({ refresh: true }));
window.addEventListener('pagehide', () => {
  persistVisiblePracticeEntries();
  clearInterval(projectPracticeTicker);
});

loadProfile();
