import { AudioSession } from './audio-session.js?v=20260830-mic-release';
import { clamp, getClientId } from './common.js?v=20260830-hide-internals';

const TEST_SECONDS = 30;
const STOP_ACK_TIMEOUT_MS = 5_000;
const MAX_BUFFERED_AUDIO_BYTES = 512 * 1024;

export class HardwareTest {
  constructor(root) {
    this.root = root;
    this.button = root.querySelector('#hardwareTestButton');
    this.status = root.querySelector('#hardwareTestStatus');
    this.device = root.querySelector('#hardwareTestDevice');
    this.meter = root.querySelector('#hardwareInputMeter');
    this.meterBar = this.meter?.querySelector('i');
    this.countdown = root.querySelector('#hardwareTestCountdown');
    this.transcript = root.querySelector('#hardwareTranscript');
    this.transcriptText = this.transcript?.querySelector('p');

    this.audio = null;
    this.socket = null;
    this.active = false;
    this.ready = false;
    this.stopping = false;
    this.stopPromise = null;
    this.stopAckResolver = null;
    this.deadlineTimer = 0;
    this.countdownTimer = 0;
    this.deadlineAt = 0;
    this.finalSegments = [];
    this.partialText = '';

    this.button?.addEventListener('click', () => {
      if (this.active || this.stopping) this.stop().catch(() => {});
      else this.start().catch(() => {});
    });
    this.inspectAvailability().catch(() => {});
  }

  async inspectAvailability() {
    if (!window.isSecureContext && location.hostname !== 'localhost') {
      this._setState('error', '当前页面无法使用麦克风', '请通过 HTTPS 打开后再测试');
      this.button.disabled = true;
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext && !window.webkitAudioContext) {
      this._setState('error', '当前浏览器不支持麦克风测试', '建议使用最新版 Chrome');
      this.button.disabled = true;
      return;
    }
    try {
      const permission = await navigator.permissions?.query?.({ name: 'microphone' });
      if (!permission) return;
      const renderPermission = () => {
        if (this.active || this.stopping) return;
        if (permission.state === 'granted') {
          this._setState('idle', '麦克风权限已授权', '点击测试，确认输入电平与实时转写');
        } else if (permission.state === 'denied') {
          this._setState('error', '麦克风权限已关闭', '请在浏览器网站设置中允许麦克风');
        } else {
          this._setState('idle', '麦克风等待测试', '点击后浏览器会请求麦克风权限');
        }
      };
      renderPermission();
      permission.addEventListener?.('change', renderPermission);
    } catch {
      // Safari does not expose the microphone permission through Permissions API.
    }
  }

  async start() {
    if (this.active || this.stopping) return;
    this.active = true;
    this.ready = false;
    this.finalSegments = [];
    this.partialText = '';
    this._renderTranscript('', false);
    this._setButton(true, '结束测试');
    this._setState('connecting', '正在启用麦克风…', '请在浏览器提示中选择允许');
    this._startDeadline();

    let audio = null;
    try {
      audio = new AudioSession({
        onAudioFrame: (buffer) => this._sendAudio(buffer),
        onInputLevel: (level) => this._setInputLevel(level),
        onCaptureState: (event) => this._handleCaptureState(event),
        onDevicesChanged: () => this._refreshDeviceLabel().catch(() => {}),
      });
      this.audio = audio;
      const result = await audio.initialize({ capture: true });
      if (!this.active || this.audio !== audio) {
        await audio.close();
        if (this.audio === audio) this.audio = null;
        return;
      }
      if (!result.microphone) throw result.error || new Error('没有取得麦克风权限。');
      await this._refreshDeviceLabel();
      this._setState('connecting', '麦克风可用，正在连接转写…', this.device.textContent);
      this._connectSocket();
    } catch (error) {
      if (audio && this.audio !== audio) await audio.close().catch(() => {});
      if (!this.active && !this.stopping) return;
      await this._fail(this._friendlyError(error));
    }
  }

