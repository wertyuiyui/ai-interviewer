class PcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.current = null;
    this.position = 0;
    this.generation = 0;
    this.lastLevelAt = -1;
    this.wasPlaying = false;
    this.pendingMarkers = [];

    this.port.onmessage = ({ data }) => {
      if (!data || typeof data !== 'object') return;
      if (data.type === 'clear') {
        this.generation = Number(data.generation) || this.generation + 1;
        this.queue.length = 0;
        this.current = null;
        this.position = 0;
        this.wasPlaying = false;
        this.pendingMarkers.length = 0;
        this.port.postMessage({ type: 'level', value: 0 });
        return;
      }
      if (Number(data.generation) < this.generation) return;
      if (data.type === 'pcm' && data.buffer instanceof ArrayBuffer) {
        const view = new DataView(data.buffer);
        const samples = new Float32Array(Math.floor(view.byteLength / 2));
        for (let index = 0; index < samples.length; index += 1) samples[index] = view.getInt16(index * 2, true) / 32768;
        this.enqueue(samples, data.sampleRate);
      } else if (data.type === 'samples' && data.samples) {
        const samples = data.samples instanceof Float32Array ? data.samples : new Float32Array(data.samples);
        this.enqueue(samples, data.sampleRate);
      } else if (data.type === 'marker' && data.id) {
        this.pendingMarkers.push(String(data.id));
        this.flushMarkersIfIdle();
      }
    };
  }

  flushMarkersIfIdle() {
    if (this.current || this.queue.length) return;
    while (this.pendingMarkers.length) {
      this.port.postMessage({ type: 'marker-drained', id: this.pendingMarkers.shift() });
    }
  }

  enqueue(samples, sourceSampleRate) {
    if (!samples?.length) return;
    this.queue.push({ samples, sampleRate: Math.max(8000, Number(sourceSampleRate) || 16000) });
    if (this.queue.length > 500) this.queue.splice(0, this.queue.length - 500);
  }

  nextSegment() {
    this.current = this.queue.shift() || null;
    this.position = 0;
    return this.current;
  }

  nextSample() {
    while (this.current || this.nextSegment()) {
      const samples = this.current.samples;
      if (this.position >= samples.length) {
        this.current = null;
        continue;
      }
      const left = Math.floor(this.position);
      const right = Math.min(left + 1, samples.length - 1);
      const fraction = this.position - left;
      const value = samples[left] + (samples[right] - samples[left]) * fraction;
      this.position += this.current.sampleRate / sampleRate;
      return value;
    }
    return 0;
  }

  process(inputs, outputs) {
    const channels = outputs[0];
    if (!channels?.length) return true;
    let sumSquares = 0;
    let hasAudio = false;
    for (let index = 0; index < channels[0].length; index += 1) {
      const value = this.nextSample();
      if (value !== 0) hasAudio = true;
      sumSquares += value * value;
      for (const channel of channels) channel[index] = value;
    }

    if (this.lastLevelAt < 0 || currentTime - this.lastLevelAt >= 0.05) {
      this.port.postMessage({ type: 'level', value: Math.sqrt(sumSquares / channels[0].length) });
      this.lastLevelAt = currentTime;
    }
    if (this.wasPlaying && !hasAudio && !this.current && this.queue.length === 0) this.port.postMessage({ type: 'drained' });
    if (!hasAudio) this.flushMarkersIfIdle();
    this.wasPlaying = hasAudio || Boolean(this.current) || this.queue.length > 0;
    return true;
  }
}

registerProcessor('pcm-playback-processor', PcmPlaybackProcessor);
