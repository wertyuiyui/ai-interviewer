import {
  $, apiFetch, base64ToArrayBuffer, companyLabel, formatSeconds,
  getClientId, getCurrentSession, modeLabel, normalizeMode, setButtonBusy, showToast,
  setCurrentSession,
} from './common.js?v=20260830-profile-bank-v2';
import { AudioSession } from './audio-session.js?v=20260830-mic-release';

const query = new URLSearchParams(location.search);
const storedSession = getCurrentSession();
const sessionId = query.get('session') || storedSession?.id || storedSession?.session_id || '';

const elements = {
  answerControl: $('#answerControl'),
  answerElapsed: $('#answerElapsed'),
  answerStateLabel: $('#answerStateLabel'),
  avatar: $('#avatarScene'),
  answerTimeGuide: $('#answerTimeGuide'),
  answerTimeValue: $('#answerTimeValue'),
  company: $('#companyChip'),
  connection: $('#connectionState'),
  endButton: $('#endButton'),
  endDialog: $('#endDialog'),
  endDialogMessage: $('#endDialogMessage'),
  endDialogTitle: $('#endDialogTitle'),
  endMessage: $('#endMessage'),
  endOverlay: $('#endOverlay'),
  endTitle: $('#endTitle'),
  hintButton: $('#hintButton'),
  hintMeta: $('#hintMeta'),
  hintPanel: $('#hintPanel'),
  hintText: $('#hintText'),
  unknownButton: $('#unknownButton'),
  inputMeter: $('#inputMeter'),
  inputMeterFill: $('#inputMeterFill'),
  interviewerVoiceToggle: $('#interviewerVoiceToggle'),
  interrupt: $('#interruptLabel'),
  join: $('#joinButton'),
  liveTranscriptBar: $('#liveTranscriptBar'),
  liveTranscriptStatus: $('#liveTranscriptStatus'),
  liveTranscriptText: $('#liveTranscriptText'),
  messageForm: $('#messageForm'),
  messageInput: $('#messageInput'),
  microphoneHealth: $('#microphoneHealth'),
  microphonePanel: $('#microphonePanel'),
  microphoneSelect: $('#microphoneSelect'),
  microphoneState: $('#microphoneState'),
  micToggle: $('#micToggle'),
  mode: $('#modePill'),
  permissionNote: $('#permissionNote'),
  readyDescription: $('#readyDescription'),
  readyPanel: $('#readyPanel'),
  rawCaptureToggle: $('#rawCaptureToggle'),
  resumeMismatchDialog: $('#resumeMismatchDialog'),
  resumeMismatchMessage: $('#resumeMismatchMessage'),
  scrollLatest: $('#scrollLatest'),
  send: $('#sendButton'),
  startAnswer: $('#startAnswerButton'),
  stageHint: $('#stageHint'),
  stageTitle: $('#stageTitle'),
  stress: $('#stressChip'),
  stressLabel: $('#stressLabel'),
  timer: $('#timer'),
  transcript: $('#transcriptList'),
  transcriptPlaceholder: $('#transcriptPlaceholder'),
  waveform: $('#waveform'),
  endAnswer: $('#endAnswerButton'),
};

let interview = null;
let serverConfig = null;
let voiceMode = 'L3';
let socket = null;
let audio = null;
let phase = 'preparing';
let intentionallyClosed = false;
let reconnectAttempts = 0;
let reconnectTimer = 0;
let heartbeatTimer = 0;
let timerInterval = 0;
let deadline = 0;
let unlimitedDuration = Boolean(
  storedSession?.unlimited === true
  || (storedSession && Object.hasOwn(storedSession, 'duration_minutes') && storedSession.duration_minutes === null)
);
let localFinishSent = false;
let inputLevel = 0;
let outputLevel = 0;
let lastAudioWarningAt = 0;
let interruptTimer = 0;
let lastTyped = null;
let audioFileQueue = Promise.resolve();
let audioEpoch = 0;
let answerPending = false;
let audioUplinkReady = false;
let audioUplinkReadyAt = 0;
let providerAudioReady = false;
let lastAudioFrameAt = 0;
let lastCaptureWarningAt = 0;
let lastSilenceWarningAt = 0;
let lastAudibleInputAt = 0;
let inputRawLevel = 0;
let nearSilenceWarning = false;
let serverInputSignal = 'unknown';
let lastServerInputLevelAt = 0;
let serverQuietSince = 0;
let droppedAudioFrames = 0;
let captureWatchdogTimer = 0;
let pendingAudioFrame = null;
let pendingAudioFlushTimer = 0;
let microphoneSwitching = false;
let microphoneExplicitlyDisabled = false;
let lastMicrophoneStateSent = null;
let liveTranscriptTimer = 0;
let candidatePartialText = '';
let candidatePartialItemId = '';
let latestCandidateItemId = '';
let pressureMomentTimer = 0;
const MAX_AUDIO_BACKLOG_BYTES = 24 * 1024;
let currentQuestion = '';
let questionReady = false;
let candidateAudioExpected = false;
let candidateAudioExpectedSince = 0;
let answerState = 'idle';
let answerStartedAt = 0;
let answerElapsedMs = 0;
let answerClockTimer = 0;
let answerAudioFrames = 0;
let answerStartAwaitingAck = false;
const INTERVIEWER_VOICE_KEY = 'mock_interview.interviewer_voice_enabled.v1';
let interviewerVoiceEnabled = readInterviewerVoicePreference();
let interviewerAudioStreamActive = false;
let interviewerAudioSuppressedUntilStreamEnd = false;
let hintLoading = false;
let resumeMismatchPrompted = false;
const hintedQuestions = new Map();
const partialTurns = new Map();
const candidateTurns = new Map();
const interviewerTurns = new Map();
const pendingTranscriptCorrections = new Map();

function readInterviewerVoicePreference() {
  try {
    return localStorage.getItem(INTERVIEWER_VOICE_KEY) !== 'false';
  } catch {
    return true;
  }
}

function updateHintAvailability() {
  const level = currentQuestion ? (hintedQuestions.get(currentQuestion) || 0) : 0;
  const answerOpen = ['ready', 'answering'].includes(answerState);
  elements.hintButton.disabled = phase !== 'live' || !answerOpen || answerPending || !questionReady || !currentQuestion || hintLoading || level >= 2;
  elements.unknownButton.disabled = phase !== 'live' || !answerOpen || answerPending || !questionReady || !currentQuestion;
  if (!hintLoading) $('.button-label', elements.hintButton).textContent = level >= 2 ? '本题已提示' : (level === 1 ? '进一步提示' : '给我一点提示');
}

function inferRecommendedAnswerSeconds(text = '') {
  const question = String(text || '');
  if (/自我介绍|introduce yourself/i.test(question)) return 90;
  if (/手撕|算法|复杂度|实现|design (?:a|an|the)|system design/i.test(question)) return 180;
  if (/项目|架构|链路|选型|故障|指标|trade.?off|难点/i.test(question)) return 120;
  if (/反问|还有什么.*问|any questions/i.test(question)) return 60;
  return 75;
}

function providedRecommendedAnswerSeconds(event = {}, text = '') {
  if (/今天的面试就到这里|感谢你的时间|面试时间到|时间到了/i.test(String(text || ''))) return 0;
  const raw = event.recommended_answer_seconds
    ?? event.suggested_answer_seconds
    ?? event.answer_time_seconds
    ?? event.recommended_seconds;
  const numeric = Number(raw);
  return Number.isFinite(numeric) && numeric > 0
    ? Math.min(600, Math.max(15, Math.round(numeric)))
    : null;
}

function recommendedAnswerSeconds(event = {}, text = '') {
  return providedRecommendedAnswerSeconds(event, text) ?? inferRecommendedAnswerSeconds(text);
}

function formatRecommendedAnswerTime(seconds) {
  const value = Math.max(15, Math.round(Number(seconds) || 75));
  if (value >= 120 && value % 60 === 0) return `约 ${value / 60} 分钟`;
  return `约 ${value} 秒`;
}

function showAnswerTimeGuide(seconds) {
  if (!(Number(seconds) > 0)) {
    elements.answerTimeGuide.classList.add('is-hidden');
    return;
  }
  elements.answerTimeValue.textContent = formatRecommendedAnswerTime(seconds);
  elements.answerTimeGuide.classList.remove('is-hidden');
}

function setCurrentQuestion(value, event = {}, { inferTiming = true } = {}) {
  const next = String(value || '').trim();
  if (!next) return false;
  const questionChanged = currentQuestion !== next;
  const providedSeconds = providedRecommendedAnswerSeconds(event, next);
  const suggestedSeconds = providedSeconds ?? (inferTiming ? inferRecommendedAnswerSeconds(next) : null);
  if (!(suggestedSeconds > 0)) {
    if (suggestedSeconds === 0) {
      showAnswerTimeGuide(0);
      return false;
    }
  }
  if (currentQuestion && currentQuestion !== next) elements.hintPanel.classList.add('is-hidden');
  currentQuestion = next;
  questionReady = true;
  if (suggestedSeconds !== null) showAnswerTimeGuide(suggestedSeconds);
  if (phase === 'live' && !['answering', 'sealing'].includes(answerState)
      && (questionChanged || answerState === 'idle')) {
    setAnswerState('ready', { resetElapsed: true });
  }
  updateHintAvailability();
  return true;
}

function renderAnswerElapsed() {
  const elapsed = answerState === 'answering' && answerStartedAt
    ? Math.max(0, Date.now() - answerStartedAt)
    : Math.max(0, answerElapsedMs);
  const seconds = Math.floor(elapsed / 1000);
  elements.answerElapsed.textContent = formatSeconds(seconds);
  elements.answerElapsed.dateTime = `PT${seconds}S`;
}

