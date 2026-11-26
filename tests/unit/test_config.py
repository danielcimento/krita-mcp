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
    assert dict(s.lint) == config.DEFAULT_LINT
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
    assert dict(s.journal) == {"max_actions": 5, "max_bytes": 10, "max_disk_bytes": 4294967296}
    assert s.errors == ()


def test_journal_defaults():
    assert config.DEFAULT_JOURNAL == {"max_actions": 20, "max_bytes": 1073741824,
                                      "max_disk_bytes": 4294967296}  # 1 GB in memory, 4 GB spilled (D6)


def test_journal_max_disk_bytes_override(cfg, out, home):
    write(cfg, {"journal": {"max_disk_bytes": 1 << 40}})
    s = load(cfg, out, home)
    assert dict(s.journal) == {**config.DEFAULT_JOURNAL, "max_disk_bytes": 1 << 40}
    assert s.errors == ()


@pytest.mark.parametrize("section,key", [("limits", "max_canvas_dim"), ("journal", "max_actions"),
                                         ("journal", "max_disk_bytes")])
@pytest.mark.parametrize("bad", [0, -1, True, False, "100", 1.5, None, [1]])
def test_invalid_override_keeps_default(cfg, out, home, section, key, bad):
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


# --- load(): lint (D14; F004) --------------------------------------------

def test_lint_defaults():
    assert config.DEFAULT_LINT == {"stray_alpha_below": 8, "stray_alpha_distance": 4,
                                   "stray_speck_max_px": 64, "reference_pattern": r"^\[.*\]$",
                                   "disable": ()}
    assert config.LINT_INT_RANGES["stray_speck_max_px"] == (1, 1_000_000)


def test_lint_overrides(cfg, out, home):
    write(cfg, {"lint": {"stray_alpha_below": 255, "stray_alpha_distance": 1, "stray_speck_max_px": 1,
                         "reference_pattern": r"^Ref ", "disable": ["stray-alpha", "empty-layer"]}})
    s = load(cfg, out, home)
    assert dict(s.lint) == {"stray_alpha_below": 255, "stray_alpha_distance": 1, "stray_speck_max_px": 1,
                            "reference_pattern": r"^Ref ", "disable": ("stray-alpha", "empty-layer")}
    assert s.errors == ()


def test_lint_reference_pattern_null_and_bad(cfg, out, home):
    write(cfg, {"lint": {"reference_pattern": None}})
    assert load(cfg, out, home).lint["reference_pattern"] is None
    for bad, why in (("[", "is not a valid regular expression"), (3, "must be a regular expression")):
        write(cfg, {"lint": {"reference_pattern": bad}})
        s = load(cfg, out, home)
        assert s.lint["reference_pattern"] == config.DEFAULT_LINT["reference_pattern"]
        assert len(s.errors) == 1 and f"lint.reference_pattern {why}" in s.errors[0]


def test_lint_disable_keeps_known_rules(cfg, out, home):
    write(cfg, {"lint": {"disable": ["empty-layer", "bogus", 3, "empty-layer", "pose-visibility"]}})
    s = load(cfg, out, home)
    assert s.lint["disable"] == ("empty-layer", "pose-visibility")
    assert [e.split(": ", 1)[1].split(" (rules:")[0] for e in s.errors] == [
        "lint.disable: unknown rule 'bogus' ignored", "lint.disable: unknown rule 3 ignored"]
    write(cfg, {"lint": {"disable": "empty-layer"}})
    s = load(cfg, out, home)
    assert s.lint["disable"] == () and "lint.disable must be a list of rule ids" in s.errors[0]


def test_lint_speck_max_px_upper_bound(cfg, out, home):
    write(cfg, {"lint": {"stray_speck_max_px": 1_000_000}})
    assert load(cfg, out, home).lint["stray_speck_max_px"] == 1_000_000


def test_lint_partial_override_keeps_other_defaults(cfg, out, home):
    write(cfg, {"lint": {"stray_alpha_distance": 64}})
    s = load(cfg, out, home)
    assert dict(s.lint) == {**config.DEFAULT_LINT, "stray_alpha_distance": 64}


