import {
  $, apiFetch, formatDate, formatSeconds, getClientId, setButtonBusy, showToast, toArray,
} from './common.js?v=20260830-profile-bank-v2';

const elements = {
  status: $('#projectProfileStatus'),
  name: $('#projectName'),
  type: $('#projectType'),
  partialScope: $('#projectPartialScope'),
  responsibility: $('#projectResponsibility'),
  files: $('#projectFiles'),
  githubUrl: $('#projectGithubUrl'),
  githubAdd: $('#projectGithubAdd'),
  list: $('#projectAssetList'),
  ready: $('#projectReady'),
  readyTitle: $('#projectReadyTitle'),
  readyCopy: $('#projectReadyCopy'),
  analyze: $('#projectAnalyzeButton'),
  loading: $('#projectAnalysisLoading'),
  progressTitle: $('#projectProgressTitle'),
  progressCopy: $('#projectProgressCopy'),
  progressSteps: $('#projectProgressSteps'),
  progressBack: $('#projectProgressBack'),
  analysis: $('#projectAnalysis'),
  analysisName: $('#analysisProjectName'),
  analysisMeta: $('#analysisProjectMeta'),
  analysisSummary: $('#analysisProjectSummary'),
  architectureTitle: $('#architectureTitle'),
  architectureDescription: $('#architectureDescription'),
  requestFlowTitle: $('#requestFlowTitle'),
  flowReviewTitle: $('#flowReviewTitle'),
  refresh: $('#projectRefreshAnalysis'),
  architecture: $('#projectArchitecture'),
  responsibilityPanel: $('#projectResponsibilityPanel'),
  responsibilityEdit: $('#projectResponsibilityEdit'),
  responsibilityScopeEdit: $('#projectResponsibilityScopeEdit'),
  responsibilitySave: $('#projectResponsibilitySave'),
  responsibilitySaveState: $('#projectResponsibilitySaveState'),
  responsibilityMerge: $('#projectResponsibilityMerge'),
  requestFlow: $('#projectRequestFlow'),
  interviewIntro: $('#projectInterviewIntro'),
  interviewIntroCopy: $('#projectInterviewIntroCopy'),
  flowReviewState: $('#projectFlowReviewState'),
  flowReviewSummary: $('#projectFlowReviewSummary'),
  flowIssues: $('#projectFlowIssues'),
  flowAssumptions: $('#projectFlowAssumptions'),
  flowToVerify: $('#projectFlowToVerify'),
  technologyChoices: $('#projectTechnologyChoices'),
  risks: $('#projectRisks'),
  improvements: $('#projectImprovements'),
  questions: $('#projectQuestionList'),
  questionStatus: $('#projectQuestionStatus'),
  moreQuestions: $('#projectMoreQuestions'),
  regenerateQuestions: $('#projectRegenerateQuestions'),
};

const queryProjectId = String(new URLSearchParams(location.search).get('project') || '').trim();
let profile = { resumes: [], projects: [], selected_project_id: '' };
let selectedProject = null;
let querySelectionHandled = false;
let currentAnalysis = null;
let currentQuestions = [];
let questionsBusy = false;
const projectPracticeTimers = new Map();
const projectPracticeTicker = window.setInterval(() => {
  projectPracticeTimers.forEach((entry) => updatePracticeTimer(entry));
}, 1000);

const ANALYSIS_PROGRESS_STAGES = [
  { id: 'reading', label: '读取项目资料' },
  { id: 'preparing_context', label: '梳理源码与本人职责' },
  { id: 'generating', label: '生成架构、链路、介绍与追问' },
  { id: 'validating', label: '检查链路与输出质量' },
  { id: 'saving', label: '保存解读结果' },
];
let progressStages = [];

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

function renderProgressSteps() {
  elements.progressSteps.replaceChildren();
  progressStages.forEach((stage) => {
    const item = document.createElement('li');
    item.dataset.stage = stage.id;
    item.dataset.state = stage.state || 'pending';
    const mark = document.createElement('span');
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = stage.state === 'done' ? '✓' : stage.state === 'error' ? '!' : '·';
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = stage.label;
    const detail = document.createElement('small');
    detail.textContent = stage.message || (stage.state === 'done' ? '已完成' : stage.state === 'error' ? '失败' : '等待中');
    copy.append(title, detail);
    item.append(mark, copy);
    elements.progressSteps.append(item);
  });
}