function setAnswerState(nextState, {
  elapsedMs = null,
  resetElapsed = false,
  startedAt = 0,
} = {}) {
  const allowed = new Set(['idle', 'ready', 'answering', 'sealing']);
  const next = allowed.has(nextState) ? nextState : 'idle';
  const wasAnswering = answerState === 'answering';
  if (wasAnswering && answerStartedAt) {
    answerElapsedMs = Math.max(answerElapsedMs, Date.now() - answerStartedAt);
  }
  clearInterval(answerClockTimer);
  answerClockTimer = 0;
  if (resetElapsed) {
    answerElapsedMs = 0;
    answerAudioFrames = 0;
  }
  const suppliedElapsed = Number(elapsedMs);
  if (elapsedMs !== null && elapsedMs !== undefined
      && Number.isFinite(suppliedElapsed) && suppliedElapsed >= 0) {
    answerElapsedMs = suppliedElapsed;
  }
  answerState = next;
  if (next === 'answering') {
    const suppliedStart = Number(startedAt);
    answerStartedAt = Number.isFinite(suppliedStart) && suppliedStart > 0
      ? suppliedStart
      : Date.now() - answerElapsedMs;
    answerClockTimer = setInterval(renderAnswerElapsed, 250);
  } else {
    answerStartedAt = 0;
  }

  const live = phase === 'live';
  const labels = {
    idle: '等待面试官提问',
    ready: '准备好后再开始计时',
    answering: '本题回答计时中',
    sealing: '正在封口并整理转写',
  };
  elements.answerControl.dataset.state = next;
  elements.answerStateLabel.textContent = labels[next];
  elements.answerControl.classList.toggle('is-hidden', !live);
  elements.startAnswer.classList.toggle('is-hidden', ['answering', 'sealing'].includes(next));
  elements.endAnswer.classList.toggle('is-hidden', next !== 'answering');
  elements.startAnswer.disabled = !live || next !== 'ready' || answerPending;
  elements.endAnswer.disabled = !live || next !== 'answering';
  const textEnabled = live && next === 'answering' && !answerPending;
  elements.messageInput.disabled = !textEnabled;
  elements.send.disabled = !textEnabled;
  elements.messageInput.placeholder = next === 'answering'
    ? (voiceMode === 'L3' ? '输入本题回答…' : '也可以在这里打字回答…')
    : '点击“开始回答”后可在这里打字…';
  if (next === 'answering') {
    expectCandidateAudio(voiceMode !== 'L3');
  } else {
    expectCandidateAudio(false);
  }
  renderAnswerElapsed();
  syncAudioUplink();
  updateHintAvailability();
  if (textEnabled && voiceMode === 'L3') elements.messageInput.focus();
}

function currentAnswerElapsedMs() {
  if (answerState === 'answering' && answerStartedAt) return Math.max(0, Date.now() - answerStartedAt);
  return Math.max(0, answerElapsedMs);
}

function startCurrentAnswer() {
  if (phase !== 'live' || answerState !== 'ready' || answerPending) return;
  if (!sendJson({ type: 'answer.start' })) {
    showToast('连接尚未恢复，暂时无法开始本题。', 'error');
    return;
  }
  answerStartAwaitingAck = true;
  if (interviewerAudioStreamActive) interviewerAudioSuppressedUntilStreamEnd = true;
  invalidateAudioPlayback();
  answerPending = false;
  setAnswerState('answering', { resetElapsed: true });
  setLiveTranscript(
    'idle',
    voiceMode === 'L3' ? '正在填写本题回答' : '回答已开始，等待你开口',
    voiceMode === 'L3' ? '写完后点击“结束回答”提交' : '点击“结束回答”后才会封口并进入下一题',
  );
  setStage('listening', '正在听你回答', '回答计时已开始；讲完后请点击“结束回答”。');
}

function finishCurrentAnswer({ allowEmpty = false } = {}) {
  if (phase !== 'live' || answerState !== 'answering' || answerPending) return;
  const text = elements.messageInput.value.trim();
  if (!allowEmpty && !text && voiceMode === 'L3') {
    showToast('请先输入本题回答，再点击“结束回答”。', 'error');
    elements.messageInput.focus();
    return;
  }
  if (!allowEmpty && !text && voiceMode !== 'L3' && answerAudioFrames === 0) {
    showToast('还没有收到语音；请先回答，或在右侧输入文字。', 'error');
    return;
  }
  const elapsedMs = Math.round(currentAnswerElapsedMs());
  setAnswerState('sealing', { elapsedMs });
  const payload = { type: 'answer.end', elapsed_ms: elapsedMs };
  if (text) payload.text = text;
  if (!sendJson(payload)) {
    setAnswerState('answering', { elapsedMs });
    showToast('连接尚未恢复，本题尚未结束。', 'error');
    return false;
  }
  answerStartAwaitingAck = false;
  answerPending = true;
  questionReady = false;
  elements.messageInput.value = '';
  elements.messageInput.rows = 1;
  setLiveTranscript(
    text ? 'final' : 'partial',
    text ? '文字回答已提交' : '正在完成本轮转写',
    text || '请稍候，系统正在封口并整理完整回答…',
  );
  setStage('thinking', '面试官正在思考', '本题回答已结束，计时已停止。');
  updateHintAvailability();
  return true;
}

async function requestHint() {
  if (elements.hintButton.disabled || hintLoading) return;
  hintLoading = true;
  setButtonBusy(elements.hintButton, true, '正在拆解…');
  try {
    const event = await apiFetch(`/api/interviews/${encodeURIComponent(sessionId)}/hint`, {
      method: 'POST',
      timeout: 15_000,
    });
    const question = String(event?.question || currentQuestion).trim();
    const level = Math.max(1, Math.min(2, Number(event?.level) || 1));
    if (question) hintedQuestions.set(question, Math.max(level, hintedQuestions.get(question) || 0));
    elements.hintText.textContent = event?.hint || '暂时没有可用提示，请先按结论、依据、边界三步组织回答。';
    elements.hintMeta.textContent = `第 ${Number(event?.ordinal) || '—'} 题 · ${level === 2 ? '简化示例' : '简化思路'} · 已计入报告`;
    elements.hintPanel.classList.remove('is-hidden');
    scrollTranscript(true);
  } catch (error) {
    showToast(error?.message || '提示暂时不可用。', 'error', 4800);
  } finally {
    hintLoading = false;
    setButtonBusy(elements.hintButton, false);
    updateHintAvailability();
  }
}

function setConnection(kind, label) {
  elements.connection.className = `connection-state${kind ? ` is-${kind}` : ''}`;
  $('span', elements.connection).textContent = label;
}

function setStage(kind, title, hint = '') {
  elements.avatar.classList.toggle('is-speaking', kind === 'speaking');
  elements.avatar.classList.toggle('is-listening', kind === 'listening');
  elements.stageTitle.textContent = title;
  if (hint) elements.stageHint.textContent = hint;
}

function expectCandidateAudio(value) {
  const expected = Boolean(value);
  if (expected && !candidateAudioExpected) candidateAudioExpectedSince = Date.now();
  if (!expected) candidateAudioExpectedSince = 0;
  candidateAudioExpected = expected;
}

function setLiveTranscript(state, status, text, autoClearMs = 0) {
  clearTimeout(liveTranscriptTimer);
  liveTranscriptTimer = 0;
  elements.liveTranscriptBar.dataset.state = state;
  elements.liveTranscriptStatus.textContent = status;
  elements.liveTranscriptText.textContent = text;
  if (autoClearMs > 0) {
    liveTranscriptTimer = setTimeout(() => {
      const textMode = voiceMode === 'L3';
      setLiveTranscript(
        'idle',
        textMode ? '文字回答模式' : '等待你开口',
        textMode ? '提交的文字回答会记录在下方' : '语音识别结果会在这里逐步出现',
      );
    }, autoClearMs);
  }
}

function resetCandidateTranscript() {
  candidatePartialText = '';
  candidatePartialItemId = '';
}

function beginCandidateSpeech() {
  discardPartialTurn('candidate');
  resetCandidateTranscript();
  setLiveTranscript('speech', '检测到你正在说话', '正在聆听并等待识别结果…');
}

function updateCandidateTranscript(event) {
  const itemId = String(event.item_id || event.itemId || '');
  if (itemId && candidatePartialItemId && itemId !== candidatePartialItemId) {
    discardPartialTurn('candidate');
    candidatePartialText = '';
  }
  if (itemId) candidatePartialItemId = itemId;
  const chunk = String(extractText(event) || '');
  const append = event.text === undefined && event.transcript === undefined;
  candidatePartialText = append ? `${candidatePartialText}${chunk}` : chunk;
  renderTurn('candidate', chunk, { partial: true, append });
  setLiveTranscript(
    'partial',
    '正在实时转写',
    candidatePartialText.trim() || '已检测到声音，正在识别…',
  );
}

function finalizeCandidateTranscript(event) {
  const finalText = String(extractText(event) || '').trim() || candidatePartialText.trim();
  const itemId = String(event.item_id || event.itemId || candidatePartialItemId || '');
  const turn = renderTurn('candidate', finalText, { itemId, editable: Boolean(itemId) });
  if (itemId && turn) {
    latestCandidateItemId = itemId;
    candidateTurns.set(itemId, turn);
  }
  resetCandidateTranscript();
  setLiveTranscript('final', '本轮回答已记录，可手动修正', finalText || '本轮没有识别到有效文字', 2600);
}

function selectedMicrophoneLabel() {
  return elements.microphoneSelect.selectedOptions?.[0]?.textContent?.trim() || '当前麦克风';
}

function renderInputMeter(value = inputRawLevel) {
  const raw = Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  const percent = Math.min(100, Math.round(Math.sqrt(raw) * 500));
  elements.inputMeterFill.style.transform = `scaleX(${percent / 100})`;
  elements.inputMeter.setAttribute('aria-valuenow', String(percent));
}

function isMicrophoneCaptureEnabled() {
  return Boolean(
    voiceMode !== 'L3'
    && !microphoneExplicitlyDisabled
    && audio?.hasMicrophone
    && !audio.muted
    && !audio.trackMuted
  );
}

function sendMicrophoneState(enabled, { force = false } = {}) {
  const nextState = Boolean(enabled);
  if (!force && lastMicrophoneStateSent === nextState) return true;
  if (!sendJson({ type: 'microphone.state', enabled: nextState })) return false;
  lastMicrophoneStateSent = nextState;
  return true;
}

function disableMicrophoneCapture({ explicit = false, notify = true } = {}) {
  if (explicit) microphoneExplicitlyDisabled = true;
  clearPendingAudioFrame();
  audio?.disableMicrophone();
  inputRawLevel = 0;
  inputLevel = 0;
  lastAudioFrameAt = 0;
  renderInputMeter(0);
  syncAudioUplink();
  if (notify) sendMicrophoneState(false);
}

