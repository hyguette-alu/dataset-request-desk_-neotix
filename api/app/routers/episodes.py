"""Episode browsing and CSV import.

Both are operator-facing. Clients never see the episode pool; they only see
the episodes attached to their own request, through the request detail
endpoint.
"""

import io
import logging

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_operator
from app.errors import DomainError
from app.models import Assignment, Episode, Quality, User
from app.schemas import EpisodeOut, EpisodePage, ImportResult
from app.services.importer import import_episodes

logger = logging.getLogger("app.episodes")

router = APIRouter(prefix="/api/episodes", tags=["episodes"])

# Uploads are read into the database row by row, but an unbounded upload is
# still a way to tie up a worker, so the body is capped.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


@router.get("", response_model=EpisodePage)
def list_episodes(
    db: Session = Depends(get_db),
    _: User = Depends(require_operator),
    task_name: str | None = Query(default=None, max_length=200),
    quality: Quality | None = Query(default=None),
    robot_id: str | None = Query(default=None, max_length=50),
    unassigned_only: bool = Query(
        default=False,
        description="Only episodes not already attached to a request.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> EpisodePage:
    statement = select(Episode)

    if task_name:
        # Case-insensitive contains. task_name is stored normalised, so this
        # matches what the operator sees in the UI.
        statement = statement.where(Episode.task_name.ilike(f"%{task_name.strip()}%"))
    if quality is not None:
        statement = statement.where(Episode.quality == quality)
    if robot_id:
        statement = statement.where(Episode.robot_id == robot_id.strip().lower())
    if unassigned_only:
        statement = statement.where(
            ~select(Assignment.id)
            .where(Assignment.episode_id == Episode.episode_id)
            .exists()
        )

    total = db.scalar(select(func.count()).select_from(statement.subquery()))

    episodes = db.scalars(
        statement.order_by(Episode.recorded_at.desc(), Episode.episode_id)
        .limit(limit)
        .offset(offset)
    ).all()

    return EpisodePage(
        items=[EpisodeOut.model_validate(e) for e in episodes],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("/import", response_model=ImportResult)
async def import_csv(
    file: UploadFile = File(..., description="CSV export from the recording system"),
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> ImportResult:
    """Import episodes from an uploaded CSV. Safe to run on the same file twice.

    The same work is available offline as `python -m app.cli import-episodes
    <path>`, which is what the container uses for the seed file.
    """
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise DomainError(
            f"file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit"
        )

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError(f"file is not valid UTF-8: {exc}") from exc

    try:
        report = import_episodes(
            db,
            io.StringIO(text, newline=""),
            source_name=file.filename or "upload.csv",
            run_by=user.id,
        )
    except ValueError as exc:
        # Raised for a missing or unusable header; the file itself is wrong.
        raise DomainError(str(exc)) from exc

    db.commit()
    return ImportResult(**report.as_dict())
