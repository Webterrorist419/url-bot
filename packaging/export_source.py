#!/usr/bin/env python3
"""Export the public project without local configuration, data or build output."""
import hashlib
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    '.gitattributes', '.gitignore', 'CHANGELOG.md', 'CONTRIBUTING.md', 'LICENSE',
    'MIGRATION.md', 'README.md', 'THIRD_PARTY.md', 'VERSION', 'bot.py',
    'config.env.example', 'install.sh', 'requirements-build.txt',
    'test_bot.py', 'test_broadcast.py',
)


def main():
    sources = [ROOT / name for name in FILES]
    for directory in ('packaging', '.github'):
        sources.extend(path for path in (ROOT / directory).rglob('*')
                       if path.is_file() and '__pycache__' not in path.parts
                       and path.suffix not in {'.pyc', '.pyo'})
    output = ROOT / 'url_bot-source.zip'
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sources):
            relative = path.relative_to(ROOT).as_posix()
            if path.is_symlink():
                raise ValueError('Source exports cannot include symlinks: ' + relative)
            info = zipfile.ZipInfo('url-bot/' + relative)
            info.create_system = 3
            executable = relative == 'packaging/url-bot' or path.suffix == '.sh'
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    checksums = []
    for name in ('url_bot.deb', 'bot-linux-x86_64', output.name):
        path = ROOT / name
        if path.is_file():
            checksums.append('{}  {}\n'.format(hashlib.sha256(path.read_bytes()).hexdigest(), name))
    (ROOT / 'SHA256SUMS').write_text(''.join(checksums), encoding='utf-8')
    print('{} ({} source files)'.format(output, len(sources)))


if __name__ == '__main__':
    main()