  _connectSocket() {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${scheme}//${location.host}/ws/hardware-test`);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('open', () => {
      if (!this.active || this.socket !== socket) return;
      socket.send(JSON.stringify({ type: 'client.ready', client_id: getClientId() }));
    });
    socket.addEventListener('message', ({ data }) => {
      if (this.socket !== socket || typeof data !== 'string') return;
      try {
        this._handleServerEvent(JSON.parse(data));
      } catch {
        this._fail('测试服务返回了无法识别的消息，请重新测试。').catch(() => {});
      }
    });
    socket.addEventListener('error', () => {
      if (this.active && !this.stopping) {
        this._setState('connecting', '实时转写连接波动', '正在确认连接状态…');
      }
    });
    socket.addEventListener('close', () => {
      if (this.socket === socket) this.socket = null;
      if (this.stopAckResolver) this.stopAckResolver();
      if (this.active && !this.stopping) {
        this._fail('实时转写连接已断开，请重新测试。').catch(() => {});
      }
    });
  }

  _handleServerEvent(event) {
    const type = String(event?.type || '');
    if (!this.active && type !== 'hardware.stopped') return;
    if (type === 'hardware.ready') {
      if (event.transcription_available === false) {
        this.ready = false;
        this._setState('error', '麦克风可用，实时转写暂不可用', '仍可观察输入电平，稍后可重新测试');
        return;
      }
      this.ready = true;
      this._setState('listening', '麦克风与实时转写正常', '请说一句话，确认下方文字是否出现');
      return;
    }
    if (type === 'hardware.speech.started') {
      this._setState('speech', '正在听你说话…', '保持正常音量与距离');
      return;
    }
    if (type === 'hardware.speech.ended') {
      this._setState('listening', '正在整理转写…', '稍等片刻查看识别结果');
      return;
    }
    if (type === 'hardware.transcript.partial') {
      this.partialText = String(event.text || '').trim();
      this._renderTranscript(this._combinedTranscript(), true);
      return;
    }
    if (type === 'hardware.transcript.done') {
      const text = String(event.text || '').trim();
      if (text && this.finalSegments.at(-1) !== text) this.finalSegments.push(text);
      this.partialText = '';
      this._renderTranscript(this._combinedTranscript(), false);
      this._setState('listening', '转写正常，可以继续测试', '识别有少量偏差属于正常现象');
      return;
    }
    if (type === 'hardware.stopped') {
      this.stopAckResolver?.();
      if (!this.stopping) {
        this.stop({ skipSignal: true, auto: true }).catch(() => {});
      }
      return;
    }
    if (type === 'hardware.error' || type === 'error') {
      const message = String(event.message || event.error?.message || '实时转写暂时不可用，请稍后重试。');
      if (event.recoverable === true) {
        if (event.code === 'TRANSCRIPTION_UNAVAILABLE') this.ready = false;
        this._setState('error', message, '麦克风仍在测试，可观察输入电平或结束后重试');
      } else {
        this._fail(message).catch(() => {});
      }
    }
  }

  _handleCaptureState(event) {
    if (!this.active || this.stopping) return;
    if (event?.type === 'microphone-muted') {
      this._setState('error', '麦克风暂时没有输入', '请检查系统输入设备或静音状态');
    }
    if (['microphone-ended', 'microphone-error'].includes(event?.type)) {
      this._fail('麦克风已断开，请检查设备后重新测试。').catch(() => {});
    }
    if (event?.type === 'microphone-unmuted' && this.ready) {
      this._setState('listening', '麦克风与实时转写正常', '请说一句话，确认下方文字是否出现');
    }
  }

  _sendAudio(buffer) {
    if (!this.active || !this.ready || !(buffer instanceof ArrayBuffer)) return;
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    if (this.socket.bufferedAmount > MAX_BUFFERED_AUDIO_BYTES) return;
    this.socket.send(buffer);
  }

  async _refreshDeviceLabel() {
    if (!this.audio?.microphoneTrack) return;
    const activeDeviceId = String(this.audio.microphoneTrack.getSettings?.().deviceId || '');
    const devices = await this.audio.listInputDevices();
    const selected = devices.find((item) => item.deviceId === activeDeviceId) || devices[0];
    this.device.textContent = selected?.label || '已检测到可用麦克风';
  }

  _combinedTranscript() {
    return [...this.finalSegments, this.partialText].filter(Boolean).join(' ');
  }

  _renderTranscript(text, partial) {
    const value = String(text || '').trim();
    this.transcript.dataset.empty = String(!value);
    this.transcript.dataset.partial = String(Boolean(partial && value));
    this.transcriptText.textContent = value || '请说一句话，例如：“我负责过一个高并发秒杀项目。”';
  }

  _setInputLevel(level) {
    const normalized = clamp(Number(level) || 0, 0, 1);
    this.meterBar.style.transform = `scaleX(${normalized.toFixed(3)})`;
    this.meter.setAttribute('aria-valuenow', String(Math.round(normalized * 100)));
  }

  _setState(state, title, detail) {
    this.root.dataset.state = state;
    this.status.textContent = title;
    if (detail) this.device.textContent = detail;
  }

  _setButton(active, label) {
    this.button.disabled = false;
    this.button.setAttribute('aria-pressed', String(active));
    this.button.querySelector('span').textContent = label;
  }

  _startDeadline() {
    this._clearDeadline();
    this.deadlineAt = Date.now() + TEST_SECONDS * 1000;
    const update = () => {
      const seconds = Math.max(0, Math.ceil((this.deadlineAt - Date.now()) / 1000));
      this.countdown.textContent = `剩余 ${seconds} 秒`;
    };
    update();
    this.countdownTimer = window.setInterval(update, 250);
    this.deadlineTimer = window.setTimeout(() => {
      this.stop({ auto: true }).catch(() => {});
    }, TEST_SECONDS * 1000);
  }

  _clearDeadline() {
    window.clearTimeout(this.deadlineTimer);
    window.clearInterval(this.countdownTimer);
    this.deadlineTimer = 0;
    this.countdownTimer = 0;
    this.countdown.textContent = `最长 ${TEST_SECONDS} 秒`;
  }

  async stop({ skipSignal = false, auto = false, quiet = false, immediate = false } = {}) {
    if (this.stopPromise) return this.stopPromise;
    if (!this.active && !this.audio && !this.socket) return;
    this.stopPromise = this._stop({ skipSignal, auto, quiet, immediate });
    try {
      await this.stopPromise;
    } finally {
      this.stopPromise = null;
    }
  }

  async _stop({ skipSignal, auto, quiet, immediate }) {
    this.active = false;
    this.ready = false;
    this.stopping = true;
    this._clearDeadline();
    this._setInputLevel(0);
    this.button.disabled = true;
    this._setState('connecting', '正在关闭测试…', '正在释放麦克风');

    // Release the physical capture track synchronously. The WebSocket may stay
    // open briefly so the server can flush a final transcript, but the browser
    // microphone indicator must turn off as soon as the user clicks stop.
    this.audio?.disableMicrophone();
    const socket = this.socket;
    if (!skipSignal && socket?.readyState === WebSocket.OPEN) {
      const acknowledged = new Promise((resolve) => { this.stopAckResolver = resolve; });
      socket.send(JSON.stringify({ type: 'hardware.stop' }));
      if (!immediate) {
        await Promise.race([
          acknowledged,
          new Promise((resolve) => window.setTimeout(resolve, STOP_ACK_TIMEOUT_MS)),
        ]);
      }
    }
    this.stopAckResolver = null;
    await this._releaseResources();
    this.stopping = false;
    this._setButton(false, '重新测试');
    if (!quiet) {
      const title = auto ? '30 秒测试已完成' : '测试已结束';
      const detail = this.finalSegments.length ? '已确认语音输入与实时转写' : '麦克风已关闭并释放';
      this._setState('success', title, detail);
    }
  }

  async _fail(message) {
    if (this.stopping) return;
    this.active = false;
    this.ready = false;
    this.stopping = true;
    this._clearDeadline();
    this._setInputLevel(0);
    await this._releaseResources();
    this.stopping = false;
    this._setButton(false, '重新测试');
    this._setState('error', message, '麦克风已释放，可检查权限后重试');
  }

  async _releaseResources() {
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      if ([WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) socket.close(1000, 'hardware test complete');
    }
    const audio = this.audio;
    this.audio = null;
    if (audio) await audio.close().catch(() => {});
  }

  _friendlyError(error) {
    if (['NotAllowedError', 'SecurityError'].includes(error?.name)) return '没有取得麦克风权限，请在浏览器设置中允许后重试。';
    if (['NotFoundError', 'DevicesNotFoundError'].includes(error?.name)) return '没有检测到麦克风，请连接设备后重试。';
    if (['NotReadableError', 'TrackStartError'].includes(error?.name)) return '麦克风正被其他应用占用，请关闭占用后重试。';
    return error?.message || '麦克风测试启动失败，请稍后重试。';
  }

  dispose() {
    this.stop({ quiet: true, immediate: true }).catch(() => {});
  }
}

export function createHardwareTest() {
  const root = document.querySelector('#hardwareTestCard');
  return root ? new HardwareTest(root) : null;
}
