import { $, apiFetch, formatSeconds, setButtonBusy, showToast, toArray } from './common.js?v=20260830-profile-bank-v2';

const elements = {
  landing: $('#codingLanding'), workbench: $('#codingWorkbench'), review: $('#codingReview'),
  topic: $('#codingTopic'), difficulty: $('#codingDifficulty'), language: $('#codingLanguage'),
  bankStatus: $('#codingBankStatus'), questionList: $('#codingQuestionList'), back: $('#codingBack'),
  previousQuestion: $('#codingPreviousQuestion'), reviewPrevious: $('#codingReviewPrevious'),
  meta: $('#codingMeta'), title: $('#codingTitle'), recommended: $('#codingRecommended'), elapsed: $('#codingElapsed'),
  prompt: $('#codingPrompt'), signature: $('#codingSignature'), constraints: $('#codingConstraints'), examples: $('#codingExamples'),
  assumptions: $('#codingAssumptions'), approach: $('#codingApproach'), code: $('#codingCode'), complexity: $('#codingComplexity'),
  tests: $('#codingTests'), addTest: $('#codingAddTest'), previous: $('#codingPrevious'), next: $('#codingNext'),
  editorLanguage: $('#codingEditorLanguage'), reviewSummary: $('#codingReviewSummary'), overall: $('#codingOverallScore'),
  dimensions: $('#codingDimensions'), strengths: $('#codingStrengths'), improvements: $('#codingImprovements'),
  improvedSolution: $('#codingImprovedSolution'),
  retry: $('#codingRetry'), another: $('#codingAnother'),
};

const stages = ['clarify', 'approach', 'code', 'test'];
const stageLabels = { clarify: '澄清约束', approach: '方案设计', code: '编码实现', test: '主动自测' };
const languageLabels = { python: 'Python', java: 'Java', go: 'Go', javascript: 'JavaScript' };
const difficultyLabels = { easy: '基础', medium: '进阶', hard: '高难' };
let catalog = null;
let current = null;
let stageIndex = 0;
let startedAt = 0;
let timer = 0;
let activeLanguage = 'python';
let visibleQuestions = [];
let currentQuestionIndex = -1;

function setVisible(element, visible) { element?.classList.toggle('is-hidden', !visible); }
function draftKey() { return current ? `coding-draft:${current.id}:${activeLanguage}` : ''; }

function starterCode(question, language) {
  const signature = String(question?.signatures?.[language] || 'function solve(input)');
  if (language === 'python') return `${signature}\n    # TODO: explain key invariant while implementing\n    pass\n`;
  if (language === 'java') return `${signature} {\n    // TODO: explain key invariant while implementing\n}\n`;
  if (language === 'go') return `${signature} {\n    // TODO: explain key invariant while implementing\n}\n`;
  return `${signature} {\n  // TODO: explain key invariant while implementing\n}\n`;
}

function saveDraft() {
  if (!current) return;
  const payload = {
    assumptions: elements.assumptions.value, approach: elements.approach.value,
    code: elements.code.value, complexity: elements.complexity.value,
    tests: [...elements.tests.querySelectorAll('input')].map((input) => input.value),
  };
  localStorage.setItem(draftKey(), JSON.stringify(payload));
}

function loadDraft() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(draftKey()) || 'null'); } catch { draft = null; }
  elements.assumptions.value = String(draft?.assumptions || '');
  elements.approach.value = String(draft?.approach || '');
  elements.code.value = String(draft?.code || starterCode(current, elements.language.value));
  elements.complexity.value = String(draft?.complexity || '');
  renderTestInputs(toArray(draft?.tests).length ? draft.tests : ['', '']);
}

function renderTestInputs(values) {
  elements.tests.replaceChildren();
  toArray(values).slice(0, 12).forEach((value, index) => {
    const row = document.createElement('label'); row.className = 'coding-test-item';
    const number = document.createElement('span'); number.textContent = `CASE ${String(index + 1).padStart(2, '0')}`;
    const input = document.createElement('input'); input.maxLength = 1000; input.value = String(value || '');
    input.placeholder = '输入 → 预期输出；覆盖的边界/风险'; input.addEventListener('input', saveDraft);
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '删除';
    remove.addEventListener('click', () => { row.remove(); saveDraft(); });
    row.append(number, input, remove); elements.tests.append(row);
  });
}