function updateMicrophoneHealth() {
  const textOnly = voiceMode === 'L3';
  elements.microphonePanel.classList.toggle('is-hidden', textOnly);
  if (textOnly) return;
  let state = 'idle';
  let label = '进入面试后检测输入';
  if (microphoneSwitching) {
    state = 'pending';
    label = '正在切换输入设备…';
  } else if (microphoneExplicitlyDisabled) {
    state = 'muted';
    label = '麦克风已关闭';
  } else if (!audio?.hasMicrophone) {
    state = phase === 'ready' || phase === 'preparing' ? 'idle' : 'error';
    label = phase === 'ready' || phase === 'preparing' ? '进入面试后检测输入' : '未取得麦克风输入';
  } else if (audio.muted) {
    state = 'muted';
    label = '麦克风已静音';
  } else if (audio.trackMuted) {
    state = 'error';
    label = '系统暂停了这支麦克风';
  } else if (nearSilenceWarning) {
    state = 'warning';
    label = `${selectedMicrophoneLabel()}：服务端仍检测为近静音`;
  } else if (!providerAudioReady && ['connecting', 'live'].includes(phase)) {
    state = 'pending';
    label = '麦克风已就绪，等待语音服务';
  } else {
    state = 'live';
    label = `${selectedMicrophoneLabel()}${audio.rawCapture ? ' · 原始输入' : ''}`;
  }
  elements.microphoneHealth.dataset.state = state;
  elements.microphoneState.textContent = label;
  elements.microphoneSelect.disabled = microphoneSwitching;
  elements.rawCaptureToggle.disabled = microphoneSwitching || !audio?.context;
  elements.micToggle.disabled = microphoneSwitching;
  elements.micToggle.classList.toggle('is-muted', Boolean(
    microphoneExplicitlyDisabled || audio?.muted || audio?.trackMuted || !audio?.hasMicrophone,
  ));
}

function syncAudioUplink() {
  const wasReady = audioUplinkReady;
  audioUplinkReady = Boolean(
    providerAudioReady
    && phase === 'live'
    && answerState === 'answering'
    && isMicrophoneCaptureEnabled()
  );
  if (audioUplinkReady && !wasReady) {
    audioUplinkReadyAt = Date.now();
    lastAudioFrameAt = 0;
    lastAudibleInputAt = audioUplinkReadyAt;
    nearSilenceWarning = false;
    serverQuietSince = 0;
  } else if (!audioUplinkReady) {
    audioUplinkReadyAt = 0;
    clearPendingAudioFrame();
  }
  updateMicrophoneHealth();
}

async function refreshMicrophoneDevices() {
  if (voiceMode === 'L3') return;
  if (!audio) audio = createAudio();
  try {
    const devices = await audio.listInputDevices();
    const preferred = audio.selectedDeviceId || elements.microphoneSelect.value;
    const fragment = document.createDocumentFragment();
    const defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = '系统默认麦克风';
    fragment.append(defaultOption);
    const seen = new Set(['']);
    devices.forEach((device) => {
      if (!device.deviceId || seen.has(device.deviceId)) return;
      seen.add(device.deviceId);
      const option = document.createElement('option');
      option.value = device.deviceId;
      option.textContent = device.label;
      fragment.append(option);
    });
    elements.microphoneSelect.replaceChildren(fragment);
    if (preferred && seen.has(preferred)) elements.microphoneSelect.value = preferred;
  } catch {
    // Device enumeration can be blocked before permission; the default option remains usable.
  }
  updateMicrophoneHealth();
}

async function switchMicrophoneDevice() {
  if (voiceMode === 'L3' || microphoneSwitching) return;
  if (microphoneExplicitlyDisabled) {
    updateMicrophoneHealth();
    showToast('输入设置已保存；点击麦克风按钮后才会重新启用采集。', 'info');
    return;
  }
  microphoneSwitching = true;
  updateMicrophoneHealth();
  try {
    if (!audio) audio = createAudio();
    if (!audio.context) await audio.initialize({ capture: false });
    await audio.enableMicrophone(elements.microphoneSelect.value, {
      force: true,
      raw: elements.rawCaptureToggle.checked,
    });
    if (microphoneExplicitlyDisabled) {
      disableMicrophoneCapture({ notify: true });
      return;
    }
    audio.setMuted(false);
    elements.rawCaptureToggle.checked = Boolean(audio.rawCapture);
    nearSilenceWarning = false;
    serverInputSignal = 'unknown';
    serverQuietSince = 0;
    syncAudioUplink();
    sendMicrophoneState(true);
    await refreshMicrophoneDevices();
    showToast(`已切换到“${selectedMicrophoneLabel()}”${audio.rawCapture ? '（原始输入）' : ''}。`, 'success');
  } catch (error) {
    elements.rawCaptureToggle.checked = Boolean(audio?.rawCapture);
    await refreshMicrophoneDevices();
    const detail = error?.message || '无法切换麦克风。';
    const recovery = audio?.hasMicrophone
      ? '已保留原输入设备。'
      : '采集已停止，请重新选择或点击麦克风按钮。';
    showToast(`${detail} ${recovery}`, 'error', 6000);
  } finally {
    microphoneSwitching = false;
    updateMicrophoneHealth();
  }
}

function setMode(mode, notify = false) {
  const previous = voiceMode;
  voiceMode = normalizeMode(mode);
  $('span', elements.mode).textContent = modeLabel(voiceMode);
  const textOnly = voiceMode === 'L3';
  elements.micToggle.classList.toggle('is-hidden', textOnly || phase === 'preparing');
  renderInterviewerVoicePreference();
  elements.microphonePanel.classList.toggle('is-hidden', textOnly);
  elements.permissionNote.textContent = textOnly
    ? '本场为纯文字模式，不会请求麦克风权限。'
    : '语音模式会请求麦克风权限，建议佩戴耳机。';
  elements.messageInput.placeholder = textOnly ? '输入你的回答…' : '也可以在这里打字回答…';
  if (phase !== 'live' || previous !== voiceMode) {
    setLiveTranscript(
      'idle',
      textOnly ? '文字回答模式' : '等待你开口',
      textOnly ? '提交的文字回答会记录在下方' : '语音识别结果会在这里逐步出现',
    );
  }
  if (textOnly && audio) {
    providerAudioReady = false;
    syncAudioUplink();
    const previousAudio = audio;
    invalidateAudioPlayback();
    audio = null;
    previousAudio.disableMicrophone();
    sendMicrophoneState(false);
    previousAudio.close();
  }
  updateMicrophoneHealth();
  if (phase === 'live') setAnswerState(answerState);
  if (notify && previous !== voiceMode) showToast(`服务已切换为 ${modeLabel(voiceMode)}`, 'info', 4500);
}

function updateDurationMode(payload = {}) {
  const source = payload?.session && typeof payload.session === 'object' ? payload.session : payload;
  if (source?.unlimited === true || source?.duration_mode === 'infinite') {
    unlimitedDuration = true;
    return;
  }
  if (source && Object.hasOwn(source, 'duration_minutes')) {
    unlimitedDuration = source.duration_minutes === null;
    return;
  }
  if (interview && Object.hasOwn(interview, 'duration_minutes')) {
    unlimitedDuration = interview.duration_minutes === null;
  }
}

function getStressLevel() {
  const raw = interview?.stress_level ?? storedSession?.stress_level;
  if (raw !== undefined && raw !== null && raw !== '') {
    const level = Number(raw);
    if (Number.isFinite(level)) return Math.min(3, Math.max(0, Math.round(level)));
  }
  return Boolean(interview?.stress ?? storedSession?.stress) ? 2 : 0;
}

function syncTimer(payload = {}) {
  updateDurationMode(payload);
  if (unlimitedDuration) {
    deadline = 0;
    renderTimer();
    return;
  }
  const remainingValue = payload.remaining_seconds ?? payload.remaining ?? interview?.remaining_seconds;
  const remaining = remainingValue === null || remainingValue === '' || remainingValue === undefined ? Number.NaN : Number(remainingValue);
  if (Number.isFinite(remaining)) deadline = Date.now() + Math.max(0, remaining) * 1000;
  else {
    const endValue = payload.ends_at ?? payload.deadline_at ?? interview?.ends_at ?? interview?.deadline_at;
    if (endValue) {
      const numeric = Number(endValue);
      deadline = Number.isFinite(numeric)
        ? (numeric < 10_000_000_000 ? numeric * 1000 : numeric)
        : new Date(endValue).getTime();
    }
  }
  if (!Number.isFinite(deadline) || deadline <= 0) {
    const rawDuration = interview?.duration_minutes ?? storedSession?.duration_minutes ?? 15;
    const duration = Number(rawDuration);
    const safeDuration = Number.isFinite(duration) && duration > 0 ? duration : 15;
    if (phase !== 'live') {
      deadline = 0;
      $('small', elements.timer).textContent = '时长';
      $('strong', elements.timer).textContent = `${safeDuration} 分钟`;
      elements.timer.classList.remove('is-low', 'is-unlimited');
      elements.timer.removeAttribute('datetime');
      return;
    }
    deadline = Date.now() + safeDuration * 60_000;
  }
  renderTimer();
}

function renderTimer() {
  if (unlimitedDuration) {
    $('small', elements.timer).textContent = '时长';
    $('strong', elements.timer).textContent = '无限·手动结束';
    elements.endButton.textContent = '手动结束面试';
    elements.endDialogTitle.textContent = '确定手动结束吗？';
    elements.endDialogMessage.textContent = '结束后将基于当前已完成的问答生成报告，本场面试不能继续。';
    elements.timer.classList.remove('is-low');
    elements.timer.classList.add('is-unlimited');
    elements.timer.removeAttribute('datetime');
    return;
  }
  if (!deadline) return;
  const remaining = Math.max(0, (deadline - Date.now()) / 1000);
  $('small', elements.timer).textContent = '剩余';
  $('strong', elements.timer).textContent = formatSeconds(remaining);
  elements.endButton.textContent = '提前结束面试';
  elements.endDialogTitle.textContent = '确定提前结束吗？';
  elements.endDialogMessage.textContent = '结束后将基于当前已完成的问答生成报告，本场面试不能继续。';
  elements.timer.classList.remove('is-unlimited');
  elements.timer.classList.toggle('is-low', remaining <= 60);
  elements.timer.dateTime = `PT${Math.ceil(remaining)}S`;
  if (remaining <= 0 && phase === 'live' && !localFinishSent) finishInterview('time');
}

function isNearTranscriptBottom() {
  return elements.transcript.scrollHeight - elements.transcript.scrollTop - elements.transcript.clientHeight < 90;
}

function scrollTranscript(force = false) {
  if (force || isNearTranscriptBottom()) {
    elements.transcript.scrollTop = elements.transcript.scrollHeight;
    elements.scrollLatest.classList.remove('is-visible');
  } else {
    elements.scrollLatest.classList.add('is-visible');
  }
}

