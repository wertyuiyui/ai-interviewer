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


def test_microphone_close_lifecycle_contract() -> None:
    interview_html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    interview_js = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    assert "/js/interview.js?v=20260830-stage-flow-v1" in interview_html
    assert "./audio-session.js?v=20260830-mic-release" in interview_js
    assert "let microphoneExplicitlyDisabled = false" in interview_js
    assert "type: 'microphone.state', enabled: nextState" in interview_js

    switch_block = interview_js.split("async function switchMicrophoneDevice()", 1)[1].split("function setMode", 1)[0]
    assert "if (microphoneExplicitlyDisabled)" in switch_block
    assert "disableMicrophoneCapture({ notify: true })" in switch_block

    toggle_block = interview_js.split("async function toggleMicrophone()", 1)[1].split("function drawWaveform", 1)[0]
    assert "disableMicrophoneCapture({ explicit: true, notify: true })" in toggle_block
    assert "setMuted(!audio.muted)" not in toggle_block
    assert "麦克风已关闭并释放" in toggle_block

    finish_block = interview_js.split("async function finishInterview", 1)[1].split("function submitText", 1)[0]
    assert "disableMicrophoneCapture({ explicit: true, notify: true })" in finish_block
    assert finish_block.index("disableMicrophoneCapture") < finish_block.index("type: 'interview.end'")

    socket_open = interview_js.split("socket.addEventListener('open'", 1)[1].split("socket.addEventListener('message'", 1)[0]
    assert "const microphoneEnabled = isMicrophoneCaptureEnabled()" in socket_open
    assert "microphone: microphoneEnabled" in socket_open
    assert "sendMicrophoneState(microphoneEnabled, { force: true })" in socket_open

    reconnect_block = interview_js.split("function scheduleReconnect", 1)[1].split("function connectSocket", 1)[0]
    assert "disableMicrophoneCapture({ explicit: true, notify: false })" in reconnect_block
    fatal_error_block = interview_js.split("if (event.fatal)", 1)[1].split("break;", 1)[0]
    assert "disableMicrophoneCapture({ explicit: true, notify: true })" in fatal_error_block


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


def test_disable_microphone_stops_capture_but_preserves_playback() -> None:
    audio_session = ROOT / "public" / "js" / "audio-session.js"
    script = r"""
const fs = require('fs');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace('export class AudioSession', 'class AudioSession');
source += '\nglobalThis.TestAudioSession = AudioSession;';
global.performance = { now: () => 0 };
eval(source);

let trackStopped = false;
let sourceDisconnected = false;
let captureDisconnected = false;
let sinkDisconnected = false;
let capturePortClosed = false;
let playbackDisconnected = false;
const track = {
  readyState: 'live',
  removeEventListener() {},
  stop() { trackStopped = true; this.readyState = 'ended'; },
};
const stream = { getAudioTracks: () => [track] };
const mediaSource = { disconnect() { sourceDisconnected = true; } };
const capturePort = {
  onmessage: () => {},
  close() { capturePortClosed = true; },
  postMessage() {},
};
const captureNode = { disconnect() { captureDisconnected = true; }, port: capturePort };
const captureSink = { disconnect() { sinkDisconnected = true; } };
const playbackNode = { disconnect() { playbackDisconnected = true; } };
const context = { state: 'running' };
const states = [];
let finalInputLevel = 1;

const session = new globalThis.TestAudioSession({
  onInputLevel: (value) => { finalInputLevel = value; },
  onCaptureState: (event) => states.push(event),
});
session.context = context;
session.playbackNode = playbackNode;
session.stream = stream;
session.source = mediaSource;
session.captureNode = captureNode;
session.captureSink = captureSink;
session.selectedDeviceId = 'test-device';
session.captureFrames = 4;

if (!session.disableMicrophone()) throw new Error('active capture was not reported');
if (!trackStopped || !sourceDisconnected || !captureDisconnected || !sinkDisconnected || !capturePortClosed) {
  throw new Error('capture resources were not fully released');
}
if (capturePort.onmessage !== null) throw new Error('capture port handler was retained');
if (session.stream || session.source || session.captureNode || session.captureSink || session.hasMicrophone) {
  throw new Error('capture references survived disableMicrophone');
}
if (session.context !== context || session.playbackNode !== playbackNode || playbackDisconnected || session.closed) {
  throw new Error('playback resources were incorrectly closed with the microphone');
}
if (finalInputLevel !== 0 || session.captureFrames !== 0 || session.selectedDeviceId !== '') {
  throw new Error('capture state was not reset');
}
if (!states.some((event) => event.type === 'microphone-disabled' && event.state === 'disabled')) {
  throw new Error(`missing microphone-disabled event: ${JSON.stringify(states)}`);
}
"""
    completed = subprocess.run(
        ["node", "-e", script, str(audio_session)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
