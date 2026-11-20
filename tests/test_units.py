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
    assert POLICY[Role.HOMEOWNER] == frozenset(
        {Perm.CHAT, Perm.TICKETS_CREATE, Perm.RESERVATIONS_BOOK}
    )
    assert Perm.RESERVATIONS_MANAGE in POLICY[Role.MANAGEMENT]
    assert Perm.RESERVATIONS_MANAGE not in POLICY[Role.BOARD]


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


def _fake_anthropic(message, events=()):
    class Stream:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            Stream.closed = True
            return False

        def __iter__(self):
            return iter(events)

        def get_final_message(self):
            return message

    captured = {}

    def stream(**kwargs):
        captured.update(kwargs)
        return Stream()

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    client.stream_class = Stream
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
        model="claude-haiku-5-5",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=None),
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
    assert (answer.input_tokens, answer.output_tokens, answer.truncated) == (1200, 80, False)

    assert captured["model"] == "claude-haiku-5-5"
    assert "temperature" not in captured
    assert "fallbacks" not in captured  # Haiku has no server-side fallback
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 1000
    assert captured["output_config"] == {"effort": "medium"}
    content = captured["messages"][-1]["content"]
    docs = [b for b in content if b["type"] == "document"]
    assert len(docs) == 2 and all(d["citations"] == {"enabled": True} for d in docs)
    assert docs[0]["title"] == "Pool policy (page 2)"
    assert content[-1] == {"type": "text", "text": "When?"}


@pytest.mark.parametrize(
    "model,thinking,fallbacks",
    [
        ("claude-haiku-5-5", {"type": "disabled"}, False),
        ("claude-sonnet-5-5", {"type": "between_tools"}, True),  # "disabled" is a 400 there
        ("claude-opus-5-5", None, True),  # thinking can't be turned off; effort controls it
    ],
)
def test_thinking_and_fallback_per_model(monkeypatch, model, thinking, fallbacks):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "anthropic_model", model)
    monkeypatch.setattr(s, "anthropic_fallbacks", True)
    message = SimpleNamespace(stop_reason="end_turn", model=model, usage=None, content=[])
    client, captured = _fake_anthropic(message)
    _llm_with(client).answer(question="When?", history=[], sources=SOURCES)
    assert captured.get("thinking") == thinking
    assert ("fallbacks" in captured) is fallbacks


def test_anthropic_client_fails_inside_the_request_time_limit(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-placeholder")
    client = AnthropicLLM().client
    assert client.max_retries == 1
    assert (client.timeout.read, client.timeout.connect) == (10.0, 3.0)


def test_a_slow_stream_is_abandoned_before_the_request_time_limit(monkeypatch):
    import app.rag.llm as llm_module

    clock = [1000.0]
    monkeypatch.setattr(llm_module.time, "monotonic", lambda: clock[0])

    def trickle():  # a chunk every 4 seconds, for a minute
        for _ in range(15):
            clock[0] += 4
            yield SimpleNamespace(type="content_block_delta")

    message = SimpleNamespace(stop_reason="end_turn", model="m", usage=None, content=[])
    client, _ = _fake_anthropic(message, events=trickle())
    with pytest.raises(llm_module.AnswerTookTooLong) as slow:
        _llm_with(client).answer(question="x", history=[], sources=SOURCES)
    # no usage seen yet: the worst case is counted, since Anthropic bills what it made
    assert slow.value.output_tokens == 1000
    assert clock[0] - 1000.0 <= llm_module.ANSWER_DEADLINE_SECONDS + 4  # stopped at once
    assert client.stream_class.closed  # the connection is let go


def test_models_that_always_think_get_room_and_low_effort():
    from app.config import Settings

    opus = Settings(llm_provider="anthropic", anthropic_model="claude-opus-5-5")
    assert (opus.anthropic_effort, opus.anthropic_max_tokens) == ("low", 4000)
    message = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=6000, output_tokens=2500),
        content=[SimpleNamespace(type="text", text="Yes.", citations=[])],
    )
    client, captured = _fake_anthropic(message)
    llm = _llm_with(client)
    llm.settings = opus
    answer = llm.answer(question="x", history=[], sources=SOURCES)
    assert captured["max_tokens"] == 4000 and captured["output_config"] == {"effort": "low"}
    assert "thinking" not in captured
    assert (answer.input_tokens, answer.output_tokens) == (6000, 2500)  # spend from usage

    # An effort or thinking that was asked for is kept; a higher cap too.
    s = Settings(
        llm_provider="anthropic", anthropic_model="claude-opus-5-5", anthropic_effort="high"
    )
    assert (s.anthropic_effort, s.anthropic_max_tokens) == ("high", 4000)
    s = Settings(
        llm_provider="anthropic", anthropic_model="claude-opus-5-5", anthropic_thinking=True
    )
    assert s.anthropic_effort == "medium"
    s = Settings(
        llm_provider="anthropic", anthropic_model="claude-opus-5-5", anthropic_max_tokens=8000
    )
    assert s.anthropic_max_tokens == 8000
    # Models that can turn thinking off keep the usual settings.
    haiku = Settings(llm_provider="anthropic", anthropic_model="claude-haiku-5-5")
    assert (haiku.anthropic_effort, haiku.anthropic_max_tokens) == ("medium", 1000)


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
