"""Load, chunk and embed the curated fix playbooks (PROJECT_SPEC §5.1).

``data/knowledge/`` holds one markdown playbook per fix category (images,
fonts, code splitting, caching, CWV tactics). Each is split on its markdown
headings rather than a fixed character window, so a retrieved chunk is a whole
coherent tactic — "Serve modern formats", with its expected impact and
trade-offs intact — instead of a sentence cut mid-thought.

Front matter (``--- key: value ---``) carries the metadata the estimator needs:
which metrics a playbook affects and the expected improvement range. That range
is what keeps §11's "LLM hallucinated improvement magnitudes" risk in check —
magnitudes come from the playbook, not from the model.

Indexing is idempotent: chunk ids are derived from the file and heading path, so
re-indexing an edited playbook replaces its chunks rather than duplicating them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from store.vectordb import Document

KNOWLEDGE_KIND = "knowledge"
DEFAULT_KNOWLEDGE_DIR = Path("data/knowledge")

# A heading line: capture level and text.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
# Front matter delimited by --- at the very top of the file.
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class KnowledgeError(Exception):
    """User-facing error for knowledge base loading failures."""


@dataclass
class Chunk:
    """One retrievable section of a playbook."""

    chunk_id: str
    text: str
    source: str
    heading_path: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> Document:
        meta = dict(self.metadata)
        meta["heading_path"] = list(self.heading_path)
        return Document(
            doc_id=self.chunk_id,
            text=self.text,
            kind=KNOWLEDGE_KIND,
            source=self.source,
            metadata=meta,
        )


def parse_front_matter(text: str) -> tuple:
    """Split simple ``key: value`` front matter from the body.

    Deliberately not YAML: the front matter is a handful of scalars and lists,
    and avoiding a parser here keeps untrusted-ish content off a richer
    deserializer. Comma-separated values become lists; ints/floats are coerced.
    """
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text

    meta: Dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        meta[key.strip()] = _coerce(raw.strip())
    return meta, text[match.end():]


def _coerce(raw: str) -> Any:
    if "," in raw:
        return [_coerce(part.strip()) for part in raw.split(",") if part.strip()]
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw.strip('"\'')


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "section"


def chunk_markdown(
    text: str,
    *,
    source: str,
    metadata: Optional[Dict[str, Any]] = None,
    min_chars: int = 40,
) -> List[Chunk]:
    """Split a playbook into one chunk per heading section.

    Content before the first heading is kept as an intro chunk so a preamble is
    never silently dropped. Sections shorter than ``min_chars`` (a bare heading
    with no body) are skipped — they carry no retrievable signal.
    """
    base_meta = dict(metadata or {})
    lines = text.splitlines()

    sections: List[tuple] = []  # (heading_path, [body lines])
    path: List[str] = []
    current: List[str] = []
    current_path: List[str] = []

    def flush():
        if current:
            sections.append((list(current_path), list(current)))

    for line in lines:
        match = _HEADING.match(line)
        if match:
            flush()
            current.clear()
            level = len(match.group(1))
            title = match.group(2).strip()
            path = path[: level - 1]
            path.append(title)
            current_path = list(path)
        else:
            current.append(line)
    flush()

    chunks: List[Chunk] = []
    seen: Dict[str, int] = {}
    for heading_path, body in sections:
        body_text = "\n".join(body).strip()
        if not body_text:
            continue
        # Prefix the heading trail so a chunk reads standalone in the prompt.
        header = " > ".join(heading_path)
        full = f"{header}\n\n{body_text}" if header else body_text
        if len(full) < min_chars:
            continue

        slug = "-".join(_slug(h) for h in heading_path) or "intro"
        seen[slug] = seen.get(slug, 0) + 1
        suffix = "" if seen[slug] == 1 else f"-{seen[slug]}"
        chunk_id = f"{source}#{slug}{suffix}"

        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                text=full,
                source=source,
                heading_path=list(heading_path),
                metadata=dict(base_meta),
            )
        )
    return chunks


def load_playbook(path: str | Path) -> List[Chunk]:
    """Read one playbook file into chunks, front matter applied as metadata."""
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KnowledgeError(f"Playbook not found: {file_path}") from exc
    except UnicodeDecodeError as exc:
        raise KnowledgeError(f"Playbook is not valid UTF-8: {file_path}") from exc

    meta, body = parse_front_matter(raw)
    return chunk_markdown(body, source=file_path.name, metadata=meta)


def load_knowledge_dir(directory: str | Path = DEFAULT_KNOWLEDGE_DIR) -> List[Chunk]:
    """Load every ``*.md`` playbook in a directory, sorted for determinism."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise KnowledgeError(f"Knowledge directory not found: {dir_path}")
    chunks: List[Chunk] = []
    for file_path in sorted(dir_path.glob("*.md")):
        chunks.extend(load_playbook(file_path))
    return chunks


def index_knowledge(
    store,
    client,
    *,
    directory: str | Path = DEFAULT_KNOWLEDGE_DIR,
    chunks: Optional[Sequence[Chunk]] = None,
) -> int:
    """Embed playbook chunks into the vector store. Returns chunks indexed.

    Re-running is safe and cheap: chunk ids are stable, so edited playbooks
    replace their old chunks, and the embedding cache means unchanged text
    costs no API calls.
    """
    items = list(chunks) if chunks is not None else load_knowledge_dir(directory)
    if not items:
        return 0
    vectors = client.embed_documents([c.text for c in items])
    return store.add(
        [c.to_document() for c in items], vectors, model=client.model
    )


def content_digest(chunks: Iterable[Chunk]) -> str:
    """Stable digest of a corpus — lets a caller detect drift cheaply."""
    hasher = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: c.chunk_id):
        hasher.update(chunk.chunk_id.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(chunk.text.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()
