# ------------------------------------------------------------------------------
# This module defines runtime context used by the worker orchestration loop.
# ------------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import AppConfig
from app.telegram_bot import TelegramConfig


# ------------------------------------------------------------------------------
# This dataclass groups shared runtime values for worker orchestration.
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkerRuntimeContext:
    CONFIG: AppConfig
    TELEGRAM: TelegramConfig
    LOG_FILE: Path
    APPLE_ID_LABEL: str
