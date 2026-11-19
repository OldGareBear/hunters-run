"""Permission-filtered retrieval.

The role filter is part of the SQL query, so chunks from documents the asking
user can't read are never loaded, scored, or sent to the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.embeddings import get_embedder
from app.models import Document, DocumentAccess, DocumentChunk, User
from app.permissions import visible_roles_for


@dataclass
class Source:
    chunk_id: int
    document_id: int
    title: str
    page: int | None
    text: str
    score: float

    @property
    def label(self) -> str:
        return f"{self.title} (page {self.page})" if self.page else self.title


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def permitted_chunks_query(user: User):
    q = (
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == "ready", Document.deleted_at.is_(None))
    )
    roles = visible_roles_for(user)
    if roles is not None:
        q = q.where(
            exists(
                select(1).where(
                    DocumentAccess.document_id == Document.id, DocumentAccess.role.in_(roles)
                )
            )
        )
    return q


def retrieve(
    db: Session, user: User, query: str, *, k: int | None = None, min_score: float | None = None
) -> list[Source]:
    s = get_settings()
    k = k or s.rag_top_k
    min_score = s.rag_min_score if min_score is None else min_score
    qvec = get_embedder().embed_query(query)
    base = permitted_chunks_query(user)

    if db.get_bind().dialect.name == "postgresql":
        distance = DocumentChunk.embedding.cosine_distance(qvec)
        db.execute(text("SET LOCAL hnsw.ef_search = 200"))
        rows = db.execute(base.add_columns(distance.label("distance")).order_by(distance).limit(k))
        scored = [(chunk, doc, 1.0 - float(dist)) for chunk, doc, dist in rows]
    else:  # SQLite (dev/tests): exact scan in Python over permitted chunks only
        scored = [(c, d, _cosine(qvec, c.embedding)) for c, d in db.execute(base)]
        scored.sort(key=lambda r: r[2], reverse=True)
        scored = scored[:k]

    return [
        Source(c.id, d.id, d.title, c.page, c.text, round(score, 4))
        for c, d, score in scored
        if score >= min_score
    ]
