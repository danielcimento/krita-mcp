"""Pin the behaviour of kritamcp.paths: resolve_allowed_path and
resolve_output_dir, messages and D2 error codes.

Every test uses temp dirs as roots, never the real output dir or config
file. Expected values are realpath'd, since macOS temp dirs live under
/private.
"""

import contextlib
import os

import pytest

from kritamcp import config, paths
from kritamcp.errors import CommandError


@pytest.fixture(autouse=True)
def no_user_config(tmp_path, monkeypatch):
    """Point config.current() at a missing config file and a temp output dir."""
    monkeypatch.setenv(config.CONFIG_FILE_ENV, str(tmp_path / "no-config.json"))
    monkeypatch.delenv(config.ALLOWED_DIRS_ENV, raising=False)
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", str(tmp_path / "default-out"))
    config.reset_cache()
    yield
    config.reset_cache()


@contextlib.contextmanager
def refused(code, match):
    """pytest.raises for a CommandError with this code and message regex."""
    with pytest.raises(CommandError, match=match) as info:
        yield info
    assert info.value.code == code


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
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", root)
    assert paths.resolve_allowed_path("a.png") == os.path.join(root, "a.png")
    with refused("forbidden_path", "outside"):
        paths.resolve_allowed_path("/etc/a.png")


def test_default_roots_include_env_roots(tmp_path, root, monkeypatch):
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", root)
    monkeypatch.setenv(config.ALLOWED_DIRS_ENV, str(extra))
    assert paths.resolve_allowed_path(str(extra / "a.png")) == os.path.join(os.path.realpath(extra), "a.png")
    assert paths.resolve_output_dir(str(extra / "d")) == os.path.join(os.path.realpath(extra), "d")


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
    with refused("forbidden_path", "^Path is outside the allowed directories: /etc/a.png$"):
        resolve("/etc/a.png", root)
    with refused("forbidden_path", "outside the allowed directories"):
        resolve(str(tmp_path / "a.png"), root)


def test_dotdot_escape_is_refused(root):
    with refused("forbidden_path", "outside the allowed directories"):
        resolve(os.path.join(root, "..", "x.png"), root)
    with refused("forbidden_path", "outside the allowed directories"):
        resolve("../x.png", root)


