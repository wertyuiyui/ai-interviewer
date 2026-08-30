import {
  $, $$, apiFetch, formatDate, getClientId, getSavedSetup, modeLabel,
  normalizeHistoryPayload, normalizeMode, saveSetup, setButtonBusy,
  setCurrentSession, showToast, firstValue, toArray,
} from './common.js?v=20260830-profile-bank-v2';
import { createHardwareTest } from './hardware-test.js?v=20260830-interview-flow';

const form = $('#setupForm');
const fileInput = $('#resumeFile');
const textInput = $('#resumeText');
const dropZone = $('#dropZone');
const startButton = $('#startButton');
const specializationPreset = $('#specializationPreset');
const specializationCustom = $('#specializationCustom');
const durationPreset = $('#durationPreset');
const durationCustom = $('#durationCustom');
const durationCustomWrap = $('#durationCustomWrap');
const memoryEnabled = $('#memoryEnabled');
const resumeAlert = $('#resumeAlert');
const settingsAlert = $('#settingsAlert');
const modePill = $('#modePill');
const profilePanel = $('#profilePanel');
const profileStatus = $('#profileStatus');
const profileResumeFiles = $('#profileResumeFiles');
const profileProjectFiles = $('#profileProjectFiles');
const profileProjectName = $('#profileProjectName');
const profileProjectResponsibility = $('#profileProjectResponsibility');
const profileGithubUrl = $('#profileGithubUrl');
const profileGithubAdd = $('#profileGithubAdd');
const profileProjectProgress = $('#profileProjectProgress');
const hardwareTest = createHardwareTest();

let resumeMode = 'pdf';
let selectedFile = null;
let selectedResumeId = '';
let profile = { resumes: [], projects: [], selected_project_id: '' };
let stressTouched = false;
let serverMode = 'L3';

const PROFILE_RESUME_KEY = 'mock_interview.profile_resume.v1';

const stressDefaults = {
  bytedance: 2, meituan: 0, tencent: 0, alibaba: 0, baidu: 0, huawei: 0,
};
const stressHints = {
  0: '关闭施压手法，仍会保留连续深挖',
  1: '温和施压：适度质疑，并给你整理思路的空间',
  2: '标准施压：针对模糊、矛盾和技术漏洞连续下钻',
  3: '高压模式：提高问题深度，仅在明显跑题或表述失控时打断',
};
const interviewTypeHints = {
  technical: '聚焦项目、实习、技术基础与口述解题思路',
  hr: '聚焦求职动机、行为经历、协作方式、职业规划与岗位匹配',
  technical_hr: '技术考察后继续聊价值观、职业选择、规划与薪酬期待',
};
const languageModeHints = {
  zh: '全程使用中文，常见英文技术术语会保留原文',
  bilingual: '中英双语会保留常用英文术语，并可能追问英文表达',
  en: '面试官的开场、技术追问、综合题与结束语都只使用英文',
};

