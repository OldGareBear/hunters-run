"""Admins' view of the weekly review: what it sent, and a dry run of what it would send."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.models import User, WeeklyReview
from app.permissions import Perm
from app.security.auth import require_perm
from app.services import weekly_review
from app.web import render

router = APIRouter(prefix="/admin/weekly-review")
admin = require_perm(Perm.WEEKLY_REVIEW)


def _page(request: Request, db: Session, **ctx):
    reviews = list(
        db.scalars(select(WeeklyReview).order_by(WeeklyReview.week_start.desc()).limit(12))
    )
    return render(request, "admin/weekly_review.html", reviews=reviews, **ctx)


@router.get("")
def index(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    return _page(request, db)


@router.post("/preview")
def preview(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    """What this week's review would send now. Nothing is sent or recorded as sent; the
    assistant's use counts against the monthly budget like any other."""
    plan = weekly_review.plan(db)
    audit(db, "weekly_review.previewed", actor=user, request=request, mode=plan.mode)
    db.commit()
    return _page(request, db, preview=weekly_review.summary(plan), plan=plan)
