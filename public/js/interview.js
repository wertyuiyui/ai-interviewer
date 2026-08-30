import {
  $, apiFetch, base64ToArrayBuffer, companyLabel, formatSeconds,
  getClientId, getCurrentSession, modeLabel, normalizeMode, showToast,
} from './common.js';
import { AudioSession } from './audio-session.js';

const query = new URLSearchParams(location.search);
const storedSession = getCurrentSession();
const sessionId = query.get('session') || storedSession?.id || storedSession?.session_id || '';

const elements = {
  avatar: $('#avatarScene'),
  company: $('#companyChip'),
  connection: $('#connectionState'),
  endButton: $('#endButton'),
  endDialog: $('#endDialog'),
  endDialogMessage: $('#endDialogMessage'),
  endDialogTitle: $('#endDialogTitle'),
  endMessage: $('#endMessage'),
  endOverlay: $('#endOverlay'),
  endTitle: $('#endTitle'),
  interrupt: $('#interruptLabel'),
  join: $('#joinButton'),
  messageForm: $('#messageForm'),
  messageInput: $('#messageInput'),
  micToggle: $('#micToggle'),
  mode: $('#modePill'),
  permissionNote: $('#permissionNote'),
  readyDescription: $('#readyDescription'),
  readyPanel: $('#readyPanel'),
  scrollLatest: $('#scrollLatest'),
  send: $('#sendButton'),
  stageHint: $('#stageHint'),
  stageTitle: $('#stageTitle'),
  stress: $('#stressChip'),
  stressLabel: $('#stressLabel'),
  timer: $('#timer'),
  transcript: $('#transcriptList'),
  transcriptPlaceholder: $('#transcriptPlaceholder'),
  waveform: $('#waveform'),
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
const partialTurns = new Map();

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

function setMode(mode, notify = false) {
  const previous = voiceMode;
  voiceMode = normalizeMode(mode);
  $('span', elements.mode).textContent = modeLabel(voiceMode);
  const textOnly = voiceMode === 'L3';
  elements.micToggle.classList.toggle('is-hidden', textOnly || phase === 'preparing');
  elements.permissionNote.textContent = textOnly
    ? '本场为纯文字模式，不会请求麦克风权限。'
    : '语音模式会请求麦克风权限，建议佩戴耳机。';
  elements.messageInput.placeholder = textOnly ? '输入你的回答…' : '也可以在这里打字回答…';
  if (textOnly && audio) {
    const previousAudio = audio;
    invalidateAudioPlayback();
    audio = null;
    previousAudio.setMuted(true);
    previousAudio.close();
  }
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

function createTurn(role, text, partial = false) {
  const turn = document.createElement('article');
  turn.className = `transcript-turn is-${role}${partial ? ' is-partial' : ''}`;
  const label = document.createElement('div');
  label.className = 'turn-label';
  const icon = document.createElement('i');
  icon.textContent = role === 'candidate' ? '我' : 'AI';
  const labelText = document.createElement('span');
  labelText.textContent = role === 'candidate' ? '我的回答' : '面试官';
  label.append(icon, labelText);
  const bubble = document.createElement('div');
  bubble.className = 'turn-bubble';
  bubble.textContent = text;
  turn.append(label, bubble);
  return turn;
}

function renderTurn(role, value, { partial = false, append = false, suppressDuplicate = false } = {}) {
  const text = String(value || '').trim();
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
      const turn = createTurn(role, text, true);
      partialTurns.set(role, turn);
      elements.transcript.append(turn);
    }
  } else if (existing) {
    $('.turn-bubble', existing).textContent = text || $('.turn-bubble', existing).textContent;
    existing.classList.remove('is-partial');
    partialTurns.delete(role);
  } else if (text) {
    elements.transcript.append(createTurn(role, text));
  }
  if (shouldStick) scrollTranscript(true);
  else elements.scrollLatest.classList.add('is-visible');
}

function extractText(event) {
  return event.text ?? event.transcript ?? event.content ?? event.delta ?? '';
}