function setResumeMode(nextMode, focus = false) {
  resumeMode = ['saved', 'text'].includes(nextMode) ? nextMode : 'pdf';
  $$('[data-resume-tab]').forEach((button) => {
    const active = button.dataset.resumeTab === resumeMode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $('#savedPanel').classList.toggle('is-hidden', resumeMode !== 'saved');
  $('#pdfPanel').classList.toggle('is-hidden', resumeMode !== 'pdf');
  $('#textPanel').classList.toggle('is-hidden', resumeMode !== 'text');
  hideResumeAlert();
  if (focus) {
    const target = resumeMode === 'saved' ? $('#openProfileButton') : resumeMode === 'pdf' ? fileInput : textInput;
    target?.focus();
  }
}

function hideResumeAlert() {
  resumeAlert.classList.add('is-hidden');
  resumeAlert.classList.remove('is-success');
  resumeAlert.textContent = '';
}

function showResumeAlert(message, success = false) {
  resumeAlert.textContent = message;
  resumeAlert.classList.remove('is-hidden');
  resumeAlert.classList.toggle('is-success', success);
}

function hideSettingsAlert() {
  settingsAlert.classList.add('is-hidden');
  settingsAlert.textContent = '';
}

function showSettingsAlert(message) {
  settingsAlert.textContent = message;
  settingsAlert.classList.remove('is-hidden');
}

function profileItemId(item) {
  return String(item?.id || item?.resume_id || item?.project_id || '');
}

function readSelectedResumeId() {
  try { return localStorage.getItem(PROFILE_RESUME_KEY) || ''; } catch { return ''; }
}

function saveSelectedResumeId(id) {
  try {
    if (id) localStorage.setItem(PROFILE_RESUME_KEY, id);
    else localStorage.removeItem(PROFILE_RESUME_KEY);
  } catch { /* 匿名资料选择仍在当前页面有效。 */ }
}

function getStructuredResume(item) {
  const resume = item?.parsed_resume || item?.structured_resume || item?.resume || item?.data;
  if (!resume || typeof resume !== 'object' || Array.isArray(resume)) return null;
  const required = ['教育', '实习经历', '项目', '技能'];
  return required.every((key) => Array.isArray(resume[key])) ? resume : null;
}

function profileItemName(item, fallback) {
  return String(item?.name || item?.file_name || item?.title || fallback).trim();
}

function updateProfileStatus(message = '') {
  if (!profileStatus) return;
  profileStatus.textContent = message || `${profile.resumes.length} 份简历 · ${profile.projects.length} 个项目 · 当前设备`;
}

function openProfileFromHash() {
  if (!profilePanel || !['#profilePanel', '#profile'].includes(window.location.hash)) return;
  profilePanel.open = true;
  window.requestAnimationFrame(() => {
    profilePanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function updateProfileProjectProgress(state, message) {
  if (!profileProjectProgress) return;
  profileProjectProgress.dataset.state = state;
  profileProjectProgress.textContent = message;
  profileProjectProgress.classList.toggle('is-hidden', !message);
}

async function readProfileProjectFileList(files) {
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    updateProfileProjectProgress('reading', `正在读取文件清单（${index + 1}/${files.length}）：${file.name}`);
    await file.slice(0, Math.min(file.size, 64 * 1024)).arrayBuffer();
  }
}

function createProfileEmpty(message) {
  const item = document.createElement('li');
  item.className = 'profile-empty';
  item.textContent = message;
  return item;
}

function selectSavedResume(id, { switchMode = true } = {}) {
  const target = profile.resumes.find((item) => profileItemId(item) === String(id || ''));
  selectedResumeId = target && getStructuredResume(target) ? profileItemId(target) : '';
  saveSelectedResumeId(selectedResumeId);
  const choice = $('#savedResumeChoice');
  if (choice) choice.textContent = selectedResumeId
    ? profileItemName(target, '已保存简历')
    : '尚未选择可直接开面的简历';
  $$('input[name="profile_resume"]').forEach((input) => {
    input.checked = input.value === selectedResumeId;
  });
  if (switchMode && selectedResumeId) setResumeMode('saved');
}

function renderProfileResumes() {
  const list = $('#profileResumeList');
  if (!list) return;
  list.replaceChildren();
  if (!profile.resumes.length) {
    list.append(createProfileEmpty('还没有保存简历，可一次选择多份 PDF。'));
    selectSavedResume('', { switchMode: false });
    return;
  }
  profile.resumes.forEach((resume) => {
    const id = profileItemId(resume);
    const ready = Boolean(getStructuredResume(resume));
    const item = document.createElement('li');
    item.className = `profile-list-item${id === selectedResumeId ? ' is-selected' : ''}`;

    const label = document.createElement('label');
    label.className = 'profile-item-select';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'profile_resume';
    radio.value = id;
    radio.checked = id === selectedResumeId;
    radio.disabled = !ready;
    radio.addEventListener('change', () => {
      selectSavedResume(id);
      renderProfileResumes();
      showToast(`已选择“${profileItemName(resume, '简历')}”用于完整模拟。`, 'success');
    });
    const icon = document.createElement('span');
    icon.className = 'profile-item-icon';
    icon.textContent = '历';
    const copy = document.createElement('span');
    copy.className = 'profile-item-copy';
    const title = document.createElement('strong');
    title.textContent = profileItemName(resume, '未命名简历');
    const meta = document.createElement('small');
    meta.textContent = ready
      ? `可直接开面 · ${formatDate(resume.created_at, false)}`
      : '解析未完成，暂不能直接开面';
    copy.append(title, meta);
    label.append(radio, icon, copy);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'profile-delete';
    remove.setAttribute('aria-label', `删除简历 ${title.textContent}`);
    remove.textContent = '删除';
    remove.addEventListener('click', () => deleteProfileItem('resumes', resume));
    item.append(label, remove);
    list.append(item);
  });
}

function renderProfileProjects() {
  const list = $('#profileProjectList');
  if (!list) return;
  list.replaceChildren();
  const noProject = document.createElement('li');
  noProject.className = `profile-list-item profile-no-project${profile.selected_project_id ? '' : ' is-selected'}`;
  const noProjectLabel = document.createElement('label');
  noProjectLabel.className = 'profile-item-select';
  const noProjectRadio = document.createElement('input');
  noProjectRadio.type = 'radio';
  noProjectRadio.name = 'profile_project';
  noProjectRadio.value = '';
  noProjectRadio.checked = !profile.selected_project_id;
  noProjectRadio.addEventListener('change', clearProfileProject);
  const noProjectIcon = document.createElement('span');
  noProjectIcon.className = 'profile-item-icon project';
  noProjectIcon.textContent = '—';
  const noProjectCopy = document.createElement('span');
  noProjectCopy.className = 'profile-item-copy';
  const noProjectTitle = document.createElement('strong');
  noProjectTitle.textContent = '不使用项目';
  const noProjectMeta = document.createElement('small');
  noProjectMeta.textContent = '本场只根据简历组织问题';
  noProjectCopy.append(noProjectTitle, noProjectMeta);
  noProjectLabel.append(noProjectRadio, noProjectIcon, noProjectCopy);
  noProject.append(noProjectLabel);
  list.append(noProject);
  if (!profile.projects.length) {
    list.append(createProfileEmpty('还没有项目，可上传多个文件或添加 GitHub 链接。'));
    return;
  }
  profile.projects.forEach((project) => {
    const id = profileItemId(project);
    const selected = id === String(profile.selected_project_id || '') || project?.selected === true;
    const item = document.createElement('li');
    item.className = `profile-list-item${selected ? ' is-selected' : ''}`;
    const label = document.createElement('label');
    label.className = 'profile-item-select';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'profile_project';
    radio.value = id;
    radio.checked = selected;
    radio.addEventListener('change', () => selectProfileProject(project));
    const icon = document.createElement('span');
    icon.className = 'profile-item-icon project';
    icon.textContent = project?.source_type === 'github' ? 'GH' : '项';
    const copy = document.createElement('span');
    copy.className = 'profile-item-copy';
    const title = document.createElement('strong');
    title.textContent = profileItemName(project, '未命名项目');
    const meta = document.createElement('small');
    const fileCount = Array.isArray(project?.files) ? project.files.length : 0;
    meta.textContent = project?.source_type === 'github'
      ? 'GitHub 仓库 · 可进入项目解读'
      : `${fileCount || '多'} 个文件 · 可进入项目解读`;
    if (String(project?.responsibility || '').trim()) meta.textContent += ' · 已填写本人职责';
    copy.append(title, meta);
    label.append(radio, icon, copy);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'profile-delete';
    remove.setAttribute('aria-label', `删除项目 ${title.textContent}`);
    remove.textContent = '删除';
    remove.addEventListener('click', () => deleteProfileItem('projects', project));
    item.append(label, remove);
    list.append(item);
  });
}

function normalizeProfile(payload) {
  const source = payload?.profile && typeof payload.profile === 'object' ? payload.profile : payload || {};
  return {
    resumes: Array.isArray(source.resumes) ? source.resumes : [],
    projects: Array.isArray(source.projects) ? source.projects : [],
    selected_project_id: String(source.selected_project_id || ''),
  };
}

async function loadProfile({ preferredResumeId = '' } = {}) {
  updateProfileStatus('正在同步当前设备资料…');
  try {
    const payload = await apiFetch('/api/profile', { timeout: 15_000 });
    profile = normalizeProfile(payload);
    const candidate = preferredResumeId || selectedResumeId || readSelectedResumeId();
    const fallback = profile.resumes.find((item) => getStructuredResume(item));
    selectedResumeId = profile.resumes.some((item) => profileItemId(item) === candidate && getStructuredResume(item))
      ? candidate
      : profileItemId(fallback);
    selectSavedResume(selectedResumeId, { switchMode: false });
    renderProfileResumes();
    renderProfileProjects();
    updateProfileStatus();
  } catch (error) {
    updateProfileStatus('资料暂时无法同步，仍可临时上传简历');
    profile = { resumes: [], projects: [], selected_project_id: '' };
    renderProfileResumes();
    renderProfileProjects();
  }
}

async function uploadProfileResumes() {
  const files = [...(profileResumeFiles?.files || [])];
  if (!files.length) return;
  const valid = files.filter((file) => (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) && file.size <= 8 * 1024 * 1024);
  if (valid.length !== files.length) showToast('已跳过非 PDF 或超过 8 MB 的简历。', 'error', 5200);
  if (!valid.length) {
    profileResumeFiles.value = '';
    return;
  }
  profileResumeFiles.disabled = true;
  updateProfileStatus(`正在保存 ${valid.length} 份简历…`);
  let latestId = '';
  let saved = 0;
  try {
    for (const file of valid) {
      const data = new FormData();
      data.append('client_id', getClientId());
      data.append('name', file.name.replace(/\.pdf$/i, ''));
      data.append('file', file, file.name);
      try {
        const response = await apiFetch('/api/profile/resumes', { method: 'POST', body: data, timeout: 65_000 });
        latestId = profileItemId(response?.resume || response);
        saved += 1;
      } catch (error) {
        showToast(`${file.name}：${error?.message || '保存失败'}`, 'error', 5200);
      }
    }
    await loadProfile({ preferredResumeId: latestId });
    if (saved) {
      selectSavedResume(latestId || selectedResumeId);
      showToast(`已保存 ${saved} 份简历，并选中最新一份。`, 'success');
    }
  } finally {
    profileResumeFiles.disabled = false;
    profileResumeFiles.value = '';
  }
}

async function uploadProfileProject() {
  const files = [...(profileProjectFiles?.files || [])];
  if (!files.length) return;
  profileProjectFiles.disabled = true;
  updateProfileProjectProgress('reading', '正在读取所选项目文件…');
  updateProfileStatus(`正在保存项目的 ${files.length} 个文件…`);
  try {
    await readProfileProjectFileList(files);
    const data = new FormData();
    data.append('client_id', getClientId());
    data.append('name', profileProjectName.value.trim() || files[0].name.replace(/\.[^.]+$/, ''));
    data.append('responsibility', profileProjectResponsibility.value.trim());
    files.forEach((file) => data.append('files', file, file.name));
    updateProfileProjectProgress('saving', `已读取 ${files.length} 个文件，正在上传并保存项目…`);
    const response = await apiFetch('/api/profile/projects', { method: 'POST', body: data, timeout: 65_000 });
    const project = response?.project || response;
    await loadProfile();
    if (profileItemId(project)) await selectProfileProject(project, { quiet: true });
    profileProjectName.value = '';
    profileProjectResponsibility.value = '';
    updateProfileProjectProgress('done', '项目文件与本人职责已保存，可以进入项目解读。');
    showToast('项目文件已保存，可以进入项目解读。', 'success');
  } catch (error) {
    updateProfileProjectProgress('error', error?.message || '项目文件保存失败，请稍后重试。');
    showToast(error?.message || '项目文件保存失败，请稍后重试。', 'error', 5200);
    updateProfileStatus();
  } finally {
    profileProjectFiles.disabled = false;
    profileProjectFiles.value = '';
  }
}

async function addGithubProject() {
  const url = profileGithubUrl.value.trim();
  let repositoryName = '';
  try {
    const parsed = new URL(url);
    const parts = parsed.pathname.split('/').filter(Boolean);
    const repository = (parts[1] || '').replace(/\.git$/i, '');
    if (parsed.protocol !== 'https:' || parsed.hostname.toLowerCase() !== 'github.com'
      || parts.length !== 2 || !parts[0] || !repository || parsed.search || parsed.hash) throw new Error();
    repositoryName = repository;
  } catch {
    showToast('请输入完整的 GitHub 仓库链接。', 'error');
    profileGithubUrl.focus();
    return;
  }
  profileGithubAdd.disabled = true;
  updateProfileProjectProgress('reading', '正在读取公开 GitHub 仓库…');
  updateProfileStatus('正在添加 GitHub 项目…');
  try {
    const response = await apiFetch('/api/profile/projects/github', {
      method: 'POST', timeout: 35_000, json: {
        client_id: getClientId(),
        name: profileProjectName.value.trim() || repositoryName,
        url,
        responsibility: profileProjectResponsibility.value.trim(),
      },
    });
    const project = response?.project || response;
    await loadProfile();
    if (profileItemId(project)) await selectProfileProject(project, { quiet: true });
    profileGithubUrl.value = '';
    profileProjectName.value = '';
    profileProjectResponsibility.value = '';
    updateProfileProjectProgress('done', 'GitHub 项目与本人职责已保存，可以进入项目解读。');
    showToast('GitHub 项目已添加，可以开始解读。', 'success');
  } catch (error) {
    updateProfileProjectProgress('error', error?.message || 'GitHub 项目添加失败，请稍后重试。');
    showToast(error?.message || 'GitHub 项目添加失败，请稍后重试。', 'error', 5200);
    updateProfileStatus();
  } finally {
    profileGithubAdd.disabled = false;
  }
}

async function selectProfileProject(project, { quiet = false } = {}) {
  const id = profileItemId(project);
  if (!id) return;
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/selection`, {
      method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), selected: true },
    });
    profile.selected_project_id = String(response?.selected_project_id || id);
    profile.projects = profile.projects.map((item) => ({ ...item, selected: profileItemId(item) === profile.selected_project_id }));
    renderProfileProjects();
    if (!quiet) showToast(`已选择“${profileItemName(project, '项目')}”。`, 'success');
  } catch (error) {
    renderProfileProjects();
    showToast(error?.message || '项目选择失败，请稍后重试。', 'error');
  }
}

async function clearProfileProject() {
  const selectedId = String(profile.selected_project_id || '');
  if (!selectedId) {
    profile.projects = profile.projects.map((item) => ({ ...item, selected: false }));
    renderProfileProjects();
    return;
  }
  try {
    await apiFetch(`/api/profile/projects/${encodeURIComponent(selectedId)}/selection`, {
      method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), selected: false },
    });
    profile.selected_project_id = '';
    profile.projects = profile.projects.map((item) => ({ ...item, selected: false }));
    renderProfileProjects();
    showToast('本场将不附加项目资料。', 'success');
  } catch (error) {
    renderProfileProjects();
    showToast(error?.message || '项目选择清除失败，请稍后重试。', 'error');
  }
}

async function deleteProfileItem(kind, item) {
  const id = profileItemId(item);
  const label = kind === 'resumes' ? '简历' : '项目';
  const name = profileItemName(item, label);
  if (!id || !window.confirm(`确定删除“${name}”吗？删除后无法在此设备档案中恢复。`)) return;
  try {
    await apiFetch(`/api/profile/${kind}/${encodeURIComponent(id)}`, {
      method: 'DELETE', timeout: 15_000,
    });
    if (kind === 'resumes' && id === selectedResumeId) selectedResumeId = '';
    await loadProfile();
    showToast(`${label}已删除。`, 'success');
  } catch (error) {
    showToast(error?.message || `${label}删除失败，请稍后重试。`, 'error', 5200);
  }
}

function setFile(file) {
  hideResumeAlert();
  if (!file) {
    selectedFile = null;
    fileInput.value = '';
    dropZone.classList.remove('has-file');
    $('#fileLabel').textContent = '拖入 PDF，或点击选择';
    $('#fileMeta').textContent = '仅支持带文字层的 PDF，不超过 8 MB';
    return;
  }
  const looksLikePdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
  if (!looksLikePdf) {
    setFile(null);
    showResumeAlert('请选择 PDF 文件；Word 简历可以复制文字后继续。');
    return;
  }
  if (file.size > 8 * 1024 * 1024) {
    setFile(null);
    showResumeAlert('文件超过 8 MB，请压缩后重试，或直接粘贴简历文字。');
    return;
  }
  selectedFile = file;
  dropZone.classList.add('has-file');
  $('#fileLabel').textContent = file.name;
  $('#fileMeta').textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB · 已选择，开始时自动解析`;
}

function updateCompanySelection({ applyDefault = true } = {}) {
  const selected = $('input[name="company"]:checked')?.value || 'bytedance';
  $$('.company-option').forEach((option) => {
    option.classList.toggle('is-selected', $('input', option)?.checked === true);
  });
  if (applyDefault && !stressTouched) setStressLevel(stressDefaults[selected]);
  $('#stressHint').textContent = stressHints[getStressLevel()];
}

function getStressLevel() {
  const value = Number($('input[name="stress_level"]:checked')?.value ?? 0);
  return Number.isInteger(value) && value >= 0 && value <= 3 ? value : 0;
}

function setStressLevel(level) {
  const normalized = Math.min(3, Math.max(0, Number(level) || 0));
  const target = $(`input[name="stress_level"][value="${normalized}"]`);
  if (target) target.checked = true;
}

function getLanguageMode() {
  const value = $('input[name="language_mode"]:checked')?.value;
  return ['zh', 'bilingual', 'en'].includes(value) ? value : 'bilingual';
}

function setLanguageMode(mode) {
  const normalized = ['zh', 'bilingual', 'en'].includes(mode) ? mode : 'bilingual';
  const target = $(`input[name="language_mode"][value="${normalized}"]`);
  if (target) target.checked = true;
  $('#languageHint').textContent = languageModeHints[normalized];
}

function getInterviewType() {
  const value = $('input[name="interview_type"]:checked')?.value;
  return ['technical', 'hr', 'technical_hr'].includes(value) ? value : 'technical';
}

function setInterviewType(type) {
  const normalized = ['technical', 'hr', 'technical_hr'].includes(type) ? type : 'technical';
  const target = $(`input[name="interview_type"][value="${normalized}"]`);
  if (target) target.checked = true;
  $('#interviewTypeHint').textContent = interviewTypeHints[normalized];
}

function syncSpecializationControl({ focus = false } = {}) {
  const custom = specializationPreset.value === 'custom';
  specializationCustom.classList.toggle('is-hidden', !custom);
  if (custom && focus) specializationCustom.focus();
  hideSettingsAlert();
}

function setSpecialization(value) {
  const normalized = String(value || '').trim();
  const option = [...specializationPreset.options].find((item) => item.value !== 'custom' && item.value === normalized);
  if (option) {
    specializationPreset.value = option.value;
    specializationCustom.value = '';
  } else if (normalized) {
    specializationPreset.value = 'custom';
    specializationCustom.value = normalized;
  }
  syncSpecializationControl();
}

function getSpecialization() {
  const value = specializationPreset.value === 'custom'
    ? specializationCustom.value.trim()
    : specializationPreset.value.trim();
  if (!value) throw new Error('请输入自定义岗位细分方向。');
  return value;
}

function applySpecializationCatalog(values) {
  if (!Array.isArray(values) || !values.length) return;
  const previous = specializationPreset.value === 'custom'
    ? specializationCustom.value.trim()
    : specializationPreset.value.trim();
  const catalog = [...new Set(values.map((item) => String(item || '').trim()).filter(Boolean))];
  if (!catalog.length) return;
  specializationPreset.replaceChildren();
  catalog.forEach((label, index) => {
    const option = document.createElement('option');
    option.value = label;
    option.textContent = label;
    if ((!previous && index === 0) || label === previous) option.selected = true;
    specializationPreset.append(option);
  });
  const custom = document.createElement('option');
  custom.value = 'custom';
  custom.textContent = '自定义方向…';
  specializationPreset.append(custom);
  if (previous && !catalog.includes(previous)) {
    specializationPreset.value = 'custom';
    specializationCustom.value = previous;
  }
  syncSpecializationControl();
}

function syncDurationControl({ focus = false } = {}) {
  const custom = durationPreset.value === 'custom';
  const infinite = durationPreset.value === 'infinite';
  durationCustomWrap.classList.toggle('is-hidden', !custom);
  $('#durationHint').textContent = infinite
    ? '不限时模式不会自动结束，请在面试页手动结束'
    : custom ? '支持 1–180 分钟，结束后保留完整反馈' : '短场也会保留完整反馈';
  if (custom && focus) durationCustom.focus();
  hideSettingsAlert();
}

function setDuration(value) {
  if (value === null) {
    durationPreset.value = 'infinite';
  } else {
    const minutes = Number(value);
    const preset = [...durationPreset.options].find((item) => ['10', '15', '25'].includes(item.value) && Number(item.value) === minutes);
    if (preset) durationPreset.value = preset.value;
    else if (Number.isInteger(minutes) && minutes > 0) {
      durationPreset.value = 'custom';
      durationCustom.value = String(minutes);
    }
  }
  syncDurationControl();
}

function getDurationMinutes() {
  if (durationPreset.value === 'infinite') return null;
  const value = durationPreset.value === 'custom' ? Number(durationCustom.value) : Number(durationPreset.value);
  if (!Number.isInteger(value) || value < 1 || value > 180) {
    throw new Error('自定义面试时长请输入 1–180 之间的整数分钟。');
  }
  return value;
}

function restoreSetup() {
  const saved = getSavedSetup();
  if (!saved || typeof saved !== 'object') return;
  const company = $$('input[name="company"]').find((input) => input.value === String(saved.company || ''));
  if (company) company.checked = true;
  if (Object.hasOwn(saved, 'duration_minutes')) setDuration(saved.duration_minutes);
  if (saved.specialization) setSpecialization(saved.specialization);
  if (saved.language_mode) setLanguageMode(saved.language_mode);
  if (saved.interview_type) setInterviewType(saved.interview_type);
  if (saved.stress_level !== undefined || typeof saved.stress === 'boolean') {
    setStressLevel(saved.stress_level ?? (saved.stress ? 2 : 0));
    stressTouched = true;
  }
  if (typeof saved.memory_enabled === 'boolean') memoryEnabled.checked = saved.memory_enabled;
  syncMemoryControl();
  updateCompanySelection({ applyDefault: false });
}

function syncMemoryControl() {
  $('#memoryHint').textContent = memoryEnabled.checked
    ? '开启后，下一场会自动加练本场弱项'
    : '本场仍生成报告，但不会参与后续弱项加权';
  if (!memoryEnabled.checked) $('#weaknessCard').classList.add('is-hidden');
}

function extractWeakTopics(payload) {
  const serverTopics = toArray(payload?.weak_topics)
    .map((item) => typeof item === 'string' ? item : firstValue(item, ['topic', 'name'], ''))
    .filter(Boolean);
  if (serverTopics.length) return serverTopics.slice(0, 3);
  const rows = normalizeHistoryPayload(payload);
  if (!rows.length) return [];
  const latestRow = rows[0]?.report || rows[0];
  const direct = firstValue(latestRow, ['weak_topics', 'weaknesses', 'must_practice', 'practice_list', '下次必练清单'], []);
  const directTopics = toArray(direct).map((item) => {
    if (typeof item === 'string') return item;
    return firstValue(item, ['topic', 'name', 'title', 'knowledge_point', '知识点'], '');
  }).filter(Boolean);
  if (directTopics.length) return directTopics.slice(0, 3);

  const scores = firstValue(latestRow, ['topic_scores', 'knowledge_scores', '知识点得分'], {});
  if (!scores || typeof scores !== 'object') return [];
  return Object.entries(scores)
    .map(([topic, score]) => [topic, Number(typeof score === 'object' ? score.score : score)])
    .filter(([, score]) => Number.isFinite(score))
    .sort((a, b) => a[1] - b[1])
    .slice(0, 3)
    .map(([topic]) => topic);
}

async function loadConfig() {
  try {
    const config = await apiFetch('/api/config', { timeout: 8_000 });
    applySpecializationCatalog(config?.specializations);
    serverMode = normalizeMode(config?.voice_mode || config?.mode || config?.VOICE_MODE);
    $('span', modePill).textContent = modeLabel(serverMode);
    modePill.classList.remove('is-loading');
    modePill.title = serverMode === 'L3' ? '本场使用文字对话，不会请求麦克风' : '本场支持实时语音与打断';
  } catch (error) {
    serverMode = 'L3';
    $('span', modePill).textContent = '服务状态待确认';
    modePill.classList.remove('is-loading');
    modePill.classList.add('is-offline');
    modePill.title = error.message;
  }
}

async function loadWeakness() {
  if (!memoryEnabled.checked) return;
  try {
    const clientId = getClientId();
    const history = await apiFetch(`/api/history?client_id=${encodeURIComponent(clientId)}`, { timeout: 8_000 });
    const topics = extractWeakTopics(history);
    if (!topics.length || !memoryEnabled.checked) return;
    $('#weaknessText').textContent = topics.join(' · ');
    $('#weaknessCard').classList.remove('is-hidden');
  } catch {
    // 历史提示不影响新建面试。
  }
}

function validateResume() {
  if (resumeMode === 'saved') {
    const selected = profile.resumes.find((item) => profileItemId(item) === selectedResumeId);
    if (!selected || !getStructuredResume(selected)) {
      throw new Error('请先从匿名 Profile 选择一份已解析的简历。');
    }
    return;
  }
  if (resumeMode === 'pdf') {
    if (!selectedFile) throw new Error('请先选择一份 PDF 简历，或切换为粘贴文字。');
    return;
  }
  if (textInput.value.trim().length < 30) throw new Error('简历文字至少需要 30 个字，才能生成有效追问。');
}

async function parseResume() {
  if (resumeMode === 'saved') {
    const selected = profile.resumes.find((item) => profileItemId(item) === selectedResumeId);
    const parsed = getStructuredResume(selected);
    if (!parsed) throw new Error('已保存简历尚未解析完成，请重新上传。');
    return parsed;
  }
  let response;
  if (resumeMode === 'pdf') {
    const data = new FormData();
    data.append('client_id', getClientId());
    data.append('name', selectedFile.name.replace(/\.pdf$/i, '') || '面试简历');
    data.append('file', selectedFile, selectedFile.name);
    response = await apiFetch('/api/profile/resumes', {
      method: 'POST', body: data, timeout: 65_000,
    });
  } else {
    response = await apiFetch('/api/profile/resumes/text', {
      method: 'POST', timeout: 65_000, json: {
        client_id: getClientId(),
        name: `粘贴简历 ${formatDate(new Date())}`,
        text: textInput.value.trim(),
      },
    });
  }
  const stored = response?.resume || response;
  const id = profileItemId(stored);
  const parsed = getStructuredResume(stored);
  if (!id || !parsed) throw new Error('简历已上传，但解析结果不完整，请重试。');
  await loadProfile({ preferredResumeId: id });
  selectSavedResume(id);
  return parsed;
}

async function startInterview(event) {
  event.preventDefault();
  hideResumeAlert();
  hideSettingsAlert();
  let specialization;
  let durationMinutes;
  try {
    validateResume();
    specialization = getSpecialization();
    durationMinutes = getDurationMinutes();
  } catch (error) {
    if (/简历|PDF|文字/.test(error.message)) showResumeAlert(error.message);
    else showSettingsAlert(error.message);
    return;
  }

  const company = $('input[name="company"]:checked')?.value || 'bytedance';
  const stressLevel = getStressLevel();
  const stress = stressLevel > 0;
  const memory = memoryEnabled.checked;
  const languageMode = getLanguageMode();
  const interviewType = getInterviewType();
  const clientId = getClientId();

  try {
    await hardwareTest?.stop({ immediate: true, quiet: true });
    setButtonBusy(startButton, true, '正在读懂你的简历…');
    const resume = await parseResume();
    if (!resume || typeof resume !== 'object') throw new Error('简历解析结果为空，请换用文字版简历重试。');
    showResumeAlert('简历解析完成，正在组装专属面试剧本…', true);
    setButtonBusy(startButton, true, '正在准备面试官…');
    const session = await apiFetch('/api/interviews', {
      method: 'POST',
      timeout: profile.selected_project_id ? 110_000 : 65_000,
      json: {
        client_id: clientId,
        resume,
        company,
        role: 'backend',
        specialization,
        interview_type: interviewType,
        language_mode: languageMode,
        stress_level: stressLevel,
        stress,
        duration_minutes: durationMinutes,
        memory_enabled: memory,
        profile_project_id: profile.selected_project_id || null,
      },
    });
    const id = session?.id || session?.session_id;
    if (!id) throw new Error('服务端没有返回面试编号，请稍后重试。');
    const current = {
      ...session,
      id: String(id),
      client_id: clientId,
      company,
      role: 'backend',
      specialization,
      interview_type: interviewType,
      language_mode: languageMode,
      stress_level: stressLevel,
      stress,
      duration_minutes: durationMinutes,
      memory_enabled: memory,
      voice_mode: normalizeMode(session?.voice_mode || serverMode),
      created_at: session?.created_at || new Date().toISOString(),
    };
    setCurrentSession(current);
    saveSetup({
      company,
      specialization,
      interview_type: interviewType,
      language_mode: languageMode,
      stress_level: stressLevel,
      stress,
      duration_minutes: durationMinutes,
      memory_enabled: memory,
    });
    window.location.assign(`/interview?session=${encodeURIComponent(id)}`);
  } catch (error) {
    setButtonBusy(startButton, false);
    const message = error?.message || '创建面试失败，请稍后重试。';
    showResumeAlert(message);
    showToast(message, 'error', 5200);
    if (/扫描|文字层|提取不到|空白|image/i.test(message)) {
      setResumeMode('text');
      showResumeAlert('这份 PDF 可能是扫描件。请粘贴简历文字后继续。');
    }
  }
}

$$('[data-resume-tab]').forEach((button, index, tabs) => {
  button.addEventListener('click', () => setResumeMode(button.dataset.resumeTab, true));
  button.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const delta = event.key === 'ArrowRight' ? 1 : -1;
    const target = tabs[(index + delta + tabs.length) % tabs.length];
    setResumeMode(target.dataset.resumeTab, true);
  });
});

fileInput.addEventListener('change', () => setFile(fileInput.files?.[0] || null));
['dragenter', 'dragover'].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.add('is-dragging');
}));
['dragleave', 'drop'].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  dropZone.classList.remove('is-dragging');
}));
dropZone.addEventListener('drop', (event) => setFile(event.dataTransfer?.files?.[0] || null));
textInput.addEventListener('input', () => {
  $('#textCount').textContent = `${textInput.value.trim().length} 字`;
  hideResumeAlert();
});

