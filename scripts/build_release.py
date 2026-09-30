"""Build a server archive from an explicit export allowlist, never workspace history."""
import hashlib
from pathlib import Path
import sys
import tarfile
import tomllib

from privacy_scan import inspect

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ('server', 'openclaw-bridge', 'gateway', 'scripts', 'docs', 'install.sh',
           'pyproject.toml', 'requirements.lock', 'README.md', 'LICENSE',
           'THIRD_PARTY_NOTICES.md', '.gitattributes')


def main():
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'dist').resolve()
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f'life-assistant-{version}-server.tar.gz'
    paths = []
    for entry in ENTRIES:
        source = ROOT / entry
        for path in sorted(source.rglob('*')) if source.is_dir() else [source]:
            if any(p in {'__pycache__', 'node_modules'} for p in path.parts) or path.suffix == '.pyc':
                continue
            if path.is_symlink():
                raise ValueError('Symlink in release inputs')
            if path.is_file():
                relative = path.relative_to(ROOT).as_posix()
                inspect(relative, path.read_bytes())
                paths.append((path, relative))
    with tarfile.open(archive, 'w:gz') as output:
        for path, relative in paths:
            info = output.gettarinfo(str(path), f'life-assistant-{version}/{relative}')
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o755 if relative in {'install.sh', 'scripts/life-admin'} else 0o644
            with path.open('rb') as stream:
                output.addfile(info, stream)
    inspect(archive.name, archive.read_bytes())
    (target / 'SHA256SUMS').write_text(
        ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                for p in sorted(target.iterdir()) if p.is_file() and p.name != 'SHA256SUMS'),
        encoding='utf-8')
    print(f'Built and scanned {archive.name}: {len(paths)} allowlisted files')


if __name__ == '__main__':
    main()
