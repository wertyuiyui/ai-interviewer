from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice import (  # noqa: E402
    CosyVoiceTTS,
    EdgeTTS,
    OmniRealtimeClient,
    ParaformerClient,
    SileroVAD,
    VoiceTransportError,
    normalize_omni_event,
    normalize_paraformer_event,
)


class FakeWebSocket:
    def __init__(self, initial: list[dict] | None = None) -> None:
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False
        for item in initial or []:
            self.incoming.put_nowait(json.dumps(item))

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, bytes):
            return
        event = json.loads(message)
        if event.get("type") == "session.update":
            await self.incoming.put(
                json.dumps({"type": "session.updated", "session": event["session"]})
            )
        header = event.get("header") or {}
        if header.get("action") == "run-task":
            await self.incoming.put(
                json.dumps(
                    {
                        "header": {
                            "event": "task-started",
                            "task_id": header["task_id"],
                            "attributes": {},
                        },
                        "payload": {},
                    }
                )
            )
        elif header.get("action") == "finish-task":
            await self.incoming.put(
                json.dumps(
                    {
                        "header": {
                            "event": "task-finished",
                            "task_id": header["task_id"],
                            "attributes": {},
                        },
                        "payload": {"output": {}, "usage": None},
                    }
                )
            )

    async def recv(self) -> object:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def test_normalize_omni_events() -> None:
    partial = normalize_omni_event(
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "text": "Redis",
            "stash": "为什么快？",
            "item_id": "u1",
        }
    )
    assert partial == [
        {
            "provider_event_id": None,
            "type": "user_partial",
            "text": "Redis为什么快？",
            "item_id": "u1",
            "language": None,
            "emotion": None,
        }
    ]

    stopped = normalize_omni_event(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "u1",
            "audio_end_ms": 980,
        }
    )
    assert stopped == [
        {
            "provider_event_id": None,
            "type": "speech_ended",
            "item_id": "u1",
            "audio_end_ms": 980,
        }
    ]

    pcm = b"\x01\x00\x02\x00"
    audio = normalize_omni_event(
        {"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()}
    )[0]
    assert audio["type"] == "audio_chunk"
    assert audio["audio"] == pcm
    assert audio["sample_rate"] == 24000
    assert audio["encoding"] == "pcm_s16le"

    tool = normalize_omni_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "record_score",
            "call_id": "call-1",
            "arguments": '{"score": 3}',
        }
    )[0]
    assert tool["type"] == "tool_call"
    assert tool["arguments"] == {"score": 3}


def test_omni_client_handshake_and_commands_are_offline_testable() -> None:
    async def scenario() -> None:
        ws = FakeWebSocket([{"type": "session.created", "session": {"id": "s1"}}])
        connection: dict[str, object] = {}

        async def factory(url: str, headers: dict[str, str]) -> FakeWebSocket:
            connection.update(url=url, headers=headers)
            return ws

        with patch.dict(
            os.environ,
            {
                "OMNI_REALTIME_URL": "wss://example.invalid/realtime?trace=1",
                "OMNI_REALTIME_MODEL": "qwen3.5-omni-flash-realtime-test",
                "OMNI_OUTPUT_SAMPLE_RATE": "24000",
            },
            clear=False,
        ):
            client = OmniRealtimeClient("你是技术面试官", websocket_factory=factory)
            await client.start()
            await client.send_audio(b"\x01\x00")
            await client.send_text("我会用 Redis。")
            await client.cancel()

            assert "model=qwen3.5-omni-flash-realtime-test" in str(connection["url"])
            sent_json = [json.loads(item) for item in ws.sent if isinstance(item, str)]
            session = next(item for item in sent_json if item["type"] == "session.update")
            assert session["session"]["audio"]["input"]["format"] == {
                "type": "pcm",
                "sample_rate": 16000,
            }
            append = next(item for item in sent_json if item["type"] == "input_audio_buffer.append")
            assert base64.b64decode(append["audio"]) == b"\x01\x00"
            assert [item["type"] for item in sent_json[-3:]] == [
                "conversation.item.create",
                "response.create",
                "response.cancel",
            ]

            await ws.incoming.put(
                json.dumps(
                    {
                        "type": "input_audio_buffer.speech_started",
                        "item_id": "u1",
                        "audio_start_ms": 20,
                    }
                )
            )
            normalized = await asyncio.wait_for(anext(client.events()), timeout=1)
            assert normalized["type"] == "speech_started"
            assert normalized["item_id"] == "u1"
            await client.close()
            assert ws.closed
            try:
                await client.start()
            except VoiceTransportError:
                pass
            else:
                raise AssertionError("closed Omni client must reject restart")

    asyncio.run(scenario())


