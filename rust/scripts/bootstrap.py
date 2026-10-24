#!/usr/bin/env python3
"""Install checksum-pinned bootstrap Wasm rustc and sysroot for Wisp's host."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = destination / member.name
            if (not (member.isfile() or member.isdir()) or
                    not path.resolve().is_relative_to(destination.resolve())):
                raise ValueError(f'unsafe archive member: {member.name}')
        source.extractall(destination, filter='data')


def install(*, offline: bool = False) -> Path:
    manifest = json.loads((ROOT / 'toolchain.json').read_text())
    if manifest.get('status') == 'source-build-required':
        raise ValueError('No prebuilt WASI compiler is selected for Rust ' + manifest['rustVersion']
                         + '; run python3 build.py build rust from the repository root')
    downloads = ROOT / 'downloads'
    downloads.mkdir(exist_ok=True)
    destination = ROOT / 'build/bootstrap'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bootstrap-', dir=destination.parent) as temp:
        stage = Path(temp)
        for key in ('compiler', 'sysroot'):
            item = manifest[key]
            archive = downloads / item['archive']
            if not archive.exists():
                if offline:
                    raise FileNotFoundError(f'offline archive missing: {archive}')
                partial = archive.with_suffix(archive.suffix + '.partial')
                try:
                    urllib.request.urlretrieve(item['url'], partial)
                    if sha256(partial) != item['sha256']:
                        raise ValueError(f'download checksum mismatch: {item["archive"]}')
                    partial.replace(archive)
                finally:
                    partial.unlink(missing_ok=True)
            if sha256(archive) != item['sha256']:
                raise ValueError(f'archive checksum mismatch: {archive}')
            out = stage if key != 'sysroot' else stage / 'sysroot/lib/rustlib/wasm32-wasip1/lib'
            extract(archive, out)
        compiler = stage / manifest['compiler']['module']
        if sha256(compiler) != manifest['compiler']['moduleSha256']:
            raise ValueError('compiler module checksum mismatch')
        files = sorted(p for p in stage.rglob('*') if p.is_file())
        inventory = {str(p.relative_to(stage)): sha256(p) for p in files}
        (stage / 'SHA256SUMS.json').write_text(json.dumps(inventory, indent=2) + '\n')
        # All downloaded bytes have been verified before replacing an earlier install.
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(stage, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    print(install(offline=args.offline))


if __name__ == '__main__':
    main()
