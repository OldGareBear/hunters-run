"""Roles and the permission policy. This is the single source of truth for who may do what.

Document *visibility* is per document (a set of roles), and is enforced in
`can_view_document` and, for the chatbot, inside the retrieval SQL itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.models import Document, User


class Role(StrEnum):
    ADMIN = "admin"
    BOARD = "board"
    MANAGEMENT = "management"
    HOMEOWNER = "homeowner"

    @property
    def label(self) -> str:
        return {
            Role.ADMIN: "Administrator",
            Role.BOARD: "Board member",
            Role.MANAGEMENT: "Management",
            Role.HOMEOWNER: "Homeowner",
        }[self]


ALL_ROLES: tuple[Role, ...] = (Role.ADMIN, Role.BOARD, Role.MANAGEMENT, Role.HOMEOWNER)
# Roles a document can be shared with. Admins always see every document.
SHAREABLE_ROLES: tuple[Role, ...] = (Role.BOARD, Role.MANAGEMENT, Role.HOMEOWNER)

VISIBILITY_PRESETS: dict[str, tuple[Role, ...]] = {
    "everyone": (Role.BOARD, Role.MANAGEMENT, Role.HOMEOWNER),
    "board_management": (Role.BOARD, Role.MANAGEMENT),
    "board_only": (Role.BOARD,),
    "management_only": (Role.MANAGEMENT,),
}


class Perm(StrEnum):
    CHAT = "chat"
    DOCS_UPLOAD = "docs.upload"
    DOCS_MANAGE = "docs.manage"  # edit visibility / delete docs the user can see
    USERS_VIEW = "users.view"
    USERS_INVITE_ANY = "users.invite_any"
    USERS_INVITE_HOMEOWNER = "users.invite_homeowner"
    USERS_MANAGE = "users.manage"  # change roles, deactivate
    BLASTS_SEND = "blasts.send"
    ASK_MANAGEMENT = "management.ask"  # email a question to the management company
    AUDIT_VIEW = "audit.view"
    DUTIES_MANAGE = "duties.manage"  # the calendar of recurring duties and its reminders
    RESERVATIONS_BOOK = "reservations.book"
    # Approve and decline requests, cancel anyone's booking, block time, see who booked.
    RESERVATIONS_MANAGE = "reservations.manage"


POLICY: dict[Role, frozenset[Perm]] = {
    Role.ADMIN: frozenset(Perm),
    Role.BOARD: frozenset(
        {
            Perm.CHAT,
            Perm.DOCS_UPLOAD,
            Perm.DOCS_MANAGE,
            Perm.USERS_VIEW,
            Perm.USERS_INVITE_HOMEOWNER,
            Perm.BLASTS_SEND,
            Perm.AUDIT_VIEW,
            Perm.ASK_MANAGEMENT,  # board members are homeowners too
            Perm.DUTIES_MANAGE,
            Perm.RESERVATIONS_BOOK,
        }
    ),
    Role.MANAGEMENT: frozenset(
        {
            Perm.CHAT,
            Perm.DOCS_UPLOAD,
            Perm.DOCS_MANAGE,
            Perm.USERS_VIEW,
            Perm.USERS_INVITE_HOMEOWNER,
            Perm.RESERVATIONS_MANAGE,
            Perm.DUTIES_MANAGE,
        }
    ),
    Role.HOMEOWNER: frozenset({Perm.CHAT, Perm.ASK_MANAGEMENT, Perm.RESERVATIONS_BOOK}),
}


def has_perm(user: User | None, perm: Perm) -> bool:
    if user is None or not user.is_active:
        return False
    return perm in POLICY.get(Role(user.role), frozenset())


def roles_user_may_invite(user: User) -> list[Role]:
    if has_perm(user, Perm.USERS_INVITE_ANY):
        return list(ALL_ROLES)
    if has_perm(user, Perm.USERS_INVITE_HOMEOWNER):
        return [Role.HOMEOWNER]
    return []


def visible_roles_for(user: User) -> list[str] | None:
    """Roles whose documents this user may read; None means 'all' (admins)."""
    if Role(user.role) == Role.ADMIN:
        return None
    return [user.role]


def can_view_document(user: User | None, doc: Document) -> bool:
    if user is None or not user.is_active or doc.deleted_at is not None:
        return False
    if Role(user.role) == Role.ADMIN:
        return True
    return user.role in doc.role_names


def can_manage_document(user: User | None, doc: Document) -> bool:
    return has_perm(user, Perm.DOCS_MANAGE) and can_view_document(user, doc)
