"""Lower Binaryen's bottom function nulls to baseline nulls for Winch.

Only reference constant expressions in element segments are changed. Table
indices, function indices, data, and function bodies retain their bytes.
"""
import hashlib
import json
import os
from pathlib import Path


def leb(data, offset):
    value=0;shift=0
    while True:
        b=data[offset];offset+=1;value|=(b&127)<<shift
        if b<128:return value,offset
        shift+=7


def lower(data):
    b=bytearray(data);offset=8;changes=0
    while offset<len(b):
        size,start=leb(b,offset+1);end=start+size
        if b[offset]==9:
            count,p=leb(b,start)
            def expression(p):
                nonlocal changes
                while True:
                    op=b[p];p+=1
                    if op==0x0b:return p
                    if op not in (0x41,0x42,0x23,0xd0,0xd2):raise ValueError('unexpected element constant expression opcode '+hex(op))
                    if op==0xd0 and b[p]==0x73:
                        b[p]=0x70;changes+=1
                    _,p=leb(b,p)
            for _ in range(count):
                flags,p=leb(b,p)
                if flags in (2,6):_,p=leb(b,p)
                if flags in (0,2,4,6):p=expression(p)
                if flags in (1,2,3):
                    if b[p]!=0:raise ValueError('unexpected element kind')
                    p+=1
                if flags in (5,6,7):
                    if b[p] not in (0x70,0x6f):raise ValueError('typed element reference requires a separate lowering')
                    p+=1
                entries,p=leb(b,p)
                for _ in range(entries):
                    if flags<4:_,p=leb(b,p)
                    else:p=expression(p)
            if p!=end:raise ValueError('element section parser did not consume its input')
        offset=end
    return bytes(b),changes


def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def image(source):
    source=Path(source);directory=Path(os.environ.get('WISP_SWIFT_COMPAT_CACHE',Path(__file__).parent/'.cache/winch'))
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/source.name;receipt=directory/(source.name+'.json');checksum=sha(source)
    if receipt.exists() and target.exists():
        record=json.loads(receipt.read_text())
        if record['sourceSha256']==checksum and record['sha256']==sha(target) and record.get('transformSha256')==sha(__file__):return target
    output,count=lower(source.read_bytes())
    if count == 0: return source
    pending=target.with_suffix('.pending');pending.write_bytes(output);pending.replace(target)
    receipt.write_text(json.dumps({'source':str(source),'sourceSha256':checksum,'path':str(target),'sha256':sha(target),'bytes':len(output),'bottomNullsLowered':count,'transformSha256':sha(__file__)},indent=2)+'\n')
    return target


def self_test(wasmtime):
    # One funcref table, initialized by a bottom null in an active segment.
    original=b'\0asm\1\0\0\0'+b'\x04\x04\x01\x70\x00\x01'+b'\x07\x05\x01\x01t\x01\x00'+b'\x09\x0b\x01\x06\x00\x41\x00\x0b\x70\x01\xd0\x73\x0b'
    converted,count=lower(original)
    assert count==1 and converted.count(b'\xd0\x70')==1
    config=wasmtime.Config();wasmtime._ffi.wasmtime_config_strategy_set(config.ptr(),2)
    engine=wasmtime.Engine(config);module=wasmtime.Module(engine,converted)
    store=wasmtime.Store(engine);instance=wasmtime.Instance(store,module,[])
    assert instance.exports(store)['t'].get(store,0).value is None
    assert lower(converted)==(converted,0)
    return {'passed':True,'bottomNullsLowered':count,'winchValidatedAndInstantiated':True,'idempotent':True}
