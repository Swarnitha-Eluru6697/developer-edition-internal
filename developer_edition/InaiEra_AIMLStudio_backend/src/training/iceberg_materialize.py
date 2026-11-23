#!/usr/bin/env python3
"""Resolve an exact Apache Iceberg snapshot with PyIceberg and materialize it.
The catalog connection is configured by environment; the request supplies only
logical table identity and optional immutable snapshot id.
"""
import json,sys,os,hashlib

def main():
    req=json.loads(sys.stdin.read() or '{}')
    try:
        from pyiceberg.catalog import load_catalog
        table_name=req.get('table')
        if not table_name: raise ValueError('table is required (namespace.table)')
        # 'lakehouse' (or none) is the default ICEBERG_* connection; other catalogs come from
        # ICEBERG_EXTRA_CATALOGS_JSON = {"<name>": {<pyiceberg properties incl. type, uri, warehouse>}}.
        key=req.get('catalog') or 'lakehouse'
        extras=json.loads(os.getenv('ICEBERG_EXTRA_CATALOGS_JSON') or '{}')
        if key!='lakehouse' and key not in extras:
            raise ValueError(f"Iceberg catalog '{key}' is not configured — known: {', '.join(['lakehouse',*extras])}")
        name=key if key in extras else os.getenv('ICEBERG_CATALOG_NAME','mlstudio')
        props={}
        if os.getenv('ICEBERG_CATALOG_URI'): props['uri']=os.environ['ICEBERG_CATALOG_URI']
        if os.getenv('ICEBERG_WAREHOUSE'): props['warehouse']=os.environ['ICEBERG_WAREHOUSE']
        if os.getenv('ICEBERG_CATALOG_TYPE'): props['type']=os.environ['ICEBERG_CATALOG_TYPE']
        if os.getenv('ICEBERG_CATALOG_PROPERTIES_JSON'):
            props.update(json.loads(os.environ['ICEBERG_CATALOG_PROPERTIES_JSON']))
        if key in extras:
            props=dict(extras[key]); name=props.pop('name',key)  # 'name': the catalog name its tables are filed under
        # Object-store keys typed into the UI for this one pull (arrive on stdin, never stored).
        creds=req.get('credentials') or {}
        if creds:
            props['s3.access-key-id']=creds['accessKeyId']; props['s3.secret-access-key']=creds['secretAccessKey']
            if creds.get('sessionToken'): props['s3.session-token']=creds['sessionToken']
        elif str(props.get('warehouse','')).startswith('s3://') and 's3.access-key-id' not in props:
            raise ValueError(f"the '{key}' catalog needs storage credentials — enter them when pulling the snapshot")
        if not props or any('<' in str(v) for v in props.values()):
            raise ValueError(f"the Iceberg catalog connection for '{key}' is not configured — fill in its ICEBERG_* settings in the backend .env and restart it")
        catalog=load_catalog(name,**props); table=catalog.load_table(table_name)
        requested=req.get('snapshotId'); snapshot_id=int(requested) if requested is not None else (table.current_snapshot().snapshot_id if table.current_snapshot() else None)
        if snapshot_id is None: raise ValueError('Iceberg table has no current snapshot')
        valid={s.snapshot_id for s in table.metadata.snapshots}
        if snapshot_id not in valid: raise ValueError(f'Iceberg snapshot {snapshot_id} not found for {table_name}')
        scan=table.scan(snapshot_id=snapshot_id)
        arrow=scan.to_arrow(); out=req.get('outputPath')
        if not out: raise ValueError('outputPath is required')
        import pyarrow.parquet as pq
        pq.write_table(arrow,out,compression='snappy')
        h=hashlib.sha256()
        with open(out,'rb') as f:
            for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
        print(json.dumps({'ok':True,'snapshotId':str(snapshot_id),'rowCount':arrow.num_rows,'columnNames':arrow.column_names,'checksum':h.hexdigest(),'table':table_name,'catalog':key,'outputPath':out}))
    except Exception as e:
        msg=f'{type(e).__name__}: {e}'
        if any(s in msg for s in ('ACCESS_DENIED','AccessDenied','InvalidAccessKeyId','SignatureDoesNotMatch','403')):
            msg=f'the storage rejected these credentials — check the access key, secret and that the key can read this bucket ({msg})'
        print(json.dumps({'ok':False,'error':msg}))
if __name__=='__main__': main()
