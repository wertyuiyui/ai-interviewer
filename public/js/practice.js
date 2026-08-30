import {
  $, apiFetch, companyLabel, formatSeconds, getClientId, normalizeMode, setButtonBusy, showToast, toArray,
} from './common.js?v=20260830-profile-bank-v2';
import { AudioSession } from './audio-session.js?v=20260830-mic-release';

const query = new URLSearchParams(location.search);
const reviewInterviewId = String(query.get('review') || query.get('interview') || '').trim();
const reviewOrdinal = Number(query.get('ordinal'));

const elements = {
  setup: $('#practiceSetup'),
  form: $('#practiceForm'),
  formTitle: $('#practiceFormTitle'),
  bankBadge: $('#practiceBankBadge'),
  reviewNotice: $('#reviewNotice'),
  interviewType: $('#practiceInterviewType'),
  answerModeChoice: $('#practiceAnswerMode'),
  company: $('#practiceCompany'),
  topic: $('#practiceTopic'),
  difficulty: $('#practiceDifficulty'),
  count: $('#practiceCount'),
  formNote: $('#practiceFormNote'),
  voiceProof: $('#practiceVoiceProof'),
  start: $('#practiceStart'),
  session: $('#practiceSession'),
  progress: $('#practiceProgress'),
  progressTrack: $('.practice-progress-track'),
  progressFill: $('#practiceProgressFill'),
  sessionLabel: $('#practiceSessionLabel'),
  elapsed: $('#practiceElapsed'),
  previousQuestion: $('#practicePreviousQuestion'),
  returnCurrent: $('#practiceReturnCurrent'),
  exit: $('#practiceExit'),
  category: $('#questionCategory'),
  questionDifficulty: $('#questionDifficulty'),
  recommended: $('#questionRecommended'),
  origin: $('#questionOrigin'),
  source: $('#questionSource'),
  question: $('#questionText'),
  previousAttempt: $('#previousAttempt'),
  previousDeductions: $('#previousDeductions'),
  answerCard: $('#practiceAnswerCard'),
  answerStatus: $('#answerStatus'),
  voiceMode: $('#voiceModeButton'),
  textMode: $('#textModeButton'),
  recorder: $('#practiceRecorder'),
  record: $('#recordButton'),
  recordLevel: $('#recordLevel'),
  recordHint: $('#recordHint'),
  answer: $('#practiceAnswer'),
  hint: $('#practiceHint'),
  hintBox: $('#practiceHintBox'),
  hintText: $('#practiceHintText'),
  skip: $('#practiceSkip'),
  submit: $('#practiceSubmit'),
  feedback: $('#practiceFeedback'),
  feedbackTitle: $('#feedbackTitle'),
  score: $('#practiceScore'),
  strengths: $('#practiceStrengths'),
  deductions: $('#practiceDeductions'),
  keyPoints: $('#practiceKeyPoints'),
  betterAnswer: $('#practiceBetterAnswer'),
  reattempt: $('#reattemptButton'),
  next: $('#nextQuestionButton'),
  complete: $('#practiceComplete'),
  completeSummary: $('#practiceCompleteSummary'),
  again: $('#practiceAgain'),
  mistakeCount: $('#mistakeBookCount'),
  mistakeList: $('#mistakeBookList'),
};

const difficultyLabels = { easy: '基础', medium: '进阶', hard: '高难', discussion: '讨论' };
let practiceSession = null;
let currentQuestion = null;
let pendingNextQuestion = null;
let answerMode = 'voice';
let reattempting = false;
let sessionStartedAt = 0;
let answerStartedAt = 0;
let elapsedTimer = 0;
let audio = null;
let socket = null;
let socketReady = false;
let recording = false;
let sealingRecording = false;
let stopResolver = null;
let stopTimer = 0;
let providerTranscript = '';
let transcriptManuallyEdited = false;
let attempts = [];
let voiceTranscriptionAvailable = true;
let questionHistory = [];
let historyIndex = -1;
let liveHistoryIndex = -1;
let browsingHistory = false;

function selectedValue(name, fallback) {
  return $(`input[name="${name}"]:checked`)?.value || fallback;
}

