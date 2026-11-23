#!/usr/bin/env python3
"""Real image/fileset ingestion for CNN training.

Accepted physical contract: a ZIP archive containing images arranged as
<class-name>/<image-file> (nested folders before the class folder are allowed).
No label fabrication: the immediate parent directory is the class label.
"""
from __future__ import annotations
import os, zipfile, tempfile, shutil
from pathlib import Path

IMAGE_EXTS={'.jpg','.jpeg','.png','.bmp','.gif','.webp','.tif','.tiff'}

class ImageDatasetError(Exception): pass

def inspect_image_zip(zip_path: str):
    if not zipfile.is_zipfile(zip_path):
        raise ImageDatasetError('image dataset must be a valid ZIP archive')
    classes={}; total=0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            p=Path(info.filename)
            if p.suffix.lower() not in IMAGE_EXTS: continue
            if len(p.parts)<2: continue
            label=p.parent.name
            classes[label]=classes.get(label,0)+1; total+=1
    if total<10: raise ImageDatasetError(f'image ZIP contains only {total} usable labeled image(s); at least 10 are required')
    if len(classes)<2: raise ImageDatasetError('image ZIP must contain at least two class directories')
    return {'ok':True,'rowCount':total,'classes':classes,'classCount':len(classes),'columnNames':['image','label'],'columns':[{'name':'image','kind':'image','missingPct':0},{'name':'label','kind':'categorical','missingPct':0,'cardinality':len(classes),'allValueCounts':classes}]}

def extract_image_zip(zip_path: str):
    info=inspect_image_zip(zip_path)
    root=tempfile.mkdtemp(prefix='mlstudio-images-')
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            if member.is_dir(): continue
            p=Path(member.filename)
            if p.suffix.lower() not in IMAGE_EXTS or len(p.parts)<2: continue
            label=p.parent.name
            out=Path(root)/label/p.name
            out.parent.mkdir(parents=True,exist_ok=True)
            with z.open(member) as src, open(out,'wb') as dst: shutil.copyfileobj(src,dst)
    return root, info
