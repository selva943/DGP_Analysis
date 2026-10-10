"""Seed the authentication users (1 admin + 7 viewers by default).

Reads ``liveapp/seed_users.json`` and inserts any user that does not exist yet.
Existing users are left untouched, so re-running is safe and never resets a
password that an administrator has already changed.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .config import LIVEAPP_DIR
from .models import User
from .security import hash_password

SEED_FILE = LIVEAPP_DIR / "seed_users.json"


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
