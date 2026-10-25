"""Guest import loader embedded in a source bundle, executed only inside Wasm."""


def __wisp_load_bundle(module_sources, entry):
    import sys as _sys
    from _frozen_importlib import ModuleSpec as _ModuleSpec

    _compile = compile
    _exec = exec
    _import = __import__

    class SourceLoader:
        def find_spec(self, fullname, path=None, target=None):
            record = module_sources.get(fullname)
            if record is None:
                return None
            text, filename, package = record
            spec = _ModuleSpec(fullname, self if text is not None else None,
                               origin=filename, is_package=package)
            spec.has_location = text is not None
            if package:
                spec.submodule_search_locations = [filename.rsplit('/', 1)[0]]
            return spec

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            text, filename, _ = module_sources[module.__name__]
            _exec(_compile(text, filename, 'exec', dont_inherit=True), module.__dict__)

        def get_source(self, fullname):
            return module_sources[fullname][0]

        def get_filename(self, fullname):
            return module_sources[fullname][1]

        def is_package(self, fullname):
            return module_sources[fullname][2]

    for name in module_sources:
        if name in _sys.modules:
            raise ImportError(f'bundled module {name!r} conflicts with a preinitialized runtime module')
    finder = SourceLoader()
    _sys.meta_path.insert(0, finder)
    before = set(_sys.modules)
    try:
        return _import(entry, fromlist=['__name__']).__dict__
    except BaseException:
        # A failed initializer must not leave a partial bundle installed.
        _sys.meta_path.remove(finder)
        for name in set(_sys.modules) - before:
            if name in module_sources:
                del _sys.modules[name]
        raise