function createAudioSession() {
  return new AudioSession({
    onAudioFrame(buffer) {
      if (!recording || !socketReady || socket?.readyState !== WebSocket.OPEN) return;
      socket.send(buffer);
    },
    onInputLevel(value) {
      const raw = Math.max(0, Number(value) || 0);
      const percent = Math.min(100, Math.round(Math.sqrt(raw) * 500));
      $('i', elements.recordLevel).style.transform = `scaleX(${percent / 100})`;
      elements.recordLevel.setAttribute('aria-valuenow', String(percent));
    },
    onCaptureState(event) {
      if (['microphone-error', 'microphone-ended'].includes(event?.type)) {
        showToast('麦克风采集已停止，可以切换到文字作答。', 'error');
        finishRecordingState();
      }
    },
  });
}

function setVisible(element, visible) {
  element?.classList.toggle('is-hidden', !visible);
}

function resetList(element, values, fallback) {
  element.replaceChildren();
  const items = toArray(values).map((value) => String(value || '').trim()).filter(Boolean);
  (items.length ? items : [fallback]).forEach((value) => {
    const item = document.createElement('li');
    item.textContent = value;
    element.append(item);
  });
}

function renderSessionElapsed() {
  if (!sessionStartedAt) return;
  elements.elapsed.textContent = formatSeconds((Date.now() - sessionStartedAt) / 1000);
}

function setAnswerMode(nextMode, { focus = true, quiet = false } = {}) {
  const requested = nextMode === 'text' ? 'text' : 'voice';
  const next = requested === 'voice' && !voiceTranscriptionAvailable ? 'text' : requested;
  if (requested === 'voice' && next === 'text' && !quiet) {
    showToast('当前服务模式不提供实时转写，已切换为文字作答。', 'info');
  }
  if ((recording || sealingRecording) && next !== answerMode) {
    showToast('请先停止当前语音回答，再切换作答方式。', 'info');
    return;
  }
  answerMode = next;
  elements.voiceMode.classList.toggle('is-active', next === 'voice');
  elements.textMode.classList.toggle('is-active', next === 'text');
  setVisible(elements.recorder, next === 'voice');
  elements.answer.placeholder = next === 'voice'
    ? '点击开始语音回答，实时转写会出现在这里，也可以手动修正…'
    : '在这里输入本题回答…';
  elements.answerStatus.textContent = next === 'voice' ? '准备好后开始录音' : '开始输入后计时';
  if (focus && next === 'text') elements.answer.focus();
}

function configureVoiceAvailability(available, { notify = false } = {}) {
  voiceTranscriptionAvailable = Boolean(available);
  const setupVoice = $('input[name="answer_mode"][value="voice"]');
  const setupText = $('input[name="answer_mode"][value="text"]');
  if (setupVoice) setupVoice.disabled = !voiceTranscriptionAvailable;
  elements.voiceMode.disabled = !voiceTranscriptionAvailable;
  if (!voiceTranscriptionAvailable) {
    if (setupText) setupText.checked = true;
    if (elements.formNote) elements.formNote.textContent = '当前为纯文字模式：不请求麦克风，不会显示实时转写状态。';
    if (elements.voiceProof) elements.voiceProof.textContent = '当前部署仅提供文字作答';
    setAnswerMode('text', { focus: false, quiet: true });
    if (notify) showToast('实时转写不可用，已切换为文字作答。', 'info', 5200);
  } else if (elements.formNote) {
    elements.formNote.textContent = '每道题都可用语音或文字作答，语音转写可在提交前修正。';
    if (elements.voiceProof) elements.voiceProof.textContent = '实时转写后仍可手动修正';
  }
}

function syncPracticeFilters() {
  if (reviewInterviewId) return;
  const interviewType = selectedValue('practice_interview_type', 'technical');
  const behavioralTopic = elements.topic.value === 'English behavioral';
  if (interviewType === 'hr') {
    elements.topic.value = '';
    elements.difficulty.value = '';
    elements.topic.disabled = true;
    elements.difficulty.disabled = true;
    return;
  }
  elements.topic.disabled = false;
  elements.difficulty.disabled = false;
  if (behavioralTopic) elements.topic.value = '';
}

function currentQuestionNumber() {
  const answered = Number(practiceSession?.answered_questions) || attempts.filter((item) => !item.reattempt).length;
  const skipped = Number(practiceSession?.skipped_questions) || 0;
  const position = answered + skipped + 1;
  if (practiceSession?.infinite) return position;
  return Math.min((Number(practiceSession?.total_questions) || 1), position);
}

