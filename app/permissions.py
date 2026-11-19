"""Roles and the permission policy. This is the single source of truth for who may do what.

Document *visibility* is per document (a set of roles), and is enforced in
`can_view_document` and, for the chatbot, inside the retrieval SQL itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from app.config import get_settings

if TYPE_CHECKING:  # pragma: no cover
    from app.models import Document, Ticket, User


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
    TICKETS_CREATE = "tickets.create"
    TICKETS_HANDLE = "tickets.handle"
    AUDIT_VIEW = "audit.view"


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
            Perm.TICKETS_CREATE,  # board members are homeowners too
        }
    ),
    Role.MANAGEMENT: frozenset(
        {
            Perm.CHAT,
            Perm.DOCS_UPLOAD,
            Perm.DOCS_MANAGE,
            Perm.USERS_VIEW,
            Perm.USERS_INVITE_HOMEOWNER,
            Perm.TICKETS_HANDLE,
        }
    ),
    Role.HOMEOWNER: frozenset({Perm.CHAT, Perm.TICKETS_CREATE}),
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


def can_view_ticket(user: User | None, ticket: Ticket) -> bool:
    if user is None or not user.is_active:
        return False
    if has_perm(user, Perm.TICKETS_HANDLE):
        return True
    if Role(user.role) == Role.BOARD and get_settings().board_can_view_tickets:
        return True
    return ticket.created_by_id == user.id