def test_omni_start_cancellation_closes_transport() -> None:
    async def scenario() -> None:
        ws = FakeWebSocket()
        connected = asyncio.Event()

        async def factory(_url: str, _headers: dict[str, str]) -> FakeWebSocket:
            connected.set()
            return ws

        client = OmniRealtimeClient("你是技术面试官", websocket_factory=factory)
        start_task = asyncio.create_task(client.start())
        await asyncio.wait_for(connected.wait(), timeout=1)
        await asyncio.sleep(0)
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled start() must propagate CancelledError")
        assert ws.closed

    asyncio.run(scenario())


def test_paraformer_waits_for_task_started_and_sends_binary_audio() -> None:
    async def scenario() -> None:
        ws = FakeWebSocket()

        async def factory(_url: str, _headers: dict[str, str]) -> FakeWebSocket:
            return ws

        client = ParaformerClient(websocket_factory=factory)
        await client.start()
        await client.send_audio(b"\x00\x00" * 1600)

        first = json.loads(ws.sent[0])
        assert first["header"]["action"] == "run-task"
        assert first["payload"]["model"] == "paraformer-realtime-v2"
        assert first["payload"]["parameters"]["sample_rate"] == 16000
        assert ws.sent[1] == b"\x00\x00" * 1600

        await client.finish()
        finish = json.loads(next(item for item in ws.sent if isinstance(item, str) and "finish-task" in item))
        assert finish["header"]["task_id"] == first["header"]["task_id"]
        await client.close()
        try:
            await client.start()
        except VoiceTransportError:
            pass
        else:
            raise AssertionError("closed Paraformer client must reject restart")

    asyncio.run(scenario())