function safeSourceUrl(value) {
  const candidate = String(value || '').trim();
  if (!candidate) return '';
  try {
    const url = new URL(candidate, location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

function renderHistoryControls() {
  elements.previousQuestion.disabled = historyIndex <= 0;
  setVisible(elements.returnCurrent, browsingHistory);
}

function currentHistoryEntry() {
  return questionHistory[historyIndex] || null;
}

function renderQuestion(question, { track = true } = {}) {
  if (!question) {
    completePractice();
    return;
  }
  if (track) {
    questionHistory.push({ question, draft: '', answer: '', assessment: null, skipped: false });
    const index = questionHistory.length - 1;
    historyIndex = index;
    liveHistoryIndex = Math.max(liveHistoryIndex, index);
    browsingHistory = false;
  }
  currentQuestion = question;
  if (track) pendingNextQuestion = null;
  reattempting = false;
  providerTranscript = '';
  transcriptManuallyEdited = false;
  answerStartedAt = 0;
  elements.answer.value = '';
  elements.answer.readOnly = false;
  elements.voiceMode.disabled = !voiceTranscriptionAvailable;
  elements.textMode.disabled = false;
  elements.question.textContent = String(question.question || question.prompt || '题目暂时不可用');
  elements.category.textContent = String(question.category || question.topic || '综合');
  elements.questionDifficulty.textContent = difficultyLabels[question.difficulty] || String(question.difficulty || '进阶');
  elements.recommended.textContent = `建议 ${Math.max(15, Number(question.recommended_answer_seconds) || 60)} 秒`;
  const rawOrigin = String(question.origin || question.source_type || '').toLowerCase();
  const origin = rawOrigin.includes('ai') ? 'ai' : rawOrigin.includes('review') ? 'review' : rawOrigin.includes('real') ? 'real' : rawOrigin;
  const badge = String(question.badge || ({ real: '真题', ai: 'AI出题', review: '错题重答' })[origin] || '').replace(/[【】]/g, '').trim();
  elements.origin.textContent = badge ? `【${badge}】` : '';
  elements.origin.classList.toggle('is-ai', origin === 'ai');
  elements.origin.classList.toggle('is-review', origin === 'review');
  setVisible(elements.origin, Boolean(badge));
  const source = String(question.source || question.source_label || '').trim();
  const sourceUrl = safeSourceUrl(question.source_url);
  elements.source.replaceChildren();
  if (source) {
    elements.source.append(document.createTextNode(`题目来源：${source}${question.from_mistake_book ? ' · 错题本优先' : ''}`));
    if (sourceUrl) {
      const link = document.createElement('a');
      link.href = sourceUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '查看公开来源 ↗';
      elements.source.append(' · ', link);
    }
  }
  setVisible(elements.source, Boolean(source));
  const total = Math.max(1, Number(practiceSession?.total_questions) || 1);
  const position = currentQuestionNumber();
  elements.progress.textContent = practiceSession?.infinite ? `第 ${position} 题 · 无限模式` : `第 ${position} / ${total} 题`;
  elements.progressTrack.classList.toggle('is-unlimited', Boolean(practiceSession?.infinite));
  elements.progressFill.style.width = practiceSession?.infinite ? '35%' : `${Math.min(100, position / total * 100)}%`;
  elements.previousDeductions.textContent = toArray(question.previous_deductions).filter(Boolean).join('；');
  setVisible(elements.previousAttempt, Boolean(question.previous_score !== undefined || elements.previousDeductions.textContent));
  setVisible(elements.answerCard, true);
  setVisible(elements.feedback, false);
  setVisible(elements.hintBox, false);
  elements.hint.disabled = false;
  elements.skip.disabled = false;
  elements.submit.disabled = false;
  elements.record.disabled = false;
  renderHistoryControls();
  setAnswerMode(answerMode, { focus: false });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function saveHistoryDraft() {
  const entry = currentHistoryEntry();
  if (entry && !entry.assessment && !entry.skipped) entry.draft = elements.answer.value;
}

function renderHistoryEntry(index) {
  const entry = questionHistory[index];
  if (!entry) return;
  saveHistoryDraft();
  historyIndex = index;
  browsingHistory = index !== liveHistoryIndex;
  renderQuestion(entry.question, { track: false });
  browsingHistory = index !== liveHistoryIndex;
  renderHistoryControls();
  elements.progress.textContent = `第 ${index + 1} 题 · ${browsingHistory ? '历史回看' : '当前题'}`;
  elements.answer.value = entry.answer || entry.draft || '';
  if (entry.assessment) {
    renderAssessment(entry.assessment);
    setVisible(elements.answerCard, false);
    setVisible(elements.feedback, true);
    elements.reattempt.disabled = browsingHistory;
    $('.button-label', elements.next).textContent = browsingHistory ? '回到当前题' : (pendingNextQuestion ? '下一题' : '查看本组结果');
  } else if (browsingHistory) {
    elements.answer.readOnly = true;
    elements.voiceMode.disabled = true;
    elements.textMode.disabled = true;
    setVisible(elements.recorder, false);
    elements.hint.disabled = true;
    elements.skip.disabled = true;
    elements.submit.disabled = true;
    elements.answerStatus.textContent = entry.skipped ? '这道题已跳过，仅供回看' : '历史题目仅供回看';
  }
}

function previousQuestion() {
  if (historyIndex <= 0) return;
  if (recording || sealingRecording) {
    showToast('请先停止当前语音回答，再回看上一题。', 'info');
    return;
  }
  renderHistoryEntry(historyIndex - 1);
}

function returnToCurrentQuestion() {
  if (liveHistoryIndex < 0) return;
  renderHistoryEntry(liveHistoryIndex);
}

function closeSocket() {
  clearTimeout(stopTimer);
  stopTimer = 0;
  socketReady = false;
  const active = socket;
  socket = null;
  if (active && active.readyState < WebSocket.CLOSING) active.close(1000, 'practice capture complete');
}

function finishRecordingState() {
  recording = false;
  sealingRecording = false;
  audio?.disableMicrophone();
  closeSocket();
  elements.record.disabled = false;
  elements.record.classList.remove('is-recording');
  $('span', elements.record).textContent = '重新录音';
  elements.answerStatus.textContent = providerTranscript ? '转写完成，可手动修正后提交' : '录音已停止，请检查回答文字';
  elements.recordHint.textContent = '麦克风已释放；提交前可以修正转写';
  $('i', elements.recordLevel).style.transform = 'scaleX(0)';
  elements.recordLevel.setAttribute('aria-valuenow', '0');
  if (stopResolver) {
    stopResolver();
    stopResolver = null;
  }
}

function stopRecording() {
  if (!recording && !sealingRecording) return Promise.resolve();
  if (sealingRecording) return new Promise((resolve) => {
    const previous = stopResolver;
    stopResolver = () => { previous?.(); resolve(); };
  });
  recording = false;
  sealingRecording = true;
  elements.record.disabled = true;
  $('span', elements.record).textContent = '正在整理转写…';
  elements.answerStatus.textContent = '正在封口并整理完整转写';
  audio?.disableMicrophone();
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'practice.stop' }));
  } else {
    finishRecordingState();
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    stopResolver = resolve;
    stopTimer = setTimeout(finishRecordingState, 7000);
  });
}

