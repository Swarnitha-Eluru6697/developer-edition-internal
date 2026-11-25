"""Profiles uploaded files that have no columns: slides, images, audio, archives.

Reads {"path", "format"} as JSON on stdin and prints one JSON profile. It checks
the file really is the declared format, using its signature or container, and
reports only what it can measure from the bytes. Standard library only.
"""
import json
import os
import struct
import sys
import wave
import zipfile
import re

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"}


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(0)


def base(fmt, path, kind):
    return {"ok": True, "kind": kind, "format": fmt, "bytes": os.path.getsize(path), "rowCount": 1, "columns": []}


def profile_pptx(path):
    if not zipfile.is_zipfile(path):
        fail("not a PowerPoint .pptx file (it isn't an Office Open XML package)")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "ppt/presentation.xml" not in names:
            fail("not a PowerPoint .pptx file (no ppt/presentation.xml inside)")
        slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        words = 0
        for n in slides:
            xml = z.read(n).decode("utf8", "ignore")
            text = " ".join(re.findall(r"<a:t>([^<]*)</a:t>", xml))
            words += len(text.split())
    p = base("pptx", path, "presentation")
    p.update({"slides": len(slides), "words": words})
    return p


def profile_ppt(path):
    with open(path, "rb") as f:
        sig = f.read(8)
    if sig != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        fail("not a legacy PowerPoint .ppt file (missing the OLE2 compound-file signature)")
    # Slide count isn't read from the legacy binary format — size only.
    return base("ppt", path, "presentation")


def profile_png(path):
    with open(path, "rb") as f:
        head = f.read(33)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        fail("not a PNG image (missing the PNG signature)")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", head[16:26])
    modes = {0: "grayscale", 2: "RGB", 3: "palette", 4: "grayscale+alpha", 6: "RGBA"}
    p = base("png", path, "image")
    p.update({"width": width, "height": height, "bitDepth": bit_depth, "colorMode": modes.get(color_type, str(color_type))})
    return p


def profile_wav(path):
    with open(path, "rb") as f:
        head = f.read(12)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        fail("not a WAV audio file (missing the RIFF/WAVE header)")
    p = base("wav", path, "audio")
    try:
        with wave.open(path, "rb") as w:
            ch, rate, frames = w.getnchannels(), w.getframerate(), w.getnframes()
            p.update({"channels": ch, "sampleRate": rate, "bitsPerSample": w.getsampwidth() * 8,
                      "durationSeconds": round(frames / rate, 3) if rate else None})
        return p
    except wave.Error:
        pass
    # Non-PCM WAV (e.g. float or compressed): read the fmt and data chunks directly.
    with open(path, "rb") as f:
        f.seek(12)
        fmt = None
        data_bytes = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                _, ch, rate, byte_rate, _, bits = struct.unpack("<HHIIHH", body[:16])
                fmt = (ch, rate, byte_rate, bits)
            elif cid == b"data":
                data_bytes = size
                f.seek(size, 1)
            else:
                f.seek(size + (size & 1), 1)
    if not fmt:
        fail("WAV file has no fmt chunk — can't read its audio format")
    ch, rate, byte_rate, bits = fmt
    p.update({"channels": ch, "sampleRate": rate, "bitsPerSample": bits,
              "durationSeconds": round(data_bytes / byte_rate, 3) if data_bytes and byte_rate else None})
    return p


def profile_zip(path):
    if not zipfile.is_zipfile(path):
        fail("not a ZIP archive")
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            fail(f"ZIP archive is corrupt (first bad entry: {bad})")
        files = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
                 and not os.path.basename(i.filename).startswith(".")]
    by_ext = {}
    for i in files:
        ext = i.filename.rsplit(".", 1)[-1].lower() if "." in os.path.basename(i.filename) else "(none)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
    p = base("zip", path, "archive")
    p.update({
        "rowCount": len(files),
        "files": len(files),
        "uncompressedBytes": sum(i.file_size for i in files),
        "fileTypes": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
        "allImages": bool(files) and all(e in IMAGE_EXTS for e in by_ext),
        "sample": [i.filename for i in files[:10]],
    })
    if not files:
        fail("ZIP archive contains no files")
    return p


def main():
    req = json.loads(sys.stdin.read() or "{}")
    path, fmt = req.get("path"), req.get("format")
    if not path or not os.path.exists(path):
        fail("file not found")
    handlers = {"pptx": profile_pptx, "ppt": profile_ppt, "png": profile_png, "wav": profile_wav, "zip": profile_zip}
    if fmt not in handlers:
        fail(f"no profiler for format {fmt}")
    print(json.dumps(handlers[fmt](path)))


if __name__ == "__main__":
    main()