function beginProgress(stages, title, copy) {
  progressStages = stages.map((stage) => ({ ...stage, state: 'pending', message: '' }));
  elements.progressTitle.textContent = title;
  elements.progressCopy.textContent = copy;
  elements.progressBack.classList.add('is-hidden');
  elements.loading.setAttribute('aria-busy', 'true');
  renderProgressSteps();
  setVisible(elements.ready, false);
  setVisible(elements.analysis, false);
  setVisible(elements.loading, true);
}

function updateProgress(stageId, message = '', { complete = false } = {}) {
  const index = progressStages.findIndex((stage) => stage.id === stageId);
  if (index < 0) return;
  progressStages = progressStages.map((stage, stageIndex) => ({
    ...stage,
    state: stageIndex < index ? 'done' : stageIndex === index ? (complete ? 'done' : 'active') : 'pending',
    message: stageIndex === index && message ? message : stage.message,
  }));
  const current = progressStages[index];
  elements.progressTitle.textContent = complete ? `${current.label}完成` : `正在${current.label}…`;
  if (message) elements.progressCopy.textContent = message;
  renderProgressSteps();
}

function completeProgress(
  message = '操作已完成。',
  { reused = false, title = '操作完成' } = {},
) {
  progressStages = progressStages.map((stage) => ({
    ...stage,
    state: 'done',
    message: reused && stage.state === 'pending' ? '已复用最近解读结果' : stage.message || '已完成',
  }));
  elements.progressTitle.textContent = title;
  elements.progressCopy.textContent = message;
  elements.loading.setAttribute('aria-busy', 'false');
  renderProgressSteps();
}

function failProgress(message) {
  const activeIndex = Math.max(0, progressStages.findIndex((stage) => stage.state === 'active'));
  progressStages = progressStages.map((stage, index) => ({
    ...stage,
    state: index === activeIndex ? 'error' : stage.state,
    message: index === activeIndex ? message : stage.message,
  }));
  elements.progressTitle.textContent = '这次操作没有完成';
  elements.progressCopy.textContent = message;
  elements.loading.setAttribute('aria-busy', 'false');
  elements.progressBack.classList.remove('is-hidden');
  renderProgressSteps();
}

async function readProjectFileList(files) {
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    updateProgress('reading_files', `正在读取文件清单（${index + 1}/${files.length}）：${file.name}`);
    await file.slice(0, Math.min(file.size, 64 * 1024)).arrayBuffer();
  }
  updateProgress('reading_files', `已读取 ${files.length} 个文件的名称、大小和可上传内容。`, { complete: true });
}

function updateStatus(message = '') {
  elements.status.textContent = message || `${profile.projects.length} 个项目 · 当前设备`;
}

function sourceMeta(project) {
  if (project?.project_type === 'paper') return `${project?.links?.length || 1} 个论文来源`;
  if (['github', 'linked'].includes(project?.source_type)) return `${project?.links?.length || 1} 个项目链接`;
  const count = Array.isArray(project?.files) ? project.files.length : 0;
  return `${count || '多'} 个项目文件`;
}

function projectResponsibilityValue(project = selectedProject) {
  return String(project?.responsibility || project?.my_responsibility || '').trim();
}

