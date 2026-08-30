from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import sys
import time
from array import array
from contextlib import suppress
from typing import Any, Awaitable, Callable
import uuid

from .config import Settings
from .db import Database
from .errors import AppError
from .interview_engine import EngineResult, InterviewEngine
from .voice import (
    CosyVoiceTTS,
    EdgeTTS,
    OmniRealtimeClient,
    ParaformerClient,
    SileroVAD,
    VoiceConfigurationError,
    VoiceTransportError,
)


SendEvent = Callable[..., Awaitable[None]]
EndCallback = Callable[[str], Awaitable[None]]


# A child of uvicorn.error inherits the server's configured journal handler.
# A standalone ``app.*`` logger has no handler under Uvicorn's default logging
# config, which would make successful audio diagnostics invisible in systemd.
logger = logging.getLogger("uvicorn.error.voice")
logger.setLevel(
    {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(
        os.getenv("VOICE_DIAGNOSTICS_LOG_LEVEL", "INFO").strip().upper(),
        logging.INFO,
    )
)


def _spoken_control_prompt(
    text: str, *, startup: bool = False, language_mode: str = "bilingual"
) -> str:
    """Build a verbatim bilingual announcement instruction for Omni TTS."""

    context = "会话启动" if startup else "面试流程控制"
    language_instruction = (
        "允许自然地中英混读，"
        if language_mode == "bilingual"
        else "以中文表达为主，但原文中的英文技术词不得翻译，"
    )
    return (
        f"这是{context}消息，不是候选人的回答。"
        f"请严格按原文自然朗读下面内容：{language_instruction}中文使用普通话；英文单词、缩写、"
        "版本号和代码术语保留英文并清晰发音。不要翻译、改写、解释或增删，"
        f"不要加开场白。只输出朗读内容：\n{text}"
    )


def _pcm16le_rms(pcm: bytes) -> int:
    """Return frame RMS without retaining or logging audio content."""

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":  # pragma: no cover - production is little-endian
        samples.byteswap()
    if not samples:
        return 0
    return int(math.sqrt(sum(sample * sample for sample in samples) / len(samples)))


def fallback_chain(requested: str, enabled: bool = True) -> list[str]:
    modes = ["L0", "L1", "L2", "L3"]
    requested = requested.upper()
    if requested not in modes:
        requested = "L3"
    if not enabled:
        return [requested]
    return modes[modes.index(requested) :]


class BrowserVoiceSession:
    """Adapts all provider modes to the browser's single WebSocket protocol."""

    def __init__(
        self,
        *,
        interview_id: str,
        interview: dict[str, Any],
        settings: Settings,
        db: Database,
        engine: InterviewEngine,
        send: SendEvent,
        on_end: EndCallback,
    ) -> None:
        self.interview_id = interview_id
        self.interview = interview
        self.settings = settings
        self.db = db
        self.engine = engine
        self.send = send
        self.on_end = on_end
        self.actual_mode = "L3"

        self.omni: OmniRealtimeClient | None = None
        self.asr: ParaformerClient | None = None
        self.vad: SileroVAD | None = None
        self.tts: CosyVoiceTTS | EdgeTTS | None = None
        self.provider_task: asyncio.Task[None] | None = None
        self.answer_lock = asyncio.Lock()
        self.response_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.tts_task: asyncio.Task[None] | None = None
        self.evaluation_tasks: set[asyncio.Task[None]] = set()
        self.generation = 0
        self.closed = False
        self.ending = False
        self._answer_pending = False
        self._omni_response_started = asyncio.Event()
        self._omni_response_events: dict[str, asyncio.Event] = {}
        self._omni_response_statuses: dict[str, str] = {}
        self._omni_active_response_id: str | None = None
        self._omni_started_response_id: str | None = None
        self._omni_response_pending = False
        self._omni_responding = False
        self._drop_omni_audio = False
        self._omni_fatal_error: str | None = None
        self._omni_cancel_in_flight = False
        self._omni_cancel_target_id: str | None = None
        self._omni_cancel_error_tokens = 0
        self._playback_waiters: dict[str, asyncio.Event] = {}
        self._candidate_speaking = False
        self._deliberate_interrupt_task: asyncio.Task[None] | None = None
        self._deliberate_interrupt_firing = False
        self._deliberate_interrupt_ordinals: set[int] = set()
        self._audio_watchdog_task: asyncio.Task[None] | None = None
        self._audio_input_frames = 0
        self._audio_input_bytes = 0
        self._audio_input_peak_rms = 0
        self._audio_level_window_peak_rms = 0
        self._audio_output_chunks = 0
        self._audio_output_bytes = 0
        self._vad_started_count = 0
        self._transcript_partial_count = 0
        self._transcript_done_count = 0
        self._transcript_failed_count = 0
        self._completed_transcription_item_ids: set[str] = set()
        self._first_audio_input_at: float | None = None
        self._last_audio_input_at: float | None = None
        self._last_audio_health_log_at = 0.0
        self._last_audio_level_sent_at = 0.0
        self._speech_started_at: float | None = None
        self._omni_expected_speech: str | None = None

    async def start(self, initial_question: str) -> str:
        errors: list[str] = []
        for mode in fallback_chain(
            self.settings.voice_mode, self.settings.voice_auto_fallback
        ):
            try:
                if mode == "L0":
                    await self._start_l0(initial_question)
                elif mode in {"L1", "L2"}:
                    await self._start_pipeline(mode, initial_question)
                else:
                    self.actual_mode = "L3"
                await self.send(
                    "mode.changed",
                    requested_mode=self.settings.voice_mode,
                    voice_mode=self.actual_mode,
                    reason="；".join(errors) if errors else None,
                )
                logger.info(
                    "voice.provider.ready interview_id=%s requested_mode=%s "
                    "actual_mode=%s fallback_count=%d",
                    self.interview_id,
                    self.settings.voice_mode,
                    self.actual_mode,
                    len(errors),
                )
                if self.actual_mode != "L3":
                    self._audio_watchdog_task = asyncio.create_task(
                        self._audio_input_watchdog(),
                        name=f"voice-audio-watchdog-{self.interview_id}",
                    )
                return self.actual_mode
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                async with self.lifecycle_lock:
                    await self._close_provider()
                continue
        self.actual_mode = "L3"
        logger.warning(
            "voice.provider.unavailable interview_id=%s requested_mode=%s attempts=%d",
            self.interview_id,
            self.settings.voice_mode,
            len(errors),
        )
        await self.send(
            "mode.changed",
            requested_mode=self.settings.voice_mode,
            voice_mode="L3",
            reason="；".join(errors) or "语音服务不可用",
        )
        return self.actual_mode

    async def _start_l0(self, initial_question: str) -> None:
        self.omni = OmniRealtimeClient(self.interview["system_prompt"])
        await self.omni.start()
        self.actual_mode = "L0"
        self._omni_fatal_error = None
        self.provider_task = asyncio.create_task(
            self._consume_omni(), name=f"omni-browser-{self.interview_id}"
        )
        self._omni_response_started.clear()
        self._omni_started_response_id = None
        self._omni_response_pending = True
        self._omni_responding = True
        self._omni_expected_speech = initial_question
        try:
            await self.omni.send_text(
                _spoken_control_prompt(
                    initial_question,
                    startup=True,
                    language_mode=str(
                        self.interview.get("language_mode") or "bilingual"
                    ),
                )
            )
        except Exception:
            self._omni_response_pending = False
            self._omni_responding = False
            self._omni_expected_speech = None
            raise

    async def _start_pipeline(self, mode: str, initial_question: str) -> None:
        language_mode = str(self.interview.get("language_mode") or "bilingual")
        language_hints = ["zh", "en"] if language_mode == "bilingual" else ["zh"]
        self.asr = ParaformerClient(language_hints=language_hints)
        await self.asr.start()
        self.vad = SileroVAD(sample_rate=16000)
        self.tts = CosyVoiceTTS() if mode == "L1" else EdgeTTS()
        self.actual_mode = mode
        await self.send("vad.status", **self.vad.status)
        self.provider_task = asyncio.create_task(
            self._consume_asr(), name=f"asr-browser-{self.interview_id}"
        )
        # Synthesis is part of the preflight: if L1 is misconfigured, start()
        # falls through to L2 before the candidate begins speaking.
        await self._speak(initial_question)

    async def _audio_input_watchdog(self) -> None:
        """Emit content-free diagnostics when the browser audio stream stalls."""

        last_frames = 0
        try:
            while not self.closed and not self.ending and self.actual_mode != "L3":
                await asyncio.sleep(5)
                frames = self._audio_input_frames
                if frames == last_frames:
                    logger.warning(
                        "voice.audio.stalled interview_id=%s mode=%s frames=%d "
                        "bytes=%d vad_started=%d transcript_done=%d",
                        self.interview_id,
                        self.actual_mode,
                        frames,
                        self._audio_input_bytes,
                        self._vad_started_count,
                        self._transcript_done_count,
                    )
                elif frames > 0 and self._vad_started_count == 0:
                    logger.info(
                        "voice.vad.awaiting_speech interview_id=%s mode=%s frames=%d peak_rms=%d",
                        self.interview_id,
                        self.actual_mode,
                        frames,
                        self._audio_input_peak_rms,
                    )
                if (
                    self._speech_started_at is not None
                    and time.monotonic() - self._speech_started_at >= 15
                ):
                    logger.warning(
                        "voice.transcript.pending interview_id=%s mode=%s "
                        "pending_ms=%d partial_count=%d",
                        self.interview_id,
                        self.actual_mode,
                        int((time.monotonic() - self._speech_started_at) * 1000),
                        self._transcript_partial_count,
                    )
                last_frames = frames
        except asyncio.CancelledError:
            raise

    async def handle_audio(self, pcm: bytes) -> None:
        if self.closed or self.ending or not pcm:
            return
        if len(pcm) % 2:
            await self.send(
                "error",
                code="INVALID_AUDIO_FRAME",
                message="音频帧必须是 PCM16LE",
                recoverable=True,
            )
            return
        now = time.monotonic()
        rms = _pcm16le_rms(pcm)
        self._audio_input_frames += 1
        self._audio_input_bytes += len(pcm)
        self._audio_input_peak_rms = max(self._audio_input_peak_rms, rms)
        self._audio_level_window_peak_rms = max(
            self._audio_level_window_peak_rms, rms
        )
        self._last_audio_input_at = now
        if self._first_audio_input_at is None:
            self._first_audio_input_at = now
            self._last_audio_health_log_at = now
            logger.info(
                "voice.audio.first_frame interview_id=%s mode=%s bytes=%d rms=%d",
                self.interview_id,
                self.actual_mode,
                len(pcm),
                rms,
            )
        elif now - self._last_audio_health_log_at >= 5:
            self._last_audio_health_log_at = now
            logger.info(
                "voice.audio.health interview_id=%s mode=%s frames=%d bytes=%d "
                "peak_rms=%d vad_started=%d transcript_done=%d transcript_failed=%d",
                self.interview_id,
                self.actual_mode,
                self._audio_input_frames,
                self._audio_input_bytes,
                self._audio_input_peak_rms,
                self._vad_started_count,
                self._transcript_done_count,
                self._transcript_failed_count,
            )
        try:
            # Confirm what reached the server, independently of the browser's
            # local waveform.  A one-second cadence is enough for diagnostics
            # without adding meaningful WebSocket traffic.
            if now - self._last_audio_level_sent_at >= 1:
                self._last_audio_level_sent_at = now
                window_peak_rms = self._audio_level_window_peak_rms
                self._audio_level_window_peak_rms = 0
                await self.send(
                    "audio.input.level",
                    rms=rms,
                    window_peak_rms=window_peak_rms,
                    peak_rms=self._audio_input_peak_rms,
                    frames=self._audio_input_frames,
                    signal="active" if window_peak_rms >= 64 else "quiet",
                )
            if self.actual_mode == "L0" and self.omni:
                await self.omni.send_audio(pcm)
                return
            if self.actual_mode in {"L1", "L2"} and self.asr:
                if self.vad:
                    for event in self.vad.process(pcm):
                        if event["type"] == "speech_started":
                            await self._candidate_speech_started(
                                source=str(event.get("source", "silero"))
                            )
                        elif event["type"] == "speech_ended":
                            speech_ms = None
                            if self._speech_started_at is not None:
                                speech_ms = int(
                                    (time.monotonic() - self._speech_started_at)
                                    * 1000
                                )
                            logger.info(
                                "voice.vad.ended interview_id=%s speech_ms=%s "
                                "source=%s",
                                self.interview_id,
                                speech_ms,
                                str(event.get("source") or "silero"),
                            )
                            await self._candidate_speech_ended()
                await self.asr.send_audio(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.send(
                "error",
                code="VOICE_PROVIDER_ERROR",
                message=f"语音上行连接异常：{exc}",
                recoverable=True,
            )
            await self._runtime_fallback("语音上行连接不可用")

    async def handle_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            raise AppError("EMPTY_ANSWER", "回答不能为空", status_code=422)
        if self.closed or self.ending:
            raise AppError("INTERVIEW_ENDED", "本场面试正在结束", status_code=409)
        if self.actual_mode == "L0" and self.omni:
            await self.send("candidate.transcript.done", text=text, source="text")
            await self._interrupt_for_typed_input()
            try:
                await self.omni.send_text(text, create_response=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.send(
                    "error",
                    code="VOICE_PROVIDER_ERROR",
                    message=f"文字上行到语音服务失败：{exc}",
                    recoverable=True,
                )
                await self._runtime_fallback("L0 文字上行不可用")
                await self._schedule_evaluation(self._pipeline_answer(text))
                return
            await self._schedule_evaluation(self._evaluate_l0(text))
            return
        if self.actual_mode in {"L1", "L2"}:
            await self.send("candidate.transcript.done", text=text, source="text")
            await self._interrupt_for_typed_input()
            await self._schedule_evaluation(self._pipeline_answer(text))
            return
        # A voice session that degraded at runtime keeps this lock so a still
        # finishing voice evaluation cannot race a second L3 engine.answer.
        await self._wait_for_current_evaluation()
        await self.send("candidate.transcript.done", text=text, source="text")
        await self._schedule_evaluation(self._pipeline_answer(text))

    async def handle_playback_done(self, announcement_id: str) -> None:
        waiter = self._playback_waiters.get(announcement_id)
        if waiter:
            waiter.set()

    async def prepare_end(self) -> None:
        """Stop new input and cancel in-flight generation before a terminal line."""

        if self.ending:
            return
        self.ending = True
        self.generation += 1
        if self._deliberate_interrupt_task and not self._deliberate_interrupt_task.done():
            self._deliberate_interrupt_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._deliberate_interrupt_task
            self._deliberate_interrupt_task = None
        current = asyncio.current_task()
        tasks = [
            task
            for task in self.evaluation_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tts_task and self.tts_task is not current and not self.tts_task.done():
            self.tts_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.tts_task

    async def announce(
        self,
        text: str,
        *,
        cancel_current: bool = True,
        wait_for_playback: bool = False,
    ) -> None:
        """Speak a server-controlled sentence such as timeout/closing copy."""

        if self.actual_mode == "L0" and self.omni:
            async with self.response_lock:
                await self._finish_active_omni_response(cancel=cancel_current)
                self._omni_response_started.clear()
                self._omni_started_response_id = None
                self._drop_omni_audio = False
                self._omni_fatal_error = None
                self._omni_response_pending = True
                self._omni_responding = True
                self._omni_expected_speech = text
                try:
                    await self.omni.send_text(
                        _spoken_control_prompt(
                            text,
                            language_mode=str(
                                self.interview.get("language_mode") or "bilingual"
                            ),
                        )
                    )
                except Exception:
                    self._omni_response_pending = False
                    self._omni_responding = False
                    self._omni_expected_speech = None
                    raise
                try:
                    await asyncio.wait_for(
                        self._omni_response_started.wait(), timeout=5
                    )
                except asyncio.TimeoutError as exc:
                    raise VoiceTransportError("Omni 未开始生成语音") from exc
                if self._omni_fatal_error:
                    raise VoiceTransportError(self._omni_fatal_error)
                response_id = self._omni_started_response_id
                if not response_id:
                    raise VoiceTransportError("Omni 响应缺少 response_id")
                done = self._omni_response_events.setdefault(
                    response_id, asyncio.Event()
                )
                try:
                    await asyncio.wait_for(done.wait(), timeout=20)
                except asyncio.TimeoutError as exc:
                    raise VoiceTransportError("Omni 语音生成超时") from exc
                status = self._omni_response_statuses.get(response_id, "")
                if status != "completed":
                    raise VoiceTransportError(
                        f"Omni 响应未完整完成（status={status or 'missing'}）"
                    )
                if wait_for_playback:
                    await self._wait_for_browser_playback()
        elif self.actual_mode in {"L1", "L2"}:
            await self._speak(text, wait_for_playback=wait_for_playback)

    async def _consume_omni(self) -> None:
        assert self.omni is not None
        try:
            async for event in self.omni.events():
                event_type = event.get("type")
                if event_type == "speech_started":
                    self._vad_started_count += 1
                    self._speech_started_at = time.monotonic()
                    logger.info(
                        "voice.vad.started interview_id=%s count=%d item_present=%s "
                        "response_active=%s",
                        self.interview_id,
                        self._vad_started_count,
                        bool(event.get("item_id")),
                        self._omni_responding,
                    )
                    await self._candidate_speech_started(
                        source="server_vad", cancel_provider=False
                    )
                elif event_type == "speech_ended":
                    speech_ms = None
                    if self._speech_started_at is not None:
                        speech_ms = int(
                            (time.monotonic() - self._speech_started_at) * 1000
                        )
                    logger.info(
                        "voice.vad.ended interview_id=%s speech_ms=%s item_present=%s",
                        self.interview_id,
                        speech_ms,
                        bool(event.get("item_id")),
                    )
                    await self._candidate_speech_ended()
                elif event_type == "user_partial":
                    self._transcript_partial_count += 1
                    if (
                        self._transcript_partial_count == 1
                        or self._transcript_partial_count % 10 == 0
                    ):
                        logger.info(
                            "voice.transcript.partial interview_id=%s partial_count=%d "
                            "chars=%d language=%s",
                            self.interview_id,
                            self._transcript_partial_count,
                            len(str(event.get("text") or "")),
                            str(event.get("language") or "unknown"),
                        )
                    await self.send(
                        "candidate.transcript.partial",
                        text=event.get("text", ""),
                        item_id=event.get("item_id"),
                    )
                elif event_type == "user_done":
                    await self._candidate_speech_ended()
                    item_id = str(event.get("item_id") or "").strip()
                    if item_id and item_id in self._completed_transcription_item_ids:
                        logger.info(
                            "voice.transcript.duplicate interview_id=%s item_present=true",
                            self.interview_id,
                        )
                        continue
                    if item_id:
                        self._completed_transcription_item_ids.add(item_id)
                    text = str(event.get("text", "")).strip()
                    self._transcript_done_count += 1
                    speech_ms = None
                    if self._speech_started_at is not None:
                        speech_ms = int(
                            (time.monotonic() - self._speech_started_at) * 1000
                        )
                    logger.info(
                        "voice.transcript.done interview_id=%s count=%d chars=%d "
                        "speech_to_final_ms=%s language=%s",
                        self.interview_id,
                        self._transcript_done_count,
                        len(text),
                        speech_ms,
                        str(event.get("language") or "unknown"),
                    )
                    self._speech_started_at = None
                    if text:
                        await self.send(
                            "candidate.transcript.done",
                            text=text,
                            item_id=event.get("item_id"),
                        )
                        await self._schedule_evaluation(self._evaluate_l0(text))
                    else:
                        self._transcript_failed_count += 1
                        logger.warning(
                            "voice.transcript.empty interview_id=%s count=%d item_present=%s",
                            self.interview_id,
                            self._transcript_failed_count,
                            bool(event.get("item_id")),
                        )
                        await self.send(
                            "candidate.transcript.failed",
                            item_id=event.get("item_id"),
                        )
                        await self.send(
                            "error",
                            code="ASR_EMPTY_TRANSCRIPT",
                            message="没有识别到清晰内容，请靠近麦克风再说一次或改用文字输入。",
                            recoverable=True,
                        )
                elif event_type == "transcription_error":
                    await self._candidate_speech_ended()
                    self._transcript_failed_count += 1
                    self._speech_started_at = None
                    logger.warning(
                        "voice.transcript.failed interview_id=%s count=%d code=%s item_present=%s",
                        self.interview_id,
                        self._transcript_failed_count,
                        str(event.get("code") or "unknown"),
                        bool(event.get("item_id")),
                    )
                    await self.send(
                        "candidate.transcript.failed",
                        item_id=event.get("item_id"),
                    )
                    await self.send(
                        "error",
                        code="ASR_TRANSCRIPTION_FAILED",
                        message="这次语音没有转写成功，请再说一次或改用文字输入。",
                        recoverable=True,
                    )
                elif event_type == "response_started":
                    response_id = str(event.get("response_id") or "").strip()
                    if not response_id:
                        raise VoiceTransportError("Omni response.created 缺少 ID")
                    if self._omni_cancel_in_flight:
                        if self._omni_cancel_target_id is None:
                            # cancel was sent while response.create was pending;
                            # this is the response being cancelled.
                            self._omni_cancel_target_id = response_id
                        elif response_id != self._omni_cancel_target_id:
                            self._clear_omni_cancel_state()
                    self._omni_response_pending = False
                    self._omni_responding = True
                    self._omni_active_response_id = response_id
                    self._omni_started_response_id = response_id
                    self._omni_response_events.setdefault(
                        response_id, asyncio.Event()
                    )
                    self._omni_response_started.set()
                    logger.info(
                        "voice.tts.started interview_id=%s response_id=%s",
                        self.interview_id,
                        response_id,
                    )
                elif event_type == "assistant_partial":
                    self._omni_responding = True
                    # All L0 responses are server-controlled announcements.
                    # Their exact text has already been emitted by the shared
                    # interview engine, so provider deltas are intentionally
                    # not duplicated into the transcript.
                elif event_type == "assistant_done":
                    actual = str(event.get("text") or "")
                    expected = self._omni_expected_speech or ""
                    compact_actual = "".join(actual.split())
                    compact_expected = "".join(expected.split())
                    logger.info(
                        "voice.tts.transcript interview_id=%s exact_match=%s "
                        "expected_chars=%d actual_chars=%d",
                        self.interview_id,
                        compact_actual == compact_expected,
                        len(expected),
                        len(actual),
                    )
                elif event_type == "audio_chunk":
                    self._omni_responding = True
                    if self._drop_omni_audio:
                        continue
                    self._audio_output_chunks += 1
                    self._audio_output_bytes += len(event.get("audio") or b"")
                    await self.send(
                        "audio.chunk",
                        audio=base64.b64encode(event["audio"]).decode("ascii"),
                        sample_rate=event.get("sample_rate", 24000),
                        format="pcm_s16le",
                    )
                elif event_type == "response_done":
                    response_id = str(event.get("response_id") or "").strip()
                    if not response_id:
                        raise VoiceTransportError("Omni response.done 缺少 ID")
                    status = str(event.get("status") or "").strip().lower()
                    self._omni_response_statuses[response_id] = status
                    self._omni_response_events.setdefault(
                        response_id, asyncio.Event()
                    ).set()
                    if response_id == self._omni_active_response_id:
                        self._omni_active_response_id = None
                        self._omni_response_pending = False
                        self._omni_responding = False
                    logger.info(
                        "voice.tts.done interview_id=%s response_id=%s status=%s "
                        "output_chunks=%d output_bytes=%d",
                        self.interview_id,
                        response_id,
                        status or "missing",
                        self._audio_output_chunks,
                        self._audio_output_bytes,
                    )
                    self._omni_expected_speech = None
                    if status == "completed" and not self._drop_omni_audio:
                        # This marker is ordered after all audio.chunk events by
                        # the browser WebSocket send lock.  The AudioWorklet
                        # turns it into a real queue-drained signal, so a brief
                        # network gap cannot masquerade as end-of-question.
                        await self.send(
                            "audio.stream.done", announcement_id=response_id
                        )
                    expected_cancel = self._omni_cancel_in_flight or self._drop_omni_audio
                    if (
                        self._omni_cancel_in_flight
                        and (
                            self._omni_cancel_target_id is None
                            or response_id == self._omni_cancel_target_id
                        )
                        and status != "completed"
                    ):
                        self._discard_omni_cancel_error_token()
                        self._clear_omni_cancel_state()
                    if status != "completed" and not expected_cancel:
                        await self.send(
                            "error",
                            code="VOICE_PROVIDER_ERROR",
                            message=f"L0 语音生成未完成（status={status or 'missing'}）",
                            recoverable=True,
                        )
                        await self._runtime_fallback("L0 语音生成未完整完成")
                        return
                elif event_type == "error":
                    if self._is_benign_cancel_error(event):
                        self._discard_omni_cancel_error_token()
                        self._clear_omni_cancel_state()
                        continue
                    logger.warning(
                        "voice.provider.error interview_id=%s code=%s",
                        self.interview_id,
                        str(event.get("code") or "unknown"),
                    )
                    await self.send(
                        "error",
                        code=event.get("code", "VOICE_PROVIDER_ERROR"),
                        message=event.get("message", "L0 语音服务异常"),
                        recoverable=True,
                    )
                    await self._runtime_fallback("L0 实时连接已断开")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                await self.send(
                    "error",
                    code="VOICE_PROVIDER_ERROR",
                    message=f"L0 语音连接异常：{exc}",
                    recoverable=True,
                )
                await self._runtime_fallback("L0 实时连接异常")

    async def _consume_asr(self) -> None:
        assert self.asr is not None
        try:
            async for event in self.asr.events():
                event_type = event.get("type")
                if event_type == "user_partial":
                    self._transcript_partial_count += 1
                    await self.send(
                        "candidate.transcript.partial", text=event.get("text", "")
                    )
                elif event_type == "user_done":
                    await self._candidate_speech_ended()
                    text = str(event.get("text", "")).strip()
                    self._transcript_done_count += 1
                    logger.info(
                        "voice.transcript.done interview_id=%s mode=%s count=%d "
                        "chars=%d",
                        self.interview_id,
                        self.actual_mode,
                        self._transcript_done_count,
                        len(text),
                    )
                    self._speech_started_at = None
                    if text:
                        await self.send("candidate.transcript.done", text=text)
                        await self._schedule_evaluation(self._pipeline_answer(text))
                    else:
                        self._transcript_failed_count += 1
                        await self.send("candidate.transcript.failed")
                        await self.send(
                            "error",
                            code="ASR_EMPTY_TRANSCRIPT",
                            message="没有识别到清晰内容，请靠近麦克风再说一次或改用文字输入。",
                            recoverable=True,
                        )
                elif event_type == "error":
                    logger.warning(
                        "voice.provider.error interview_id=%s mode=%s code=%s",
                        self.interview_id,
                        self.actual_mode,
                        str(event.get("code") or "unknown"),
                    )
                    await self.send(
                        "error",
                        code=event.get("code", "ASR_ERROR"),
                        message=event.get("message", "实时识别异常"),
                        recoverable=True,
                    )
                    await self._runtime_fallback("Paraformer 连接已断开")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                await self.send(
                    "error",
                    code="ASR_ERROR",
                    message=f"实时识别异常：{exc}",
                    recoverable=True,
                )
                await self._runtime_fallback("Paraformer 连接异常")

    async def _evaluate_l0(self, text: str) -> None:
        async with self.answer_lock:
            await self.send("interviewer.state", state="thinking")
            try:
                result = await self.engine.answer(self.interview_id, text)
            except AppError as exc:
                if exc.code == "INTERVIEW_TIMEOUT":
                    await self.on_end("time")
                    return
                await self.send(
                    "error",
                    code=exc.code,
                    message=exc.message,
                    recoverable=exc.code not in {"INTERVIEW_ENDED", "INTERVIEW_TIMEOUT"},
                )
                return
            await self.db.set_last_question(self.interview_id, result.question)
            if result.silence_seconds:
                await self.send(
                    "interviewer.state",
                    state="silent",
                    seconds=result.silence_seconds,
                )
                await asyncio.sleep(result.silence_seconds)
            await self.send(
                "interviewer.text.done",
                text=result.question,
                pressure_action=result.pressure_action,
            )
            if result.ended:
                self.ending = True
            if self.omni:
                try:
                    await self.announce(
                        result.question,
                        cancel_current=False,
                        wait_for_playback=result.ended,
                    )
                except Exception as exc:
                    await self.send(
                        "error",
                        code="VOICE_PROVIDER_ERROR",
                        message=f"结束语音播放失败：{exc}",
                        recoverable=True,
                    )
                    if not result.ended:
                        await self._runtime_fallback("L0 语音生成不可用")
            if result.ended:
                await self.on_end(result.end_reason or "poor_performance")
            else:
                await self.send("interviewer.state", state="listening")

    async def _pipeline_answer(self, text: str) -> None:
        async with self.answer_lock:
            await self.send("interviewer.state", state="thinking")
            try:
                result = await self.engine.answer(self.interview_id, text)
            except AppError as exc:
                if exc.code == "INTERVIEW_TIMEOUT":
                    await self.on_end("time")
                    return
                await self.send(
                    "error",
                    code=exc.code,
                    message=exc.message,
                    recoverable=exc.code not in {"INTERVIEW_ENDED", "INTERVIEW_TIMEOUT"},
                )
                return
            if result.silence_seconds:
                await self.send(
                    "interviewer.state",
                    state="silent",
                    seconds=result.silence_seconds,
                )
                await asyncio.sleep(result.silence_seconds)
            await self.send(
                "interviewer.text.done",
                text=result.question,
                pressure_action=result.pressure_action,
            )
            if result.ended:
                self.ending = True
            if self.actual_mode in {"L1", "L2"}:
                await self._speak(
                    result.question, wait_for_playback=result.ended
                )
            if result.ended:
                await self.on_end(result.end_reason or "poor_performance")
            else:
                await self.send("interviewer.state", state="listening")

    async def _speak(
        self, text: str, *, wait_for_playback: bool = False
    ) -> None:
        if not self.tts:
            return
        self.generation += 1
        generation = self.generation

        async def synthesize() -> None:
            try:
                audio, mime_type = await self.tts.synthesize(text)
            except Exception as exc:
                if self.actual_mode == "L1":
                    self.tts = EdgeTTS()
                    self.actual_mode = "L2"
                    await self.db.set_voice_mode(self.interview_id, "L2")
                    await self.send(
                        "mode.changed",
                        requested_mode=self.settings.voice_mode,
                        voice_mode="L2",
                        reason=f"CosyVoice 不可用，已切 edge-tts：{exc}",
                    )
                    audio, mime_type = await self.tts.synthesize(text)
                else:
                    raise
            if generation != self.generation or self.closed:
                return
            encoded = base64.b64encode(audio).decode("ascii")
            self._audio_output_chunks += 1
            self._audio_output_bytes += len(audio)
            if mime_type.startswith("audio/pcm"):
                rate = 24000
                marker = "rate="
                if marker in mime_type:
                    with suppress(ValueError):
                        rate = int(mime_type.split(marker, 1)[1].split(";", 1)[0])
                await self.send(
                    "audio.chunk",
                    audio=encoded,
                    sample_rate=rate,
                    format="pcm_s16le",
                )
            else:
                await self.send(
                    "audio.file", audio=encoded, mime_type=mime_type
                )

        tts_task = asyncio.create_task(
            synthesize(), name=f"tts-{self.interview_id}-{generation}"
        )
        self.tts_task = tts_task
        try:
            # Shield distinguishes a child-only barge-in cancellation from
            # cancellation of the whole answer/WebSocket task on Python 3.10+.
            await asyncio.shield(tts_task)
        except asyncio.CancelledError:
            if tts_task.cancelled() and not self.closed:
                # The child TTS task alone was cancelled by barge-in; keep the
                # ASR/evaluation parent alive for the next candidate turn.
                return
            if not tts_task.done():
                tts_task.cancel()
                with suppress(asyncio.CancelledError):
                    await tts_task
            raise
        except Exception as exc:
            await self.send(
                "error",
                code="VOICE_PROVIDER_ERROR",
                message=f"语音合成失败：{exc}",
                recoverable=True,
            )
            await self._runtime_fallback("语音合成不可用，已切换文字模式")
            return
        finally:
            if self.tts_task is tts_task:
                self.tts_task = None
        if not self.closed:
            await self._mark_browser_playback_end(wait=wait_for_playback)

    async def _barge_in(
        self, *, source: str, cancel_provider: bool = True
    ) -> None:
        if self.closed or self.ending:
            return
        self.generation += 1
        if self.omni:
            self._drop_omni_audio = True
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
        if cancel_provider and self.omni:
            self._begin_omni_cancel()
            try:
                await self.omni.cancel()
            except Exception:
                self._discard_omni_cancel_error_token()
                self._clear_omni_cancel_state()
        await self.send("input.speech_started", source=source)
        await self.send("audio.clear")

    async def _interrupt_for_typed_input(self) -> None:
        """Typed answers are also barge-ins and must stop stale playback."""

        self.generation += 1
        if self.omni and (
            self._omni_response_pending or self._omni_active_response_id
        ):
            self._drop_omni_audio = True
            self._begin_omni_cancel()
            try:
                await self.omni.cancel()
            except Exception:
                self._discard_omni_cancel_error_token()
                self._clear_omni_cancel_state()
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.tts_task
        await self.send("audio.clear")
        await self._wait_for_current_evaluation()

    async def _wait_for_current_evaluation(self, timeout: float = 7.0) -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in self.evaluation_tasks
            if task is not current and not task.done()
        ]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _candidate_speech_started(
        self, *, source: str, cancel_provider: bool = True
    ) -> None:
        if source != "server_vad":
            self._vad_started_count += 1
            self._speech_started_at = time.monotonic()
            logger.info(
                "voice.vad.started interview_id=%s count=%d source=%s "
                "response_active=%s",
                self.interview_id,
                self._vad_started_count,
                source,
                self._omni_responding or bool(self.tts_task),
            )
        self._candidate_speaking = True
        await self._barge_in(source=source, cancel_provider=cancel_provider)
        raw_stress_level = self.interview.get("stress_level")
        stress_level = int(
            raw_stress_level
            if raw_stress_level is not None
            else (2 if self.interview.get("stress") else 0)
        )
        if stress_level < 2 or self.ending or self.closed:
            return
        turns = await self.db.list_turns(self.interview_id)
        ordinal = len(turns) + 1
        should_interrupt = (
            ordinal % 2 == 0 if stress_level >= 3 else ordinal % 4 == 3
        )
        if not should_interrupt or ordinal in self._deliberate_interrupt_ordinals:
            return
        if self._deliberate_interrupt_task and not self._deliberate_interrupt_task.done():
            return
        self._deliberate_interrupt_task = asyncio.create_task(
            self._deliberate_interrupt_after_delay(ordinal),
            name=f"pressure-interrupt-{self.interview_id}-{ordinal}",
        )

    async def _candidate_speech_ended(self) -> None:
        self._candidate_speaking = False
        task = self._deliberate_interrupt_task
        if task and not task.done() and not self._deliberate_interrupt_firing:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._deliberate_interrupt_task is task:
                self._deliberate_interrupt_task = None

    async def _deliberate_interrupt_after_delay(self, ordinal: int) -> None:
        try:
            delay = max(
                0.0,
                float(getattr(self.settings, "pressure_interrupt_seconds", 4)),
            )
            await asyncio.sleep(delay)
            if not self._candidate_speaking or self.ending or self.closed:
                return
            self._deliberate_interrupt_firing = True
            self._deliberate_interrupt_ordinals.add(ordinal)
            text = "先停一下，请先用一句话给出结论，再补最关键的依据。"
            await self.send(
                "pressure.interrupt", text=text, ordinal=ordinal
            )
            await self.send(
                "interviewer.text.done",
                text=text,
                pressure_action="interrupt",
                interjection=True,
            )
            await self.send(
                "interviewer.state", state="speaking", pressure_action="interrupt"
            )
            try:
                await self.announce(text, cancel_current=True)
            except Exception as exc:
                await self.send(
                    "error",
                    code="VOICE_PROVIDER_ERROR",
                    message=f"压力面打断语音失败：{exc}",
                    recoverable=True,
                )
                await self._runtime_fallback("压力面打断语音不可用")
            if not self.ending and not self.closed:
                await self.send("interviewer.state", state="listening")
        except asyncio.CancelledError:
            raise
        finally:
            self._deliberate_interrupt_firing = False
            if self._deliberate_interrupt_task is asyncio.current_task():
                self._deliberate_interrupt_task = None

    async def _schedule_evaluation(self, coroutine: Awaitable[None]) -> None:
        if self.closed or self.ending:
            if hasattr(coroutine, "close"):
                coroutine.close()  # type: ignore[attr-defined]
            return
        if self._answer_pending or self.answer_lock.locked():
            if hasattr(coroutine, "close"):
                coroutine.close()  # type: ignore[attr-defined]
            await self.send(
                "error",
                code="ANSWER_IN_PROGRESS",
                message="上一轮仍在生成，请稍候",
                recoverable=True,
            )
            return
        self._answer_pending = True

        async def run() -> None:
            try:
                await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.closed:
                    await self.send(
                        "error",
                        code="ANSWER_FAILED",
                        message=f"本轮处理失败：{exc}",
                        recoverable=True,
                    )
            finally:
                self._answer_pending = False

        task = asyncio.create_task(
            run(), name=f"voice-answer-{self.interview_id}"
        )
        self.evaluation_tasks.add(task)
        task.add_done_callback(self.evaluation_tasks.discard)

    async def _finish_active_omni_response(self, *, cancel: bool) -> None:
        if not self.omni or not (
            self._omni_response_pending or self._omni_active_response_id
        ):
            self._omni_responding = False
            return
        if self._omni_response_pending and not self._omni_active_response_id:
            try:
                await asyncio.wait_for(
                    self._omni_response_started.wait(), timeout=5
                )
            except asyncio.TimeoutError as exc:
                raise VoiceTransportError("Omni 上一响应未进入生成状态") from exc
        response_id = self._omni_active_response_id
        if not response_id:
            return
        done = self._omni_response_events.setdefault(response_id, asyncio.Event())
        if cancel and not done.is_set():
            self._drop_omni_audio = True
            self._begin_omni_cancel()
            try:
                await self.omni.cancel()
                await self.send("audio.clear")
            except Exception:
                self._discard_omni_cancel_error_token()
                self._clear_omni_cancel_state()
                raise
        try:
            await asyncio.wait_for(done.wait(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise VoiceTransportError("Omni 上一响应未能结束") from exc

    def _begin_omni_cancel(self) -> None:
        self._omni_cancel_in_flight = True
        self._omni_cancel_target_id = self._omni_active_response_id
        self._omni_cancel_error_tokens = min(
            8, self._omni_cancel_error_tokens + 1
        )

    def _clear_omni_cancel_state(self) -> None:
        self._omni_cancel_in_flight = False
        self._omni_cancel_target_id = None

    def _discard_omni_cancel_error_token(self) -> None:
        self._omni_cancel_error_tokens = max(
            0, self._omni_cancel_error_tokens - 1
        )

    def _is_benign_cancel_error(self, event: dict[str, Any]) -> bool:
        if self._omni_cancel_error_tokens <= 0:
            return False
        combined = f"{event.get('code', '')} {event.get('message', '')}".lower()
        return "cancel" in combined and any(
            marker in combined
            for marker in (
                "no active",
                "not active",
                "not found",
                "in progress",
                "not running",
                "没有正在",
                "无进行中",
            )
        )

    async def _mark_browser_playback_end(self, *, wait: bool) -> None:
        announcement_id = uuid.uuid4().hex
        waiter: asyncio.Event | None = None
        if wait:
            waiter = asyncio.Event()
            self._playback_waiters[announcement_id] = waiter
        await self.send(
            "audio.stream.done", announcement_id=announcement_id
        )
        if waiter is None:
            return
        try:
            await asyncio.wait_for(waiter.wait(), timeout=12)
        except asyncio.TimeoutError:
            # A hidden tab or disabled audio device must not block report
            # generation forever.
            pass
        finally:
            self._playback_waiters.pop(announcement_id, None)

    async def _wait_for_browser_playback(self) -> None:
        """Backward-compatible terminal playback helper."""

        await self._mark_browser_playback_end(wait=True)

    async def _runtime_fallback(self, reason: str) -> None:
        self._fail_omni_waiters(reason)
        async with self.lifecycle_lock:
            if self.closed or self.actual_mode == "L3":
                return
            logger.warning(
                "voice.provider.fallback interview_id=%s from_mode=%s reason=%s",
                self.interview_id,
                self.actual_mode,
                reason,
            )
            self.generation += 1
            current = asyncio.current_task()
            if self.tts_task and self.tts_task is not current and not self.tts_task.done():
                self.tts_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.tts_task
            self.tts = None
            self.vad = None
            await self._close_provider(
                from_provider_task=asyncio.current_task() is self.provider_task
            )
            self._clear_omni_cancel_state()
            self._omni_cancel_error_tokens = 0
            self.actual_mode = "L3"
            await self.db.set_voice_mode(self.interview_id, "L3")
            await self.send("audio.clear")
            await self.send(
                "mode.changed",
                requested_mode=self.settings.voice_mode,
                voice_mode="L3",
                reason=reason,
            )

    def _fail_omni_waiters(self, message: str) -> None:
        self._omni_fatal_error = message
        self._omni_response_started.set()
        for waiter in self._omni_response_events.values():
            waiter.set()

    async def close(self) -> None:
        async with self.lifecycle_lock:
            if self.closed:
                return
            self.closed = True
            self.ending = True
            self.generation += 1
            if self._deliberate_interrupt_task and not self._deliberate_interrupt_task.done():
                self._deliberate_interrupt_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._deliberate_interrupt_task
            tasks = [task for task in self.evaluation_tasks if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
            if self._audio_watchdog_task and not self._audio_watchdog_task.done():
                self._audio_watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._audio_watchdog_task
            self._audio_watchdog_task = None
            self._clear_omni_cancel_state()
            self._omni_cancel_error_tokens = 0
            await self._close_provider()
            logger.info(
                "voice.session.closed interview_id=%s mode=%s input_frames=%d "
                "input_bytes=%d peak_rms=%d vad_started=%d transcript_partial=%d "
                "transcript_done=%d transcript_failed=%d output_chunks=%d "
                "output_bytes=%d",
                self.interview_id,
                self.actual_mode,
                self._audio_input_frames,
                self._audio_input_bytes,
                self._audio_input_peak_rms,
                self._vad_started_count,
                self._transcript_partial_count,
                self._transcript_done_count,
                self._transcript_failed_count,
                self._audio_output_chunks,
                self._audio_output_bytes,
            )

    async def _close_provider(self, *, from_provider_task: bool = False) -> None:
        if self.omni:
            omni = self.omni
            self.omni = None
            await self._complete_cleanup(
                omni.close(), name=f"close-omni-{self.interview_id}"
            )
        if self.asr:
            asr = self.asr
            self.asr = None
            await self._complete_cleanup(
                asr.close(), name=f"close-asr-{self.interview_id}"
            )
        self.vad = None
        current = asyncio.current_task()
        if (
            self.provider_task
            and not self.provider_task.done()
            and self.provider_task is not current
            and not from_provider_task
        ):
            provider_task = self.provider_task
            provider_task.cancel()

            async def join_provider() -> None:
                await asyncio.gather(provider_task, return_exceptions=True)

            await self._complete_cleanup(
                join_provider(),
                name=f"join-provider-{self.interview_id}",
            )
        self.provider_task = None

    @staticmethod
    async def _complete_cleanup(
        awaitable: Awaitable[Any], *, name: str
    ) -> None:
        """Finish transport cleanup even if its owning request is cancelled."""

        task = asyncio.create_task(awaitable, name=name)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The independent close/join remains alive under shield. Await it
            # to completion before propagating parent cancellation so provider
            # clients cannot be left in a half-closed, non-retryable state.
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
            raise
        except Exception:
            # Cleanup errors must not mask the original provider failure.
            return
