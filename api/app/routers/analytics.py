"""Analytics endpoint. Operator and admin only."""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_operator
from app.errors import DomainError
from app.models import User
from app.schemas import AnalyticsOut
from app.services.analytics import build_report

logger = logging.getLogger("app.analytics")

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# An open-ended range is a full table scan waiting to happen, so the window is
# bounded and defaults to something small.
MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 30


@router.get("", response_model=AnalyticsOut)
def analytics(
    db: Session = Depends(get_db),
    _: User = Depends(require_operator),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> AnalyticsOut:
    """Aggregates for a date range. Both bounds are inclusive calendar days."""
    today = date.today()
    date_to = date_to or today
    date_from = date_from or date_to - timedelta(days=DEFAULT_RANGE_DAYS)

    if date_from > date_to:
        raise DomainError("`from` must not be after `to`")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise DomainError(f"date range must not exceed {MAX_RANGE_DAYS} days")

    return AnalyticsOut(**build_report(db, date_from, date_to))
