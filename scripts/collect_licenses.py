"""Collect dependency attribution into the Windows bundle without local paths."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def licenses(folder, explicit=None):
    candidates = [folder / explicit] if explicit else []
    candidates += [p for p in folder.iterdir()
                   if p.is_file() and p.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'))]
    return '\n\n'.join(p.read_text(encoding='utf-8', errors='replace')
                        for p in sorted(set(candidates)) if p.is_file())


def main():
    raw = subprocess.check_output(['cargo', 'metadata', '--locked', '--offline', '--filter-platform', 'x86_64-pc-windows-msvc',
                                  '--format-version', '1', '--manifest-path',
                                  str(ROOT / 'desktop/src-tauri/Cargo.toml')])
    metadata = json.loads(raw)
    sections = ['Third-party notices for Life Assistant\nGenerated from locked dependencies.']
    missing = []
    for package in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        if package['name'] == 'life-assistant-desktop':
            continue
        folder = Path(package['manifest_path']).parent
        body = licenses(folder, package.get('license_file'))
        if not body:
            for sibling in metadata['packages']:
                if (package.get('repository') and sibling.get('repository') == package['repository']
                        and sibling.get('license') == package.get('license')):
                    body = licenses(Path(sibling['manifest_path']).parent, sibling.get('license_file'))
                    if body:
                        body = 'Repository license (from companion crate ' + sibling['name'] + '):\n' + body
                        break
        if not body:
            family = ('rust-unic' if package['name'].startswith('unic-') else
                      'webview2-rs' if package['name'].startswith('webview2-com') else package['name'])
            vendored = ROOT / 'scripts/licenses' / (family + '.txt')
            if vendored.is_file(): body = vendored.read_text(encoding='utf-8')
            else: missing.append(package['name'])
        sections.append(f"{package['name']} {package['version']}\nAuthors: {', '.join(package.get('authors', []))}\nLicense: {package.get('license') or 'See below'}\nCorresponding unmodified source: https://crates.io/api/v1/crates/{package['name']}/{package['version']}/download\n\n{body}")
    for name in ('@tauri-apps/api', 'vite'):
        folder = ROOT / 'desktop/node_modules' / name
        package = json.loads((folder / 'package.json').read_text())
        body = licenses(folder)
        if not body:
            raise SystemExit('Missing dependency license text: ' + name)
        sections.append(f"{name} {package['version']}\nLicense: {package['license']}\n\n{body}")
    if missing:
        raise SystemExit('Missing dependency license text: ' + ', '.join(missing))
    (ROOT / 'desktop/THIRD_PARTY_LICENSES.txt').write_text('\n\n' + ('\n\n' + '=' * 72 + '\n\n').join(sections), encoding='utf-8')
    print(f'Collected {len(sections)-1} dependency notices')


if __name__ == '__main__':
    main()
