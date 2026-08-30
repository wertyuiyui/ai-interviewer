from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_explicit_per_question_answer_controls_contract() -> None:
    html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    for element_id in (
        "answerControl",
        "answerStateLabel",
        "answerElapsed",
        "startAnswerButton",
        "endAnswerButton",
    ):
        assert f'id="{element_id}"' in html

    start_block = script.split("function startCurrentAnswer()", 1)[1].split(
        "function finishCurrentAnswer", 1
    )[0]
    assert "type: 'answer.start'" in start_block
    assert "setAnswerState('answering', { resetElapsed: true })" in start_block

    end_block = script.split("function finishCurrentAnswer(", 1)[1].split(
        "async function requestHint", 1
    )[0]
    assert "type: 'answer.end', elapsed_ms: elapsedMs" in end_block
    assert "payload.text = text" in end_block
    assert end_block.index("setAnswerState('sealing'") < end_block.index("sendJson(payload)")

    uplink_block = script.split("function syncAudioUplink()", 1)[1].split(
        "async function refreshMicrophoneDevices", 1
    )[0]
    assert "answerState === 'answering'" in uplink_block

    speech_started = script.split("case 'input.speech_started':", 1)[1].split(
        "case 'audio.input.level'", 1
    )[0]
    assert "if (answerState !== 'answering') break" in speech_started

    submit_block = script.split("function submitText(event)", 1)[1].split(
        "async function toggleMicrophone", 1
    )[0]
    assert "finishCurrentAnswer()" in submit_block
    assert "user.text" not in submit_block

    finish_interview = script.split("async function finishInterview", 1)[1].split(
        "function submitText", 1
    )[0]
    assert "finishCurrentAnswer({ allowEmpty: true })" in finish_interview
    assert finish_interview.index("finishCurrentAnswer({ allowEmpty: true })") < finish_interview.index(
        "type: 'interview.end'"
    )

    start_error = script.split("case 'error':", 1)[1].split("if (event.fatal)", 1)[0]
    assert "if (answerStartAwaitingAck" in start_error
    assert "setAnswerState(questionReady ? 'ready' : 'idle'" in start_error


def test_interviewer_voice_playback_toggle_is_local_only() -> None:
    html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    assert 'id="interviewerVoiceToggle"' in html
    assert 'aria-pressed="true"' in html
    assert "INTERVIEWER_VOICE_KEY" in script

    toggle_block = script.split("function toggleInterviewerVoice()", 1)[1].split(
        "function setAnswerPending", 1
    )[0]
    assert "invalidateAudioPlayback()" in toggle_block
    assert "disableMicrophone" not in toggle_block
    assert "sendJson" not in toggle_block

    audio_chunk = script.split("case 'audio.chunk':", 1)[1].split(
        "case 'audio.file'", 1
    )[0]
    assert "noteInterviewerAudioStream()" in audio_chunk
    assert "!canPlayInterviewerAudio()" in audio_chunk

    stream_done = script.split("case 'audio.stream.done':", 1)[1].split(
        "case 'timer.sync'", 1
    )[0]
    assert "canPlayInterviewerAudio()" in stream_done
    assert "audio.playback.done" in stream_done
    assert "interviewerAudioSuppressedUntilStreamEnd = false" in stream_done


def test_interview_script_cache_busts_answer_controls() -> None:
    html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    assert "/js/interview.js?v=20260830-answer-controls" in html
    assert "/assets/app.css?v=20260830-answer-controls" in html
