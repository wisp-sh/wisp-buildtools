#!/usr/bin/env python3
"""Exercise the real WASI Goose frontend, C backend, initialization and failures."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
from compile import ROOT, compile_goose, runtime


def main():
    (ROOT / 'work/verification.json').unlink(missing_ok=True)
    host = runtime()
    work = ROOT / 'work/verify'
    work.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ}
    environment.setdefault('XDG_CACHE_HOME', str(Path.home() / '.cache'))
    pin = json.loads((ROOT / 'toolchain.lock.json').read_text())['goose']['revision']
    result = subprocess.run([str(runtime(frontend=True)), 'run', '-W', 'exceptions=y', str(ROOT/'dist/bin/goose.wasm'), '--version'],
                            env=environment, capture_output=True, text=True, check=True)
    assert pin[:12] in result.stdout, result.stdout
    def invoke(path, name, *args):
        return subprocess.run([str(host), 'run', '--invoke', name, str(path), *map(str,args)],
                              env=environment, capture_output=True, text=True)
    output=work/'add.wasm'
    receipt=compile_goose(ROOT/'examples/add.goose', output)
    result=invoke(output, 'add', 20, 22)
    assert result.returncode==0 and result.stdout.strip()=='42', result.stdout+result.stderr
    features=work/'features.wasm'
    compile_goose(ROOT/'examples/features.goose', features)
    for name,args,expected in [('initialized',[],1),('doubled',[21],42),('sum',[10],45),('root',[81],9)]:
        result=invoke(features,name,*args)
        assert result.returncode==0 and result.stdout.strip()==str(expected), result.stdout+result.stderr
    result=invoke(features,'quotient',1,0)
    assert result.returncode!=0 and 'division by zero' in result.stderr, result.stdout+result.stderr
    cases={
        'invalid':('export fn broken(a: i32) -> i32 { missing(a) }','unknown'),
        'dynamic':('export fn size(n: i32) -> i32 { var xs: i32[>..] = []; for i in 0..n { xs.push(i); } xs.len as i32 }','dynamic data stacks'),
        'compound':('struct Pair { a: i32, b: i32 }\nexport fn pair() -> Pair { Pair { a: 1, b: 2 } }','scalar export'),
        'fetch':('import other from "https://github.com/example/other@main";\nexport fn add(a: i32,b: i32)->i32 {a+b}','does not fetch'),
    }
    for name,(source,diagnostic) in cases.items():
        path=work/f'{name}.goose'; path.write_text(source)
        output=work/f'{name}.wasm'; output.write_bytes(b'previous-output')
        result=subprocess.run([sys.executable, str(ROOT/'compile.py'),str(path),'-o',str(output)],
                              env=environment,capture_output=True,text=True)
        assert result.returncode!=0 and diagnostic in result.stderr, result.stdout+result.stderr
        assert output.read_bytes()==b'previous-output'
        (work/f'{name}.log').write_text(result.stdout+result.stderr)
    empty=work/'empty-path'; empty.mkdir(exist_ok=True)
    portable=work/'path with spaces/add.wasm'
    subprocess.run([sys.executable,str(ROOT/'compile.py'),str(ROOT/'examples/add.goose'),'-o',str(portable)],
                   env={**environment,'PATH':str(empty),'WISP_WASMTIME':str(host)}, check=True)
    result=invoke(portable,'add',20,22)
    assert result.returncode==0 and result.stdout.strip()=='42', result.stdout+result.stderr
    report={'schema':1,'passed':True,'revision':pin,'profile':'scalar-v1','fullyWasmHostedBuild':True,
            'add':42,'localImports':True,'standardLibraryMath':True,'generics':True,'globalInitialization':1,'loopSum':45,
            'divisionByZeroRejected':True,'invalidSourceRejected':True,'dynamicMemoryRejected':True,
            'compoundApiRejected':True,'networkFetchingRejected':True,'failedBuildPreservesOutput':True,
            'emptyNativePath':True,'pathsWithSpaces':True,'receipt':receipt['outputSha256']}
    (ROOT/'work/verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
