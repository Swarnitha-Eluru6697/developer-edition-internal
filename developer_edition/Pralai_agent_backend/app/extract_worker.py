"""Isolated document-text extraction for Agent Files.

Run as a separate, resource-limited process (see agent_assets.extract_text_isolated):
bytes on stdin, extension in argv[1], one JSON line on stdout. Parsing untrusted
PDF/DOCX happens here, never in the API process.
"""
import io
import json
import sys

MAX_CHARS = 200_000


def main() -> int:
    ext = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    data = sys.stdin.buffer.read()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(data))
            if r.is_encrypted:
                raise ValueError("encrypted PDF")
            text = "\n".join((p.extract_text() or "") for p in r.pages[:100])
        elif ext == ".docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append("\t".join(c.text for c in row.cells))
            text = "\n".join(parts)
        else:
            text = data.decode("utf-8", errors="replace")
        sys.stdout.write(json.dumps({"ok": True, "text": text[:MAX_CHARS]}))
    except Exception as e:  # noqa: BLE001 - report, never crash the caller
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
