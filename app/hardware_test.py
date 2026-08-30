from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable

from .config import Settings
from .voice import OmniRealtimeClient, ParaformerClient


SendEvent = Callable[..., Awaitable[None]]
logger = logging.getLogger("uvicorn.error.hardware_test")

HARDWARE_TEST_MAX_SECONDS = 30
HARDWARE_TEST_MAX_PCM_BYTES = 16000 * 2 * HARDWARE_TEST_MAX_SECONDS
HARDWARE_TEST_TAIL_FRAMES = 16
HARDWARE_TEST_TAIL_TIMEOUT_SECONDS = 3.0
HARDWARE_TEST_FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 4.0


def _fallback_chain(requested: str, enabled: bool) -> list[str]:
    modes = ["L0", "L1", "L2", "L3"]
    normalized = requested.upper()
    if normalized not in modes:
        normalized = "L3"
    return modes[modes.index(normalized) :] if enabled else [normalized]


class HardwareTranscriptionSession:
    """Short-lived ASR-only session used before an interview starts.

    It deliberately reuses the same Alibaba realtime transports as the real
    interview. No resume, interview prompt, score or history row is created.
    """

    def __init__(
        self,
        settings: Settings,
        send: SendEvent,
        *,
        max_seconds: int | None = None,
    ) -> None:
        self.settings = settings
        self.send = send
        self.max_seconds = max_seconds
        self.omni: OmniRealtimeClient | None = None
        self.asr: ParaformerClient | None = None
        self.provider_task: asyncio.Task[None] | None = None
        self.actual_mode = "L3"
        self.closed = False
        self.stopped = False
        self.audio_bytes = 0
        self.heard_speech = False
        self.final_transcript = asyncio.Event()

    @property
    def transcription_available(self) -> bool:
        return self.actual_mode != "L3"

    @property
    def audio_limit_bytes(self) -> int:
        if self.max_seconds is None:
            # Keep the module constant patchable for the focused unit tests.
            return HARDWARE_TEST_MAX_PCM_BYTES
        return 16000 * 2 * self.max_seconds

    async def start(self) -> bool:
        errors: list[str] = []
        pipeline_attempted = False
        for mode in _fallback_chain(
            self.settings.voice_mode,
            self.settings.voice_auto_fallback,
        ):
            if mode == "L3":
                break
            try:
                if mode == "L0":
                    self.omni = OmniRealtimeClient(
                        "你只负责麦克风测试中的中英文实时转写。不要回答、评价或生成语音。"
                    )
                    await self.omni.start()
                    self.actual_mode = "L0"
                    self.provider_task = asyncio.create_task(
                        self._consume_omni(),
                        name="hardware-test-omni",
                    )
                else:
                    if pipeline_attempted:
                        continue
                    pipeline_attempted = True
                    self.asr = ParaformerClient(language_hints=["zh", "en"])
                    await self.asr.start()
                    self.actual_mode = mode
                    self.provider_task = asyncio.create_task(
                        self._consume_paraformer(),
                        name="hardware-test-paraformer",
                    )
                await self.send(
                    "hardware.ready",
                    transcription_available=True,
                    max_seconds=self.max_seconds or HARDWARE_TEST_MAX_SECONDS,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(type(exc).__name__)
                await self._close_provider()

        self.actual_mode = "L3"
        logger.warning(
            "hardware_test.provider_unavailable attempts=%d errors=%s",
            len(errors),
            ",".join(errors) or "none",
        )
        await self.send(
            "hardware.ready",
            transcription_available=False,
            max_seconds=self.max_seconds or HARDWARE_TEST_MAX_SECONDS,
        )
        return False

    async def handle_audio(self, pcm: bytes) -> None:
        if self.closed or self.stopped or not pcm:
            return
        if len(pcm) % 2:
            await self.send(
                "hardware.error",
                code="INVALID_AUDIO_FRAME",
                message="麦克风测试音频格式不正确",
                recoverable=True,
            )
            return
        remaining = self.audio_limit_bytes - self.audio_bytes
        if remaining <= 0:
            await self.stop(reason="limit")
            return
        frame = pcm[: remaining - (remaining % 2)]
        self.audio_bytes += len(frame)
        try:
            if self.omni:
                await self.omni.send_audio(frame)
            elif self.asr:
                await self.asr.send_audio(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "hardware_test.audio_failed provider=%s error=%s",
                self.actual_mode,
                type(exc).__name__,
            )
            await self.send(
                "hardware.error",
                code="TRANSCRIPTION_UNAVAILABLE",
                message="实时转写连接中断，麦克风状态仍可继续查看",
                recoverable=True,
            )
            await self._close_provider()
            self.actual_mode = "L3"

    async def stop(self, *, reason: str = "manual") -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.transcription_available:
            silence = b"\x00\x00" * 1600

            async def flush_tail() -> None:
                for _ in range(HARDWARE_TEST_TAIL_FRAMES):
                    if self.omni:
                        await self.omni.send_audio(silence)
                    elif self.asr:
                        await self.asr.send_audio(silence)
                    else:
                        return
                    await asyncio.sleep(0)

            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    flush_tail(),
                    timeout=HARDWARE_TEST_TAIL_TIMEOUT_SECONDS,
                )
            if self.heard_speech and not self.final_transcript.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self.final_transcript.wait(),
                        timeout=HARDWARE_TEST_FINAL_TRANSCRIPT_TIMEOUT_SECONDS,
                    )
        await self.send("hardware.stopped", reason=reason)

    async def _consume_omni(self) -> None:
        assert self.omni is not None
        try:
            async for event in self.omni.events():
                event_type = str(event.get("type") or "")
                if event_type == "speech_started":
                    self.heard_speech = True
                    self.final_transcript.clear()
                    await self.send("hardware.speech.started")
                elif event_type == "speech_ended":
                    await self.send("hardware.speech.ended")
                elif event_type == "user_partial":
                    text = str(event.get("text") or "")
                    if text:
                        self.heard_speech = True
                        await self.send("hardware.transcript.partial", text=text)
                elif event_type == "user_done":
                    text = str(event.get("text") or "").strip()
                    if text:
                        await self.send("hardware.transcript.done", text=text)
                    self.final_transcript.set()
                elif event_type in {"error", "transcription_error"}:
                    await self.send(
                        "hardware.error",
                        code="TRANSCRIPTION_FAILED",
                        message="这段声音没有识别清楚，请靠近麦克风再试一次",
                        recoverable=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                logger.warning(
                    "hardware_test.omni_failed error=%s",
                    type(exc).__name__,
                )
                await self.send(
                    "hardware.error",
                    code="TRANSCRIPTION_UNAVAILABLE",
                    message="实时转写连接中断，麦克风状态仍可继续查看",
                    recoverable=True,
                )
        finally:
            self.final_transcript.set()

    async def _consume_paraformer(self) -> None:
        assert self.asr is not None
        try:
            async for event in self.asr.events():
                event_type = str(event.get("type") or "")
                if event_type == "user_partial":
                    text = str(event.get("text") or "")
                    if text:
                        self.heard_speech = True
                        await self.send("hardware.transcript.partial", text=text)
                elif event_type == "user_done":
                    text = str(event.get("text") or "").strip()
                    if text:
                        self.heard_speech = True
                        await self.send("hardware.transcript.done", text=text)
                    self.final_transcript.set()
                elif event_type == "error":
                    await self.send(
                        "hardware.error",
                        code="TRANSCRIPTION_FAILED",
                        message="这段声音没有识别清楚，请再试一次",
                        recoverable=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                logger.warning(
                    "hardware_test.paraformer_failed error=%s",
                    type(exc).__name__,
                )
                await self.send(
                    "hardware.error",
                    code="TRANSCRIPTION_UNAVAILABLE",
                    message="实时转写连接中断，麦克风状态仍可继续查看",
                    recoverable=True,
                )
        finally:
            self.final_transcript.set()

    async def _close_provider(self) -> None:
        omni, asr = self.omni, self.asr
        self.omni = None
        self.asr = None
        if omni:
            with suppress(Exception):
                await asyncio.wait_for(omni.close(), timeout=5)
        if asr:
            with suppress(Exception):
                await asyncio.wait_for(asr.close(), timeout=5)
        current = asyncio.current_task()
        task = self.provider_task
        self.provider_task = None
        if task and task is not current and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=2)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._close_provider()
