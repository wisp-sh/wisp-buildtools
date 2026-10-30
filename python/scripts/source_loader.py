"""Read-only guest source and resource filesystem. Runs only inside CPython Wasm."""


def __wisp_load_bundle(program, entry):
    import sys as _sys
    import base64 as _base64
    import io as _io
    import os as _os
    import builtins as _builtins
    from _frozen_importlib import ModuleSpec as _ModuleSpec

    modules = program['modules']
    data = {name: _base64.b64decode(value) for name, value in program['assets'].items()}
    for text, filename, _ in modules.values():
        if text is not None:
            data[filename] = text.encode('utf-8')
    root = '/__wisp__'
    directories = {root}
    for name in data:
        parts = name.split('/')
        directories.update('/'.join(parts[:count]) for count in range(2, len(parts)))
    _compile, _exec, _import = compile, exec, __import__
    _open, _io_open = _builtins.open, _io.open
    _stat, _listdir, _scandir, _lstat, _access = _os.stat, _os.listdir, _os.scandir, _os.lstat, _os.access

    def path_name(path):
        try:
            value = _os.fsdecode(_os.fspath(path))
        except TypeError:
            return None
        value = _os.path.normpath(value)
        return value if value == root or value.startswith(root + '/') else None

    def read(path):
        if path not in data:
            raise FileNotFoundError(path)
        return data[path]

    class MemoryFile(_io.BytesIO):
        def __init__(self, value, name):
            super().__init__(value)
            self.name, self.mode = name, 'rb'

    def memory_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None,
                    closefd=True, opener=None):
        name = path_name(path)
        if name is None:
            return _open(path, mode, buffering, encoding, errors, newline, closefd, opener)
        if mode not in ('r', 'rt', 'rb') or opener is not None:
            raise PermissionError('bundled Python package data is read-only: ' + name)
        buffer = MemoryFile(read(name), name)
        return buffer if 'b' in mode else _io.TextIOWrapper(buffer, encoding=encoding or 'utf-8', errors=errors, newline=newline)

    def memory_stat(path, *args, **kwargs):
        name = path_name(path)
        if name is None:
            return _stat(path, *args, **kwargs)
        if name not in data and name not in directories:
            raise FileNotFoundError(name)
        directory = name in directories
        return _os.stat_result((0o40555 if directory else 0o100444, 0, 0, 1, 0, 0,
                                0 if directory else len(data[name]), 0, 0, 0))

    def memory_lstat(path, *args, **kwargs):
        return _lstat(path, *args, **kwargs) if path_name(path) is None else memory_stat(path)

    def memory_access(path, mode, *args, **kwargs):
        name = path_name(path)
        if name is None: return _access(path, mode, *args, **kwargs)
        if name not in data and name not in directories: return False
        return not mode & _os.W_OK and (not mode & _os.X_OK or name in directories)

    def memory_listdir(path='.'):
        name = path_name(path)
        if name is None:
            return _listdir(path)
        if name not in directories:
            raise NotADirectoryError(name)
        prefix = name.rstrip('/') + '/'
        return sorted({value[len(prefix):].split('/')[0] for value in set(data) | directories
                       if value.startswith(prefix)})

    class DirectoryEntry:
        def __init__(self, parent, name):
            self.name, self.path = name, parent.rstrip('/') + '/' + name
        def __fspath__(self): return self.path
        def inode(self): return 0
        def is_dir(self, *, follow_symlinks=True): return self.path in directories
        def is_file(self, *, follow_symlinks=True): return self.path in data
        def is_symlink(self): return False
        def stat(self, *, follow_symlinks=True): return memory_stat(self.path)

    class Scan:
        def __init__(self, path): self.iterator = iter(DirectoryEntry(path, name) for name in memory_listdir(path))
        def __iter__(self): return self
        def __next__(self): return next(self.iterator)
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def close(self): pass

    def memory_scandir(path='.'):
        name = path_name(path)
        return _scandir(path) if name is None else Scan(name)

    class Reader:
        def __init__(self, package): self.root = modules[package][1].rsplit('/', 1)[0]
        def files(self):
            from pathlib import Path
            return Path(self.root)
        def open_resource(self, name): return self.files().joinpath(name).open('rb')
        def resource_path(self, name): raise FileNotFoundError('bundled resources have no host filesystem path')
        def is_resource(self, name): return self.files().joinpath(name).is_file()
        def contents(self): return memory_listdir(self.root)

    class SourceLoader:
        def find_spec(self, fullname, path=None, target=None):
            record = modules.get(fullname)
            if record is None: return None
            text, filename, package = record
            spec = _ModuleSpec(fullname, self, origin=filename, is_package=package)
            spec.has_location = text is not None
            if package: spec.submodule_search_locations = [filename.rsplit('/', 1)[0]]
            return spec
        def create_module(self, spec): return None
        def exec_module(self, module):
            text, filename, _ = modules[module.__name__]
            if text is not None: _exec(_compile(text, filename, 'exec', dont_inherit=True), module.__dict__)
        def get_source(self, fullname): return modules[fullname][0]
        def get_filename(self, fullname): return modules[fullname][1]
        def is_package(self, fullname): return modules[fullname][2]
        def get_data(self, path): return read(path)
        def get_resource_reader(self, fullname): return Reader(fullname) if self.is_package(fullname) else None
        def find_distributions(self, context=None):
            from importlib.metadata import Distribution
            class BundledDistribution(Distribution):
                def __init__(self, prefix): self.prefix = prefix
                def read_text(self, filename):
                    value = data.get(self.prefix + '/' + filename)
                    return value.decode('utf-8') if value is not None else None
                def locate_file(self, path):
                    from pathlib import Path
                    return Path(root) / path
            for prefix in sorted({name.split('/', 3)[2] for name in data if '.dist-info/' in name}):
                if root + '/' + prefix + '/METADATA' not in data: continue
                distribution = BundledDistribution(root + '/' + prefix)
                wanted = getattr(context, 'name', None)
                normalize = lambda name: name.lower().replace('-', '_').replace('.', '_')
                if wanted is None or normalize(distribution.metadata['Name']) == normalize(wanted):
                    yield distribution

    for name in modules:
        if name in _sys.modules:
            raise ImportError(f'bundled module {name!r} conflicts with a preinitialized runtime module')
    finder = SourceLoader()
    _sys.meta_path.insert(0, finder)
    before = set(_sys.modules)
    _builtins.open = _io.open = memory_open
    _os.stat, _os.listdir, _os.scandir = memory_stat, memory_listdir, memory_scandir
    _os.lstat, _os.access = memory_lstat, memory_access
    try:
        return _import(entry, fromlist=['__name__']).__dict__
    except BaseException:
        _sys.meta_path.remove(finder)
        for name in set(_sys.modules) - before: del _sys.modules[name]
        _builtins.open, _io.open = _open, _io_open
        _os.stat, _os.listdir, _os.scandir = _stat, _listdir, _scandir
        _os.lstat, _os.access = _lstat, _access
        raise