function handlePracticeEvent(event) {
  const type = String(event?.type || '');
  if (type === 'practice.ready') {
    if (event.transcription_available === false) {
      finishRecordingState();
      configureVoiceAvailability(false, { notify: true });
      return;
    }
    socketReady = true;
    recording = true;
    sealingRecording = false;
    if (!answerStartedAt) answerStartedAt = Date.now();
    elements.record.disabled = false;
    elements.record.classList.add('is-recording');
    $('span', elements.record).textContent = '停止并生成转写';
    elements.answerStatus.textContent = '正在实时转写';
    elements.recordHint.textContent = '说完后点击停止；本题时间照常记录';
    return;
  }
  if (type === 'practice.speech.started') {
    elements.answerStatus.textContent = '检测到语音，正在转写';
    return;
  }
  if (type === 'practice.speech.ended') {
    elements.answerStatus.textContent = '正在整理这一段语音';
    return;
  }
  if (['practice.transcript.partial', 'practice.transcript.done'].includes(type)) {
    const text = String(event.text || event.transcript || '').trim();
    if (text) {
      providerTranscript = text;
      if (!transcriptManuallyEdited) elements.answer.value = text;
    }
    if (type.endsWith('.done')) elements.answerStatus.textContent = '已收到完整转写';
    return;
  }
  if (type === 'practice.stopped') {
    finishRecordingState();
    return;
  }
  if (type === 'practice.error') {
    showToast(event.message || '实时转写暂时不可用，请改用文字作答。', 'error', 5200);
    finishRecordingState();
    configureVoiceAvailability(false);
  }
}

