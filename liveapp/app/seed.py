"""Create the initial login users on a fresh deployment.

Two independent paths, both safe to run on every startup:

* ``bootstrap_admin`` - reads DGP_BOOTSTRAP_ADMIN_USERNAME / _PASSWORD from the
  environment and creates that single admin, but *only* when the users table is
  empty. This is how a serverless deployment gets its first login: the password
  lives in the platform's env vars and never enters git. Once any user exists
  it is a no-op, so it cannot resurrect or re-password anyone.

* ``seed_users`` - reads ``liveapp/seed_users.json`` and inserts any user that
  is not there yet. That file is deliberately gitignored (it holds plaintext
  passwords), so it is only present on the source machine.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .config import LIVEAPP_DIR, settings
from .models import User
from .security import hash_password

SEED_FILE = LIVEAPP_DIR / "seed_users.json"


def bootstrap_admin(db: Session) -> str | None:
    """Create the env-configured admin if - and only if - there are no users.

    Returns the created username, or None when nothing was done.
    """
    spec = settings.bootstrap_admin
    if spec is None:
        return None
    username, password, display_name = spec

    if db.query(User.id).first() is not None:
        return None

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            display_name=display_name,
        )
    )
    db.commit()
    return username


def seed_users(db: Session) -> list[str]:
    if not SEED_FILE.is_file():
        raise FileNotFoundError(f"Seed file not found: {SEED_FILE}")

    payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    users = payload.get("users", [])
    if not users:
        raise ValueError("seed_users.json contains no users")

    created: list[str] = []
    for entry in users:
        username = str(entry["username"]).strip()
        if not username:
            continue
        exists = db.query(User).filter(User.username == username).first()
        if exists:
            continue
        db.add(
            User(
                username=username,
                password_hash=hash_password(str(entry["password"])),
                role=str(entry.get("role", "viewer")).lower(),
                display_name=str(entry.get("display_name", username)),
            )
        )
        created.append(f"{username} ({entry.get('role', 'viewer')})")
    db.commit()
    return created