function createTurn(role, text, { partial = false, itemId = '', editable = false, recommendedSeconds = 0 } = {}) {
  const turn = document.createElement('article');
  turn.className = `transcript-turn is-${role}${partial ? ' is-partial' : ''}`;
  if (itemId) turn.dataset.itemId = itemId;
  const label = document.createElement('div');
  label.className = 'turn-label';
  const icon = document.createElement('i');
  icon.textContent = role === 'candidate' ? '我' : 'AI';
  const labelText = document.createElement('span');
  labelText.textContent = role === 'candidate' ? '我的回答' : '面试官';
  label.append(icon, labelText);
  if (role === 'candidate' && !partial) {
    const transcriptMeta = document.createElement('small');
    transcriptMeta.className = 'turn-transcript-meta';
    transcriptMeta.textContent = editable ? '语音转写 · 可修正' : '文字回答';
    label.append(transcriptMeta);
  }
  if (role === 'interviewer' && recommendedSeconds > 0) {
    const time = document.createElement('small');
    time.className = 'turn-answer-time';
    time.textContent = `建议回答 ${formatRecommendedAnswerTime(recommendedSeconds).replace(/^约\s*/, '')}`;
    label.append(time);
  }
  const bubble = document.createElement('div');
  bubble.className = 'turn-bubble';
  bubble.textContent = text;
  turn.append(label, bubble);
  if (role === 'candidate' && editable && itemId) addTranscriptCorrectionControls(turn, itemId, text);
  return turn;
}

function addTranscriptCorrectionControls(turn, itemId, originalText) {
  if (!turn || !itemId || $('.turn-correction', turn)) return;
  const controls = document.createElement('div');
  controls.className = 'turn-correction';
  const edit = document.createElement('button');
  edit.className = 'turn-edit-button';
  edit.type = 'button';
  edit.dataset.action = 'edit-transcript';
  edit.textContent = '修正转写';
  const form = document.createElement('form');
  form.className = 'transcript-edit-form is-hidden';
  form.dataset.itemId = itemId;
  const textarea = document.createElement('textarea');
  textarea.name = 'correctedTranscript';
  textarea.maxLength = 4000;
  textarea.rows = 3;
  textarea.value = String(originalText || '');
  textarea.setAttribute('aria-label', '修正后的语音转写');
  const actions = document.createElement('div');
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.dataset.action = 'cancel-transcript-edit';
  cancel.textContent = '取消';
  const save = document.createElement('button');
  save.type = 'submit';
  save.className = 'save-transcript-button';
  save.textContent = '保存修正';
  actions.append(cancel, save);
  form.append(textarea, actions);
  controls.append(edit, form);
  turn.append(controls);
}

function discardPartialTurn(role) {
  partialTurns.get(role)?.remove();
  partialTurns.delete(role);
}

function renderTurn(role, value, {
  partial = false,
  append = false,
  suppressDuplicate = false,
  itemId = '',
  editable = false,
  recommendedSeconds = 0,
} = {}) {
  const rawText = String(value || '');
  const text = append ? rawText : rawText.trim();
  const existing = partialTurns.get(role);
  if (partial && role === 'candidate' && lastTyped && Date.now() - lastTyped.at < 12_000
      && lastTyped.text.replace(/\s/g, '').startsWith(text.replace(/\s/g, ''))) return;
  if (!partial && suppressDuplicate && lastTyped && role === 'candidate'
      && Date.now() - lastTyped.at < 12_000 && text.replace(/\s/g, '') === lastTyped.text.replace(/\s/g, '')) {
    existing?.remove();
    partialTurns.delete(role);
    lastTyped = null;
    return;
  }
  if (!partial && suppressDuplicate && text) {
    const finalized = elements.transcript.querySelectorAll(`.transcript-turn.is-${role}:not(.is-partial)`);
    const latest = finalized[finalized.length - 1];
    if (latest && $('.turn-bubble', latest)?.textContent?.trim() === text) return;
  }
  if (!text && !existing) return;
  const shouldStick = isNearTranscriptBottom();
  elements.transcriptPlaceholder?.remove();

  if (partial) {
    if (existing) {
      const bubble = $('.turn-bubble', existing);
      bubble.textContent = append ? `${bubble.textContent}${text}` : text;
    } else {
      const turn = createTurn(role, text, { partial: true, itemId });
      partialTurns.set(role, turn);
      elements.transcript.append(turn);
    }
  } else if (existing) {
    $('.turn-bubble', existing).textContent = text || $('.turn-bubble', existing).textContent;
    existing.classList.remove('is-partial');
    if (itemId) existing.dataset.itemId = itemId;
    if (role === 'candidate' && editable && itemId) {
      const label = $('.turn-label', existing);
      if (!$('.turn-transcript-meta', label)) {
        const transcriptMeta = document.createElement('small');
        transcriptMeta.className = 'turn-transcript-meta';
        transcriptMeta.textContent = '语音转写 · 可修正';
        label.append(transcriptMeta);
      }
      addTranscriptCorrectionControls(existing, itemId, text || $('.turn-bubble', existing).textContent);
    }
    if (role === 'interviewer' && recommendedSeconds > 0 && !$('.turn-answer-time', existing)) {
      const time = document.createElement('small');
      time.className = 'turn-answer-time';
      time.textContent = `建议回答 ${formatRecommendedAnswerTime(recommendedSeconds).replace(/^约\s*/, '')}`;
      $('.turn-label', existing).append(time);
    }
    partialTurns.delete(role);
  } else if (text) {
    const turn = createTurn(role, text, { itemId, editable, recommendedSeconds });
    elements.transcript.append(turn);
    if (shouldStick) scrollTranscript(true);
    else elements.scrollLatest.classList.add('is-visible');
    return turn;
  }
  if (shouldStick) scrollTranscript(true);
  else elements.scrollLatest.classList.add('is-visible');
  return existing || partialTurns.get(role) || null;
}

function extractText(event) {
  return event.spoken_text
    ?? event.audio_transcript
    ?? event.transcript
    ?? event.text
    ?? event.content
    ?? event.delta
    ?? '';
}

function interviewerMessageId(event = {}) {
  return String(
    event.response_id
    || event.announcement_id
    || event.item_id
    || event.message_id
    || '',
  );
}

function isPressureInterjection(event = {}) {
  return event.interjection === true || String(event.interjection || '').toLowerCase() === 'true';
}

function markPressureInterjection(turn) {
  if (!turn) return;
  turn.classList.add('is-pressure-interjection');
  const label = $('.turn-label', turn);
  if (!label || $('.pressure-interjection-label', label)) return;
  const badge = document.createElement('small');
  badge.className = 'turn-transcript-meta pressure-interjection-label';
  badge.textContent = '压力插话';
  label.append(badge);
}

function syncInterviewerTranscript(event = {}, { updateQuestion = true } = {}) {
  const messageId = interviewerMessageId(event);
  const finalized = elements.transcript.querySelectorAll('.transcript-turn.is-interviewer:not(.is-partial)');
  const turn = interviewerTurns.get(messageId) || finalized[finalized.length - 1];
  const spokenText = String(event.spoken_text || event.audio_transcript || event.transcript || '').trim();
  if (!turn || !spokenText) return;
  $('.turn-bubble', turn).textContent = spokenText;
  if (messageId) {
    turn.dataset.itemId = messageId;
    interviewerTurns.set(messageId, turn);
  }
  if (!updateQuestion) return;
  if (isPressureInterjection(event)) {
    markPressureInterjection(turn);
    showPressureMoment();
    return;
  }
  const seconds = providedRecommendedAnswerSeconds(event, spokenText);
  if (seconds > 0) {
    let time = $('.turn-answer-time', turn);
    if (!time) {
      time = document.createElement('small');
      time.className = 'turn-answer-time';
      $('.turn-label', turn).append(time);
    }
    time.textContent = `建议回答 ${formatRecommendedAnswerTime(seconds).replace(/^约\s*/, '')}`;
  }
  setCurrentQuestion(spokenText, event, { inferTiming: false });
}

