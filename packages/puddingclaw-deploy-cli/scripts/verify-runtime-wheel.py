"""Bind wheel Python payload and package identity to the independent stage."""
from __future__ import annotations
import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import tomllib
import zipfile


def verify(stage: Path, wheel: Path):
    manifest=json.loads((stage/'.stage-manifest.json').read_bytes())
    project=tomllib.loads((stage/'pyproject.toml').read_text())['project']
    expected={row['path']:row['sha256'] for row in manifest['python']['files']}
    if not expected: raise ValueError('empty staged Python inventory')
    with zipfile.ZipFile(wheel) as archive:
        if len(archive.infolist())>10000 or sum(item.file_size for item in archive.infolist())>256*1024*1024:
            raise ValueError("wheel exceeds verification bounds")
        names=archive.namelist()
        if len(names)!=len(set(names)): raise ValueError('duplicate wheel entries')
        for item in archive.infolist():
            name=PurePosixPath(item.filename)
            if name.is_absolute() or '..' in name.parts or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('unsafe wheel entry')
        metadata=[name for name in names if name.endswith('.dist-info/METADATA')]
        if len(metadata)!=1: raise ValueError('wheel metadata must be unique')
        value=BytesParser().parsebytes(archive.read(metadata[0]))
        if len(value.get_all('Name',[]))!=1 or len(value.get_all('Version',[]))!=1:
            raise ValueError('wheel metadata identity is ambiguous')
        if value['Name']!=project['name'] or value['Version']!=project['version']:
            raise ValueError('wheel package identity differs from staged project')
        if {name for name in names if name.endswith('.py')}!=set(expected):
            raise ValueError('wheel Python inventory differs from stage')
        for name,digest in expected.items():
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=digest or data!=(stage/name).read_bytes():
                raise ValueError('wheel Python payload differs from stage')
    return {'name':value['Name'],'version':value['Version'],'python_files_verified':len(expected)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage',type=Path,required=True);parser.add_argument('--wheel',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.stage,args.wheel),sort_keys=True))
