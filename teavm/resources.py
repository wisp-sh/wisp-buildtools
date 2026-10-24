"""Stage identical CLDR bytes without DEFLATE for guest ZipInputStream compatibility."""
from __future__ import annotations
import hashlib
import io
from pathlib import Path
import shutil
import zipfile

RESOURCE='org/teavm/classlib/impl/unicode/cldr-json.zip'
def prepare(dist: Path, target: Path) -> dict:
    jars=sorted((dist/'jars').glob('teavm-classlib-*.jar'))
    if len(jars)!=1:raise ValueError('expected one TeaVM class library archive')
    with zipfile.ZipFile(jars[0]) as outer:
        data=outer.read(RESOURCE)
    output=target/RESOURCE;output.parent.mkdir(parents=True,exist_ok=True)
    count=0; payload=hashlib.sha256(); total=0
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        size=sum(entry.file_size for entry in source.infolist())
        if shutil.disk_usage(target).free < size+64*1024**2:raise ValueError('insufficient space to stage CLDR ZIP_STORED resources')
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as stored:
            for entry in source.infolist():
                contents=source.read(entry.filename)
                staged=zipfile.ZipInfo(entry.filename,(1980,1,1,0,0,0));staged.compress_type=zipfile.ZIP_STORED
                staged.external_attr=entry.external_attr
                stored.writestr(staged,contents)
                payload.update(entry.filename.encode()+b'\0'+hashlib.sha256(contents).digest())
                total+=len(contents);count+=1
        with zipfile.ZipFile(output) as check:
            if [i.filename for i in source.infolist()]!=check.namelist():raise ValueError('CLDR archive entries changed')
            for entry in source.infolist():
                if source.read(entry.filename)!=check.read(entry.filename):raise ValueError('CLDR payload changed: '+entry.filename)
    return {'resource':RESOURCE,'inputSha256':hashlib.sha256(data).hexdigest(),
            'outputSha256':hashlib.file_digest(output.open('rb'),'sha256').hexdigest(),
            'entryPayloadInventorySha256':payload.hexdigest(),'entries':count,'payloadBytes':total,
            'transformation':'DEFLATE to ZIP_STORED only; all entry names/order and contents verified identical',
            'reason':'guest JVM ZipInputStream produces truncated DEFLATE entries for the nested CLDR resource'}