function openTranscriptEditor(turn) {
  const form = $('.transcript-edit-form', turn);
  if (!form) return;
  const textarea = $('textarea', form);
  if (!textarea) return;
  textarea.value = $('.turn-bubble', turn)?.textContent?.trim() || '';
  form.classList.remove('is-hidden');
  $('.turn-edit-button', turn)?.classList.add('is-hidden');
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

function closeTranscriptEditor(turn) {
  $('.transcript-edit-form', turn)?.classList.add('is-hidden');
  $('.turn-edit-button', turn)?.classList.remove('is-hidden');
}

function submitTranscriptCorrection(form) {
  const turn = form.closest('.transcript-turn.is-candidate');
  const itemId = String(form.dataset.itemId || turn?.dataset.itemId || '');
  const textarea = $('textarea', form);
  const save = $('.save-transcript-button', form);
  const text = String(textarea?.value || '').trim();
  const originalText = String($('.turn-bubble', turn)?.textContent || '').trim();
  if (!itemId || !turn) {
    showToast('这条转写缺少语音编号，暂时无法修正。', 'error');
    return;
  }
  if (!text) {
    showToast('修正内容不能为空。', 'error');
    textarea?.focus();
    return;
  }
  if (text === originalText) {
    closeTranscriptEditor(turn);
    return;
  }
  if (!sendJson({
    type: 'candidate.transcript.correct',
    item_id: itemId,
    text,
    original_text: originalText,
  })) {
    showToast('连接尚未恢复，修正暂未保存。', 'error');
    return;
  }
  pendingTranscriptCorrections.set(itemId, { turn, text, originalText });
  save.disabled = true;
  save.textContent = '保存中…';
}

function applyTranscriptCorrection(event = {}) {
  const itemId = String(event.item_id || event.itemId || latestCandidateItemId || '');
  const pending = pendingTranscriptCorrections.get(itemId);
  const turn = pending?.turn || candidateTurns.get(itemId);
  if (!turn) return;
  const text = String(extractText(event) || pending?.text || '').trim();
  if (text) $('.turn-bubble', turn).textContent = text;
  const meta = $('.turn-transcript-meta', turn);
  if (meta) meta.textContent = '语音转写 · 已人工修正';
  const save = $('.save-transcript-button', turn);
  if (save) {
    save.disabled = false;
    save.textContent = '保存修正';
  }
  closeTranscriptEditor(turn);
  pendingTranscriptCorrections.delete(itemId);
  setLiveTranscript('final', '转写修正已保存', text, 1800);
  showToast('转写修正已保存，并将用于最终报告。', 'success');
}

function showPressureMoment(active = true) {
  if (getStressLevel() <= 0) return;
  clearTimeout(pressureMomentTimer);
  elements.stress.classList.toggle('is-active', active);
  if (active) {
    elements.stressLabel.textContent = `压力面 ${getStressLevel()}/3 · 情境进行中`;
    pressureMomentTimer = setTimeout(() => {
      elements.stress.classList.remove('is-active');
      elements.stressLabel.textContent = `压力面 ${getStressLevel()}/3 · 已启用`;
    }, 4500);
  }
}

function showInterrupt() {
  clearTimeout(interruptTimer);
  elements.interrupt.classList.add('is-visible');
  interruptTimer = setTimeout(() => elements.interrupt.classList.remove('is-visible'), 1800);
}

function maybeShowResumeMismatch(event = {}) {
  const warning = event.resume_selection_warning === true
    || String(event.reason || '').toLowerCase() === 'resume_mismatch';
  if (!warning || resumeMismatchPrompted) return;
  resumeMismatchPrompted = true;
  const reason = String(event.resume_mismatch_reason || '').trim();
  elements.resumeMismatchMessage.textContent = reason
    ? `${reason} 请确认是否选错了简历；你可以继续澄清，或退出并返回首页重新开始。`
    : '你的基础介绍与当前简历明显不一致。请确认是否选错了简历；你可以继续澄清，或退出并返回首页重新开始。';
  if (typeof elements.resumeMismatchDialog?.showModal === 'function') {
    elements.resumeMismatchDialog.showModal();
  } else if (window.confirm(`${elements.resumeMismatchMessage.textContent}\n\n确定退出并返回首页吗？`)) {
    exitForResumeMismatch();
  }
}

function exitForResumeMismatch() {
  intentionallyClosed = true;
  phase = 'ending';
  setCurrentSession(null);
  sendJson({ type: 'interview.end', reason: 'manual' });
  disableMicrophoneCapture({ explicit: true, notify: true });
  invalidateAudioPlayback();
  socket?.close();
  audio?.close();
  location.assign('/');
}

function handleCaptureState(event = {}) {
  const type = String(event.type || 'unknown');
  const state = String(event.state || 'unknown');
  console.info(`[voice.capture] type=${type} state=${state}`);
  if (type === 'audio-context' && ['suspended', 'interrupted'].includes(state)) {
    audio?.resume().catch(() => {});
  }
  if (['microphone-ready', 'microphone-unmuted'].includes(type)) {
    nearSilenceWarning = false;
    syncAudioUplink();
    if (isMicrophoneCaptureEnabled()) sendMicrophoneState(true);
    refreshMicrophoneDevices();
    return;
  }
  if (type === 'microphone-muted') {
    syncAudioUplink();
    sendMicrophoneState(false);
    if (phase === 'live' && Date.now() - lastCaptureWarningAt > 5000) {
      showToast('系统暂时停止了麦克风音轨，请检查设备和系统权限；也可继续打字。', 'error', 6500);
      lastCaptureWarningAt = Date.now();
    }
    return;
  }
  if (type === 'microphone-disabled') {
    syncAudioUplink();
    sendMicrophoneState(false);
    updateMicrophoneHealth();
    return;
  }
  if (type === 'microphone-switch-error') {
    updateMicrophoneHealth();
    return;
  }
  if (['microphone-ended', 'microphone-error'].includes(type)) {
    audio?.disableMicrophone();
    syncAudioUplink();
    sendMicrophoneState(false);
    elements.micToggle.setAttribute('aria-label', '重新启用麦克风');
    const now = Date.now();
    if (phase === 'live' && now - lastCaptureWarningAt > 5000) {
      showToast('麦克风输入已中断，请重新选择输入设备或检查系统权限；也可继续打字。', 'error', 6500);
      lastCaptureWarningAt = now;
    }
  }
}

function checkCaptureHealth() {
  if (phase !== 'live' || voiceMode === 'L3') return;
  if (['suspended', 'interrupted'].includes(audio?.context?.state)) audio.resume().catch(() => {});
  if (!audioUplinkReady) {
    updateMicrophoneHealth();
    return;
  }
  const baseline = lastAudioFrameAt || audioUplinkReadyAt;
  const now = Date.now();
  if (baseline && now - baseline > 3000) {
    if (now - lastCaptureWarningAt > 8000) {
      console.warn(`[voice.capture] stalled_ms=${now - baseline} context=${audio?.context?.state || 'missing'}`);
      showToast('暂未收到麦克风音频，请切换输入设备或检查系统权限。', 'error', 6500);
      lastCaptureWarningAt = now;
      audio?.resume().catch(() => {});
    }
    return;
  }

  // Do not warn while the candidate is expected to listen to the interviewer.
  if (!candidateAudioExpected || answerPending) {
    if (nearSilenceWarning) {
      nearSilenceWarning = false;
      updateMicrophoneHealth();
    }
    return;
  }

  const expectedSince = candidateAudioExpectedSince || now;
  const quietFor = now - Math.max(lastAudibleInputAt || 0, audioUplinkReadyAt || 0, expectedSince);
  const localQuiet = quietFor >= 8000;
  const serverQuiet = serverQuietSince > 0
    && now - Math.max(serverQuietSince, expectedSince) >= 8000
    && serverInputSignal === 'quiet'
    && now - lastServerInputLevelAt < 3500;
  if (!localQuiet && !serverQuiet) {
    if (nearSilenceWarning) {
      nearSilenceWarning = false;
      updateMicrophoneHealth();
    }
    return;
  }
  nearSilenceWarning = true;
  updateMicrophoneHealth();
  if (now - lastSilenceWarningAt <= 15_000) return;
  showToast(
    `“${selectedMicrophoneLabel()}”持续近静音：请切换设备或检查系统麦克风权限；也可打开“原始输入”。`,
    'error',
    8500,
  );
  lastSilenceWarningAt = now;
}

function clearPendingAudioFrame() {
  clearTimeout(pendingAudioFlushTimer);
  pendingAudioFlushTimer = 0;
  pendingAudioFrame = null;
}

function flushPendingAudioFrame() {
  pendingAudioFlushTimer = 0;
  if (!pendingAudioFrame || !audioUplinkReady || phase !== 'live'
      || socket?.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > MAX_AUDIO_BACKLOG_BYTES) {
    pendingAudioFlushTimer = setTimeout(flushPendingAudioFrame, 50);
    return;
  }
  const latest = pendingAudioFrame;
  pendingAudioFrame = null;
  socket.send(latest);
}

function sendRealtimeAudioFrame(buffer) {
  if (!audioUplinkReady || socket?.readyState !== WebSocket.OPEN || phase !== 'live') return;
  if (socket.bufferedAmount > MAX_AUDIO_BACKLOG_BYTES) {
    if (pendingAudioFrame) droppedAudioFrames += 1;
    // Keep at most the newest unsent 100 ms packet. Replacing this slot drops
    // stale speech instead of letting recognition drift seconds behind.
    pendingAudioFrame = buffer;
    if (!pendingAudioFlushTimer) pendingAudioFlushTimer = setTimeout(flushPendingAudioFrame, 50);
    if (Date.now() - lastAudioWarningAt > 5000) {
      console.warn(`[voice.uplink] dropped_frames=${droppedAudioFrames} buffered_bytes=${socket.bufferedAmount}`);
      showToast('网络有些拥堵，已丢弃陈旧语音以保持实时。', 'error');
      lastAudioWarningAt = Date.now();
    }
    return;
  }
  if (pendingAudioFrame) {
    const latest = pendingAudioFrame;
    pendingAudioFrame = null;
    socket.send(latest);
  }
  socket.send(buffer);
}

function createAudio() {
  return new AudioSession({
    onAudioFrame: (buffer) => {
      lastAudioFrameAt = Date.now();
      if (audioUplinkReady && answerState === 'answering') answerAudioFrames += 1;
      sendRealtimeAudioFrame(buffer);
    },
    onInputLevel: (value) => {
      inputRawLevel = Math.max(0, Number(value) || 0);
      inputLevel = Math.min(1, inputRawLevel * 4.2);
      renderInputMeter(inputRawLevel);
      if (inputRawLevel >= .0015) {
        lastAudibleInputAt = Date.now();
        if (nearSilenceWarning) {
          nearSilenceWarning = false;
          updateMicrophoneHealth();
        }
      }
    },
    onOutputLevel: (value) => {
      if (!canPlayInterviewerAudio()) return;
      outputLevel = Math.min(1, value * 3.4);
      if (outputLevel > .035 && phase === 'live') {
        expectCandidateAudio(false);
        setStage('speaking', '面试官正在提问', '你可以随时开口打断，播放会立即停止。');
      }
    },
    onPlaybackDrained: () => {
      outputLevel = 0;
      // A streaming response can briefly run out of queued chunks while the
      // provider is still generating.  Wait for the server's ordered end
      // marker before presenting the turn as finished.
    },
    onPlaybackMarkerDrained: (announcementId) => {
      if (announcementId) sendJson({ type: 'audio.playback.done', announcement_id: announcementId });
      outputLevel = 0;
      if (phase === 'live' && !answerPending) {
        if (answerState === 'answering') {
          expectCandidateAudio(true);
          setStage('listening', '正在听你回答', '讲完后请点击“结束回答”。');
        } else if (answerState === 'ready') {
          expectCandidateAudio(false);
          setStage('listening', '可以开始回答', '准备好后点击“开始回答”，计时会从点击时开始。');
        }
      }
    },
    onCaptureState: handleCaptureState,
    onDevicesChanged: refreshMicrophoneDevices,
  });
}

async function initializeAudio(capture = true, deviceId = '') {
  if (!audio) audio = createAudio();
  if (!audio.context) {
    try {
      return await audio.initialize({ capture, deviceId, raw: elements.rawCaptureToggle.checked });
    } catch (error) {
      const failedAudio = audio;
      audio = null;
      await failedAudio.close().catch(() => {});
      throw error;
    }
  }
  await audio.resume();
  const selectedDiffers = Boolean(deviceId && deviceId !== audio.selectedDeviceId);
  if (capture && (!audio.hasMicrophone || selectedDiffers)) {
    try {
      await audio.enableMicrophone(deviceId, {
        force: selectedDiffers,
        raw: elements.rawCaptureToggle.checked,
      });
      return { microphone: true, error: null };
    } catch (error) {
      return { microphone: false, error };
    }
  }
  return { microphone: audio.hasMicrophone, error: null };
}

function sendJson(payload) {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(payload));
  return true;
}

function setLive() {
  if (phase === 'ended' || phase === 'ending') return;
  phase = 'live';
  elements.readyPanel.classList.add('is-hidden');
  elements.endButton.disabled = false;
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  elements.micToggle.classList.toggle('is-hidden', voiceMode === 'L3');
  elements.interviewerVoiceToggle.classList.toggle('is-hidden', voiceMode === 'L3');
  updateHintAvailability();
  setConnection('live', '面试进行中');
  expectCandidateAudio(false);
  setStage(voiceMode === 'L3' ? 'thinking' : 'listening', '面试进行中', '请先听完题目，再点击“开始回答”。');
  syncAudioUplink();
  const restoredLocalState = ['answering', 'sealing'].includes(answerState)
    ? answerState
    : (questionReady && currentQuestion ? 'ready' : 'idle');
  setAnswerState(restoredLocalState);
}

function invalidateAudioPlayback() {
  audioEpoch += 1;
  audioFileQueue = Promise.resolve();
  audio?.clearPlayback();
  outputLevel = 0;
}

