#!/usr/bin/env python3
"""Verify Wizer snapshots using actual WASI tool execution and fresh output instances."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(HERE))
import wasm_tools
from snapshot import snapshot as make_snapshot
from host import host


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'build/wizer/verify')
    args=parser.parse_args()
    output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=True)
    checks=[]
    runtime,_=host()
    env={**os.environ,'RAYON_NUM_THREADS':'1','WIZER_SHOULD_NOT_LEAK':'secret-sentinel'}
    def run(argv,*,success=True):
        result=subprocess.run(list(map(str,argv)),text=True,capture_output=True,timeout=120,env=env)
        if success and result.returncode:raise AssertionError(result.stderr+result.stdout)
        return result
    def module(name,wat):
        source=output/(name+'.wat');source.write_text(wat)
        compiled=output/(name+'.wasm')
        run(wasm_tools.command('parse',source,'-o',compiled))
        return compiled
    def snapshot(name,wat,options=(),*,success=True):
        source=module(name,wat);dest=output/(name+'.snapshot.wasm')
        if dest.exists():dest.unlink()
        start=time.perf_counter()
        values=list(options);wasi=False;fuel=5_000_000_000;directories=[];environment={}
        while values:
            option=values.pop(0)
            if option=='--allow-wasi':wasi=True
            elif option=='--fuel':fuel=int(values.pop(0))
            elif option=='--mapdir':
                guest,path=values.pop(0).split('::',1);directories.append((guest,path))
            elif option=='--env':
                name,value=values.pop(0).split('=',1);environment[name]=value
        try:
            receipt=make_snapshot(source,dest,allow_wasi=wasi,fuel=fuel,directories=directories,environment=environment)
        except ValueError as error:
            if success:raise
            assert not dest.exists()
            return subprocess.CompletedProcess([],1,'',str(error))
        if not success:raise AssertionError('invalid initializer accepted')
        assert receipt['nativeSnapshotHostApproved'] and receipt['fullyWasmHostedBuild'] is False
        if not success:
            assert result.returncode and not dest.exists(),result
            return result
        run(wasm_tools.command('validate',dest))
        result=run([runtime,'run','-Ccache=n','-Oopt-level=0','--invoke','get',dest])
        checks.append({'name':name,'passed':True,'bytes':dest.stat().st_size,'seconds':time.perf_counter()-start,'result':result.stdout.strip()})
        return result.stdout.strip(),dest
    value,dest=snapshot('memory-global','''(module
      (memory 1) (global $g (mut i32) (i32.const 0))
      (func (export "wizer.initialize")
        i32.const 1 memory.grow drop
        i32.const 65536 i32.const 41 i32.store
        i32.const 1 global.set $g)
      (func (export "get") (result i32) i32.const 65536 i32.load global.get $g i32.add))''')
    assert value=='42',value
    rejected=run([runtime,'run','-Ccache=n','-Oopt-level=0','--invoke','wizer.initialize',dest],success=False)
    assert rejected.returncode,'initializer must be removed'
    checks.append({'name':'initializer removed','passed':True})
    value,_=snapshot('reactor','''(module (global $g (mut i32) (i32.const 0))
      (func (export "_initialize") global.get $g i32.const 1 i32.add global.set $g)
      (func (export "wizer.initialize") global.get $g i32.const 1 i32.ne if unreachable end
        i32.const 42 global.set $g)
      (func (export "get") (result i32) global.get $g))''')
    assert value=='42',value
    value,_=snapshot('simd','''(module (global $v (mut v128) (v128.const i32x4 0 0 0 0))
      (func (export "wizer.initialize") i32.const 42 i32x4.splat global.set $v)
      (func (export "get") (result i32) global.get $v i32x4.extract_lane 0))''')
    assert value=='42',value
    snapshot('trap','(module (func (export "wizer.initialize") unreachable))',success=False)
    checks.append({'name':'initializer traps reject publication','passed':True})
    result=snapshot('fuel','(module (func (export "wizer.initialize") (loop $loop br $loop)))',['--fuel','1000'],success=False)
    assert 'fuel' in result.stderr.lower(),result.stderr
    checks.append({'name':'fuel bounds initialization','passed':True})
    environ='''(module (import "wasi_snapshot_preview1" "environ_sizes_get" (func $env (param i32 i32) (result i32)))
      (memory (export "memory") 1)
      (func (export "wizer.initialize") i32.const 0 i32.const 4 call $env if unreachable end)
      (func (export "get") (result i32) i32.const 0 i32.load))'''
    value,_=snapshot('no-inherited-env',environ,['--allow-wasi']);assert value=='0',value
    value,_=snapshot('explicit-env',environ,['--allow-wasi','--env','EXPLICIT=1']);assert value=='1',value
    snapshot('disabled-wasi',environ,success=False)
    checks.append({'name':'WASI disabled by default','passed':True})
    file_wat='''(module
      (import "wasi_snapshot_preview1" "path_open" (func $open (param i32 i32 i32 i32 i32 i64 i64 i32 i32) (result i32)))
      (import "wasi_snapshot_preview1" "fd_read" (func $read (param i32 i32 i32 i32) (result i32)))
      (memory (export "memory") 1) (data (i32.const 0) "message.txt")
      (func (export "wizer.initialize")
        i32.const 3 i32.const 0 i32.const 0 i32.const 11 i32.const 0 i64.const 2 i64.const 0 i32.const 0 i32.const 32 call $open if unreachable end
        i32.const 40 i32.const 64 i32.store i32.const 44 i32.const 1 i32.store
        i32.const 32 i32.load i32.const 40 i32.const 1 i32.const 48 call $read if unreachable end)
      (func (export "get") (result i32) i32.const 64 i32.load8_u))'''
    (output/'message.txt').write_bytes(b'*')
    value,_=snapshot('explicit-directory',file_wat,['--allow-wasi','--mapdir','/inputs::'+str(output)]);assert value=='42',value
    snapshot('no-preopens',file_wat,['--allow-wasi'],success=False)
    checks.append({'name':'no implicit filesystem preopens','passed':True})
    fake=output/'untrusted-host'
    marker=output/'untrusted-host-ran'
    marker.unlink(missing_ok=True)
    fake.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\n')
    fake.chmod(0o755)
    try:host(fake)
    except ValueError:pass
    else:raise AssertionError('unpinned host accepted')
    assert not marker.exists(),'unpinned host was executed before verification'
    checks.append({'name':'unrecognized native hosts rejected before execution','passed':True})
    native_input=output/'native-input';native_input.write_bytes(b'\x7fELF-not-Wasm')
    try:make_snapshot(native_input,output/'native-input.snapshot.wasm')
    except ValueError as error:assert 'Core Wasm' in str(error)
    else:raise AssertionError('native application input accepted')
    checks.append({'name':'native application inputs rejected','passed':True})
    sentinel=output/'preserved.snapshot.wasm';sentinel.write_bytes(b'previous-output')
    try:make_snapshot(module('preserve-trap','(module (func (export "wizer.initialize") unreachable))'),sentinel)
    except ValueError:pass
    else:raise AssertionError('trapping initializer accepted')
    assert sentinel.read_bytes()==b'previous-output'
    checks.append({'name':'failed initialization preserves previous output','passed':True})
    report={'passed':True,'checks':checks,'compilerExecution':'Approved normal native Wizer host; application initialization is Wasm'}
    (output/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"Wizer: {len(checks)}/{len(checks)} checks passed")

if __name__=='__main__':main()