def test_symlink_pointing_outside_is_refused(tmp_path, root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.png").write_bytes(b"")
    os.symlink(outside / "x.png", os.path.join(root, "link.png"))
    os.symlink(outside, os.path.join(root, "linkdir"))
    with refused("forbidden_path", "outside the allowed directories"):
        resolve("link.png", root)
    with refused("forbidden_path", "outside the allowed directories"):
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
    with refused("forbidden_path", "outside the allowed directories"):
        resolve(str(sibling / "a.png"), root)


def test_second_root_is_allowed(tmp_path, root):
    other = tmp_path / "other"
    other.mkdir()
    got = paths.resolve_allowed_path(str(other / "a.png"), roots=[root, str(other)], base_dir=root)
    assert got == os.path.join(os.path.realpath(other), "a.png")


@pytest.mark.parametrize("name", ["a.txt", "a", "a.png.txt", ".png"])
def test_disallowed_extension_is_refused(root, name):
    with refused("forbidden_path", f"^File type not allowed: {name}$"):
        resolve(name, root)


@pytest.mark.parametrize("name", ["a.PNG", "a.Jpg", "a.kra", "a.ora", "a.TIFF", "a.psd"])
def test_extension_check_is_case_insensitive(root, name):
    assert resolve(name, root) == os.path.join(root, name)


def test_root_itself_is_refused_for_lack_of_extension(root):
    with refused("forbidden_path", "File type not allowed"):
        resolve(root, root)


def test_outside_check_comes_before_extension_check(root):
    with refused("forbidden_path", "outside the allowed directories"):
        resolve("/etc/passwd", root)


def test_must_exist_missing_file_is_refused(root):
    with refused("not_found", "^File not found: a.png$"):
        resolve("a.png", root, must_exist=True)


def test_must_exist_directory_is_refused(root):
    os.mkdir(os.path.join(root, "x.png"))
    with refused("not_found", "^File not found: x.png$"):
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
    with refused("bad_request", "^No path specified$"):
        resolve(bad, root)


# --- .json manifests ---------------------------------------------------

@pytest.mark.parametrize("name", ["manifest.json", "M.JSON"])
def test_json_refused_by_default(root, name):
    with refused("forbidden_path", f"^File type not allowed: {name}$"):
        resolve(name, root)


@pytest.mark.parametrize("name", ["manifest.json", "sub/M.JSON"])
def test_json_allowed_with_allow_json(root, name):
    got = paths.resolve_allowed_path(name, roots=[root], base_dir=root, allow_json=True)
    assert got == os.path.join(root, name)


def test_allow_json_still_allows_images_and_refuses_others(root):
    assert paths.resolve_allowed_path("a.png", roots=[root], base_dir=root, allow_json=True)
    with refused("forbidden_path", "File type not allowed"):
        paths.resolve_allowed_path("a.txt", roots=[root], base_dir=root, allow_json=True)


def test_allow_json_keeps_containment_and_must_exist(root):
    with refused("forbidden_path", "outside"):
        paths.resolve_allowed_path("/etc/x.json", roots=[root], base_dir=root, allow_json=True)
    with refused("not_found", "^File not found: m.json$"):
        paths.resolve_allowed_path("m.json", True, roots=[root], base_dir=root, allow_json=True)


# --- resolve_output_dir ------------------------------------------------

def outdir(path, root, **kw):
    return paths.resolve_output_dir(path, roots=[root], base_dir=root, **kw)


def test_output_dir_is_created(root):
    got = outdir("exp", root)
    assert got == os.path.join(root, "exp") and os.path.isdir(got)


def test_output_dir_nested_create(root):
    got = outdir("a/b/c", root)
    assert got == os.path.join(root, "a", "b", "c") and os.path.isdir(got)


def test_output_dir_no_create_missing_is_not_found(root):
    with refused("not_found", "^Directory not found: exp$"):
        outdir("exp", root, create=False)
    assert not os.path.exists(os.path.join(root, "exp"))


def test_output_dir_existing_empty_is_ok(root):
    os.mkdir(os.path.join(root, "exp"))
    assert outdir("exp", root) == os.path.join(root, "exp")
    assert outdir("exp", root, create=False) == os.path.join(root, "exp")


def test_output_dir_existing_non_empty_is_conflict(root):
    os.mkdir(os.path.join(root, "exp"))
    open(os.path.join(root, "exp", "old.png"), "wb").close()
    with refused("conflict", "^Output directory is not empty: exp .*overwrite"):
        outdir("exp", root)


def test_output_dir_non_empty_with_overwrite_is_ok(root):
    os.mkdir(os.path.join(root, "exp"))
    open(os.path.join(root, "exp", "old.png"), "wb").close()
    assert outdir("exp", root, overwrite=True) == os.path.join(root, "exp")


def test_output_dir_file_in_the_way_is_conflict(root):
    open(os.path.join(root, "exp"), "wb").close()
    with refused("conflict", "^Not a directory: exp$"):
        outdir("exp", root)
    with refused("conflict", "Not a directory"):
        outdir("exp", root, overwrite=True)


def test_output_dir_file_as_parent_fails_without_escaping(root):
    open(os.path.join(root, "f"), "wb").close()
    with refused("krita_error", "^Cannot create directory f/sub"):
        outdir("f/sub", root)


def test_output_dir_symlink_escape_is_forbidden(tmp_path, root):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, os.path.join(root, "escape"))
    with refused("forbidden_path", "outside the allowed directories"):
        outdir("escape/out", root)
    with refused("forbidden_path", "outside the allowed directories"):
        outdir("escape", root)
    assert os.listdir(outside) == []


def test_output_dir_symlink_inside_is_resolved(root):
    os.mkdir(os.path.join(root, "real"))
    os.symlink(os.path.join(root, "real"), os.path.join(root, "alias"))
    assert outdir("alias/new", root) == os.path.join(root, "real", "new")


def test_output_dir_dotdot_escape_is_forbidden(tmp_path, root):
    with refused("forbidden_path", "outside the allowed directories"):
        outdir("../exp", root)
    with refused("forbidden_path", "outside the allowed directories"):
        outdir(os.path.join(root, "..", "exp"), root)
    assert not os.path.exists(tmp_path / "exp")


def test_output_dir_root_itself_is_like_any_dir(root):
    assert outdir(root, root) == root  # empty
    open(os.path.join(root, "a.png"), "wb").close()
    with refused("conflict", "not empty"):
        outdir(root, root)
    assert outdir(root, root, overwrite=True) == root


def test_output_dir_has_no_extension_check(root):
    assert outdir("layers.png", root) == os.path.join(root, "layers.png")
    assert outdir("plain", root) == os.path.join(root, "plain")


def test_output_dir_escape_detected_after_create(tmp_path, root, monkeypatch):
    # Simulate the race: makedirs lands on a symlink swapped in after the check.
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def racy_makedirs(path, exist_ok=False):
        os.symlink(outside, path)

    monkeypatch.setattr(paths.os, "makedirs", racy_makedirs)
    with refused("forbidden_path", "outside the allowed directories"):
        outdir("exp", root)


@pytest.mark.parametrize("bad", ["", None, 42])
def test_output_dir_empty_or_non_string_is_bad_request(root, bad):
    with refused("bad_request", "^No path specified$"):
        outdir(bad, root)
