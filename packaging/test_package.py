import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader('url_cli', str(ROOT / 'packaging/url-bot'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = self.directory / 'config.env'
        self.database = self.directory / 'bot.sqlite3'
        for key, value in [('CONFIG', self.config), ('DATABASE', self.database)]:
            mock = patch.object(cli, key, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_credential_validation(self):
        self.assertFalse(cli.valid_credentials({}))
        self.assertFalse(cli.valid_credentials({'BOT_TOKEN': '123:abc', 'ADMIN_ID': '-1'}))
        self.assertFalse(cli.valid_credentials({'BOT_TOKEN': '123:abc', 'ADMIN_ID': str(2**63)}))
        self.assertTrue(cli.valid_credentials({'BOT_TOKEN': '123:abc_DEF', 'ADMIN_ID': '42'}))

    def edit(self, field, value):
        with patch.object(cli, 'require_root'), patch.object(cli.grp, 'getgrnam') as group, patch.object(cli.os, 'fchown'), patch('builtins.input', return_value=value), patch.object(cli, 'service', return_value=0) as service:
            group.return_value.gr_gid = os.getgid()
            cli.configure(field)
            return service.call_args.args[0]

    def test_configure_is_atomic_and_preserves_blank_input(self):
        self.assertEqual(self.edit('token', '123:example'), 'stop')
        self.assertEqual(cli.read_config()['BOT_TOKEN'], '123:example')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.edit('admin', '42'), 'restart')
        self.assertEqual(self.edit('token', ''), 'restart')
        self.assertEqual(cli.read_config()['BOT_TOKEN'], '123:example')
        self.assertEqual(self.edit('admin', ''), 'restart')
        self.assertEqual(cli.read_config()['ADMIN_ID'], '42')
        old = self.config.read_bytes()
        with patch.object(cli, 'require_root'), patch('builtins.input', return_value='bad'), patch.object(cli, 'service') as service:
            with self.assertRaises(ValueError):
                cli.configure('token')
            service.assert_not_called()
        self.assertEqual(self.config.read_bytes(), old)

    def test_menu_and_uninstall(self):
        with patch.object(cli, 'require_root'), patch('builtins.input', side_effect=['1', '2', '0']), patch.object(cli, 'configure') as edit:
            self.assertEqual(cli.menu(), 0)
            self.assertEqual([call.args for call in edit.call_args_list], [('token',), ('admin',)])
        with patch.object(cli, 'require_root'), patch('builtins.input', return_value='нет'), patch.object(cli, 'service') as service, patch.object(cli.subprocess, 'run') as run:
            self.assertFalse(cli.uninstall())
            service.assert_not_called()
            run.assert_not_called()
        with patch.object(cli, 'require_root'), patch('builtins.input', return_value='ДА'), patch.object(cli, 'service', return_value=1), patch.object(cli.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                cli.uninstall()
            run.assert_not_called()
        with patch.object(cli, 'require_root'), patch('builtins.input', return_value='ДА'), patch.object(cli, 'service', return_value=0), patch.object(cli.subprocess, 'run') as run:
            run.return_value.returncode = 0
            self.assertTrue(cli.uninstall())
            run.assert_called_once_with(['apt-get', 'purge', '--yes', 'url-bot'], check=False)

    def test_purge_cleanup_in_isolated_directory(self):
        data = self.directory / 'data'
        data.mkdir()
        (data / 'bot.sqlite3').write_text('example')
        outside = self.directory / 'outside'
        outside.mkdir()
        marker = outside / 'keep'
        marker.write_text('keep')
        config_link = self.directory / 'configuration'
        config_link.symlink_to(outside, target_is_directory=True)
        script = (ROOT / 'packaging/postrm').read_text().split("python3 - <<'PY'\n", 1)[1].split('\nPY', 1)[0]
        with patch('pathlib.Path', side_effect=lambda name: {'/var/lib/url-bot': data, '/etc/url-bot': config_link}[name]):
            exec(compile(script, 'postrm-cleanup', 'exec'), {})
        self.assertFalse(data.exists())
        self.assertFalse(config_link.is_symlink())
        self.assertTrue(marker.is_file())

    def test_backup_preserves_live_database_and_rejects_overwrite(self):
        db = sqlite3.connect(str(self.database))
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE example(value TEXT)')
        db.execute("INSERT INTO example VALUES('saved')")
        db.commit()
        destination = self.directory / 'backup.sqlite3'
        with patch.object(cli, 'require_root'):
            cli.backup(str(destination))
            with self.assertRaises(FileExistsError):
                cli.backup(str(destination))
        copy = sqlite3.connect(str(destination))
        try:
            self.assertEqual(copy.execute('SELECT value FROM example').fetchone(), ('saved',))
        finally:
            copy.close()
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)


class PackageTests(unittest.TestCase):
    def test_package_contents_and_binary_startup(self):
        package = ROOT / 'url_bot.deb'
        self.assertEqual(subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package'], text=True).strip(), 'url-bot')
        self.assertEqual(subprocess.check_output(['dpkg-deb', '-f', str(package), 'Version'], text=True).strip(), (ROOT / 'VERSION').read_text().strip())
        self.assertEqual(subprocess.check_output(['dpkg-deb', '-f', str(package), 'Architecture'], text=True).strip(), 'amd64')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['dpkg-deb', '-R', str(package), directory], check=True)
            binary = root / 'usr/lib/url-bot/bot-linux-x86_64'
            self.assertEqual(hashlib.sha256(binary.read_bytes()).digest(), hashlib.sha256((ROOT / 'bot-linux-x86_64').read_bytes()).digest())
            config = root / 'etc/url-bot/config.env'
            self.assertEqual(config.read_text(), 'BOT_TOKEN=\nADMIN_ID=\nDATABASE_PATH=/var/lib/url-bot/bot.sqlite3\n')
            self.assertEqual(config.stat().st_mode & 0o777, 0o640)
            document = root / 'usr/share/doc/url-bot'
            self.assertEqual((document / 'source/bot.py').read_bytes(), (ROOT / 'bot.py').read_bytes())
            self.assertEqual((document / 'copyright').read_bytes(), (ROOT / 'LICENSE').read_bytes())
            manifest = json.loads((document / 'build.json').read_text())
            self.assertEqual(manifest['binary_sha256'], hashlib.sha256(binary.read_bytes()).hexdigest())
            self.assertEqual(manifest['source_sha256'], hashlib.sha256((ROOT / 'bot.py').read_bytes()).hexdigest())
            for name in ('Python-LICENSE.txt', 'PyInstaller-COPYING.txt'):
                self.assertGreater((document / 'licenses' / name).stat().st_size, 100)
            for script in ('postinst', 'prerm', 'postrm'):
                subprocess.run(['sh', '-n', str(root / 'DEBIAN' / script)], check=True)
            for line in (root / 'DEBIAN/md5sums').read_text().splitlines():
                expected, name = line.split('  ', 1)
                self.assertEqual(hashlib.md5((root / name).read_bytes()).hexdigest(), expected)
            result = subprocess.run([str(binary)], env=dict(os.environ, BOT_TOKEN='invalid', ADMIN_ID='1'), capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('Set a valid BOT_TOKEN', result.stderr)
            result = subprocess.run([str(root / 'usr/bin/url-bot'), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('url-bot', result.stdout)
            self.assertNotIn('configure', result.stdout)
            # Validate the actual unit in an isolated root, without installing or starting it.
            for name in ('sysinit.target', 'basic.target', 'shutdown.target', 'network-online.target'):
                (root / 'usr/lib/systemd/system' / name).write_text('[Unit]\nDescription=Test target\nDefaultDependencies=no\n')
            verification = subprocess.run(['systemd-analyze', '--root=' + str(root), 'verify', 'url-bot.service'], capture_output=True, text=True)
            self.assertEqual(verification.returncode, 0, verification.stderr)


if __name__ == '__main__':
    unittest.main()
