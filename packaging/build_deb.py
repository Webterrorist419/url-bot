#!/usr/bin/env python3
"""Build url_bot.deb from the verified output of build_release.py (Linux amd64)."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'packaging'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    version = (ROOT / 'VERSION').read_text().strip()
    binary = ROOT / 'bot-linux-x86_64'
    manifest = json.loads((ROOT / '.build/release-manifest.json').read_text())
    if (manifest['version'] != version
            or manifest['source_sha256'] != sha256(ROOT / 'bot.py')
            or manifest['binary_sha256'] != sha256(binary)):
        raise ValueError('Build output is stale. Run python3 packaging/build_release.py.')
    header = binary.read_bytes()[:20]
    if header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 62:
        raise ValueError('Expected Linux x86_64 executable')

    with tempfile.TemporaryDirectory(prefix='url-bot-deb-') as directory:
        stage = Path(directory)
        stage.chmod(0o755)

        def write(name, content, mode=0o644):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
            target.chmod(mode)

        def copy(path, destination, mode=0o644):
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(mode)

        copy(binary, 'usr/lib/url-bot/bot-linux-x86_64', 0o755)
        copy(ROOT / 'bot.py', 'usr/share/doc/url-bot/source/bot.py')
        copy(SOURCE / 'url-bot', 'usr/bin/url-bot', 0o755)
        copy(SOURCE / 'url-bot.service', 'usr/lib/systemd/system/url-bot.service')
        copy(SOURCE / 'README-deb.md', 'usr/share/doc/url-bot/README.md')
        copy(ROOT / 'LICENSE', 'usr/share/doc/url-bot/copyright')
        copy(ROOT / 'THIRD_PARTY.md', 'usr/share/doc/url-bot/THIRD_PARTY.md')
        copy(ROOT / '.build/release-manifest.json', 'usr/share/doc/url-bot/build.json')
        for license_file in sorted((ROOT / '.build/third-party').glob('*')):
            copy(license_file, 'usr/share/doc/url-bot/licenses/' + license_file.name)
        write('etc/url-bot/config.env', 'BOT_TOKEN=\nADMIN_ID=\nDATABASE_PATH=/var/lib/url-bot/bot.sqlite3\n', 0o640)
        write('DEBIAN/conffiles', '/etc/url-bot/config.env\n')
        for script in ('postinst', 'prerm', 'postrm'):
            write('DEBIAN/' + script, (SOURCE / script).read_text(encoding='utf-8'), 0o755)
            subprocess.run(['sh', '-n', str(stage / 'DEBIAN' / script)], check=True)
        size = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file())
        write('DEBIAN/control', '\n'.join([
            'Package: url-bot',
            'Version: ' + version,
            'Section: net',
            'Priority: optional',
            'Architecture: amd64',
            'Maintainer: URL Bot contributors <maintainers@localhost>',
            'Depends: libc6 (>= {}), zlib1g, python3 (>= 3.8), ca-certificates, adduser, apt, sudo, init-system-helpers (>= 1.54~), systemd (>= 243)'.format(manifest['minimum_glibc']),
            'Installed-Size: {}'.format((size + 1023) // 1024),
            'Description: Telegram link bot with broadcasts, subscription and SSH CLI',
            ' Includes the Linux executable, systemd service, source and local CLI.',
            ' Run url-bot for a numbered Russian administration menu.',
            '',
        ]))
        checksums = []
        for path in sorted(stage.rglob('*')):
            if path.is_file() and 'DEBIAN' not in path.relative_to(stage).parts:
                checksums.append('{}  {}'.format(hashlib.md5(path.read_bytes()).hexdigest(), path.relative_to(stage).as_posix()))
        write('DEBIAN/md5sums', '\n'.join(checksums) + '\n')
        output = ROOT / 'url_bot.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(stage), str(output)], check=True)
        print(output)
        return output


if __name__ == '__main__':
    build()
