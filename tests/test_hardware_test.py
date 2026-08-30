from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.hardware_test import (
    HARDWARE_TEST_MAX_SECONDS,
    HARDWARE_TEST_TAIL_FRAMES,
    HardwareTranscriptionSession,
)


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event_type: str, **payload: Any) -> None:
        self.events.append({"type": event_type, **payload})

    def matching(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == event_type]


class FakeProvider:
    instances: list["FakeProvider"] = []

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.events_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent_audio: list[bytes] = []
        self.start_calls = 0
        self.close_calls = 0
        self.closed = False
        type(self).instances.append(self)

    async def start(self) -> None:
        self.start_calls += 1

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def emit(self, event: dict[str, Any]) -> None:
        await self.events_queue.put(event)

    async def events(self):
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        await self.events_queue.put(None)


class FakeOmni(FakeProvider):
    instances: list[FakeProvider] = []

    def __init__(self, instructions: str) -> None:
        super().__init__()
        self.instructions = instructions


class FakeParaformer(FakeProvider):
    instances: list[FakeProvider] = []


class FailingOmni(FakeOmni):
    instances: list[FakeProvider] = []

    async def start(self) -> None:
        self.start_calls += 1
        raise RuntimeError("synthetic Omni outage")


class FailingParaformer(FakeParaformer):
    instances: list[FakeProvider] = []

    async def start(self) -> None:
        self.start_calls += 1
        raise RuntimeError("synthetic Paraformer outage")


async def wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.001)


def settings(mode: str = "L0", *, fallback: bool = False) -> SimpleNamespace:
    return SimpleNamespace(voice_mode=mode, voice_auto_fallback=fallback)


def reset_fake_instances() -> None:
    for provider in (FakeOmni, FakeParaformer, FailingOmni, FailingParaformer):
        provider.instances.clear()


def test_l0_ready_and_transcription_events_are_forwarded() -> None:
    async def scenario() -> None:
        reset_fake_instances()
        recorder = EventRecorder()
        with patch("app.hardware_test.OmniRealtimeClient", FakeOmni):
            session = HardwareTranscriptionSession(settings(), recorder)
            assert await session.start() is True

            provider = FakeOmni.instances[0]
            assert provider.start_calls == 1
            assert "中英文实时转写" in provider.instructions
            assert session.actual_mode == "L0"
            assert recorder.events[0] == {
                "type": "hardware.ready",
                "transcription_available": True,
                "max_seconds": HARDWARE_TEST_MAX_SECONDS,
            }

            await provider.emit({"type": "speech_started"})
            await provider.emit({"type": "user_partial", "text": "我负责 Redis"})
            await provider.emit({"type": "speech_ended"})
            await provider.emit(
                {"type": "user_done", "text": "  我负责 Redis 缓存一致性。  "}
            )
            await wait_until(
                lambda: bool(recorder.matching("hardware.transcript.done"))
            )

            assert [event["type"] for event in recorder.events[1:]] == [
                "hardware.speech.started",
                "hardware.transcript.partial",
                "hardware.speech.ended",
                "hardware.transcript.done",
            ]
            assert recorder.matching("hardware.transcript.partial")[0]["text"] == (
                "我负责 Redis"
            )
            assert recorder.matching("hardware.transcript.done")[0]["text"] == (
                "我负责 Redis 缓存一致性。"
            )
            assert session.heard_speech is True
            assert session.final_transcript.is_set()

            await session.close()
            assert provider.closed is True
            assert provider.close_calls == 1

    asyncio.run(scenario())


