#!/usr/bin/env python3
"""Warm one pinned compiler module in isolation. Caller holds the memory lock."""
import json
from pathlib import Path
import resource
import sys
from runtime import ROOT, binding, compilation_engine, integrity, provider, sha, load_module, prepared_shards
from compat import image
integrity()
name=sys.argv[1]
allowed={n+s: (ROOT/'dist/bin'/(n+s+'.wasm') if s!='-llvm.so' else provider()/(n+s+'.wasm'))
         for n in ['swift-frontend','wasm-ld'] for s in ['-primary','-llvm.so','']}
if name.startswith('backend-') or name.startswith('part'):
    from multisplit import ensure
    for entry in ensure()['files']: allowed[Path(entry['path']).stem] = Path(entry['path'])
if ':' in name:
    guest, member = name.split(':', 1)
    if guest not in {'swift-frontend', 'wasm-ld'}:
        raise SystemExit('unknown compiler guest')
    record = prepared_shards(guest)
    for entry in [record['state'], *record['shards']]:
        allowed[guest + ':' + Path(entry['path']).stem] = Path(entry['path'])
if name not in allowed:raise SystemExit('unknown compiler image')
source=allowed[name];wasmtime=binding();engine,backend=compilation_engine(wasmtime)
selected=image(source) if backend in {'winch-compatible','winch-sharded'} else source
module=load_module(wasmtime,engine,selected)
print(json.dumps({'image':name,'source':str(source),'sourceSha256':sha(source),'selected':str(selected),'sha256':sha(selected),'backend':backend,'maxRssKiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'cached':True}))
