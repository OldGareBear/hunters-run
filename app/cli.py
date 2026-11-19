"""Management commands.

python -m app.cli generate-key                 # key for LOCAL_STORAGE_KEY / SECRET_KEY
python -m app.cli create-admin you@example.com # first administrator (prompts for password)
python -m app.cli invite person@example.com homeowner
python -m app.cli seed                         # demo users + sample documents (dev only)
python -m app.cli reindex                      # rebuild embeddings, e.g. after switching provider
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import session_factory
from app.email.outbox import deliver_pending
from app.models import Document, User
from app.permissions import Role
from app.security.passwords import hash_password, password_problems
from app.services import accounts
from app.services import documents as doc_service
from app.storage.local import generate_key

SEED_DIR = Path(__file__).resolve().parent.parent / "seed" / "documents"

SEED_USERS = [
    ("admin@example.com", "Alex Admin", Role.ADMIN),
    ("board@example.com", "Blair Board", Role.BOARD),
    ("manager@example.com", "Morgan Manager", Role.MANAGEMENT),
    ("owner@example.com", "Harper Homeowner", Role.HOMEOWNER),
]
# (file, title, roles)
SEED_DOCS = [
    (
        "rules-and-regulations.md",
        "Rules and Regulations (sample)",
        ["board", "management", "homeowner"],
    ),
    (
        "pool-and-amenities.md",
        "Pool and Amenities Policy (sample)",
        ["board", "management", "homeowner"],
    ),
    (
        "architectural-guidelines.md",
        "Architectural Review Guidelines (sample)",
        ["board", "management", "homeowner"],
    ),
    (
        "board-minutes-executive-session.md",
        "Executive Session Minutes (sample, board only)",
        ["board"],
    ),
    ("management-agreement.md", "Management Agreement Summary (sample)", ["board", "management"]),
]


def cmd_generate_key(_args) -> None:
    print("LOCAL_STORAGE_KEY=" + generate_key())
    print("SECRET_KEY=" + secrets.token_urlsafe(48))


def cmd_create_admin(args) -> None:
    db = session_factory()()
    email = accounts.normalise_email(args.email)
    if accounts.get_user_by_email(db, email):
        sys.exit(f"{email} already has an account.")
    password = getpass.getpass("Password: ")
    problems = password_problems(password, email)
    if password != getpass.getpass("Confirm: "):
        problems.append("Passwords don't match.")
    if problems:
        sys.exit("\n".join(problems))
    db.add(
        User(
            email=email,
            full_name=args.name or "",
            role=Role.ADMIN.value,
            password_hash=hash_password(password),
        )
    )
    db.commit()
    print(f"Created administrator {email}.")


def cmd_invite(args) -> None:
    db = session_factory()()
    inv = accounts.create_invite(db, email=args.email, role=Role(args.role), invited_by=None)
    db.commit()
    deliver_pending(db)
    print(f"Invite queued for {inv.email} ({args.role}).")


def cmd_seed(_args) -> None:
    s = get_settings()
    if s.is_production:
        sys.exit("Refusing to seed demo data in production.")
    password = s.seed_demo_password or "demo-password-123"
    db = session_factory()()
    users = {}
    for email, name, role in SEED_USERS:
        user = accounts.get_user_by_email(db, email)
        if user is None:
            user = User(
                email=email, full_name=name, role=role.value, password_hash=hash_password(password)
            )
            db.add(user)
            db.flush()
        users[role] = user
    for filename, title, roles in SEED_DOCS:
        if db.scalar(
            select(Document).where(Document.title == title, Document.deleted_at.is_(None))
        ):
            continue
        data = (SEED_DIR / filename).read_bytes()
        doc_service.create_document(
            db,
            uploader=users[Role.ADMIN],
            title=title,
            description="Sample document for demos.",
            filename=filename,
            data=data,
            declared_type="text/markdown",
            roles=roles,
        )
    db.commit()
    print(f"Seeded demo data. Sign in with any of these (password: {password}):")
    for email, _name, role in SEED_USERS:
        print(f"  {role.value:<11} {email}")


def cmd_reindex(_args) -> None:
    db = session_factory()()
    docs = list(db.scalars(select(Document).where(Document.deleted_at.is_(None))))
    for doc in docs:
        doc_service.index_document(db, doc)
        db.commit()
        print(f"{doc.status:<8} {doc.title}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate-key").set_defaults(func=cmd_generate_key)
    p = sub.add_parser("create-admin")
    p.add_argument("email")
    p.add_argument("--name", default="")
    p.set_defaults(func=cmd_create_admin)
    p = sub.add_parser("invite")
    p.add_argument("email")
    p.add_argument("role", choices=[r.value for r in Role])
    p.set_defaults(func=cmd_invite)
    sub.add_parser("seed").set_defaults(func=cmd_seed)
    sub.add_parser("reindex").set_defaults(func=cmd_reindex)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