function showInterrupt() {
  clearTimeout(interruptTimer);
  elements.interrupt.classList.add('is-visible');
  interruptTimer = setTimeout(() => elements.interrupt.classList.remove('is-visible'), 1800);
}

function createAudio() {
  return new AudioSession({
    onAudioFrame: (buffer) => {
      if (socket?.readyState !== WebSocket.OPEN || phase !== 'live') return;
      if (socket.bufferedAmount > 512 * 1024) {
        if (Date.now() - lastAudioWarningAt > 5000) {
          showToast('网络有些拥堵，正在自动降低上行积压。', 'error');
          lastAudioWarningAt = Date.now();
        }
        return;
      }
      socket.send(buffer);
    },
    onInputLevel: (value) => { inputLevel = Math.min(1, value * 4.2); },
    onOutputLevel: (value) => {
      outputLevel = Math.min(1, value * 3.4);
      if (outputLevel > .035 && phase === 'live') setStage('speaking', '面试官正在提问', '你可以随时开口打断，播放会立即停止。');
    },
    onPlaybackDrained: () => {
      outputLevel = 0;
      if (phase === 'live') setStage('listening', '轮到你回答', '请尽量给出具体链路、数据口径和技术取舍。');
    },
    onPlaybackMarkerDrained: (announcementId) => {
      if (announcementId) sendJson({ type: 'audio.playback.done', announcement_id: announcementId });
    },
  });
}

