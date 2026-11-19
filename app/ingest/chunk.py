"""Split page text into overlapping chunks on paragraph / sentence boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingest.extract import Page

_WS = re.compile(r"[ \t\f\v]+")
_SENT = re.compile(r"(?<=[.!?;:])\s+")


@dataclass
class Chunk:
    ordinal: int
    page: int | None
    text: str


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pieces(text: str, size: int) -> list[str]:
    """Paragraphs, with any paragraph longer than `size` split into sentences/words."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(para)
            continue
        buf = ""
        for sent in _SENT.split(para):
            while len(sent) > size:  # pathological long run with no punctuation
                cut = sent.rfind(" ", 0, size)
                cut = cut if cut > size // 2 else size
                out.append(sent[:cut].strip())
                sent = sent[cut:].strip()
            if len(buf) + len(sent) + 1 > size and buf:
                out.append(buf.strip())
                buf = ""
            buf += sent + " "
        if buf.strip():
            out.append(buf.strip())
    return out


def chunk_pages(pages: list[Page], size: int = 1800, overlap: int = 250) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page in pages:
        text = _normalise(page.text)
        if not text:
            continue
        buf = ""
        for piece in _pieces(text, size):
            if buf and len(buf) + len(piece) + 2 > size:
                chunks.append(Chunk(len(chunks), page.number, buf.strip()))
                tail = buf[-overlap:] if overlap else ""
                # start the overlap at a word boundary
                space = tail.find(" ")
                buf = (tail[space + 1 :] if space != -1 else tail) + "\n\n"
            buf += piece + "\n\n"
        if buf.strip():
            chunks.append(Chunk(len(chunks), page.number, buf.strip()))
    return chunks
