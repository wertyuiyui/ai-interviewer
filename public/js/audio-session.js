export class AudioSession {
  constructor(callbacks = {}) {
    this.onAudioFrame = callbacks.onAudioFrame || (() => {});
    this.onInputLevel = callbacks.onInputLevel || (() => {});
    this.onOutputLevel = callbacks.onOutputLevel || (() => {});
    this.onPlaybackDrained = callbacks.onPlaybackDrained || (() => {});
    this.onPlaybackMarkerDrained = callbacks.onPlaybackMarkerDrained || (() => {});
    this.context = null;
    this.stream = null;
    this.source = null;
    this.captureNode = null;
    this.captureSink = null;
    this.playbackNode = null;
    this.generation = 1;
    this.muted = false;
    this.closed = false;
  }

  get hasMicrophone() {
    return Boolean(this.stream?.getAudioTracks().some((track) => track.readyState === 'live'));
  }

  async initialize({ capture = true } = {}) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !window.AudioWorkletNode) throw new Error('当前浏览器不支持 AudioWorklet，请使用最新版 Chrome。');
    if (!window.isSecureContext && location.hostname !== 'localhost') throw new Error('麦克风只允许在 HTTPS 页面使用。');

    this.context = new AudioContextClass({ latencyHint: 'interactive' });
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
      await this.enableMicrophone();
      return { microphone: true, error: null };
    } catch (error) {
      return { microphone: false, error };
    }
  }

  async enableMicrophone() {
    if (this.hasMicrophone) return;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('浏览器无法访问麦克风，请检查 HTTPS 与浏览器权限。');
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    this.source = this.context.createMediaStreamSource(this.stream);
    this.captureNode = new AudioWorkletNode(this.context, 'pcm-capture-processor', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: 'explicit',
      processorOptions: { targetSampleRate: 16000, frameSamples: 320 },
    });
    this.captureSink = this.context.createGain();
    this.captureSink.gain.value = 0;
    this.captureNode.port.onmessage = ({ data }) => {
      if (data?.type === 'audio' && data.buffer instanceof ArrayBuffer && !this.muted) this.onAudioFrame(data.buffer);
      if (data?.type === 'level') this.onInputLevel(this.muted ? 0 : Number(data.value) || 0);
    };
    this.source.connect(this.captureNode).connect(this.captureSink).connect(this.context.destination);
  }

  async resume() {
    if (this.context?.state === 'suspended') await this.context.resume();
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
    this.stream?.getTracks().forEach((track) => track.stop());
    this.source?.disconnect();
    this.captureNode?.disconnect();
    this.captureSink?.disconnect();
    this.playbackNode?.disconnect();
    this.captureNode?.port.close();
    this.playbackNode?.port.close();
    if (this.context && this.context.state !== 'closed') await this.context.close().catch(() => {});
    this.stream = null;
  }
}
