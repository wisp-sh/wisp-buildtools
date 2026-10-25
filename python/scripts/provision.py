"""Contract-only native bootstrap, serialized and cached outside the toolchain."""
import fcntl
import hashlib
import json
from pathlib import Path
import platform
import sys

from scripts.common import ROOT, digest, run

INPUTS = ('bootstrap.py', 'dist/manifest.json', 'scripts/common.py', 'scripts/provision.py',
          'bootstrap_contract.py', 'scripts/contracts.py', 'scripts/runtime_contract.py',
          'scripts/source_bundle.py', 'scripts/source_loader.py', 'toolchain.lock.json')


def template(key, exports, wit, world, cache_dir):
    generation = hashlib.sha256(json.dumps({p: digest(ROOT / p) for p in INPUTS}, sort_keys=True).encode()).hexdigest()
    cache = Path(cache_dir) / generation / 'contracts'
    cache.mkdir(parents=True, exist_ok=True)
    # Provisioning touches no application source and never uses the network.
    with (cache / '.provision.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        destination = cache / key
        if not destination.is_dir():
            if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
                raise ValueError('automatic Python runtime provisioning currently requires Linux x86_64; '
                                 'install a matching preprovisioned contract template on this host')
            run([sys.executable, ROOT / 'bootstrap_contract.py', '--wit', wit, '--world', world,
                 '--cache-dir', cache, '--offline'], capture=True)
        return destination
