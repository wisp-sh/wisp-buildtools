#!/usr/bin/env python3
"""Snapshot Wasm with normal Wizer in the explicitly approved native host."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import time
from host import host,digest,HERE
from bootstrap import check


def snapshot(source,output,*,init_func='wizer.initialize',directories=(),environment=None,
             stdin=b'',allow_wasi=False,keep_init=False,renames=(),timeout=120,fuel=5_000_000_000,runtime=None):
    check()
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    if source==output:raise ValueError('snapshot input and output must differ')
    with source.open('rb') as stream:header=stream.read(8)
    if header!=b'\0asm\x01\0\0\0':raise ValueError('snapshot input must be a Core Wasm module')
    if not 1<=timeout<=600 or not 1<=fuel<=100_000_000_000:raise ValueError('timeout or fuel outside supported bounds')
    if (directories or environment) and not allow_wasi:raise ValueError('preopens and environment require --allow-wasi')
    executable,identity=host(runtime)
    source_hash=digest(source)
    mounts=[]
    for guest,path in directories:
        path=Path(path).resolve(strict=True)
        if not path.is_dir() or not Path(guest).is_absolute() or '\0' in guest or '::' in str(path)+guest:
            raise ValueError('invalid explicit preopen')
        mounts.append((guest,path))
    values=environment or {}
    for name,value in values.items():
        if not name or '=' in name or '\0' in name+value:raise ValueError('invalid guest environment')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wizer-',dir=output.parent) as temporary:
        work=Path(temporary);candidate=work/'snapshot.wasm'
        staged_input=work/'input.wasm';shutil.copyfile(source,staged_input)
        if digest(staged_input)!=source_hash:raise ValueError('snapshot input changed while staging')
        command=[str(executable),'wizer','-Ccache=n','-Cparallel-compilation=n','-Oopt-level=0',
                 '-Wmax-memory-size=1073741824','-Wmax-wasm-stack=33554432','-Wasync-stack-size=67108864',
                 '-Wunknown-imports-trap=y',f'-Wfuel={fuel}',f'-Wtimeout={timeout}s',
                 '-Scli='+('y' if allow_wasi else 'n'),'-Sinherit-env=n','-Sinherit-network=n',
                 '-Sallow-ip-name-lookup=n','-Stcp=n','-Sudp=n','--init-func',init_func,'-o',str(candidate)]
        if keep_init:command.append('--keep-init-func=true')
        for guest,path in mounts:command+=['--dir',str(path)+'::'+guest]
        for name,value in sorted(values.items()):command+=['--env',name+'='+value]
        for dst,src in renames:command+=['--rename-func',dst+'='+src]
        command.append(staged_input.name)
        start=time.perf_counter()
        result=subprocess.run(command,input=stdin,capture_output=True,timeout=timeout+30,cwd=work,
            env={'RAYON_NUM_THREADS':'1','TOKIO_WORKER_THREADS':'1','TMPDIR':str(work)})
        if result.returncode:
            raise ValueError('Wizer initialization failed:\n'+(result.stderr+result.stdout).decode('utf-8','replace'))
        with candidate.open('rb') as stream:header=stream.read(8)
        if header!=b'\0asm\x01\0\0\0':raise ValueError('Wizer did not produce Core Wasm')
        if digest(source)!=source_hash:raise ValueError('snapshot input changed during initialization')
        receipt={'schemaVersion':1,'tool':'wizer@50.0.0','nativeSnapshotHostApproved':True,
                 'snapshotExecution':'native Wizer host','applicationInitialization':'Wasm inside Wasmtime',
                 'fullyWasmHostedBuild':False,'host':identity,'initFunc':init_func,
                 'input':{'sha256':source_hash,'bytes':source.stat().st_size},
                 'output':{'sha256':digest(candidate),'bytes':candidate.stat().st_size},
                 'preopens':[{'guest':guest,'host':str(path)} for guest,path in mounts],
                 'environment':{name:hashlib.sha256(value.encode()).hexdigest() for name,value in sorted(values.items())},
                 'stdinSha256':hashlib.sha256(stdin).hexdigest(),'keepInit':keep_init,'renames':list(renames),
                 'limits':{'seconds':timeout,'fuel':fuel,'memoryBytes':1073741824,'network':False},
                 'milliseconds':round((time.perf_counter()-start)*1000,3),
                 'recipe':{name:digest(HERE/name) for name in ['host.py','snapshot.py','toolchain.lock.json']}}
        candidate.replace(output)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('-o','--output',type=Path,required=True)
    parser.add_argument('-f','--init-func',default='wizer.initialize')
    parser.add_argument('--allow-wasi',action='store_true')
    parser.add_argument('--mapdir',action='append',default=[],metavar='GUEST::HOST')
    parser.add_argument('--env',action='append',default=[],metavar='NAME=VALUE')
    parser.add_argument('--stdin',type=Path)
    parser.add_argument('--receipt',type=Path)
    parser.add_argument('--keep-init',action='store_true')
    parser.add_argument('--timeout',type=int,default=120)
    parser.add_argument('--fuel',type=int,default=5_000_000_000)
    args=parser.parse_args()
    receipt_path=args.receipt or Path(str(args.output)+'.build.json')
    if receipt_path.resolve() in {args.source.resolve(),args.output.resolve()}:parser.error('receipt must differ from input and output')
    maps=[];environment={}
    for pair in args.mapdir:
        guest,separator,path=pair.partition('::')
        if not separator:parser.error('--mapdir needs GUEST::HOST')
        maps.append((guest,path))
    for pair in args.env:
        name,separator,value=pair.partition('=')
        if not separator:parser.error('--env needs NAME=VALUE')
        environment[name]=value
    for target in [args.output,receipt_path]:
        if target.exists() or target.is_symlink():parser.error('output or receipt already exists; choose fresh paths')
        target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wizer-publish-',dir=args.output.parent) as temporary:
        staged=Path(temporary)/'snapshot.wasm'
        receipt=snapshot(args.source,staged,init_func=args.init_func,directories=maps,
                         environment=environment,stdin=args.stdin.read_bytes() if args.stdin else b'',
                         allow_wasi=args.allow_wasi,keep_init=args.keep_init,timeout=args.timeout,fuel=args.fuel)
        with tempfile.NamedTemporaryFile(dir=receipt_path.parent,mode='w',delete=False) as stream:
            staged_receipt=Path(stream.name)
            stream.write(json.dumps(receipt,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
        published=False
        try:
            os.link(staged_receipt,receipt_path);published=True
            os.link(staged,args.output)
        except OSError:
            if published:receipt_path.unlink(missing_ok=True)
            raise
        finally:staged_receipt.unlink(missing_ok=True)
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':main()
