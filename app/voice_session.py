from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import math
import os
import re
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
NON_SILENT_PCM_RMS = 64
MICROPHONE_TAIL_SILENCE_SECONDS = 1.6
MICROPHONE_TAIL_SILENCE_TIMEOUT_SECONDS = 2.5
VOICE_CLEANUP_TIMEOUT_SECONDS = 5.0
ANSWER_FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 4.5


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

    if language_mode == "en":
        context = "session opening" if startup else "interview flow control"
        return (
            f"This is a {context} message, not the candidate's answer. Read the following "
            "text verbatim in natural, professional English. Preserve technical names, "
            "abbreviations, version numbers, and code terms exactly. Do not translate, "
            f"paraphrase, explain, add, or remove anything. Output only this text:\n{text}"
        )
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


def _edge_tts_for_language(language_mode: str) -> EdgeTTS:
    return EdgeTTS(voice="en-US-GuyNeural") if language_mode == "en" else EdgeTTS()


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
        self.microphone_enabled = True
        self._microphone_state_lock = asyncio.Lock()
        self._answer_pending = False
        # The explicit per-question answer protocol is enabled lazily on the
        # first answer.start event so an already-open legacy browser tab keeps
        # working. Once enabled, PCM outside the start/end boundary is ignored.
        self.answer_boundary_enabled = False
        self.answer_capture_active = False
        self._answer_boundary_lock = asyncio.Lock()
        self._answer_finalize_lock = asyncio.Lock()
        self._answer_boundary_generation = 0
        self._answer_boundary_started_at: float | None = None
        self._answer_boundary_duration_seconds: float | None = None
        self._answer_boundary_item_id = ""
        self._answer_boundary_segments: list[str] = []
        self._answer_boundary_segment_ids: set[str] = set()
        self._answer_boundary_speech_started_count = 0
        self._answer_boundary_transcript_done_count = 0
        self._answer_boundary_transcript_event = asyncio.Event()
        self._answer_boundary_finalized = False
        self._answer_boundary_text_override = False
        self._preserving_pending_transcripts = False
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
        self._latest_candidate_partial = ""
        self._expression_interrupt_reason = ""
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
        self._transcript_items: dict[str, dict[str, Any]] = {}
        self._first_audio_input_at: float | None = None
        self._last_audio_input_at: float | None = None
        self._last_audio_health_log_at = 0.0
        self._last_audio_level_sent_at = 0.0
        self._speech_started_at: float | None = None
        self._speech_duration_seconds: float | None = None
        self._omni_expected_speech: str | None = None
        # L0 audio is held until the provider's completed audio transcript is
        # checked against the already displayed question.  This prevents a
        # generative realtime model from paraphrasing text while audio is
        # already playing in the browser.
        self._omni_expected_by_response: dict[str, str] = {}
        self._omni_spoken_by_response: dict[str, str] = {}
        self._omni_audio_buffers: dict[str, list[tuple[bytes, int]]] = {}
        self._omni_release_tasks: dict[str, asyncio.Task[None]] = {}
        self._omni_non_silent_input_pending_response = False
        self._omni_non_silent_input_response_ids: set[str] = set()
        self._omni_input_cancelled_response_ids: set[str] = set()

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
        self._omni_non_silent_input_pending_response = False
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
        language_hints = {
            "bilingual": ["zh", "en"],
            "en": ["en"],
        }.get(language_mode, ["zh"])
        self.asr = ParaformerClient(language_hints=language_hints)
        await self.asr.start()
        self.vad = SileroVAD(sample_rate=16000)
        self.tts = (
            CosyVoiceTTS()
            if mode == "L1"
            else _edge_tts_for_language(language_mode)
        )
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
        if (
            self.closed
            or self.ending
            or not self.microphone_enabled
            or (self.answer_boundary_enabled and not self.answer_capture_active)
            or not pcm
        ):
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
                if rms >= NON_SILENT_PCM_RMS:
                    if self._omni_active_response_id:
                        self._omni_non_silent_input_response_ids.add(
                            self._omni_active_response_id
                        )
                    elif self._omni_response_pending:
                        # Provider response.created may still be queued behind
                        # browser PCM. Transfer this evidence to its ID when
                        # response_started arrives.
                        self._omni_non_silent_input_pending_response = True
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

    async def handle_microphone_state(self, enabled: bool) -> None:
        """Apply browser capture state and flush a bounded final speech tail."""

        async with self._microphone_state_lock:
            enabled = bool(enabled)
            was_enabled = self.microphone_enabled
            self.microphone_enabled = enabled
            if enabled or self.closed:
                return
            if not was_enabled:
                await self._candidate_speech_ended()
                return
            try:
                await self._flush_input_tail()
            except asyncio.TimeoutError:
                logger.warning(
                    "voice.microphone.tail_timeout interview_id=%s mode=%s",
                    self.interview_id,
                    self.actual_mode,
                )
            except Exception as exc:
                logger.warning(
                    "voice.microphone.tail_failed interview_id=%s mode=%s error=%s",
                    self.interview_id,
                    self.actual_mode,
                    type(exc).__name__,
                )
            finally:
                # This also cancels a scheduled pressure interruption.
                await self._candidate_speech_ended()

    async def _flush_input_tail(self) -> None:
        """Close the provider VAD turn without changing microphone ownership."""

        frame = b"\x00\x00" * 1600  # 100 ms at 16 kHz
        frame_count = max(
            1,
            int(math.ceil(MICROPHONE_TAIL_SILENCE_SECONDS * 10)),
        )

        async def flush_tail() -> None:
            for _ in range(frame_count):
                if self.actual_mode == "L0" and self.omni:
                    await self.omni.send_audio(frame)
                elif self.actual_mode in {"L1", "L2"} and self.asr:
                    if self.vad:
                        for event in self.vad.process(frame):
                            if event.get("type") == "speech_ended":
                                await self._candidate_speech_ended()
                    await self.asr.send_audio(frame)
                else:
                    return
                await asyncio.sleep(0)

        await asyncio.wait_for(
            flush_tail(),
            timeout=MICROPHONE_TAIL_SILENCE_TIMEOUT_SECONDS,
        )

    async def handle_answer_start(self) -> None:
        """Open one explicit answer boundary and start server-side timing."""

        if self.closed or self.ending:
            raise AppError("INTERVIEW_ENDED", "本场面试正在结束", status_code=409)
        if self._answer_pending or self.answer_lock.locked():
            raise AppError(
                "ANSWER_IN_PROGRESS",
                "上一题仍在处理中，请等面试官问完下一题",
                status_code=409,
            )
        async with self._answer_boundary_lock:
            self.answer_boundary_enabled = True
            if self.answer_capture_active:
                return
            self._answer_boundary_generation += 1
            self.answer_capture_active = True
            self._answer_boundary_started_at = time.monotonic()
            self._answer_boundary_duration_seconds = None
            self._answer_boundary_item_id = f"answer-{uuid.uuid4().hex}"
            self._answer_boundary_segments = []
            self._answer_boundary_segment_ids = set()
            self._answer_boundary_speech_started_count = 0
            self._answer_boundary_transcript_done_count = 0
            self._answer_boundary_transcript_event.clear()
            self._answer_boundary_finalized = False
            self._answer_boundary_text_override = False
            self._latest_candidate_partial = ""
            self._expression_interrupt_reason = ""
        await self.send("answer.state.changed", state="answering")

    async def handle_answer_end(self, text: str = "") -> None:
        """Seal one answer, preserve its full elapsed time, then evaluate once."""

        text = str(text or "").strip()
        async with self._answer_boundary_lock:
            if not self.answer_boundary_enabled or not self.answer_capture_active:
                raise AppError(
                    "ANSWER_NOT_STARTED",
                    "请先点击“开始回答”",
                    status_code=409,
                )
            started_at = self._answer_boundary_started_at or time.monotonic()
            self._answer_boundary_duration_seconds = round(
                max(0.0, min(time.monotonic() - started_at, 3600.0)),
                2,
            )
            self.answer_capture_active = False
            generation = self._answer_boundary_generation
            # If speech is still active, wait for the final provider segment
            # produced by the VAD tail instead of immediately consuming a
            # segment from an earlier thinking pause.
            if (
                self._candidate_speaking
                or self._answer_boundary_speech_started_count
                > self._answer_boundary_transcript_done_count
            ):
                self._answer_boundary_transcript_event.clear()
            self._answer_boundary_text_override = bool(text)
        await self.send(
            "answer.state.changed",
            state="sealing",
            elapsed_ms=int((self._answer_boundary_duration_seconds or 0) * 1000),
        )

        with suppress(asyncio.TimeoutError, Exception):
            await self._flush_input_tail()
        await self._candidate_speech_ended()

        if text:
            async with self._answer_finalize_lock:
                if generation != self._answer_boundary_generation:
                    return
                self._answer_boundary_finalized = True
                self._answer_boundary_segments = []
            await self.handle_text(
                text,
                answer_duration_seconds=self._answer_boundary_duration_seconds,
            )
            return

        if self.actual_mode == "L3" or (not self.omni and not self.asr):
            await self._fail_explicit_answer(
                "当前没有可用的实时转写，请输入文字后再结束回答。"
            )
            return

        if not self._answer_boundary_transcript_event.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._answer_boundary_transcript_event.wait(),
                    timeout=ANSWER_FINAL_TRANSCRIPT_TIMEOUT_SECONDS,
                )
        await self._finalize_explicit_voice_answer(generation)

    async def _collect_explicit_voice_segment(
        self,
        *,
        text: str,
        provider_item_id: str,
    ) -> bool:
        """Collect VAD segments until the candidate clicks end answer."""

        if not self.answer_boundary_enabled:
            return False
        normalized = text.strip()
        if self._answer_boundary_finalized or self._answer_boundary_text_override:
            logger.info(
                "voice.transcript.after_boundary interview_id=%s chars=%d",
                self.interview_id,
                len(normalized),
            )
            return True
        if provider_item_id and provider_item_id in self._answer_boundary_segment_ids:
            return True
        if provider_item_id:
            self._answer_boundary_segment_ids.add(provider_item_id)
        self._answer_boundary_transcript_done_count += 1
        if not normalized:
            self._answer_boundary_transcript_event.set()
            return True
        self._answer_boundary_segments.append(normalized)
        self._answer_boundary_transcript_event.set()
        combined = "；".join(self._answer_boundary_segments)
        await self.send(
            "candidate.transcript.partial",
            text=combined,
            item_id=self._answer_boundary_item_id,
        )
        if not self.answer_capture_active:
            await self._finalize_explicit_voice_answer(
                self._answer_boundary_generation
            )
        return True

    async def _finalize_explicit_voice_answer(self, generation: int) -> None:
        async with self._answer_finalize_lock:
            if (
                generation != self._answer_boundary_generation
                or self._answer_boundary_finalized
                or self._answer_boundary_text_override
            ):
                return
            text = "；".join(
                segment.strip()
                for segment in self._answer_boundary_segments
                if segment.strip()
            ).strip()
            if not text:
                self._answer_boundary_finalized = True
                should_fail = True
            else:
                should_fail = False
                self._answer_boundary_finalized = True
                item_id = self._answer_boundary_item_id or f"answer-{uuid.uuid4().hex}"
                duration = self._answer_boundary_duration_seconds
                ordinal = len(await self.db.list_turns(self.interview_id)) + 1
                self._transcript_items[item_id] = {
                    "ordinal": None,
                    "predicted_ordinal": ordinal,
                    "text": text,
                    "original_text": text,
                    "input_mode": "voice",
                    "answer_duration_seconds": duration,
                }
        if should_fail:
            await self._fail_explicit_answer(
                "没有识别到有效回答，请重新开始回答，或改用文字输入。"
            )
            return
        await self.send(
            "candidate.transcript.done",
            text=text,
            item_id=item_id,
            ordinal=ordinal,
            source="voice",
            editable=True,
        )
        if self.actual_mode == "L0" and self.omni:
            scheduled = await self._schedule_evaluation(
                self._evaluate_l0(
                    text,
                    input_mode="voice",
                    item_id=item_id,
                    answer_duration_seconds=duration,
                ),
                item_id=item_id,
            )
        else:
            scheduled = await self._schedule_evaluation(
                self._pipeline_answer(
                    text,
                    input_mode="voice",
                    item_id=item_id,
                    answer_duration_seconds=duration,
                ),
                item_id=item_id,
            )
        if not scheduled:
            await self.send("answer.state.changed", state="idle")

    async def _fail_explicit_answer(self, message: str) -> None:
        self._transcript_failed_count += 1
        await self.send(
            "candidate.transcript.failed",
            item_id=self._answer_boundary_item_id or None,
        )
        await self.send(
            "error",
            code="ANSWER_TRANSCRIPT_EMPTY",
            message=message,
            recoverable=True,
        )
        await self.send("answer.state.changed", state="idle")

    async def handle_text(
        self,
        text: str,
        *,
        answer_duration_seconds: float | None = None,
    ) -> None:
        text = text.strip()
        if not text:
            raise AppError("EMPTY_ANSWER", "回答不能为空", status_code=422)
        if self.closed or self.ending:
            raise AppError("INTERVIEW_ENDED", "本场面试正在结束", status_code=409)
        ordinal = len(await self.db.list_turns(self.interview_id)) + 1
        if self.actual_mode == "L0" and self.omni:
            await self.send(
                "candidate.transcript.done", text=text, source="text", ordinal=ordinal
            )
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
                await self._schedule_evaluation(
                    self._pipeline_answer(
                        text,
                        input_mode="text",
                        answer_duration_seconds=answer_duration_seconds,
                    )
                )
                return
            await self._schedule_evaluation(
                self._evaluate_l0(
                    text,
                    input_mode="text",
                    answer_duration_seconds=answer_duration_seconds,
                )
            )
            return
        if self.actual_mode in {"L1", "L2"}:
            await self.send(
                "candidate.transcript.done", text=text, source="text", ordinal=ordinal
            )
            await self._interrupt_for_typed_input()
            await self._schedule_evaluation(
                self._pipeline_answer(
                    text,
                    input_mode="text",
                    answer_duration_seconds=answer_duration_seconds,
                )
            )
            return
        # A voice session that degraded at runtime keeps this lock so a still
        # finishing voice evaluation cannot race a second L3 engine.answer.
        await self._wait_for_current_evaluation()
        await self.send(
            "candidate.transcript.done", text=text, source="text", ordinal=ordinal
        )
        await self._schedule_evaluation(
            self._pipeline_answer(
                text,
                input_mode="text",
                answer_duration_seconds=answer_duration_seconds,
            )
        )

    async def handle_transcript_correction(
        self,
        *,
        text: str,
        ordinal: int | None,
        item_id: str | None,
    ) -> None:
        """Resolve a live ASR item to a persisted turn, re-score, and update it."""

        evaluation_finished = await self._wait_for_current_evaluation(timeout=15.0)
        if not evaluation_finished:
            raise AppError(
                "TRANSCRIPT_NOT_PERSISTED",
                "这段实时转写仍在处理，请等下一题出现后再修正。",
                status_code=409,
            )
        normalized_item_id = str(item_id or "").strip()
        item = self._transcript_items.get(normalized_item_id, {})
        # A browser-provided ordinal is only a display hint for a live ASR
        # item.  Concurrent answers can claim that ordinal before this item is
        # persisted, so item_id mappings must be resolved exclusively from the
        # engine's committed turn.
        resolved_ordinal = (
            item.get("ordinal")
            if normalized_item_id and item.get("persisted") is True
            else (ordinal if not normalized_item_id else None)
        )
        if resolved_ordinal is None:
            raise AppError(
                "TRANSCRIPT_NOT_PERSISTED",
                "这段实时转写仍在处理，请等下一题出现后再修正。",
                status_code=409,
            )
        corrected = await self.engine.correct_answer(
            self.interview_id,
            ordinal=int(resolved_ordinal),
            text=text,
        )
        if normalized_item_id:
            self._transcript_items[normalized_item_id] = {
                **item,
                "ordinal": int(resolved_ordinal),
                "text": text,
                "original_text": corrected["original_text"],
            }
        await self.send(
            "candidate.transcript.corrected",
            **corrected,
            item_id=normalized_item_id or None,
        )

    async def handle_playback_done(self, announcement_id: str) -> None:
        waiter = self._playback_waiters.get(announcement_id)
        if waiter:
            waiter.set()

    async def prepare_end(self, *, drain_timeout: float = 20.0) -> None:
        """Stop input while preserving every accepted voice transcript."""

        if self.answer_boundary_enabled and self.answer_capture_active:
            with suppress(AppError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.handle_answer_end(),
                    timeout=(
                        MICROPHONE_TAIL_SILENCE_TIMEOUT_SECONDS
                        + ANSWER_FINAL_TRANSCRIPT_TIMEOUT_SECONDS
                        + 1
                    ),
                )
        await self.handle_microphone_state(False)
        if self.ending:
            return
        self.ending = True
        self.generation += 1
        self._drop_omni_audio = True
        await self._cancel_omni_audio_releases()
        self._preserving_pending_transcripts = True
        if self._deliberate_interrupt_task and not self._deliberate_interrupt_task.done():
            self._deliberate_interrupt_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._deliberate_interrupt_task
            self._deliberate_interrupt_task = None
        async def drain_and_preserve() -> None:
            # Match the text-mode drain: give an accepted answer time to commit
            # its real assessment before cancelling slow provider work.
            await self._wait_for_current_evaluation(timeout=max(0.0, drain_timeout))
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

            # Cancellation can arrive before the engine commits a scored turn.
            # Keep the already displayed transcript in SQLite as explicitly
            # unscored so report generation cannot race an empty interview.
            pending_items = [
                (item_id, dict(item))
                for item_id, item in self._transcript_items.items()
                if item.get("persisted") is not True
            ]
            for item_id, item in pending_items:
                await self._preserve_unscored_transcript(item_id, item)

            if self.tts_task and self.tts_task is not current and not self.tts_task.done():
                self.tts_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.tts_task

        drain_task = asyncio.create_task(
            drain_and_preserve(),
            name=f"drain-voice-answers-{self.interview_id}",
        )
        try:
            await asyncio.shield(drain_task)
        except asyncio.CancelledError:
            # Ending/report generation may outlive the WebSocket owner. Finish
            # the accepted-transcript write before propagating cancellation.
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(drain_task)
            raise
        finally:
            self._preserving_pending_transcripts = False

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
                self._omni_non_silent_input_pending_response = False
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
                input_interrupted = (
                    response_id in self._omni_input_cancelled_response_ids
                )
                if status != "completed" and not input_interrupted:
                    raise VoiceTransportError(
                        f"Omni 响应未完整完成（status={status or 'missing'}）"
                    )
                if input_interrupted:
                    self._omni_input_cancelled_response_ids.discard(response_id)
                    return
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
                    await self._observe_candidate_partial(
                        str(event.get("text") or "")
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
                    speech_ms = (
                        int(self._speech_duration_seconds * 1000)
                        if self._speech_duration_seconds is not None
                        else None
                    )
                    logger.info(
                        "voice.transcript.done interview_id=%s count=%d chars=%d "
                        "speech_ms=%s language=%s",
                        self.interview_id,
                        self._transcript_done_count,
                        len(text),
                        speech_ms,
                        str(event.get("language") or "unknown"),
                    )
                    self._speech_started_at = None
                    self._speech_duration_seconds = None
                    if self.answer_boundary_enabled:
                        await self._collect_explicit_voice_segment(
                            text=text,
                            provider_item_id=item_id,
                        )
                        continue
                    if text:
                        ordinal = len(await self.db.list_turns(self.interview_id)) + 1
                        if item_id:
                            self._transcript_items[item_id] = {
                                "ordinal": None,
                                "predicted_ordinal": ordinal,
                                "text": text,
                                "original_text": text,
                                "input_mode": "voice",
                                "answer_duration_seconds": (
                                    speech_ms / 1000
                                    if speech_ms is not None
                                    else None
                                ),
                            }
                        await self.send(
                            "candidate.transcript.done",
                            text=text,
                            item_id=event.get("item_id"),
                            ordinal=ordinal,
                            source="voice",
                            editable=True,
                        )
                        await self._schedule_evaluation(
                            self._evaluate_l0(
                                text,
                                input_mode="voice",
                                item_id=item_id or None,
                                answer_duration_seconds=(
                                    speech_ms / 1000 if speech_ms is not None else None
                                ),
                            ),
                            item_id=item_id or None,
                        )
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
                    self._speech_duration_seconds = None
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
                    # A response ID is unique and announcements are serialized;
                    # any older expected input-cancel marker no longer has a
                    # waiter and can be discarded.
                    self._omni_input_cancelled_response_ids.clear()
                    if self._omni_non_silent_input_pending_response:
                        self._omni_non_silent_input_response_ids.add(response_id)
                        self._omni_non_silent_input_pending_response = False
                    self._omni_response_events.setdefault(
                        response_id, asyncio.Event()
                    )
                    self._omni_expected_by_response[response_id] = (
                        self._omni_expected_speech or ""
                    )
                    self._omni_audio_buffers[response_id] = []
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
                    response_id = str(
                        event.get("response_id")
                        or self._omni_active_response_id
                        or ""
                    )
                    expected = self._omni_expected_by_response.get(
                        response_id, self._omni_expected_speech or ""
                    )
                    if response_id:
                        self._omni_spoken_by_response[response_id] = actual
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
                    response_id = str(
                        event.get("response_id")
                        or self._omni_active_response_id
                        or ""
                    )
                    if response_id:
                        self._omni_audio_buffers.setdefault(response_id, []).append(
                            (event["audio"], int(event.get("sample_rate", 24000)))
                        )
                elif event_type == "response_done":
                    response_id = str(
                        event.get("response_id")
                        or self._omni_active_response_id
                        or self._omni_started_response_id
                        or ""
                    ).strip()
                    if not response_id:
                        pending_ids = [
                            candidate_id
                            for candidate_id, waiter in self._omni_response_events.items()
                            if not waiter.is_set()
                        ]
                        if len(pending_ids) == 1:
                            response_id = pending_ids[0]
                    synthetic_response_id = False
                    if not response_id:
                        # Some compatible gateways omit response.id from the
                        # normalized response.done event. Do not tear down an
                        # otherwise healthy realtime session. Create a bounded
                        # waiter and let the locked display text go through the
                        # exact EdgeTTS fallback instead of releasing unknown
                        # provider speech.
                        response_id = f"missing-{uuid.uuid4().hex}"
                        synthetic_response_id = True
                        self._omni_started_response_id = response_id
                        self._omni_response_started.set()
                        self._omni_expected_by_response[response_id] = (
                            self._omni_expected_speech or ""
                        )
                        self._omni_audio_buffers.setdefault(response_id, [])
                        logger.warning(
                            "voice.tts.done_missing_id interview_id=%s event_fields=%s",
                            self.interview_id,
                            ",".join(sorted(str(key) for key in event)),
                        )
                    status = str(event.get("status") or "").strip().lower()
                    if synthetic_response_id and not status:
                        status = "completed"
                    self._omni_response_statuses[response_id] = status
                    input_cancelled_response = (
                        status in {"cancelled", "canceled"}
                        and response_id
                        in self._omni_non_silent_input_response_ids
                    )
                    self._omni_non_silent_input_response_ids.discard(response_id)
                    if input_cancelled_response:
                        self._omni_input_cancelled_response_ids.add(response_id)
                    response_complete = self._omni_response_events.setdefault(
                        response_id, asyncio.Event()
                    )
                    if response_id == self._omni_active_response_id or (
                        self._omni_active_response_id is None
                        and response_id == self._omni_started_response_id
                    ):
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
                        self._start_verified_omni_audio_release(
                            response_id,
                            generation=self.generation,
                        )
                    else:
                        self._omni_audio_buffers.pop(response_id, None)
                        self._omni_expected_by_response.pop(response_id, None)
                        self._omni_spoken_by_response.pop(response_id, None)
                        response_complete.set()
                    expected_cancel = (
                        self._omni_cancel_in_flight
                        or self._drop_omni_audio
                        or input_cancelled_response
                    )
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

    @staticmethod
    def _speech_signature(text: str) -> str:
        # Punctuation and whitespace are not audible content.  Everything
        # else, including numbers and technical terms, must remain identical.
        return re.sub(r"[\s，。！？,.!?；;：:“”‘’'\"（）()、]", "", text).casefold()

    def _start_verified_omni_audio_release(
        self,
        response_id: str,
        *,
        generation: int,
    ) -> None:
        """Release buffered L0 audio without blocking provider VAD events."""

        existing = self._omni_release_tasks.get(response_id)
        if existing is not None and not existing.done():
            return
        self._omni_response_events.setdefault(response_id, asyncio.Event())

        async def release() -> None:
            try:
                await self._release_verified_omni_audio(
                    response_id,
                    generation=generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.closed:
                    with suppress(Exception):
                        await self.send(
                            "error",
                            code="VOICE_PROVIDER_ERROR",
                            message=f"L0 语音下发失败：{exc}",
                            recoverable=True,
                        )
                    if self.actual_mode == "L0":
                        await self._runtime_fallback("L0 语音下发不可用")
            finally:
                self._finish_omni_audio_release(
                    response_id,
                    asyncio.current_task(),
                )

        task = asyncio.create_task(
            release(),
            name=f"omni-audio-release-{self.interview_id}-{response_id}",
        )
        self._omni_release_tasks[response_id] = task
        # A queued speech_started event can cancel this task before its
        # coroutine executes, in which case its ``finally`` block never runs.
        # The callback still releases the matching announce() waiter.
        task.add_done_callback(
            lambda completed, rid=response_id: self._finish_omni_audio_release(
                rid, completed
            )
        )

    def _finish_omni_audio_release(
        self,
        response_id: str,
        task: asyncio.Task[Any] | None,
    ) -> None:
        self._omni_audio_buffers.pop(response_id, None)
        self._omni_expected_by_response.pop(response_id, None)
        self._omni_spoken_by_response.pop(response_id, None)
        if task is not None and self._omni_release_tasks.get(response_id) is task:
            self._omni_release_tasks.pop(response_id, None)
        # ``announce()`` must not start another response until either verified
        # audio and its marker were sent, or a barge-in definitively cancelled
        # this release.
        self._omni_response_events.setdefault(response_id, asyncio.Event()).set()

    def _omni_release_allowed(self, response_id: str, generation: int) -> bool:
        return (
            not self.closed
            and generation == self.generation
            and not self._drop_omni_audio
            and self._omni_release_tasks.get(response_id) is asyncio.current_task()
        )

    async def _release_verified_omni_audio(
        self,
        response_id: str,
        *,
        generation: int,
    ) -> None:
        expected = self._omni_expected_by_response.pop(response_id, "")
        provider_spoken = self._omni_spoken_by_response.pop(response_id, "")
        chunks = self._omni_audio_buffers.pop(response_id, [])
        if not self._omni_release_allowed(response_id, generation):
            return
        exact_content = bool(expected) and self._speech_signature(
            provider_spoken
        ) == self._speech_signature(expected)
        if exact_content:
            for audio, sample_rate in chunks:
                # The release runs outside the provider consumer so queued VAD
                # events get a chance to cancel it between buffered chunks.
                await asyncio.sleep(0)
                if not self._omni_release_allowed(response_id, generation):
                    return
                await self.send(
                    "audio.chunk",
                    audio=base64.b64encode(audio).decode("ascii"),
                    sample_rate=sample_rate,
                    format="pcm_s16le",
                )
                if not self._omni_release_allowed(response_id, generation):
                    return
            if not self._omni_release_allowed(response_id, generation):
                return
            await self.send(
                "interviewer.audio.synced",
                text=expected,
                spoken_text=provider_spoken,
                audio_transcript=provider_spoken,
                exact_match=True,
                fallback_tts=False,
            )
        else:
            # Never play a known paraphrase.  A conventional TTS engine takes
            # the locked display string as direct synthesis input, so the
            # spoken content and transcript stay on the same source of truth.
            if not self._omni_release_allowed(response_id, generation):
                return
            await self.send("audio.clear")
            if not self._omni_release_allowed(response_id, generation):
                return
            try:
                fallback_tts = _edge_tts_for_language(
                    str(self.interview.get("language_mode") or "bilingual")
                )
                audio, mime_type = await fallback_tts.synthesize(expected)
                if not self._omni_release_allowed(response_id, generation):
                    return
                self._audio_output_chunks += 1
                self._audio_output_bytes += len(audio)
                encoded = base64.b64encode(audio).decode("ascii")
                if mime_type.startswith("audio/pcm"):
                    rate = 24000
                    if "rate=" in mime_type:
                        with suppress(ValueError):
                            rate = int(mime_type.split("rate=", 1)[1].split(";", 1)[0])
                    await self.send(
                        "audio.chunk",
                        audio=encoded,
                        sample_rate=rate,
                        format="pcm_s16le",
                    )
                else:
                    await self.send("audio.file", audio=encoded, mime_type=mime_type)
                if not self._omni_release_allowed(response_id, generation):
                    return
                await self.send(
                    "interviewer.audio.synced",
                    text=expected,
                    spoken_text=expected,
                    audio_transcript=expected,
                    discarded_provider_transcript=provider_spoken,
                    exact_match=False,
                    fallback_tts=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._omni_release_allowed(response_id, generation):
                    return
                await self.send(
                    "error",
                    code="VOICE_TEXT_MISMATCH",
                    message=f"检测到语音与题目文字不一致，已阻止播放；精确朗读兜底失败：{exc}",
                    recoverable=True,
                )
        # Ordered after the verified/fallback audio; the browser only marks the
        # interviewer idle after its playback queue drains.
        if not self._omni_release_allowed(response_id, generation):
            return
        await self.send("audio.stream.done", announcement_id=response_id)

    async def _cancel_omni_audio_releases(self) -> None:
        current = asyncio.current_task()
        releases = [
            (response_id, task)
            for response_id, task in self._omni_release_tasks.items()
            if task is not current and not task.done()
        ]
        for _, task in releases:
            task.cancel()
        if releases:
            await asyncio.gather(
                *(task for _, task in releases),
                return_exceptions=True,
            )
            for response_id, task in releases:
                self._finish_omni_audio_release(response_id, task)

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
                    await self._observe_candidate_partial(
                        str(event.get("text") or "")
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
                    speech_ms = (
                        int(self._speech_duration_seconds * 1000)
                        if self._speech_duration_seconds is not None
                        else None
                    )
                    self._speech_started_at = None
                    self._speech_duration_seconds = None
                    if self.answer_boundary_enabled:
                        await self._collect_explicit_voice_segment(
                            text=text,
                            provider_item_id=str(event.get("item_id") or ""),
                        )
                        continue
                    if text:
                        item_id = str(event.get("item_id") or uuid.uuid4().hex)
                        ordinal = len(await self.db.list_turns(self.interview_id)) + 1
                        self._transcript_items[item_id] = {
                            "ordinal": None,
                            "predicted_ordinal": ordinal,
                            "text": text,
                            "original_text": text,
                            "input_mode": "voice",
                            "answer_duration_seconds": (
                                speech_ms / 1000 if speech_ms is not None else None
                            ),
                        }
                        await self.send(
                            "candidate.transcript.done",
                            text=text,
                            item_id=item_id,
                            ordinal=ordinal,
                            source="voice",
                            editable=True,
                        )
                        await self._schedule_evaluation(
                            self._pipeline_answer(
                                text,
                                input_mode="voice",
                                item_id=item_id,
                                answer_duration_seconds=(
                                    speech_ms / 1000 if speech_ms is not None else None
                                ),
                            ),
                            item_id=item_id,
                        )
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

    async def _evaluate_l0(
        self,
        text: str,
        *,
        input_mode: str = "voice",
        item_id: str | None = None,
        answer_duration_seconds: float | None = None,
    ) -> None:
        async with self.answer_lock:
            await self.send("interviewer.state", state="thinking")
            try:
                result = await self._engine_answer(
                    text,
                    input_mode=input_mode,
                    answer_duration_seconds=answer_duration_seconds,
                )
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
            if item_id and item_id in self._transcript_items and hasattr(result, "turn"):
                self._transcript_items[item_id]["ordinal"] = result.turn.ordinal
                self._transcript_items[item_id]["persisted"] = True
            if self.ending and not result.ended:
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
                resume_consistency=getattr(result, "resume_consistency", "supported"),
                resume_mismatch_reason=getattr(result, "resume_mismatch_reason", ""),
                resume_selection_warning=getattr(result, "resume_selection_warning", False),
                recommended_answer_seconds=getattr(
                    result,
                    "recommended_answer_seconds",
                    InterviewEngine.recommended_answer_seconds(result.question),
                ),
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

    async def _pipeline_answer(
        self,
        text: str,
        *,
        input_mode: str = "voice",
        item_id: str | None = None,
        answer_duration_seconds: float | None = None,
    ) -> None:
        async with self.answer_lock:
            await self.send("interviewer.state", state="thinking")
            try:
                result = await self._engine_answer(
                    text,
                    input_mode=input_mode,
                    answer_duration_seconds=answer_duration_seconds,
                )
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
            if item_id and item_id in self._transcript_items and hasattr(result, "turn"):
                self._transcript_items[item_id]["ordinal"] = result.turn.ordinal
                self._transcript_items[item_id]["persisted"] = True
            if self.ending and not result.ended:
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
                resume_consistency=getattr(result, "resume_consistency", "supported"),
                resume_mismatch_reason=getattr(result, "resume_mismatch_reason", ""),
                resume_selection_warning=getattr(result, "resume_selection_warning", False),
                recommended_answer_seconds=getattr(
                    result,
                    "recommended_answer_seconds",
                    InterviewEngine.recommended_answer_seconds(result.question),
                ),
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

    async def _engine_answer(
        self,
        text: str,
        *,
        input_mode: str,
        answer_duration_seconds: float | None,
    ) -> EngineResult:
        parameters = inspect.signature(self.engine.answer).parameters
        if "input_mode" not in parameters:
            return await self.engine.answer(self.interview_id, text)
        return await self.engine.answer(
            self.interview_id,
            text,
            input_mode=input_mode,
            answer_duration_seconds=answer_duration_seconds,
        )

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
                    self.tts = _edge_tts_for_language(
                        str(self.interview.get("language_mode") or "bilingual")
                    )
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
            await self.send(
                "interviewer.audio.synced",
                text=text,
                spoken_text=text,
                audio_transcript=text,
                exact_match=True,
                fallback_tts=False,
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
        if self.omni or self._omni_release_tasks:
            self._drop_omni_audio = True
        await self._cancel_omni_audio_releases()
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
        if self.omni or self._omni_release_tasks:
            self._drop_omni_audio = True
        await self._cancel_omni_audio_releases()
        if self.omni and (
            self._omni_response_pending or self._omni_active_response_id
        ):
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

    async def _wait_for_current_evaluation(self, timeout: float = 7.0) -> bool:
        current = asyncio.current_task()
        pending = [
            task
            for task in self.evaluation_tasks
            if task is not current and not task.done()
        ]
        if not pending:
            return True
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        return not still_pending

    async def _candidate_speech_started(
        self, *, source: str, cancel_provider: bool = True
    ) -> None:
        if self.answer_boundary_enabled and not self.answer_capture_active:
            # A provider event can arrive after the explicit answer boundary
            # has closed.  It may still deliver the pending final transcript,
            # but it must not barge into the next interviewer question.
            return
        was_speaking = self._candidate_speaking
        self._speech_duration_seconds = None
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
        if (
            self.answer_boundary_enabled
            and self.answer_capture_active
            and not was_speaking
        ):
            self._answer_boundary_speech_started_count += 1
        self._latest_candidate_partial = ""
        self._expression_interrupt_reason = ""
        await self._barge_in(source=source, cancel_provider=cancel_provider)

    async def _observe_candidate_partial(self, text: str) -> None:
        """Only arm a pressure interruption when expression has clearly failed."""

        self._latest_candidate_partial = text.strip()
        raw_stress_level = self.interview.get("stress_level")
        stress_level = int(
            raw_stress_level
            if raw_stress_level is not None
            else (2 if self.interview.get("stress") else 0)
        )
        if (
            stress_level < 2
            or self.ending
            or self.closed
            or (self.answer_boundary_enabled and not self.answer_capture_active)
        ):
            return
        reason = self._expression_issue(self._latest_candidate_partial)
        if not reason:
            return
        turns = await self.db.list_turns(self.interview_id)
        ordinal = len(turns) + 1
        if ordinal in self._deliberate_interrupt_ordinals:
            return
        if self._deliberate_interrupt_task and not self._deliberate_interrupt_task.done():
            return
        self._expression_interrupt_reason = reason
        self._deliberate_interrupt_task = asyncio.create_task(
            self._deliberate_interrupt_after_delay(ordinal, reason),
            name=f"pressure-interrupt-{self.interview_id}-{ordinal}",
        )

    def _expression_issue(self, text: str) -> str:
        if InterviewEngine._explicit_resume_mismatch(text):
            return "resume_mismatch"
        compact = re.sub(r"[\s，,。.!！？?；;：:]", "", text)
        elapsed = (
            time.monotonic() - self._speech_started_at
            if self._speech_started_at is not None
            else 0.0
        )
        filler_pattern = r"(?:嗯+|呃+|额+|那个|这个|就是|然后|怎么说|大概吧)"
        fillers = re.findall(filler_pattern, text)
        repeated_filler = re.search(
            r"(然后|就是|这个|那个)(?:[，,、\s]*(?:然后|就是|这个|那个)){2,}",
            text,
        )
        if len(compact) >= 36 and (len(fillers) >= 5 or repeated_filler):
            return "rambling"
        meaningful = re.sub(filler_pattern, "", compact)
        if elapsed >= 12 and len(meaningful) < 10:
            return "stalled"
        return ""

    async def _candidate_speech_ended(self) -> None:
        if self._candidate_speaking and self._speech_started_at is not None:
            self._speech_duration_seconds = max(
                0.0, time.monotonic() - self._speech_started_at
            )
        self._candidate_speaking = False
        task = self._deliberate_interrupt_task
        if task and not task.done() and not self._deliberate_interrupt_firing:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._deliberate_interrupt_task is task:
                self._deliberate_interrupt_task = None

    async def _deliberate_interrupt_after_delay(
        self,
        ordinal: int,
        reason: str = "rambling",
    ) -> None:
        try:
            delay = max(
                0.0,
                float(getattr(self.settings, "pressure_interrupt_seconds", 4)),
            )
            await asyncio.sleep(delay)
            if not self._candidate_speaking or self.ending or self.closed:
                return
            if self._expression_issue(self._latest_candidate_partial) != reason:
                return
            self._deliberate_interrupt_firing = True
            self._deliberate_interrupt_ordinals.add(ordinal)
            if self.interview.get("language_mode") == "en":
                text = (
                    "Let me pause you. You said the selected resume may not be yours. "
                    "Please confirm it first; you can also exit and restart from the home page."
                    if reason == "resume_mismatch"
                    else
                    "I am going to pause you because the explanation is becoming circular. "
                    "State the conclusion in one sentence, then explain the approach, evidence, and result."
                    if reason == "rambling"
                    else "Let me pause you. You have not reached an answer yet; start with the core conclusion."
                )
            else:
                text = (
                    "我先打断一下。你提到当前简历可能不是你的，请先确认是否选错；"
                    "你也可以点击“退出”返回首页重新选择。"
                    if reason == "resume_mismatch"
                    else
                    "我先打断一下，你这段有点绕。先用一句话说清结论，"
                    "再按做法、依据和结果展开。"
                    if reason == "rambling"
                    else "先停一下，你还没有进入有效回答。先明确要回答的核心结论。"
                )
            await self.send(
                "pressure.interrupt",
                text=text,
                ordinal=ordinal,
                reason=reason,
                resume_selection_warning=reason == "resume_mismatch",
            )
            await self.send(
                "interviewer.text.done",
                text=text,
                pressure_action="interrupt",
                interjection=True,
                resume_consistency=("mismatch" if reason == "resume_mismatch" else "uncertain"),
                resume_mismatch_reason=(
                    "候选人明确表示当前简历并非本人材料或选择有误。"
                    if reason == "resume_mismatch" else ""
                ),
                resume_selection_warning=reason == "resume_mismatch",
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

    async def _preserve_unscored_transcript(
        self,
        item_id: str,
        item: dict[str, Any],
    ) -> None:
        preserve = getattr(self.engine, "preserve_unscored_answer", None)
        if preserve is None:
            await self._fail_pending_transcript(item_id)
            return
        predicted_ordinal = item.get("predicted_ordinal")
        preserve_task = asyncio.create_task(
            preserve(
                self.interview_id,
                str(item.get("text") or ""),
                input_mode=str(item.get("input_mode") or "voice"),
                answer_duration_seconds=item.get("answer_duration_seconds"),
                ordinal=(
                    int(predicted_ordinal)
                    if predicted_ordinal is not None
                    else None
                ),
            ),
            name=f"preserve-voice-answer-{self.interview_id}-{item_id}",
        )
        turn: Any = None
        cancelled = False
        try:
            turn = await asyncio.shield(preserve_task)
        except asyncio.CancelledError:
            cancelled = True
            # Complete the independent SQLite write before propagating
            # cancellation so report generation cannot overtake it.
            with suppress(asyncio.CancelledError, Exception):
                turn = await asyncio.shield(preserve_task)
        except Exception as exc:
            if not self.closed:
                await self.send(
                    "error",
                    code="ANSWER_PRESERVE_FAILED",
                    message=f"结束时保留最后一条回答失败：{exc}",
                    recoverable=True,
                )
            await self._fail_pending_transcript(item_id)
            return

        if turn is not None and item_id in self._transcript_items:
            committed_ordinal = getattr(turn, "ordinal", predicted_ordinal)
            self._transcript_items[item_id].update(
                ordinal=committed_ordinal,
                persisted=True,
                unscored=True,
            )
        if cancelled:
            raise asyncio.CancelledError

    async def _fail_pending_transcript(self, item_id: str | None) -> None:
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            return
        item = self._transcript_items.pop(normalized_item_id, None)
        if item is None:
            return
        self._transcript_failed_count += 1
        if not self.closed:
            await self.send(
                "candidate.transcript.failed",
                item_id=normalized_item_id,
            )

    async def _schedule_evaluation(
        self,
        coroutine: Awaitable[None],
        *,
        item_id: str | None = None,
    ) -> bool:
        normalized_item_id = str(item_id or "").strip() or None
        if self.closed or self.ending:
            if hasattr(coroutine, "close"):
                coroutine.close()  # type: ignore[attr-defined]
            await self._fail_pending_transcript(normalized_item_id)
            return False
        if self._answer_pending or self.answer_lock.locked():
            if hasattr(coroutine, "close"):
                coroutine.close()  # type: ignore[attr-defined]
            await self._fail_pending_transcript(normalized_item_id)
            await self.send(
                "error",
                code="ANSWER_IN_PROGRESS",
                message="上一轮仍在生成，请稍候",
                recoverable=True,
            )
            return False
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
                try:
                    item = (
                        self._transcript_items.get(normalized_item_id)
                        if normalized_item_id
                        else None
                    )
                    if (
                        item is not None
                        and item.get("persisted") is not True
                        and not self._preserving_pending_transcripts
                    ):
                        await self._fail_pending_transcript(normalized_item_id)
                finally:
                    self._answer_pending = False
                    if (
                        self.answer_boundary_enabled
                        and not self.closed
                        and not self.ending
                    ):
                        await self.send("answer.state.changed", state="idle")

        task = asyncio.create_task(
            run(), name=f"voice-answer-{self.interview_id}"
        )
        self.evaluation_tasks.add(task)
        task.add_done_callback(self.evaluation_tasks.discard)
        return True

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
            self._drop_omni_audio = True
            await self._cancel_omni_audio_releases()
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
            self._drop_omni_audio = True
            await self._cancel_omni_audio_releases()
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
        """Finish transport cleanup across cancellation, with a hard bound."""

        task = asyncio.create_task(awaitable, name=name)

        def consume_result(done: asyncio.Task[Any]) -> None:
            if done.cancelled():
                return
            with suppress(Exception):
                done.exception()

        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=VOICE_CLEANUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            task.cancel()
            task.add_done_callback(consume_result)
            return
        except asyncio.CancelledError:
            # The independent close/join remains alive under shield. Await it
            # to completion before propagating parent cancellation so provider
            # clients cannot be left in a half-closed, non-retryable state.
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=VOICE_CLEANUP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                task.cancel()
                task.add_done_callback(consume_result)
            except Exception:
                pass
            raise
        except Exception:
            # Cleanup errors must not mask the original provider failure.
            return
