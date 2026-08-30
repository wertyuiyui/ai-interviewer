from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_interview_audio_diagnostics_contract() -> None:
    interview_html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    interview_js = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")
    audio_js = (ROOT / "public" / "js" / "audio-session.js").read_text(encoding="utf-8")

    for element_id in (
        "liveTranscriptBar",
        "liveTranscriptStatus",
        "liveTranscriptText",
        "microphoneSelect",
        "inputMeter",
        "microphoneState",
        "rawCaptureToggle",
    ):
        assert f'id="{element_id}"' in interview_html

    assert "providerAudioReady" in interview_js
    assert "candidateAudioExpected" in interview_js
    assert "serverQuietSince" in interview_js
    assert "case 'audio.input.level'" in interview_js
    assert "enumerateDevices" in audio_js
    assert "channelCountMode: 'max'" in audio_js
    assert "echoCancellation: raw ? false" in audio_js


def test_capture_worklet_uses_audible_channel_without_stereo_cancellation() -> None:
    worklet = ROOT / "public" / "worklets" / "capture-processor.js"
    script = r"""
const fs = require('fs');
global.sampleRate = 48000;
global.currentTime = 0;
global.AudioWorkletProcessor = class {
  constructor() {
    this.port = {
      messages: [],
      postMessage(message) { this.messages.push(message); },
      onmessage: null,
    };
  }
};
let Processor = null;
global.registerProcessor = (_name, constructor) => { Processor = constructor; };
eval(fs.readFileSync(process.argv[1], 'utf8'));

function capture(channels) {
  const processor = new Processor({
    processorOptions: { targetSampleRate: 48000, frameSamples: 16 },
  });
  processor.process([channels], [[new Float32Array(64)]]);
  const audio = processor.port.messages.find((message) => message.type === 'audio');
  const level = processor.port.messages.find((message) => message.type === 'level');
  if (!audio || !level) throw new Error('worklet did not emit audio and level messages');
  const view = new DataView(audio.buffer);
  let peak = 0;
  for (let offset = 0; offset < view.byteLength; offset += 2) {
    peak = Math.max(peak, Math.abs(view.getInt16(offset, true)));
  }
  return { peak, level: level.value };
}

const silent = new Float32Array(64);
const sine = Float32Array.from({ length: 64 }, (_, index) => Math.sin(index * 0.31) * 0.5);
const inverse = Float32Array.from(sine, (sample) => -sample);
const silentFirst = capture([silent, sine]);
const antiphase = capture([sine, inverse]);
if (silentFirst.peak < 1000 || silentFirst.level < 0.05) {
  throw new Error(`silent-first stereo collapsed: ${JSON.stringify(silentFirst)}`);
}
if (antiphase.peak < 1000 || antiphase.level < 0.05) {
  throw new Error(`antiphase stereo cancelled: ${JSON.stringify(antiphase)}`);
}
"""
    completed = subprocess.run(
        ["node", "-e", script, str(worklet)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_microphone_graph_failure_stops_adopted_track_and_disables_uplink() -> None:
    audio_session = ROOT / "public" / "js" / "audio-session.js"
    script = r"""
const fs = require('fs');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace('export class AudioSession', 'class AudioSession');
source += '\nglobalThis.TestAudioSession = AudioSession;';
global.performance = { now: () => 0 };

let stopped = false;
const track = {
  muted: false,
  readyState: 'live',
  getSettings: () => ({ deviceId: 'new-device' }),
  addEventListener: () => {},
  removeEventListener: () => {},
  stop() { stopped = true; this.readyState = 'ended'; },
};
const stream = {
  getAudioTracks: () => [track],
  getTracks: () => [track],
};
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: { mediaDevices: { getUserMedia: async () => stream } },
});
global.AudioWorkletNode = class {
  constructor() { throw new Error('synthetic graph failure'); }
};
eval(source);

(async () => {
  const states = [];
  const session = new globalThis.TestAudioSession({
    onCaptureState: (event) => states.push(event),
  });
  session.context = {
    state: 'running',
    resume: async () => {},
    createMediaStreamSource: () => ({
      connect() { return this; },
      disconnect() {},
    }),
  };
  let failed = false;
  try {
    await session.enableMicrophone('new-device', { force: true });
  } catch (error) {
    failed = error.message === 'synthetic graph failure';
  }
  if (!failed) throw new Error('graph failure did not propagate');
  if (!stopped) throw new Error('adopted microphone track leaked');
  if (session.hasMicrophone || session.stream || session.captureNode) {
    throw new Error('failed graph remained eligible for audio uplink');
  }
  if (!states.some((event) => event.type === 'microphone-error')) {
    throw new Error(`missing capture failure state: ${JSON.stringify(states)}`);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        ["node", "-e", script, str(audio_session)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