function renderResponsibilityEditor() {
  const hasProject = Boolean(selectedProject);
  setVisible(elements.responsibilityPanel, hasProject);
  if (!hasProject) return;
  elements.responsibilityEdit.value = projectResponsibilityValue();
  const partial = selectedProject?.responsibility_scope === 'partial';
  elements.responsibilityScopeEdit.checked = partial;
  elements.responsibilityEdit.classList.toggle('is-hidden', !partial);
  elements.responsibilitySaveState.textContent = partial ? '部分负责 · 已保存' : '默认负责整个项目';
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
  renderResponsibilityEditor();
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
    icon.textContent = project?.project_type === 'paper' ? '论' : ['github', 'linked'].includes(project?.source_type) ? 'GH' : '项';
    const copy = document.createElement('span');
    copy.className = 'project-asset-copy';
    const title = document.createElement('strong');
    title.textContent = itemName(project);
    const meta = document.createElement('small');
    meta.textContent = `${sourceMeta(project)} · ${formatDate(project?.created_at, false)}${project?.responsibility_scope === 'partial' ? ' · 部分负责' : ' · 默认全责'}`;
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
  const changed = itemId(project) !== itemId(selectedProject);
  selectedProject = project || null;
  if (changed) {
    currentAnalysis = null;
    currentQuestions = [];
  }
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
    const nextProject = profile.projects.find((item) => itemId(item) === selectedId)
      || profile.projects.find((item) => item?.selected === true)
      || null;
    if (itemId(nextProject) !== itemId(selectedProject)) {
      currentAnalysis = null;
      currentQuestions = [];
    }
    selectedProject = nextProject;
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
    currentAnalysis = null;
    currentQuestions = [];
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
  beginProgress(
    [
      { id: 'reading_files', label: '读取本地文件' },
      { id: 'uploading_files', label: '上传并保存项目' },
    ],
    '正在读取本地文件…',
    '只读取所选文件，不会执行其中的代码。',
  );
  updateStatus(`正在读取 ${files.length} 个项目文件…`);
  try {
    await readProjectFileList(files);
    const data = new FormData();
    data.append('client_id', getClientId());
    data.append('name', elements.name.value.trim() || files[0].name.replace(/\.[^.]+$/, ''));
    data.append('project_type', elements.type.value);
    data.append('responsibility_scope', elements.partialScope.checked ? 'partial' : 'all');
    data.append('responsibility', elements.partialScope.checked ? elements.responsibility.value.trim() : '');
    files.forEach((file) => data.append('files', file, file.name));
    updateProgress('uploading_files', `正在上传并保存 ${files.length} 个文件，完成前请勿关闭页面。`);
    updateStatus(`正在保存 ${files.length} 个项目文件…`);
    const response = await apiFetch('/api/profile/projects', { method: 'POST', body: data, timeout: 65_000 });
    const project = response?.project || response;
    if (!itemId(project)) throw new Error('服务端没有返回项目编号。');
    updateProgress('uploading_files', '项目文件和本人职责已保存。', { complete: true });
    completeProgress('项目已保存，可以开始解读。', { title: '项目保存完成' });
    await loadProfile({ preferredProjectId: itemId(project) });
    elements.name.value = '';
    elements.responsibility.value = '';
    elements.partialScope.checked = false;
    elements.responsibility.classList.add('is-hidden');
    showToast('项目资料已保存，点击“开始项目解读”继续。', 'success');
  } catch (error) {
    const message = error?.message || '项目资料保存失败，请稍后重试。';
    failProgress(message);
    showToast(message, 'error', 5200);
    updateStatus();
  } finally {
    elements.files.disabled = false;
    elements.files.value = '';
  }
}

function validProjectUrl(value, projectType) {
  try {
    const url = new URL(value);
    const parts = url.pathname.split('/').filter(Boolean);
    const repository = (parts[1] || '').replace(/\.git$/i, '');
    const host = url.hostname.toLowerCase();
    const github = host === 'github.com' && parts.length === 2 && Boolean(parts[0] && repository);
    const arxiv = ['arxiv.org', 'www.arxiv.org'].includes(host)
      && /^(abs|pdf)\/[a-z0-9.\/-]+(?:\.pdf)?$/i.test(parts.join('/'));
    return url.protocol === 'https:' && !url.search && !url.hash
      && (github || (projectType === 'paper' && arxiv));
  } catch {
    return false;
  }
}

