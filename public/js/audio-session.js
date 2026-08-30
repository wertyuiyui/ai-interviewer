export class AudioSession {
  constructor(callbacks = {}) {
    this.onAudioFrame = callbacks.onAudioFrame || (() => {});
    this.onInputLevel = callbacks.onInputLevel || (() => {});
    this.onOutputLevel = callbacks.onOutputLevel || (() => {});
    this.onPlaybackDrained = callbacks.onPlaybackDrained || (() => {});
    this.onPlaybackMarkerDrained = callbacks.onPlaybackMarkerDrained || (() => {});
    this.onCaptureState = callbacks.onCaptureState || (() => {});
    this.onDevicesChanged = callbacks.onDevicesChanged || (() => {});
    this.context = null;
    this.stream = null;
    this.source = null;
    this.captureNode = null;
    this.captureSink = null;
    this.playbackNode = null;
    this.generation = 1;
    this.muted = false;
    this.closed = false;
    this.captureFrames = 0;
    this.lastCaptureFrameAt = 0;
    this.selectedDeviceId = '';
    this.trackMuted = false;
    this.rawCapture = false;
    this._captureStateHandler = null;
    this._trackEndedHandler = null;
    this._trackMuteHandler = null;
    this._trackUnmuteHandler = null;
    this._visibilityHandler = null;
    this._deviceChangeHandler = null;
  }

  get hasMicrophone() {
    return Boolean(
      this.captureNode
      && this.stream?.getAudioTracks().some((track) => track.readyState === 'live'),
    );
  }

  get microphoneTrack() {
    return this.stream?.getAudioTracks()[0] || null;
  }

  async listInputDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    let unnamed = 0;
    return devices.filter((device) => device.kind === 'audioinput').map((device) => {
      unnamed += device.label ? 0 : 1;
      return {
        deviceId: String(device.deviceId || ''),
        label: String(device.label || `麦克风 ${unnamed}`),
      };
    });
  }

  async initialize({ capture = true, deviceId = '', raw = false } = {}) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !window.AudioWorkletNode) throw new Error('当前浏览器不支持 AudioWorklet，请使用最新版 Chrome。');
    if (!window.isSecureContext && location.hostname !== 'localhost') throw new Error('麦克风只允许在 HTTPS 页面使用。');

    this.context = new AudioContextClass({ latencyHint: 'interactive' });
    this._captureStateHandler = () => {
      this.onCaptureState({ type: 'audio-context', state: this.context?.state || 'closed' });
    };
    this.context.addEventListener('statechange', this._captureStateHandler);
    this._visibilityHandler = () => {
      if (document.visibilityState === 'visible'
          && ['suspended', 'interrupted'].includes(this.context?.state)) {
        this.resume().catch(() => {});
      }
    };
    document.addEventListener('visibilitychange', this._visibilityHandler);
    this._deviceChangeHandler = () => this.onDevicesChanged();
    navigator.mediaDevices?.addEventListener?.('devicechange', this._deviceChangeHandler);
    await Promise.all([
      this.context.audioWorklet.addModule('/worklets/capture-processor.js'),
      this.context.audioWorklet.addModule('/worklets/playback-processor.js'),
    ]);
    this.playbackNode = new AudioWorkletNode(this.context, 'pcm-playback-processor', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    this.playbackNode.port.onmessage = ({ data }) => {
      if (data?.type === 'level') this.onOutputLevel(Number(data.value) || 0);
      if (data?.type === 'drained') this.onPlaybackDrained();
      if (data?.type === 'marker-drained') this.onPlaybackMarkerDrained(String(data.id || ''));
    };
    this.playbackNode.connect(this.context.destination);
    await this.resume();

    if (!capture) return { microphone: false, error: null };
    try {
      await this.enableMicrophone(deviceId, { raw });
      return { microphone: true, error: null };
    } catch (error) {
      return { microphone: false, error };
    }
  }

  async enableMicrophone(deviceId = '', { force = false, raw = false } = {}) {
    const requestedDeviceId = String(deviceId || '');
    if (this.hasMicrophone && !force
        && (!requestedDeviceId || requestedDeviceId === this.selectedDeviceId)
        && Boolean(raw) === this.rawCapture) return;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('浏览器无法访问麦克风，请检查 HTTPS 与浏览器权限。');
    await this.resume();
    let nextStream = null;
    let adoptedStream = null;
    const replacingExistingCapture = this.hasMicrophone;
    let rawFallbackUsed = false;
    const processedConstraints = {
      // Keep stereo-capable devices intact until the worklet can choose a
      // healthy channel. Asking the browser for mono may discard channel 2.
      channelCount: { ideal: 2 },
      echoCancellation: raw ? false : { ideal: true },
      noiseSuppression: raw ? false : { ideal: true },
      autoGainControl: raw ? false : { ideal: true },
    };
    if (requestedDeviceId) processedConstraints.deviceId = { exact: requestedDeviceId };
    try {
      try {
        nextStream = await navigator.mediaDevices.getUserMedia({ audio: processedConstraints, video: false });
      } catch (error) {
        const mayRetryRaw = ['OverconstrainedError', 'ConstraintNotSatisfiedError', 'NotReadableError'].includes(error?.name);
        if (!mayRetryRaw) throw error;
        rawFallbackUsed = Boolean(raw);
        nextStream = await navigator.mediaDevices.getUserMedia({
          audio: requestedDeviceId ? { deviceId: { exact: requestedDeviceId } } : true,
          video: false,
        });
      }
      const track = nextStream.getAudioTracks()[0];
      if (!track) throw new Error('没有检测到可用的麦克风音轨。');
      const trackSettings = track.getSettings?.() || {};
      const actualDeviceId = String(trackSettings.deviceId || requestedDeviceId || '');
      this._closeCapture();
      this.stream = nextStream;
      adoptedStream = nextStream;
      nextStream = null;
      this.selectedDeviceId = actualDeviceId;
      this.trackMuted = Boolean(track.muted);
      const rawProcessingActive = ['echoCancellation', 'noiseSuppression', 'autoGainControl']
        .some((key) => trackSettings[key] === true);
      this.rawCapture = Boolean(raw) && !rawFallbackUsed && !rawProcessingActive;
      this._trackEndedHandler = () => this.onCaptureState({ type: 'microphone-ended', state: 'ended' });
      this._trackMuteHandler = () => {
        this.trackMuted = true;
        this.onInputLevel(0);
        this.onCaptureState({ type: 'microphone-muted', state: 'muted' });
      };
      this._trackUnmuteHandler = () => {
        this.trackMuted = false;
        this.onCaptureState({ type: 'microphone-unmuted', state: 'live' });
      };
      track.addEventListener('ended', this._trackEndedHandler);
      track.addEventListener('mute', this._trackMuteHandler);
      track.addEventListener('unmute', this._trackUnmuteHandler);
      this.source = this.context.createMediaStreamSource(this.stream);
      this.captureNode = new AudioWorkletNode(this.context, 'pcm-capture-processor', {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        channelCountMode: 'max',
        channelInterpretation: 'discrete',
        processorOptions: { targetSampleRate: 16000, frameSamples: 1600 },
      });
      this.captureSink = this.context.createGain();
      this.captureSink.gain.value = 0;
      this.captureNode.port.onmessage = ({ data }) => {
        if (data?.type === 'audio' && data.buffer instanceof ArrayBuffer && !this.muted) {
          this.captureFrames += 1;
          this.lastCaptureFrameAt = performance.now();
          this.onAudioFrame(data.buffer);
        }
        if (data?.type === 'level') this.onInputLevel(this.muted || this.trackMuted ? 0 : Number(data.value) || 0);
      };
      this.source.connect(this.captureNode).connect(this.captureSink).connect(this.context.destination);
      this.onCaptureState({ type: 'microphone-ready', state: this.trackMuted ? 'muted' : 'live' });
      if (this.trackMuted) this.onCaptureState({ type: 'microphone-muted', state: 'muted' });
    } catch (error) {
      nextStream?.getTracks().forEach((track) => track.stop());
      if (adoptedStream && this.stream === adoptedStream) this._closeCapture();
      this.onCaptureState({
        type: replacingExistingCapture ? 'microphone-switch-error' : 'microphone-error',
        state: 'error',
      });
      throw error;
    }
  }

  _closeCapture() {
    const tracks = this.stream?.getAudioTracks() || [];
    tracks.forEach((track) => {
      if (this._trackEndedHandler) track.removeEventListener('ended', this._trackEndedHandler);
      if (this._trackMuteHandler) track.removeEventListener('mute', this._trackMuteHandler);
      if (this._trackUnmuteHandler) track.removeEventListener('unmute', this._trackUnmuteHandler);
      track.stop();
    });
    this.source?.disconnect();
    this.captureNode?.disconnect();
    this.captureSink?.disconnect();
    if (this.captureNode?.port) this.captureNode.port.onmessage = null;
    this.captureNode?.port.close();
    this.stream = null;
    this.source = null;
    this.captureNode = null;
    this.captureSink = null;
    this._trackEndedHandler = null;
    this._trackMuteHandler = null;
    this._trackUnmuteHandler = null;
    this.selectedDeviceId = '';
    this.trackMuted = false;
    this.rawCapture = false;
    this.captureFrames = 0;
    this.lastCaptureFrameAt = 0;
  }

  disableMicrophone() {
    const hadCapture = Boolean(this.stream || this.source || this.captureNode || this.captureSink);
    this._closeCapture();
    // `muted` describes a live capture track. A disabled microphone has no
    // track at all, so leave the session ready for a later enableMicrophone().
    this.muted = false;
    this.onInputLevel(0);
    if (hadCapture) this.onCaptureState({ type: 'microphone-disabled', state: 'disabled' });
    return hadCapture;
  }

  async resume() {
    if (['suspended', 'interrupted'].includes(this.context?.state)) await this.context.resume();
  }

  setMuted(value) {
    this.muted = Boolean(value);
    this.captureNode?.port.postMessage({ type: 'muted', value: this.muted });
    this.stream?.getAudioTracks().forEach((track) => { track.enabled = !this.muted; });
    if (this.muted) this.onInputLevel(0);
    return this.muted;
  }

  enqueuePCM(buffer, sourceSampleRate = 16000) {
    if (!this.playbackNode || this.closed) return;
    let transferable = buffer;
    if (ArrayBuffer.isView(buffer)) {
      transferable = buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
    }
    if (!(transferable instanceof ArrayBuffer) || transferable.byteLength === 0) return;
    this.playbackNode.port.postMessage({
      type: 'pcm',
      buffer: transferable,
      sampleRate: Number(sourceSampleRate) || 16000,
      generation: this.generation,
    }, [transferable]);
  }

  async enqueueEncoded(buffer) {
    if (!this.context || this.closed || !(buffer instanceof ArrayBuffer) || !buffer.byteLength) return;
    const expectedGeneration = this.generation;
    try {
      const decoded = await this.context.decodeAudioData(buffer.slice(0));
      if (this.closed || expectedGeneration !== this.generation || !this.playbackNode) return;
      const samples = new Float32Array(decoded.length);
      for (let channelIndex = 0; channelIndex < decoded.numberOfChannels; channelIndex += 1) {
        const channel = decoded.getChannelData(channelIndex);
        for (let index = 0; index < decoded.length; index += 1) samples[index] += channel[index] / decoded.numberOfChannels;
      }
      this.playbackNode.port.postMessage({
        type: 'samples',
        samples,
        sampleRate: decoded.sampleRate,
        generation: expectedGeneration,
      }, [samples.buffer]);
    } catch (error) {
      throw new Error(`无法解码面试官语音：${error?.message || '未知音频格式'}`);
    }
  }

  clearPlayback() {
    this.generation += 1;
    this.playbackNode?.port.postMessage({ type: 'clear', generation: this.generation });
    this.onOutputLevel(0);
  }

  markPlaybackEnd(id) {
    if (!id) return;
    if (!this.playbackNode || this.closed) {
      this.onPlaybackMarkerDrained(String(id));
      return;
    }
    this.playbackNode.port.postMessage({ type: 'marker', id: String(id) });
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    this.clearPlayback();
    this._closeCapture();
    this.playbackNode?.disconnect();
    this.playbackNode?.port.close();
    if (this._captureStateHandler) this.context?.removeEventListener('statechange', this._captureStateHandler);
    if (this._visibilityHandler) document.removeEventListener('visibilitychange', this._visibilityHandler);
    if (this._deviceChangeHandler) navigator.mediaDevices?.removeEventListener?.('devicechange', this._deviceChangeHandler);
    if (this.context && this.context.state !== 'closed') await this.context.close().catch(() => {});
    this._captureStateHandler = null;
    this._visibilityHandler = null;
    this._deviceChangeHandler = null;
  }
}