function renderInterviewerVoicePreference() {
  const enabled = interviewerVoiceEnabled && voiceMode !== 'L3';
  elements.interviewerVoiceToggle.classList.toggle('is-off', !interviewerVoiceEnabled);
  elements.interviewerVoiceToggle.classList.toggle('is-hidden', voiceMode === 'L3' || phase === 'preparing');
  elements.interviewerVoiceToggle.setAttribute('aria-pressed', String(interviewerVoiceEnabled));
  elements.interviewerVoiceToggle.setAttribute('aria-label', enabled ? '关闭面试官语音' : '开启面试官语音');
  elements.interviewerVoiceToggle.title = enabled ? '关闭面试官语音' : '开启面试官语音';
}

function canPlayInterviewerAudio() {
  return interviewerVoiceEnabled
    && !interviewerAudioSuppressedUntilStreamEnd
    && voiceMode !== 'L3';
}

function noteInterviewerAudioStream() {
  interviewerAudioStreamActive = true;
  if (!interviewerVoiceEnabled) interviewerAudioSuppressedUntilStreamEnd = true;
}

function toggleInterviewerVoice() {
  if (voiceMode === 'L3') return;
  interviewerVoiceEnabled = !interviewerVoiceEnabled;
  try {
    localStorage.setItem(INTERVIEWER_VOICE_KEY, String(interviewerVoiceEnabled));
  } catch {
    // Storage can be unavailable in a private browsing context; the page-level choice still works.
  }
  if (!interviewerVoiceEnabled) {
    if (interviewerAudioStreamActive) interviewerAudioSuppressedUntilStreamEnd = true;
    invalidateAudioPlayback();
  }
  renderInterviewerVoicePreference();
  showToast(
    interviewerVoiceEnabled ? '面试官语音已开启，将从后续内容恢复播放。' : '面试官语音已关闭，题目文字和麦克风不受影响。',
    'info',
    3600,
  );
}

function setAnswerPending(pending) {
  answerPending = Boolean(pending);
  if (answerPending) expectCandidateAudio(false);
  const disabled = answerPending || phase !== 'live' || answerState !== 'answering';
  elements.messageInput.disabled = disabled;
  elements.send.disabled = disabled;
  elements.startAnswer.disabled = answerPending || phase !== 'live' || answerState !== 'ready';
  elements.endAnswer.disabled = answerPending || phase !== 'live' || answerState !== 'answering';
  updateHintAvailability();
  if (!disabled && voiceMode === 'L3') elements.messageInput.focus();
}