function renderQuestions() {
  const topic = elements.topic.value;
  const difficulty = elements.difficulty.value;
  const questions = toArray(catalog?.questions).filter((item) => (
    (!topic || item.topic === topic) && (!difficulty || item.difficulty === difficulty)
  ));
  visibleQuestions = questions;
  elements.questionList.replaceChildren();
  questions.forEach((question) => {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'coding-question';
    const meta = document.createElement('div'); meta.className = 'coding-question-meta';
    meta.innerHTML = `<span>${question.topic}</span><span>${difficultyLabels[question.difficulty] || question.difficulty}</span>`;
    const title = document.createElement('h2'); title.textContent = question.title?.zh || question.id;
    const patterns = document.createElement('p'); patterns.textContent = toArray(question.patterns).join(' · ');
    const footer = document.createElement('footer'); footer.innerHTML = `<span>完整流程建议 ${question.recommended_minutes} 分钟</span><span>开始 →</span>`;
    button.append(meta, title, patterns, footer); button.addEventListener('click', () => openQuestion(question));
    elements.questionList.append(button);
  });
  if (!questions.length) elements.questionList.innerHTML = '<p>当前筛选没有题目，请调整条件。</p>';
}

function renderQuestionDetails() {
  const language = elements.language.value;
  elements.meta.textContent = `${current.topic} · ${difficultyLabels[current.difficulty] || current.difficulty}`;
  elements.title.textContent = current.title?.zh || current.id;
  elements.recommended.textContent = String(current.recommended_minutes || 30);
  elements.prompt.textContent = current.prompt?.zh || current.prompt?.en || '';
  elements.signature.textContent = current.signatures?.[language] || '';
  elements.editorLanguage.textContent = languageLabels[language] || language;
  elements.constraints.replaceChildren();
  toArray(current.constraints).forEach((value) => { const item = document.createElement('li'); item.textContent = value; elements.constraints.append(item); });
  elements.examples.replaceChildren();
  toArray(current.examples).forEach((example, index) => {
    const card = document.createElement('div'); card.className = 'coding-example';
    card.innerHTML = `<span>示例 ${index + 1} · 输入</span><code></code><span>输出</span><code></code>`;
    const codes = card.querySelectorAll('code'); codes[0].textContent = example.input; codes[1].textContent = example.output;
    elements.examples.append(card);
  });
}

