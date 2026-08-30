"""Voice transports for the interview application.

The module deliberately keeps the browser-facing event vocabulary small.  Both
the end-to-end Omni client and the pipeline clients emit dictionaries using the
same transcript event names, while provider-specific frames stay private to
this module.

All third-party packages are optional imports.  This lets ``VOICE_MODE=L3`` and
the unit tests run without installing the audio stack, and makes dependency
failures explicit at the point where a particular backend is selected.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import math
import os
import sys
import uuid
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


Event = dict[str, Any]
WebSocketFactory = Callable[[str, Mapping[str, str]], Awaitable[Any] | Any]


logger = logging.getLogger("uvicorn.error.voice.provider")


class VoiceConfigurationError(RuntimeError):
    """A selected voice backend is missing required configuration."""


class VoiceTransportError(RuntimeError):
    """A remote voice transport failed or returned an invalid response."""


_END_OF_EVENTS = object()


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise VoiceConfigurationError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise VoiceConfigurationError(f"{name} must be a number") from exc


def _region_domain(workspace_id: str, region: str) -> str:
    normalized = region.strip().lower()
    if normalized in {"cn", "cn-beijing", "beijing"}:
        return f"{workspace_id}.cn-beijing.maas.aliyuncs.com"
    if normalized in {"intl", "sg", "singapore", "ap-southeast-1"}:
        return f"{workspace_id}.ap-southeast-1.maas.aliyuncs.com"
    # Keeping a predictable convention is useful for future workspace regions,
    # while callers can always provide an explicit URL override.
    return f"{workspace_id}.{region}.maas.aliyuncs.com"


def _dashscope_ws_url(path: str, explicit: str, workspace_id: str, region: str) -> str:
    if explicit:
        return explicit
    if workspace_id:
        return f"wss://{_region_domain(workspace_id, region)}{path}"
    if region.strip().lower() in {"intl", "sg", "singapore", "ap-southeast-1"}:
        return f"wss://dashscope-intl.aliyuncs.com{path}"
    return f"wss://dashscope.aliyuncs.com{path}"


def _with_query(url: str, **values: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in values.items():
        if value:
            query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_keyword(callable_object: Callable[..., Any], name: str) -> bool:
    """Return whether a callable advertises a keyword (or ``**kwargs``)."""

    try:
        parameters = inspect.signature(callable_object).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _default_websocket_factory(url: str, headers: Mapping[str, str]) -> Any:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise VoiceConfigurationError(
            "The 'websockets' package is required for L0/L1 voice modes"
        ) from exc

    kwargs = {
        "ping_interval": 20,
        "ping_timeout": 20,
        "close_timeout": 5,
        "max_size": None,
    }
    # websockets 14+ renamed extra_headers to additional_headers.  Supporting
    # both keeps the adapter usable with distro-packaged and current versions.
    try:
        return await websockets.connect(url, additional_headers=dict(headers), **kwargs)
    except TypeError:
        return await websockets.connect(url, extra_headers=dict(headers), **kwargs)


async def _close_websocket(ws: Any) -> None:
    if ws is None:
        return
    close = getattr(ws, "close", None)
    if close is not None:
        await _maybe_await(close())


def _decode_json_frame(frame: Any) -> dict[str, Any]:
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    if not isinstance(frame, str):
        raise VoiceTransportError(f"Expected a JSON WebSocket frame, got {type(frame).__name__}")
    value = json.loads(frame)
    if not isinstance(value, dict):
        raise VoiceTransportError("Expected a JSON object from the voice provider")
    return value


def _provider_error(event: Mapping[str, Any]) -> Event:
    error = event.get("error")
    if not isinstance(error, Mapping):
        error = {}
    return {
        "type": "error",
        "code": str(error.get("code") or error.get("type") or "provider_error"),
        "message": str(error.get("message") or "Voice provider returned an error"),
        "param": error.get("param"),
        "provider_event": dict(event),
    }


def normalize_omni_event(event: Mapping[str, Any], output_sample_rate: int = 24000) -> list[Event]:
    """Translate one Qwen-Omni Realtime server event into public events.

    The function is intentionally pure so protocol fixtures can be tested
    without opening a network connection.
    """

    event_type = str(event.get("type") or "")
    common: Event = {"provider_event_id": event.get("event_id")}

    if event_type == "input_audio_buffer.speech_started":
        return [
            {
                **common,
                "type": "speech_started",
                "item_id": event.get("item_id"),
                "audio_start_ms": event.get("audio_start_ms"),
            }
        ]

    if event_type == "input_audio_buffer.speech_stopped":
        return [
            {
                **common,
                "type": "speech_ended",
                "item_id": event.get("item_id"),
                "audio_end_ms": event.get("audio_end_ms"),
            }
        ]

    if event_type == "conversation.item.input_audio_transcription.delta":
        text = f"{event.get('text') or ''}{event.get('stash') or ''}"
        return [
            {
                **common,
                "type": "user_partial",
                "text": text,
                "item_id": event.get("item_id"),
                "language": event.get("language"),
                "emotion": event.get("emotion"),
            }
        ]

    if event_type == "conversation.item.input_audio_transcription.completed":
        return [
            {
                **common,
                "type": "user_done",
                "text": str(event.get("transcript") or ""),
                "item_id": event.get("item_id"),
                "language": event.get("language"),
                "emotion": event.get("emotion"),
            }
        ]

    if event_type == "conversation.item.input_audio_transcription.failed":
        error = event.get("error")
        if not isinstance(error, Mapping):
            error = {}
        # A transcription failure is scoped to one utterance.  Keep it
        # distinct from a transport/provider error so the browser session can
        # ask the candidate to repeat the answer without tearing down a
        # healthy realtime connection.
        return [
            {
                **common,
                "type": "transcription_error",
                "code": str(error.get("code") or "input_audio_transcription_failed"),
                "message": str(error.get("message") or "Input audio transcription failed"),
                "param": error.get("param"),
                "item_id": event.get("item_id"),
            }
        ]

    if event_type in {"response.audio_transcript.delta", "response.text.delta"}:
        return [
            {
                **common,
                "type": "assistant_partial",
                "text": str(event.get("delta") or ""),
                "response_id": event.get("response_id"),
                "item_id": event.get("item_id"),
            }
        ]

    if event_type in {"response.audio_transcript.done", "response.text.done"}:
        text = event.get("transcript") if event_type.endswith("audio_transcript.done") else event.get("text")
        return [
            {
                **common,
                "type": "assistant_done",
                "text": str(text or ""),
                "response_id": event.get("response_id"),
                "item_id": event.get("item_id"),
            }
        ]

    if event_type == "response.created":
        response = event.get("response")
        if not isinstance(response, Mapping):
            response = {}
        return [
            {
                **common,
                "type": "response_started",
                "response_id": response.get("id"),
                "status": response.get("status"),
            }
        ]

    if event_type == "response.audio.delta":
        encoded = event.get("delta")
        try:
            audio = base64.b64decode(str(encoded or ""), validate=True)
        except (ValueError, TypeError) as exc:
            return [
                {
                    **common,
                    "type": "error",
                    "code": "invalid_audio_base64",
                    "message": str(exc),
                    "provider_event": dict(event),
                }
            ]
        return [
            {
                **common,
                "type": "audio_chunk",
                "audio": audio,
                "sample_rate": output_sample_rate,
                "channels": 1,
                "sample_width": 2,
                "encoding": "pcm_s16le",
                "mime_type": f"audio/pcm;rate={output_sample_rate};channels=1",
                "response_id": event.get("response_id"),
                "item_id": event.get("item_id"),
            }
        ]

    if event_type == "response.function_call_arguments.done":
        raw_arguments = str(event.get("arguments") or "{}")
        try:
            arguments: Any = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = raw_arguments
        return [
            {
                **common,
                "type": "tool_call",
                "name": event.get("name"),
                "call_id": event.get("call_id"),
                "arguments": arguments,
                "arguments_json": raw_arguments,
                "response_id": event.get("response_id"),
                "item_id": event.get("item_id"),
            }
        ]

    if event_type == "response.done":
        response = event.get("response")
        if not isinstance(response, Mapping):
            response = {}
        return [
            {
                **common,
                "type": "response_done",
                "response_id": response.get("id"),
                "status": response.get("status"),
                "usage": response.get("usage"),
                "response": dict(response),
            }
        ]

    if event_type in {"error", "conversation.item.input_audio_transcription.failed"}:
        return [_provider_error(event)]

    return []


class _EventEmitter:
    def __init__(self) -> None:
        self._event_queue: asyncio.Queue[Event | object] = asyncio.Queue()
        self._events_finished = False

    async def _emit(self, event: Event) -> None:
        if not self._events_finished:
            await self._event_queue.put(event)

    def _finish_events(self) -> None:
        if not self._events_finished:
            self._events_finished = True
            self._event_queue.put_nowait(_END_OF_EVENTS)

    async def events(self) -> AsyncIterator[Event]:
        """Yield normalized events until the provider connection is closed."""

        while True:
            item = await self._event_queue.get()
            if item is _END_OF_EVENTS:
                return
            assert isinstance(item, dict)
            yield item


class OmniRealtimeClient(_EventEmitter):
    """Async Qwen3.5-Omni Realtime client using the native event protocol."""

    def __init__(
        self,
        instructions: str,
        tools: list[dict[str, Any]] | None = None,
        *,
        websocket_factory: WebSocketFactory | None = None,
    ) -> None:
        super().__init__()
        if not instructions.strip():
            raise ValueError("instructions must not be empty")

        self.instructions = instructions
        self.tools = tools or []
        self.api_key = _env_first("OMNI_API_KEY", "DASHSCOPE_API_KEY")
        self.workspace_id = _env_first("OMNI_WORKSPACE_ID", "DASHSCOPE_WORKSPACE_ID")
        self.region = _env_first("DASHSCOPE_REGION", default="cn-beijing")
        self.model = _env_first(
            "OMNI_REALTIME_MODEL",
            "QWEN_REALTIME_MODEL",
            "DASHSCOPE_OMNI_MODEL",
            default="qwen3.5-omni-flash-realtime",
        )
        base_url = _dashscope_ws_url(
            "/api-ws/v1/realtime",
            _env_first(
                "OMNI_REALTIME_URL",
                "DASHSCOPE_REALTIME_URL",
                "DASHSCOPE_REALTIME_WS_URL",
            ),
            self.workspace_id,
            self.region,
        )
        self.url = _with_query(base_url, model=self.model)
        self.voice = _env_first("OMNI_VOICE", default="Tina")
        self.input_sample_rate = _env_int("OMNI_INPUT_SAMPLE_RATE", 16000)
        self.output_sample_rate = _env_int("OMNI_OUTPUT_SAMPLE_RATE", 24000)
        self.vad_type = _env_first("OMNI_VAD_TYPE", default="server_vad")
        # Laptop microphones can be considerably quieter after browser echo
        # cancellation.  A slightly more sensitive default avoids the common
        # "waveform moves but server VAD never starts" failure while retaining
        # an environment override for noisy rooms.
        self.vad_threshold = _env_float("OMNI_VAD_THRESHOLD", 0.2)
        self.vad_prefix_padding_ms = _env_int("OMNI_PREFIX_PADDING_MS", 300)
        # Technical answers naturally contain short thinking pauses.  A 1.5s
        # boundary avoids splitting one answer into multiple scored turns while
        # keeping the interaction responsive.
        self.silence_duration_ms = _env_int("OMNI_SILENCE_DURATION_MS", 1500)
        self.input_transcription_model = _env_first(
            "OMNI_TRANSCRIPTION_MODEL", default="qwen3-asr-flash-realtime"
        )
        self.connect_timeout = _env_float("VOICE_CONNECT_TIMEOUT_SECONDS", 10.0)
        self._websocket_factory = websocket_factory

        self._ws: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._session_created = asyncio.Event()
        self._session_updated = asyncio.Event()
        self._last_error: Event | None = None
        self._started = False
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started

    async def _connect(self) -> Any:
        if not self.api_key and self._websocket_factory is None:
            raise VoiceConfigurationError("DASHSCOPE_API_KEY is required for Omni realtime")
        factory = self._websocket_factory or _default_websocket_factory
        headers = {"Authorization": f"Bearer {self.api_key}"}
        return await _maybe_await(factory(self.url, headers))

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise VoiceTransportError("Omni realtime connection is not open")
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await _maybe_await(self._ws.send(message))

    def _raise_handshake_error(self) -> None:
        if self._last_error:
            raise VoiceTransportError(self._last_error.get("message") or "Omni handshake failed")

    async def start(self) -> None:
        """Connect, receive ``session.created``, and apply the session config."""

        if self._closing or self._events_finished:
            raise VoiceTransportError(
                "Omni client cannot be restarted after close; create a new client"
            )
        if self._started:
            return
        if self._ws is not None:
            raise VoiceTransportError("Omni client is already starting")
        try:
            self._ws = await self._connect()
            self._receive_task = asyncio.create_task(self._receive_loop(), name="omni-realtime-recv")
            await asyncio.wait_for(self._session_created.wait(), timeout=self.connect_timeout)
            self._raise_handshake_error()

            session: dict[str, Any] = {
                "modalities": ["text", "audio"],
                "model": self.model,
                "voice": self.voice,
                "audio": {
                    "input": {
                        "format": {"type": "pcm", "sample_rate": self.input_sample_rate}
                    },
                    "output": {
                        "format": {"type": "pcm", "sample_rate": self.output_sample_rate}
                    },
                },
                "instructions": self.instructions,
                "input_audio_transcription": {
                    "model": self.input_transcription_model
                },
                "turn_detection": {
                    "type": self.vad_type,
                    "threshold": self.vad_threshold,
                    "prefix_padding_ms": self.vad_prefix_padding_ms,
                    "silence_duration_ms": self.silence_duration_ms,
                    # Keep server VAD, transcription, and barge-in, but let the
                    # application state machine decide when to ask the next
                    # question. This prevents an unscored automatic reply from
                    # racing the >=3-level drill and early-stop rules.
                    "create_response": False,
                    "interrupt_response": True,
                },
            }
            if self.tools:
                session["tools"] = self.tools
            await self._send_json(
                {"event_id": _event_id(), "type": "session.update", "session": session}
            )
            await asyncio.wait_for(self._session_updated.wait(), timeout=self.connect_timeout)
            self._raise_handshake_error()
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, pcm: bytes) -> None:
        """Append mono signed-16-bit little-endian PCM to the remote buffer."""

        if not self._started:
            raise VoiceTransportError("Call start() before send_audio()")
        if not pcm:
            return
        await self._send_json(
            {
                "event_id": _event_id(),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def send_text(self, text: str, *, create_response: bool = True) -> None:
        """Add a typed user message and optionally request a response."""

        if not self._started:
            raise VoiceTransportError("Call start() before send_text()")
        text = text.strip()
        if not text:
            return
        await self._send_json(
            {
                "event_id": _event_id(),
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if create_response:
            await self._send_json({"event_id": _event_id(), "type": "response.create"})

    async def cancel(self) -> None:
        if self._started:
            await self._send_json({"event_id": _event_id(), "type": "response.cancel"})

    async def send_tool_result(self, call_id: str, output: Any) -> None:
        """Return a function result and resume generation after ``tool_call``."""

        if not self._started:
            raise VoiceTransportError("Call start() before send_tool_result()")
        if not call_id.strip():
            raise ValueError("call_id must not be empty")
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        await self._send_json(
            {
                "event_id": _event_id(),
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        await self._send_json({"event_id": _event_id(), "type": "response.create"})

    async def _receive_loop(self) -> None:
        try:
            while True:
                frame = await _maybe_await(self._ws.recv())
                if frame is None:
                    break
                event = _decode_json_frame(frame)
                event_type = str(event.get("type") or "")
                if event_type == "session.created":
                    self._session_created.set()
                elif event_type == "session.updated":
                    self._session_updated.set()

                normalized = normalize_omni_event(event, self.output_sample_rate)
                if (
                    event_type.startswith(
                        "conversation.item.input_audio_transcription."
                    )
                    and not normalized
                ):
                    # Protocol-drift diagnostics intentionally log only the
                    # event name and field names, never transcript values.
                    logger.warning(
                        "voice.transcript.unhandled provider_event_type=%s fields=%s",
                        event_type,
                        ",".join(sorted(str(key) for key in event)),
                    )
                for item in normalized:
                    if item.get("type") == "error":
                        self._last_error = item
                        self._session_created.set()
                        self._session_updated.set()
                    await self._emit(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                error = {
                    "type": "error",
                    "code": "omni_connection_error",
                    "message": str(exc),
                }
                self._last_error = error
                await self._emit(error)
        finally:
            if not self._closing and self._last_error is None:
                error = {
                    "type": "error",
                    "code": "omni_connection_closed",
                    "message": "Omni realtime connection closed unexpectedly",
                }
                self._last_error = error
                await self._emit(error)
            self._session_created.set()
            self._session_updated.set()
            self._finish_events()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._ws is not None and self._started:
            with contextlib.suppress(Exception):
                await self._send_json({"event_id": _event_id(), "type": "session.finish"})
        with contextlib.suppress(Exception):
            await _close_websocket(self._ws)
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receive_task
        self._started = False
        self._ws = None
        self._finish_events()


def normalize_paraformer_event(event: Mapping[str, Any]) -> list[Event]:
    """Translate one Paraformer WebSocket event into public events."""

    header = event.get("header")
    if not isinstance(header, Mapping):
        header = {}
    event_type = str(header.get("event") or "")
    task_id = header.get("task_id")

    if event_type == "task-started":
        return [{"type": "asr_started", "task_id": task_id}]
    if event_type == "task-finished":
        return [{"type": "asr_finished", "task_id": task_id}]
    if event_type == "task-failed":
        return [
            {
                "type": "error",
                "code": str(header.get("error_code") or "paraformer_task_failed"),
                "message": str(header.get("error_message") or "Paraformer task failed"),
                "task_id": task_id,
                "provider_event": dict(event),
            }
        ]
    if event_type != "result-generated":
        return []

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return []
    output = payload.get("output")
    if not isinstance(output, Mapping):
        return []
    sentence = output.get("sentence")
    if not isinstance(sentence, Mapping) or sentence.get("heartbeat") is True:
        return []

    return [
        {
            "type": "user_done" if sentence.get("sentence_end") is True else "user_partial",
            "text": str(sentence.get("text") or ""),
            "task_id": task_id,
            "begin_time_ms": sentence.get("begin_time"),
            "end_time_ms": sentence.get("end_time"),
            "words": sentence.get("words") or [],
        }
    ]


class ParaformerClient(_EventEmitter):
    """Native duplex WebSocket client for Paraformer realtime ASR."""

    def __init__(
        self,
        *,
        language_hints: list[str] | None = None,
        websocket_factory: WebSocketFactory | None = None,
    ) -> None:
        super().__init__()
        self.api_key = _env_first("PARAFORMER_API_KEY", "DASHSCOPE_API_KEY")
        self.workspace_id = _env_first("PARAFORMER_WORKSPACE_ID", "DASHSCOPE_WORKSPACE_ID")
        # Paraformer realtime is currently a Beijing-only service.  Keep its
        # region independent from an Omni international-region selection.
        self.region = _env_first(
            "PARAFORMER_REGION", "DASHSCOPE_SPEECH_REGION", default="cn-beijing"
        )
        self.model = _env_first("PARAFORMER_MODEL", default="paraformer-realtime-v2")
        self.url = _dashscope_ws_url(
            "/api-ws/v1/inference",
            _env_first(
                "PARAFORMER_URL", "DASHSCOPE_ASR_URL", "DASHSCOPE_INFERENCE_WS_URL"
            ),
            self.workspace_id,
            self.region,
        )
        self.sample_rate = _env_int("PARAFORMER_SAMPLE_RATE", 16000)
        self.format = _env_first("PARAFORMER_FORMAT", default="pcm")
        if language_hints is None:
            language_raw = _env_first("PARAFORMER_LANGUAGE_HINTS", default="zh")
            language_hints = language_raw.split(",")
        self.language_hints = [
            item.strip().lower()
            for item in language_hints
            if isinstance(item, str) and item.strip()
        ]
        self.max_sentence_silence = _env_int("PARAFORMER_MAX_SENTENCE_SILENCE_MS", 1000)
        self.semantic_punctuation = _env_bool("PARAFORMER_SEMANTIC_PUNCTUATION", False)
        self.disfluency_removal = _env_bool("PARAFORMER_DISFLUENCY_REMOVAL", False)
        self.heartbeat = _env_bool("PARAFORMER_HEARTBEAT", True)
        self.connect_timeout = _env_float("VOICE_CONNECT_TIMEOUT_SECONDS", 10.0)
        self.finish_timeout = _env_float("VOICE_FINISH_TIMEOUT_SECONDS", 5.0)
        self.task_id = str(uuid.uuid4())
        self._websocket_factory = websocket_factory

        self._ws: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._task_started = asyncio.Event()
        self._task_finished = asyncio.Event()
        self._last_error: Event | None = None
        self._started = False
        self._finish_sent = False
        self._finishing = False
        self._finish_send_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started

    async def _connect(self) -> Any:
        if not self.api_key and self._websocket_factory is None:
            raise VoiceConfigurationError("DASHSCOPE_API_KEY is required for Paraformer")
        factory = self._websocket_factory or _default_websocket_factory
        return await _maybe_await(
            factory(self.url, {"Authorization": f"Bearer {self.api_key}"})
        )

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise VoiceTransportError("Paraformer connection is not open")
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await _maybe_await(self._ws.send(message))

    async def start(self) -> None:
        if self._closing or self._events_finished:
            raise VoiceTransportError(
                "Paraformer client cannot be restarted after close; create a new client"
            )
        if self._started:
            return
        if self._ws is not None:
            raise VoiceTransportError("Paraformer client is already starting")
        try:
            self._ws = await self._connect()
            self._receive_task = asyncio.create_task(self._receive_loop(), name="paraformer-recv")
            parameters: dict[str, Any] = {
                "format": self.format,
                "sample_rate": self.sample_rate,
                "disfluency_removal_enabled": self.disfluency_removal,
                "semantic_punctuation_enabled": self.semantic_punctuation,
                "max_sentence_silence": self.max_sentence_silence,
                "heartbeat": self.heartbeat,
            }
            if self.language_hints:
                parameters["language_hints"] = self.language_hints
            await self._send_json(
                {
                    "header": {
                        "action": "run-task",
                        "task_id": self.task_id,
                        "streaming": "duplex",
                    },
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": self.model,
                        "parameters": parameters,
                        "input": {},
                    },
                }
            )
            await asyncio.wait_for(self._task_started.wait(), timeout=self.connect_timeout)
            if self._last_error:
                raise VoiceTransportError(self._last_error.get("message") or "Paraformer start failed")
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, pcm: bytes) -> None:
        """Send raw mono PCM only after the provider confirms ``task-started``."""

        if not self._started or not self._task_started.is_set():
            raise VoiceTransportError("Call start() and wait for task-started before send_audio()")
        if not pcm:
            return
        async with self._send_lock:
            # Checking under the same lock used by finish-task makes the wire
            # ordering deterministic: audio is either sent before finish, or
            # rejected after finish has claimed the stream.
            if (
                self._finishing
                or self._finish_sent
                or self._task_finished.is_set()
                or self._closing
            ):
                raise VoiceTransportError("Cannot send audio after finish()")
            if self._ws is None:
                raise VoiceTransportError("Paraformer connection is not open")
            await _maybe_await(self._ws.send(bytes(pcm)))

    async def _send_finish_frame(self) -> None:
        await self._send_json(
            {
                "header": {
                    "action": "finish-task",
                    "task_id": self.task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            }
        )
        self._finish_sent = True

    def _finish_frame_done(self, task: asyncio.Task[None]) -> None:
        # Retrieve the exception so a finish() caller cancelled while the
        # shielded send was running does not cause an unhandled-task warning.
        failed = task.cancelled()
        if not failed:
            failed = task.exception() is not None
        if self._finish_send_task is task:
            self._finishing = False
            if failed:
                self._finish_send_task = None

    async def finish(self) -> None:
        if not self._started or self._task_finished.is_set():
            return
        if not self._finish_sent:
            task = self._finish_send_task
            if task is None:
                # Claim the stream before waiting for the audio send lock, so
                # no later audio can overtake finish-task.  Shielding keeps the
                # actual control frame alive if this caller is cancelled.
                self._finishing = True
                task = asyncio.create_task(
                    self._send_finish_frame(), name="paraformer-finish-send"
                )
                self._finish_send_task = task
                task.add_done_callback(self._finish_frame_done)
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.finish_timeout
            )
            self._finishing = False
        await asyncio.wait_for(self._task_finished.wait(), timeout=self.finish_timeout)
        if self._last_error:
            raise VoiceTransportError(self._last_error.get("message") or "Paraformer finish failed")

    async def _receive_loop(self) -> None:
        try:
            while True:
                frame = await _maybe_await(self._ws.recv())
                if frame is None:
                    break
                event = _decode_json_frame(frame)
                header = event.get("header")
                provider_type = header.get("event") if isinstance(header, Mapping) else None
                if provider_type == "task-started":
                    self._task_started.set()
                elif provider_type == "task-finished":
                    self._task_finished.set()
                elif provider_type == "task-failed":
                    normalized_error = normalize_paraformer_event(event)
                    self._last_error = normalized_error[0] if normalized_error else {
                        "type": "error",
                        "message": "Paraformer task failed",
                    }
                    self._task_started.set()
                    self._task_finished.set()

                for item in normalize_paraformer_event(event):
                    await self._emit(item)
                if provider_type in {"task-finished", "task-failed"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                error = {
                    "type": "error",
                    "code": "paraformer_connection_error",
                    "message": str(exc),
                }
                self._last_error = error
                await self._emit(error)
        finally:
            if (
                not self._closing
                and not self._task_finished.is_set()
                and self._last_error is None
            ):
                error = {
                    "type": "error",
                    "code": "paraformer_connection_closed",
                    "message": "Paraformer connection closed unexpectedly",
                }
                self._last_error = error
                await self._emit(error)
            self._task_started.set()
            self._task_finished.set()
            self._finish_events()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._started and not self._task_finished.is_set():
            with contextlib.suppress(Exception):
                await self.finish()
        with contextlib.suppress(Exception):
            await _close_websocket(self._ws)
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receive_task
        if self._finish_send_task is not None and not self._finish_send_task.done():
            self._finish_send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._finish_send_task
        self._started = False
        self._ws = None
        self._finish_events()


class SileroVAD:
    """Streaming VAD with a deterministic RMS-energy fallback.

    ``process`` accepts raw 16-bit little-endian mono PCM and returns zero or
    more ``speech_started`` / ``speech_ended`` events.  ``status`` makes it
    explicit whether the Silero model or the fallback is currently active.
    """

    def __init__(self, sample_rate: int = 16000) -> None:
        if sample_rate not in {8000, 16000}:
            raise ValueError("Silero VAD supports 8000 or 16000 Hz input")
        self.sample_rate = sample_rate
        self.threshold = _env_float("SILERO_VAD_THRESHOLD", 0.5)
        self.min_silence_ms = _env_int("SILERO_VAD_MIN_SILENCE_MS", 300)
        self.speech_pad_ms = _env_int("SILERO_VAD_SPEECH_PAD_MS", 30)
        self.energy_threshold = _env_float("ENERGY_VAD_THRESHOLD", 0.018)
        self.energy_start_ms = _env_int("ENERGY_VAD_MIN_SPEECH_MS", 64)
        self.energy_frame_ms = _env_int("ENERGY_VAD_FRAME_MS", 20)
        self.requested_backend = _env_first("SILERO_VAD_BACKEND", default="auto").lower()

        self._iterator: Any = None
        self._torch: Any = None
        self._silero_buffer = bytearray()
        self._energy_buffer = bytearray()
        self._active = False
        self._total_samples = 0
        self._voiced_samples = 0
        self._silent_samples = 0
        self._status: Event
        self._load_backend()

    def _load_backend(self) -> None:
        if self.requested_backend == "energy":
            self._use_energy("energy fallback forced by SILERO_VAD_BACKEND")
            return
        try:
            import torch
            from silero_vad import VADIterator, load_silero_vad

            model = load_silero_vad(onnx=_env_bool("SILERO_VAD_ONNX", True))
            self._iterator = VADIterator(
                model,
                threshold=self.threshold,
                sampling_rate=self.sample_rate,
                min_silence_duration_ms=self.min_silence_ms,
                speech_pad_ms=self.speech_pad_ms,
            )
            self._torch = torch
            self._status = {
                "type": "vad_status",
                "backend": "silero",
                "silero_available": True,
                "fallback": False,
                "reason": None,
            }
        except Exception as exc:  # optional dependency or native runtime failure
            self._use_energy(f"Silero unavailable: {type(exc).__name__}: {exc}")

    def _use_energy(self, reason: str) -> None:
        self._iterator = None
        self._torch = None
        self._status = {
            "type": "vad_status",
            "backend": "energy",
            "silero_available": False,
            "fallback": True,
            "reason": reason,
        }

    @property
    def status(self) -> Event:
        return dict(self._status)

    @property
    def active(self) -> bool:
        return self._active

    @staticmethod
    def _samples_from_pcm(pcm: bytes) -> array:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples

    def process(self, pcm: bytes) -> list[Event]:
        if len(pcm) % 2:
            raise ValueError("PCM byte length must be even for signed 16-bit audio")
        if not pcm:
            return []
        if self._iterator is None:
            return self._process_energy(pcm)

        # Current Silero streaming models consume 512 samples at 16 kHz and
        # 256 samples at 8 kHz (32 ms in both cases).
        window_samples = 512 if self.sample_rate == 16000 else 256
        window_bytes = window_samples * 2
        self._silero_buffer.extend(pcm)
        events: list[Event] = []
        failed_frame = b""
        failed_frame_counted = False
        try:
            while len(self._silero_buffer) >= window_bytes:
                failed_frame = bytes(self._silero_buffer[:window_bytes])
                del self._silero_buffer[:window_bytes]
                failed_frame_counted = False
                samples = self._samples_from_pcm(failed_frame)
                self._total_samples += window_samples
                failed_frame_counted = True
                tensor = self._torch.tensor(
                    [sample / 32768.0 for sample in samples], dtype=self._torch.float32
                )
                result = self._iterator(tensor, return_seconds=False)
                if not result:
                    continue
                if "start" in result and not self._active:
                    self._active = True
                    events.append(
                        {
                            "type": "speech_started",
                            "source": "silero",
                            "timestamp_ms": round(int(result["start"]) * 1000 / self.sample_rate),
                        }
                    )
                if "end" in result and self._active:
                    self._active = False
                    events.append(
                        {
                            "type": "speech_ended",
                            "source": "silero",
                            "timestamp_ms": round(int(result["end"]) * 1000 / self.sample_rate),
                        }
                    )
                failed_frame = b""
                failed_frame_counted = False
            return events
        except Exception as exc:
            # A runtime model failure should degrade voice interaction instead
            # of taking down the interview WebSocket. Preserve the failed
            # frame and unconsumed tail, without replaying frames that Silero
            # already accepted.
            if failed_frame_counted:
                self._total_samples -= len(failed_frame) // 2
            remaining = failed_frame + bytes(self._silero_buffer)
            was_active = self._active
            self._silero_buffer.clear()
            self._use_energy(f"Silero inference failed: {type(exc).__name__}: {exc}")
            # A backend switch is not a real speech boundary.  Carry the
            # active state into the energy detector so one continuous answer
            # cannot be split into a false end/start pair.
            self._active = was_active
            self._energy_buffer.clear()
            self._voiced_samples = 0
            self._silent_samples = 0
            events.extend(self._process_energy(remaining))
            return events

    def _process_energy(self, pcm: bytes) -> list[Event]:
        frame_samples = max(1, round(self.sample_rate * self.energy_frame_ms / 1000))
        frame_bytes = frame_samples * 2
        min_voice_samples = max(1, round(self.sample_rate * self.energy_start_ms / 1000))
        min_silence_samples = max(1, round(self.sample_rate * self.min_silence_ms / 1000))
        self._energy_buffer.extend(pcm)
        events: list[Event] = []

        while len(self._energy_buffer) >= frame_bytes:
            frame = bytes(self._energy_buffer[:frame_bytes])
            del self._energy_buffer[:frame_bytes]
            samples = self._samples_from_pcm(frame)
            count = len(samples)
            self._total_samples += count
            rms = math.sqrt(sum(sample * sample for sample in samples) / max(1, count)) / 32768.0

            if rms >= self.energy_threshold:
                self._voiced_samples += count
                self._silent_samples = 0
                if not self._active and self._voiced_samples >= min_voice_samples:
                    self._active = True
                    start_sample = max(0, self._total_samples - self._voiced_samples)
                    events.append(
                        {
                            "type": "speech_started",
                            "source": "energy",
                            "timestamp_ms": round(start_sample * 1000 / self.sample_rate),
                            "rms": rms,
                        }
                    )
            else:
                self._voiced_samples = 0
                if self._active:
                    self._silent_samples += count
                    if self._silent_samples >= min_silence_samples:
                        self._active = False
                        end_sample = max(0, self._total_samples - self._silent_samples)
                        events.append(
                            {
                                "type": "speech_ended",
                                "source": "energy",
                                "timestamp_ms": round(end_sample * 1000 / self.sample_rate),
                                "rms": rms,
                            }
                        )
                        self._silent_samples = 0
        return events

    feed = process

    def flush(self) -> list[Event]:
        active = self._active
        source = self._status["backend"]
        pending = self._silero_buffer if self._iterator is not None else self._energy_buffer
        timestamp_ms = round(
            (self._total_samples + len(pending) // 2) * 1000 / self.sample_rate
        )
        self.reset()
        if not active:
            return []
        return [
            {
                "type": "speech_ended",
                "source": source,
                "timestamp_ms": timestamp_ms,
            }
        ]

    def reset(self) -> None:
        if self._iterator is not None:
            with contextlib.suppress(Exception):
                self._iterator.reset_states()
        self._silero_buffer.clear()
        self._energy_buffer.clear()
        self._active = False
        self._total_samples = 0
        self._voiced_samples = 0
        self._silent_samples = 0


def _cosy_mime_type(format_name: str) -> str:
    upper = format_name.upper()
    if upper.startswith("PCM_"):
        try:
            rate = int(upper.split("_")[1].removesuffix("HZ"))
        except (IndexError, ValueError):
            rate = 24000
        return f"audio/pcm;rate={rate};channels=1"
    if upper.startswith("WAV_"):
        return "audio/wav"
    if upper.startswith("OPUS_") or upper.startswith("OGG_OPUS_"):
        return "audio/ogg;codecs=opus"
    return "audio/mpeg"


class CosyVoiceTTS:
    """One-shot asynchronous facade over DashScope's CosyVoice SDK."""

    def __init__(self, *, synthesizer_factory: Callable[..., Any] | None = None) -> None:
        self.api_key = _env_first("COSYVOICE_API_KEY", "DASHSCOPE_API_KEY")
        self.workspace_id = _env_first("COSYVOICE_WORKSPACE_ID", "DASHSCOPE_WORKSPACE_ID")
        # CosyVoice shares the speech endpoint with Paraformer and should not
        # inherit an unrelated international Omni region.
        self.region = _env_first(
            "COSYVOICE_REGION", "DASHSCOPE_SPEECH_REGION", default="cn-beijing"
        )
        self.model = _env_first("COSYVOICE_MODEL", default="cosyvoice-v3-flash")
        self.voice = _env_first("COSYVOICE_VOICE", default="longanyang")
        self.format_name = _env_first(
            "COSYVOICE_FORMAT", default="PCM_24000HZ_MONO_16BIT"
        ).upper()
        self.url = _dashscope_ws_url(
            "/api-ws/v1/inference",
            _env_first("COSYVOICE_URL", "DASHSCOPE_INFERENCE_WS_URL"),
            self.workspace_id,
            self.region,
        )
        self.timeout = _env_float("TTS_TIMEOUT_SECONDS", 30.0)
        self.call_timeout_ms = _env_int(
            "COSYVOICE_CALL_TIMEOUT_MS", max(1, round(self.timeout * 900))
        )
        self._synthesizer_factory = synthesizer_factory

    def _default_synthesizer(self) -> Any:
        if not self.api_key:
            raise VoiceConfigurationError("DASHSCOPE_API_KEY is required for CosyVoice")
        try:
            import dashscope
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise VoiceConfigurationError(
                "The 'dashscope' package is required for VOICE_MODE=L1"
            ) from exc

        dashscope.api_key = self.api_key
        dashscope.base_websocket_api_url = self.url
        try:
            audio_format = getattr(AudioFormat, self.format_name)
        except AttributeError as exc:
            raise VoiceConfigurationError(
                f"Unsupported COSYVOICE_FORMAT: {self.format_name}"
            ) from exc
        return SpeechSynthesizer(model=self.model, voice=self.voice, format=audio_format)

    async def _make_synthesizer(self) -> Any:
        if self._synthesizer_factory is None:
            return await asyncio.to_thread(self._default_synthesizer)
        factory = self._synthesizer_factory
        kwargs = {
            "model": self.model,
            "voice": self.voice,
            "format_name": self.format_name,
            "url": self.url,
            "api_key": self.api_key,
        }
        is_async_factory = inspect.iscoroutinefunction(factory) or inspect.iscoroutinefunction(
            getattr(factory, "__call__", None)
        )
        if is_async_factory:
            return await _maybe_await(factory(**kwargs))
        return await _maybe_await(await asyncio.to_thread(factory, **kwargs))

    async def _synthesize(self, text: str) -> bytes:
        synthesizer = await self._make_synthesizer()
        call = getattr(synthesizer, "call", None)
        if call is None:
            raise VoiceTransportError("CosyVoice synthesizer has no call() method")
        call_kwargs: dict[str, Any] = {}
        # Current DashScope SDKs expose a native timeout.  Passing it slightly
        # inside the async facade timeout lets the SDK close its own task and
        # avoids leaving a non-cancellable worker thread running after timeout.
        if _accepts_keyword(call, "timeout_millis"):
            call_kwargs["timeout_millis"] = self.call_timeout_ms
        if inspect.iscoroutinefunction(call):
            result = await call(text, **call_kwargs)
        else:
            result = await asyncio.to_thread(call, text, **call_kwargs)
            result = await _maybe_await(result)
        if not isinstance(result, (bytes, bytearray, memoryview)):
            raise VoiceTransportError("CosyVoice returned no audio bytes")
        return bytes(result)

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        text = text.strip()
        if not text:
            return b"", _cosy_mime_type(self.format_name)
        try:
            audio = await asyncio.wait_for(self._synthesize(text), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise VoiceTransportError("CosyVoice synthesis timed out") from exc
        return audio, _cosy_mime_type(self.format_name)


class EdgeTTS:
    """Free TTS fallback using edge-tts, returned as one MP3 byte string."""

    def __init__(self, *, communicate_factory: Callable[..., Any] | None = None) -> None:
        self.voice = _env_first("EDGE_TTS_VOICE", default="zh-CN-YunxiNeural")
        self.rate = _env_first("EDGE_TTS_RATE", default="+0%")
        self.volume = _env_first("EDGE_TTS_VOLUME", default="+0%")
        self.pitch = _env_first("EDGE_TTS_PITCH", default="+0Hz")
        self.timeout = _env_float("TTS_TIMEOUT_SECONDS", 30.0)
        self._communicate_factory = communicate_factory

    def _default_communicate(self, text: str) -> Any:
        try:
            import edge_tts
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise VoiceConfigurationError(
                "The 'edge-tts' package is required for VOICE_MODE=L2"
            ) from exc
        return edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            volume=self.volume,
            pitch=self.pitch,
        )

    async def _collect(self, text: str) -> bytes:
        if self._communicate_factory is None:
            communicate = self._default_communicate(text)
        else:
            communicate = await _maybe_await(
                self._communicate_factory(
                    text=text,
                    voice=self.voice,
                    rate=self.rate,
                    volume=self.volume,
                    pitch=self.pitch,
                )
            )
        stream = getattr(communicate, "stream", None)
        if stream is None:
            raise VoiceTransportError("edge-tts communicator has no stream() method")
        iterator = stream()
        iterator = await _maybe_await(iterator)
        chunks: list[bytes] = []
        async for chunk in iterator:
            if isinstance(chunk, Mapping) and chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray, memoryview)):
                    chunks.append(bytes(data))
        if not chunks:
            raise VoiceTransportError("edge-tts returned no audio")
        return b"".join(chunks)

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        text = text.strip()
        if not text:
            return b"", "audio/mpeg"
        try:
            audio = await asyncio.wait_for(self._collect(text), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise VoiceTransportError("edge-tts synthesis timed out") from exc
        return audio, "audio/mpeg"


__all__ = [
    "CosyVoiceTTS",
    "EdgeTTS",
    "OmniRealtimeClient",
    "ParaformerClient",
    "SileroVAD",
    "VoiceConfigurationError",
    "VoiceTransportError",
    "normalize_omni_event",
    "normalize_paraformer_event",
]
