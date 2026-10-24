"""Derive a heap-configured JVM guest without rebuilding or replacing the installed runtime.

The pinned C launcher embeds one heap-size constant in main. Change that single
same-length signed LEB instruction. Everything else, including the collector,
compiler JARs and linked runtime, stays byte-for-byte identical.
"""
from pathlib import Path
import hashlib,json
from .runtime import ROOT,integrity,digest

def uleb(data, offset):
    value=shift=0
    while True:
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if not byte&128:return value,offset
        shift+=7
        if shift>35:raise ValueError('invalid WebAssembly integer')
def string(data,offset):
    size,offset=uleb(data,offset);return data[offset:offset+size].decode(),offset+size
def sleb(value):
    result=bytearray()
    while True:
        byte=value&127;value>>=7
        done=(value==0 and not byte&64) or (value==-1 and byte&64)
        result.append(byte if done else byte|128)
        if done:return bytes(result)
def derive(mebibytes=768):
    integrity()
    if mebibytes not in [640,768]:raise ValueError('only bounded 640/768 MiB guest heaps are supported')
    original=ROOT/'dist/jvm.wasm';data=original.read_bytes()
    if data[:8]!=b'\0asm\x01\0\0\0':raise ValueError('expected a Core JVM guest')
    sections=[];pos=8;imports=0;names={}
    while pos<len(data):
        kind=data[pos];size,start=uleb(data,pos+1);end=start+size;sections.append((kind,start,end));pos=end
        if kind==2:
            count,p=uleb(data,start)
            for _ in range(count):
                _,p=string(data,p);_,p=string(data,p);tag=data[p];p+=1
                if tag!=0:raise ValueError('expected only function imports in the pinned JVM')
                _,p=uleb(data,p);imports+=1
        if kind==0:
            name,p=string(data,start)
            if name=='name':
                while p<end:
                    sub=data[p];n,a=uleb(data,p+1);b=a+n;p=b
                    if sub==1:
                        count,a=uleb(data,a)
                        for _ in range(count):
                            index,a=uleb(data,a);text,a=string(data,a);names[index]=text
    mains=[i for i,n in names.items() if n=='main']
    if len(mains)!=1:raise ValueError('expected exactly one named launcher main')
    function=mains[0]-imports;bounds=None
    for kind,start,end in sections:
        if kind==10:
            count,p=uleb(data,start)
            for i in range(count):
                n,a=uleb(data,p);b=a+n;p=b
                if i==function:bounds=(a,b)
    if bounds is None:raise ValueError('launcher main code body missing')
    old=b'\x41'+sleb(512*1024**2);new=b'\x41'+sleb(mebibytes*1024**2)
    if len(old)!=len(new):raise ValueError('heap patch must preserve module offsets')
    a,b=bounds
    if data[a:b].count(old)!=1:raise ValueError('expected exactly one 512 MiB heap instruction in main')
    offset=data.index(old,a,b);updated=data[:offset]+new+data[offset+len(old):]
    directory=ROOT/'.cache/heap-variants';directory.mkdir(parents=True,exist_ok=True)
    output=directory/f'jvm-{mebibytes}MiB.wasm'
    if output.exists() and output.read_bytes()!=updated:raise ValueError('existing JVM heap variant differs')
    if not output.exists():
        temporary=output.with_suffix('.pending');temporary.write_bytes(updated);temporary.replace(output)
    record={'schemaVersion':1,'base':str(original),'baseSha256':digest(original),'output':str(output),'outputSha256':digest(output),'bytes':len(updated),'heapBytes':mebibytes*1024**2,'originalHeapBytes':512*1024**2,'function':'main','instructionOffset':offset,'originalInstructionHex':old.hex(),'patchedInstructionHex':new.hex(),'changedBytes':sum(x!=y for x,y in zip(data,updated)),'nativeBootstrapExecuted':False,'purpose':'Bounded workaround for Bovine major_gc corruption during large Kotlin compilation; collector code is unchanged.'}
    output.with_suffix('.json').write_text(json.dumps(record,indent=2)+'\n')
    return output,record
