"""Permission-filtered retrieval.

The role filter is part of the SQL query, so chunks from documents the asking
user can't read are never loaded, scored, or sent to the model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import Text, cast, exists, func, literal_column, select, text
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


def _shared_with(roles: list[str]):
    return exists(
        select(1).where(DocumentAccess.document_id == Document.id, DocumentAccess.role.in_(roles))
    )


def permitted_chunks_query(user: User, also_visible_to: str | None = None):
    """Chunks of documents the user may read. With also_visible_to, only documents that
    role may read too (e.g. what an email to management may quote, with the asker copied)."""
    q = (
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.status == "ready", Document.deleted_at.is_(None))
    )
    roles = visible_roles_for(user)
    if roles is not None:
        q = q.where(_shared_with(roles))
    if also_visible_to is not None:
        q = q.where(_shared_with([also_visible_to]))
    return q


# --- Keyword search ----------------------------------------------------------
# Postgres: the expression matches the GIN index in migration 0004, so keep them in step.
TSVECTOR = "to_tsvector('english', document_chunks.text)"
ENGLISH = literal_column("'english'::regconfig")


def _tsquery(query: str):
    """Any of the question's words may match (plainto_tsquery alone requires all of them)."""
    words = cast(func.plainto_tsquery(ENGLISH, query), Text)
    return func.to_tsquery(ENGLISH, func.replace(words, " & ", " | "))


_WORD = re.compile(r"[a-z0-9]+")
_STOP = set(
    "a an and are as at be but by can do does for from has have how i if in is it its may "
    "me my no not of on or our should so than that the their there these they this to "
    "us was we what when where which who why will with would you your".split()
)


def _terms(value: str) -> list[str]:
    out = []
    for w in _WORD.findall(value.lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        if len(w) > 3 and w.endswith("e"):
            w = w[:-1]  # fence / fences / fencing -> fenc
        out.append(w)
    return out


def _keyword_scores(rows: list, query: str) -> list[tuple]:
    """Small BM25 over the permitted chunks, for SQLite (dev and tests)."""
    q = set(_terms(query))
    if not q:
        return []
    docs = [(c, d, Counter(_terms(c.text))) for c, d in rows]
    n = len(docs) or 1
    avg = sum(sum(tf.values()) for *_, tf in docs) / n or 1.0
    df = Counter(t for *_, tf in docs for t in q if tf[t])
    scored = []
    for c, d, tf in docs:
        length = sum(tf.values()) or 1
        score = 0.0
        for t in q:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * length / avg))
        if score > 0:
            scored.append((c, d, score / (score + 1)))  # squash to 0..1 like the ranks below
    return scored


def _keyword(db: Session, base, query: str, k: int) -> list[tuple]:
    if db.get_bind().dialect.name == "postgresql":
        vector, tsq = literal_column(TSVECTOR), _tsquery(query)
        rank = func.ts_rank_cd(vector, tsq, 32)
        rows = db.execute(
            base.where(vector.op("@@")(tsq))
            .add_columns(rank.label("rank"))
            .order_by(rank.desc())
            .limit(k)
        )
        return [(c, d, float(r)) for c, d, r in rows]
    scored = _keyword_scores(list(db.execute(base)), query)
    scored.sort(key=lambda r: r[2], reverse=True)
    return scored[:k]


def retrieve(
    db: Session,
    user: User,
    query: str,
    *,
    k: int | None = None,
    min_score: float | None = None,
    also_visible_to: str | None = None,
) -> list[Source]:
    s = get_settings()
    k = k or s.rag_top_k
    base = permitted_chunks_query(user, also_visible_to)
    if s.retrieval == "keyword":
        # Any matching passage counts; the ranking decides which k go to the model.
        scored = _keyword(db, base, query, k)
        return [
            Source(c.id, d.id, d.title, c.page, c.text, round(score, 4)) for c, d, score in scored
        ]

    min_score = s.rag_min_score if min_score is None else min_score
    qvec = get_embedder().embed_query(query)

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
