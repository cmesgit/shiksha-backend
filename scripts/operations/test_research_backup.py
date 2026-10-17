import gzip
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('research_backup', Path(__file__).with_name('research_backup.py'))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class RecoveryBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'private'
        self.database = self.root / 'database'
        self.destination = self.root / 'backups'
        self.source.mkdir()
        self.database.mkdir()
        with gzip.open(self.database / 'shiksha-fixture.sql.gz', 'wb') as stream:
            stream.write(b'-- disposable database backup\n')

    def create(self):
        return ops.backup(self.source, self.database, self.destination)

    def test_round_trip_with_database_and_private_pdf(self):
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'paper.pdf').write_bytes(b'%PDF-1.7\ndisposable')
        output = self.create()
        target = self.root / 'restore'
        ops.verify(output, target)
        self.assertEqual((target / 'manuscripts/nested/paper.pdf').read_bytes(), b'%PDF-1.7\ndisposable')
        self.assertTrue((target / 'database/shiksha-fixture.sql.gz').is_file())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            ops.verify(output, target)

    def test_empty_private_directory_still_backs_up_database(self):
        result = ops.verify(self.create())
        self.assertEqual(len(result['files']), 1)

    def test_symlink_rejected(self):
        (self.source / 'escape.pdf').symlink_to(self.database / 'shiksha-fixture.sql.gz')
        with self.assertRaises(ValueError):
            self.create()

    def test_missing_database_fails_without_archive(self):
        (self.database / 'shiksha-fixture.sql.gz').unlink()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(list(self.destination.glob('*.tar.gz')), [])

    def test_tampered_checksum_rejected_before_restore(self):
        archive = self.root / 'tampered.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name, payload in [('paper.pdf', b'changed'), ('manifest.json', json.dumps({'files': {'paper.pdf': {'sha256': 'bad', 'bytes': 7}}}).encode())]:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        target = self.root / 'restore'
        with self.assertRaises(ValueError):
            ops.verify(archive, target)
        self.assertFalse(target.exists())

    def test_path_escape_rejected(self):
        archive = self.root / 'unsafe.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            for name in ['../escape', 'manifest.json']:
                info = tarfile.TarInfo(name)
                info.size = 2
                bundle.addfile(info, io.BytesIO(b'{}'))
        with self.assertRaises(ValueError):
            ops.verify(archive)


if __name__ == '__main__':
    unittest.main()
