"""Request and response bodies."""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tradingagents.portfolio import PortfolioContext

_TICKER = re.compile(r"^[A-Za-z0-9.\-^=_]{1,32}$")


class RunOptions(BaseModel):
    """Per-run overrides of the server's TRADINGAGENTS_* configuration."""

    model_config = ConfigDict(extra="forbid")

    llm_provider: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    deep_think_provider: str | None = None
    quick_think_provider: str | None = None
    output_language: str | None = None
    max_debate_rounds: int | None = Field(default=None, ge=1, le=10)
    max_risk_discuss_rounds: int | None = Field(default=None, ge=1, le=10)
    max_tool_rounds: int | None = Field(default=None, ge=1, le=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    openai_reasoning_effort: str | None = None
    google_thinking_level: str | None = None
    anthropic_effort: str | None = None

    def overrides(self) -> dict:
        return self.model_dump(exclude_none=True)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(description="Yahoo Finance ticker, e.g. NVDA, 0700.HK, MNS.WA, BTC-USD")
    date: str | None = Field(default=None, description="Analysis date YYYY-MM-DD; default today (UTC)")
    analysts: list[str] | None = Field(
        default=None,
        description="Any of market, sentiment, news, fundamentals; default all the asset type allows",
    )
    asset_type: Literal["stock", "crypto"] | None = Field(
        default=None, description="Default: detected from the ticker"
    )
    portfolio: PortfolioContext | None = Field(
        default=None, description="Inline portfolio (same shape as my_book.json.example)"
    )
    use_saved_portfolio: bool = Field(
        default=True,
        description="When no inline portfolio is given, use the one stored with PUT /portfolio (if any)",
    )
    options: RunOptions = Field(default_factory=RunOptions)
    html: bool = Field(default=True, description="Also write complete_report.html")
    callback_url: str | None = Field(
        default=None, description="POSTed the final run status when the run ends"
    )

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        v = v.strip()
        if not _TICKER.match(v):
            raise ValueError("ticker may hold only letters, digits and . - ^ = _ (max 32)")
        return v

    @field_validator("date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            parsed = _date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD") from exc
        if parsed.isoformat() != v:
            raise ValueError("date must be YYYY-MM-DD")
        return v

    @field_validator("callback_url")
    @classmethod
    def _callback(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://", v):
            raise ValueError("callback_url must be an http(s) URL")
        return v
