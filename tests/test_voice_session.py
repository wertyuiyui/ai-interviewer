from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice import OmniRealtimeClient  # noqa: E402
from app.voice_session import BrowserVoiceSession, logger as voice_logger  # noqa: E402
from app.errors import AppError  # noqa: E402


async def wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.001)


class HandshakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.incoming.put_nowait(
            json.dumps({"type": "session.created", "session": {"id": "session-1"}})
        )
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, bytes):
            return
        event = json.loads(message)
        if event.get("type") == "session.update":
            await self.incoming.put(
                json.dumps(
                    {"type": "session.updated", "session": event["session"]}
                )
            )

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event_type: str, **payload: Any) -> None:
        self.events.append({"type": event_type, **payload})

    def first(self, event_type: str) -> dict[str, Any] | None:
        return next(
            (event for event in self.events if event["type"] == event_type),
            None,
        )


class BlockingAudioRecorder(EventRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.audio_send_started = asyncio.Event()
        self.allow_audio_send = asyncio.Event()

    async def __call__(self, event_type: str, **payload: Any) -> None:
        if event_type == "audio.chunk":
            self.audio_send_started.set()
            await self.allow_audio_send.wait()
        await super().__call__(event_type, **payload)


class FakeDatabase:
    def __init__(self, turn_count: int = 0) -> None:
        self.last_questions: list[tuple[str, str]] = []
        self.turn_count = turn_count

    async def set_last_question(self, interview_id: str, question: str) -> None:
        self.last_questions.append((interview_id, question))

    async def list_turns(self, _interview_id: str) -> list[object]:
        return [object() for _ in range(self.turn_count)]

    async def set_voice_mode(self, _interview_id: str, _voice_mode: str) -> None:
        return None


class FakeEngine:
    async def answer(self, _interview_id: str, _text: str) -> Any:
        return SimpleNamespace(
            turn=SimpleNamespace(ordinal=1),
            question="请继续解释 Redis 的过期删除策略。",
            pressure_action="chain",
            silence_seconds=0,
            ended=False,
            end_reason=None,
        )


class BilingualFakeEngine:
    async def answer(self, _interview_id: str, _text: str) -> Any:
        return SimpleNamespace(
            turn=SimpleNamespace(ordinal=1),
            question="请解释 MySQL InnoDB 的 MVCC，并说明 Redis cache miss 的处理。",
            pressure_action="chain",
            silence_seconds=0,
            ended=False,
            end_reason=None,
        )


class TimeoutEngine:
    async def answer(self, _interview_id: str, _text: str) -> Any:
        raise AppError("INTERVIEW_TIMEOUT", "面试时间已到", status_code=409)


class FakeOmni:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent_text: list[tuple[str, bool]] = []
        self.sent_audio: list[bytes] = []
        self.cancel_calls = 0
        self.closed = False
        self.control_response_id = "response-controlled"
        self.control_sent = asyncio.Event()

    async def send_text(self, text: str, *, create_response: bool = True) -> None:
        self.sent_text.append((text, create_response))
        if create_response:
            self.control_sent.set()
            await self.incoming.put(
                {
                    "type": "response_started",
                    "response_id": self.control_response_id,
                    "status": "in_progress",
                }
            )

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def cancel(self) -> None:
        self.cancel_calls += 1

    async def emit(self, event: dict[str, Any]) -> None:
        await self.incoming.put(event)

    async def events(self):
        while True:
            event = await self.incoming.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.incoming.put(None)


class FailingSendOmni(FakeOmni):
    async def send_audio(self, _pcm: bytes) -> None:
        raise RuntimeError("synthetic websocket disconnect")


class SlowCloseOmni(FakeOmni):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_calls = 0
        self.close_cancelled = False

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            await self.allow_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        await super().close()


class CancellationSensitiveOmni(FakeOmni):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.transport_closed = False
        self.close_cancelled = False

    async def close(self) -> None:
        self.close_started.set()
        try:
            await self.allow_close.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.transport_closed = True
        await super().close()


class FakeTTS:
    async def synthesize(self, text: str) -> tuple[bytes, str]:
        return f"pcm:{text}".encode(), "audio/pcm;rate=24000;channels=1"


class FailingTTS:
    async def synthesize(self, _text: str) -> tuple[bytes, str]:
        from app.voice import VoiceTransportError

        raise VoiceTransportError("synthetic TTS outage")


class BlockingTTS:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def synthesize(self, _text: str) -> tuple[bytes, str]:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class CancellationResistantTTS:
    """Models a provider operation that consumes task cancellation late."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancellation_received = asyncio.Event()
        self.allow_return = asyncio.Event()

    async def synthesize(self, _text: str) -> tuple[bytes, str]:
        self.started.set()
        try:
            await self.allow_return.wait()
        except asyncio.CancelledError:
            self.cancellation_received.set()
            await self.allow_return.wait()
        return b"late-fallback-audio", "audio/mpeg"


class BlockingCorrectionEngine:
    def __init__(self) -> None:
        self.answer_started = asyncio.Event()
        self.allow_answer = asyncio.Event()
        self.corrected_ordinals: list[int] = []
        self.preserved_answers: list[dict[str, Any]] = []

    async def answer(self, _interview_id: str, _text: str) -> Any:
        self.answer_started.set()
        await self.allow_answer.wait()
        return SimpleNamespace(
            turn=SimpleNamespace(ordinal=7),
            question="请继续说明事务隔离级别。",
            pressure_action=None,
            silence_seconds=0,
            ended=False,
            end_reason=None,
        )

    async def correct_answer(
        self,
        _interview_id: str,
        *,
        ordinal: int,
        text: str,
    ) -> dict[str, Any]:
        self.corrected_ordinals.append(ordinal)
        return {
            "ordinal": ordinal,
            "original_text": "原始转写",
            "answer": text,
        }

    async def preserve_unscored_answer(
        self,
        _interview_id: str,
        text: str,
        **metadata: Any,
    ) -> Any:
        self.preserved_answers.append({"text": text, **metadata})
        return SimpleNamespace(ordinal=metadata.get("ordinal") or 1)


def make_session(
    recorder: EventRecorder,
    *,
    engine: Any | None = None,
    database: Any | None = None,
) -> BrowserVoiceSession:
    async def on_end(_reason: str) -> None:
        return None

    return BrowserVoiceSession(
        interview_id="a" * 32,
        interview={"system_prompt": "你是后端技术面试官。"},
        settings=SimpleNamespace(voice_mode="L0", voice_auto_fallback=False),
        db=database or FakeDatabase(),
        engine=engine or FakeEngine(),
        send=recorder,
        on_end=on_end,
    )


def test_l0_session_enables_asr_and_disables_automatic_response() -> None:
    async def scenario() -> None:
        websocket = HandshakeWebSocket()

        async def factory(
            _url: str, _headers: Mapping[str, str]
        ) -> HandshakeWebSocket:
            return websocket

        with patch.dict(
            os.environ,
            {
                "OMNI_TRANSCRIPTION_MODEL": "qwen3-asr-flash-realtime",
                "VOICE_CONNECT_TIMEOUT_SECONDS": "1",
            },
            clear=False,
        ):
            client = OmniRealtimeClient(
                "你是后端技术面试官。", websocket_factory=factory
            )
            await client.start()

            sent = [
                json.loads(message)
                for message in websocket.sent
                if isinstance(message, str)
            ]
            update = next(event for event in sent if event["type"] == "session.update")
            session = update["session"]
            assert session["input_audio_transcription"] == {
                "model": "qwen3-asr-flash-realtime"
            }
            assert session["turn_detection"]["create_response"] is False
            assert session["turn_detection"]["interrupt_response"] is True
            assert session["turn_detection"]["threshold"] == 0.2
            assert session["turn_detection"]["prefix_padding_ms"] == 300
            assert session["turn_detection"]["silence_duration_ms"] == 1500

            await client.close()
            assert websocket.closed

    asyncio.run(scenario())


def test_typed_l0_answer_does_not_cancel_and_waits_for_matching_response_done() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        database = FakeDatabase()
        session = make_session(recorder, database=database)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session.provider_task = asyncio.create_task(session._consume_omni())

        await session.handle_text("Redis 使用惰性删除和定期删除。")
        await asyncio.wait_for(omni.control_sent.wait(), timeout=1)
        await wait_until(
            lambda: session._omni_active_response_id == omni.control_response_id
        )

        assert omni.cancel_calls == 0
        assert omni.sent_text[0] == (
            "Redis 使用惰性删除和定期删除。",
            False,
        )
        assert omni.sent_text[1][1] is True

        await omni.emit(
            {
                "type": "response_done",
                "response_id": "response-stale",
                "status": "completed",
            }
        )
        await wait_until(
            lambda: session._omni_response_events.get("response-stale") is not None
        )
        assert session.evaluation_tasks
        assert any(not task.done() for task in session.evaluation_tasks)
        assert recorder.first("interviewer.state") is not None
        assert not any(
            event.get("state") == "listening" for event in recorder.events
        )

        await omni.emit(
            {
                "type": "assistant_done",
                "response_id": omni.control_response_id,
                "text": "请继续解释 Redis 的过期删除策略。",
            }
        )
        await omni.emit(
            {
                "type": "response_done",
                "response_id": omni.control_response_id,
                "status": "completed",
            }
        )
        await wait_until(lambda: not session.evaluation_tasks)

        assert any(
            event.get("state") == "listening" for event in recorder.events
        )
        assert database.last_questions == [
            (session.interview_id, "请继续解释 Redis 的过期删除策略。")
        ]
        await session.close()

    asyncio.run(scenario())


def test_l0_browser_session_mixed_language_audio_to_next_spoken_question(
    caplog: Any,
) -> None:
    caplog.set_level("INFO", logger=voice_logger.name)

    async def scenario() -> None:
        recorder = EventRecorder()
        database = FakeDatabase()
        session = make_session(
            recorder, engine=BilingualFakeEngine(), database=database
        )
        session.interview["language_mode"] = "bilingual"
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session.provider_task = asyncio.create_task(session._consume_omni())

        pcm = b"\x10\x00" * 1600
        await session.handle_audio(pcm)
        assert omni.sent_audio == [pcm]
        input_level = recorder.first("audio.input.level")
        assert input_level is not None
        assert input_level["rms"] == 16
        assert input_level["window_peak_rms"] == 16
        assert input_level["peak_rms"] == 16
        assert input_level["frames"] == 1
        assert input_level["signal"] == "quiet"

        await omni.emit({"type": "speech_started", "item_id": "mixed-1"})
        await omni.emit(
            {
                "type": "user_partial",
                "item_id": "mixed-1",
                "text": "我会先检查 Redis cache",
                "language": "zh",
            }
        )
        await omni.emit({"type": "speech_ended", "item_id": "mixed-1"})
        await omni.emit(
            {
                "type": "user_done",
                "item_id": "mixed-1",
                "text": "我会先检查 Redis cache miss，再回源 MySQL。",
                "language": "zh",
            }
        )

        await asyncio.wait_for(omni.control_sent.wait(), timeout=1)
        await omni.emit(
            {
                "type": "assistant_done",
                "response_id": omni.control_response_id,
                "text": "请解释 MySQL InnoDB 的 MVCC，并说明 Redis cache miss 的处理。",
            }
        )
        await omni.emit(
            {
                "type": "audio_chunk",
                "response_id": omni.control_response_id,
                "audio": b"\x01\x00\x02\x00",
                "sample_rate": 24000,
            }
        )
        await omni.emit(
            {
                "type": "response_done",
                "response_id": omni.control_response_id,
                "status": "completed",
            }
        )
        await wait_until(lambda: not session.evaluation_tasks)

        assert recorder.first("candidate.transcript.partial") is not None
        assert recorder.first("candidate.transcript.done") is not None
        assert recorder.first("audio.chunk") is not None
        assert recorder.first("audio.stream.done") is not None
        assert database.last_questions == [
            (
                session.interview_id,
                "请解释 MySQL InnoDB 的 MVCC，并说明 Redis cache miss 的处理。",
            )
        ]
        spoken_control = omni.sent_text[-1][0]
        assert "允许自然地中英混读" in spoken_control
        assert "MySQL InnoDB" in spoken_control
        assert "不要翻译" in spoken_control
        await session.close()

    asyncio.run(scenario())
    assert "voice.transcript.done" in caplog.text
    assert "voice.tts.done" in caplog.text
    assert "我会先检查 Redis" not in caplog.text
    assert "请解释 MySQL InnoDB" not in caplog.text


def test_l0_blocks_paraphrased_audio_and_synthesizes_locked_display_text() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_expected_speech = "请解释 Redis 的过期删除策略。"
        session.provider_task = asyncio.create_task(session._consume_omni())

        with patch("app.voice_session.EdgeTTS", return_value=FakeTTS()):
            await omni.emit(
                {
                    "type": "response_started",
                    "response_id": "mismatch-response",
                    "status": "in_progress",
                }
            )
            await omni.emit(
                {
                    "type": "audio_chunk",
                    "response_id": "mismatch-response",
                    "audio": b"provider-paraphrase",
                    "sample_rate": 24000,
                }
            )
            await omni.emit(
                {
                    "type": "assistant_done",
                    "response_id": "mismatch-response",
                    "text": "我们换一道题吧。",
                }
            )
            await omni.emit(
                {
                    "type": "response_done",
                    "response_id": "mismatch-response",
                    "status": "completed",
                }
            )
            await wait_until(
                lambda: recorder.first("interviewer.audio.synced") is not None
            )

        synced = recorder.first("interviewer.audio.synced")
        assert synced is not None
        assert synced["text"] == "请解释 Redis 的过期删除策略。"
        assert synced["spoken_text"] == synced["text"]
        assert synced["audio_transcript"] == synced["text"]
        assert synced["fallback_tts"] is True
        assert synced["discarded_provider_transcript"] == "我们换一道题吧。"
        audio_event = recorder.first("audio.chunk")
        assert audio_event is not None
        played = base64.b64decode(audio_event["audio"])
        assert played == "pcm:请解释 Redis 的过期删除策略。".encode()
        assert played != b"provider-paraphrase"
        await session.close()

    asyncio.run(scenario())


def test_l0_response_waiter_wakes_only_after_verified_audio_is_sent() -> None:
    async def scenario() -> None:
        recorder = BlockingAudioRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_expected_speech = "请解释 Redis 的过期删除策略。"
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit(
            {
                "type": "response_started",
                "response_id": "ordered-response",
                "status": "in_progress",
            }
        )
        await omni.emit(
            {
                "type": "assistant_done",
                "response_id": "ordered-response",
                "text": "请解释 Redis 的过期删除策略。",
            }
        )
        await omni.emit(
            {
                "type": "audio_chunk",
                "response_id": "ordered-response",
                "audio": b"verified-audio",
                "sample_rate": 24000,
            }
        )
        await omni.emit(
            {
                "type": "response_done",
                "response_id": "ordered-response",
                "status": "completed",
            }
        )

        await asyncio.wait_for(recorder.audio_send_started.wait(), timeout=1)
        response_done = session._omni_response_events["ordered-response"]
        assert not response_done.is_set()

        recorder.allow_audio_send.set()
        await asyncio.wait_for(response_done.wait(), timeout=1)
        event_types = [event["type"] for event in recorder.events]
        assert event_types.index("audio.chunk") < event_types.index("audio.stream.done")
        await session.close()

    asyncio.run(scenario())


def test_l0_vad_cancels_verified_release_without_blocking_event_consumer() -> None:
    async def scenario() -> None:
        recorder = BlockingAudioRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_expected_speech = "请解释 Redis 的过期删除策略。"
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit(
            {
                "type": "response_started",
                "response_id": "vad-release",
                "status": "in_progress",
            }
        )
        await omni.emit(
            {
                "type": "assistant_done",
                "response_id": "vad-release",
                "text": "请解释 Redis 的过期删除策略。",
            }
        )
        await omni.emit(
            {
                "type": "audio_chunk",
                "response_id": "vad-release",
                "audio": b"verified-audio",
                "sample_rate": 24000,
            }
        )
        await omni.emit(
            {
                "type": "response_done",
                "response_id": "vad-release",
                "status": "completed",
            }
        )
        await asyncio.wait_for(recorder.audio_send_started.wait(), timeout=1)

        # This event is consumed by the same provider loop that previously
        # blocked while forwarding verified audio.
        await omni.emit({"type": "speech_started", "item_id": "next-answer"})
        await wait_until(
            lambda: recorder.first("input.speech_started") is not None
        )
        await asyncio.wait_for(
            session._omni_response_events["vad-release"].wait(), timeout=1
        )

        event_types = [event["type"] for event in recorder.events]
        assert "audio.chunk" not in event_types
        assert "interviewer.audio.synced" not in event_types
        assert "audio.stream.done" not in event_types
        assert "audio.clear" in event_types
        assert omni.cancel_calls == 0
        assert session._omni_release_tasks == {}
        await session.close()

    asyncio.run(scenario())


def test_typed_barge_in_drops_late_fallback_audio_after_cancellation() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        fallback_tts = CancellationResistantTTS()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_expected_speech = "请解释 Redis 的过期删除策略。"
        session.provider_task = asyncio.create_task(session._consume_omni())

        with patch("app.voice_session.EdgeTTS", return_value=fallback_tts):
            await omni.emit(
                {
                    "type": "response_started",
                    "response_id": "fallback-release",
                    "status": "in_progress",
                }
            )
            await omni.emit(
                {
                    "type": "assistant_done",
                    "response_id": "fallback-release",
                    "text": "这段内容被模型改写了。",
                }
            )
            await omni.emit(
                {
                    "type": "response_done",
                    "response_id": "fallback-release",
                    "status": "completed",
                }
            )
            await asyncio.wait_for(fallback_tts.started.wait(), timeout=1)

            interrupt = asyncio.create_task(session._interrupt_for_typed_input())
            await asyncio.wait_for(
                fallback_tts.cancellation_received.wait(), timeout=1
            )
            fallback_tts.allow_return.set()
            await asyncio.wait_for(interrupt, timeout=1)

        await asyncio.wait_for(
            session._omni_response_events["fallback-release"].wait(), timeout=1
        )
        event_types = [event["type"] for event in recorder.events]
        assert "audio.chunk" not in event_types
        assert "audio.file" not in event_types
        assert "interviewer.audio.synced" not in event_types
        assert "audio.stream.done" not in event_types
        assert session._drop_omni_audio is True
        assert session._omni_release_tasks == {}
        # response.done already cleared the active provider response, so typed
        # barge-in only needs to cancel the local verified/fallback release.
        assert omni.cancel_calls == 0
        await session.close()

    asyncio.run(scenario())


def test_l0_transcription_failure_is_recoverable_and_clears_partial() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit({"type": "speech_started", "item_id": "failed-1"})
        await omni.emit(
            {"type": "user_partial", "item_id": "failed-1", "text": "Redis"}
        )
        await omni.emit(
            {
                "type": "transcription_error",
                "item_id": "failed-1",
                "code": "transcription_error",
            }
        )
        await wait_until(
            lambda: recorder.first("candidate.transcript.failed") is not None
        )

        assert session.actual_mode == "L0"
        assert session.evaluation_tasks == set()
        error = recorder.first("error")
        assert error is not None
        assert error["code"] == "ASR_TRANSCRIPTION_FAILED"
        assert error["recoverable"] is True
        await session.close()

    asyncio.run(scenario())


def test_l0_duplicate_completed_item_is_scored_only_once() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        # Keep this test focused on provider de-duplication instead of waiting
        # for a generated follow-up response.
        session._answer_pending = True
        session.provider_task = asyncio.create_task(session._consume_omni())

        completed = {
            "type": "user_done",
            "item_id": "duplicate-item-1",
            "text": "Redis 使用惰性删除和定期删除。",
        }
        await omni.emit(completed)
        await omni.emit(completed)
        await wait_until(lambda: session._transcript_done_count == 1)
        await asyncio.sleep(0.01)

        assert sum(
            event["type"] == "candidate.transcript.done"
            for event in recorder.events
        ) == 1
        assert session._completed_transcription_item_ids == {"duplicate-item-1"}
        await session.close()

    asyncio.run(scenario())


def test_voice_item_uses_committed_ordinal_for_correction_not_prediction() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        database = FakeDatabase(turn_count=4)
        engine = BlockingCorrectionEngine()
        session = make_session(recorder, engine=engine, database=database)
        asr = FakeOmni()
        session.actual_mode = "L3"
        session.asr = asr  # type: ignore[assignment]
        session.provider_task = asyncio.create_task(session._consume_asr())

        await asr.emit(
            {
                "type": "user_done",
                "item_id": "correction-item",
                "text": "MySQL 默认是可重复读。",
            }
        )
        await asyncio.wait_for(engine.answer_started.wait(), timeout=1)

        displayed = recorder.first("candidate.transcript.done")
        assert displayed is not None and displayed["ordinal"] == 5
        assert session._transcript_items["correction-item"]["ordinal"] is None

        correction = asyncio.create_task(
            session.handle_transcript_correction(
                text="MySQL InnoDB 默认是可重复读。",
                ordinal=5,
                item_id="correction-item",
            )
        )
        await asyncio.sleep(0)
        assert not correction.done()

        engine.allow_answer.set()
        await asyncio.wait_for(correction, timeout=1)

        assert engine.corrected_ordinals == [7]
        item = session._transcript_items["correction-item"]
        assert item["ordinal"] == 7
        assert item["persisted"] is True
        await session.close()

    asyncio.run(scenario())


def test_rejected_voice_evaluation_discards_uncommitted_item() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        session._answer_pending = True
        session._transcript_items["overlap-item"] = {
            "ordinal": None,
            "text": "重叠回答",
            "original_text": "重叠回答",
        }

        accepted = await session._schedule_evaluation(
            session._pipeline_answer("重叠回答", item_id="overlap-item"),
            item_id="overlap-item",
        )

        assert accepted is False
        assert "overlap-item" not in session._transcript_items
        failed = recorder.first("candidate.transcript.failed")
        assert failed is not None and failed["item_id"] == "overlap-item"
        error = recorder.first("error")
        assert error is not None and error["code"] == "ANSWER_IN_PROGRESS"
        session._answer_pending = False
        await session.close()

    asyncio.run(scenario())


def test_correction_reports_timeout_instead_of_using_client_ordinal() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        engine = BlockingCorrectionEngine()
        session = make_session(recorder, engine=engine)
        session._transcript_items["pending-item"] = {
            "ordinal": None,
            "text": "待处理转写",
            "original_text": "待处理转写",
        }

        async def timed_out(*, timeout: float = 7.0) -> bool:
            assert timeout == 15.0
            return False

        session._wait_for_current_evaluation = timed_out  # type: ignore[method-assign]
        try:
            await session.handle_transcript_correction(
                text="修正后的转写",
                ordinal=99,
                item_id="pending-item",
            )
        except AppError as exc:
            assert exc.code == "TRANSCRIPT_NOT_PERSISTED"
            assert exc.status_code == 409
        else:
            raise AssertionError("a correction timeout must be reported")

        assert engine.corrected_ordinals == []
        await session.close()

    asyncio.run(scenario())


def test_prepare_end_preserves_accepted_voice_answer_when_scoring_times_out() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        engine = BlockingCorrectionEngine()
        session = make_session(recorder, engine=engine)
        session._transcript_items["final-voice-item"] = {
            "ordinal": None,
            "predicted_ordinal": 1,
            "text": "这是结束前已经完成转写的回答。",
            "original_text": "这是结束前已经完成转写的回答。",
            "input_mode": "voice",
            "answer_duration_seconds": 3.5,
        }
        accepted = await session._schedule_evaluation(
            session._pipeline_answer(
                "这是结束前已经完成转写的回答。",
                input_mode="voice",
                item_id="final-voice-item",
                answer_duration_seconds=3.5,
            ),
            item_id="final-voice-item",
        )
        assert accepted is True
        await asyncio.wait_for(engine.answer_started.wait(), timeout=1)

        await session.prepare_end(drain_timeout=0.01)

        assert engine.preserved_answers == [
            {
                "text": "这是结束前已经完成转写的回答。",
                "input_mode": "voice",
                "answer_duration_seconds": 3.5,
                "ordinal": 1,
            }
        ]
        item = session._transcript_items["final-voice-item"]
        assert item["ordinal"] == 1
        assert item["persisted"] is True
        assert item["unscored"] is True
        assert recorder.first("candidate.transcript.failed") is None
        await session.close()

    asyncio.run(scenario())


def test_terminal_audio_waits_for_matching_browser_playback_ack() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        session.actual_mode = "L2"
        session.tts = FakeTTS()  # type: ignore[assignment]

        announcement = asyncio.create_task(
            session.announce(
                "今天的面试就到这里，感谢你的时间。",
                wait_for_playback=True,
            )
        )
        await wait_until(lambda: recorder.first("audio.stream.done") is not None)
        stream_done = recorder.first("audio.stream.done")
        assert stream_done is not None
        announcement_id = str(stream_done["announcement_id"])
        assert announcement_id
        assert not announcement.done()

        await session.handle_playback_done("a-different-announcement")
        await asyncio.sleep(0)
        assert not announcement.done()

        await session.handle_playback_done(announcement_id)
        await asyncio.wait_for(announcement, timeout=1)
        assert announcement_id not in session._playback_waiters

        audio_index = next(
            index
            for index, event in enumerate(recorder.events)
            if event["type"] == "audio.chunk"
        )
        done_index = recorder.events.index(stream_done)
        assert audio_index < done_index
        await session.close()

    asyncio.run(scenario())


def test_runtime_tts_failure_downgrades_to_text_mode() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        session.actual_mode = "L2"
        session.tts = FailingTTS()  # type: ignore[assignment]

        await session._speak("请解释缓存击穿。")

        assert session.actual_mode == "L3"
        changed = recorder.first("mode.changed")
        assert changed is not None
        assert changed["voice_mode"] == "L3"
        assert recorder.first("error") is not None
        await session.close()

    asyncio.run(scenario())


def test_stress_interrupt_speaks_while_candidate_is_still_talking() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        database = FakeDatabase(turn_count=2)
        session = make_session(recorder, database=database)
        session.interview["stress"] = True
        session.settings = SimpleNamespace(
            voice_mode="L2",
            voice_auto_fallback=False,
            pressure_interrupt_seconds=0,
        )
        session.actual_mode = "L2"
        session.tts = FakeTTS()  # type: ignore[assignment]

        await session._candidate_speech_started(source="silero")
        await wait_until(lambda: recorder.first("pressure.interrupt") is not None)
        await wait_until(
            lambda: any(event["type"] == "audio.chunk" for event in recorder.events)
        )

        interrupt = recorder.first("pressure.interrupt")
        assert interrupt is not None
        assert interrupt["ordinal"] == 3
        assert session._candidate_speaking is True
        assert "先停一下" in interrupt["text"]
        await session._candidate_speech_ended()
        await session.close()

    asyncio.run(scenario())


def test_answer_duration_stops_at_vad_end_not_transcription_completion() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)

        await session._candidate_speech_started(source="silero")
        await asyncio.sleep(0.01)
        await session._candidate_speech_ended()
        measured = session._speech_duration_seconds
        assert measured is not None and measured >= 0.005

        # ASR finalization can arrive later. A duplicate speech-ended call from
        # that final event must not add provider latency to the spoken duration.
        await asyncio.sleep(0.04)
        await session._candidate_speech_ended()
        assert session._speech_duration_seconds == measured
        await session.close()

    asyncio.run(scenario())


def test_parent_cancellation_is_not_swallowed_by_speak() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        tts = BlockingTTS()
        session.actual_mode = "L2"
        session.tts = tts  # type: ignore[assignment]

        parent = asyncio.create_task(session._speak("一个很长的问题"))
        await asyncio.wait_for(tts.started.wait(), timeout=1)
        parent.cancel()
        try:
            await parent
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("parent cancellation must propagate")
        assert parent.cancelled()
        await session.close()

    asyncio.run(scenario())


def test_voice_timeout_calls_terminal_callback() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder, engine=TimeoutEngine())
        reasons: list[str] = []

        async def on_end(reason: str) -> None:
            reasons.append(reason)

        session.on_end = on_end
        await session._pipeline_answer("这是超时边界上的回答")
        assert reasons == ["time"]
        await session.close()

    asyncio.run(scenario())


def test_failed_omni_response_downgrades_without_waiting_for_playback() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit(
            {"type": "response_started", "response_id": "failed-r"}
        )
        await omni.emit(
            {"type": "response_done", "response_id": "failed-r", "status": "failed"}
        )
        await wait_until(lambda: session.actual_mode == "L3")

        assert recorder.first("error") is not None
        changed = recorder.first("mode.changed")
        assert changed is not None and changed["voice_mode"] == "L3"
        assert recorder.first("audio.stream.done") is None
        await session.close()

    asyncio.run(scenario())


def test_audio_send_disconnect_downgrades_to_l3() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FailingSendOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]

        await session.handle_audio(b"\x01\x00")

        assert session.actual_mode == "L3"
        assert recorder.first("mode.changed") is not None
        await session.close()

    asyncio.run(scenario())


def test_typed_input_cancels_active_voice_and_clears_browser_audio() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_active_response_id = "active-r"

        await session._interrupt_for_typed_input()

        assert omni.cancel_calls == 1
        assert session._drop_omni_audio is True
        assert recorder.first("audio.clear") is not None
        await session.close()

    asyncio.run(scenario())


def test_completed_done_then_no_active_cancel_error_stays_on_l0() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_active_response_id = "old-r"
        session._begin_omni_cancel()
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit(
            {"type": "response_done", "response_id": "old-r", "status": "completed"}
        )
        await wait_until(lambda: session._omni_active_response_id is None)
        assert session._omni_cancel_in_flight is True
        await omni.emit(
            {
                "type": "error",
                "code": "invalid_request_error",
                "message": "response.cancel failed: no active response",
            }
        )
        await wait_until(lambda: session._omni_cancel_in_flight is False)

        assert session.actual_mode == "L0"
        assert recorder.first("mode.changed") is None
        await session.close()

    asyncio.run(scenario())


def test_delayed_cancel_error_after_next_response_started_is_still_benign() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = FakeOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]
        session._omni_active_response_id = "old-r"
        session._begin_omni_cancel()
        session.provider_task = asyncio.create_task(session._consume_omni())

        await omni.emit(
            {"type": "response_done", "response_id": "old-r", "status": "completed"}
        )
        await omni.emit(
            {"type": "response_started", "response_id": "new-r"}
        )
        await wait_until(lambda: session._omni_active_response_id == "new-r")
        assert session._omni_cancel_in_flight is False
        assert session._omni_cancel_error_tokens == 1
        await omni.emit(
            {
                "type": "error",
                "code": "invalid_request_error",
                "message": "response.cancel failed: no active response",
            }
        )
        await wait_until(lambda: session._omni_cancel_error_tokens == 0)

        assert session.actual_mode == "L0"
        assert recorder.first("mode.changed") is None
        await session.close()

    asyncio.run(scenario())


def test_concurrent_runtime_fallback_has_one_transport_close_owner() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = SlowCloseOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]

        first = asyncio.create_task(session._runtime_fallback("first failure"))
        await asyncio.wait_for(omni.close_started.wait(), timeout=1)
        second = asyncio.create_task(session._runtime_fallback("second failure"))
        await asyncio.sleep(0)
        omni.allow_close.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        assert session.actual_mode == "L3"
        assert omni.close_calls == 1
        assert omni.close_cancelled is False
        assert sum(event["type"] == "mode.changed" for event in recorder.events) == 1
        await session.close()

    asyncio.run(scenario())


def test_cancelled_fallback_still_finishes_provider_transport_close() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        session = make_session(recorder)
        omni = CancellationSensitiveOmni()
        session.actual_mode = "L0"
        session.omni = omni  # type: ignore[assignment]

        fallback = asyncio.create_task(session._runtime_fallback("provider failed"))
        await asyncio.wait_for(omni.close_started.wait(), timeout=1)
        fallback.cancel()
        await asyncio.sleep(0)
        omni.allow_close.set()
        try:
            await fallback
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("fallback owner cancellation must propagate")

        assert omni.transport_closed is True
        assert omni.close_cancelled is False
        assert session.omni is None
        await session.close()
        assert session.closed is True

    asyncio.run(scenario())
