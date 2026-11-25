#!/usr/bin/env python3
"""Combines a dataset's files (the latest version of each) into one training input.

stdin: {"files": [{"path", "format", "fileName"}], "outDir"}
- Tables (csv/json/jsonl/xlsx/xls/parquet) are stacked into one Parquet table; columns
  missing from a file are left empty. A `source_file` column is NOT added, so the
  features stay exactly the dataset's own columns.
- Everything else becomes one ZIP: each archive's entries go under a folder named after
  its file (image class folders keep their names, so labels survive), single files are
  added as-is, and every metadata.csv (audio transcripts) is merged into one root
  metadata.csv with paths rewritten.
stdout: {"ok", "path", "format", "rows", "columns", "parts": [{"fileName", "rows"}]}
"""
import io, json, os, sys, zipfile

TABLE = {"csv", "json", "jsonl", "xlsx", "xls", "parquet"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def main():
    req = json.loads(sys.stdin.read() or "{}")
    files, out_dir = req["files"], req["outDir"]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if all(f["format"] in TABLE for f in files):
        import pandas as pd
        from dataset_loader import load_tabular
        frames, parts = [], []
        for f in files:
            df = load_tabular(f["path"], f["format"])
            frames.append(df)
            parts.append({"fileName": f["fileName"], "rows": len(df)})
        out = pd.concat(frames, ignore_index=True, sort=False)
        path = os.path.join(out_dir, "combined.parquet")
        out.to_parquet(path, index=False)
        print(json.dumps({"ok": True, "path": path, "format": "parquet", "rows": len(out), "columns": list(map(str, out.columns)), "parts": parts}))
        return

    import pandas as pd
    path = os.path.join(out_dir, "combined.zip")
    metas, parts, all_images = [], [], True
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for f in files:
            stem = os.path.splitext(os.path.basename(f["fileName"]))[0] or "file"
            if f["format"] in ("zip", "image-zip"):
                with zipfile.ZipFile(f["path"]) as z:
                    n = 0
                    for name in z.namelist():
                        if name.endswith("/") or name.startswith("__MACOSX/"):
                            continue
                        if os.path.basename(name).lower() == "metadata.csv":
                            m = pd.read_csv(io.BytesIO(z.read(name)))
                            base = os.path.dirname(name)
                            fcol = next((c for c in m.columns if c.lower() in ("file", "path", "audio", "filename", "file_name")), None)
                            if fcol:
                                m[fcol] = [f"{stem}/{os.path.join(base, p) if base else p}" for p in m[fcol].astype(str)]
                                m = m.rename(columns={fcol: "file"})
                            metas.append(m)
                            continue
                        out.writestr(f"{stem}/{name}", z.read(name))
                        n += 1
                        all_images &= os.path.splitext(name)[1].lower() in IMAGE_EXT
                parts.append({"fileName": f["fileName"], "rows": n})
            else:
                with open(f["path"], "rb") as fh:
                    out.writestr(os.path.basename(f["fileName"]), fh.read())
                all_images &= os.path.splitext(f["fileName"])[1].lower() in IMAGE_EXT
                parts.append({"fileName": f["fileName"], "rows": 1})
        if metas:
            out.writestr("metadata.csv", pd.concat(metas, ignore_index=True, sort=False).to_csv(index=False))
    fmt = "image-zip" if all_images and all(f["format"] in ("image-zip", "png") for f in files) else "zip"
    print(json.dumps({"ok": True, "path": path, "format": fmt, "rows": sum(p["rows"] for p in parts), "columns": [], "parts": parts}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
