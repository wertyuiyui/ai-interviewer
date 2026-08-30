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


def test_realtime_transcript_correction_timing_and_pressure_contract() -> None:
    interview_html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    interview_js = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    assert 'id="answerTimeGuide"' in interview_html
    assert "candidate.transcript.correct'" in interview_js
    assert "case 'candidate.transcript.corrected'" in interview_js
    assert "case 'interviewer.audio.synced'" in interview_js
    assert "recommended_answer_seconds" in interview_js
    assert "event.pressure_action" in interview_js
    assert "压力面 ${getStressLevel()}/3 · 情境进行中" in interview_js


def test_audio_sync_and_pressure_interjection_do_not_replace_question_state() -> None:
    interview_js = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    audio_sync_case = interview_js.split("case 'interviewer.audio.synced':", 1)[1].split("break;", 1)[0]
    assert "syncInterviewerTranscript(event, { updateQuestion: false })" in audio_sync_case
    assert "setCurrentQuestion" not in audio_sync_case

    partial_case = interview_js.split("case 'interviewer.text.partial':", 1)[1].split("break;", 1)[0]
    assert "const interjection = isPressureInterjection(event)" in partial_case
    assert "if (!interjection)" in partial_case
    assert "questionReady = false" in partial_case
    assert "markPressureInterjection(turn)" in partial_case

    done_case = interview_js.split("case 'interviewer.text.done':", 1)[1].split("break;", 1)[0]
    assert "recommendedSeconds: interjection ? 0" in done_case
    assert "markPressureInterjection(resolvedTurn)" in done_case
    assert "} else {" in done_case

    sync_function = interview_js.split("function syncInterviewerTranscript", 1)[1].split("function openTranscriptEditor", 1)[0]
    assert "if (!updateQuestion) return" in sync_function
    assert sync_function.index("if (!updateQuestion) return") < sync_function.index("if (isPressureInterjection(event))")
    assert "providedRecommendedAnswerSeconds" in sync_function
    assert "recommendedAnswerSeconds(event, spokenText)" not in sync_function

    partial_guard = partial_case.split("if (!interjection)", 1)[1].split("const turn =", 1)[0]
    assert "expectCandidateAudio(false)" in partial_guard


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