$$('input[name="company"]').forEach((input) => input.addEventListener('change', () => {
  updateCompanySelection();
}));
$$('input[name="stress_level"]').forEach((input) => input.addEventListener('change', () => {
  stressTouched = true;
  updateCompanySelection({ applyDefault: false });
  hideSettingsAlert();
}));
$$('input[name="language_mode"]').forEach((input) => input.addEventListener('change', () => {
  setLanguageMode(getLanguageMode());
  hideSettingsAlert();
}));
$$('input[name="interview_type"]').forEach((input) => input.addEventListener('change', () => {
  setInterviewType(getInterviewType());
  hideSettingsAlert();
}));
specializationPreset.addEventListener('change', () => syncSpecializationControl({ focus: true }));
specializationCustom.addEventListener('input', hideSettingsAlert);
durationPreset.addEventListener('change', () => syncDurationControl({ focus: true }));
durationCustom.addEventListener('input', hideSettingsAlert);
memoryEnabled.addEventListener('change', () => {
  syncMemoryControl();
  if (memoryEnabled.checked) loadWeakness();
});
profileResumeFiles?.addEventListener('change', uploadProfileResumes);
profileProjectFiles?.addEventListener('change', uploadProfileProject);
profileGithubAdd?.addEventListener('click', addGithubProject);
profileGithubUrl?.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  addGithubProject();
});
$('#openProfileButton')?.addEventListener('click', () => {
  profilePanel.open = true;
  profilePanel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => profileResumeFiles?.focus(), 320);
});
window.addEventListener('hashchange', openProfileFromHash);
form.addEventListener('submit', startInterview);
window.addEventListener('pagehide', () => hardwareTest?.dispose());

restoreSetup();
setLanguageMode(getLanguageMode());
setInterviewType(getInterviewType());
syncMemoryControl();
syncSpecializationControl();
syncDurationControl();
updateCompanySelection({ applyDefault: !stressTouched });
selectedResumeId = readSelectedResumeId();
Promise.allSettled([loadConfig(), loadWeakness(), loadProfile()]);
openProfileFromHash();
