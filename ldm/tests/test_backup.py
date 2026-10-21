"""Backup and restore: plain and encrypted, scheduled, pruning, restoring into another database."""

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta

import pytest

from ldm import backup
from ldm.manager import InvalidArgument, Manager
from ldm.passwords import MemoryPasswords
from ldm.store import Store

from test_manager import run


def new_manager(path, clock=None, passwords=None):
    store = Store(path)
    store.set_setting("download_dir", str(path.parent / "dl"))
    extra = {"clock": clock} if clock else {}
    return Manager(store, passwords=passwords or MemoryPasswords(), **extra)


def filled(tmp_path):
    manager = new_manager(tmp_path / "one.db")
    manager.update_settings({"max_parallel": 5, "user_agent": "firefox", "proxy_mode": "none"})
    manager.set_host_rule("example.com", max_connections=2)
    manager.add("https://example.com/a.iso", start=False)
    done = manager.add("https://example.com/b.iso", start=False)
    manager.store.finish(done, "b.iso", 10)
    return manager


def test_plain_backup_and_restore(tmp_path):
    first = filled(tmp_path)

    async def make():
        first.set_login("example.com", "ann", "s3cret")
        return await first.create_backup(str(tmp_path / "backups"))

    path = run(first, make)
    assert os.path.basename(path).startswith("ldm-backup-") and path.endswith(".json")
    data = json.loads(open(path).read())
    assert "s3cret" not in open(path).read()  # passwords only go into encrypted backups
    assert data["settings"]["max_parallel"] == 5 and len(data["downloads"]) == 2

    second = new_manager(tmp_path / "two.db")
    second.add("https://example.com/a.iso", start=False)  # already there: not added twice

    async def restore():
        return await second.restore_backup(path)

    counts = run(second, restore)
    assert counts["downloads"] == 1 and counts["host_rules"] == 1 and counts["logins"] == 1
    assert second.store.get_settings()["max_parallel"] == 5
    assert [r.host for r in second.host_rules()] == ["example.com"]
    assert sorted(d.url for d in second.store.list()) == ["https://example.com/a.iso", "https://example.com/b.iso"]
    assert second.list_logins()[0]["username"] == "ann"


def test_encrypted_backup_carries_passwords(tmp_path):
    first = filled(tmp_path)

    async def make():
        first.set_login("example.com", "ann", "s3cret")
        first.update_settings({"proxy_password": "proxypw"})
        return await first.create_backup(str(tmp_path / "x.ldmbackup"), "pass phrase")

    path = run(first, make)
    blob = open(path, "rb").read()
    assert backup.is_encrypted(blob) and b"s3cret" not in blob and b"example.com" not in blob
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(backup.BackupError, match="wrong password"):
        backup.decode(blob, "nope")
    with pytest.raises(backup.BackupError, match="protected"):
        backup.decode(blob)

    passwords = MemoryPasswords()
    second = new_manager(tmp_path / "two.db", passwords=passwords)

    async def restore():
        with pytest.raises(InvalidArgument, match="wrong password"):
            await second.restore_backup(path, "nope")
        counts = await second.restore_backup(path, "pass phrase")
        await asyncio.sleep(0.1)  # passwords are saved in the background
        return counts

    counts = run(second, restore)
    assert counts["logins"] == 1
    assert passwords.get("login", "example.com") == "s3cret" and passwords.get("proxy", "proxy") == "proxypw"


def test_scheduled_backups_are_pruned(tmp_path):
    now = [datetime(2026, 10, 21, 12, 0)]
    manager = new_manager(tmp_path / "s.db", clock=lambda: now[0])
    folder = tmp_path / "auto"
    manager.update_settings({"backup_schedule": "daily", "backup_dir": str(folder), "backup_keep": 2})

    async def scenario():
        for day in range(4):
            now[0] = datetime(2026, 10, 21, 12, 0) + timedelta(days=day)
            manager._scheduled_backup()
            manager._scheduled_backup()  # same day: nothing new
            await asyncio.sleep(1.1)  # file names have one-second resolution
        await asyncio.sleep(0.2)

    run(manager, scenario)
    assert len(list(folder.glob("ldm-backup-*.json"))) == 2


@pytest.mark.parametrize("blob", [b"not json", b'{"format": "other"}', b'{"format": "ldm-backup", "version": 9}'])
def test_bad_backup_files(blob):
    with pytest.raises(backup.BackupError):
        backup.decode(blob)
