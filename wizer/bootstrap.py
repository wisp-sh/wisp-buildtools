#!/usr/bin/env python3
"""Prepare the Wizer host adapter. Native execution hosts remain external."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import tempfile
from host import digest,host,HERE

def recipe():
    return {name:digest(HERE/name) for name in ['bootstrap.py','host.py','snapshot.py','toolchain.lock.json']}

def check():
    lock=json.loads((HERE/'toolchain.lock.json').read_text())
    manifest=json.loads((HERE/'dist/manifest.json').read_text())
    if manifest!={'schemaVersion':1,'kind':'native-host-adapter','version':lock['version'],
                  'hostPins':lock,'recipe':recipe(),'nativeSnapshotHostApproved':True}:
        raise ValueError('Wizer adapter recipe or pins changed; rerun bootstrap --rebuild')
    if list((HERE/'dist').iterdir())!=[HERE/'dist/manifest.json']:
        raise ValueError('unexpected files in Wizer adapter package')
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--rebuild',action='store_true')
    args=parser.parse_args()
    _,identity=host()
    if not args.check and (args.rebuild or not (HERE/'dist').exists()):
        dist=HERE/'dist';dist.mkdir(exist_ok=True)
        lock=json.loads((HERE/'toolchain.lock.json').read_text())
        manifest={'schemaVersion':1,'kind':'native-host-adapter','version':lock['version'],
                  'hostPins':lock,'recipe':recipe(),'nativeSnapshotHostApproved':True}
        with tempfile.NamedTemporaryFile(dir=dist,mode='w',delete=False) as output:
            path=Path(output.name);output.write(json.dumps(manifest,indent=2)+'\n')
        path.replace(dist/'manifest.json')
    check()
    print('Wizer 50.0.0 host adapter ready (host and recipe verified)')
    print(json.dumps(identity,sort_keys=True))

if __name__=='__main__':main()
