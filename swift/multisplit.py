#!/usr/bin/env python3
"""Derive smaller Swift LLVM guest images with pinned native Binaryen bootstrap.

The caller holds the shared compiler-memory.lock. This transforms compiler
images only; application source and target linking still execute in Wasmtime.
"""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
from runtime import ROOT, integrity, provider, sha
from split import tools, export_primary_memory


def directory():
    return Path(os.environ['WISP_SWIFT_COMPAT_CACHE']).parent/'compiler-shards'


def ensure():
    integrity();out=directory();out.mkdir(parents=True,exist_ok=True)
    source=provider()/'swift-frontend-llvm.so.wasm';receipt=out/'manifest.json'
    if receipt.exists():
        record=json.loads(receipt.read_text())
        if record.get('sourceSha256')==sha(source) and record.get('recipeSha256')==sha(__file__) and all(Path(p['path']).is_file() and sha(p['path'])==p['sha256'] for p in record['files']):return record
    if shutil.disk_usage(out).free<180*1024*1024:raise ValueError('need 180 MiB free for compiler bootstrap split')
    lock=json.loads((ROOT/'toolchain.lock.json').read_text());opt,splitter,env=tools(ROOT,lock);env['BINARYEN_CORES']='1'
    commands=[]
    argv=[str(opt),str(source),'--nm','-o',os.devnull,'--all-features','--disable-compact-imports','--disable-custom-descriptors']
    with (out/'symbols.txt').open('w') as f:result=subprocess.run(argv,stdout=f,stderr=subprocess.PIPE,text=True,env=env)
    commands.append({'argv':argv,'exitCode':result.returncode,'stderr':result.stderr,'maxChildRssKiB':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss})
    if result.returncode:raise ValueError('Binaryen symbol inventory failed: '+result.stderr)
    names=[line.strip().rsplit(' : ',1)[0] for line in (out/'symbols.txt').read_text().splitlines() if ' : ' in line]
    if len(names)<40000:raise ValueError('unexpected compiler function inventory '+str(len(names)))
    manifest=out/'split.txt';manifest.write_text('\n\n'.join('part'+str(i)+'\n'+'\n'.join(names[i::2]) for i in range(2))+'\n')
    primary=out/'backend-primary.wasm'
    argv=[str(splitter),str(source),'--multi-split','--no-validation','--manifest',str(manifest),'--output',str(primary),'--out-prefix',str(out)+'/', '--no-placeholders','--import-namespace','swift-frontend-backend-image','--all-features','--disable-gc','--disable-compact-imports','--disable-custom-descriptors']
    result=subprocess.run(argv,capture_output=True,text=True,env=env)
    commands.append({'argv':argv,'exitCode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'maxChildRssKiB':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss})
    if result.returncode:
        (out/'failed.json').write_text(json.dumps({'commands':commands},indent=2)+'\n')
        raise ValueError('Binaryen multi-split failed: '+result.stderr)
    files=[primary]
    for i in range(2):
        p=out/('part'+str(i)+'.wasm');pending=p.with_suffix('.memory.wasm');export_primary_memory(p,pending);pending.replace(p);files.append(p)
    record={'schemaVersion':1,'source':str(source),'sourceSha256':sha(source),'recipeSha256':sha(__file__),'functions':len(names),'selectionSha256':sha(manifest),'nativeBootstrapOnly':True,'nativeApplicationCompilerUsed':False,'tools':{'wasm-opt':sha(opt),'wasm-split':sha(splitter)},'commands':commands,'files':[{'path':str(p),'sha256':sha(p),'bytes':p.stat().st_size} for p in files]}
    receipt.write_text(json.dumps(record,indent=2)+'\n');return record

if __name__=='__main__':print(json.dumps(ensure()))
