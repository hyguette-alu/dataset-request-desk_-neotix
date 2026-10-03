"""Request/response schemas.

Pydantic is doing the input validation for the whole API: types, lengths and
ranges are declared here, so a malformed body is rejected with a 422 before it
reaches any handler or the database.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import Quality, RequestStatus, Role
from app.security import MAX_PASSWORD_BYTES


class LoginRequest(BaseModel):
    email: EmailStr
    # Upper bound matches what bcrypt can actually hash; see app/security.py.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    organisation: str | None
    role: Role
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------
# User administration (admin only)
# --------------------------------------------------------------------------


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)
    role: Role
    # Minimum length is a floor, not a policy. A real deployment needs a
    # proper password policy; see NOTES.md.
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)
    organisation: str | None = Field(default=None, max_length=200)


class UserUpdate(BaseModel):
    """Every field optional: this is a PATCH.

    Email and password are deliberately not changeable here. Rotating a
    password is the account holder's business, not an admin's, and changing
    an email silently changes who can log in to an account that already owns
    requests.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None
    organisation: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class RequestCreate(BaseModel):
    task_name: str = Field(min_length=1, max_length=200)
    episodes_requested: int = Field(ge=1, le=100_000)
    deadline: date
    notes: str | None = Field(default=None, max_length=5_000)


class StatusChange(BaseModel):
    to_status: RequestStatus
    # Mainly for a client explaining a rejection, but allowed on any change.
    note: str | None = Field(default=None, max_length=2_000)


class RequesterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    organisation: str | None


class StatusEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: RequestStatus | None
    to_status: RequestStatus
    actor_id: int
    actor_name: str
    note: str | None
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_name: str
    episodes_requested: int
    deadline: date
    notes: str | None
    status: RequestStatus
    created_at: datetime
    updated_at: datetime

    client: RequesterOut
    assigned_count: int
    # What the *calling* user is allowed to do next. The UI uses this to decide
    # which buttons to show; the API checks again on every call regardless.
    allowed_transitions: list[RequestStatus]


class RequestDetail(RequestOut):
    episodes: list["EpisodeOut"]
    history: list[StatusEventOut]


class AssignEpisodes(BaseModel):
    episode_ids: list[str] = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------------
# Episodes
# --------------------------------------------------------------------------


class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    episode_id: str
    robot_id: str
    task_name: str
    recorded_at: datetime
    duration_seconds: int
    operator_name: str | None
    quality: Quality


class EpisodePage(BaseModel):
    items: list[EpisodeOut]
    total: int
    limit: int
    offset: int


class RequestPage(BaseModel):
    items: list[RequestOut]
    total: int
    limit: int
    offset: int


class ImportResult(BaseModel):
    source_file: str
    rows_read: int
    created: int
    updated: int
    unchanged: int
    skipped: int
    reasons: dict[str, int]
    sample_problems: list[dict]
    sample_truncated: bool


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------


class EpisodesPerDay(BaseModel):
    day: date
    robot_id: str
    episodes: int


class StatusCount(BaseModel):
    status: RequestStatus
    count: int


class TopTask(BaseModel):
    task_name: str
    good_episodes: int


class AnalyticsOut(BaseModel):
    date_from: date
    date_to: date
    episodes_per_day_per_robot: list[EpisodesPerDay]
    requests_by_status: list[StatusCount]
    # None when no request reached `delivered` in the window.
    median_submitted_to_delivered_hours: float | None
    top_tasks_by_good_episodes: list[TopTask]


RequestDetail.model_rebuild()
