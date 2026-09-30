"""Fail closed on release inputs; scan archives recursively without extracting them."""
import io
import re
from pathlib import Path
import sys
import tarfile
import zipfile

PATTERNS = [re.compile(x, re.I) for x in [
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    rb'\bsk-[A-Za-z0-9_-]{24,}',
    rb'\bgh[pousr]_[A-Za-z0-9]{30,}',
    rb'\bgithub_pat_[A-Za-z0-9_]{30,}',
    rb'[A-Z]:[\\/]Users[\\/](?!runneradmin|runner|Public|user[\\/])[^\s\x00"<>]+',
    rb'\b[0-9]{5,12}@qq\.com\b',
]]
PRIVATE_NAMES = {'.env','settings.json','config.json','state.sqlite3','accounts.json','credentials.dpapi','app-stdout.log','app-stderr.log'}
EXCLUDED = {'.git','.venv','node_modules','target','dist','build','__pycache__','.test-artifacts'}

def inspect(name, data, depth=0):
    if depth > 5: raise ValueError('nested_archive_limit')
    parts=Path(name).parts
    if any(p in {'.local','.runtime','.secrets'} for p in parts) or Path(name).name in PRIVATE_NAMES:
        raise ValueError('private_runtime_file: '+name)
    for pattern in PATTERNS:
        # PE resources and Windows installers may encode strings as UTF-16LE.
        if pattern.search(data) or pattern.search(data.replace(b'\x00', b'')):
            raise ValueError('sensitive_content: '+name)
    source=io.BytesIO(data)
    if name.endswith(('.zip','.whl')):
        with zipfile.ZipFile(source) as z:
            if sum(i.file_size for i in z.infolist()) > 1_000_000_000: raise ValueError('archive_too_large')
            for item in z.infolist():
                if not item.is_dir():inspect(item.filename,z.read(item),depth+1)
    elif name.endswith(('.tar.gz','.tgz')):
        with tarfile.open(fileobj=source,mode='r:gz') as t:
            for item in t:
                if item.isfile():inspect(item.name,t.extractfile(item).read(),depth+1)

def main():
    root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()
    count=0
    if root.is_file():
        inspect(root.name,root.read_bytes());count=1
    else:
        for path in root.rglob('*'):
            rel=path.relative_to(root)
            if any(p in EXCLUDED or p.endswith('.egg-info') for p in rel.parts):continue
            if path.is_symlink():raise ValueError('symlink_in_release_inputs: '+str(rel))
            if path.is_file():inspect(rel.as_posix(),path.read_bytes());count+=1
    print(f'Privacy scan passed: {count} inputs. Manual source review still required.')

if __name__=='__main__':main()
