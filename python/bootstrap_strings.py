#!/usr/bin/env python3
"""Bootstrap the additive pure-Python string/package runtime, never user source."""
import json,os,shutil,sys,tempfile
from pathlib import Path
import bootstrap
from scripts.common import ROOT,digest,run,tool,LOCK,MAX_COMPONENT_BYTES
from scripts.wasm import COMPONENT_HEADER,source_component,section,sections,read_uleb,uleb

def public(component):
    result=component[:8];found=0
    for kind,payload in sections(component):
        if kind==11:
            count,pos=read_uleb(payload,0);entries=[]
            for _ in range(count):
                start=pos
                if payload[pos]!=0:raise ValueError('qualified export')
                length,pos=read_uleb(payload,pos+1);name=payload[pos:pos+length].decode();pos+=length
                if payload[pos]!=5:raise ValueError('expected instance')
                _,pos=read_uleb(payload,pos+1)
                if payload[pos]!=0:raise ValueError('unexpected export type')
                pos+=1
                if name=='wisp:python-strings/api@0.1.0':entries.append(payload[start:pos]);found+=1
            payload=uleb(len(entries))+b''.join(entries)
        result+=section(kind,payload)
    if found!=1:raise ValueError('string interface missing')
    return result

def main():
    # Callers hold compiler-memory.lock for this complete bootstrap.
    installed=ROOT/'.toolchains/componentize-py';dist=ROOT/'dist/strings'
    if not installed.is_dir():raise ValueError('bootstrap componentize-py installation is missing')
    import zipfile,hashlib
    wheel=ROOT/'.downloads'/LOCK['componentize_py']['filename']
    if digest(wheel)!=LOCK['componentize_py']['sha256']:raise ValueError('bootstrap wheel checksum mismatch')
    with zipfile.ZipFile(wheel) as archive:
        for filename in ['componentize_py/componentize_py.abi3.so','componentize_py/__init__.py']:
            if digest(installed/filename)!=hashlib.sha256(archive.read(filename)).hexdigest():raise ValueError('bootstrap installed file mismatch: '+filename)
    environment=dict(os.environ,PYTHONPATH=str(installed),BINARYEN_CORES='1',RAYON_NUM_THREADS='1',TOKIO_WORKER_THREADS='1');environment.pop('VIRTUAL_ENV',None)
    inputs=[ROOT/'bootstrap_strings.py',ROOT/'runtime_strings/app.py',*sorted((ROOT/'wit_strings').rglob('*.wit')),*sorted((ROOT/'.toolchains/stdlib-pure').rglob('*.py'))]
    input_hashes={str(p.relative_to(ROOT)):digest(p) for p in inputs}
    if (dist/'manifest.json').exists():
        manifest=json.loads((dist/'manifest.json').read_text())
        if manifest['inputs']==input_hashes and all(digest(dist/name)==expected for name,expected in manifest['sha256'].items()):print('strings template ready');return
        raise ValueError('existing string template changed; preserve it and use explicit rebuild workflow')
    tools={name:tool(name) for name in ['wasm-tools','wasm-opt','wac']}
    for name,path in tools.items():
        expected='250c11762916ba733c7d22b62487580f21270ec9dde4f13460ea69d300e25406' if name=='wac' else LOCK['tools'][name]['sha256']
        if digest(path)!=expected:raise ValueError('unpinned bootstrap tool: '+name)
    with tempfile.TemporaryDirectory(prefix='.strings-bootstrap-',dir=ROOT) as directory:
        work=Path(directory);runtime=work/'runtime.wasm'
        run([sys.executable,'-c','import componentize_py; componentize_py.script()','-d',ROOT/'wit_strings','-w','python-strings','componentize','-p',ROOT/'runtime_strings','app','-o',runtime],env=environment,timeout=600)
        # Native Binaryen optimizes only the reusable bootstrap template.
        bootstrap.optimizer_environment=lambda binary:environment
        runtime.write_bytes(bootstrap.optimize_runtime(runtime.read_bytes(),work,tools['wasm-opt']))
        provider=work/'source.wasm';provider.write_bytes(source_component(b'def transform(text):\n    return text\n','transform'))
        template=work/'template.component.wasm'
        run([tools['wac'],'plug',runtime,'--plug',provider,'-o',template],env=environment)
        template.write_bytes(public(template.read_bytes()))
        run([tools['wasm-tools'],'validate',template],env=environment)
        if template.stat().st_size>MAX_COMPONENT_BYTES:
            evidence=ROOT/'dist/strings-blocked';evidence.mkdir(exist_ok=True)
            shutil.copyfile(template,evidence/'oversize-template.component.wasm')
            (evidence/'size.json').write_text(json.dumps({'bytes':template.stat().st_size,'limit':MAX_COMPONENT_BYTES,'sha256':digest(template)}))
            raise ValueError(f'string template exceeds 64 MiB: {template.stat().st_size}; retained at {evidence}')
        output=run([tool('wasmtime'),'run','-C','cache=y','-C','parallel-compilation=n','-O','opt-level=0','--invoke','transform("世界")',template],capture=True,env=environment,timeout=180).stdout.strip()
        if output!='ok("世界")':raise ValueError('string runtime regression: '+output)
        staged=work/'strings';staged.mkdir();shutil.copyfile(template,staged/template.name)
        shutil.copytree(ROOT/'.toolchains/stdlib-pure',staged/'stdlib')
        shutil.copytree(ROOT/'dist/licenses',staged/'licenses');shutil.copyfile(ROOT/'wit_strings/runtime.wit',staged/'runtime.wit')
        manifest={'schemaVersion':1,'inputs':input_hashes,'bootstrapOnlyNativeTools':{name:{'path':str(path),'sha256':digest(path)} for name,path in tools.items()},'componentizePy':LOCK['componentize_py'],'applicationCompilation':'CPython Wasm compile/exec after source-provider replacement','bytes':template.stat().st_size,'sha256':{str(p.relative_to(staged)):digest(p) for p in sorted(staged.rglob('*')) if p.is_file()}}
        (staged/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');staged.rename(dist)
    print('strings template ready',dist)
if __name__=='__main__':main()