async function addGithubProject() {
  const urls = elements.githubUrl.value.split(/\n+/).map((value) => value.trim()).filter(Boolean);
  if (!urls.length || urls.length > 5 || urls.some((url) => !validProjectUrl(url, elements.type.value))) {
    showToast('请提供 1–5 个有效链接；论文支持 arXiv，其他类型支持 GitHub。', 'error');
    elements.githubUrl.focus();
    return;
  }
  if (elements.type.value === 'paper' && !urls.some((url) => /(^|\.)arxiv\.org$/i.test(new URL(url).hostname))) {
    showToast('论文类型至少需要一个 arXiv 链接。', 'error');
    return;
  }
  const defaultName = new URL(urls[0]).pathname.split('/').filter(Boolean).pop().replace(/\.git$|\.pdf$/gi, '');
  elements.githubAdd.disabled = true;
  beginProgress(
    [{ id: 'github_fetch', label: '读取并保存链接资料' }],
    '正在读取链接资料…',
    '服务端只读取支持的公开 GitHub 文本或 arXiv 论文，不会运行代码。',
  );
  updateStatus('正在添加 GitHub 项目…');
  try {
    updateProgress('github_fetch', '正在获取公开仓库的文件清单与受支持源码。');
    const singleGithub = urls.length === 1 && new URL(urls[0]).hostname.toLowerCase() === 'github.com';
    const requestOptions = {
      method: 'POST', timeout: 35_000, json: {
        client_id: getClientId(), name: elements.name.value.trim() || defaultName, urls,
        project_type: elements.type.value,
        responsibility_scope: elements.partialScope.checked ? 'partial' : 'all',
        responsibility: elements.partialScope.checked ? elements.responsibility.value.trim() : '',
        ...(singleGithub ? { url: urls[0], urls: undefined } : {}),
      },
    };
    const response = singleGithub
      ? await apiFetch('/api/profile/projects/github', requestOptions)
      : await apiFetch('/api/profile/projects/links', requestOptions);
    const project = response?.project || response;
    if (!itemId(project)) throw new Error('服务端没有返回项目编号。');
    updateProgress('github_fetch', '仓库快照和本人职责已保存。', { complete: true });
    completeProgress('论文/项目链接已保存，可以开始解读。', { title: '资料保存完成' });
    await loadProfile({ preferredProjectId: itemId(project) });
    elements.githubUrl.value = '';
    elements.name.value = '';
    elements.responsibility.value = '';
    elements.partialScope.checked = false;
    elements.responsibility.classList.add('is-hidden');
    showToast('GitHub 项目已添加，点击“开始项目解读”继续。', 'success');
  } catch (error) {
    const message = error?.message || 'GitHub 项目添加失败，请稍后重试。';
    failProgress(message);
    showToast(message, 'error', 5200);
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

async function saveProjectResponsibility({ mergedFromArchitecture = false } = {}) {
  const id = itemId(selectedProject);
  if (!id) return;
  const responsibility = elements.responsibilityEdit.value.trim();
  const partial = elements.responsibilityScopeEdit.checked || mergedFromArchitecture;
  if (partial && !responsibility) {
    showToast('部分负责时请填写或选择具体组件。', 'error');
    return;
  }
  elements.responsibilitySave.disabled = true;
  elements.responsibilityMerge.disabled = true;
  elements.responsibilitySaveState.textContent = '正在保存…';
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}`, {
      method: 'PATCH', timeout: 15_000, json: {
        client_id: getClientId(), responsibility_scope: partial ? 'partial' : 'all',
        responsibility: partial ? responsibility : '',
      },
    });
    const canonical = response?.project && typeof response.project === 'object'
      ? response.project
      : { ...selectedProject, responsibility: partial ? responsibility : '', responsibility_scope: partial ? 'partial' : 'all' };
    selectedProject = canonical;
    profile.projects = profile.projects.map((project) => (itemId(project) === id ? canonical : project));
    currentAnalysis = null;
    currentQuestions = [];
    elements.questions.replaceChildren();
    renderProjects();
    renderReady();
    elements.responsibilitySaveState.textContent = responsibility ? '已保存 · 需重新解读' : '已清空 · 需重新解读';
    showToast(
      mergedFromArchitecture
        ? '所选组件已合并到本人职责。请重新解读，以生成对应追问。'
        : '本人职责已保存。请重新解读，以免继续使用旧职责题。',
      'success',
      5200,
    );
  } catch (error) {
    elements.responsibilitySaveState.textContent = '保存失败';
    showToast(error?.message || '本人职责保存失败，请稍后重试。', 'error', 5200);
  } finally {
    elements.responsibilitySave.disabled = false;
    elements.responsibilityMerge.disabled = false;
  }
}

async function mergeSelectedArchitectureResponsibilities() {
  const selected = [...elements.architecture.querySelectorAll('input[data-responsibility-text]:checked')]
    .map((input) => input.dataset.responsibilityText?.trim())
    .filter(Boolean);
  if (!selected.length) {
    showToast('请先勾选你确实负责过的架构组件。', 'error');
    return;
  }
  const existing = elements.responsibilityEdit.value.trim();
  const parts = existing ? existing.split(/\n+/).map((value) => value.trim()).filter(Boolean) : [];
  selected.forEach((value) => {
    if (!parts.some((part) => part === value)) parts.push(value);
  });
  elements.responsibilityEdit.value = parts.join('\n');
  elements.responsibilityScopeEdit.checked = true;
  elements.responsibilityEdit.classList.remove('is-hidden');
  await saveProjectResponsibility({ mergedFromArchitecture: true });
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

function flowLabels(projectType) {
  if (projectType === 'paper') return {
    section: '研究结构与证据链',
    title: '方法与实验链',
    review: '证据链检查',
    description: '沿研究问题、核心方法、实验验证到结论梳理论证主线。',
    missing: '尚未梳理出完整的方法与实验链，需补充方法、实验和结论之间的依据。',
  };
  if (projectType === 'technical') return {
    section: '架构与核心运行链路',
    title: '核心运行链路',
    review: '运行链路检查',
    description: '从输入或触发出发，沿核心机制、依赖与状态变化走到可观察结果。',
    missing: '尚未梳理出完整的核心运行链路，需补充入口、机制和结果之间的依据。',
  };
  return {
    section: '架构与核心业务流程',
    title: '核心业务流程',
    review: '业务流程检查',
    description: '从用户动作或业务输入出发，经过核心功能、数据或外部依赖，走到用户可见结果。',
    missing: '尚未梳理出完整的核心业务流程，需补充入口、业务调用和数据读写依据。',
  };
}

function renderFlowReview(value, hasRequestFlow, labels) {
  const review = value && typeof value === 'object' ? value : {};
  const status = hasRequestFlow && ['verified', 'partial', 'needs_verification'].includes(review.status)
    ? review.status
    : 'needs_verification';
  const statusLabels = {
    verified: '已从快照核对',
    partial: '部分核对',
    needs_verification: '待核实',
  };
  elements.flowReviewState.dataset.state = status;
  elements.flowReviewState.textContent = statusLabels[status];
  elements.flowReviewSummary.textContent = textFromValue(review.summary)
    || (hasRequestFlow
      ? '已按当前快照梳理链路；仍应由候选人核对实际调用顺序和失败路径。'
      : labels.missing);
  const issues = toArray(review.issues).filter(Boolean);
  if (!hasRequestFlow) issues.unshift(labels.missing);
  renderList(
    elements.flowIssues,
    issues,
    status === 'verified' ? '当前快照未显示明确的链路矛盾。' : '尚未获得足够证据判断链路问题。',
  );
  renderList(elements.flowAssumptions, review.assumptions, '未声明额外分析假设。');
  renderList(
    elements.flowToVerify,
    review.to_verify,
    hasRequestFlow ? '面试前仍建议用入口、数据读写和失败路径逐步核对。' : '补充路由入口、服务调用、数据读写和失败处理的实际证据。',
  );
}

function renderArchitecture(value) {
  elements.architecture.replaceChildren();
  let selectableCount = 0;
  if (Array.isArray(value)) {
    value.filter(Boolean).forEach((entry, index) => {
      const row = document.createElement('div');
      const componentName = textFromValue(entry?.name || entry?.component || `模块 ${index + 1}`);
      const componentResponsibility = textFromValue(entry?.responsibility || entry?.description || entry) || '暂无说明';
      const title = document.createElement('label');
      title.className = 'project-component-choice';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.dataset.responsibilityText = `${componentName}：${componentResponsibility}`;
      checkbox.checked = selectedProject?.responsibility_scope === 'partial'
        && projectResponsibilityValue().includes(componentName);
      const titleCopy = document.createElement('strong');
      titleCopy.textContent = componentName;
      const choiceCopy = document.createElement('small');
      choiceCopy.textContent = '标记为我负责';
      title.append(checkbox, titleCopy, choiceCopy);
      const copy = document.createElement('p');
      copy.textContent = componentResponsibility;
      row.append(title, copy);
      elements.architecture.append(row);
      selectableCount += 1;
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
  elements.responsibilityMerge.classList.toggle('is-hidden', selectableCount === 0);
}

function practiceStorageKey() {
  return `mock_interview.project_practice.v1.${getClientId()}.${itemId(selectedProject)}`;
}

function isSkillRuleText(value) {
  const text = textFromValue(value);
  return /(请先做自我介绍|听完后服务端|必须另开一题|默认强制\s*[34]\s*层|若深度弱则扩至|当候选人回答|AI\s*必须基于|system\s*prompt|skill\s*规则|元规则)/i.test(text);
}

function questionFingerprint(value) {
  return textFromValue(value).toLocaleLowerCase().replace(/[\s，。！？；：,.!?;:'"“”‘’（）()、]/g, '');
}

function normalizeProjectQuestions(values) {
  const seen = new Set();
  return toArray(values).reduce((items, question) => {
    const source = typeof question === 'object' && question !== null ? question : { question };
    const prompt = textFromValue(source.question || source.prompt || source.title);
    const evidence = toArray(source.evidence).map(textFromValue).filter(Boolean);
    const fingerprint = questionFingerprint(prompt);
    if (!prompt || !fingerprint || seen.has(fingerprint) || isSkillRuleText(prompt) || evidence.length === 0) return items;
    seen.add(fingerprint);
    items.push({ ...source, question: prompt, evidence });
    return items;
  }, []);
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

function renderQuestions(values, { append = false } = {}) {
  persistVisiblePracticeEntries();
  projectPracticeTimers.clear();
  elements.questions.replaceChildren();
  const questions = normalizeProjectQuestions(append ? [...currentQuestions, ...toArray(values)] : values);
  currentQuestions = questions;
  if (!questions.length) {
    const empty = document.createElement('p');
    empty.className = 'project-question-empty';
    empty.textContent = '当前没有带项目依据的可核实追问。可补充关键模块和本人职责后重新解读。';
    elements.questions.append(empty);
    elements.questionStatus.dataset.state = 'error';
    elements.questionStatus.textContent = '未展示缺少项目依据或不属于面试题面的内容';
    return;
  }
  elements.questionStatus.dataset.state = 'done';
  elements.questionStatus.textContent = `${questions.length} 道围绕当前项目的可核实追问`;
  const drafts = readPracticeDrafts();
  questions.forEach((question, index) => {
    const source = typeof question === 'object' ? question : { question };
    const card = document.createElement('article');
    card.className = 'project-question-card';
    const heading = document.createElement('header');
    const number = document.createElement('span');
    number.textContent = String(index + 1).padStart(2, '0');
    const copy = document.createElement('div');
    const interviewer = document.createElement('small');
    interviewer.className = 'project-interviewer-label';
    interviewer.textContent = '面试官 · 项目深挖';
    const title = document.createElement('h4');
    title.textContent = textFromValue(source.question || source.prompt || source.title) || `项目追问 ${index + 1}`;
    const focus = document.createElement('p');
    const focusText = textFromValue(source.focus);
    focus.textContent = focusText && !isSkillRuleText(focusText)
      ? `考察重点：${focusText}`
      : '先讲结论，再用项目中的真实取舍与结果支撑。';
    const relevance = document.createElement('p');
    relevance.className = 'project-question-relevance';
    const relevanceText = textFromValue(source.responsibility_relevance);
    relevance.textContent = relevanceText && !isSkillRuleText(relevanceText)
      ? `与你的职责相关：${relevanceText}`
      : '请明确区分本人工作、团队成果和仍待核实的部分。';
    const evidence = document.createElement('p');
    evidence.className = 'project-question-evidence';
    evidence.textContent = '已基于当前项目材料核对';
    copy.append(interviewer, title, focus, relevance, evidence);
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
    hint.textContent = '可直接查看参考答案，不会自动填入你的回答';
    const reveal = document.createElement('button');
    reveal.type = 'button';
    reveal.textContent = '直接查看答案';
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
      showToast('本题回答已保存，可直接查看参考答案对照。', 'success');
    });
    actions.append(hint, finish, reveal);

    const suggested = document.createElement('div');
    suggested.className = 'project-suggested-answer is-hidden';
    const suggestedTitle = document.createElement('strong');
    suggestedTitle.textContent = '参考答案';
    const suggestedCopy = document.createElement('p');
    const suggestedText = textFromValue(source.suggested_answer || source.answer || source.key_points);
    suggestedCopy.textContent = suggestedText && !isSkillRuleText(suggestedText)
      ? suggestedText
      : '我会先说明项目背景和本人职责，再讲清关键决策、落地过程、真实结果与复盘；没有实际测量的数据不会补写。';
    suggested.append(suggestedTitle, suggestedCopy);
    reveal.addEventListener('click', () => {
      const hidden = suggested.classList.toggle('is-hidden');
      reveal.textContent = hidden ? '直接查看答案' : '收起参考答案';
      reveal.setAttribute('aria-expanded', String(!hidden));
    });

    card.append(heading, label, actions, suggested);
    elements.questions.append(card);
  });
}

function renderAnalysis(payload) {
  const analysis = payload?.analysis && typeof payload.analysis === 'object' ? payload.analysis : payload || {};
  currentAnalysis = analysis;
  elements.analysisName.textContent = itemName(selectedProject);
  const typeLabel = { application: '应用类', technical: '技术类', paper: '论文' }[selectedProject?.project_type] || '应用类';
  const scopeLabel = selectedProject?.responsibility_scope === 'partial' ? '部分负责' : '默认负责整个项目';
  elements.analysisMeta.textContent = `${typeLabel} · ${sourceMeta(selectedProject)} · ${scopeLabel}${payload?.cached ? ' · 已复用最近解读' : ''}`;
  elements.analysisSummary.textContent = textFromValue(analysis.project_summary)
    || '以下结论只基于当前保存的项目资料，可补充更多上下文后重新解读。';
  renderArchitecture(analysis.architecture);
  const requestFlow = toArray(analysis.request_flow).filter(Boolean);
  const labels = flowLabels(selectedProject?.project_type);
  elements.architectureTitle.textContent = labels.section;
  elements.architectureDescription.textContent = labels.description;
  elements.requestFlowTitle.textContent = labels.title;
  elements.flowReviewTitle.textContent = labels.review;
  renderList(elements.requestFlow, requestFlow, labels.missing);
  elements.interviewIntro.textContent = textFromValue(analysis.interview_intro || analysis.interview_introduction)
    || '当前资料不足，暂未生成可在面试中直接使用的项目介绍。';
  renderFlowReview(analysis.request_flow_review || analysis.flow_review, requestFlow.length > 0, labels);
  renderList(elements.technologyChoices, analysis.technology_choices, '当前资料未明确说明技术选型依据。');
  renderList(elements.risks, analysis.risks, '暂未识别到明确风险，仍建议准备容量与故障场景。');
  renderList(elements.improvements, analysis.improvements, '可补充压测数据、监控指标与复盘结论。');
  renderQuestions(analysis.interview_questions);
  renderResponsibilityEditor();
  elements.moreQuestions.disabled = false;
  elements.regenerateQuestions.disabled = false;
  setVisible(elements.ready, false);
  setVisible(elements.loading, false);
  setVisible(elements.analysis, true);
  window.scrollTo({ top: Math.max(0, elements.analysis.offsetTop - 90), behavior: 'smooth' });
}

function analysisStageFromEvent(stage) {
  if (stage === 'cache_check') return 'reading';
  return ANALYSIS_PROGRESS_STAGES.some((item) => item.id === stage) ? stage : '';
}

async function responseErrorMessage(response) {
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('json')
    ? await response.json().catch(() => null)
    : await response.text().catch(() => '');
  return payload?.detail?.message || payload?.error?.message || payload?.detail || payload?.message
    || `请求失败（${response.status}）`;
}

async function streamProjectAnalysis(id, { refresh = false } = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(`/api/profile/projects/${encodeURIComponent(id)}/analysis/stream`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Profile-Key': getClientId(),
      },
      body: JSON.stringify({ client_id: getClientId(), refresh: Boolean(refresh) }),
      signal: controller.signal,
    });
    if ([404, 405].includes(response.status)) {
      const unsupported = new Error('当前服务端暂不支持流式解读。');
      unsupported.streamUnsupported = true;
      throw unsupported;
    }
    if (!response.ok) throw new Error(await responseErrorMessage(response));
    if (!response.body) throw new Error('服务端没有返回可读取的解读进度。');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result = null;
    const handleLine = (line) => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch { throw new Error('服务端返回了无法识别的解读进度。'); }
      if (event?.type === 'progress') {
        const stage = analysisStageFromEvent(String(event.stage || ''));
        if (stage) updateProgress(stage, textFromValue(event.message));
      } else if (event?.type === 'complete') {
        result = event.result;
      } else if (event?.type === 'error') {
        throw new Error(event?.error?.message || '项目解读失败。');
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      lines.forEach(handleLine);
      if (done) break;
    }
    if (buffer.trim()) handleLine(buffer);
    if (!result?.analysis) throw new Error('解读流已结束，但没有返回完整分析。');
    return result;
  } catch (error) {
    if (error?.name === 'AbortError') throw new Error('项目解读超时，请检查网络后重试。');
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function requestProjectAnalysis(id, { refresh = false } = {}) {
  try {
    return await streamProjectAnalysis(id, { refresh });
  } catch (error) {
    if (!error?.streamUnsupported) throw error;
    updateProgress('generating', '当前服务端使用兼容模式，正在生成完整解读。');
    return apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/analysis`, {
      method: 'POST', timeout: 90_000, json: { client_id: getClientId(), refresh: Boolean(refresh) },
    });
  }
}

