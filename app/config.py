from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - python-dotenv is installed in normal runs
    pass


ROOT_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    host: str
    port: int
    voice_mode: str
    voice_auto_fallback: bool
    dashscope_api_key: str
    dashscope_workspace_id: str
    dashscope_base_url: str
    qwen_text_model: str
    qwen_report_model: str
    qwen_realtime_model: str
    dashscope_realtime_url: str
    paraformer_model: str
    dashscope_asr_url: str
    cosyvoice_model: str
    cosyvoice_voice: str
    edge_tts_voice: str
    db_path: Path
    mock_llm: bool
    llm_timeout_seconds: int
    max_pdf_mb: int
    daily_interview_limit: int
    client_daily_interview_limit: int
    pressure_interrupt_seconds: int
    allowed_origins: tuple[str, ...]

    @property
    def has_api_key(self) -> bool:
        return bool(self.dashscope_api_key)

    @property
    def realtime_ws_url(self) -> str:
        base = self.dashscope_realtime_url.strip()
        if base:
            separator = "&" if "?" in base else "?"
            if "model=" not in base:
                return f"{base}{separator}model={self.qwen_realtime_model}"
            return base
        if self.dashscope_workspace_id:
            return (
                f"wss://{self.dashscope_workspace_id}.cn-beijing.maas.aliyuncs.com"
                f"/api-ws/v1/realtime?model={self.qwen_realtime_model}"
            )
        return (
            "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
            f"?model={self.qwen_realtime_model}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    mode = os.getenv("VOICE_MODE", "L3").strip().upper()
    if mode not in {"L0", "L1", "L2", "L3"}:
        raise RuntimeError("VOICE_MODE 必须是 L0、L1、L2 或 L3")

    workspace = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
    dedicated_http = (
        f"https://{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else ""
    )
    origins = tuple(
        item.strip()
        for item in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    )
    return Settings(
        app_name="AI 模拟面试官",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_int("PORT", 8000),
        voice_mode=mode,
        voice_auto_fallback=_bool("VOICE_AUTO_FALLBACK", True),
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        dashscope_workspace_id=workspace,
        dashscope_base_url=os.getenv(
            "DASHSCOPE_BASE_URL",
            f"{dedicated_http}/compatible-mode/v1"
            if dedicated_http
            else "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/"),
        qwen_text_model=os.getenv("QWEN_TEXT_MODEL", "qwen-plus"),
        qwen_report_model=os.getenv("QWEN_REPORT_MODEL", "qwen-plus"),
        qwen_realtime_model=os.getenv(
            "QWEN_REALTIME_MODEL", "qwen3.5-omni-flash-realtime"
        ),
        dashscope_realtime_url=os.getenv("DASHSCOPE_REALTIME_URL", ""),
        paraformer_model=os.getenv("PARAFORMER_MODEL", "paraformer-realtime-v2"),
        dashscope_asr_url=os.getenv(
            "DASHSCOPE_ASR_URL",
            f"wss://{workspace}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
            if workspace
            else "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        ),
        cosyvoice_model=os.getenv("COSYVOICE_MODEL", "cosyvoice-v3-flash"),
        cosyvoice_voice=os.getenv("COSYVOICE_VOICE", "longanyang"),
        edge_tts_voice=os.getenv("EDGE_TTS_VOICE", "zh-CN-YunxiNeural"),
        db_path=Path(os.getenv("DB_PATH", str(ROOT_DIR / "data" / "interviews.db"))),
        mock_llm=_bool("MOCK_LLM", False),
        llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 90),
        max_pdf_mb=_int("MAX_PDF_MB", 8),
        daily_interview_limit=_int("DAILY_INTERVIEW_LIMIT", 20),
        client_daily_interview_limit=_int("CLIENT_DAILY_INTERVIEW_LIMIT", 5),
        pressure_interrupt_seconds=_int("PRESSURE_INTERRUPT_SECONDS", 4),
        allowed_origins=origins,
    )
