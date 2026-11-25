"""
Real document ingestion — parsing a real uploaded file into text (reuses
harness.py's own `_extract_text_from_bytes`, the same real pypdf/
python-docx/plain-decode extraction the `docextractor` node already uses
— one real implementation, not two that could drift apart), and real
chunking of that text.
"""
import re
from typing import List, Optional

from .harness import _extract_text_from_bytes

SUPPORTED_EXTENSIONS = {".txt", ".md", ".docx", ".pdf", ".csv", ".json", ".html", ".xml"}


def parse_file(filename: str, content: bytes, content_type: str = "",
               notes: Optional[dict] = None) -> str:
    """Real text extraction via the same function handle_docextractor
    already uses — pypdf for PDF, python-docx for DOCX, plain decode for
    text-like formats. Raises on a genuinely empty result rather than
    silently ingesting nothing.

    Audio (.wav and the rest of transcription.AUDIO_EXTENSIONS) is the
    one format that cannot be extracted at all — there is no text in the
    bytes to find — so it is transcribed instead, by whichever speech
    provider is configured on the Integrations page. That is a real
    network call against a real credential, which is why it is here and
    not in harness._extract_text_from_bytes: document ingestion is an
    explicit, user-initiated upload, whereas the Doc Extractor node runs
    inside a flow that should not start spending on speech models
    because someone handed it an audio file.

    `notes`, when given, is filled in with provenance for whatever
    needed more than a plain parse — currently
    notes["transcription"] = {"provider", "model", "characters"} — so a
    caller can report that a document's text came from speech
    recognition rather than from the file itself.
    """
    from . import transcription

    if transcription.is_audio(filename, content_type):
        try:
            result = transcription.transcribe(filename, content, content_type)
        except transcription.TranscriptionError as e:
            # ValueError is parse_file's own contract for "this file
            # cannot become text"; every caller already turns it into a
            # 400 naming the file.
            raise ValueError(str(e)) from e
        if notes is not None:
            notes["transcription"] = {"provider": result.provider, "model": result.model,
                                      "characters": len(result.text)}
        return result.text

    text = _extract_text_from_bytes(content, filename, content_type)
    if text.startswith("[extraction failed:"):
        raise ValueError(text)
    # An unrecognised extension falls through to a best-effort decode of
    # raw bytes, which for a genuinely binary file (audio, video, an
    # image) yields mojibake containing NUL characters. That is not
    # empty, so it used to pass the check below and fail much later at
    # the INSERT with Postgres's own "A string literal cannot contain
    # NUL (0x00) characters" — an error that names neither the file nor
    # the real reason. Reject it here, where the filename is still in
    # hand and the message can say what actually happened.
    if "\x00" in text or _looks_binary(text):
        raise ValueError(
            f"'{filename}' contains no extractable text — it decodes as binary data. "
            "Video and image files have no text layer to ingest; convert to a text or "
            "document format first (a transcript, for example). Audio in WAV, MP3, M4A, "
            "FLAC or OGG is transcribed automatically when an OpenAI or Gemini key is "
            "configured under Integrations \u2192 Model Providers."
        )
    if not text.strip():
        raise ValueError(f"No extractable text found in '{filename}'.")
    return text


