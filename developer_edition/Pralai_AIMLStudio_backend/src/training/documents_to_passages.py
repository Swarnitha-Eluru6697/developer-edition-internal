#!/usr/bin/env python3
"""Turns a document dataset (PDF, Markdown, text, HTML, Word, PowerPoint — single files
or inside ZIPs) into training passages for LLM fine-tuning on a `text` column.

stdin:  {"path", "format", "outPath", "maxChars": 2000, "maxPassages": null, "seed": 42}
stdout: {"ok", "path", "passages", "totalPassages", "documents", "byType", "skipped"}
Writes JSONL rows {"text", "source"}. Passages follow paragraph boundaries; when
maxPassages caps the set, a seeded sample is taken in a way that keeps every source
represented in proportion, so one large book can't crowd out the rest.
"""
import io, json, os, random, re, sys, zipfile

TEXT_EXT = {".md", ".mdx", ".txt", ".rst", ".markdown"}
HTML_EXT = {".html", ".htm"}
DOC_EXT = TEXT_EXT | HTML_EXT | {".pdf", ".docx", ".pptx"}


def html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n", raw)
    return re.sub(r"<[^>]+>", " ", raw)


def extract(name, data):
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_EXT:
        text = data.decode("utf-8", "ignore")
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # Markdown front matter
        return re.sub(r"(?s)<!--.*?-->", "", text)  # comments (e.g. licence headers)
    if ext in HTML_EXT:
        return html_to_text(data.decode("utf-8", "ignore"))
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if ext in (".docx", ".pptx"):
        z = zipfile.ZipFile(io.BytesIO(data))
        parts = [n for n in z.namelist() if (n == "word/document.xml") or re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        out = []
        for n in sorted(parts):
            xml = z.read(n).decode("utf-8", "ignore")
            xml = re.sub(r"</w:p>|</a:p>", "\n", xml)
            out.append(" ".join(re.findall(r"<(?:w|a):t[^>]*>([^<]*)</(?:w|a):t>", xml)).replace(" \n ", "\n"))
        return "\n\n".join(out)
    return None


def documents(path, fmt):
    """(source, bytes) for every document in the file / archive."""
    if fmt in ("zip", "image-zip"):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if not n.endswith("/") and not n.startswith("__MACOSX/"):
                    yield n, z.read(n)
    else:
        with open(path, "rb") as f:
            yield os.path.basename(path), f.read()


def chunk(text, max_chars):
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out, cur = [], ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:  # an oversized paragraph is split on a line or space
            cut = para.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else para.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if cur:
                out.append(cur); cur = ""
            out.append(para[:cut].strip()); para = para[cut:].strip()
        if len(cur) + len(para) + 2 > max_chars and cur:
            out.append(cur); cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [p for p in out if len(p) >= 200]  # skip fragments (headings alone, nav lines)


def main():
    req = json.loads(sys.stdin.read() or "{}")
    max_chars = int(req.get("maxChars") or 2000)
    max_passages = req.get("maxPassages")
    rng = random.Random(int(req.get("seed") or 42))
    by_type, skipped, docs = {}, 0, 0
    passages = []  # (source, text)
    for name, data in documents(req["path"], req["format"]):
        ext = os.path.splitext(name)[1].lower()
        if ext not in DOC_EXT or os.path.basename(name).upper().split(".")[0] in ("SOURCE", "LICENSE", "NOTICE", "COPYRIGHT"):
            skipped += 1
            continue
        try:
            text = extract(name, data)
        except Exception:
            text = None
        if not text or len(text.strip()) < 200:
            skipped += 1
            continue
        docs += 1
        by_type[ext.lstrip(".")] = by_type.get(ext.lstrip("."), 0) + 1
        passages += [(name, p) for p in chunk(text, max_chars)]
    total = len(passages)
    if not total:
        print(json.dumps({"ok": False, "error": "no readable text found — the dataset needs PDF, Markdown, text, HTML, Word or PowerPoint documents"}))
        return
    if max_passages and total > int(max_passages):
        keep = sorted(rng.sample(range(total), int(max_passages)))  # uniform over passages ⇒ proportional per source
        passages = [passages[i] for i in keep]
    with open(req["outPath"], "w") as f:
        for src, text in passages:
            f.write(json.dumps({"text": text, "source": src}, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "path": req["outPath"], "passages": len(passages), "totalPassages": total,
                      "documents": docs, "byType": by_type, "skipped": skipped}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
