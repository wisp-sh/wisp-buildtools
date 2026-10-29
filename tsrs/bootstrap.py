#!/usr/bin/env python3
"""Install the pinned tsrs WASIp1 compiler, or bootstrap it from pinned Rust sources."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from cwasm import digest


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def recipe():
    return {name: digest(HERE / name) for name in ['source.json', 'toolchain.lock.json', 'bootstrap.py']}


def check():
    manifest = json.loads((HERE / 'dist/manifest.json').read_text())
    lock = json.loads((HERE / 'toolchain.lock.json').read_text())
    source = json.loads((HERE / 'source.json').read_text())
    if (manifest.get('version') != lock['version']
            or manifest.get('typescriptVersion') != lock['typescriptVersion']
            or manifest.get('source') != source
            or manifest.get('compilerHost') != 'wasm32-wasip1'
            or manifest.get('applicationCompilerWasmHosted') is not True):
        raise ValueError('tsrs installed identity differs from its pins')
    if manifest.get('bootstrapMode') == 'pinned-upstream-wasm':
        if manifest['files'] != lock['files']:
            raise ValueError('tsrs prebuilt payload differs from pinned release')
    elif manifest.get('bootstrapMode') == 'pinned-source-opt1':
        if {r['name']: {'version':r['version'], 'sha256':r['sha256']} for r in manifest['bootstrapTools']} != lock['nativeBootstrap']:
            raise ValueError('tsrs source bootstrap tool identities differ from pins')
    else:
        raise ValueError('unknown tsrs bootstrap profile')
    if manifest['recipe'] != recipe():
        raise ValueError('tsrs recipe changed; rerun bootstrap --reinstall')
    actual = {p.relative_to(HERE / 'dist').as_posix() for p in (HERE / 'dist').rglob('*')
              if p.is_file() and p.name != 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('tsrs installed inventory mismatch')
    for name, record in manifest['files'].items():
        path = HERE / 'dist' / name
        if (path.is_symlink() or not path.resolve().is_relative_to(HERE / 'dist')
                or digest(path) != record['sha256'] or path.stat().st_size != record['bytes']):
            raise ValueError('tsrs installed checksum mismatch: ' + name)
    return manifest


def resources(growth):
    for command in (['free','-h'], ['df','-h',str(HERE)], ['df','-i',str(HERE)],
                    ['findmnt','-T',str(HERE)], ['ps','-C','cargo,rustc,wasmtime,clang,cc1,make,ninja','-o','pid,ppid,rss,comm']):
        subprocess.run(command, check=command[0] != 'ps')
    if shutil.disk_usage(HERE).free < 5 * 1024**3 + growth:
        raise ValueError('need 5 GiB reserve plus ' + str(growth / 1024**3) + ' GiB bootstrap growth')
    filesystem = subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T',str(HERE)],text=True).strip()
    if filesystem in {'tmpfs','ramfs'}:
        raise ValueError('bootstrap storage must be on disk')


def fetch(record, name, offline):
    cache = HERE / '.downloads'; cache.mkdir(exist_ok=True)
    path = cache / name
    if not path.exists():
        if offline:
            raise ValueError('offline input missing: ' + str(path))
        with urllib.request.urlopen(record['url'], timeout=60) as source:
            payload = source.read(record['bytes'] + 1)
        if len(payload) != record['bytes'] or hashlib.sha256(payload).hexdigest() != record['sha256']:
            raise ValueError('download checksum mismatch')
        path.write_bytes(payload)
    if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
        raise ValueError('cached input checksum mismatch: ' + name)
    return path


def source_build(args, source, lock):
    archive = fetch(source['archive'], 'source.tar.gz', args.offline)
    cache = HERE / '.downloads/source'
    if not cache.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(cache, filter='data')
    directory = cache / ('tsrs-' + source['commit'])
    # Recheck the original source bytes before allowing Cargo/build scripts to run.
    with tarfile.open(archive) as tar:
        expected = {Path(*Path(m.name).parts[1:]).as_posix(): hashlib.sha256(tar.extractfile(m).read()).hexdigest()
                    for m in tar.getmembers() if m.isfile()}
    actual = {p.relative_to(directory).as_posix(): digest(p) for p in directory.rglob('*') if p.is_file() and not p.is_symlink()}
    with tarfile.open(archive) as tar:
        links = {Path(*Path(m.name).parts[1:]).as_posix(): m.linkname for m in tar.getmembers() if m.issym()}
    actual_links = {p.relative_to(directory).as_posix(): os.readlink(p) for p in directory.rglob('*') if p.is_symlink()}
    if actual_links != links or any(not (directory/name).resolve().is_relative_to(directory) for name in links):
        raise ValueError('source symlink inventory mismatch')
    if actual != expected or digest(directory / 'Cargo.lock') != source['cargoLockSha256']:
        raise ValueError('source inventory or Cargo.lock mismatch')
    crates = {(p['name'],p['version']):p['checksum'] for p in tomllib.loads((directory/'Cargo.lock').read_text())['package']
              if p.get('source','').startswith('registry+')}
    if crates != {(p['name'],p['version']):p['sha256'] for p in source['crates']}:
        raise ValueError('registry source pins differ from Cargo.lock')
    rustc = Path(os.environ.get('WISP_TSRS_BOOTSTRAP_RUSTC', shutil.which('rustc') or 'rustc')).resolve(strict=True)
    cargo = rustc.with_name('cargo')
    tools = []
    for name,path in [('rustc',rustc),('cargo',cargo)]:
        version = subprocess.check_output([path,'--version'],text=True).strip()
        if version != lock['nativeBootstrap'][name]['version'] or digest(path) != lock['nativeBootstrap'][name]['sha256']:
            raise ValueError('native bootstrap ' + name + ' differs from pinned Rust 1.99.0')
        tools.append({'name':name,'version':version,'sha256':digest(path)})
    build = ROOT / 'build/tsrs/source'; build.mkdir(parents=True,exist_ok=True)
    sysroot = Path(subprocess.check_output([rustc,'--print','sysroot'],text=True).strip())
    cargo_home = Path(os.environ.get('CARGO_HOME',Path.home()/'.cargo')).resolve()
    flags = ['--remap-path-prefix='+str(Path.home())+'=/bootstrap',
             '--remap-path-prefix='+str(directory)+'=/tsrs',
             '--remap-path-prefix='+str(cargo_home/'registry/src')+'=/cargo',
             '--remap-path-prefix='+str(sysroot/'lib/rustlib/src/rust')+'=/rustc']
    env = {**os.environ,'RUSTC':str(rustc),'CARGO_BUILD_JOBS':'1','RAYON_NUM_THREADS':'1',
           'CARGO_TARGET_DIR':str(build),'TMPDIR':str(build),'CARGO_INCREMENTAL':'0',
           'CARGO_PROFILE_RELEASE_OPT_LEVEL':'1','CARGO_PROFILE_RELEASE_LTO':'false',
           'CARGO_PROFILE_RELEASE_CODEGEN_UNITS':'1','CARGO_PROFILE_RELEASE_DEBUG':'false',
           'CARGO_PROFILE_RELEASE_STRIP':'debuginfo','CARGO_PROFILE_RELEASE_PANIC':'abort',
           'CARGO_ENCODED_RUSTFLAGS':'\x1f'.join(flags)}
    command = [str(cargo),'build','--locked','--release','--target','wasm32-wasip1','-p','tsrs_wasm','-j1']
    if args.offline:command.append('--offline')
    subprocess.run(command,cwd=directory,env=env,check=True)
    return build/'wasm32-wasip1/release/tsrs_wasm.wasm', tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--reinstall',action='store_true')
    parser.add_argument('--rebuild',action='store_true',help='native Rust bootstraps the compiler package; application checks remain in Wasm')
    args = parser.parse_args()
    if args.check or ((HERE/'dist').exists() and not (args.reinstall or args.rebuild)):
        check();print('tsrs ready (checksums verified)');return
    resources(3*1024**3 if args.rebuild else 30*1024**2)
    source = json.loads((HERE/'source.json').read_text())
    lock = json.loads((HERE/'toolchain.lock.json').read_text())
    archive = fetch(lock['release'],'tsrs-wasm.tgz',args.offline)
    with tempfile.TemporaryDirectory(prefix='.install-',dir=HERE) as temporary:
        stage = Path(temporary)/'dist';stage.mkdir()
        with tarfile.open(archive) as tar:
            for name,record in lock['files'].items():
                data = tar.extractfile('package/'+name).read()
                if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
                    raise ValueError('release payload checksum mismatch: '+name)
                (stage/name).write_bytes(data)
        tools = []
        if args.rebuild:
            module,tools = source_build(args,source,lock)
            shutil.copyfile(module,stage/'tsrs.wasm')
        if (stage/'tsrs.wasm').read_bytes()[:8]!=b'\0asm\x01\0\0\0':
            raise ValueError('tsrs compiler must be core Wasm')
        files = {p.name:{'sha256':digest(p),'bytes':p.stat().st_size} for p in sorted(stage.iterdir())}
        write_json(stage/'manifest.json',{'schemaVersion':1,'version':lock['version'],
            'typescriptVersion':lock['typescriptVersion'],'recipe':recipe(),'files':files,
            'compilerHost':'wasm32-wasip1','applicationCompilerWasmHosted':True,
            'bootstrapMode':'pinned-source-opt1' if args.rebuild else 'pinned-upstream-wasm',
            'bootstrapTools':tools,'source':source})
        dist = HERE/'dist'
        if dist.exists():
            previous = Path(temporary)/'previous';dist.rename(previous)
            try:stage.rename(dist)
            except BaseException:previous.rename(dist);raise
        else:stage.rename(dist)
    check();print('tsrs ready (checksums verified)')


if __name__=='__main__':main()
