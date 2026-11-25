"""The live suite's config_override backup/restore (tests/live/conftest.py),
on temp files: it guards the user's real config file, so it is checked
offline here."""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parents[1] / "live" / "conftest.py"
_spec = importlib.util.spec_from_file_location("kmcp_live_conftest", _CONFTEST)
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_existing_file_restored_exactly(tmp_path):
    path = tmp_path / "config.json"
    original = b'{"allowed_dirs": ["~/art"]}\n'
    path.write_bytes(original)
    os.chmod(path, 0o640)
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    backups = tmp_path / "backups"

    override = live.ConfigOverride(str(path), str(backups))
    assert override.original == {"allowed_dirs": ["~/art"]}
    backup = list(backups.iterdir())
    assert len(backup) == 1 and backup[0].read_bytes() == original and mode(backup[0]) == 0o600

    override.write({"limits": {"max_canvas_pixels": 5}})
    assert json.loads(path.read_text()) == {"limits": {"max_canvas_pixels": 5}}
    assert mode(path) == 0o600
    override.chmod(0o666)
    assert mode(path) == 0o666

    override.restore()
    assert path.read_bytes() == original
    assert mode(path) == 0o640
    assert os.stat(path).st_mtime_ns == 2_000_000_000
    assert list(backups.iterdir()) == []
    override.restore()  # idempotent
    assert path.read_bytes() == original


def test_absent_file_is_removed_again(tmp_path):
    path = tmp_path / "config.json"
    override = live.ConfigOverride(str(path), str(tmp_path / "backups"))
    assert override.original == {}
    override.write({"allowed_dirs": []}, mode=0o666)
    assert mode(path) == 0o666
    override.restore()
    assert not path.exists()
    assert not (tmp_path / "backups").exists()


def test_restore_after_an_interrupt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    os.chmod(path, 0o600)
    override = live.ConfigOverride(str(path), str(tmp_path / "b"))
    with pytest.raises(KeyboardInterrupt):
        try:
            override.write({"x": 1})
            raise KeyboardInterrupt
        finally:
            override.restore()
    assert path.read_text() == "{}"


def test_symlink_is_not_overridden(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "config.json"
    link.symlink_to(target)
    with pytest.raises(pytest.skip.Exception):
        live.ConfigOverride(str(link), str(tmp_path / "b"))
    assert target.read_text() == "{}"
