#!/usr/bin/env python3
"""Bounded cold-runtime spike, not the JavaScript library Component compiler.

Compiler operations use the installed esbuild and wasm-tools Wasm guests.
The upstream native Wizer/Weval/Node pipeline is never invoked.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import urllib.request

JS = Path(__file__).resolve().parents[2]
ROOT = JS.parent
sys.path[:0] = [str(JS), str(ROOT)]
import wasm_tools
from typescript import bundle
from toolchain import run_guest


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def preflight(directory, growth=100*1024**2):
    directory.mkdir(parents=True, exist_ok=True)
    for cmd in (['free','-h'], ['df','-h',str(directory)], ['df','-i',str(directory)],
                ['findmnt','-T',str(directory)], ['ps','-C','cargo,rustc,wasmtime,clang,cc1,make,ninja','-o','pid,ppid,rss,comm']):
        subprocess.run(cmd, check=cmd[0] != 'ps')
    if shutil.disk_usage(directory).free < 5*1024**3 + growth:
        raise RuntimeError('need 5 GiB disk reserve plus 100 MiB spike growth')
    filesystem = subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T',str(directory)],text=True).strip()
    if filesystem in {'tmpfs','ramfs'}:
        raise RuntimeError('spike artifacts must be on disk')


def fetch(spec, path, offline):
    if not path.exists():
        if offline:
            raise RuntimeError('missing offline input: '+str(path))
        with urllib.request.urlopen(spec['url'],timeout=60) as src:
            data=src.read(spec['bytes']+1)
        if len(data)!=spec['bytes'] or hashlib.sha256(data).hexdigest()!=spec['sha256']:
            raise RuntimeError('download checksum mismatch')
        path.write_bytes(data)
    if path.stat().st_size!=spec['bytes'] or sha(path)!=spec['sha256']:
        raise RuntimeError('cached input checksum mismatch: '+str(path))


def unpack(archive, destination, *, selected=None):
    with tarfile.open(archive) as tar:
        for member in tar:
            parts=Path(member.name).parts
            if not parts or member.name.startswith('/') or '..' in parts:
                raise RuntimeError('unsafe source archive')
            if not member.isfile():
                if member.isdir():continue
                raise RuntimeError('unsupported source entry')
            relative=Path(*parts[1:])
            if selected and relative.as_posix() not in selected:continue
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


def recipes(args, directory):
    outputs=[]
    yaml=args.recipes/'packages/yaml'; ym=directory/'yaml'
    spec=json.loads((yaml/'source.json').read_text())
    archive=args.recipe_cache/'.cache/round2/yaml/source.tar.gz'
    if sha(archive)!=spec['sha256']:raise RuntimeError('YAML archive checksum mismatch')
    unpack(archive,ym/'upstream')
    for name in ['entry.ts','browser-buffer.ts','browser-process.ts']:
        shutil.copyfile(yaml/name,ym/name if name=='entry.ts' else ym/'upstream/src'/name)
    substitutions={'src/parse/parser.ts':('../browser-process','node:process'),
        'src/compose/composer.ts':('../browser-process','node:process'),
        'src/log.ts':('./browser-process','node:process'),
        'src/schema/yaml-1.1/binary.ts':('../../browser-buffer','node:buffer')}
    for name,(replacement,original) in substitutions.items():
        path=ym/'upstream'/name;path.write_text(path.read_text().replace("'"+original+"'","'"+replacement+"'"))
    (ym/'check.ts').write_text('''import { parseYaml, parseDocuments, stringifyJson } from './entry.ts';
function equal(actual, expected) { if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(JSON.stringify({actual, expected})); }
equal(JSON.parse(parseYaml('name: 世界\\nitems: [1, true, null]\\n')), {ok:true,value:{name:'世界',items:[1,true,null]}});
equal(JSON.parse(parseYaml('base: &x {a: 1}\\ncopy: *x')), {ok:true,value:{base:{a:1},copy:{a:1}}});
equal(JSON.parse(parseDocuments('---\\na: 1\\n---\\n- 二\\n')), {ok:true,value:[{a:1},['二']]});
equal(JSON.parse(stringifyJson('{"name":"世界"}')), {ok:true,value:'name: 世界\\n'});
for (const result of [parseYaml('a: ['), stringifyJson('invalid'), parseYaml('a: 1\\na: 2')]) { const value=JSON.parse(result); if(value.ok !== false || !value.error) throw new Error(result); }
console.log('yaml: 7/7 recipe checks passed');
''')
    outputs.append(('yaml',ym,{'sourceSha256':spec['sha256'],
        'remainingWorkarounds':['browser-buffer.ts: Buffer=null; StarlingMonkey has no Node Buffer',
        'browser-process.ts: env and emitWarning; StarlingMonkey has no Node process',
        'browser-target.patch: four Node import substitutions retained'],
        'removedWorkarounds':[], 'checks':7}))
    markdown=args.recipes/'packages/markdown-it';md=directory/'markdown-it';md.mkdir(exist_ok=True)
    spec=json.loads((markdown/'source.json').read_text())
    if sha(markdown/'package-lock.json')!=spec['lockSha256']:raise RuntimeError('markdown lock mismatch')
    lock=json.loads((markdown/'package-lock.json').read_text())
    for name,record in lock['packages'].items():
        if not name:continue
        algorithm,expected=record['integrity'].split('-',1)
        if algorithm!='sha512' or not name.startswith('node_modules/') or '..' in Path(name).parts:
            raise RuntimeError('invalid npm package pin')
        archive=args.recipe_cache/'.cache/npm'/(base64.b64decode(expected).hex()+'.tgz')
        if base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()!=expected:
            raise RuntimeError('npm archive mismatch')
        unpack(archive,md/name)
    shutil.copyfile(markdown/'entry.ts',md/'entry.ts')
    (md/'check.ts').write_text('''import { render, renderInline } from './entry.ts';
function equal(actual, expected) { if(actual!==expected) throw new Error(JSON.stringify({actual,expected})); }
equal(render('# Hello\\n'), '<h1>Hello</h1>\\n');
equal(render('Hello **世界** 🦀\\n'), '<p>Hello <strong>世界</strong> 🦀</p>\\n');
equal(renderInline('*hello* `code`'), '<em>hello</em> <code>code</code>');
equal(render('<script>alert(1)</script>\\n'), '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>\\n');
equal(renderInline('https://example.com'), '<a href="https://example.com">https://example.com</a>');
equal(render('| a | b |\\n|---|---|\\n| 1 | 2 |\\n'), '<table>\\n<thead>\\n<tr>\\n<th>a</th>\\n<th>b</th>\\n</tr>\\n</thead>\\n<tbody>\\n<tr>\\n<td>1</td>\\n<td>2</td>\\n</tr>\\n</tbody>\\n</table>\\n');
console.log('markdown-it: 6/6 recipe checks passed');
''')
    outputs.append(('markdown-it',md,{'lockSha256':spec['lockSha256'],'remainingWorkarounds':[],
        'removedWorkarounds':['QuickJS deferred-initialization compiler patch not used',
                              'QuickJS guest-inventory compiler patch not used'], 'checks':6}))
    return outputs


def probe_exports(guest, work, runtime):
    guest = guest.resolve(strict=True)
    if guest.read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError('splicer must be a WASI Core Wasm guest')
    mounts = [(work, '/work'), (JS / 'examples', '/wit')]
    run_guest([guest, 'splice-bindings', '--input', '/work/package/lib/starlingmonkey_embedding.wasm',
               '--out-dir', '/work/arithmetic', '--wit-path', '/wit/add.wit', '--world-name', 'arithmetic'],
              mounts=mounts, timeout=180)
    run_guest([guest, 'stub-wasi', '--input', '/work/arithmetic/component.wasm',
               '--output', '/work/arithmetic/pure.wasm', '--wit-path', '/wit/add.wit',
               '--world-name', 'arithmetic'], mounts=mounts, timeout=180)
    results = {}
    for name, source in [('stubbed', 'pure.wasm'), ('unstubbed', 'component.wasm')]:
        output = work / 'arithmetic' / (name + '-no-snapshot.component.wasm')
        subprocess.run(wasm_tools.command('component', 'new', work/'arithmetic'/source,
            '--adapt', 'wasi_snapshot_preview1='+str(wasm_tools.preview1_adapter()), '-o', output), check=True)
        subprocess.run(wasm_tools.command('validate', output), check=True)
        command = [str(runtime), 'run', '-Ccache=n', '-Oopt-level=0', '-Cparallel-compilation=n']
        if name == 'unstubbed': command.append('-Shttp')
        command += ['--invoke', 'add(20,22)', str(output)]
        start = time.perf_counter()
        result = subprocess.run(command, text=True, capture_output=True, timeout=90,
            env={**os.environ, 'RAYON_NUM_THREADS':'1', 'TOKIO_WORKER_THREADS':'1'})
        results[name] = {'componentBytes':output.stat().st_size, 'componentSha256':sha(output),
                         'seconds':time.perf_counter()-start, 'exitCode':result.returncode,
                         'stdout':result.stdout, 'stderr':result.stderr, 'snapshot':False}
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'build/starlingmonkey-spike')
    parser.add_argument('--runtime',type=Path,required=True,help='pinned Wasmtime 50 CLI with wasi-http enabled')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--splicer',type=Path,help='optional WASIp1 splicer CLI guest for no-snapshot authored-WIT probes')
    parser.add_argument('--recipes',type=Path)
    parser.add_argument('--recipe-cache',type=Path)
    args=parser.parse_args()
    work=args.output_dir.resolve();preflight(work)
    pins=json.loads((JS/'checksums/starlingmonkey.json').read_text())
    if sha(args.runtime)!=pins['benchmarkHost']['sha256']:
        raise RuntimeError('benchmark requires the pinned official Wasmtime 50.0.0 binary')
    archive=work/'componentize-js-0.23.0.tgz';fetch(pins['release'],archive,args.offline)
    unpack(archive,work/'package',selected={'lib/starlingmonkey_embedding.wasm','LICENSE'})
    runtime=work/'package/lib/starlingmonkey_embedding.wasm'
    if sha(runtime)!=pins['runtime']['sha256']:raise RuntimeError('runtime pin mismatch')
    component=work/'cold-runtime.component.wasm'
    subprocess.run(wasm_tools.command('component','new',runtime,'--adapt',
        'wasi_snapshot_preview1='+str(wasm_tools.preview1_adapter()),'-o',component),check=True)
    subprocess.run(wasm_tools.command('validate',component),check=True)
    command=[str(args.runtime.resolve()),'run','-Ccache=n','-Oopt-level=0',
             '-Cparallel-compilation=n','-Shttp']
    report={'decision':'no-go','scope':'cold CLI runtime only; authored-WIT library adapter not implemented',
            'runtimeBytes':runtime.stat().st_size,'runtimeSha256':sha(runtime),
            'componentBytes':component.stat().st_size,'componentSha256':sha(component),
            'snapshot':False,'sourceCompilerWasmHosted':True,
            'hostSha256':sha(args.runtime),'flags':command[2:],'timings':[], 'recipes':{}}
    def run(arguments):
        start=time.perf_counter()
        result=subprocess.run([*command,*map(str,arguments)],text=True,capture_output=True,timeout=90,
            env={**os.environ,'RAYON_NUM_THREADS':'1','TOKIO_WORKER_THREADS':'1'})
        detail={'seconds':time.perf_counter()-start,'exitCode':result.returncode,
                'stdout':result.stdout,'stderr':result.stderr}
        if result.returncode:raise RuntimeError(json.dumps(detail))
        return detail
    script='console.log(JSON.stringify({URL:typeof URL,TextEncoder:typeof TextEncoder,fetch:typeof fetch,crypto:typeof crypto,Buffer:typeof Buffer,process:typeof process}))'
    for _ in range(3):report['timings'].append(run([component,'-e',script]))
    report['medianColdProcessSeconds']=statistics.median(r['seconds'] for r in report['timings'])
    report['asyncJavaScript']=run([component,'-e',
        'async function add(a,b) { return await Promise.resolve(a+b); } add(20,22).then(v=>{if(v!==42)throw new Error(String(v));console.log("async Promise settled: 42")})'])
    if args.recipes:
        if not args.recipe_cache:parser.error('--recipes needs --recipe-cache')
        for name,project,detail in recipes(args,work):
            result=bundle(project/'check.ts',project,work/('bundle-'+name),
                esbuild_wasm=JS/'.toolchains/wasm/esbuild.wasm',
                wasmtime=wasm_tools.runtime(),timeout=90)
            output=project/'check.mjs';output.write_text(result['js'])
            detail['bundleInputs']=result['inputs']
            detail['execution']=run(['--dir',str(project)+'::/project',component,'/project/check.mjs'])
            report['recipes'][name]=detail
    report['apiProbe']=run(['--dir',str(Path(__file__).resolve().parent)+'::/probe',
                            component,'/probe/api-check.mjs'])
    if args.splicer:
        report['authoredWitNoSnapshot']=probe_exports(args.splicer,work,args.runtime.resolve())
    report['maxChildrenRssKiB']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    (work/'spike-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='recipes'},indent=2))
    for name,detail in report['recipes'].items():print(name,detail['execution']['stdout'].strip())


if __name__=='__main__':main()