async function analyzeProject({ refresh = false } = {}) {
  const id = itemId(selectedProject);
  if (!id) return;
  setButtonBusy(elements.analyze, true, '正在解读项目…');
  elements.refresh.disabled = true;
  elements.moreQuestions.disabled = true;
  elements.regenerateQuestions.disabled = true;
  beginProgress(
    ANALYSIS_PROGRESS_STAGES,
    '正在读取项目资料…',
    '服务端正在读取已保存的项目快照，完成后才会进入后续步骤。',
  );
  updateProgress('reading', '正在读取已保存的文本与源码快照，不会运行项目代码。');
  try {
    const response = await requestProjectAnalysis(id, { refresh });
    completeProgress(
      response?.cached ? '已复用与当前项目及职责一致的最近解读。' : '架构、链路检查、项目介绍和项目追问均已返回。',
      { reused: Boolean(response?.cached), title: '项目解读完成' },
    );
    renderAnalysis(response);
  } catch (error) {
    const message = error?.message || '项目解读失败，请稍后重试。';
    failProgress(message);
    showToast(message, 'error', 6000);
  } finally {
    setButtonBusy(elements.analyze, false);
    elements.refresh.disabled = false;
    if (!questionsBusy) {
      elements.moreQuestions.disabled = false;
      elements.regenerateQuestions.disabled = false;
    }
  }
}