function openQuestion(question, questionIndex = visibleQuestions.findIndex((item) => item.id === question.id)) {
  current = question; activeLanguage = elements.language.value; stageIndex = 0; startedAt = Date.now(); clearInterval(timer);
  currentQuestionIndex = questionIndex;
  timer = setInterval(() => { elements.elapsed.textContent = formatSeconds((Date.now() - startedAt) / 1000); }, 500);
  renderQuestionDetails(); loadDraft(); renderStage();
  elements.previousQuestion.disabled = currentQuestionIndex <= 0;
  elements.reviewPrevious.disabled = currentQuestionIndex <= 0;
  setVisible(elements.landing, false); setVisible(elements.review, false); setVisible(elements.workbench, true);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderStage() {
  const active = stages[stageIndex];
  document.querySelectorAll('[data-stage]').forEach((button) => button.classList.toggle('is-active', button.dataset.stage === active));
  document.querySelectorAll('[data-panel]').forEach((panel) => setVisible(panel, panel.dataset.panel === active));
  elements.previous.disabled = stageIndex === 0;
  $('.button-label', elements.next).textContent = stageIndex === stages.length - 1 ? '提交四维复盘' : `进入${stageLabels[stages[stageIndex + 1]]}`;
}

function collectTests() { return [...elements.tests.querySelectorAll('input')].map((input) => input.value.trim()).filter(Boolean); }
function validateSubmission() {
  const required = [
    [elements.assumptions.value.trim().length >= 2, '先写下至少一项澄清问题。'],
    [elements.approach.value.trim().length >= 10, '先说明方案和关键不变量。'],
    [elements.code.value.trim().length >= 10, '请完成代码或完整伪代码。'],
    [elements.complexity.value.trim().length >= 3, '请说明时间与空间复杂度。'],
    [collectTests().length > 0, '至少写一个主动自测用例。'],
  ];
  const missing = required.find(([valid]) => !valid); if (missing) { showToast(missing[1], 'error'); return false; }
  return true;
}

async function submitReview() {
  if (!validateSubmission()) return;
  saveDraft(); setButtonBusy(elements.next, true, '正在四维复盘…');
  try {
    const response = await apiFetch('/api/coding/review', { method: 'POST', timeout: 65_000, json: {
      challenge_id: current.id, language: elements.language.value,
      assumptions: elements.assumptions.value, approach: elements.approach.value,
      code: elements.code.value, complexity: elements.complexity.value, test_cases: collectTests(),
    } });
    renderReview(response.assessment || {}); clearInterval(timer);
    setVisible(elements.workbench, false); setVisible(elements.review, true); window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) { showToast(error?.message || '暂时无法生成代码复盘。', 'error', 5200); }
  finally { setButtonBusy(elements.next, false); }
}

function fillList(element, values, fallback) {
  element.replaceChildren(); (toArray(values).length ? values : [fallback]).forEach((value) => { const item = document.createElement('li'); item.textContent = value; element.append(item); });
}

function renderReview(assessment) {
  elements.overall.textContent = Number.isFinite(Number(assessment.overall_score)) ? Number(assessment.overall_score).toFixed(1) : '—';
  elements.reviewSummary.textContent = assessment.summary || '复盘完成。'; elements.dimensions.replaceChildren();
  const dimensions = [['communication', '沟通与澄清'], ['problem_solving', '解题与取舍'], ['technical_competency', '技术实现'], ['testing', '主动测试']];
  dimensions.forEach(([key, label]) => { const value = assessment[key] || {}; const card = document.createElement('article'); card.className = 'coding-dimension'; card.innerHTML = `<span>${label}</span><strong>${Number(value.score || 0).toFixed(1)}</strong><p></p>`; $('p', card).textContent = value.feedback || '暂无反馈'; elements.dimensions.append(card); });
  fillList(elements.strengths, assessment.strengths, '本次暂无明确加分点。'); fillList(elements.improvements, assessment.improvements, assessment.next_drill || '按反馈再练一次。');
  elements.improvedSolution.textContent = assessment.improved_solution || '暂无代码或伪代码改写示范。';
}

function openPreviousQuestion() {
  if (!current || currentQuestionIndex <= 0) return;
  saveDraft();
  openQuestion(visibleQuestions[currentQuestionIndex - 1], currentQuestionIndex - 1);
}

async function requestHint(stage, button) {
  try { setButtonBusy(button, true, '读取中…'); const response = await apiFetch('/api/coding/hint', { method: 'POST', timeout: 15_000, json: { challenge_id: current.id, stage } }); showToast(response.hint || '先用最小示例推演。', 'info', 7000); }
  catch (error) { showToast(error?.message || '暂时无法获取提示。', 'error'); } finally { setButtonBusy(button, false); }
}

async function initialize() {
  try {
    catalog = await apiFetch('/api/coding/catalog', { timeout: 15_000 });
    toArray(catalog.topics).forEach((topic) => { const option = document.createElement('option'); option.value = topic; option.textContent = topic; elements.topic.append(option); });
    elements.bankStatus.textContent = `${Number(catalog.question_count) || 0} 道公开策展真题 · 静态复盘`;
    renderQuestions();
  } catch (error) { elements.bankStatus.textContent = '题库读取失败'; showToast(error?.message || '暂时无法读取手撕代码题库。', 'error'); }
}

elements.topic.addEventListener('change', renderQuestions); elements.difficulty.addEventListener('change', renderQuestions);
elements.language.addEventListener('change', () => {
  if (!current) return;
  saveDraft();
  activeLanguage = elements.language.value;
  renderQuestionDetails();
  loadDraft();
});
elements.back.addEventListener('click', () => { saveDraft(); clearInterval(timer); current = null; setVisible(elements.workbench, false); setVisible(elements.landing, true); });
elements.previousQuestion.addEventListener('click', openPreviousQuestion);
elements.reviewPrevious.addEventListener('click', openPreviousQuestion);
document.querySelectorAll('[data-stage]').forEach((button) => button.addEventListener('click', () => { stageIndex = stages.indexOf(button.dataset.stage); renderStage(); }));
document.querySelectorAll('[data-hint]').forEach((button) => button.addEventListener('click', () => requestHint(button.dataset.hint, button)));
elements.previous.addEventListener('click', () => { stageIndex = Math.max(0, stageIndex - 1); renderStage(); });
elements.next.addEventListener('click', () => { if (stageIndex < stages.length - 1) { stageIndex += 1; renderStage(); } else submitReview(); });
elements.addTest.addEventListener('click', () => { const values = [...elements.tests.querySelectorAll('input')].map((input) => input.value); if (values.length < 12) renderTestInputs([...values, '']); });
[elements.assumptions, elements.approach, elements.code, elements.complexity].forEach((input) => input.addEventListener('input', saveDraft));
elements.retry.addEventListener('click', () => { stageIndex = 0; renderStage(); setVisible(elements.review, false); setVisible(elements.workbench, true); startedAt = Date.now(); timer = setInterval(() => { elements.elapsed.textContent = formatSeconds((Date.now() - startedAt) / 1000); }, 500); });
elements.another.addEventListener('click', () => { current = null; setVisible(elements.review, false); setVisible(elements.landing, true); window.scrollTo({ top: 0, behavior: 'smooth' }); });
window.addEventListener('pagehide', () => { saveDraft(); clearInterval(timer); });

initialize();
