#!/usr/bin/env python3
"""Build a Linux amd64 binary and Debian package directly from the public sources."""
import ast
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig

from build_deb import ROOT, build, sha256


def main():
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise SystemExit('Build on Linux x86_64 (including WSL).')
    for command in ('dpkg-deb', 'readelf', 'sh'):
        if not shutil.which(command):
            raise SystemExit('Missing build tool: ' + command)
    version = (ROOT / 'VERSION').read_text().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('VERSION must contain a numeric MAJOR.MINOR.PATCH version')
    build_dir = ROOT / '.build/release'
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--noupx', '--name', 'bot-linux-x86_64',
        '--distpath', str(build_dir / 'dist'), '--workpath', str(build_dir / 'work'),
        '--specpath', str(build_dir), str(ROOT / 'bot.py'),
    ], cwd=ROOT, check=True)
    binary = ROOT / 'bot-linux-x86_64'
    shutil.copyfile(build_dir / 'dist/bot-linux-x86_64', binary)
    binary.chmod(0o755)

    # Include every bundled native library when calculating the libc dependency.
    analysis = ast.literal_eval((build_dir / 'work/bot-linux-x86_64/Analysis-00.toc').read_text())
    native = {str(binary)}
    for group in analysis:
        if isinstance(group, list):
            for item in group:
                if isinstance(item, tuple) and len(item) == 3 and item[2] in ('BINARY', 'EXTENSION'):
                    native.add(item[1])
    versions = set()
    for name in sorted(native):
        output = subprocess.check_output(['readelf', '--version-info', name], text=True)
        versions.update(re.findall(r'GLIBC_([0-9.]+)', output))
    if not versions:
        raise ValueError('Cannot determine minimum glibc version')
    minimum_glibc = max(versions, key=lambda value: tuple(map(int, value.split('.'))))

    licenses = ROOT / '.build/third-party'
    licenses.mkdir(parents=True, exist_ok=True)
    python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError('Python LICENSE.txt is required: ' + str(python_license))
    shutil.copyfile(python_license, licenses / 'Python-LICENSE.txt')
    distribution = importlib.metadata.distribution('pyinstaller')
    pyinstaller_license = next(path for path in distribution.files if str(path).endswith('/COPYING.txt'))
    shutil.copyfile(distribution.locate_file(pyinstaller_license), licenses / 'PyInstaller-COPYING.txt')
    manifest = {
        'version': version,
        'python': platform.python_version(),
        'pyinstaller': distribution.version,
        'minimum_glibc': minimum_glibc,
        'source_sha256': sha256(ROOT / 'bot.py'),
        'binary_sha256': sha256(binary),
    }
    (ROOT / '.build/release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    package = build()
    (ROOT / 'SHA256SUMS').write_text(''.join('{}  {}\n'.format(sha256(path), path.name) for path in (package, binary)))
    print('Built URL Bot {} (glibc >= {}).'.format(version, minimum_glibc))


if __name__ == '__main__':
    main()