async function startRecording() {
  if (!voiceTranscriptionAvailable) {
    setAnswerMode('text');
    return;
  }
  if (recording) {
    await stopRecording();
    return;
  }
  if (sealingRecording || !practiceSession?.id || !currentQuestion) return;
  elements.record.disabled = true;
  $('span', elements.record).textContent = '正在启用麦克风…';
  transcriptManuallyEdited = false;
  providerTranscript = '';
  answerStartedAt = 0;
  elements.answer.value = '';
  try {
    if (!audio) audio = createAudioSession();
    let result = null;
    if (!audio.context) result = await audio.initialize({ capture: true });
    else {
      await audio.enableMicrophone('', { force: true });
      result = { microphone: true };
    }
    if (!result?.microphone) throw result?.error || new Error('没有取得麦克风权限。');
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${location.host}/ws/practice/sessions/${encodeURIComponent(practiceSession.id)}`);
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('open', () => {
      socket?.send(JSON.stringify({ type: 'client.ready', client_id: getClientId() }));
    });
    socket.addEventListener('message', ({ data }) => {
      if (typeof data !== 'string') return;
      try { handlePracticeEvent(JSON.parse(data)); } catch { /* ignore malformed provider event */ }
    });
    socket.addEventListener('close', () => {
      if (recording || sealingRecording) finishRecordingState();
    });
    socket.addEventListener('error', () => {
      showToast('无法连接实时转写，请检查网络或切换文字作答。', 'error');
      finishRecordingState();
      configureVoiceAvailability(false);
    });
  } catch (error) {
    audio?.disableMicrophone();
    elements.record.disabled = false;
    $('span', elements.record).textContent = '开始语音回答';
    showToast(error?.message || '无法启用麦克风。', 'error', 5200);
  }
}

function renderAssessment(assessment = {}) {
  const score = Number(assessment.score);
  const scored = assessment.scorable !== false && assessment.status !== 'unscored' && Number.isFinite(score);
  elements.score.textContent = scored ? score.toFixed(1) : '—';
  elements.score.parentElement.classList.toggle('is-unscored', !scored);
  elements.feedbackTitle.textContent = scored ? '评分完成' : '证据不足，本题未评分';
  const strengths = toArray(assessment.strengths).map((item) => String(item || '').trim()).filter(Boolean);
  const misplaced = strengths.filter((item) => /^(未完成|未提及|未回答|未说明|未覆盖|缺少)/.test(item));
  const validStrengths = strengths.filter((item) => !misplaced.includes(item));
  const deductions = [...toArray(assessment.deductions), ...misplaced];
  resetList(elements.strengths, validStrengths, scored ? '本题暂无明确加分点。' : '有效回答不足，暂不判断。');
  resetList(elements.deductions, deductions, scored ? '本题没有返回具体扣分点。' : '请先完成一段可评估的回答。');
  resetList(elements.keyPoints, assessment.key_points, '回答时先给结论，再说明依据、边界和验证方法。');
  elements.betterAnswer.textContent = String(assessment.better_answer || '暂无改写示范。');
}

async function submitAnswer() {
  if (!currentQuestion || !practiceSession?.id || elements.submit.disabled) return;
  if (recording || sealingRecording) await stopRecording();
  const answer = elements.answer.value.trim();
  if (!answer) {
    showToast('请先完成本题回答。', 'error');
    if (answerMode === 'text') elements.answer.focus();
    return;
  }
  const duration = answerStartedAt ? Math.max(0, (Date.now() - answerStartedAt) / 1000) : null;
  try {
    setButtonBusy(elements.submit, true, '正在评分…');
    const response = await apiFetch(`/api/practice/sessions/${encodeURIComponent(practiceSession.id)}/answers`, {
      method: 'POST',
      timeout: 65_000,
      json: {
        client_id: getClientId(),
        question_id: currentQuestion.id,
        answer,
        input_mode: answerMode,
        answer_duration_seconds: duration,
        reattempt: reattempting,
      },
    });
    attempts.push({ ...response, reattempt: reattempting });
    const historyEntry = currentHistoryEntry();
    if (historyEntry) {
      historyEntry.answer = answer;
      historyEntry.draft = answer;
      historyEntry.assessment = response.assessment || {};
    }
    pendingNextQuestion = response.next_question || null;
    practiceSession.answered_questions = reattempting
      ? Number(practiceSession.answered_questions) || 0
      : (Number(practiceSession.answered_questions) || 0) + 1;
    renderAssessment(response.assessment || {});
    setVisible(elements.answerCard, false);
    setVisible(elements.feedback, true);
    $('.button-label', elements.next).textContent = response.done ? '查看本组结果' : '下一题';
    elements.reattempt.disabled = false;
    elements.next.disabled = false;
    reattempting = false;
    elements.feedback.scrollIntoView({ behavior: 'smooth', block: 'start' });
    void loadMistakes({ quiet: true });
  } catch (error) {
    showToast(error?.message || '本题评分失败，请稍后重试。', 'error', 5200);
  } finally {
    setButtonBusy(elements.submit, false);
  }
}

async function skipQuestion() {
  if (!currentQuestion || !practiceSession?.id || elements.skip.disabled) return;
  if (recording || sealingRecording) await stopRecording();
  try {
    setButtonBusy(elements.skip, true, '跳过中…');
    const response = await apiFetch(`/api/practice/sessions/${encodeURIComponent(practiceSession.id)}/skip`, {
      method: 'POST', timeout: 20_000,
      json: { client_id: getClientId(), question_id: currentQuestion.id },
    });
    practiceSession.skipped_questions = Number(response.skipped_questions)
      || (Number(practiceSession.skipped_questions) || 0) + 1;
    const historyEntry = currentHistoryEntry();
    if (historyEntry) historyEntry.skipped = true;
    if (response.done || !response.next_question) completePractice();
    else renderQuestion(response.next_question);
  } catch (error) {
    showToast(error?.message || '暂时无法跳过这道题。', 'error');
  } finally {
    setButtonBusy(elements.skip, false);
  }
}

async function requestHint() {
  if (!currentQuestion || !practiceSession?.id || elements.hint.disabled) return;
  elements.hint.disabled = true;
  try {
    const response = await apiFetch(`/api/practice/sessions/${encodeURIComponent(practiceSession.id)}/hint`, {
      method: 'POST', timeout: 30_000,
      json: { client_id: getClientId(), question_id: currentQuestion.id },
    });
    elements.hintText.textContent = String(response.hint || '先给出结论，再按原理、场景和边界组织回答。');
    setVisible(elements.hintBox, true);
    elements.hint.textContent = '已使用提示';
  } catch (error) {
    elements.hint.disabled = false;
    showToast(error?.message || '暂时无法获取提示。', 'error');
  }
}

function reattemptQuestion() {
  reattempting = true;
  providerTranscript = '';
  transcriptManuallyEdited = false;
  answerStartedAt = 0;
  elements.answer.value = '';
  setVisible(elements.feedback, false);
  setVisible(elements.answerCard, true);
  setVisible(elements.hintBox, false);
  elements.hint.disabled = false;
  elements.hint.textContent = '给我一个提示';
  elements.answerStatus.textContent = '同一道题重新计时并单独评分';
  elements.answerCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function completePractice() {
  stopRecording();
  clearInterval(elapsedTimer);
  const scored = attempts
    .map((item) => Number(item?.assessment?.score))
    .filter((score) => Number.isFinite(score));
  const average = scored.length ? scored.reduce((sum, score) => sum + score, 0) / scored.length : null;
  const skipped = Number(practiceSession?.skipped_questions) || 0;
  const prefix = practiceSession?.infinite ? '无限练习已手动结束。' : '';
  const skippedText = skipped ? `，跳过 ${skipped} 题` : '';
  const summary = average === null
    ? `${prefix}本组完成 ${attempts.length} 次作答${skippedText}；可评分证据不足，没有生成虚构分数。`
    : `${prefix}本组完成 ${attempts.length} 次作答${skippedText}，${scored.length} 次有效评分，平均 ${average.toFixed(1)} 分。`;
  elements.completeSummary.textContent = summary;
  setVisible(elements.setup, false);
  setVisible(elements.session, false);
  setVisible(elements.complete, true);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function finishPractice() {
  if (practiceSession?.status === 'completed') {
    completePractice();
    return;
  }
  if (!practiceSession?.id || elements.exit.disabled) {
    completePractice();
    return;
  }
  if (recording || sealingRecording) await stopRecording();
  try {
    setButtonBusy(elements.exit, true, '结束中…');
    const response = await apiFetch(`/api/practice/sessions/${encodeURIComponent(practiceSession.id)}/finish`, {
      method: 'POST', timeout: 20_000, json: { client_id: getClientId() },
    });
    practiceSession = { ...practiceSession, ...response };
    completePractice();
  } catch (error) {
    showToast(error?.message || '暂时无法结束本组，请稍后重试。', 'error');
  } finally {
    setButtonBusy(elements.exit, false);
  }
}

function nextQuestion() {
  if (browsingHistory) {
    returnToCurrentQuestion();
    return;
  }
  if (!pendingNextQuestion) {
    completePractice();
    return;
  }
  renderQuestion(pendingNextQuestion);
}

async function createPracticeSession(event = null) {
  event?.preventDefault();
  if (elements.start.disabled) return;
  const reviewMode = Boolean(reviewInterviewId);
  const languageMode = selectedValue('practice_language', 'bilingual');
  const interviewType = selectedValue('practice_interview_type', 'technical');
  const infinite = elements.count.value === 'unlimited';
  const payload = reviewMode
    ? {
      client_id: getClientId(), mode: 'review', source_interview_id: reviewInterviewId,
      review_score_lte: 6, language_mode: languageMode, interview_type: interviewType,
      ...(Number.isInteger(reviewOrdinal) && reviewOrdinal > 0 ? { review_ordinals: [reviewOrdinal] } : {}),
    }
    : {
      client_id: getClientId(), mode: 'quick', company: elements.company.value,
      topic: elements.topic.value || null,
      difficulty: elements.difficulty.value || null,
      language_mode: languageMode, interview_type: interviewType,
      count: infinite ? null : Number(elements.count.value) || 5, infinite,
    };
  try {
    setButtonBusy(elements.start, true, reviewMode ? '正在读取错题…' : '正在选题…');
    const response = await apiFetch('/api/practice/sessions', {
      method: 'POST', timeout: 35_000, json: payload,
    });
    if (!response?.id || !response?.current_question) throw new Error('服务端没有返回可练习的题目。');
    practiceSession = response;
    attempts = toArray(response.attempts);
    questionHistory = [];
    historyIndex = -1;
    liveHistoryIndex = -1;
    browsingHistory = false;
    sessionStartedAt = Date.now();
    clearInterval(elapsedTimer);
    elapsedTimer = setInterval(renderSessionElapsed, 500);
    elements.sessionLabel.textContent = reviewMode
      ? '面后错题重答'
      : `${companyLabel(response.company || payload.company)} · ${response.infinite ? '无限刷题' : '快速刷题'}`;
    setVisible(elements.setup, false);
    setVisible(elements.complete, false);
    setVisible(elements.session, true);
    answerMode = voiceTranscriptionAvailable ? selectedValue('answer_mode', 'voice') : 'text';
    renderQuestion(response.current_question);
    renderSessionElapsed();
  } catch (error) {
    showToast(error?.message || '创建练习失败，请稍后重试。', 'error', 5200);
  } finally {
    setButtonBusy(elements.start, false);
  }
}

function mistakeQuestionText(mistake) {
  const question = mistake?.question;
  if (question && typeof question === 'object') {
    return String(question.question || question.prompt || question.text || '题目内容暂不可用');
  }
  return String(question || mistake?.question_text || '题目内容暂不可用');
}

function renderMistakes(items) {
  const mistakes = toArray(items);
  elements.mistakeList.replaceChildren();
  elements.mistakeCount.textContent = `${mistakes.length} 题`;
  if (!mistakes.length) {
    const empty = document.createElement('li');
    empty.className = 'practice-mistake-empty';
    empty.textContent = '还没有错题。模拟面试出现低分或未通过题、或快速刷题出现低分题后，会自动收录到这里。';
    elements.mistakeList.append(empty);
    return;
  }
  mistakes.forEach((mistake) => {
    const id = String(mistake?.id || '');
    const item = document.createElement('li');
    item.className = 'practice-mistake-item';
    const copy = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = mistakeQuestionText(mistake);
    title.title = title.textContent;
    const meta = document.createElement('small');
    const hasScore = mistake?.latest_score !== null && mistake?.latest_score !== undefined && mistake?.latest_score !== '';
    const score = Number(mistake?.latest_score);
    const deductions = toArray(mistake?.latest_deductions).map((value) => String(value || '').trim()).filter(Boolean);
    meta.textContent = `${hasScore && Number.isFinite(score) ? `最近 ${score.toFixed(1)} 分` : '最近未评分'} · 已练 ${Math.max(1, Number(mistake?.attempt_count) || 1)} 次${deductions.length ? ` · ${deductions[0]}` : ''}`;
    copy.append(title, meta);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'practice-mistake-delete';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', `从错题本删除：${title.textContent}`);
    remove.addEventListener('click', () => deleteMistake(id, title.textContent, remove));
    item.append(copy, remove);
    elements.mistakeList.append(item);
  });
}

async function loadMistakes({ quiet = false } = {}) {
  try {
    const response = await apiFetch(`/api/practice/mistakes?client_id=${encodeURIComponent(getClientId())}&limit=100`, {
      timeout: 12_000,
    });
    renderMistakes(response?.items);
  } catch (error) {
    if (!quiet) {
      renderMistakes([]);
      elements.mistakeCount.textContent = '同步失败';
      showToast(error?.message || '暂时无法读取错题本。', 'error');
    }
  }
}

async function deleteMistake(id, question, button) {
  if (!id || !window.confirm(`确定从错题本删除“${question}”吗？`)) return;
  try {
    setButtonBusy(button, true, '删除中…');
    await apiFetch(`/api/practice/mistakes/${encodeURIComponent(id)}?client_id=${encodeURIComponent(getClientId())}`, {
      method: 'DELETE', timeout: 12_000,
    });
    await loadMistakes({ quiet: true });
    showToast('已从错题本删除。', 'success');
  } catch (error) {
    showToast(error?.message || '删除错题失败，请稍后重试。', 'error');
  } finally {
    setButtonBusy(button, false);
  }
}

function populateCompanies(companies) {
  const items = toArray(companies).filter((item) => item && (item.id || item.value));
  if (!items.length) return;
  const previous = elements.company.value;
  elements.company.replaceChildren();
  items.forEach((item) => {
    const option = document.createElement('option');
    option.value = String(item.id || item.value);
    option.textContent = String(item.name || item.label || companyLabel(option.value));
    elements.company.append(option);
  });
  if ([...elements.company.options].some((option) => option.value === previous)) elements.company.value = previous;
}

async function initialize() {
  const reviewMode = Boolean(reviewInterviewId);
  elements.formTitle.textContent = reviewMode ? '面后错题重答' : '快速刷题';
  $('.button-label', elements.start).textContent = reviewMode ? '开始重答' : '开始刷题';
  setVisible(elements.reviewNotice, reviewMode);
  if (reviewMode) {
    [elements.company, elements.topic, elements.difficulty, elements.count].forEach((element) => { element.disabled = true; });
  }
  void loadMistakes();
  try {
    const [config, catalog] = await Promise.all([
      apiFetch('/api/config', { timeout: 12_000 }),
      apiFetch('/api/practice/catalog', { timeout: 12_000 }),
    ]);
    populateCompanies(catalog?.companies || config?.companies);
    configureVoiceAvailability(normalizeMode(config?.voice_mode) !== 'L3');
    syncPracticeFilters();
    elements.start.disabled = false;
    if (reviewMode) await createPracticeSession();
  } catch (error) {
    elements.bankBadge.textContent = '练习配置待恢复';
    configureVoiceAvailability(false);
    showToast(error?.message || '暂时无法读取刷题配置。', 'error', 5200);
  }
}

elements.form.addEventListener('submit', createPracticeSession);
document.querySelectorAll('input[name="practice_interview_type"]').forEach((input) => {
  input.addEventListener('change', syncPracticeFilters);
});
elements.voiceMode.addEventListener('click', () => setAnswerMode('voice'));
elements.textMode.addEventListener('click', () => setAnswerMode('text'));
elements.record.addEventListener('click', startRecording);
elements.answer.addEventListener('input', () => {
  if (!answerStartedAt) answerStartedAt = Date.now();
  if (providerTranscript && elements.answer.value.trim() !== providerTranscript) transcriptManuallyEdited = true;
});
elements.hint.addEventListener('click', requestHint);
elements.skip.addEventListener('click', skipQuestion);
elements.submit.addEventListener('click', submitAnswer);
elements.reattempt.addEventListener('click', reattemptQuestion);
elements.next.addEventListener('click', nextQuestion);
elements.previousQuestion.addEventListener('click', previousQuestion);
elements.returnCurrent.addEventListener('click', returnToCurrentQuestion);
elements.exit.addEventListener('click', finishPractice);
elements.again.addEventListener('click', () => location.assign('/practice'));
window.addEventListener('pagehide', () => {
  clearInterval(elapsedTimer);
  audio?.disableMicrophone();
  closeSocket();
});

initialize();
