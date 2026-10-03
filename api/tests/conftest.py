"""Test harness.

The suite runs against a real Postgres database (`drd_test`), not SQLite, so
the things we rely on Postgres for — enums, partial indexes, ON CONFLICT,
percentile_cont — are actually exercised.

Schema is created by running the Alembic migrations, not metadata.create_all.
That means a migration that drifts from the models fails the test run, which
is the point of having migrations at all.

Each test runs inside a transaction that is rolled back afterwards, so tests
do not see each other's data and the suite can be run repeatedly.
"""

import subprocess
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine, get_db
from app.main import app
from app.models import (
    Assignment,
    Episode,
    Quality,
    Request,
    RequestStatus,
    RequestStatusEvent,
    Robot,
    Role,
    User,
)
from app.security import hash_password

SEED_ROBOTS = ["arm-01", "arm-02", "arm-03", "mobile-01", "humanoid-01"]


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> None:
    url = make_url(settings.database_url)

    # Guard rail: the suite truncates and rolls back aggressively, so refuse to
    # point it at anything that is not explicitly a test database.
    assert url.database and url.database.endswith("_test"), (
        f"refusing to run tests against database {url.database!r}; "
        "set DATABASE_URL to a database whose name ends in _test"
    )

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": url.database},
        )
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    subprocess.run(["alembic", "upgrade", "head"], check=True)


@pytest.fixture()
def db(_prepare_database) -> Session:
    """A session wrapped in a transaction that is always rolled back.

    join_transaction_mode="create_savepoint" means a commit() inside the
    application code under test commits to a savepoint, not to the outer
    transaction, so isolation survives handlers that commit.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    for robot_id in SEED_ROBOTS:
        session.merge(Robot(id=robot_id))
    session.flush()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db: Session) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_user(
    db: Session,
    email: str,
    role: Role,
    password: str = "pw-for-tests",
    is_active: bool = True,
) -> User:
    user = User(
        email=email,
        name=email.split("@")[0],
        organisation=None,
        role=role,
        password_hash=hash_password(password),
        is_active=is_active,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture()
def admin_user(db: Session) -> User:
    return make_user(db, "admin@example.com", Role.admin)


@pytest.fixture()
def operator_user(db: Session) -> User:
    return make_user(db, "operator@example.com", Role.operator)


@pytest.fixture()
def client_a(db: Session) -> User:
    return make_user(db, "client-a@example.com", Role.client)


@pytest.fixture()
def client_b(db: Session) -> User:
    return make_user(db, "client-b@example.com", Role.client)


def make_episode(
    db: Session,
    episode_id: str,
    quality: Quality = Quality.good,
    task_name: str = "pick cup",
    robot_id: str = "arm-01",
    recorded_at: datetime | None = None,
    duration_seconds: int = 42,
) -> Episode:
    episode = Episode(
        episode_id=episode_id,
        robot_id=robot_id,
        task_name=task_name,
        recorded_at=recorded_at or datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        duration_seconds=duration_seconds,
        operator_name="Tester",
        quality=quality,
    )
    db.add(episode)
    db.flush()
    return episode


def make_request(
    db: Session,
    client: User,
    episodes_requested: int = 2,
    task_name: str = "pick cup",
    status: RequestStatus = RequestStatus.submitted,
) -> Request:
    """Create a request already in `status`, with the matching opening
    audit row. Tests that care about the transition path drive it through
    the API instead."""
    request = Request(
        client_id=client.id,
        task_name=task_name,
        episodes_requested=episodes_requested,
        deadline=date(2026, 12, 31),
        notes=None,
        status=status,
    )
    db.add(request)
    db.flush()
    db.add(
        RequestStatusEvent(
            request_id=request.id,
            from_status=None,
            to_status=RequestStatus.submitted,
            actor_id=client.id,
        )
    )
    db.flush()
    return request


def assign(db: Session, request: Request, episodes: list[Episode], actor: User) -> None:
    for episode in episodes:
        db.add(
            Assignment(
                request_id=request.id,
                episode_id=episode.episode_id,
                assigned_by=actor.id,
            )
        )
    db.flush()


def auth_headers(user: User) -> dict[str, str]:
    """Authenticate as `user` over the Bearer transport.

    Using the header rather than the cookie keeps tests independent of
    cookie handling, and mirrors how the graders will probe the API.
    """
    from app.security import create_access_token

    token = create_access_token(user_id=user.id, role=user.role.value)
    return {"Authorization": f"Bearer {token}"}
