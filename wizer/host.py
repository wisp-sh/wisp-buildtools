"""Resolve normal Wizer in a pinned official or source-verified Wisp host."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil

HERE=Path(__file__).resolve().parent

def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def host(configured=None):
    lock=json.loads((HERE/'toolchain.lock.json').read_text())
    selected=configured or os.environ.get('WISP_WIZER_WASMTIME') or os.environ.get('WISP_BOOTSTRAP_WASMTIME')
    if not selected:raise ValueError('set WISP_WIZER_WASMTIME to the pinned Wasmtime 50 host with its wizer subcommand')
    path=Path(shutil.which(str(selected)) or selected).resolve(strict=True)
    binary_hash=digest(path)
    identity=None
    for name,pin in lock['hosts'].items():
        if binary_hash==pin['sha256']:identity={'kind':name,'version':pin['version'],'binarySha256':binary_hash}
    if identity is None:
        try:receipt=json.loads((path.parent/'receipt.json').read_text())
        except (OSError,ValueError) as error:raise ValueError('Wizer host does not match a pinned binary or Wisp source receipt') from error
        expected=lock['wispSourceHost']
        if (receipt.get('owner')!='wisp' or receipt.get('source')!=expected['source']
            or receipt.get('patchSha256')!=expected['patchSha256'] or receipt.get('binarySha256')!=binary_hash
            or receipt.get('cargoLockSha256')!=expected['cargoLockSha256']
            or receipt.get('smoke',{}).get('wizerSnapshot')!={'value':42,'initializerRemoved':True}):
            raise ValueError('Wizer host receipt differs from the pinned Wisp host recipe')
        identity={'kind':'wisp-source-host','version':lock['version'],'binarySha256':binary_hash,
                  'source':expected,'cargoLockSha256':receipt.get('cargoLockSha256')}
    return path,identity
