#!/usr/bin/env python3
"""Retry arithmetic and Promise exports with the approved normal Wizer host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'wizer'),str(ROOT/'javascript'),str(ROOT)]
from snapshot import snapshot
from host import host,digest
from toolchain import run_guest
import wasm_tools


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'build/starlingmonkey-native-wizer')
    parser.add_argument('--splicer',type=Path,default=ROOT/'build/starlingmonkey-spike/cargo-target/wasm32-wasip1/debug/splicer.wasm')
    args=parser.parse_args()
    work=args.output_dir.resolve();work.mkdir(parents=True,exist_ok=True)
    pins=json.loads((ROOT/'javascript/checksums/starlingmonkey.json').read_text())
    engine=ROOT/'build/starlingmonkey-spike/package/lib/starlingmonkey_embedding.wasm'
    if digest(engine)!=pins['runtime']['sha256'] or digest(args.splicer)!=pins['splicer']['moduleSha256']:
        raise ValueError('runtime or splicer pin mismatch; provision the pinned spike inputs first')
    runtime,_=host()
    results={}
    for name,source in [('sync','export function add(a,b) { return a+b; }\n'),
                        ('async','export async function add(a,b) { return await Promise.resolve(a+b); }\n')]:
        directory=work/name;directory.mkdir(exist_ok=True)
        (directory/'source.js').write_text(source)
        # Bindings and assembly run in the same pinned compiler guests as before.
        run_guest([args.splicer,'splice-bindings','--input','/engine/starlingmonkey_embedding.wasm',
                   '--out-dir','/work','--wit-path','/wit/add.wit','--world-name','arithmetic'],
                  mounts=[(engine.parent,'/engine'),(directory,'/work'),(ROOT/'javascript/examples','/wit')],timeout=180)
        environment={'SOURCE_NAME':'source.js','DEBUG':'0','FEATURE_CLOCKS':'0','IMPORT_CNT':'0',
                     'EXPORT_CNT':'1','EXPORT0_NAME':'export_add','EXPORT0_ARGS':'f64,f64','EXPORT0_RET':'f64','EXPORT0_RETSIZE':'0'}
        receipt=snapshot(directory/'component.wasm',directory/'snapshot.wasm',init_func='componentize.wizer',
                         allow_wasi=True,directories=[('/',directory)],environment=environment,
                         stdin=b'--initializer-script-path /initializer.js --strip-path-prefix / source.js',timeout=180)
        (directory/'snapshot.build.json').write_text(json.dumps(receipt,indent=2)+'\n')
        check=subprocess.run([runtime,'run','-Ccache=n','-Cparallel-compilation=n','-Oopt-level=0',
                              '-Wunknown-imports-trap=y','--invoke','check_init',directory/'snapshot.wasm'],
                             text=True,capture_output=True,timeout=90,env={**os.environ,'RAYON_NUM_THREADS':'1','TOKIO_WORKER_THREADS':'1'})
        if check.returncode or check.stdout.strip()!='0':raise ValueError('check_init failed: '+check.stdout+check.stderr)
        run_guest([args.splicer,'stub-wasi','--input','/work/snapshot.wasm','--output','/work/pure.wasm',
                   '--wit-path','/wit/add.wit','--world-name','arithmetic'],
                  mounts=[(directory,'/work'),(ROOT/'javascript/examples','/wit')],timeout=180)
        component=directory/'add.component.wasm'
        subprocess.run(wasm_tools.command('component','new',directory/'pure.wasm','--adapt',
                       'wasi_snapshot_preview1='+str(wasm_tools.preview1_adapter()),'-o',component),check=True)
        subprocess.run(wasm_tools.command('validate',component),check=True)
        start=time.perf_counter()
        invoked=subprocess.run([runtime,'run','-Ccache=n','-Cparallel-compilation=n','-Oopt-level=0',
                                '--invoke','add(20,22)',component],text=True,capture_output=True,timeout=90,
                               env={**os.environ,'RAYON_NUM_THREADS':'1','TOKIO_WORKER_THREADS':'1'})
        results[name]={'snapshot':receipt,'sourceSha256':digest(directory/'source.js'),
                       'initializerSha256':digest(directory/'initializer.js'),'componentBytes':component.stat().st_size,
                       'componentSha256':digest(component),'checkInit':0,'exitCode':invoked.returncode,
                       'stdout':invoked.stdout,'stderr':invoked.stderr,'coldProcessSeconds':time.perf_counter()-start}
        (work/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        if invoked.returncode or invoked.stdout.strip()!='42':raise ValueError('export failed: '+invoked.stdout+invoked.stderr)
        print(name+': add(20,22) = 42; component '+str(component.stat().st_size)+' bytes',flush=True)

if __name__=='__main__':main()
