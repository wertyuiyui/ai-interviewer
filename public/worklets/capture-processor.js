class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const settings = options.processorOptions || {};
    this.targetSampleRate = Number(settings.targetSampleRate) || 16000;
    this.frameSamples = Number(settings.frameSamples) || 320;
    this.step = sampleRate / this.targetSampleRate;
    this.cursor = 0;
    this.tail = new Float32Array(0);
    this.frame = new Float32Array(this.frameSamples);
    this.frameOffset = 0;
    this.muted = false;
    this.lastLevelAt = -1;

    this.port.onmessage = ({ data }) => {
      if (data?.type !== 'muted') return;
      this.muted = Boolean(data.value);
      if (this.muted) this.frameOffset = 0;
    };
  }

  emitFrame() {
    const buffer = new ArrayBuffer(this.frameSamples * 2);
    const view = new DataView(buffer);
    for (let index = 0; index < this.frameSamples; index += 1) {
      const sample = Math.max(-1, Math.min(1, this.frame[index]));
      const integer = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
      view.setInt16(index * 2, integer, true);
    }
    this.port.postMessage({ type: 'audio', buffer }, [buffer]);
    this.frameOffset = 0;
  }

  appendSample(value) {
    if (this.muted) return;
    this.frame[this.frameOffset] = value;
    this.frameOffset += 1;
    if (this.frameOffset === this.frameSamples) this.emitFrame();
  }

  resample(input) {
    if (!input?.length) return;
    const combined = new Float32Array(this.tail.length + input.length);
    combined.set(this.tail, 0);
    combined.set(input, this.tail.length);

    while (this.cursor + 1 < combined.length) {
      const left = Math.floor(this.cursor);
      const fraction = this.cursor - left;
      const value = combined[left] + (combined[left + 1] - combined[left]) * fraction;
      this.appendSample(value);
      this.cursor += this.step;
    }

    const retainedIndex = Math.max(0, combined.length - 1);
    this.tail = combined.slice(retainedIndex);
    this.cursor -= retainedIndex;
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0];
    if (output) output.forEach((channel) => channel.fill(0));
    if (!input?.length) return true;

    let sumSquares = 0;
    for (let index = 0; index < input.length; index += 1) sumSquares += input[index] * input[index];
    if (this.lastLevelAt < 0 || currentTime - this.lastLevelAt >= 0.05) {
      this.port.postMessage({ type: 'level', value: Math.sqrt(sumSquares / input.length) });
      this.lastLevelAt = currentTime;
    }
    this.resample(input);
    return true;
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
