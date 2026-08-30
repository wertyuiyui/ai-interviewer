import {
  $, $$, apiFetch, getClientId, getSavedSetup, modeLabel,
  normalizeHistoryPayload, normalizeMode, saveSetup, setButtonBusy,
  setCurrentSession, showToast, firstValue, toArray,
} from './common.js';

const form = $('#setupForm');
const fileInput = $('#resumeFile');
const textInput = $('#resumeText');
const dropZone = $('#dropZone');
const startButton = $('#startButton');
const stressToggle = $('#stressToggle');
const resumeAlert = $('#resumeAlert');
const modePill = $('#modePill');

let resumeMode = 'pdf';
let selectedFile = null;
let stressTouched = false;
let serverMode = 'L3';

const stressDefaults = { bytedance: true, meituan: false, tencent: false };

function setResumeMode(nextMode, focus = false) {
  resumeMode = nextMode === 'text' ? 'text' : 'pdf';
  $$('[data-resume-tab]').forEach((button) => {
    const active = button.dataset.resumeTab === resumeMode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $('#pdfPanel').classList.toggle('is-hidden', resumeMode !== 'pdf');
  $('#textPanel').classList.toggle('is-hidden', resumeMode !== 'text');
  hideResumeAlert();
  if (focus) (resumeMode === 'pdf' ? fileInput : textInput).focus();
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
  if (applyDefault && !stressTouched) stressToggle.checked = stressDefaults[selected];
  $('#stressHint').textContent = stressToggle.checked
    ? '启用质疑、打断、沉默与连续施压'
    : selected === 'tencent' ? '保持循循善诱的温和追问' : '关闭施压手法，保留连续深挖';
}

function restoreSetup() {
  const saved = getSavedSetup();
  if (!saved || typeof saved !== 'object') return;
  const company = $$('input[name="company"]').find((input) => input.value === String(saved.company || ''));
  const duration = $(`input[name="duration"][value="${Number(saved.duration_minutes)}"]`);
  if (company) company.checked = true;
  if (duration) duration.checked = true;
  if (typeof saved.stress === 'boolean') {
    stressToggle.checked = saved.stress;
    stressTouched = true;
  }
  updateCompanySelection({ applyDefault: false });
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
  try {
    const clientId = getClientId();
    const history = await apiFetch(`/api/history?client_id=${encodeURIComponent(clientId)}`, { timeout: 8_000 });
    const topics = extractWeakTopics(history);
    if (!topics.length) return;
    $('#weaknessText').textContent = topics.join(' · ');
    $('#weaknessCard').classList.remove('is-hidden');
  } catch {
    // 历史提示不影响新建面试。
  }
}

function validateResume() {
  if (resumeMode === 'pdf') {
    if (!selectedFile) throw new Error('请先选择一份 PDF 简历，或切换为粘贴文字。');
    return;
  }
  if (textInput.value.trim().length < 30) throw new Error('简历文字至少需要 30 个字，才能生成有效追问。');
}

async function parseResume() {
  const data = new FormData();
  if (resumeMode === 'pdf') data.append('file', selectedFile, selectedFile.name);
  else data.append('text', textInput.value.trim());
  const parsed = await apiFetch('/api/resumes/parse', { method: 'POST', body: data, timeout: 65_000 });
  return parsed?.resume || parsed?.structured_resume || parsed?.data || parsed;
}

async function startInterview(event) {
  event.preventDefault();
  hideResumeAlert();
  try {
    validateResume();
  } catch (error) {
    showResumeAlert(error.message);
    return;
  }

  const company = $('input[name="company"]:checked')?.value || 'bytedance';
  const durationMinutes = Number($('input[name="duration"]:checked')?.value || 15);
  const stress = stressToggle.checked;
  const clientId = getClientId();

  try {
    setButtonBusy(startButton, true, '正在读懂你的简历…');
    const resume = await parseResume();
    if (!resume || typeof resume !== 'object') throw new Error('简历解析结果为空，请换用文字版简历重试。');
    showResumeAlert('简历解析完成，正在组装专属面试剧本…', true);
    setButtonBusy(startButton, true, '正在准备面试官…');
    const session = await apiFetch('/api/interviews', {
      method: 'POST',
      timeout: 65_000,
      json: {
        client_id: clientId,
        resume,
        company,
        role: 'backend',
        stress,
        duration_minutes: durationMinutes,
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
      stress,
      duration_minutes: durationMinutes,
      voice_mode: normalizeMode(session?.voice_mode || serverMode),
      created_at: session?.created_at || new Date().toISOString(),
    };
    setCurrentSession(current);
    saveSetup({ company, stress, duration_minutes: durationMinutes });
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
  stressTouched = false;
  updateCompanySelection();
}));
stressToggle.addEventListener('change', () => {
  stressTouched = true;
  updateCompanySelection({ applyDefault: false });
});
form.addEventListener('submit', startInterview);

restoreSetup();
updateCompanySelection({ applyDefault: !stressTouched });
Promise.allSettled([loadConfig(), loadWeakness()]);
