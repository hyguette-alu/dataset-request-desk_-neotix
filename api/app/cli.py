"""Operational commands.

    python -m app.cli seed
    python -m app.cli import-episodes /seed/episodes.csv

Both are safe to run repeatedly; the container entrypoint runs `seed` on every
start.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.logging_setup import configure_logging
from app.models import Robot, Role, User
from app.security import hash_password
from app.services.importer import import_episodes_from_path

logger = logging.getLogger("app.cli")

# From seed/README.md. Episodes referencing anything else are rejected by the
# importer via the foreign key on episodes.robot_id.
KNOWN_ROBOTS = ["arm-01", "arm-02", "arm-03", "mobile-01", "humanoid-01"]


def seed_robots(db: Session) -> int:
    existing = set(db.scalars(select(Robot.id)))
    created = 0
    for robot_id in KNOWN_ROBOTS:
        if robot_id not in existing:
            db.add(Robot(id=robot_id))
            created += 1
    db.flush()
    return created


def seed_users(db: Session, users_path: Path) -> tuple[int, int]:
    """Create the accounts in seed/users.json.

    Idempotent on email. Existing users are left alone rather than having
    their password reset, so a seed on restart cannot silently undo a
    password change.
    """
    if not users_path.exists():
        logger.warning("seed_users_file_missing", extra={"path": str(users_path)})
        return (0, 0)

    payload = json.loads(users_path.read_text())
    created = skipped = 0

    for entry in payload:
        email = entry["email"].strip().lower()
        if db.scalar(select(User).where(User.email == email)) is not None:
            skipped += 1
            continue
        db.add(
            User(
                email=email,
                name=entry["name"],
                organisation=entry.get("organisation"),
                role=Role(entry["role"]),
                password_hash=hash_password(entry["password"]),
                is_active=True,
            )
        )
        created += 1

    db.flush()
    return (created, skipped)


def cmd_seed(_args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        robots = seed_robots(db)
        created, skipped = seed_users(db, Path(settings.seed_users_path))
        db.commit()

    logger.info(
        "seed_complete",
        extra={
            "robots_created": robots,
            "users_created": created,
            "users_already_present": skipped,
        },
    )
    return 0


def cmd_import_episodes(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        logger.error("import_file_missing", extra={"path": str(path)})
        return 2

    with SessionLocal() as db:
        report = import_episodes_from_path(db, path)
        db.commit()

    summary = report.as_dict()
    # Printed as well as logged: this command is run by a human at a terminal
    # who needs to see what happened to their file.
    print(json.dumps(summary, indent=2, default=str))

    # Non-zero when nothing landed at all, so a scheduled run fails loudly.
    if report.created == 0 and report.updated == 0 and report.unchanged == 0:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="create known robots and seed user accounts")
    seed.set_defaults(func=cmd_seed)

    imp = sub.add_parser(
        "import-episodes", help="import episodes from a CSV export (idempotent)"
    )
    imp.add_argument("path", nargs="?", default=settings.seed_episodes_path)
    imp.set_defaults(func=cmd_import_episodes)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging(settings.log_level)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
