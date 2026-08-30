from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_home_interview_type_is_saved_and_sent() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")

    assert 'name="interview_type" value="technical"' in html
    assert 'name="interview_type" value="technical_hr"' in html
    assert "function getInterviewType()" in script
    assert "saved.interview_type" in script
    # The same value must be present in the create payload, current-session
    # handoff, and saved setup rather than being a display-only control.
    assert script.count("interview_type: interviewType") == 3


def test_optional_home_hardware_test_contract() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "hardware-test.js").read_text(encoding="utf-8")
    home = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")

    for element_id in (
        "hardwareTestCard",
        "hardwareTestButton",
        "hardwareTestStatus",
        "hardwareTestDevice",
        "hardwareInputMeter",
        "hardwareTestCountdown",
        "hardwareTranscript",
    ):
        assert f'id="{element_id}"' in html

    assert "new AudioSession" in script
    assert "/ws/hardware-test" in script
    assert "type: 'client.ready', client_id: getClientId()" in script
    assert "this.socket.send(buffer)" in script
    assert "hardware.speech.started" in script
    assert "hardware.speech.ended" in script
    assert "hardware.transcript.partial" in script
    assert "hardware.transcript.done" in script
    assert "type: 'hardware.stop'" in script
    assert "type === 'hardware.stopped'" in script
    assert "TEST_SECONDS = 30" in script
    assert "await audio.close()" in script
    assert "SpeechRecognition" not in script
    assert "webkitSpeechRecognition" not in script

    # Starting the interview must close a running preflight test, and leaving
    # the page must release capture even if navigation interrupts async work.
    start_block = home.split("async function startInterview", 1)[1].split("$$('[data-resume-tab]')", 1)[0]
    assert "await hardwareTest?.stop({ immediate: true, quiet: true })" in start_block
    assert "pagehide" in home and "hardwareTest?.dispose()" in home

    stop_block = script.split("async _stop(", 1)[1].split("async _fail(", 1)[0]
    assert "this.audio?.disableMicrophone()" in stop_block
    assert stop_block.index("disableMicrophone()") < stop_block.index("type: 'hardware.stop'")
    assert "event.transcription_available === false" in script
    assert "event.recoverable === true" in script


def test_home_pressure_copy_describes_conditional_interruptions() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")

    assert "针对模糊、矛盾和技术漏洞连续下钻" in html
    assert "仅在明显跑题或表述失控时打断" in script
    assert "更频繁打断" not in script


def test_home_can_create_a_persistent_microphone_free_text_interview() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    home = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")
    interview = (ROOT / "public" / "js" / "interview.js").read_text(encoding="utf-8")

    assert 'name="answer_mode" value="voice"' in html
    assert 'name="answer_mode" value="text"' in html
    assert 'id="hardwareTestSection"' in html
    assert "/js/home.js?v=20260830-resume-merge-v1" in html
    assert "function getAnswerMode()" in home
    assert "serverMode === 'L3' ? 'text' : preferredAnswerMode" in home
    assert home.count("answer_mode: answerMode") == 2
    assert "answer_mode: session?.answer_mode" in home
    assert "hardwareTestSection?.classList.toggle('is-hidden', textOnly)" in home
    assert "hardwareTestButton.disabled = textOnly" in home
    assert "hardwareTest?.stop({ immediate: true, quiet: true })" in home

    join_block = interview.split("async function joinInterview", 1)[1].split("async function finishInterview", 1)[0]
    assert "if (voiceMode !== 'L3')" in join_block
    assert "initializeAudio(true" in join_block


def test_saved_resume_can_be_selected_directly_in_home_setup() -> None:
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    home = (ROOT / "public" / "js" / "home.js").read_text(encoding="utf-8")

    assert 'id="savedResumeSelect"' in html
    assert 'for="savedResumeSelect"' in html
    assert "直接在首页选择；个人档案只用于添加或管理资料" not in html
    assert 'data-resume-tab="saved"' not in html
    assert 'data-resume-tab="pdf"' not in html
    assert 'data-resume-tab="resume"' in html
    assert 'id="savedPanel"' not in html
    assert 'id="pdfPanel"' not in html
    resume_panel = html.split('id="resumePanel"', 1)[1].split('id="textPanel"', 1)[0]
    assert resume_panel.index('id="savedResumeSelect"') < resume_panel.index('id="resumeFile"')
    assert "function renderSavedResumeOptions()" in home
    assert "readyResumes.forEach((resume)" in home
    assert "savedResumeSelect?.addEventListener('change'" in home
    assert "selectSavedResume(savedResumeSelect.value)" in home
    assert "resumeMode === 'resume' ? (selectedResumeId ? savedResumeSelect : fileInput)" in home
    assert "resumeMode === 'resume' && !selectedFile" in home
    assert "selectSavedResume('', { switchMode: false })" in home
