"""Server settings, read from TA_API_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]
    data_dir: Path
    max_concurrent_runs: int = 1
    run_timeout_seconds: int = 3600
    run_ttl_hours: int = 24
    callback_secret: str | None = None
    docs_enabled: bool = True

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def portfolio_path(self) -> Path:
        return self.data_dir / "portfolio.json"


def load_settings() -> Settings:
    keys = tuple(k.strip() for k in os.environ.get("TA_API_KEY", "").split(",") if k.strip())
    if not keys:
        raise RuntimeError(
            "TA_API_KEY is not set. Set it to one key (or several, comma-separated, for rotation)."
        )
    default_dir = Path.home() / ".tradingagents" / "api"
    return Settings(
        api_keys=keys,
        data_dir=Path(os.environ.get("TA_API_DATA_DIR") or default_dir),
        max_concurrent_runs=max(1, _int("TA_API_MAX_CONCURRENT_RUNS", 1)),
        run_timeout_seconds=max(60, _int("TA_API_RUN_TIMEOUT_SECONDS", 3600)),
        run_ttl_hours=max(1, _int("TA_API_RUN_TTL_HOURS", 24)),
        callback_secret=os.environ.get("TA_API_CALLBACK_SECRET") or None,
        docs_enabled=os.environ.get("TA_API_DOCS", "true").strip().lower() not in ("0", "false", "no", "off"),
    )