@pytest.mark.parametrize("key,bad", [
    ("stray_alpha_below", 0), ("stray_alpha_below", 256), ("stray_alpha_below", True),
    ("stray_alpha_below", 8.0), ("stray_alpha_below", "8"),
    ("stray_alpha_distance", 0), ("stray_alpha_distance", 65), ("stray_alpha_distance", -4),
    ("stray_alpha_distance", None),
    ("stray_speck_max_px", 0), ("stray_speck_max_px", 1_000_001), ("stray_speck_max_px", -1),
    ("stray_speck_max_px", 64.0), ("stray_speck_max_px", True), ("stray_speck_max_px", "64"),
])
def test_lint_bad_threshold_keeps_default(cfg, out, home, key, bad):
    write(cfg, {"lint": {key: bad}})
    s = load(cfg, out, home)
    assert dict(s.lint) == config.DEFAULT_LINT
    lo, hi = config.LINT_INT_RANGES[key]
    assert s.errors == (f"config file {cfg}: lint.{key} must be an integer from {lo} to {hi}, "
                        f"got {bad!r}; default {config.DEFAULT_LINT[key]} kept",)


def test_lint_unknown_key_and_non_object(cfg, out, home):
    write(cfg, {"lint": {"additive": "add"}})
    s = load(cfg, out, home)
    assert dict(s.lint) == config.DEFAULT_LINT
    assert s.errors == (f"config file {cfg}: unknown key lint.additive ignored",)
    write(cfg, {"lint": "add"})
    s = load(cfg, out, home)
    assert dict(s.lint) == config.DEFAULT_LINT
    assert s.errors == (f"config file {cfg}: 'lint' must be an object; ignored",)


def test_lint_is_read_only_and_defaults_not_mutated(cfg, out, home):
    write(cfg, {"lint": {"stray_alpha_below": 4}, "live2d": {"additive_mode": "add"}})
    s = load(cfg, out, home)
    with pytest.raises(TypeError):
        s.lint["stray_alpha_below"] = 8
    with pytest.raises(TypeError):
        s.live2d["additive_mode"] = "either"
    assert config.DEFAULT_LINT["stray_alpha_below"] == 8
    assert config.DEFAULT_LIVE2D["additive_mode"] == "either"


# --- load(): live2d (F004) ------------------------------------------------

def test_live2d_defaults_off(cfg, out, home):
    assert config.DEFAULT_LIVE2D == {
        "enabled": False, "additive_mode": "either",
        "allowed_blend_modes": ("normal", "multiply", "add", "linear_dodge"), "max_nesting": 3,
        "pose_pattern": r"^(?P<stem>.*\S) (?P<pose>[A-KM-QS-Z])$", "default_pose": "A"}
    assert dict(load(cfg, out, home).live2d) == config.DEFAULT_LIVE2D


@pytest.mark.parametrize("mode", ["either", "add", "linear_dodge"])
def test_live2d_overrides(cfg, out, home, mode):
    given = {"enabled": True, "additive_mode": mode, "allowed_blend_modes": ["normal", "screen", "normal"],
             "max_nesting": 4, "pose_pattern": r"(?P<stem>.+) v(?P<pose>\d+)", "default_pose": "1"}
    write(cfg, {"live2d": given})
    s = load(cfg, out, home)
    assert dict(s.live2d) == {**given, "allowed_blend_modes": ("normal", "screen")}
    assert s.errors == ()


