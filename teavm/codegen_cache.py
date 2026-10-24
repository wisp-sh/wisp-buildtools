"""Content-checked external cache for TeaVM-generated C, before LLVM compilation."""
import hashlib,json,os
from pathlib import Path
import shutil,tempfile

def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def target(base, inputs):
    return Path(base)/hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest() if base else None
def restore(cache, inputs, output):
    if cache is None:return None
    try:
        record=json.loads((cache/'record.json').read_text())
        if record['inputs']!=inputs:return None
        files=record['files']
        if set(files)!={p.relative_to(cache/'output').as_posix() for p in (cache/'output').rglob('*') if p.is_file()}:return None
        for name,sha in files.items():
            p=cache/'output'/name
            if Path(name).is_absolute() or '..' in Path(name).parts or p.is_symlink() or digest(p)!=sha:return None
        shutil.copytree(cache/'output',output)
        return record
    except (OSError,ValueError,KeyError):return None
def save(cache, inputs, output, command):
    if cache is None:return None
    cache.parent.mkdir(parents=True,exist_ok=True)
    files={p.relative_to(output).as_posix():digest(p) for p in sorted(output.rglob('*')) if p.is_file()}
    total=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
    if shutil.disk_usage(cache.parent).free<total+64*1024**2:raise ValueError('insufficient space for external TeaVM code generation cache')
    record={'inputs':inputs,'files':files,'command':command,'sourceCompilationWasmHosted':True,'bytes':total}
    with tempfile.TemporaryDirectory(dir=cache.parent,prefix='.codegen-') as temp:
        stage=Path(temp)/'cache';stage.mkdir();shutil.copytree(output,stage/'output')
        (stage/'record.json').write_text(json.dumps(record,indent=2)+'\n')
        if cache.exists():shutil.rmtree(cache)
        stage.rename(cache)
    return record
