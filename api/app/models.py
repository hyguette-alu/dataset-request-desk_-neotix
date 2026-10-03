"""SQLAlchemy models.

Domain rules are enforced in the database wherever a constraint can express
them, so they hold even if application code has a bug or someone writes to the
database directly:

  * an episode belongs to at most one request  -> UNIQUE(assignments.episode_id)
  * only known robots                          -> FK episodes.robot_id -> robots.id
  * durations are positive and plausible       -> CHECK on episodes
  * a request asks for at least one episode    -> CHECK on requests

Two rules cannot be expressed as constraints and live in the service layer
instead (see app/services/):

  * only `good` or `usable` episodes may be assigned
  * a request cannot be delivered below its requested episode count

Both are multi-row or cross-table conditions; enforcing them in Postgres would
need triggers, which I judged to be more machinery than this system warrants.
They are covered by tests.
"""

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Role(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    client = "client"


class Quality(str, enum.Enum):
    good = "good"
    usable = "usable"
    bad = "bad"


class RequestStatus(str, enum.Enum):
    submitted = "submitted"
    in_progress = "in_progress"
    delivered = "delivered"
    accepted = "accepted"
    rejected = "rejected"


# Qualities an operator is allowed to attach to a request.
ASSIGNABLE_QUALITIES = (Quality.good, Quality.usable)


def _enum(python_enum: type[enum.Enum], name: str) -> Enum:
    """Postgres native enum storing the member *values*, not their names."""
    return Enum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        native_enum=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored lower-cased by the application so logins are case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organisation: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    requests: Mapped[list["Request"]] = relationship(back_populates="client")


class Robot(Base):
    """Reference table of known robots.

    Making this a table rather than a hard-coded list means the CSV importer
    rejects an unknown robot (such as `arm-99` in the seed file) through a
    foreign key, and operations staff can add a robot without a deploy.
    """

    __tablename__ = "robots"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Episode(TimestampMixin, Base):
    """A recorded clip.

    `episode_id` from the source system is the primary key. That is what makes
    re-importing the same file idempotent: the importer upserts on this column
    rather than inserting blindly.
    """

    __tablename__ = "episodes"

    episode_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    robot_id: Mapped[str] = mapped_column(
        ForeignKey("robots.id", ondelete="RESTRICT"), nullable=False
    )
    # Normalised on import: trimmed, collapsed whitespace, lower-cased, so
    # "  Pick Cup " and "PICK CUP" aggregate together in analytics.
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    operator_name: Mapped[str | None] = mapped_column(String(200))
    quality: Mapped[Quality] = mapped_column(_enum(Quality, "quality"), nullable=False)

    source_file: Mapped[str | None] = mapped_column(String(500))

    assignment: Mapped["Assignment | None"] = relationship(back_populates="episode")

    __table_args__ = (
        CheckConstraint(
            "duration_seconds > 0 AND duration_seconds <= 86400",
            name="ck_episodes_duration_plausible",
        ),
        Index("ix_episodes_recorded_at", "recorded_at"),
        Index("ix_episodes_robot_recorded_at", "robot_id", "recorded_at"),
        # Supports "top 5 task names by good episodes" without scanning the
        # bad/usable rows at all.
        Index(
            "ix_episodes_good_task_recorded",
            "task_name",
            "recorded_at",
            postgresql_where=text("quality = 'good'"),
        ),
        # Supports the operator episode browser, which filters on both.
        Index("ix_episodes_quality_task", "quality", "task_name"),
    )


class Request(TimestampMixin, Base):
    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    episodes_requested: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline: Mapped[date] = mapped_column(nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, "request_status"),
        nullable=False,
        default=RequestStatus.submitted,
    )

    client: Mapped["User"] = relationship(back_populates="requests")
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )
    status_events: Mapped[list["RequestStatusEvent"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="RequestStatusEvent.created_at",
    )

    __table_args__ = (
        CheckConstraint("episodes_requested > 0", name="ck_requests_episodes_positive"),
        Index("ix_requests_client_id", "client_id"),
        Index("ix_requests_status", "status"),
    )


class RequestStatusEvent(Base):
    """Append-only audit of every status change.

    `requests.status` is the current value; this table is the history. The
    median submitted -> delivered metric is computed from these rows, which is
    why it is a table and not a log line.
    """

    __tablename__ = "request_status_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    # NULL on the row recorded when the request is first created.
    from_status: Mapped[RequestStatus | None] = mapped_column(
        _enum(RequestStatus, "request_status")
    )
    to_status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, "request_status"), nullable=False
    )
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped["Request"] = relationship(back_populates="status_events")
    actor: Mapped["User"] = relationship()

    __table_args__ = (
        Index("ix_status_events_request_created", "request_id", "created_at"),
        Index("ix_status_events_to_status", "to_status"),
    )


class Assignment(Base):
    """An episode attached to a request.

    UNIQUE on episode_id is the rule "an episode can be assigned to at most one
    request at a time", enforced by the database rather than by a check in
    Python that two concurrent requests could both pass.

    Only the *current* assignment is modelled. Unassigning deletes the row; we
    do not keep a history of past assignments. See NOTES.md.
    """

    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    episode_id: Mapped[str] = mapped_column(
        ForeignKey("episodes.episode_id", ondelete="CASCADE"), nullable=False
    )
    assigned_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    request: Mapped["Request"] = relationship(back_populates="assignments")
    episode: Mapped["Episode"] = relationship(back_populates="assignment")

    __table_args__ = (
        UniqueConstraint("episode_id", name="uq_assignments_episode"),
        Index("ix_assignments_request_id", "request_id"),
    )


class ImportRun(Base):
    """One execution of the CSV importer, kept so the result is auditable
    after the HTTP response or terminal output is gone."""

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-reason counts plus a capped sample of rejected rows, so the report
    # explains *why* something was skipped without storing the whole file.
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    run_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
