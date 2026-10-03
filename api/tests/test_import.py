"""CSV import: cleaning rules and idempotency.

Most tests drive small inline CSVs so each defect is isolated and the expected
outcome is obvious. The last section runs the real `seed/episodes.csv` to
prove the rules hold together on the messy file the brief ships.
"""

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import Episode, Quality
from app.services.importer import Reason, import_episodes

HEADER = "episode_id,robot_id,task_name,recorded_at,duration_seconds,operator_name,quality"

# Mounted into the container at /seed; absent when the suite is run outside
# Docker, in which case the test that uses it is skipped rather than failing.
SEED_CSV = Path(settings.seed_episodes_path)


def run_import(db, *rows: str, name: str = "test.csv"):
    body = "\n".join([HEADER, *rows]) + "\n"
    return import_episodes(db, io.StringIO(body, newline=""), source_name=name)


def good_row(episode_id="EP-1", **overrides) -> str:
    fields = {
        "episode_id": episode_id,
        "robot_id": "arm-01",
        "task_name": "pick cup",
        "recorded_at": "2026-08-15T12:00:00",
        "duration_seconds": "42",
        "operator_name": "Aline",
        "quality": "good",
    }
    fields.update(overrides)
    return ",".join(str(fields[c]) for c in fields)


# --------------------------------------------------------------------------
# Idempotency: the property the brief calls out
# --------------------------------------------------------------------------


def test_importing_the_same_rows_twice_creates_no_duplicates(db):
    first = run_import(db, good_row("EP-1"), good_row("EP-2"))
    assert (first.created, first.updated, first.unchanged) == (2, 0, 0)

    second = run_import(db, good_row("EP-1"), good_row("EP-2"))

    assert (second.created, second.updated, second.unchanged) == (0, 0, 2)
    assert db.scalar(select(func.count()).select_from(Episode)) == 2


def test_a_changed_row_updates_in_place_rather_than_inserting(db):
    run_import(db, good_row("EP-1", quality="good"))

    report = run_import(db, good_row("EP-1", quality="usable"))

    assert (report.created, report.updated, report.unchanged) == (0, 1, 0)
    assert db.scalar(select(func.count()).select_from(Episode)) == 1
    assert db.get(Episode, "EP-1").quality is Quality.usable


@pytest.mark.skipif(not SEED_CSV.exists(), reason="seed/episodes.csv is not mounted")
def test_the_real_seed_file_is_idempotent(db):
    """The whole messy export, twice. Nothing may be created on the second
    run, and the row count must not move."""
    with SEED_CSV.open(encoding="utf-8-sig", newline="") as handle:
        first = import_episodes(db, handle, source_name="episodes.csv")
    count_after_first = db.scalar(select(func.count()).select_from(Episode))

    with SEED_CSV.open(encoding="utf-8-sig", newline="") as handle:
        second = import_episodes(db, handle, source_name="episodes.csv")

    assert first.created > 0
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == first.created
    assert db.scalar(select(func.count()).select_from(Episode)) == count_after_first


# --------------------------------------------------------------------------
# Normalisation: rows that are kept, after cleaning
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-15T12:00:00", datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
        ("2026-08-15 12:00:00", datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
        ("2026-08-15T12:00:00Z", datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
        ("15/08/2026 12:00", datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)),
    ],
)
def test_mixed_date_formats_are_parsed_to_utc(db, raw, expected):
    run_import(db, good_row("EP-1", recorded_at=raw))
    assert db.get(Episode, "EP-1").recorded_at == expected


@pytest.mark.parametrize("raw", ["  Pick Cup ", "PICK CUP", "pick  cup"])
def test_task_names_are_normalised_so_analytics_group_correctly(db, raw):
    run_import(db, f'EP-1,arm-01,"{raw}",2026-08-15T12:00:00,42,Aline,good')
    assert db.get(Episode, "EP-1").task_name == "pick cup"


@pytest.mark.parametrize("raw", ["Good", "GOOD", " good "])
def test_quality_casing_is_normalised(db, raw):
    run_import(db, good_row("EP-1", quality=raw))
    assert db.get(Episode, "EP-1").quality is Quality.good


def test_surrounding_whitespace_in_robot_id_is_trimmed(db):
    run_import(db, good_row("EP-1", robot_id=" arm-01"))
    assert db.get(Episode, "EP-1").robot_id == "arm-01"


def test_episode_ids_are_upper_cased_so_case_variants_are_one_episode(db):
    """`ep-00003` and `EP-00003` in the export are the same recording."""
    report = run_import(db, good_row("EP-3"), good_row("ep-3", quality="usable"))

    assert db.scalar(select(func.count()).select_from(Episode)) == 1
    assert report.reasons[Reason.CONFLICTING_DUPLICATE_IN_FILE] == 1


def test_fractional_duration_is_rounded(db):
    run_import(db, good_row("EP-1", duration_seconds="45.5"))
    assert db.get(Episode, "EP-1").duration_seconds == 46


def test_a_missing_operator_name_is_kept_as_null(db):
    """A gap in the record is not a reason to discard an otherwise good
    episode."""
    run_import(db, good_row("EP-1", operator_name=""))

    assert db.get(Episode, "EP-1").operator_name is None