async function initializeAudio(capture = true) {
  if (!audio) audio = createAudio();
  if (!audio.context) return audio.initialize({ capture });
  await audio.resume();
  if (capture && !audio.hasMicrophone) {
    try {
      await audio.enableMicrophone();
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
  elements.messageInput.disabled = answerPending;
  elements.send.disabled = answerPending;
  elements.micToggle.classList.toggle('is-hidden', voiceMode === 'L3');
  setConnection('live', '面试进行中');
  setStage(voiceMode === 'L3' ? 'thinking' : 'listening', '面试进行中', voiceMode === 'L3' ? '请在右侧输入框作答。' : '面试官说话时，你也可以直接开口打断。');
  if (voiceMode === 'L3') elements.messageInput.focus();
}

function invalidateAudioPlayback() {
  audioEpoch += 1;
  audioFileQueue = Promise.resolve();
  audio?.clearPlayback();
  outputLevel = 0;
}

function setAnswerPending(pending) {
  answerPending = Boolean(pending);
  const disabled = answerPending || phase !== 'live';
  elements.messageInput.disabled = disabled;
  elements.send.disabled = disabled;
  if (!disabled && voiceMode === 'L3') elements.messageInput.focus();
}

async function handleAudioFile(event, epoch) {
  if (epoch !== audioEpoch || !audio || voiceMode === 'L3') return;
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
  intentionallyClosed = true;
  clearTimeout(reconnectTimer);
  clearInterval(heartbeatTimer);
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  elements.endButton.disabled = true;
  audio?.setMuted(true);
  audio?.clearPlayback();
  audio?.close();
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
      if (event.mode || event.voice_mode) setMode(event.mode || event.voice_mode, true);
      if (event.session && typeof event.session === 'object') interview = { ...interview, ...event.session };
      syncTimer(event.session || event);
      setLive();
      break;
    case 'candidate.transcript.partial':
      renderTurn('candidate', extractText(event), { partial: true, append: event.text === undefined && event.transcript === undefined });
      setStage('listening', '正在听你回答', '把关键链路、指标口径和取舍说具体。');
      break;
    case 'candidate.transcript.done':
      renderTurn('candidate', extractText(event));
      lastTyped = null;
      setAnswerPending(true);
      setStage('thinking', '面试官正在思考', '回答结束后不会即时点评。');
      break;
    case 'interviewer.text.partial':
      renderTurn('interviewer', extractText(event), { partial: true, append: event.text === undefined && event.transcript === undefined });
      setStage('speaking', voiceMode === 'L3' ? '面试官正在追问' : '面试官正在提问', '注意听问题中的限定条件。');
      break;
    case 'interviewer.text.done':
      renderTurn('interviewer', extractText(event), { suppressDuplicate: true });
      if (voiceMode === 'L3') setStage('listening', '轮到你回答', '请在输入框中作答，Enter 发送。');
      break;
    case 'input.speech_started':
      invalidateAudioPlayback();
      showInterrupt();
      setStage('listening', '正在听你回答', '已停止播放面试官语音。');
      break;
    case 'pressure.interrupt':
      showInterrupt();
      setStage('speaking', '面试官打断追问', '压力面正在要求你先给出结论。');
      break;
    case 'audio.chunk': {
      if (!audio || voiceMode === 'L3') break;
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
      const epoch = audioEpoch;
      audioFileQueue = audioFileQueue.then(() => handleAudioFile(event, epoch));
      break;
    }
    case 'audio.clear':
      invalidateAudioPlayback();
      break;
    case 'audio.stream.done':
      audioFileQueue.finally(() => {
        if (audio && voiceMode !== 'L3') audio.markPlaybackEnd(event.announcement_id);
        else sendJson({ type: 'audio.playback.done', announcement_id: event.announcement_id });
      });
      break;
    case 'timer.sync':
      syncTimer(event);
      break;
    case 'mode.changed':
      setMode(event.mode || event.voice_mode, true);
      break;
    case 'interviewer.state':
      if (event.state === 'thinking') setStage('thinking', '面试官正在思考', '回答结束后不会即时点评。');
      else if (event.state === 'silent') setStage('thinking', '面试官保持沉默', '这是压力面的一部分，请保持冷静并检查刚才的回答。');
      else if (event.state === 'listening') {
        setAnswerPending(false);
        setStage('listening', '轮到你回答', '请给出具体链路、数据口径和技术取舍。');
      }
      break;
    case 'interview.ended':
      handleEnded(event);
      break;
    case 'report.ready':
      if (phase !== 'ended') handleEnded({ ...event, report_ready: true });
      else location.assign(`/report?session=${encodeURIComponent(sessionId)}`);
      break;
    case 'error':
      showToast(event.message || event.error || '面试服务发生错误。', 'error', 5500);
      if (['EMPTY_ANSWER', 'ANSWER_FAILED', 'INTERVIEW_TIMEOUT'].includes(event.code)) setAnswerPending(false);
      if (event.fatal) {
        intentionallyClosed = true;
        phase = 'error';
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
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${scheme}//${location.host}/ws/interviews/${encodeURIComponent(sessionId)}`);
  socket.binaryType = 'arraybuffer';
  setConnection('warning', reconnectAttempts ? '正在恢复连接' : '正在连接');

  socket.addEventListener('open', () => {
    sendJson({
      type: 'client.ready',
      client_id: getClientId(),
      mode: voiceMode,
      audio: voiceMode === 'L3' ? null : { format: 'pcm_s16le', sample_rate: 16000, channels: 1, microphone: Boolean(audio?.hasMicrophone) },
    });
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
      audio.enqueuePCM(data, 16000);
    } else if (data instanceof Blob && audio && voiceMode !== 'L3') {
      audio.enqueuePCM(await data.arrayBuffer(), 16000);
    }
  });
  socket.addEventListener('error', () => setConnection('warning', '连接波动'));
  socket.addEventListener('close', ({ code }) => {
    clearInterval(heartbeatTimer);
    invalidateAudioPlayback();
    if ([4400, 4403, 4404].includes(code)) {
      intentionallyClosed = true;
      phase = 'error';
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
      const result = await initializeAudio(true);
      if (!result.microphone) {
        showToast('没有取得麦克风权限；你仍可听取问题并用文字作答。', 'error', 6000);
        elements.permissionNote.textContent = '麦克风未启用，可使用右侧文字输入继续。';
        elements.micToggle.classList.remove('is-hidden');
        elements.micToggle.classList.add('is-muted');
        elements.micToggle.setAttribute('aria-label', '重新启用麦克风');
      }
    } catch (error) {
      audio = null;
      showToast(`${error.message} 已自动保留文字输入。`, 'error', 6500);
    }
  }
  connectSocket();
}

async function finishInterview(reason = 'manual') {
  if (localFinishSent || ['ending', 'ended'].includes(phase)) return;
  localFinishSent = true;
  phase = 'ending';
  elements.messageInput.disabled = true;
  elements.send.disabled = true;
  elements.endButton.disabled = true;
  audio?.setMuted(true);
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
  if (phase !== 'live' || answerPending) return;
  const text = elements.messageInput.value.trim();
  if (!text) return;
  if (!sendJson({ type: 'user.text', text })) {
    showToast('连接尚未恢复，回答暂未发送。', 'error');
    return;
  }
  setAnswerPending(true);
  lastTyped = { text, at: Date.now() };
  elements.messageInput.value = '';
  elements.messageInput.rows = 1;
  setStage('thinking', '面试官正在思考', '回答结束后不会即时点评。');
}

async function toggleMicrophone() {
  if (voiceMode === 'L3') return;
  if (!audio || !audio.hasMicrophone) {
    try {
      const result = await initializeAudio(true);
      if (!result.microphone) throw result.error || new Error('未取得麦克风权限');
      audio.setMuted(false);
      elements.micToggle.classList.remove('is-muted');
      elements.micToggle.setAttribute('aria-label', '关闭麦克风');
      elements.micToggle.title = '关闭麦克风';
      showToast('麦克风已启用。', 'success');
    } catch (error) {
      showToast(error.message || '无法启用麦克风。', 'error');
    }
    return;
  }
  const muted = audio.setMuted(!audio.muted);
  elements.micToggle.classList.toggle('is-muted', muted);
  elements.micToggle.setAttribute('aria-label', muted ? '开启麦克风' : '关闭麦克风');
  elements.micToggle.title = muted ? '开启麦克风' : '关闭麦克风';
  showToast(muted ? '麦克风已静音，可继续打字回答。' : '麦克风已恢复。', muted ? 'info' : 'success');
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

  const company = interview?.company || storedSession?.company || 'bytedance';
  const specialization = String(interview?.specialization || storedSession?.specialization || '通用后端').trim();
  const stressLevel = getStressLevel();
  const stressLabels = { 1: '压力 · 温和', 2: '压力 · 标准', 3: '压力 · 高压' };
  elements.company.textContent = `${companyLabel(company)} · ${specialization}一面`;
  elements.stress.classList.toggle('is-hidden', stressLevel === 0);
  elements.stressLabel.textContent = stressLabels[stressLevel] || '';
  setMode(interview?.voice_mode || serverConfig?.voice_mode || serverConfig?.mode || storedSession?.voice_mode || 'L3');
  syncTimer(interview || {});
  const status = String(interview?.status || interview?.state || '').toLowerCase();
  if (['ended', 'reporting', 'reported', 'completed', 'finished', 'report_ready'].includes(status)) {
    location.replace(`/report?session=${encodeURIComponent(sessionId)}`);
    return;
  }
  phase = 'ready';
  $('span', elements.join).textContent = ['active', 'live', 'running'].includes(status) ? '重新加入面试' : (voiceMode === 'L3' ? '开始文字面试' : '开启麦克风并开始');
  elements.readyDescription.textContent = ['active', 'live', 'running'].includes(status) ? '这场面试仍在进行，可以继续加入。' : '点击后将建立面试连接。';
  elements.join.disabled = false;
  setConnection('', '等待进入');
}

elements.join.addEventListener('click', joinInterview);
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
elements.micToggle.addEventListener('click', toggleMicrophone);
elements.endButton.addEventListener('click', () => {
  if (typeof elements.endDialog.showModal === 'function') elements.endDialog.showModal();
  else if (window.confirm('确定提前结束并生成报告吗？')) finishInterview('manual');
});
$('#confirmEnd').addEventListener('click', (event) => {
  event.preventDefault();
  elements.endDialog.close();
  finishInterview('manual');
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
  socket?.close();
  audio?.close();
});

initialize();