async function handleAudioFile(event, epoch) {
  if (epoch !== audioEpoch || !audio || !canPlayInterviewerAudio()) return;
  try {
    const encoded = base64ToArrayBuffer(event.audio || event.data || '');
    if (encoded.byteLength && epoch === audioEpoch) await audio.enqueueEncoded(encoded);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function handleEnded(event = {}) {
  if (phase === 'ended') return;
  phase = 'ended';
  expectCandidateAudio(false);
  intentionallyClosed = true;
  clearTimeout(reconnectTimer);
  clearInterval(heartbeatTimer);
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  elements.endButton.disabled = true;
  elements.hintButton.disabled = true;
  clearInterval(answerClockTimer);
  answerClockTimer = 0;
  elements.answerControl.classList.add('is-hidden');
  providerAudioReady = false;
  disableMicrophoneCapture({ explicit: true, notify: true });
  audio?.clearPlayback();
  const endedAudio = audio;
  audio = null;
  endedAudio?.close();
  const reason = event.reason || event.end_reason || '';
  if (/poor|fail|early|崩|提前/i.test(reason)) {
    elements.endTitle.textContent = '今天的面试就到这里';
    elements.endMessage.textContent = event.message || '本场已提前结束，正在整理你最需要补齐的部分…';
  } else if (reason === 'time') {
    elements.endTitle.textContent = '本场面试时间到';
    elements.endMessage.textContent = '正在整理逐题反馈报告…';
  } else {
    elements.endTitle.textContent = '本场面试已结束';
    elements.endMessage.textContent = event.message || '正在整理逐题反馈报告…';
  }
  elements.endOverlay.classList.remove('is-hidden');
  setConnection('', '已结束');
  const delay = event.report_ready || event.report ? 650 : 1400;
  setTimeout(() => location.assign(`/report?session=${encodeURIComponent(sessionId)}`), delay);
}

function handleServerEvent(event) {
  switch (event.type) {
    case 'session.ready':
      reconnectAttempts = 0;
      answerPending = false;
      expectCandidateAudio(false);
      // Provider preflight and fallback selection happen after session.ready.
      // Keep microphone frames local until the final mode.changed event.
      providerAudioReady = false;
      syncAudioUplink();
      if (event.mode || event.voice_mode) setMode(event.mode || event.voice_mode, true);
      if (event.session && typeof event.session === 'object') interview = { ...interview, ...event.session };
      syncTimer(event.session || event);
      setLive();
      {
        const restoredAnswerState = String(event.answer_state || event.session?.answer_state || '').toLowerCase();
        if (['answering', 'sealing'].includes(restoredAnswerState)) {
          setAnswerState(restoredAnswerState, { elapsedMs: event.elapsed_ms ?? event.session?.answer_elapsed_ms });
        }
      }
      break;
    case 'candidate.transcript.partial':
      if (!['answering', 'sealing'].includes(answerState)) break;
      expectCandidateAudio(answerState === 'answering');
      updateHintAvailability();
      updateCandidateTranscript(event);
      setStage('listening', '正在听你回答', '把关键链路、指标口径和取舍说具体。');
      break;
    case 'candidate.transcript.done':
      if (!['answering', 'sealing'].includes(answerState)) break;
      finalizeCandidateTranscript(event);
      lastTyped = null;
      if (answerState === 'sealing') {
        setAnswerPending(true);
        setStage('thinking', '面试官正在思考', '回答结束后不会即时点评。');
      } else {
        setStage('listening', '正在听你回答', '讲完后请点击“结束回答”。');
      }
      break;
    case 'candidate.transcript.corrected':
      applyTranscriptCorrection(event);
      break;
    case 'candidate.transcript.failed':
      discardPartialTurn('candidate');
      resetCandidateTranscript();
      setLiveTranscript('error', '这次没有识别清楚', '请再说一次，或改用右侧文字输入', 3200);
      setAnswerPending(false);
      questionReady = Boolean(currentQuestion);
      setAnswerState(questionReady ? 'ready' : 'idle');
      updateHintAvailability();
      setStage('listening', '请重新开始回答', '本轮转写失败；点击“开始回答”重试，也可以改用文字。');
      break;
    case 'interviewer.text.partial':
      {
        const interjection = isPressureInterjection(event);
        if (!interjection) {
          if (voiceMode !== 'L3') noteInterviewerAudioStream();
          expectCandidateAudio(false);
          questionReady = false;
          answerPending = true;
          setAnswerState('idle', { resetElapsed: true });
          updateHintAvailability();
        }
        const turn = renderTurn('interviewer', extractText(event), {
          partial: true,
          append: event.text === undefined && event.transcript === undefined,
        });
        if (interjection) {
          markPressureInterjection(turn);
          showPressureMoment();
          setStage('speaking', '压力情境进行中', '先给结论，再补充依据与边界。');
        } else {
          setStage('speaking', voiceMode === 'L3' ? '面试官正在追问' : '面试官正在提问', '注意听问题中的限定条件。');
        }
      }
      break;
    case 'interviewer.text.done':
      {
        maybeShowResumeMismatch(event);
        const interjection = isPressureInterjection(event);
        const messageId = interviewerMessageId(event);
        const turn = renderTurn('interviewer', extractText(event), {
          suppressDuplicate: true,
          itemId: messageId,
          recommendedSeconds: interjection ? 0 : recommendedAnswerSeconds(event, extractText(event)),
        });
        const finalized = elements.transcript.querySelectorAll('.transcript-turn.is-interviewer:not(.is-partial)');
        const resolvedTurn = turn || finalized[finalized.length - 1];
        if (messageId && resolvedTurn) interviewerTurns.set(messageId, resolvedTurn);
        if (interjection) {
          markPressureInterjection(resolvedTurn);
          showPressureMoment();
          setStage('speaking', '压力情境进行中', '先给结论，再补充依据与边界。');
        } else {
          if (voiceMode !== 'L3') noteInterviewerAudioStream();
          if (event.pressure_action && String(event.pressure_action).toLowerCase() !== 'none') showPressureMoment();
          const hasQuestion = setCurrentQuestion(extractText(event), event);
          answerPending = false;
          if (!hasQuestion) questionReady = false;
          setAnswerState(hasQuestion ? 'ready' : 'idle', { resetElapsed: true });
          if (hasQuestion && (voiceMode === 'L3' || !interviewerVoiceEnabled)) {
            setStage('listening', '可以开始回答', '准备好后点击“开始回答”，计时会从点击时开始。');
          }
        }
      }
      break;
    case 'interviewer.text.corrected':
    case 'interviewer.text.sync':
      syncInterviewerTranscript(event);
      break;
    case 'interviewer.audio.synced':
      syncInterviewerTranscript(event, { updateQuestion: false });
      break;
    case 'input.speech_started':
      {
        if (answerState !== 'answering') break;
        expectCandidateAudio(true);
        const interruptedPlayback = outputLevel > .02;
        invalidateAudioPlayback();
        beginCandidateSpeech();
        if (interruptedPlayback) showInterrupt();
        setStage(
          'listening',
          '正在听你回答',
          interruptedPlayback ? '已停止播放面试官语音。' : '请把关键链路、指标口径和取舍说具体。',
        );
      }
      break;
    case 'audio.input.level': {
      const signal = String(event.signal || '').toLowerCase();
      serverInputSignal = signal === 'active' ? 'active' : signal === 'quiet' ? 'quiet' : 'unknown';
      lastServerInputLevelAt = Date.now();
      if (serverInputSignal === 'active') {
        serverQuietSince = 0;
        lastAudibleInputAt = lastServerInputLevelAt;
        if (nearSilenceWarning) {
          nearSilenceWarning = false;
          updateMicrophoneHealth();
        }
      } else if (serverInputSignal === 'quiet' && !serverQuietSince) {
        serverQuietSince = lastServerInputLevelAt;
      } else if (serverInputSignal === 'unknown') {
        serverQuietSince = 0;
      }
      break;
    }
    case 'pressure.interrupt':
      expectCandidateAudio(false);
      maybeShowResumeMismatch(event);
      showInterrupt();
      showPressureMoment();
      setStage('speaking', '压力情境进行中', '先给结论，再补充依据与边界。');
      break;
    case 'audio.chunk': {
      noteInterviewerAudioStream();
      if (!audio || !canPlayInterviewerAudio()) break;
      const format = String(event.format || 'pcm_s16le').toLowerCase();
      if (!['pcm_s16le', 'pcm16', 'pcm'].includes(format)) {
        showToast(`暂不支持音频格式 ${format}`, 'error');
        break;
      }
      const pcm = base64ToArrayBuffer(event.audio || event.data || '');
      audio.enqueuePCM(pcm, Number(event.sample_rate) || 16000);
      break;
    }
    case 'audio.file': {
      noteInterviewerAudioStream();
      const epoch = audioEpoch;
      audioFileQueue = audioFileQueue.then(() => handleAudioFile(event, epoch));
      break;
    }
    case 'audio.clear':
      invalidateAudioPlayback();
      interviewerAudioStreamActive = false;
      interviewerAudioSuppressedUntilStreamEnd = false;
      break;
    case 'audio.stream.done': {
      const shouldPlayMarker = Boolean(audio && canPlayInterviewerAudio());
      interviewerAudioStreamActive = false;
      interviewerAudioSuppressedUntilStreamEnd = false;
      audioFileQueue.finally(() => {
        if (shouldPlayMarker && audio) audio.markPlaybackEnd(event.announcement_id);
        else sendJson({ type: 'audio.playback.done', announcement_id: event.announcement_id });
      });
      break;
    }
    case 'timer.sync':
      syncTimer(event);
      break;
    case 'mode.changed':
      clearPendingAudioFrame();
      setMode(event.mode || event.voice_mode, true);
      providerAudioReady = voiceMode !== 'L3';
      syncAudioUplink();
      if (audioUplinkReady) console.info(`[voice.uplink] ready mode=${voiceMode}`);
      break;
    case 'interviewer.state':
      if (event.state === 'thinking') {
        expectCandidateAudio(false);
        setStage('thinking', '面试官正在思考', '回答结束后不会即时点评。');
      } else if (event.state === 'silent') {
        expectCandidateAudio(false);
        showPressureMoment();
        setStage('thinking', '压力情境进行中', '保持冷静，检查结论、依据和边界是否完整。');
      }
      else if (event.state === 'listening') {
        setAnswerPending(false);
        if (currentQuestion && questionReady && answerState === 'idle') setAnswerState('ready');
        if (answerState === 'answering') {
          setStage('listening', '正在听你回答', '讲完后请点击“结束回答”。');
        } else if (answerState === 'ready') {
          setStage('listening', '可以开始回答', '准备好后点击“开始回答”，计时会从点击时开始。');
        }
      }
      break;
    case 'answer.state.changed': {
      const state = String(event.state || '').toLowerCase();
      if (state === 'answering') {
        answerStartAwaitingAck = false;
        answerPending = false;
        if (answerState !== 'sealing') {
          setAnswerState('answering', { elapsedMs: event.elapsed_ms });
        }
      } else if (state === 'sealing') {
        answerStartAwaitingAck = false;
        answerPending = true;
        setAnswerState('sealing', { elapsedMs: event.elapsed_ms });
      } else if (state === 'idle') {
        answerPending = false;
        setAnswerState(
          questionReady && currentQuestion ? 'ready' : 'idle',
          { elapsedMs: event.elapsed_ms },
        );
      }
      break;
    }
    case 'interview.ended':
      handleEnded(event);
      break;
    case 'report.ready':
      if (phase !== 'ended') handleEnded({ ...event, report_ready: true });
      else location.assign(`/report?session=${encodeURIComponent(sessionId)}`);
      break;
    case 'error':
      showToast(event.message || event.error || '面试服务发生错误。', 'error', 5500);
      if (answerStartAwaitingAck && event.recoverable !== false) {
        answerStartAwaitingAck = false;
        answerPending = false;
        questionReady = Boolean(currentQuestion);
        setAnswerState(questionReady ? 'ready' : 'idle', { resetElapsed: true });
        setStage('listening', '可以重新开始回答', '服务端没有接受刚才的开始操作，请再试一次。');
      }
      if (/TRANSCRIPT|CORRECTION|TURN_|REPORT_ALREADY/i.test(String(event.code || ''))) {
        const itemId = String(event.item_id || event.itemId || '');
        const pendingEntry = itemId && pendingTranscriptCorrections.has(itemId)
          ? [itemId, pendingTranscriptCorrections.get(itemId)]
          : [...pendingTranscriptCorrections.entries()].at(-1);
        const pending = pendingEntry?.[1];
        const save = pending?.turn ? $('.save-transcript-button', pending.turn) : null;
        if (save) {
          save.disabled = false;
          save.textContent = '保存修正';
        }
        if (pendingEntry?.[0]) pendingTranscriptCorrections.delete(pendingEntry[0]);
      }
      if (['EMPTY_ANSWER', 'ANSWER_FAILED', 'ANSWER_NOT_STARTED', 'ANSWER_TRANSCRIPT_EMPTY', 'INTERVIEW_TIMEOUT'].includes(event.code)) {
        setAnswerPending(false);
        if (phase === 'live' && currentQuestion) setAnswerState('ready');
      }
      if (event.fatal) {
        intentionallyClosed = true;
        phase = 'error';
        disableMicrophoneCapture({ explicit: true, notify: true });
        setConnection('error', '连接错误');
        setStage('', '面试暂时中断', '请刷新页面尝试重新加入。');
      }
      break;
    case 'pong':
      break;
    default:
      break;
  }
}

function scheduleReconnect() {
  if (intentionallyClosed || phase === 'ended' || reconnectAttempts >= 3) {
    if (!intentionallyClosed && phase !== 'ended') {
      disableMicrophoneCapture({ explicit: true, notify: false });
      setConnection('error', '连接已断开');
      showToast('无法恢复面试连接。刷新页面可尝试重新加入，已完成内容不会丢失。', 'error', 6500);
    }
    return;
  }
  const delays = [900, 1800, 3500];
  const delay = delays[reconnectAttempts];
  reconnectAttempts += 1;
  if (phase === 'live') phase = 'connecting';
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  setConnection('warning', `正在重连 ${reconnectAttempts}/3`);
  reconnectTimer = setTimeout(connectSocket, delay);
}

function connectSocket() {
  clearTimeout(reconnectTimer);
  expectCandidateAudio(false);
  providerAudioReady = false;
  syncAudioUplink();
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${scheme}//${location.host}/ws/interviews/${encodeURIComponent(sessionId)}`);
  lastMicrophoneStateSent = null;
  socket.binaryType = 'arraybuffer';
  setConnection('warning', reconnectAttempts ? '正在恢复连接' : '正在连接');

  socket.addEventListener('open', () => {
    const microphoneEnabled = isMicrophoneCaptureEnabled();
    sendJson({
      type: 'client.ready',
      client_id: getClientId(),
      mode: voiceMode,
      audio: voiceMode === 'L3' ? null : {
        format: 'pcm_s16le', sample_rate: 16000, channels: 1, microphone: microphoneEnabled,
      },
    });
    sendMicrophoneState(microphoneEnabled, { force: true });
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(() => sendJson({ type: 'ping', timestamp: Date.now() }), 18_000);
  });
  socket.addEventListener('message', async ({ data }) => {
    if (typeof data === 'string') {
      try {
        handleServerEvent(JSON.parse(data));
      } catch {
        showToast('收到无法解析的服务端消息。', 'error');
      }
    } else if (data instanceof ArrayBuffer && audio && voiceMode !== 'L3') {
      noteInterviewerAudioStream();
      if (canPlayInterviewerAudio()) audio.enqueuePCM(data, 16000);
    } else if (data instanceof Blob && audio && voiceMode !== 'L3') {
      noteInterviewerAudioStream();
      if (canPlayInterviewerAudio()) audio.enqueuePCM(await data.arrayBuffer(), 16000);
    }
  });
  socket.addEventListener('error', () => setConnection('warning', '连接波动'));
  socket.addEventListener('close', ({ code }) => {
    clearInterval(heartbeatTimer);
    providerAudioReady = false;
    syncAudioUplink();
    invalidateAudioPlayback();
    if ([4400, 4403, 4404].includes(code)) {
      intentionallyClosed = true;
      phase = 'error';
      disableMicrophoneCapture({ explicit: true, notify: false });
      setConnection('error', '面试不可用');
      return;
    }
    if (!intentionallyClosed && phase !== 'ended') scheduleReconnect();
  });
}

async function joinInterview() {
  if (!sessionId || phase === 'connecting' || phase === 'live') return;
  phase = 'connecting';
  elements.join.disabled = true;
  $('span', elements.join).textContent = voiceMode === 'L3' ? '正在连接文字面试…' : '正在启用麦克风…';
  setConnection('warning', '正在进入面试');
  if (voiceMode !== 'L3') {
    try {
      const result = await initializeAudio(true, elements.microphoneSelect.value);
      if (!result.microphone) {
        showToast('没有取得麦克风权限；你仍可听取问题并用文字作答。', 'error', 6000);
        elements.permissionNote.textContent = '麦克风未启用，可使用右侧文字输入继续。';
        elements.micToggle.classList.remove('is-hidden');
        elements.micToggle.classList.add('is-muted');
        elements.micToggle.setAttribute('aria-label', '重新启用麦克风');
      } else {
        elements.rawCaptureToggle.checked = Boolean(audio?.rawCapture);
        await refreshMicrophoneDevices();
      }
    } catch (error) {
      audio = null;
      showToast(`${error.message} 已自动保留文字输入。`, 'error', 6500);
    }
  }
  updateMicrophoneHealth();
  connectSocket();
}

async function finishInterview(reason = 'manual') {
  if (localFinishSent || ['ending', 'ended'].includes(phase)) return;
  if (answerState === 'ready' && elements.messageInput.value.trim()) {
    startCurrentAnswer();
    if (answerState !== 'answering') return;
  }
  if (answerState === 'answering') {
    const sealed = finishCurrentAnswer({ allowEmpty: true });
    if (!sealed) return;
  }
  localFinishSent = true;
  phase = 'ending';
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  elements.endButton.disabled = true;
  elements.hintButton.disabled = true;
  clearInterval(answerClockTimer);
  answerClockTimer = 0;
  elements.answerControl.classList.add('is-hidden');
  disableMicrophoneCapture({ explicit: true, notify: true });
  invalidateAudioPlayback();
  setConnection('warning', '正在结束');
  elements.endTitle.textContent = reason === 'time' ? '本场面试时间到' : '正在结束本场面试';
  elements.endMessage.textContent = '正在整理逐题反馈报告…';
  elements.endOverlay.classList.remove('is-hidden');
  const finishViaRest = async () => {
    const result = await apiFetch(`/api/interviews/${encodeURIComponent(sessionId)}/finish`, {
      method: 'POST',
      timeout: 65_000,
      json: { reason },
    });
    handleEnded({ reason, report_ready: Boolean(result?.report_ready || result?.report || result?.status === 'completed') });
  };

  if (sendJson({ type: 'interview.end', reason })) {
    setTimeout(async () => {
      if (phase !== 'ending') return;
      try {
        await finishViaRest();
      } catch {
        try { await finishViaRest(); } catch (error) {
          showToast(error.message, 'error');
          setTimeout(() => location.assign(`/report?session=${encodeURIComponent(sessionId)}`), 1600);
        }
      }
    }, 45_000);
    return;
  }
  try {
    await finishViaRest();
  } catch (error) {
    showToast(error.message, 'error');
    setTimeout(() => location.assign(`/report?session=${encodeURIComponent(sessionId)}`), 1600);
  }
}

function submitText(event) {
  event.preventDefault();
  if (phase !== 'live' || answerPending || answerState !== 'answering') return;
  const text = elements.messageInput.value.trim();
  if (text) lastTyped = { text, at: Date.now() };
  finishCurrentAnswer();
}

function submitUnknown() {
  if (phase !== 'live' || answerPending || !['ready', 'answering'].includes(answerState)) return;
  if (answerState === 'ready') startCurrentAnswer();
  if (answerState !== 'answering') return;
  elements.messageInput.value = interview?.language_mode === 'en'
    ? "I don't know, please move on"
    : '我不知道，请继续下一题';
  finishCurrentAnswer();
}

async function toggleMicrophone() {
  if (voiceMode === 'L3' || microphoneSwitching) return;
  if (!audio || !audio.hasMicrophone) {
    microphoneSwitching = true;
    updateMicrophoneHealth();
    try {
      const result = await initializeAudio(true, elements.microphoneSelect.value);
      if (!result.microphone) throw result.error || new Error('未取得麦克风权限');
      microphoneExplicitlyDisabled = false;
      audio.setMuted(false);
      syncAudioUplink();
      sendMicrophoneState(true);
      await refreshMicrophoneDevices();
      elements.micToggle.classList.remove('is-muted');
      elements.micToggle.setAttribute('aria-label', '关闭麦克风');
      elements.micToggle.title = '关闭麦克风';
      showToast('麦克风已启用。', 'success');
    } catch (error) {
      showToast(error.message || '无法启用麦克风。', 'error');
    } finally {
      microphoneSwitching = false;
      updateMicrophoneHealth();
    }
    return;
  }
  disableMicrophoneCapture({ explicit: true, notify: true });
  elements.micToggle.classList.add('is-muted');
  elements.micToggle.setAttribute('aria-label', '开启麦克风');
  elements.micToggle.title = '开启麦克风';
  showToast('麦克风已关闭并释放，可继续打字回答。', 'info');
}

function drawWaveform() {
  const canvas = elements.waveform;
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  if (canvas.width !== Math.round(rect.width * ratio) || canvas.height !== Math.round(rect.height * ratio)) {
    canvas.width = Math.max(1, Math.round(rect.width * ratio));
    canvas.height = Math.max(1, Math.round(rect.height * ratio));
  }
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const activeLevel = Math.max(inputLevel, outputLevel);
  const bars = Math.max(28, Math.floor(rect.width / 8));
  const center = canvas.height / 2;
  const spacing = canvas.width / bars;
  const now = performance.now() / 280;
  for (let index = 0; index < bars; index += 1) {
    const envelope = .28 + .72 * Math.sin(Math.PI * (index + .5) / bars);
    const idle = phase === 'live' ? .04 : .018;
    const activity = idle + activeLevel * (.35 + .65 * Math.abs(Math.sin(now + index * .43)));
    const height = Math.max(2 * ratio, canvas.height * envelope * activity);
    context.fillStyle = `rgba(${outputLevel > inputLevel ? '100, 167, 239' : '93, 213, 180'}, ${.28 + envelope * .55})`;
    context.fillRect(index * spacing + spacing * .25, center - height / 2, Math.max(1, spacing * .42), height);
  }
  inputLevel *= .91;
  outputLevel *= .93;
  requestAnimationFrame(drawWaveform);
}

async function initialize() {
  drawWaveform();
  timerInterval = setInterval(renderTimer, 250);
  captureWatchdogTimer = setInterval(checkCaptureHealth, 1000);
  if (!sessionId) {
    phase = 'error';
    elements.join.disabled = true;
    elements.readyDescription.textContent = '没有找到面试编号，请返回首页重新创建。';
    $('span', elements.join).textContent = '无法进入面试';
    setConnection('error', '缺少面试编号');
    return;
  }
  const [configResult, interviewResult] = await Promise.allSettled([
    apiFetch('/api/config', { timeout: 8_000 }),
    apiFetch(`/api/interviews/${encodeURIComponent(sessionId)}`, { timeout: 15_000 }),
  ]);
  serverConfig = configResult.status === 'fulfilled' ? configResult.value : {};
  if (interviewResult.status === 'fulfilled') {
    interview = interviewResult.value?.interview || interviewResult.value?.session || interviewResult.value;
  } else if (storedSession && String(storedSession.id || storedSession.session_id) === String(sessionId)) {
    interview = storedSession;
    showToast('暂时无法读取面试状态，仍可尝试重新连接。', 'error');
  } else {
    phase = 'error';
    elements.join.disabled = true;
    elements.readyDescription.textContent = interviewResult.reason?.message || '无法读取这场面试。';
    setConnection('error', '读取失败');
    return;
  }

  (interview?.hint_events || []).forEach((event) => {
    const question = String(event?.question || '').trim();
    const level = Math.max(1, Math.min(2, Number(event?.level) || 1));
    if (question) hintedQuestions.set(question, Math.max(level, hintedQuestions.get(question) || 0));
  });

  const company = interview?.company || storedSession?.company || 'bytedance';
  const specialization = String(interview?.specialization || storedSession?.specialization || '通用后端').trim();
  const interviewType = (interview?.interview_type || storedSession?.interview_type) === 'technical_hr'
    ? '技术 / 综合面'
    : '技术面';
  const stressLevel = getStressLevel();
  elements.company.textContent = `${companyLabel(company)} · ${specialization} · ${interviewType}`;
  elements.stress.classList.toggle('is-hidden', stressLevel === 0);
  elements.stress.dataset.level = String(stressLevel);
  elements.stress.setAttribute('aria-label', stressLevel > 0 ? `压力面强度 ${stressLevel}/3，已启用` : '无压力面');
  elements.stressLabel.textContent = stressLevel > 0 ? `压力面 ${stressLevel}/3 · 已启用` : '';
  setMode(interview?.voice_mode || serverConfig?.voice_mode || serverConfig?.mode || storedSession?.voice_mode || 'L3');
  syncTimer(interview || {});
  const status = String(interview?.status || interview?.state || '').toLowerCase();
  if (['ended', 'reporting', 'reported', 'completed', 'finished', 'report_ready'].includes(status)) {
    location.replace(`/report?session=${encodeURIComponent(sessionId)}`);
    return;
  }
  phase = 'ready';
  if (voiceMode !== 'L3') await refreshMicrophoneDevices();
  renderInterviewerVoicePreference();
  $('span', elements.join).textContent = ['active', 'live', 'running'].includes(status) ? '重新加入面试' : (voiceMode === 'L3' ? '开始文字面试' : '开启麦克风并开始');
  elements.readyDescription.textContent = ['active', 'live', 'running'].includes(status) ? '这场面试仍在进行，可以继续加入。' : '点击后将建立面试连接。';
  elements.join.disabled = false;
  setConnection('', '等待进入');
}

elements.join.addEventListener('click', joinInterview);
elements.startAnswer.addEventListener('click', startCurrentAnswer);
elements.endAnswer.addEventListener('click', () => finishCurrentAnswer());
elements.messageForm.addEventListener('submit', submitText);
elements.messageInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.messageForm.requestSubmit();
  }
});
elements.messageInput.addEventListener('input', () => {
  elements.messageInput.rows = 1;
  elements.messageInput.rows = Math.min(5, Math.max(1, Math.ceil(elements.messageInput.scrollHeight / 20)));
});
elements.scrollLatest.addEventListener('click', () => scrollTranscript(true));
elements.transcript.addEventListener('scroll', () => {
  if (isNearTranscriptBottom()) elements.scrollLatest.classList.remove('is-visible');
});
elements.transcript.addEventListener('click', (event) => {
  const turn = event.target.closest('.transcript-turn.is-candidate');
  if (!turn) return;
  if (event.target.closest('[data-action="edit-transcript"]')) openTranscriptEditor(turn);
  if (event.target.closest('[data-action="cancel-transcript-edit"]')) closeTranscriptEditor(turn);
});
elements.transcript.addEventListener('submit', (event) => {
  const form = event.target.closest('.transcript-edit-form');
  if (!form) return;
  event.preventDefault();
  submitTranscriptCorrection(form);
});
elements.micToggle.addEventListener('click', toggleMicrophone);
elements.interviewerVoiceToggle.addEventListener('click', toggleInterviewerVoice);
elements.microphoneSelect.addEventListener('change', switchMicrophoneDevice);
elements.rawCaptureToggle.addEventListener('change', switchMicrophoneDevice);
elements.hintButton.addEventListener('click', requestHint);
elements.unknownButton.addEventListener('click', submitUnknown);
$('#closeHint').addEventListener('click', () => elements.hintPanel.classList.add('is-hidden'));
elements.endButton.addEventListener('click', () => {
  if (typeof elements.endDialog.showModal === 'function') elements.endDialog.showModal();
  else if (window.confirm('确定提前结束并生成报告吗？')) finishInterview('manual');
});
$('#confirmEnd').addEventListener('click', (event) => {
  event.preventDefault();
  elements.endDialog.close();
  finishInterview('manual');
});
$('#continueWithResume').addEventListener('click', () => {
  elements.resumeMismatchDialog?.close();
  showToast('请在下一轮先澄清与当前简历不一致的地方。', 'success', 4200);
});
$('#exitForResume').addEventListener('click', (event) => {
  event.preventDefault();
  elements.resumeMismatchDialog?.close();
  exitForResumeMismatch();
});

window.addEventListener('beforeunload', (event) => {
  if (phase !== 'live') return;
  event.preventDefault();
  event.returnValue = '';
});
window.addEventListener('pagehide', () => {
  intentionallyClosed = true;
  clearInterval(heartbeatTimer);
  clearInterval(timerInterval);
  clearInterval(captureWatchdogTimer);
  clearInterval(answerClockTimer);
  clearTimeout(liveTranscriptTimer);
  disableMicrophoneCapture({ explicit: true, notify: true });
  socket?.close();
  audio?.close();
});

initialize();
