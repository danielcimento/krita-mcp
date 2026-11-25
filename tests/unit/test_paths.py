"""Pin the current behaviour of kritamcp.paths.resolve_allowed_path.

Every test uses temp dirs as roots, never the real output dir. Expected
values are realpath'd, since macOS temp dirs live under /private.
"""

import os

import pytest

from kritamcp import config, paths
from kritamcp.errors import CommandError


@pytest.fixture
def root(tmp_path):
    """An allowed root (realpath'd) inside the temp dir."""
    r = tmp_path / "out"
    r.mkdir()
    return os.path.realpath(r)


def resolve(path, root, must_exist=False):
    return paths.resolve_allowed_path(path, must_exist, roots=[root], base_dir=root)


def test_relative_path_resolves_under_base_dir(root):
    assert resolve("a.png", root) == os.path.join(root, "a.png")
    assert resolve("sub/b.png", root) == os.path.join(root, "sub", "b.png")


def test_default_roots_and_base_dir_come_from_config_at_call_time(root, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_DIRS", [root])
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", root)
    assert paths.resolve_allowed_path("a.png") == os.path.join(root, "a.png")
    with pytest.raises(CommandError):
        paths.resolve_allowed_path("/etc/a.png")


def test_tilde_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "out").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = os.path.realpath(home / "out")
    assert resolve("~/out/a.png", root) == os.path.join(root, "a.png")


def test_absolute_path_inside_root_is_returned_realpathed(tmp_path, root):
    # tmp_path itself may not be realpath'd (/var -> /private/var on macOS).
    given = os.path.join(str(tmp_path), "out", "x", "..", "a.png")
    assert resolve(given, root) == os.path.join(root, "a.png")


def test_path_outside_root_is_refused(tmp_path, root):
    with pytest.raises(CommandError, match="^Path is outside the allowed directories: /etc/a.png$"):
        resolve("/etc/a.png", root)
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve(str(tmp_path / "a.png"), root)


def test_dotdot_escape_is_refused(root):
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve(os.path.join(root, "..", "x.png"), root)
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve("../x.png", root)


def test_symlink_pointing_outside_is_refused(tmp_path, root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.png").write_bytes(b"")
    os.symlink(outside / "x.png", os.path.join(root, "link.png"))
    os.symlink(outside, os.path.join(root, "linkdir"))
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve("link.png", root)
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve("linkdir/new.png", root)


def test_symlink_pointing_inside_is_resolved(root):
    os.mkdir(os.path.join(root, "real"))
    target = os.path.join(root, "real", "x.png")
    open(target, "wb").close()
    os.symlink(target, os.path.join(root, "link.png"))
    assert resolve("link.png", root, must_exist=True) == target


def test_root_given_through_symlink_is_realpathed(tmp_path, root):
    alias = tmp_path / "alias"
    os.symlink(root, alias)
    got = paths.resolve_allowed_path(str(alias / "a.png"), roots=[str(alias)], base_dir=str(alias))
    assert got == os.path.join(root, "a.png")


def test_sibling_dir_with_shared_prefix_is_not_inside(tmp_path, root):
    sibling = tmp_path / "out2"
    sibling.mkdir()
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve(str(sibling / "a.png"), root)


def test_second_root_is_allowed(tmp_path, root):
    other = tmp_path / "other"
    other.mkdir()
    got = paths.resolve_allowed_path(str(other / "a.png"), roots=[root, str(other)], base_dir=root)
    assert got == os.path.join(os.path.realpath(other), "a.png")


@pytest.mark.parametrize("name", ["a.txt", "a", "a.png.txt", ".png"])
def test_disallowed_extension_is_refused(root, name):
    with pytest.raises(CommandError, match=f"^File type not allowed: {name}$"):
        resolve(name, root)


@pytest.mark.parametrize("name", ["a.PNG", "a.Jpg", "a.kra", "a.ora", "a.TIFF", "a.psd"])
def test_extension_check_is_case_insensitive(root, name):
    assert resolve(name, root) == os.path.join(root, name)


def test_root_itself_is_refused_for_lack_of_extension(root):
    with pytest.raises(CommandError, match="File type not allowed"):
        resolve(root, root)


def test_outside_check_comes_before_extension_check(root):
    with pytest.raises(CommandError, match="outside the allowed directories"):
        resolve("/etc/passwd", root)


def test_must_exist_missing_file_is_refused(root):
    with pytest.raises(CommandError, match="^File not found: a.png$"):
        resolve("a.png", root, must_exist=True)


def test_must_exist_directory_is_refused(root):
    os.mkdir(os.path.join(root, "x.png"))
    with pytest.raises(CommandError, match="^File not found: x.png$"):
        resolve("x.png", root, must_exist=True)


def test_must_exist_existing_file_is_ok(root):
    target = os.path.join(root, "a.png")
    open(target, "wb").close()
    assert resolve("a.png", root, must_exist=True) == target


def test_missing_file_ok_without_must_exist(root):
    assert not os.path.exists(os.path.join(root, "nope", "a.png"))
    assert resolve("nope/a.png", root) == os.path.join(root, "nope", "a.png")


@pytest.mark.parametrize("bad", ["", None, 42, b"a.png", ["a.png"]])
def test_empty_or_non_string_is_refused(root, bad):
    with pytest.raises(CommandError, match="^No path specified$"):
        resolve(bad, root)