async function generateProjectQuestions(mode) {
  const id = itemId(selectedProject);
  if (!id || questionsBusy || !['more', 'regenerate'].includes(mode)) return;
  questionsBusy = true;
  const append = mode === 'more';
  const beforeCount = currentQuestions.length;
  elements.moreQuestions.disabled = true;
  elements.regenerateQuestions.disabled = true;
  elements.questionStatus.dataset.state = 'loading';
  elements.questionStatus.textContent = append
    ? '面试官正在沿当前项目继续追问…'
    : '面试官正在根据项目与本人职责重新出题…';
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/questions`, {
      method: 'POST', timeout: 90_000, json: {
        client_id: getClientId(),
        mode,
        count: 3,
        existing_questions: currentQuestions
          .map((question) => textFromValue(question.question))
          .filter(Boolean)
          .slice(-30),
      },
    });
    const batch = normalizeProjectQuestions(response?.questions || response?.interview_questions);
    if (!batch.length) throw new Error('服务端没有返回带项目依据且可核实的新题。');
    renderQuestions(batch, { append });
    const added = Math.max(0, currentQuestions.length - (append ? beforeCount : 0));
    if (!added) {
      elements.questionStatus.dataset.state = 'error';
      elements.questionStatus.textContent = '没有返回新的、带项目依据且可核实的题目';
      showToast('这次没有生成可展示的新题，请补充职责或项目证据后重试。', 'error', 5200);
    } else {
      elements.questionStatus.dataset.state = 'done';
      elements.questionStatus.textContent = append
        ? `已增加 ${added} 道项目追问 · 共 ${currentQuestions.length} 道`
        : `已重新生成 ${currentQuestions.length} 道项目追问`;
      showToast(append ? `已增加 ${added} 道项目追问。` : '项目追问已重新生成。', 'success');
    }
  } catch (error) {
    elements.questionStatus.dataset.state = 'error';
    elements.questionStatus.textContent = error?.message || '题目生成失败，请稍后重试';
    showToast(error?.message || '题目生成失败，请稍后重试。', 'error', 5200);
  } finally {
    questionsBusy = false;
    elements.moreQuestions.disabled = false;
    elements.regenerateQuestions.disabled = false;
  }
}

elements.files.addEventListener('change', uploadProjectFiles);
elements.partialScope.addEventListener('change', () => {
  elements.responsibility.classList.toggle('is-hidden', !elements.partialScope.checked);
  if (elements.partialScope.checked) elements.responsibility.focus();
});
elements.type.addEventListener('change', () => {
  elements.files.accept = elements.type.value === 'paper'
    ? '.pdf,.zip,.md,.txt,.py,.json,.yaml,.yml'
    : '.zip,.md,.txt,.json,.yaml,.yml,.toml,.ini,.conf,.py,.java,.go,.js,.ts,.tsx,.jsx,.sql,.xml,.proto,.c,.cc,.cpp,.h,.hpp,.rs,.rb,.php,.kt,.swift,.sh';
});
elements.githubAdd.addEventListener('click', addGithubProject);
elements.githubUrl.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addGithubProject();
});
elements.analyze.addEventListener('click', () => analyzeProject());
elements.refresh.addEventListener('click', () => analyzeProject({ refresh: true }));
elements.responsibilitySave.addEventListener('click', () => saveProjectResponsibility());
elements.responsibilityScopeEdit.addEventListener('change', () => {
  elements.responsibilityEdit.classList.toggle('is-hidden', !elements.responsibilityScopeEdit.checked);
  elements.responsibilitySaveState.textContent = '有未保存修改';
});
elements.responsibilityMerge.addEventListener('click', mergeSelectedArchitectureResponsibilities);
elements.responsibilityEdit.addEventListener('input', () => {
  elements.responsibilitySaveState.textContent = '有未保存修改';
});
elements.progressBack.addEventListener('click', () => {
  if (currentAnalysis) renderAnalysis({ analysis: currentAnalysis, cached: true });
  else renderReady();
});
elements.interviewIntroCopy.addEventListener('click', async () => {
  const value = elements.interviewIntro.textContent.trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showToast('项目介绍已复制。', 'success');
  } catch {
    showToast('浏览器未允许自动复制，请手动选择文本。', 'error');
  }
});
elements.moreQuestions.addEventListener('click', () => generateProjectQuestions('more'));
elements.regenerateQuestions.addEventListener('click', () => generateProjectQuestions('regenerate'));
window.addEventListener('pagehide', () => {
  persistVisiblePracticeEntries();
  clearInterval(projectPracticeTicker);
});

loadProfile();