def test_quoted_comma_inside_a_task_name_is_handled(db):
    run_import(db, 'EP-1,arm-01,"pick cup, then place",2026-08-15T12:00:00,40,Eric,good')
    assert db.get(Episode, "EP-1").task_name == "pick cup, then place"


# --------------------------------------------------------------------------
# Rejection: rows that are skipped, with a reason
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,reason",
    [
        (good_row(""), Reason.MISSING_EPISODE_ID),
        (good_row("EP-1", robot_id=""), Reason.MISSING_ROBOT_ID),
        (good_row("EP-1", robot_id="arm-99"), Reason.UNKNOWN_ROBOT),
        (good_row("EP-1", task_name=""), Reason.MISSING_TASK_NAME),
        (good_row("EP-1", recorded_at=""), Reason.MISSING_RECORDED_AT),
        (good_row("EP-1", recorded_at="not a date"), Reason.INVALID_RECORDED_AT),
        (good_row("EP-1", recorded_at="2031-01-01T00:00:00"), Reason.FUTURE_RECORDED_AT),
        (good_row("EP-1", duration_seconds=""), Reason.MISSING_DURATION),
        (good_row("EP-1", duration_seconds="N/A"), Reason.INVALID_DURATION),
        (good_row("EP-1", duration_seconds="-5"), Reason.INVALID_DURATION),
        (good_row("EP-1", duration_seconds="0"), Reason.INVALID_DURATION),
        (good_row("EP-1", duration_seconds="999999"), Reason.IMPLAUSIBLE_DURATION),
        (good_row("EP-1", quality=""), Reason.MISSING_QUALITY),
        (good_row("EP-1", quality="excellent"), Reason.INVALID_QUALITY),
        ("EP-1,arm-02,open drawer,2026-08-20T10:00:00,30", Reason.MALFORMED_ROW),
    ],
)
def test_bad_rows_are_skipped_with_the_right_reason(db, row, reason):
    report = run_import(db, row)

    assert report.created == 0
    assert report.skipped == 1
    assert report.reasons[reason] == 1
    assert db.scalar(select(func.count()).select_from(Episode)) == 0


def test_a_bad_row_does_not_stop_the_good_ones(db):
    report = run_import(db, good_row("EP-1"), good_row("EP-2", quality="excellent"), good_row("EP-3"))

    assert report.created == 2
    assert report.skipped == 1
    assert db.scalar(select(func.count()).select_from(Episode)) == 2


def test_skipped_rows_are_reported_with_their_line_number(db):
    report = run_import(db, good_row("EP-1"), good_row("EP-2", robot_id="arm-99"))

    problem = next(p for p in report.problems if p.reason == Reason.UNKNOWN_ROBOT)
    assert problem.line == 3  # header is line 1
    assert "arm-99" in problem.detail


def test_blank_trailing_lines_are_counted_but_not_reported_as_errors(db):
    body = HEADER + "\n" + good_row("EP-1") + "\n   \n\n"
    report = import_episodes(db, io.StringIO(body, newline=""), source_name="t.csv")

    assert report.created == 1
    assert report.skipped == 0  # whitespace is noise, not an operator's problem
    assert report.reasons[Reason.BLANK_ROW] >= 1


def test_a_file_without_the_required_header_is_refused_outright(db):
    body = "id,robot\nEP-1,arm-01\n"
    with pytest.raises(ValueError, match="missing required columns"):
        import_episodes(db, io.StringIO(body, newline=""), source_name="t.csv")


# --------------------------------------------------------------------------
# Duplicates within one file
# --------------------------------------------------------------------------


def test_an_identical_repeated_row_is_counted_once(db):
    report = run_import(db, good_row("EP-1"), good_row("EP-1"))

    assert report.created == 1
    assert report.reasons[Reason.DUPLICATE_IN_FILE] == 1
    assert db.scalar(select(func.count()).select_from(Episode)) == 1


def test_a_contradicting_repeated_row_keeps_the_later_one_and_says_so(db):
    """EP-00011 appears twice in the seed export with different qualities.
    Later wins, because later lines in an export are the more recent truth,
    but it is reported because a flipped quality changes whether the episode
    may be delivered to a client."""
    report = run_import(
        db, good_row("EP-1", quality="bad"), good_row("EP-1", quality="good")
    )

    assert report.created == 1
    assert report.reasons[Reason.CONFLICTING_DUPLICATE_IN_FILE] == 1
    assert db.get(Episode, "EP-1").quality is Quality.good


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------


def test_the_report_accounts_for_every_row_read(db):
    report = run_import(
        db,
        good_row("EP-1"),
        good_row("EP-2", quality="excellent"),
        good_row("EP-1"),
    )

    accounted = (
        report.created
        + report.updated
        + report.unchanged
        + report.skipped
        + report.reasons[Reason.BLANK_ROW]
    )
    assert accounted == report.rows_read


def test_an_import_run_is_recorded_for_audit(db):
    from app.models import ImportRun

    run_import(db, good_row("EP-1"), name="monday.csv")

    run = db.scalars(select(ImportRun).order_by(ImportRun.id.desc())).first()
    assert run.source_file == "monday.csv"
    assert run.created == 1
    assert run.finished_at is not None
    assert "reasons" in run.report