def test_paraformer_finish_is_serialized_after_inflight_audio() -> None:
    class BlockingAudioWebSocket(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.audio_send_started = asyncio.Event()
            self.release_audio = asyncio.Event()

        async def send(self, message: str | bytes) -> None:
            if isinstance(message, bytes):
                self.audio_send_started.set()
                await self.release_audio.wait()
            await super().send(message)

    async def scenario() -> None:
        ws = BlockingAudioWebSocket()

        async def factory(_url: str, _headers: dict[str, str]) -> FakeWebSocket:
            return ws

        client = ParaformerClient(websocket_factory=factory)
        await client.start()
        audio_task = asyncio.create_task(client.send_audio(b"\x01\x00" * 320))
        await asyncio.wait_for(ws.audio_send_started.wait(), timeout=1)
        finish_task = asyncio.create_task(client.finish())
        await asyncio.sleep(0)
        finish_task.cancel()
        try:
            await finish_task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("finish cancellation must propagate")
        ws.release_audio.set()
        await audio_task
        await client.finish()

        actions = [
            json.loads(item)["header"]["action"]
            for item in ws.sent
            if isinstance(item, str)
        ]
        assert actions == ["run-task", "finish-task"]
        assert isinstance(ws.sent[1], bytes)
        try:
            await client.send_audio(b"\x00\x00")
        except VoiceTransportError:
            pass
        else:
            raise AssertionError("audio after finish-task must be rejected")
        await client.close()

    asyncio.run(scenario())


def test_normalize_paraformer_partial_final_and_failure() -> None:
    def result(sentence_end: bool) -> dict:
        return {
            "header": {"event": "result-generated", "task_id": "task-1"},
            "payload": {
                "output": {
                    "sentence": {
                        "text": "索引下推",
                        "sentence_end": sentence_end,
                        "heartbeat": False,
                    }
                }
            },
        }

    assert normalize_paraformer_event(result(False))[0]["type"] == "user_partial"
    assert normalize_paraformer_event(result(True))[0]["type"] == "user_done"
    failed = normalize_paraformer_event(
        {
            "header": {
                "event": "task-failed",
                "task_id": "task-1",
                "error_code": "CLIENT_ERROR",
                "error_message": "bad audio",
            }
        }
    )[0]
    assert failed["type"] == "error"
    assert failed["message"] == "bad audio"


def test_silero_vad_reports_energy_fallback_and_detects_edges() -> None:
    env = {
        "SILERO_VAD_BACKEND": "energy",
        "ENERGY_VAD_THRESHOLD": "0.01",
        "ENERGY_VAD_MIN_SPEECH_MS": "20",
        "ENERGY_VAD_FRAME_MS": "20",
        "SILERO_VAD_MIN_SILENCE_MS": "40",
    }
    with patch.dict(os.environ, env, clear=False):
        vad = SileroVAD(sample_rate=16000)
        assert vad.status["backend"] == "energy"
        assert vad.status["fallback"] is True

        loud_frame = struct.pack("<h", 4000) * 320
        silence_frame = b"\x00\x00" * 320
        assert vad.process(loud_frame)[0]["type"] == "speech_started"
        assert vad.process(silence_frame) == []
        assert vad.process(silence_frame)[0]["type"] == "speech_ended"


def test_silero_runtime_failure_preserves_audio_and_flush_resets_state() -> None:
    class FailingTorch:
        float32 = object()

        @staticmethod
        def tensor(*_args, **_kwargs):
            raise RuntimeError("inference unavailable")

    env = {
        "SILERO_VAD_BACKEND": "energy",
        "ENERGY_VAD_THRESHOLD": "0.01",
        "ENERGY_VAD_MIN_SPEECH_MS": "20",
        "ENERGY_VAD_FRAME_MS": "20",
    }
    with patch.dict(os.environ, env, clear=False):
        vad = SileroVAD(sample_rate=16000)
        vad._iterator = object()
        vad._torch = FailingTorch()
        vad._status = {
            "type": "vad_status",
            "backend": "silero",
            "silero_available": True,
            "fallback": False,
            "reason": None,
        }

        loud_window = struct.pack("<h", 4000) * 512
        events = vad.process(loud_window)
        assert vad.status["backend"] == "energy"
        assert "inference failed" in str(vad.status["reason"])
        assert [event["type"] for event in events] == ["speech_started"]

        assert vad.flush()[0]["type"] == "speech_ended"
        restarted = vad.process(struct.pack("<h", 4000) * 320)
        assert restarted[0]["type"] == "speech_started"
        assert restarted[0]["timestamp_ms"] == 0


def test_silero_runtime_fallback_does_not_split_active_speech() -> None:
    class FailingTorch:
        float32 = object()

        @staticmethod
        def tensor(*_args, **_kwargs):
            raise RuntimeError("inference unavailable")

    with patch.dict(os.environ, {"SILERO_VAD_BACKEND": "energy"}, clear=False):
        vad = SileroVAD(sample_rate=16000)
        vad._iterator = object()
        vad._torch = FailingTorch()
        vad._active = True
        vad._status = {
            "type": "vad_status",
            "backend": "silero",
            "silero_available": True,
            "fallback": False,
            "reason": None,
        }

        loud_window = struct.pack("<h", 4000) * 512
        assert vad.process(loud_window) == []
        assert vad.active is True


def test_cosyvoice_and_edge_tts_support_injected_factories() -> None:
    class FakeSynthesizer:
        def __init__(self) -> None:
            self.timeout_millis: int | None = None

        async def call(self, text: str, timeout_millis: int | None = None) -> bytes:
            self.timeout_millis = timeout_millis
            return f"pcm:{text}".encode()

    fake_synthesizer = FakeSynthesizer()

    async def synthesizer_factory(**_kwargs):
        return fake_synthesizer

    async def edge_chunks():
        yield {"type": "WordBoundary", "text": "忽略"}
        yield {"type": "audio", "data": b"mp3-a"}
        yield {"type": "audio", "data": b"mp3-b"}

    class FakeCommunicate:
        def stream(self):
            return edge_chunks()

    async def scenario() -> None:
        cosy = CosyVoiceTTS(synthesizer_factory=synthesizer_factory)
        cosy_audio, cosy_mime = await cosy.synthesize("继续追问")
        assert cosy_audio == b"pcm:\xe7\xbb\xa7\xe7\xbb\xad\xe8\xbf\xbd\xe9\x97\xae"
        assert cosy_mime == "audio/pcm;rate=24000;channels=1"
        assert fake_synthesizer.timeout_millis == cosy.call_timeout_ms

        edge = EdgeTTS(communicate_factory=lambda **_kwargs: FakeCommunicate())
        edge_audio, edge_mime = await edge.synthesize("兜底")
        assert edge_audio == b"mp3-amp3-b"
        assert edge_mime == "audio/mpeg"

    asyncio.run(scenario())