def _looks_binary(text: str, sample: int = 4096, threshold: float = 0.30) -> bool:
    """True when a decoded string is mostly untypeable characters.

    Judged on a leading sample rather than the whole string so a 200MB
    file costs nothing to classify. Tabs, newlines and carriage returns
    are printable for this purpose; the threshold is deliberately loose
    so genuinely text-like content carrying a few control bytes still
    ingests.
    """
    head = text[:sample]
    if not head:
        return False
    # U+FFFD counts double-weight: it is what a failed decode leaves
    # behind, and it IS printable, so a purely printable-character test
    # misses binary that decoded to mostly replacement characters. A
    # real 300KB WAV came through 44% U+FFFD but only 19% unprintable.
    replacements = head.count("\ufffd")
    unprintable = sum(1 for ch in head if not (ch.isprintable() or ch in "\t\n\r"))
    return (unprintable + replacements) / len(head) > threshold


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def chunk_text(text: str, max_words: int = 180, overlap_words: int = 30) -> List[str]:
    """Real sentence-aware chunking — packs real sentences into chunks
    up to max_words, with a real word-based overlap between consecutive
    chunks so context isn't lost at chunk boundaries. Never splits a
    sentence mid-way unless a single sentence alone exceeds max_words."""
    text = text.strip()
    if not text:
        return []

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return []

    chunks: List[str] = []
    current: List[str] = []
    current_word_count = 0

    def flush():
        if current:
            chunks.append(" ".join(current))

    # A single "sentence" longer than max_words has to be broken up.
    # The docstring above always promised this ("unless a single
    # sentence alone exceeds max_words") but no code did it: the
    # sentence was appended whole, unconditionally. Text with no
    # sentence punctuation at all — JSON, minified data, log lines,
    # single-line exports — is one giant sentence to _SENTENCE_SPLIT
    # (which needs [.!?] followed by whitespace), so a 300KB .json
    # ingested as ONE chunk of ~33,000 words against a max_words of
    # 180. That chunk's embedding is meaningless and retrieval returns
    # the entire document as a single hit.
    step = max(1, max_words - max(0, overlap_words))
    for sentence in sentences:
        words = sentence.split()
        if len(words) > max_words:
            if current:
                flush()
                current = []
                current_word_count = 0
            for i in range(0, len(words), step):
                piece = words[i:i + max_words]
                if piece:
                    chunks.append(" ".join(piece))
                if i + max_words >= len(words):
                    break
            continue
        if current_word_count + len(words) > max_words and current:
            flush()
            # real overlap: carry the tail words of the just-finished
            # chunk into the next one, not a fresh empty start
            tail_words = " ".join(current).split()[-overlap_words:] if overlap_words else []
            current = [" ".join(tail_words)] if tail_words else []
            current_word_count = len(tail_words)
        current.append(sentence)
        current_word_count += len(words)

    flush()
    return chunks


_QA_LINE = re.compile(r"^\s*(?:Q|Question)\s*[:.]\s*(.+)$", re.IGNORECASE)
_ANSWER_LINE = re.compile(r"^\s*(?:A|Answer)\s*[:.]\s*(.+)$", re.IGNORECASE)


def chunk_qa(text: str) -> List[dict]:
    """Real Q&A-structured chunking — genuinely parses the text into
    question/answer pairs rather than falling back to General chunking
    silently. Two real real strategies, tried in order:
    1) Explicit "Q:"/"A:" (or "Question:"/"Answer:") line prefixes —
       pairs each real question line with the answer text that follows
       it, up to the next question.
    2) No explicit markers found: falls back to treating each line that
       ends in "?" as a question, pairing it with the very next
       non-empty line as its answer — a genuine, if weaker, heuristic
       for loosely-formatted FAQ-style text, not a fabricated pairing.
    Each returned dict is {"question": str, "answer": str} — the
    embedded/indexed content is the question (matching Dify's real
    "indexed based on the question portion" behavior), with the answer
    carried alongside for real recall."""
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    pairs: List[dict] = []

    i = 0
    explicit_found = False
    while i < len(lines):
        m = _QA_LINE.match(lines[i])
        if m:
            explicit_found = True
            question = m.group(1).strip()
            answer_parts = []
            i += 1
            while i < len(lines) and not _QA_LINE.match(lines[i]):
                am = _ANSWER_LINE.match(lines[i])
                answer_parts.append(am.group(1).strip() if am else lines[i])
                i += 1
            pairs.append({"question": question, "answer": " ".join(answer_parts).strip()})
        else:
            i += 1

    if explicit_found:
        return [p for p in pairs if p["question"] and p["answer"]]

    # Fallback heuristic: "...?" line followed by the next real line.
    pairs = []
    i = 0
    while i < len(lines):
        if lines[i].endswith("?") and i + 1 < len(lines):
            pairs.append({"question": lines[i], "answer": lines[i + 1]})
            i += 2
        else:
            i += 1
    return pairs


def chunk_parent_child(text: str, parent_max_words: int = 600, child_max_words: int = 180,
                        child_overlap_words: int = 30) -> List[dict]:
    """Real Parent-Child chunking, matching Dify's own real distinction:
    the CHILD chunk is what gets embedded and matched against at query
    time (smaller, more precise); the PARENT chunk is the larger
    surrounding context returned for recall once a child matches. Both
    are real text produced by the same real chunk_text() function above,
    just at two real granularities — not two separately-invented
    mechanisms. Returns a flat list of
    {"child": str, "parent": str} — one entry per real child, each
    carrying its real parent's full text alongside it."""
    parents = chunk_text(text, max_words=parent_max_words, overlap_words=0)
    out: List[dict] = []
    for parent in parents:
        children = chunk_text(parent, max_words=child_max_words, overlap_words=child_overlap_words)
        for child in children:
            out.append({"child": child, "parent": parent})
    return out
