"""Unit tests for storage, ingestion, the outbox and the Claude response parser."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from app.email import outbox as outbox_mod
from app.email.providers import EmailSendError
from app.ingest.chunk import chunk_pages
from app.ingest.extract import ExtractionError, Page, extract_pages, sniff_content_type
from app.models import EmailMessage
from app.permissions import POLICY, Perm, Role
from app.rag.llm import REFUSAL_ANSWER, AnthropicLLM
from app.rag.retrieval import Source
from app.storage.base import StorageError
from app.storage.local import LocalEncryptedStorage, generate_key


def test_policy_matrix():
    assert POLICY[Role.ADMIN] == frozenset(Perm)
    assert Perm.BLASTS_SEND in POLICY[Role.BOARD]
    assert Perm.BLASTS_SEND not in POLICY[Role.MANAGEMENT]
    assert Perm.TICKETS_HANDLE in POLICY[Role.MANAGEMENT]
    assert POLICY[Role.HOMEOWNER] == frozenset({Perm.CHAT, Perm.TICKETS_CREATE})


def test_local_storage_round_trip_and_tamper(tmp_path):
    import base64

    key = base64.urlsafe_b64decode(generate_key())
    store = LocalEncryptedStorage(tmp_path, key)
    store.put("abc", b"secret minutes", "text/plain")
    assert store.get("abc") == b"secret minutes"
    path = next(tmp_path.rglob("*.bin"))
    assert b"secret" not in path.read_bytes()
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 1
    path.write_bytes(bytes(blob))
    with pytest.raises(StorageError):
        store.get("abc")
    other = LocalEncryptedStorage(tmp_path, b"x" * 32)
    store.put("def", b"data", "text/plain")
    with pytest.raises(StorageError):
        other.get("def")


def test_chunking_overlaps_and_respects_size():
    text = "\n\n".join(f"Paragraph {i}. " + ("word " * 60) for i in range(20))
    chunks = chunk_pages([Page(3, text)], size=800, overlap=100)
    assert len(chunks) > 3
    assert all(len(c.text) <= 900 for c in chunks)
    assert all(c.page == 3 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_docx_extraction():
    import docx

    d = docx.Document()
    d.add_paragraph("Fences may be up to 4 feet tall.")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Fee"
    t.rows[0].cells[1].text = "$50"
    buf = io.BytesIO()
    d.save(buf)
    data = buf.getvalue()
    ctype = sniff_content_type("rules.docx", data, None)
    text = extract_pages(data, ctype)[0].text
    assert "4 feet" in text and "$50" in text


def test_sniffing_rejects_mismatched_content():
    with pytest.raises(ExtractionError):
        sniff_content_type("x.pdf", b"hello", "application/pdf")
    with pytest.raises(ExtractionError):
        sniff_content_type("x.txt", b"\xff\xfe\x00bad", "text/plain")
    assert sniff_content_type("x.pdf", b"%PDF-1.7 ...", None) == "application/pdf"


def test_outbox_retries_then_fails(db, monkeypatch):
    class Flaky:
        def send(self, msg):
            raise EmailSendError("temporary")

    monkeypatch.setattr(outbox_mod, "get_provider", lambda: Flaky())
    db.add(EmailMessage(to_email="a@example.com", subject="s", text_body="t", kind="ticket"))
    db.commit()
    outbox_mod.deliver_pending(db)
    m = db.query(EmailMessage).one()
    assert m.status == "pending" and m.attempts == 1 and m.last_error == "temporary"

    class Permanent:
        def send(self, msg):
            raise EmailSendError("bad address", permanent=True)

    monkeypatch.setattr(outbox_mod, "get_provider", lambda: Permanent())
    m.next_attempt_at = m.created_at
    db.commit()
    outbox_mod.deliver_pending(db)
    db.refresh(m)
    assert m.status == "failed"


def _fake_anthropic(message):
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return message

    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return Stream()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return client, captured


def _llm_with(client):
    llm = AnthropicLLM.__new__(AnthropicLLM)
    from app.config import get_settings

    llm.settings = get_settings()
    llm.client = client
    return llm


SOURCES = [
    Source(1, 10, "Pool policy", 2, "The pool opens at 10 AM.", 0.9),
    Source(2, 11, "Rules", None, "Quiet hours start at 10 PM.", 0.8),
]


def test_anthropic_request_shape_and_citation_parsing():
    message = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5-5",
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(
                type="text",
                text="The pool opens at 10 AM",
                citations=[
                    SimpleNamespace(
                        type="char_location",
                        document_index=0,
                        cited_text="The pool opens at 10 AM.",
                    )
                ],
            ),
            SimpleNamespace(
                type="text",
                text=" and quiet hours start at 10 PM.",
                citations=[
                    SimpleNamespace(
                        type="char_location",
                        document_index=1,
                        cited_text="Quiet hours start at 10 PM.",
                    )
                ],
            ),
        ],
    )
    client, captured = _fake_anthropic(message)
    answer = _llm_with(client).answer(question="When?", history=[], sources=SOURCES)
    assert (
        answer.text == "The pool opens at 10 AM[[src:0]] and quiet hours start at 10 PM.[[src:1]]"
    )
    assert [c.source_index for c in answer.citations] == [0, 1]

    assert captured["model"] == "claude-opus-5-5"
    assert "temperature" not in captured
    assert captured["fallbacks"] == "default"
    assert captured["output_config"] == {"effort": "medium"}
    content = captured["messages"][-1]["content"]
    docs = [b for b in content if b["type"] == "document"]
    assert len(docs) == 2 and all(d["citations"] == {"enabled": True} for d in docs)
    assert docs[0]["title"] == "Pool policy (page 2)"
    assert content[-1] == {"type": "text", "text": "When?"}


def test_anthropic_refusal_is_reported():
    message = SimpleNamespace(stop_reason="refusal", model="m", content=[])
    client, _ = _fake_anthropic(message)
    answer = _llm_with(client).answer(question="x", history=[], sources=SOURCES)
    assert answer.refused and answer.text == REFUSAL_ANSWER


def test_production_settings_are_validated():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(app_env="production")
