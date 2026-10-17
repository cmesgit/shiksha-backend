#!/usr/bin/env python3
"""Root-only local research recovery bundles; never restores into the live app.

Run after the existing database backup. Bundles include that database dump,
immutable private manuscripts, and SHA-256 metadata. Off-host replication is
separate: a copy on the application disk cannot survive loss of that disk.
"""
import argparse
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from datetime import datetime, timezone, timedelta


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(archive, restore_to=None):
    """Validate all members before optionally restoring into a new directory."""
    with tarfile.open(archive, 'r:gz') as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or 'manifest.json' not in names:
            raise ValueError('Missing manifest or duplicate archive members')
        for member in members:
            name = PurePosixPath(member.name)
            if not member.isfile() or name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe archive member')
        manifest = json.load(bundle.extractfile('manifest.json'))
        expected = manifest['files']
        if set(names) != set(expected) | {'manifest.json'}:
            raise ValueError('Archive does not match manifest')
        for name, entry in expected.items():
            h = hashlib.sha256()
            size = 0
            with bundle.extractfile(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(chunk)
                    size += len(chunk)
            if h.hexdigest() != entry['sha256'] or size != entry['bytes']:
                raise ValueError('Archive checksum or size mismatch')
        if restore_to is not None:
            target = Path(restore_to)
            target.mkdir(mode=0o700, parents=False, exist_ok=False)
            for member in members:
                dest = target / member.name
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with bundle.extractfile(member) as src, dest.open('xb') as out:
                    shutil.copyfileobj(src, out)
                dest.chmod(0o600)
        return manifest


def backup(source, database_dir, destination, retention=14):
    source, database_dir, destination = map(Path, (source, database_dir, destination))
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Private manuscript directory is missing or is a symlink')
    source = source.resolve()
    if destination.is_symlink():
        raise ValueError('Backup destination must not be a symlink')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    if destination.resolve().is_relative_to(source):
        raise ValueError('Backup destination must be outside private storage')
    dumps = sorted(database_dir.glob('shiksha-*.sql.gz'), key=lambda p: p.stat().st_mtime)
    if not dumps or datetime.now(timezone.utc).timestamp() - dumps[-1].stat().st_mtime > 3600:
        raise ValueError('A successful database backup from the last hour is required')
    database = dumps[-1]
    if database.is_symlink():
        raise ValueError('Database dump must not be a symlink')
    with gzip.open(database, 'rb') as stream:
        if not stream.read(1):
            raise ValueError('Empty database backup')
        while stream.read(1024 * 1024):
            pass
    with (destination / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        paths = [(database, 'database/' + database.name)]
        for path in sorted(source.rglob('*')):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('Unsupported file in private storage')
            if path.is_file():
                paths.append((path, 'manuscripts/' + path.relative_to(source).as_posix()))
        now = datetime.now(timezone.utc)
        manifest = {'format': 1, 'created_at': now.isoformat(), 'source': str(source),
                    'database': database.name, 'files': {}}
        output = destination / ('research-' + now.strftime('%Y%m%dT%H%M%S%fZ') + '.tar.gz')
        fd, tmp = tempfile.mkstemp(prefix='.research-', suffix='.partial', dir=destination)
        os.close(fd)
        try:
            with tarfile.open(tmp, 'w:gz') as bundle:
                for path, name in paths:
                    before = path.stat()
                    manifest['files'][name] = {'sha256': digest(path), 'bytes': before.st_size}
                    bundle.add(path, arcname=name, recursive=False)
                    after = path.stat()
                    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                        raise ValueError('A source file changed during backup')
                payload = json.dumps(manifest, sort_keys=True).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size, info.mode = len(payload), 0o600
                bundle.addfile(info, io.BytesIO(payload))
            verify(tmp)
            os.replace(tmp, output)
        finally:
            Path(tmp).unlink(missing_ok=True)
        cutoff = now - timedelta(days=retention)
        for old in destination.glob('research-*.tar.gz'):
            if old != output and old.stat().st_mtime < cutoff.timestamp():
                old.unlink()
        print(json.dumps({'archive': str(output), 'manuscripts': len(paths) - 1,
                          'database_included': True, 'verified': True}))
        return output


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--source', required=True)
    create.add_argument('--database-dir', default='/var/backups/shiksha-db')
    create.add_argument('--destination', default='/var/backups/shiksha-research')
    create.add_argument('--retention-days', type=int, default=14)
    check = commands.add_parser('verify')
    check.add_argument('archive')
    check.add_argument('--restore-to', help='New directory only; never the live manuscript directory')
    args = parser.parse_args()
    if args.command == 'create':
        if args.retention_days < 1:
            parser.error('Retention must be at least one day')
        backup(args.source, args.database_dir, args.destination, args.retention_days)
    else:
        manifest = verify(args.archive, args.restore_to)
        print(json.dumps({'verified': True, 'files': len(manifest['files']),
                          'restored': bool(args.restore_to)}))


if __name__ == '__main__':
    main()
