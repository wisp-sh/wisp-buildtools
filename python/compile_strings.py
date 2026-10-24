#!/usr/bin/env python3
"""Compile a pure Python package and string entry in guest CPython."""
import argparse,json,os,sys,tempfile,zlib,base64
from pathlib import Path
from scripts.common import ROOT,digest,guest_tools,invoke,run,MAX_COMPONENT_BYTES
# This CLI is invoked under the caller's shared memory lock.
_original_command = guest_tools.command
_original_guest_command = guest_tools.guest_command
def _low_memory(command):
    command[2:2]=['-O','opt-level=0','-C','parallel-compilation=n'];return command
guest_tools.command=lambda *a,**kw:_low_memory(_original_command(*a,**kw))
guest_tools.guest_command=lambda *a,**kw:_low_memory(_original_guest_command(*a,**kw))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('--package-root',type=Path,required=True);p.add_argument('--entry',default='transform');p.add_argument('--stdlib-root',type=Path,default=ROOT/'dist/strings/stdlib');p.add_argument('-o','--output',type=Path,required=True);p.add_argument('--timeout',type=int,default=120);args=p.parse_args()
    dist=ROOT/'dist/strings';manifest=json.loads((dist/'manifest.json').read_text())
    for name,expected in manifest['sha256'].items():
        if digest(dist/name)!=expected:raise ValueError('string runtime checksum mismatch: '+name)
    if not args.entry.isidentifier():raise ValueError('invalid entry name')
    package=args.package_root.resolve(strict=True);source=args.source.resolve(strict=True);modules={};inputs=[]
    roots=[package]+([args.stdlib_root.resolve(strict=True)] if args.stdlib_root else [])
    for path in [path for root in roots for path in sorted(root.rglob('*.py'))]:
        if path.is_symlink():raise ValueError('package source symlink not permitted')
        owner=next(root for root in roots if path.is_relative_to(root))
        rel=path.relative_to(owner);parts=list(rel.with_suffix('').parts);ispackage=parts[-1]=='__init__'
        if ispackage:parts.pop()
        if not parts:continue
        modules['.'.join(parts)]=[path.read_text(encoding='utf-8'),ispackage]
        inputs.append({'path':str(path),'sha256':digest(path)})
    # A plain Python source loader needs no host Python import or compilation.
    prefix='''import sys, importlib.util
class _WispSourceLoader:
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _wisp_modules: return None
        return importlib.util.spec_from_loader(fullname, self, is_package=_wisp_modules[fullname][1])
    def create_module(self, spec): return None
    def exec_module(self, module):
        text, package = _wisp_modules[module.__name__]
        module.__file__ = '<package>/' + module.__name__.replace('.', '/') + ('/__init__.py' if package else '.py')
        if package: module.__path__ = [module.__file__.rsplit('/', 1)[0]]
        exec(compile(text, module.__file__, 'exec', dont_inherit=True), module.__dict__)
    def get_source(self, fullname): return _wisp_modules[fullname][0]
    def is_package(self, fullname): return _wisp_modules[fullname][1]
sys.meta_path.insert(0, _WispSourceLoader())
'''
    serialized=json.dumps(modules,ensure_ascii=False,separators=(',',':')).encode()
    compressed=zlib.compress(serialized,9)
    payload=('import json, zlib, binascii\n_wisp_modules = json.loads(zlib.decompress(binascii.a2b_base64('+repr(base64.b64encode(compressed))+')))\n'+prefix+source.read_text()).encode()
    if len(payload)>1024*1024:raise ValueError('package source map exceeds 1 MiB')
    output=args.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.python-strings-',dir=output.parent) as directory:
        work=Path(directory);bundled=work/'program.py';bundled.write_bytes(payload);candidate=work/'candidate.wasm'
        run(guest_tools.guest_command(ROOT/'dist/python-packager.wasm',dist/'template.component.wasm',bundled,args.entry,candidate))
        if candidate.stat().st_size>MAX_COMPONENT_BYTES:raise ValueError('Python Component exceeds 16 MiB')
        run(guest_tools.command('validate',candidate))
        result=run([str(guest_tools.runtime()),'run','-C','cache=y','-O','opt-level=0','-C','parallel-compilation=n','--invoke','validate()',str(candidate)],capture=True,timeout=args.timeout).stdout.strip()
        if result!='ok':raise ValueError('guest package load failed: '+result)
        details={'schemaVersion':1,'source':{'path':str(source),'sha256':digest(source)},'inputs':inputs,'bundledSourceSha256':digest(bundled),'bundledBytes':len(payload),'sourceMapCompression':'zlib level 9, decompressed in guest','uncompressedSourceMapBytes':len(serialized),'templateSha256':digest(dist/'template.component.wasm'),'runtimeManifestSha256':digest(dist/'manifest.json'),'output':{'sha256':digest(candidate),'bytes':candidate.stat().st_size},'fullyWasmHostedBuild':True,'sourceCompilerWasmHosted':True,'stages':['WASI source-provider packaging','guest CPython package source compile/exec','WASI wasm-tools validation'],'nativeApplicationPythonExecuted':False,'interface':'wisp:python-strings/api@0.1.0','compilerRunnerSha256':digest(Path(__file__)),'packagerWasmSha256':digest(ROOT/'dist/python-packager.wasm')}
        os.replace(candidate,output)
    Path(str(output)+'.build.json').write_text(json.dumps(details,indent=2)+'\n');print(json.dumps(details,sort_keys=True))
if __name__=='__main__':main()