def test_audio_frames_are_validated_truncated_at_limit_and_then_stopped() -> None:
    async def scenario() -> None:
        recorder = EventRecorder()
        provider = FakeOmni("test only")
        session = HardwareTranscriptionSession(settings(), recorder)
        session.actual_mode = "L0"
        session.omni = provider  # type: ignore[assignment]

        with patch("app.hardware_test.HARDWARE_TEST_MAX_PCM_BYTES", 8):
            await session.handle_audio(b"\x01")
            assert session.audio_bytes == 0
            assert provider.sent_audio == []
            assert recorder.matching("hardware.error") == [
                {
                    "type": "hardware.error",
                    "code": "INVALID_AUDIO_FRAME",
                    "message": "麦克风测试音频格式不正确",
                    "recoverable": True,
                }
            ]

            await session.handle_audio(b"123456")
            await session.handle_audio(b"abcdef")
            assert session.audio_bytes == 8
            assert provider.sent_audio[:2] == [b"123456", b"ab"]

            await session.handle_audio(b"zz")
            assert session.stopped is True
            assert recorder.matching("hardware.stopped") == [
                {"type": "hardware.stopped", "reason": "limit"}
            ]
            # Reaching the cap never forwards another caller-supplied frame.
            # The remaining frames are bounded tail silence used to flush ASR.
            assert len(provider.sent_audio) == 2 + HARDWARE_TEST_TAIL_FRAMES
            assert all(
                frame == b"\x00\x00" * 1600 for frame in provider.sent_audio[2:]
            )

            await session.handle_audio(b"more")
            assert len(provider.sent_audio) == 2 + HARDWARE_TEST_TAIL_FRAMES

    asyncio.run(scenario())


def test_all_providers_unavailable_falls_back_to_l3_without_network() -> None:
    async def scenario() -> None:
        reset_fake_instances()
        recorder = EventRecorder()
        with (
            patch("app.hardware_test.OmniRealtimeClient", FailingOmni),
            patch("app.hardware_test.ParaformerClient", FailingParaformer),
        ):
            session = HardwareTranscriptionSession(
                settings("L0", fallback=True), recorder
            )
            assert await session.start() is False

        assert len(FailingOmni.instances) == 1
        # L1 and L2 share one pipeline provider, so a failed L1 must not open a
        # duplicate external connection for L2.
        assert len(FailingParaformer.instances) == 1
        assert FailingOmni.instances[0].closed is True
        assert FailingParaformer.instances[0].closed is True
        assert session.actual_mode == "L3"
        assert session.transcription_available is False
        assert session.omni is None and session.asr is None
        assert session.provider_task is None
        assert recorder.events == [
            {
                "type": "hardware.ready",
                "transcription_available": False,
                "max_seconds": HARDWARE_TEST_MAX_SECONDS,
            }
        ]

        await session.handle_audio(b"\x00\x00")
        assert session.audio_bytes == 2
        await session.close()
        assert session.closed is True

    asyncio.run(scenario())


def test_stop_flushes_final_transcript_and_close_is_idempotent() -> None:
    async def scenario() -> None:
        reset_fake_instances()
        recorder = EventRecorder()
        with patch("app.hardware_test.OmniRealtimeClient", FakeOmni):
            session = HardwareTranscriptionSession(settings(), recorder)
            await session.start()
            provider = FakeOmni.instances[0]

            await provider.emit({"type": "speech_started"})
            await wait_until(lambda: session.heard_speech)
            stop_task = asyncio.create_task(session.stop(reason="manual"))
            await wait_until(
                lambda: len(provider.sent_audio) == HARDWARE_TEST_TAIL_FRAMES
            )
            assert stop_task.done() is False

            await provider.emit(
                {"type": "user_done", "text": "项目峰值 QPS 是三千。"}
            )
            await asyncio.wait_for(stop_task, timeout=1)

            event_types = [event["type"] for event in recorder.events]
            assert event_types.index("hardware.transcript.done") < event_types.index(
                "hardware.stopped"
            )
            assert recorder.matching("hardware.stopped") == [
                {"type": "hardware.stopped", "reason": "manual"}
            ]

            await session.stop(reason="duplicate")
            assert len(recorder.matching("hardware.stopped")) == 1
            await session.close()
            await session.close()
            assert session.closed is True
            assert provider.close_calls == 1
            assert session.omni is None
            assert session.provider_task is None

    asyncio.run(scenario())
