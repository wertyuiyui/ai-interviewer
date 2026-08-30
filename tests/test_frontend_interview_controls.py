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
    script = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")
    assert "/js/interview.js?v=20260830-exit-v1" in html
    assert "/assets/app.css?v=20260830-stage-flow-v1" in html
    assert 'id="unknownButton"' in html
    assert 'id="advanceStageButton"' in html
    assert 'id="interviewStageList"' in html
    assert "面试进程" in html
    assert "interview.stage.advance" in script
    assert "interview.stage.changed" in script
    assert "renderInterviewStage" in script
    assert "control_intent" in script
    assert "进一步提示" in html
    assert "function submitUnknown()" in script
    assert "hintedQuestions = new Map()" in script


def test_resume_mismatch_can_exit_to_home_and_clear_selected_session() -> None:
    html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    for element_id in (
        "resumeMismatchDialog",
        "resumeMismatchMessage",
        "continueWithResume",
        "exitForResume",
    ):
        assert f'id="{element_id}"' in html
    assert "是否选错了简历" in html
    assert "function maybeShowResumeMismatch" in script
    assert "event.resume_selection_warning === true" in script
    exit_block = script.split("function exitForResumeMismatch()", 1)[1].split(
        "function handleCaptureState", 1
    )[0]
    assert "discardInterview()" in exit_block
    assert "interview.end" not in exit_block


def test_exit_discards_interview_without_profile_history() -> None:
    html = (ROOT / "public" / "interview.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    assert 'id="exitButton"' in html
    assert "不生成报告" in html
    assert "不计入个人档案" in html
    discard = script.split("async function discardInterview()", 1)[1].split(
        "function submitText", 1
    )[0]
    assert "setCurrentSession(null)" in discard
    assert "/api/history/${encodeURIComponent(sessionId)}" in discard
    assert "method: 'DELETE'" in discard
    assert "type: 'interview.end'" not in discard
    assert "if (!/^[a-f0-9]{32}$/.test(sessionId))" in discard
    assert discard.index("/^[a-f0-9]{32}$/") < discard.index("await apiFetch")
    assert discard.index("await apiFetch") < discard.rindex("setCurrentSession(null)")
    assert "未能舍弃本场" in discard
    assert "location.replace('/')" in discard
    assert "elements.exitButton.addEventListener('click', discardInterview)" in script