@pytest.mark.parametrize("key,bad,why", [
    ("enabled", "yes", "live2d.enabled must be true or false"),
    ("enabled", 1, "live2d.enabled must be true or false"),
    ("additive_mode", "Add", "live2d.additive_mode must be one of 'either', 'add', 'linear_dodge'"),
    ("allowed_blend_modes", [], "live2d.allowed_blend_modes must be a non-empty list"),
    ("allowed_blend_modes", ["normal", "Screen"], "live2d.allowed_blend_modes must be a non-empty list"),
    ("max_nesting", 0, "live2d.max_nesting must be an integer from 1 to 64"),
    ("max_nesting", True, "live2d.max_nesting must be an integer from 1 to 64"),
    ("pose_pattern", "(", "live2d.pose_pattern is not a valid regular expression"),
    ("pose_pattern", "(?P<stem>.*) (.)", "live2d.pose_pattern needs the named groups (?P<pose>...)"),
    ("default_pose", "", "live2d.default_pose must be a non-empty string"),
    ("bogus", 1, "unknown key live2d.bogus ignored"),
])
def test_live2d_bad_value_keeps_default(cfg, out, home, key, bad, why):
    write(cfg, {"live2d": {key: bad, "max_nesting": 5} if key != "max_nesting" else {key: bad}})
    s = load(cfg, out, home)
    assert {k: v for k, v in s.live2d.items() if k != "max_nesting"} == \
        {k: v for k, v in config.DEFAULT_LIVE2D.items() if k != "max_nesting"}
    assert len(s.errors) == 1 and why in s.errors[0], s.errors


def test_live2d_non_object(cfg, out, home):
    write(cfg, {"live2d": True})
    s = load(cfg, out, home)
    assert dict(s.live2d) == config.DEFAULT_LIVE2D
    assert s.errors == (f"config file {cfg}: 'live2d' must be an object; ignored",)


def test_legacy_lint_additive_mode_moves_to_live2d(cfg, out, home):
    """F003 configs had lint.additive_mode: still read, with a note."""
    write(cfg, {"lint": {"additive_mode": "add"}})
    s = load(cfg, out, home)
    assert s.live2d["additive_mode"] == "add" and "additive_mode" not in s.lint
    assert s.errors == (f"config file {cfg}: lint.additive_mode moved to live2d.additive_mode (F004); "
                        "read from the old place for now",)
    write(cfg, {"lint": {"additive_mode": "Add"}})
    s = load(cfg, out, home)
    assert s.live2d["additive_mode"] == "either"
    assert "lint.additive_mode must be one of 'either', 'add', 'linear_dodge', got 'Add'" in s.errors[1]
    write(cfg, {"lint": {"additive_mode": "add"}, "live2d": {"additive_mode": "linear_dodge"}})
    s = load(cfg, out, home)
    assert s.live2d["additive_mode"] == "linear_dodge"
    assert s.errors == (f"config file {cfg}: lint.additive_mode ignored: it moved to "
                        "live2d.additive_mode, which is also set (F004)",)


def test_describe_reports_lint_and_live2d_as_json(cfg, out, home):
    write(cfg, {"lint": {"disable": ["empty-layer"]}, "live2d": {"enabled": True}})
    d = config.describe(load(cfg, out, home))
    assert d["lint"]["disable"] == ["empty-layer"]
    assert d["live2d"]["enabled"] is True
    assert d["live2d"]["allowed_blend_modes"] == ["normal", "multiply", "add", "linear_dodge"]
    json.dumps(d)


# --- load(): paint (F005) -------------------------------------------------

def test_paint_defaults_and_override(cfg, out, home):
    assert config.DEFAULT_PAINT == {"check_visibility": True}
    assert dict(load(cfg, out, home).paint) == {"check_visibility": True}
    write(cfg, {"paint": {"check_visibility": False}})
    s = load(cfg, out, home)
    assert dict(s.paint) == {"check_visibility": False} and s.errors == ()
    assert config.describe(s)["paint"] == {"check_visibility": False}


@pytest.mark.parametrize("given,why", [
    ({"check_visibility": "no"}, "paint.check_visibility must be true or false, got 'no'; "
                                 "default True kept"),
    ({"check_visibility": 0}, "paint.check_visibility must be true or false"),
    ({"notes": False}, "unknown key paint.notes ignored"),
    ([], "'paint' must be an object; ignored"),
])
def test_paint_bad_values_keep_the_default(cfg, out, home, given, why):
    write(cfg, {"paint": given})
    s = load(cfg, out, home)
    assert dict(s.paint) == config.DEFAULT_PAINT
    assert len(s.errors) == 1 and why in s.errors[0]


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
