"""CSV episode import.

The export from the recording system is messy. Every defect in it is handled
explicitly below and counted in the returned report; nothing is dropped
silently. The rules, and why each one was chosen, are in NOTES.md.

Two properties the brief asks for specifically:

*Idempotent.* The import upserts on `episode_id`, so running it twice over the
same file produces the same database. The second run reports every row as
`unchanged` rather than creating duplicates.

*Explains itself.* The report carries per-reason counts plus a capped sample of
rejected rows with their line numbers, so an operator can find the bad rows in
the source file.

Rows are processed in batches so memory does not grow with file size. The one
structure that does grow is a map of episode ids already seen, which is what
lets the report distinguish a harmless repeated row from one that contradicts
an earlier row. See NOTES.md for what would change for a multi-gigabyte file.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Episode, ImportRun, Quality, Robot

logger = logging.getLogger("app.importer")

EXPECTED_COLUMNS = [
    "episode_id",
    "robot_id",
    "task_name",
    "recorded_at",
    "duration_seconds",
    "operator_name",
    "quality",
]

# Matches the CHECK constraint on episodes.duration_seconds. Real clips in the
# seed data run 8-120s; one day is a generous ceiling that still rejects the
# obvious sensor glitch (999999) without guessing at a tighter business rule.
MAX_DURATION_SECONDS = 86_400

# Clock skew allowance for "recorded in the future".
FUTURE_TOLERANCE = timedelta(hours=24)

# How many rejected rows to keep as examples in the stored report.
MAX_SAMPLE_ERRORS = 50

BATCH_SIZE = 1_000

_WHITESPACE = re.compile(r"\s+")

# Accepted date formats, tried in order after ISO-8601. The seed file contains
# ISO with and without the T separator, a trailing Z, and one dd/mm/yyyy value.
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d",
)


class Reason:
    """Why a row was skipped. Values are the keys in the report."""

    BLANK_ROW = "blank_row"
    MALFORMED_ROW = "malformed_row"
    MISSING_EPISODE_ID = "missing_episode_id"
    MISSING_ROBOT_ID = "missing_robot_id"
    UNKNOWN_ROBOT = "unknown_robot"
    MISSING_TASK_NAME = "missing_task_name"
    MISSING_RECORDED_AT = "missing_recorded_at"
    INVALID_RECORDED_AT = "invalid_recorded_at"
    FUTURE_RECORDED_AT = "future_recorded_at"
    MISSING_DURATION = "missing_duration"
    INVALID_DURATION = "invalid_duration"
    IMPLAUSIBLE_DURATION = "implausible_duration"
    MISSING_QUALITY = "missing_quality"
    INVALID_QUALITY = "invalid_quality"
    DUPLICATE_IN_FILE = "duplicate_in_file"
    CONFLICTING_DUPLICATE_IN_FILE = "conflicting_duplicate_in_file"


@dataclass(frozen=True)
class RowProblem:
    line: int
    episode_id: str | None
    reason: str
    detail: str


@dataclass
class ImportReport:
    source_file: str
    rows_read: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    reasons: Counter = field(default_factory=Counter)
    problems: list[RowProblem] = field(default_factory=list)

    def record_problem(self, problem: RowProblem) -> None:
        self.reasons[problem.reason] += 1
        # Blank trailing lines are noise, not something an operator should be
        # asked to fix, so they are counted but never surfaced as examples.
        if problem.reason != Reason.BLANK_ROW:
            self.skipped += 1
            if len(self.problems) < MAX_SAMPLE_ERRORS:
                self.problems.append(problem)

    def as_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "rows_read": self.rows_read,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "reasons": dict(sorted(self.reasons.items())),
            "sample_problems": [
                {
                    "line": p.line,
                    "episode_id": p.episode_id,
                    "reason": p.reason,
                    "detail": p.detail,
                }
                for p in self.problems
            ],
            "sample_truncated": self.reasons.total() - self.reasons[Reason.BLANK_ROW]
            > len(self.problems),
        }


# --------------------------------------------------------------------------
# Field normalisation
# --------------------------------------------------------------------------


def normalise_episode_id(raw: str | None) -> str | None:
    """Upper-cased and trimmed.

    The seed file contains `ep-00003` alongside `EP-00003`. Treating ids
    case-insensitively means those are the same episode, which is almost
    certainly what the recording system meant, rather than two records that
    differ only in case.
    """
    if raw is None:
        return None
    value = raw.strip().upper()
    return value or None


def normalise_robot_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    return value or None


def normalise_task_name(raw: str | None) -> str | None:
    """Trimmed, inner whitespace collapsed, lower-cased.

    Without this, "  Pick Cup ", "PICK CUP" and "pick cup" are three different
    tasks and the "top 5 task names" analytic is wrong.
    """
    if raw is None:
        return None
    value = _WHITESPACE.sub(" ", raw).strip().lower()
    return value or None


def parse_recorded_at(raw: str | None) -> datetime:
    """Parse the mixed date formats in the export into an aware UTC datetime.

    Values without a timezone are assumed to be UTC. That is an assumption,
    not a fact: the export does not say what zone it wrote. It is recorded in
    NOTES.md because getting it wrong shifts every "episodes per day" bucket.
    """
    if raw is None or not raw.strip():
        raise ValueError("empty")

    value = raw.strip()

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"unrecognised date format: {value!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_duration(raw: str | None) -> int:
    """Return a positive whole number of seconds.

    The export contains blanks, the literal "N/A", a float (45.5), a negative
    (-5) and an implausible outlier (999999). Floats are rounded; the rest are
    rejected by the caller on the exception type/message.
    """
    if raw is None or not raw.strip():
        raise ValueError(Reason.MISSING_DURATION)

    value = raw.strip()
    try:
        number = float(value)
    except ValueError:
        raise ValueError(Reason.INVALID_DURATION) from None

    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(Reason.INVALID_DURATION)

    seconds = round(number)
    if seconds <= 0:
        raise ValueError(Reason.INVALID_DURATION)
    if seconds > MAX_DURATION_SECONDS:
        raise ValueError(Reason.IMPLAUSIBLE_DURATION)
    return seconds


def normalise_quality(raw: str | None) -> Quality:
    if raw is None or not raw.strip():
        raise ValueError(Reason.MISSING_QUALITY)
    try:
        return Quality(raw.strip().lower())
    except ValueError:
        raise ValueError(Reason.INVALID_QUALITY) from None


def normalise_operator_name(raw: str | None) -> str | None:
    """Optional. A missing operator name is a gap in the record, not a reason
    to throw away an otherwise good episode."""
    if raw is None:
        return None
    return _WHITESPACE.sub(" ", raw).strip() or None


# --------------------------------------------------------------------------
# Row handling
# --------------------------------------------------------------------------

# The fields compared to decide whether an existing episode actually changed.
_COMPARED_FIELDS = (
    "robot_id",
    "task_name",
    "recorded_at",
    "duration_seconds",
    "operator_name",
    "quality",
)


def _is_blank(raw: dict) -> bool:
    """True for the whitespace-only trailing lines at the end of the export."""
    for value in raw.values():
        if value is None:
            continue
        if not isinstance(value, str):  # the restkey surplus is a list
            return False
        if value.strip():
            return False
    return True


def _normalise_row(
    line: int, raw: dict, known_robots: set[str], now: datetime
) -> tuple[dict | None, RowProblem | None]:
    """Turn one CSV row into a clean record, or explain why it cannot be."""

    def problem(reason: str, detail: str) -> tuple[None, RowProblem]:
        return None, RowProblem(
            line=line,
            episode_id=normalise_episode_id(raw.get("episode_id")),
            reason=reason,
            detail=detail,
        )

    # csv.DictReader signals a short row with None values and a long row by
    # collecting the surplus under the restkey.
    if raw.get("__extra__"):
        return problem(Reason.MALFORMED_ROW, "more fields than the header declares")
    if any(raw.get(column) is None for column in EXPECTED_COLUMNS):
        missing = [c for c in EXPECTED_COLUMNS if raw.get(c) is None]
        return problem(
            Reason.MALFORMED_ROW, f"missing columns: {', '.join(missing)}"
        )

    episode_id = normalise_episode_id(raw["episode_id"])
    if episode_id is None:
        # No natural key means we cannot deduplicate or update it later.
        return problem(Reason.MISSING_EPISODE_ID, "episode_id is blank")

    robot_id = normalise_robot_id(raw["robot_id"])
    if robot_id is None:
        return problem(Reason.MISSING_ROBOT_ID, "robot_id is blank")
    if robot_id not in known_robots:
        # Rejected rather than auto-created: an unrecognised robot usually
        # means a typo or a device nobody has registered, and silently
        # inventing it would hide that.
        return problem(Reason.UNKNOWN_ROBOT, f"{robot_id!r} is not a known robot")

    task_name = normalise_task_name(raw["task_name"])
    if task_name is None:
        return problem(Reason.MISSING_TASK_NAME, "task_name is blank")

    if not (raw["recorded_at"] or "").strip():
        return problem(Reason.MISSING_RECORDED_AT, "recorded_at is blank")
    try:
        recorded_at = parse_recorded_at(raw["recorded_at"])
    except ValueError as exc:
        return problem(Reason.INVALID_RECORDED_AT, str(exc))
    if recorded_at > now + FUTURE_TOLERANCE:
        return problem(
            Reason.FUTURE_RECORDED_AT,
            f"recorded_at {recorded_at.isoformat()} is in the future",
        )

    try:
        duration_seconds = parse_duration(raw["duration_seconds"])
    except ValueError as exc:
        return problem(str(exc), f"duration_seconds={raw['duration_seconds']!r}")

    try:
        quality = normalise_quality(raw["quality"])
    except ValueError as exc:
        return problem(str(exc), f"quality={raw['quality']!r}")

    record = {
        "episode_id": episode_id,
        "robot_id": robot_id,
        "task_name": task_name,
        "recorded_at": recorded_at,
        "duration_seconds": duration_seconds,
        "operator_name": normalise_operator_name(raw["operator_name"]),
        "quality": quality,
    }
    return record, None


def _comparable(record: dict) -> tuple:
    return tuple(record[field_name] for field_name in _COMPARED_FIELDS)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _flush_batch(
    db: Session, batch: list[dict], source_name: str, report: ImportReport
) -> None:
    """Upsert one batch and classify each row as created / updated / unchanged.

    Existing rows are fetched once per batch and compared in Python. The
    alternative is Postgres' `xmax = 0` trick inside RETURNING, which is
    shorter but relies on an internal system column; this version is plainer
    to read and gives us the `unchanged` count for free.
    """
    if not batch:
        return

    ids = [record["episode_id"] for record in batch]
    existing = {
        row.episode_id: row
        for row in db.scalars(select(Episode).where(Episode.episode_id.in_(ids)))
    }

    to_write: list[dict] = []
    for record in batch:
        current = existing.get(record["episode_id"])
        if current is None:
            report.created += 1
        elif _comparable(record) == tuple(
            getattr(current, name) for name in _COMPARED_FIELDS
        ):
            report.unchanged += 1
            # Nothing to write: this is the second-run case that makes the
            # import idempotent.
            continue
        else:
            report.updated += 1
        to_write.append({**record, "source_file": source_name})

    if not to_write:
        return

    statement = pg_insert(Episode).values(to_write)
    statement = statement.on_conflict_do_update(
        index_elements=[Episode.episode_id],
        set_={
            name: getattr(statement.excluded, name)
            for name in (*_COMPARED_FIELDS, "source_file")
        }
        | {"updated_at": datetime.now(tz=timezone.utc)},
    )
    db.execute(statement)


def import_episodes(
    db: Session,
    handle: IO[str],
    source_name: str,
    run_by: int | None = None,
) -> ImportReport:
    """Import episodes from an open CSV stream. Safe to run repeatedly."""
    report = ImportReport(source_file=source_name)
    run = ImportRun(source_file=source_name, run_by=run_by, report={})
    db.add(run)
    db.flush()

    known_robots = set(db.scalars(select(Robot.id).where(Robot.is_active.is_(True))))
    now = datetime.now(tz=timezone.utc)

    reader = csv.DictReader(handle, restkey="__extra__")
    if reader.fieldnames is None:
        raise ValueError("file is empty: no header row")

    header = [(name or "").strip().lower() for name in reader.fieldnames]
    missing_columns = [c for c in EXPECTED_COLUMNS if c not in header]
    if missing_columns:
        raise ValueError(
            f"header is missing required columns: {', '.join(missing_columns)}"
        )
    reader.fieldnames = header

    # episode_id -> the values last accepted for it in this file. Used to tell
    # a harmless repeated row from one that contradicts an earlier row.
    seen: dict[str, tuple] = {}
    batch: list[dict] = []

    for line, raw in enumerate(reader, start=2):  # line 1 is the header
        report.rows_read += 1

        if _is_blank(raw):
            report.record_problem(
                RowProblem(line, None, Reason.BLANK_ROW, "empty line")
            )
            continue

        record, problem = _normalise_row(line, raw, known_robots, now)
        if problem is not None:
            report.record_problem(problem)
            continue

        assert record is not None
        episode_id = record["episode_id"]
        values = _comparable(record)

        if episode_id in seen:
            if seen[episode_id] == values:
                report.record_problem(
                    RowProblem(
                        line,
                        episode_id,
                        Reason.DUPLICATE_IN_FILE,
                        "identical to an earlier row in this file",
                    )
                )
            else:
                # Last row wins: later lines in an export are assumed to be the
                # more recent truth. Reported loudly because a flipped quality
                # changes whether the episode may be delivered to a client.
                report.record_problem(
                    RowProblem(
                        line,
                        episode_id,
                        Reason.CONFLICTING_DUPLICATE_IN_FILE,
                        "contradicts an earlier row in this file; later row kept",
                    )
                )
                seen[episode_id] = values
                batch = [r for r in batch if r["episode_id"] != episode_id]
                batch.append(record)
            continue

        seen[episode_id] = values
        batch.append(record)

        if len(batch) >= BATCH_SIZE:
            _flush_batch(db, batch, source_name, report)
            batch = []

    _flush_batch(db, batch, source_name, report)

    run.finished_at = datetime.now(tz=timezone.utc)
    run.rows_read = report.rows_read
    run.created = report.created
    run.updated = report.updated
    run.unchanged = report.unchanged
    run.skipped = report.skipped
    run.report = report.as_dict()
    db.flush()

    # Field names are suffixed because `created` collides with an attribute
    # the logging module sets on every record.
    logger.info(
        "import_complete",
        extra={
            "source_file": source_name,
            "rows_read": report.rows_read,
            "created_count": report.created,
            "updated_count": report.updated,
            "unchanged_count": report.unchanged,
            "skipped_count": report.skipped,
        },
    )
    return report


def import_episodes_from_path(
    db: Session, path: str | Path, run_by: int | None = None
) -> ImportReport:
    path = Path(path)
    # newline="" is required by the csv module so quoted fields containing
    # newlines are read correctly.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return import_episodes(db, handle, source_name=path.name, run_by=run_by)
