"""Usage limits for the assistant: questions per person per day, spend per month.

Spend is an estimate from the token counts the API reports and the prices in
settings. The hard stop is the spend limit on the Anthropic API key itself (see
DEPLOY.md); this keeps the portal well inside it and tells people why it paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import ChatUsage, User


@dataclass
class Status:
    used_today: int
    daily_limit: int  # 0 = no limit
    month_spend_usd: float
    monthly_budget_usd: float  # 0 = no limit
    resumes_on: date  # first day of next month, local time

    @property
    def remaining_today(self) -> int | None:
        if not self.daily_limit:
            return None
        return max(self.daily_limit - self.used_today, 0)

    @property
    def over_budget(self) -> bool:
        return bool(self.monthly_budget_usd) and self.month_spend_usd >= self.monthly_budget_usd

    @property
    def blocked_reason(self) -> str:
        if self.over_budget:
            return (
                "The assistant has used this month's budget and is paused until "
                f"{self.resumes_on:%B} {self.resumes_on.day}. You can still send your "
                "question to management, and they'll get back to you."
            )
        if self.remaining_today == 0:
            return (
                f"You've asked {self.daily_limit} questions today, which is the daily limit. "
                "The assistant is available again tomorrow. For anything that can't wait, "
                "send your question to management."
            )
        return ""


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().hoa_timezone)


def _utc_at(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=_tz()).astimezone(UTC)


def status(db: Session, user: User, now: datetime | None = None) -> Status:
    s = get_settings()
    today = (now or utcnow()).astimezone(_tz()).date()
    month_start = today.replace(day=1)
    next_month = month_start.replace(day=28).toordinal() + 4  # always lands in next month
    resumes_on = date.fromordinal(next_month).replace(day=1)
    used_today = db.scalar(
        select(func.count(ChatUsage.id)).where(
            ChatUsage.user_id == user.id, ChatUsage.created_at >= _utc_at(today)
        )
    )
    return Status(
        used_today=int(used_today or 0),
        daily_limit=max(s.chat_daily_limit_per_user, 0),
        month_spend_usd=month_spend_usd(db, today),
        monthly_budget_usd=max(s.chat_monthly_budget_usd, 0.0),
        resumes_on=resumes_on,
    )


def month_spend_usd(db: Session, today: date) -> float:
    """Estimated spend this month: everyone's questions, plus the weekly review."""
    spent = db.scalar(
        select(func.coalesce(func.sum(ChatUsage.cost_micros), 0)).where(
            ChatUsage.created_at >= _utc_at(today.replace(day=1))
        )
    )
    return int(spent or 0) / 1_000_000


def cost_micros(input_tokens: int, output_tokens: int) -> int:
    s = get_settings()
    return round(
        input_tokens * s.chat_price_input_per_mtok + output_tokens * s.chat_price_output_per_mtok
    )


def record(
    db: Session,
    user: User | None,
    *,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ChatUsage:
    row = ChatUsage(
        user_id=user.id if user else None,  # None: the weekly review
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micros=cost_micros(input_tokens, output_tokens),
    )
    db.add(row)
    return row
