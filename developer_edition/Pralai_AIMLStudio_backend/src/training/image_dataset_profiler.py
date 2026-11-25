#!/usr/bin/env python3
import json,sys
from image_dataset import inspect_image_zip
try:
    req=json.loads(sys.stdin.read() or '{}')
    print(json.dumps(inspect_image_zip(req['path'])))
except Exception as e:
    print(json.dumps({'ok':False,'error':f'{type(e).__name__}: {e}'}))
