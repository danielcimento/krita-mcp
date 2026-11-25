"""F001 acceptance tests (docs/f001_buildout/acceptance.md), against the GUI
Krita through server.py's tools. Run with KRITA_MCP_LIVE=1.

Covered so far: 1 (Mao rebuild; also needs KRITA_MCP_MAO_ORA), 3 (canvas
cap), 5 (allowlist) and 6 (handles).
"""

import collections
import importlib.util
import os
import shutil
import tempfile

import pytest

SMALL = {"width": 64, "height": 64}
MAO_ENV = "KRITA_MCP_MAO_ORA"
TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "tools")


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


# --- 1. Mao rebuild -----------------------------------------------------------

MAO_SIZE = (5800, 8400)
MAO_HIDDEN_GROUPS = ["Expression References", "[ Guide Image ]"]


def _ora_to_manifest():
    spec = importlib.util.spec_from_file_location("ora_to_manifest",
                                                  os.path.join(TOOLS, "ora_to_manifest.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mao_dir(scratch):
    """``<scratch>/mao``, for the Mao-derived files (third-party art).
    Removed after the test whatever happens, KRITA_MCP_KEEP_SCRATCH or not.
    List it before ``docs_to_close`` so the documents close first."""
    ora = os.environ.get(MAO_ENV)
    if not ora:
        pytest.skip(f"set {MAO_ENV} to the Mao .ora (acceptance.md § Fixtures)")
    path = os.path.join(scratch[0], "mao")
    os.makedirs(path)
    try:
        yield os.path.expanduser(ora), path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _without(layers, layer_id):
    """``layers`` minus the one with ``layer_id`` (new_canvas's Background)."""
    rest = [l for l in layers if l["id"] != layer_id]
    assert len(layers) - len(rest) == 1, layer_id
    return rest


def test_mao_rebuild(call, mao_dir, docs_to_close, record_property):
    from PIL import Image

    ora, work = mao_dir
    out = os.path.join(work, "manifest")
    m = _ora_to_manifest().convert(ora, out)  # step 1
    entries = [e for e in m["layers"] if not e.get("projection")]
    assert (m["document"]["width"], m["document"]["height"]) == MAO_SIZE

    canvas = call("new_canvas", width=MAO_SIZE[0], height=MAO_SIZE[1],  # step 2
                  background="transparent", name="f001-mao")
    doc = canvas["doc"]
    docs_to_close.append(doc)
    background = canvas["background_layer"]

    created = call("create_layers", doc=doc, manifest=os.path.join(out, "manifest.json"),
                   timeout_s=600)  # step 3: one call
    record_property("create_layers_elapsed_ms", created["elapsed_ms"])
    print(f"create_layers: {created['elapsed_ms']} ms; {created['summary']}")
    assert created["counts"] == {"grouplayer": 38, "paintlayer": 184, "total": 222}
    assert (created["skipped"], created["warnings"]) == ([], [])

    kra = os.path.join(work, "mao.kra")
    saved = call("save", doc=doc, path=kra, timeout_s=600)  # step 4
    assert saved["path"] == os.path.realpath(kra) and os.path.isfile(saved["path"])

    layers = _without(call("list_layers", doc=doc)["layers"], background)  # step 5
    kinds = collections.Counter(l["type"] for l in layers)
    assert kinds == {"paintlayer": 184, "grouplayer": 38}, kinds
    paths = collections.Counter(e["path"] for e in entries)
    assert collections.Counter(l["path"] for l in layers) == paths
    multiply = sorted(l["path"] for l in layers if l["blend_mode"] == "multiply")
    assert len(multiply) == 7
    assert multiply == sorted(e["path"] for e in entries if e["blend_mode"] == "multiply")
    hidden_groups = sorted(l["name"] for l in layers
                           if l["type"] == "grouplayer" and not l["visible"])
    assert hidden_groups == sorted(MAO_HIDDEN_GROUPS)

    # Bounds: the PNG's opaque bounds (alpha > 0) offset by its x, y.
    by_key = {c["key"]: c["id"] for c in created["created"]}
    by_id = {l["id"]: l for l in layers}
    for e in entries:
        if e["type"] != "paint":
            continue
        want = None
        if e["png"]:
            with Image.open(os.path.join(out, e["png"])) as im:
                box = im.convert("RGBA").getchannel("A").getbbox()
            if box is not None:
                left, top, right, bottom = box
                want = {"x": e["x"] + left, "y": e["y"] + top,
                        "w": right - left, "h": bottom - top}
        assert by_id[by_key[e["key"]]]["bounds"] == want, e["path"]

    # The saved .kra reopens with the same counts.
    call("close_document", doc=doc, discard=True)
    reopened = call("open_file", path=kra, timeout_s=600)
    docs_to_close.append(reopened["doc"])
    again = _without(call("list_layers", doc=reopened["doc"])["layers"], background)
    assert collections.Counter(l["type"] for l in again) == kinds
    assert collections.Counter(l["path"] for l in again) == paths


# --- 3. Canvas cap ------------------------------------------------------------

def test_canvas_cap(call, docs_to_close, config_override):
    # 5800 x 8400 (Mao's size) is allowed at the default limits.
    big = call("new_canvas", width=5800, height=8400, name="f001-canvas-cap")
    docs_to_close.append(big["doc"])
    info = call("get_document_info", doc=big["doc"])
    assert (info["width"], info["height"]) == (5800, 8400)
    assert (info["color_model"], info["color_depth"]) == ("RGBA", "U8")
    assert info["color_profile"].startswith("sRGB"), info["color_profile"]
    call("close_document", doc=big["doc"], discard=True)  # 195 MB: don't keep it

    message = call.error("too_large", "new_canvas", width=16385, height=16)
    assert "max_canvas_dim" in message
    message = call.error("too_large", "new_canvas", width=9000, height=9000)
    assert "max_canvas_pixels" in message

    # A config file override of max_canvas_pixels is honoured, then restored.
    before = call.health()["limits"]
    config_override.write({**config_override.original,
                           "limits": {**config_override.original.get("limits", {}),
                                      "max_canvas_pixels": 1000}})
    health = call.health()
    assert health["limits"]["max_canvas_pixels"] == 1000, health["config_errors"]
    message = call.error("too_large", "new_canvas", width=40, height=40)  # 1600 px
    assert "max_canvas_pixels" in message and "1000" in message
    small = call("new_canvas", width=20, height=20, name="f001-cap-small")  # 400 px
    docs_to_close.append(small["doc"])
    call("close_document", doc=small["doc"], discard=True)

    config_override.restore()
    assert call.health()["limits"] == before
    again = call("new_canvas", width=40, height=40, name="f001-cap-restored")
    docs_to_close.append(again["doc"])


# --- 5. Allowlist -------------------------------------------------------------

@pytest.fixture
def repo_root():
    """``R``, standing in for a project repo, with ``R/escape -> /tmp``.

    R is ``<system temp>/kmcp-f001-<random>/R``: outside the output dir and
    every configured root, and so is its parent, so ``R/../x.png`` lands
    outside every root (the test checks that precondition). It isn't under
    /tmp either, so ``/tmp/x.png`` stays outside.
    """
    base = tempfile.mkdtemp(prefix="kmcp-f001-")
    r = os.path.join(base, "R")
    os.mkdir(r)
    os.symlink("/tmp", os.path.join(r, "escape"))
    try:
        yield r
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_allowlist(call, repo_root, config_override, docs_to_close, scratch):
    r = repo_root
    r_real = os.path.realpath(r)
    roots_before = call.health()["allowed_dirs"]
    for root in roots_before:  # preconditions for the R/../x.png and /tmp rows
        assert not _inside(os.path.dirname(r_real), root), (root, r_real)
        assert not _inside("/tmp/x.png", root), root

    # Setup: add R to the config file's allowed_dirs.
    dirs = list(config_override.original.get("allowed_dirs", []))
    config_override.write({**config_override.original, "allowed_dirs": dirs + [r]})
    health = call.health()
    assert r_real in health["allowed_dirs"], health

    doc = call("new_canvas", **SMALL, name="f001-allowlist")["doc"]
    docs_to_close.append(doc)

    # R/art/x.kra (absolute): OK, resolved path returned, file exists.
    saved = call("save", doc=doc, path=os.path.join(r, "art", "x.kra"))
    assert saved["path"] == os.path.join(r_real, "art", "x.kra")
    assert os.path.isfile(saved["path"]) and saved["bytes"] > 0

    # A relative path goes to the output dir (into this run's scratch dir
    # there, not the output dir's top level, so no user file is replaced).
    _, rel = scratch
    saved = call("save", doc=doc, path=os.path.join(rel, "x.png"))
    assert _inside(saved["path"], health["allowed_dirs"][0])
    assert saved["path"] == os.path.join(health["allowed_dirs"][0], rel, "x.png")
    assert os.path.isfile(saved["path"])

    call.error("forbidden_path", "save", doc=doc, path="/tmp/x.png")
    call.error("forbidden_path", "save", doc=doc, path=os.path.join(r, "..", "x.png"))
    call.error("forbidden_path", "save", doc=doc, path=os.path.join(r, "escape", "x.png"))
    message = call.error("forbidden_path", "save", doc=doc, path=os.path.join(r, "x.txt"))
    assert "File type not allowed" in message
    assert not os.path.exists(os.path.join(os.path.dirname(r_real), "x.png"))

    # The config file made world-writable: its roots are ignored, and
    # krita_health reports the error.
    config_override.chmod(0o666)
    health = call.health()
    assert r_real not in health["allowed_dirs"], health
    assert any("writable" in e for e in health["config_errors"]), health["config_errors"]
    call.error("forbidden_path", "save", doc=doc, path=os.path.join(r, "y.png"))
    config_override.chmod(0o600)
    assert r_real in call.health()["allowed_dirs"]

    config_override.restore()
    assert call.health()["allowed_dirs"] == roots_before


def test_allowlist_export_layers():
    # acceptance.md § 5, row `krita_export_layers(doc, dir="R/escape/out")`
    # -> forbidden_path. TODO(P3.2): krita_export_layers doesn't exist yet.
    pytest.skip("krita_export_layers lands in P3.2")


# --- 6. Handles ---------------------------------------------------------------

def test_handles(call, docs_to_close):
    # Step 1: two canvases; b, created last, is active in the UI.
    a = call("new_canvas", **SMALL, name="A")["doc"]
    docs_to_close.append(a)
    b = call("new_canvas", **SMALL, name="B")["doc"]
    docs_to_close.append(b)
    assert a != b

    # Step 2: a layer in a only.
    created = call("create_layers", doc=a, specs=[{"name": "Only In A"}])
    assert created["doc"] == a
    assert [(c["name"], c["path"]) for c in created["created"]] == [("Only In A", "Only In A")]

    # Step 3: a lists it, b doesn't.
    assert "Only In A" in {l["path"] for l in call("list_layers", doc=a)["layers"]}
    assert "Only In A" not in {l["path"] for l in call("list_layers", doc=b)["layers"]}
    call.error("not_found", "get_color_at", doc=b, layer="Only In A", x=32, y=32)

    # Step 4: b is active in the UI; a is not.
    listed = {d["doc"]: d for d in call("list_documents")["documents"]}
    assert listed[b]["active"] is True
    assert listed[a]["active"] is False

    # Step 5: painting a's "Only In A" changes a, not b.
    b_before = call("get_color_at", doc=b, x=32, y=32)
    before = call("get_color_at", doc=a, layer="Only In A", x=32, y=32)
    assert before["a"] == 0, before
    call("set_color", color="#ff0000")
    painted = call("fill", doc=a, layer="Only In A", x=32, y=32, radius=8)
    assert painted["doc"] == a
    after = call("get_color_at", doc=a, layer="Only In A", x=32, y=32)
    assert (after["color"], after["a"]) == ("#ff0000", 255), after
    assert call("get_color_at", doc=b, x=32, y=32)["color"] == b_before["color"]
    assert call("get_color_at", doc=b, layer="Background", x=32, y=32)["color"] == \
        b_before["color"]

    # Step 6: a call without doc hits b.
    assert call("get_document_info")["doc"] == b
    assert call("get_color_at", x=32, y=32)["doc"] == b

    # Step 7: close a; its handle is then not_found.
    closed = call("close_document", doc=a, discard=True)
    assert (closed["doc"], closed["closed"]) == (a, True)
    call.error("not_found", "get_document_info", doc=a)
    call.error("not_found", "fill", doc=a, layer="Only In A", x=1, y=1)
    assert a not in {d["doc"] for d in call("list_documents")["documents"]}
