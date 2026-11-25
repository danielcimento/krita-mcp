"""kritamcp.config.load() and current() (D7, D8).

Temp dirs everywhere: config_file, output_dir, home and env are always
passed (or pointed) at tmp_path, never the user's real files.
"""

import json
import os

import pytest

from kritamcp import config


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return os.path.realpath(h)


@pytest.fixture
def cfg(tmp_path):
    """Path of the config file (not created)."""
    return str(tmp_path / "config.json")


@pytest.fixture
def out(tmp_path):
    return os.path.realpath(tmp_path) + "/out"  # need not exist


def write(path, data, mode=0o600):
    with open(path, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    os.chmod(path, mode)


def mkdir(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True)
    return os.path.realpath(d)


def load(cfg, out, home, env=None):
    return config.load(config_file=cfg, env=env or {}, output_dir=out, home=home)


# --- load(): sources and roots -----------------------------------------

def test_no_file_gives_defaults(cfg, out, home):
    s = load(cfg, out, home)
    assert s.roots == (out,)
    assert s.output_dir == out
    assert dict(s.limits) == config.DEFAULT_LIMITS
    assert dict(s.journal) == config.DEFAULT_JOURNAL
    assert s.errors == ()
    assert s.config_loaded is False
    assert s.config_file == cfg


def test_settings_are_immutable(cfg, out, home):
    s = load(cfg, out, home)
    with pytest.raises(AttributeError):
        s.roots = ()
    with pytest.raises(TypeError):
        s.limits["max_canvas_dim"] = 1


def test_file_and_env_roots_combined_in_order_and_deduped(tmp_path, cfg, out, home):
    a, b, c = (mkdir(tmp_path, n) for n in "abc")
    write(cfg, {"allowed_dirs": [a, b, a + "/", out]})
    s = load(cfg, out, home, env={config.ALLOWED_DIRS_ENV: os.pathsep.join([c, b, ""])})
    assert s.roots == (out, a, b, c)
    assert s.errors == ()
    assert s.config_loaded is True
    assert s.requested_roots == (a, b, a + "/", out, c, b)


def test_tilde_is_expanded(tmp_path, cfg, out, home, monkeypatch):
    monkeypatch.setenv("HOME", home)
    os.mkdir(os.path.join(home, "art"))
    write(cfg, {"allowed_dirs": ["~/art"]})
    assert load(cfg, out, home).roots == (out, os.path.join(home, "art"))


def test_output_dir_is_realpathed_and_tilde_expanded(tmp_path, cfg, home, monkeypatch):
    monkeypatch.setenv("HOME", home)
    os.symlink(home, tmp_path / "alias")
    s = config.load(config_file=cfg, env={}, output_dir=str(tmp_path / "alias" / "out"), home=home)
    assert s.roots == (os.path.join(home, "out"),)
    assert config.load(config_file=cfg, env={}, output_dir="~/out", home=home).output_dir == os.path.join(home, "out")


def test_symlinked_root_is_resolved(tmp_path, cfg, out, home):
    real = mkdir(tmp_path, "real")
    os.symlink(real, tmp_path / "link")
    write(cfg, {"allowed_dirs": [str(tmp_path / "link"), real]})
    assert load(cfg, out, home).roots == (out, real)


@pytest.mark.parametrize("which, reason", [
    ("missing", "does not exist"),
    ("file", "not a directory"),
    ("slash", "too broad (filesystem root)"),
    ("home", "too broad (home directory)"),
    ("above_home", "too broad (contains the home directory)"),
    ("relative", "not an absolute path"),
    ("empty", "not an absolute path"),
])
def test_invalid_roots_are_skipped_with_errors(tmp_path, cfg, out, home, which, reason):
    (tmp_path / "f").write_text("")
    raw = {"missing": str(tmp_path / "nope"), "file": str(tmp_path / "f"), "slash": "/",
           "home": home + "/.", "above_home": os.path.dirname(home), "relative": "art", "empty": ""}[which]
    good = mkdir(tmp_path, "good")
    write(cfg, {"allowed_dirs": [raw, good]})
    s = load(cfg, out, home)
    assert s.roots == (out, good)
    assert s.errors == (f"allowed root {raw!r} skipped: {reason}",)


def test_invalid_env_root_is_skipped_with_error(tmp_path, cfg, out, home):
    s = load(cfg, out, home, env={config.ALLOWED_DIRS_ENV: "/"})
    assert s.roots == (out,)
    assert s.errors == ("allowed root '/' skipped: too broad (filesystem root)",)


def test_home_via_symlink_is_still_refused(tmp_path, cfg, out, home):
    os.symlink(home, tmp_path / "homelink")
    s = load(cfg, out, home, env={config.ALLOWED_DIRS_ENV: str(tmp_path / "homelink")})
    assert s.roots == (out,) and "too broad (home directory)" in s.errors[0]


def test_dir_inside_home_is_fine(tmp_path, cfg, out, home):
    art = mkdir(tmp_path, "home/art")
    assert load(cfg, out, home, env={config.ALLOWED_DIRS_ENV: art}).roots == (out, art)


# --- load(): the file itself -------------------------------------------

@pytest.mark.parametrize("mode, what", [(0o602, "world"), (0o620, "group"), (0o666, "both")])
def test_writable_file_is_ignored_entirely(tmp_path, cfg, out, home, mode, what):
    a = mkdir(tmp_path, "a")
    write(cfg, {"allowed_dirs": [a], "limits": {"max_canvas_dim": 100}}, mode=mode)
    s = load(cfg, out, home)
    assert s.roots == (out,)
    assert dict(s.limits) == config.DEFAULT_LIMITS
    assert s.config_loaded is False
    assert len(s.errors) == 1 and "group- or world-writable" in s.errors[0] and cfg in s.errors[0]


def test_env_roots_still_apply_when_file_is_ignored(tmp_path, cfg, out, home):
    a, b = mkdir(tmp_path, "a"), mkdir(tmp_path, "b")
    write(cfg, {"allowed_dirs": [a]}, mode=0o666)
    s = load(cfg, out, home, env={config.ALLOWED_DIRS_ENV: b})
    assert s.roots == (out, b)


def test_file_not_owned_by_user_is_ignored(tmp_path, cfg, out, home, monkeypatch):
    write(cfg, {"allowed_dirs": [mkdir(tmp_path, "a")]})
    monkeypatch.setattr(config.os, "getuid", lambda: os.stat(cfg).st_uid + 1)
    s = load(cfg, out, home)
    assert s.roots == (out,) and not s.config_loaded
    assert "not owned by the current user" in s.errors[0]


def test_symlinked_file_checks_the_target_mode(tmp_path, cfg, out, home):
    a = mkdir(tmp_path, "a")
    target = str(tmp_path / "real.json")
    write(target, {"allowed_dirs": [a]})
    os.symlink(target, cfg)
    assert load(cfg, out, home).roots == (out, a)
    os.chmod(target, 0o666)
    s = load(cfg, out, home)
    assert s.roots == (out,) and "group- or world-writable" in s.errors[0]


def test_dangling_symlink_file_is_reported(tmp_path, cfg, out, home):
    os.symlink(tmp_path / "gone.json", cfg)
    s = load(cfg, out, home)
    assert s.roots == (out,) and "dangling" in s.errors[0]


def test_directory_as_file_is_reported(cfg, out, home):
    os.mkdir(cfg)
    s = load(cfg, out, home)
    assert not s.config_loaded and "not a regular file" in s.errors[0]


@pytest.mark.parametrize("text, msg", [
    ("{not json", "invalid JSON"),
    ("", "invalid JSON"),
    ("\xff", "invalid JSON"),
    ("[1, 2]", "top level must be a JSON object"),
    ('"x"', "top level must be a JSON object"),
])
def test_bad_json_is_ignored_with_error(cfg, out, home, text, msg):
    write(cfg, text)
    s = load(cfg, out, home)
    assert s.roots == (out,) and not s.config_loaded
    assert len(s.errors) == 1 and msg in s.errors[0]


def test_unknown_key_is_reported_and_rest_used(tmp_path, cfg, out, home):
    a = mkdir(tmp_path, "a")
    write(cfg, {"allowed_dirs": [a], "alowed_dirs": ["/x"]})
    s = load(cfg, out, home)
    assert s.roots == (out, a) and s.config_loaded
    assert s.errors == (f"config file {cfg}: unknown key 'alowed_dirs' ignored",)


@pytest.mark.parametrize("value", ["/a", {"a": 1}, None])
def test_allowed_dirs_not_a_list_is_ignored(cfg, out, home, value):
    write(cfg, {"allowed_dirs": value})
    s = load(cfg, out, home)
    assert s.roots == (out,) and "'allowed_dirs' must be a list of strings" in s.errors[0]


def test_non_string_allowed_dir_entry_is_skipped(tmp_path, cfg, out, home):
    a = mkdir(tmp_path, "a")
    write(cfg, {"allowed_dirs": [1, a, None]})
    s = load(cfg, out, home)
    assert s.roots == (out, a) and len(s.errors) == 2
    assert "entry 1 is not a string" in s.errors[0]


# --- load(): limits and journal ----------------------------------------

def test_limit_overrides(cfg, out, home):
    write(cfg, {"limits": {"max_canvas_pixels": 1000}, "journal": {"max_actions": 5, "max_bytes": 10}})
    s = load(cfg, out, home)
    assert dict(s.limits) == {"max_canvas_dim": 16384, "max_canvas_pixels": 1000}
    assert dict(s.journal) == {"max_actions": 5, "max_bytes": 10}
    assert s.errors == ()


@pytest.mark.parametrize("section", ["limits", "journal"])
@pytest.mark.parametrize("bad", [0, -1, True, False, "100", 1.5, None, [1]])
def test_invalid_override_keeps_default(cfg, out, home, section, bad):
    key = next(iter(getattr(config, f"DEFAULT_{section.upper()}")))
    write(cfg, {section: {key: bad}})
    s = load(cfg, out, home)
    assert dict(getattr(s, section)) == getattr(config, f"DEFAULT_{section.upper()}")
    assert len(s.errors) == 1
    assert f"{section}.{key} must be a positive integer, got {bad!r}" in s.errors[0]


def test_invalid_override_does_not_affect_others(cfg, out, home):
    write(cfg, {"limits": {"max_canvas_dim": 0, "max_canvas_pixels": 99}})
    s = load(cfg, out, home)
    assert dict(s.limits) == {"max_canvas_dim": 16384, "max_canvas_pixels": 99}
    assert len(s.errors) == 1


def test_unknown_limit_key_and_non_object_section(cfg, out, home):
    write(cfg, {"limits": {"max_brush": 5}, "journal": [1]})
    s = load(cfg, out, home)
    assert dict(s.limits) == config.DEFAULT_LIMITS and dict(s.journal) == config.DEFAULT_JOURNAL
    assert s.errors == (f"config file {cfg}: unknown key limits.max_brush ignored",
                        f"config file {cfg}: 'journal' must be an object; ignored")


def test_defaults_are_not_mutated(cfg, out, home):
    write(cfg, {"limits": {"max_canvas_dim": 1}})
    load(cfg, out, home)
    assert config.DEFAULT_LIMITS["max_canvas_dim"] == 16384


# --- load(): defaults from the environment -----------------------------

def test_config_file_env_var_is_used(tmp_path, out, home, monkeypatch):
    a = mkdir(tmp_path, "a")
    path = str(tmp_path / "elsewhere.json")
    write(path, {"allowed_dirs": [a]})
    s = config.load(env={config.CONFIG_FILE_ENV: path}, output_dir=out, home=home)
    assert s.config_file == path and s.roots == (out, a)


# --- current(): caching and reload -------------------------------------

@pytest.fixture
def live(tmp_path, monkeypatch):
    """Point current() at temp files; count load() calls."""
    path = str(tmp_path / "live.json")
    out = str(tmp_path / "live-out")
    monkeypatch.setenv(config.CONFIG_FILE_ENV, path)
    monkeypatch.delenv(config.ALLOWED_DIRS_ENV, raising=False)
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", out)
    calls = []
    real_load = config.load

    def counting_load(*a, **kw):
        calls.append(1)
        return real_load(*a, **kw)

    monkeypatch.setattr(config, "load", counting_load)
    config.reset_cache()
    yield path, calls
    config.reset_cache()


def bump_mtime(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_current_caches(live):
    _, calls = live
    s = config.current()
    assert config.current() is s
    assert len(calls) == 1


def test_current_reloads_on_mtime_change(tmp_path, live):
    path, calls = live
    a = mkdir(tmp_path, "a")
    write(path, {"allowed_dirs": []})
    assert a not in config.current().roots
    # Same size, so only the mtime tells them apart.
    with open(path, "w") as f:
        f.write(json.dumps({"allowed_dirs": [a]}))
    bump_mtime(path)
    assert a in config.current().roots
    assert len(calls) == 2
    config.current()
    assert len(calls) == 2


def test_current_reloads_when_file_appears_and_disappears(tmp_path, live):
    path, calls = live
    a = mkdir(tmp_path, "a")
    assert config.current().config_loaded is False
    write(path, {"allowed_dirs": [a]})
    assert a in config.current().roots
    os.remove(path)
    assert a not in config.current().roots
    assert len(calls) == 3


def test_current_reloads_on_chmod(tmp_path, live):
    path, _ = live
    a = mkdir(tmp_path, "a")
    write(path, {"allowed_dirs": [a]})
    assert a in config.current().roots
    os.chmod(path, 0o666)  # changes ctime and mode, not mtime
    s = config.current()
    assert a not in s.roots and "group- or world-writable" in s.errors[0]


def test_current_reloads_on_env_change(tmp_path, live, monkeypatch):
    _, calls = live
    a = mkdir(tmp_path, "a")
    assert a not in config.current().roots
    monkeypatch.setenv(config.ALLOWED_DIRS_ENV, a)
    assert a in config.current().roots
    config.current()
    assert len(calls) == 2


def test_current_reloads_when_requested_root_appears(tmp_path, live):
    path, calls = live
    later = str(tmp_path / "later")
    write(path, {"allowed_dirs": [later]})
    s = config.current()
    assert s.roots == (s.output_dir,) and "does not exist" in s.errors[0]
    os.mkdir(later)
    s = config.current()
    assert os.path.realpath(later) in s.roots and s.errors == ()
    assert len(calls) == 2


def test_current_follows_output_dir_constant(tmp_path, live, monkeypatch):
    monkeypatch.setattr(config, "CANVAS_OUTPUT_DIR", str(tmp_path / "other-out"))
    assert config.current().output_dir == os.path.join(os.path.realpath(tmp_path), "other-out")


def test_reset_cache_forces_reload(live):
    _, calls = live
    config.current()
    config.reset_cache()
    config.current()
    assert len(calls) == 2


def test_current_is_thread_safe(live):
    import threading
    _, calls = live
    results = []
    threads = [threading.Thread(target=lambda: results.append(config.current())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and all(r is results[0] for r in results)
