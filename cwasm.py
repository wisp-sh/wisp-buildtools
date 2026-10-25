#!/usr/bin/python3
"""Precompile trusted compiler guests and launch them with Wasmtime."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
HEADERS = {b'\0asm\x01\0\0\0', b'\0asm\x0d\0\x01\0'}
ENGINE_OPTIONS = {'-O', '--optimize', '-C', '--codegen', '-D', '--debug',
                  '-W', '--wasm', '-S', '--wasi', '--target'}
VALUE_OPTIONS = {'--dir', '--env', '--invoke', '--argv0', '--timeout-ms', '--memory-bytes'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def host_target():
    machine = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine())
    suffix = {'Linux': 'unknown-linux-gnu', 'Darwin': 'apple-darwin', 'Windows': 'pc-windows-msvc'}
    return machine + '-' + suffix[platform.system()]


def executable(value):
    path = Path(shutil.which(str(value)) or value).resolve(strict=True)
    if path == Path(__file__).resolve():
        raise ValueError('WISP_CWASM_RUNTIME must name the native Wasmtime executable')
    return path


def private_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    attributes = path.lstat()
    if (not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != os.getuid()
            or attributes.st_mode & 0o077):
        raise ValueError(f'cwasm cache must be a private directory owned by this user: {path}')
    return path


def cache_directory():
    return private_directory(os.environ.get('WISP_CWASM_CACHE', ROOT / 'build/cwasm'))


def default_runtime():
    """Resolve the native host even when a guest launcher clears its environment."""
    supplied = os.environ.get('WISP_CWASM_RUNTIME')
    if supplied:
        return executable(supplied)
    siblings = [ROOT.parent / 'wisp', Path(str(ROOT).removesuffix('-buildtools'))]
    candidates = [ROOT / '.ci/hosts/wisp-wasmtime-50.0.0/wasmtime',
                  *(p / '.tools/shared-hosts/wisp-wasmtime-50.0.0/wasmtime' for p in siblings),
                  shutil.which('wasmtime')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return executable(candidate)
    raise ValueError('install the Wasmtime 50 compiler host or set WISP_CWASM_RUNTIME')


def normalized_flags(flags):
    parsed = engine_options([*flags, 'guest.wasm'], engine_only=True)
    if parsed is None or parsed[2] != len(flags):
        raise ValueError('expected only engine options')
    aliases = {'--optimize': '-O', '--codegen': '-C', '--debug': '-D', '--wasm': '-W', '--wasi': '-S'}
    values = {}
    tokens = parsed[0]
    for group, value in zip(tokens[::2], tokens[1::2]):
        group = aliases.get(group, group)
        for setting in value.split(',') if group != '--target' else [value]:
            key = setting.split('=', 1)[0]
            if group == '-C' and key in {'cache', 'parallel-compilation'}:
                continue
            if group == '-S' and key in {'inherit-env', 'inherit-network', 'tcp', 'udp', 'http'}:
                continue
            if group == '-S' and setting == 'p3=n':
                continue
            values[group, key] = setting
    return [token for pair in sorted(values) for token in (pair[0], values[pair])]


def packaged(source, identity):
    """Read native companions from an already authenticated toolchain installation."""
    try:
        relative = Path(source).resolve().relative_to(ROOT)
    except ValueError:
        return None
    provider = ('llvm-swift' if relative.is_relative_to(Path('llvm/dist/swift-shared'))
                else relative.parts[0])
    manifest = ROOT / 'precompiled' / provider / 'manifest.json'
    if not manifest.is_file():
        return None
    document = json.loads(manifest.read_text())
    for record in document.get('artifacts', []):
        if record.get('source') != relative.as_posix() or record.get('identity') != identity:
            continue
        path = ROOT / record['path']
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT / 'precompiled'):
            raise ValueError('unsafe precompiled artifact path')
        if not path.is_file() or digest(path) != record['sha256']:
            raise ValueError('precompiled toolchain artifact checksum mismatch')
        return path
    return None


def engine_options(arguments, *, engine_only=False):
    """Parse only known CLI options. Never interpret guest arguments as host flags."""
    flags, preloads = [], []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == '--':
            return None if engine_only else (flags, preloads, index + 1)
        if not value.startswith('-'):
            return flags, preloads, index
        option, equals, attached = value.partition('=')
        if option in ENGINE_OPTIONS | VALUE_OPTIONS | {'--preload'}:
            if engine_only and option not in ENGINE_OPTIONS:
                return None
            if not equals:
                index += 1
                if index == len(arguments):
                    raise ValueError(f'missing value for {option}')
                attached = arguments[index]
            if option in ENGINE_OPTIONS:
                flags += [option, attached]
            elif option == '--preload':
                name, separator, path = attached.partition('=')
                if not separator or not name or not path:
                    raise ValueError('expected --preload NAME=PATH')
                preloads.append((index, bool(equals), name, path))
        elif value[:2] in {'-O', '-C', '-D', '-W', '-S'} and len(value) > 2:
            flags += [value[:2], value[2:]]
        elif value != '--allow-precompiled' or engine_only:
            # Unknown options may affect the engine or consume the next word.
            # Let Wasmtime handle these invocations without changing their meaning.
            return None
        index += 1
    return None


def precompile(source, *, runtime, compiler=None, flags=(), cache=None, worker=False):
    if worker and flags:
        raise ValueError('worker precompilation uses its fixed engine configuration; omit engine flags')
    source = Path(source).resolve(strict=True)
    with source.open('rb') as stream:
        if stream.read(8) not in HEADERS:
            raise ValueError(f'expected a binary Wasm module or Component: {source}')
    runtime = executable(runtime)
    compiler = executable(compiler or runtime)
    flags = normalized_flags(flags)
    identity = {'schema': 1, 'sourceSha256': digest(source),
                'runtimeSha256': digest(runtime), 'compilerSha256': digest(compiler),
                'platform': platform.system(), 'target': host_target(), 'machine': platform.machine(),
                'engineFlags': list(flags), 'backend': 'worker' if worker else 'cli'}
    published = packaged(source, identity)
    if published is not None:
        return published
    cache = private_directory(cache) if cache is not None else cache_directory()
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = private_directory(cache / key)
    output, receipt = directory / 'module.cwasm', directory / 'receipt.json'
    # Serialize compilation across all guests and processes using this cache.
    lock = cache / 'compile.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if output.is_file() and not output.is_symlink() and receipt.is_file() and not receipt.is_symlink():
            try:
                record = json.loads(receipt.read_text())
            except (ValueError, OSError):
                record = {}
            if isinstance(record, dict) and record.get('identity') == identity and record.get('sha256') == digest(output):
                return output
        with tempfile.TemporaryDirectory(prefix='.compile-', dir=directory) as temporary:
            pending = Path(temporary) / 'module.cwasm'
            if worker:
                command = [str(compiler), 'precompile', str(source), '-o', str(pending)]
            else:
                command = [str(compiler), 'compile', *flags, '-C', 'parallel-compilation=n',
                           '-o', str(pending), str(source)]
            print('+ ' + ' '.join(command), file=sys.stderr, flush=True)
            subprocess.run(command, check=True,
                           env={**os.environ, 'RAYON_NUM_THREADS': '1', 'TOKIO_WORKER_THREADS': '1'})
            if digest(source) != identity['sourceSha256']:
                raise ValueError('Wasm source changed during precompilation')
            if digest(runtime) != identity['runtimeSha256'] or digest(compiler) != identity['compilerSha256']:
                raise ValueError('native host changed during precompilation')
            record = {'identity': identity, 'sha256': digest(pending), 'size': pending.stat().st_size}
            pending.chmod(0o600)
            pending.replace(output)
            staged = Path(temporary) / 'receipt.json'
            staged.write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
            staged.replace(receipt)
    return output


def load_module(wasmtime, engine, source, *, configuration, compile_module=None, artifact_only=False):
    """Load native code using the Python embedding's exact engine configuration."""
    source = Path(source).resolve(strict=True)
    identity = {'schema': 1, 'sourceSha256': digest(source), 'backend': 'python',
                'librarySha256': digest(Path(wasmtime._ffi.dll._name)),
                'configuration': configuration, 'machine': platform.machine(),
                'platform': platform.system(), 'target': host_target()}
    published = packaged(source, identity)
    if published is not None:
        return published if artifact_only else wasmtime.Module.deserialize_file(engine, str(published))
    cache = cache_directory()
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    directory = private_directory(cache / key)
    output, receipt = directory / 'module.cwasm', directory / 'receipt.json'
    fd = os.open(cache / 'compile.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        valid = False
        if output.is_file() and receipt.is_file() and not output.is_symlink() and not receipt.is_symlink():
            try:
                record = json.loads(receipt.read_text())
                valid = record['identity'] == identity and record['sha256'] == digest(output)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not valid:
            module = compile_module() if compile_module is not None else wasmtime.Module.from_file(engine, str(source))
            binary = module.serialize()
            del module
            if digest(source) != identity['sourceSha256']:
                raise ValueError('Wasm source changed during precompilation')
            with tempfile.TemporaryDirectory(prefix='.compile-', dir=directory) as temporary:
                pending = Path(temporary) / 'module.cwasm'
                pending.write_bytes(binary)
                del binary
                record = {'identity': identity, 'sha256': digest(pending), 'size': pending.stat().st_size}
                pending.replace(output)
                pending = Path(temporary) / 'receipt.json'
                pending.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
                pending.replace(receipt)
    return output if artifact_only else wasmtime.Module.deserialize_file(engine, str(output))


def record_for(path):
    path = Path(path)
    receipt = path.with_name('receipt.json')
    if receipt.is_file():
        return json.loads(receipt.read_text())
    document = json.loads(path.with_name('manifest.json').read_text())
    for record in document['artifacts']:
        if ROOT / record['path'] == path:
            return {key: record[key] for key in ('identity', 'sha256', 'size')}
    raise ValueError(f'missing precompilation receipt: {path}')


def prepare(arguments, *, runtime, compiler=None, cache=None, worker=False):
    """Replace the guest and preloads, preserving all execution options and args."""
    arguments = list(map(str, arguments))
    parsed = engine_options(arguments)
    if parsed is None:
        return arguments
    flags, preloads, guest = parsed
    # The worker validates run flags against its fixed engine configuration.
    # Its own precompile command uses that same configuration directly.
    if worker:
        flags = []
    if guest >= len(arguments):
        return arguments
    source = Path(arguments[guest])
    if not source.is_file():
        return arguments
    with source.open('rb') as stream:
        if stream.read(8) not in HEADERS:
            return arguments
    for index, attached, name, path in preloads:
        compiled = precompile(path, runtime=runtime, compiler=compiler, flags=flags, cache=cache, worker=worker)
        arguments[index] = ('--preload=' if attached else '') + name + '=' + str(compiled)
    arguments[guest] = str(precompile(source, runtime=runtime, compiler=compiler, flags=flags, cache=cache, worker=worker))
    if not worker and not any(value == '--argv0' or value.startswith('--argv0=') for value in arguments[:guest]):
        return ['--allow-precompiled', '--argv0', str(source), *arguments]
    return ['--allow-precompiled', *arguments]


def main():
    runtime = default_runtime()
    arguments = sys.argv[1:]
    if not arguments:
        os.execv(str(runtime), [str(runtime)])
    if arguments[:1] == ['run']:
        worker = os.environ.get('WISP_CWASM_BACKEND') == 'worker'
        if worker:
            os.environ.setdefault('XDG_CACHE_HOME', str(cache_directory() / 'wasmtime'))
        arguments = ['run', *prepare(arguments[1:], runtime=runtime,
                                    compiler=os.environ.get('WISP_CWASM_COMPILER'),
                                    worker=worker)]
    os.execv(str(runtime), [str(runtime), *arguments])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'cwasm: {error}', file=sys.stderr)
        raise SystemExit(1)
