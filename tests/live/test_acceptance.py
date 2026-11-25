"""F001 acceptance tests (docs/f001_buildout/acceptance.md), against the GUI
Krita through server.py's tools. Run with KRITA_MCP_LIVE=1.

Phase 1 covers tests 3 (canvas cap), 5 (allowlist) and 6 (handles).
"""

import os
import shutil
import tempfile

import pytest

SMALL = {"width": 64, "height": 64}


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


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
    # acceptance.md § 6, adapted to Phase 1: krita_create_layers and
    # krita_list_layers come in P2, so steps 2, 3 and 5 paint into the
    # Background layer new_canvas makes instead of a created "Only In A"
    # layer. The full version replaces this after P2.3.
    a = call("new_canvas", **SMALL, name="A")["doc"]
    docs_to_close.append(a)
    b = call("new_canvas", **SMALL, name="B")["doc"]
    docs_to_close.append(b)
    assert a != b

    # Steps 2-3 and 5 (adapted): paint into a's Background only.
    b_before = call("get_color_at", doc=b, layer="Background", x=32, y=32)["color"]
    assert call("get_color_at", doc=a, layer="Background", x=32, y=32)["color"] == b_before
    call("set_color", color="#ff0000")
    painted = call("fill", doc=a, layer="Background", x=32, y=32, radius=8)
    assert painted["doc"] == a
    assert call("get_color_at", doc=a, layer="Background", x=32, y=32)["color"] == "#ff0000"
    assert call("get_color_at", doc=b, layer="Background", x=32, y=32)["color"] == b_before

    # Step 4: b, created last, is active in the UI; a is not.
    listed = {d["doc"]: d for d in call("list_documents")["documents"]}
    assert listed[b]["active"] is True
    assert listed[a]["active"] is False

    # Step 6: a call without doc hits b.
    assert call("get_document_info")["doc"] == b
    assert call("get_color_at", x=32, y=32)["doc"] == b

    # Step 7: close a; its handle is then not_found.
    closed = call("close_document", doc=a, discard=True)
    assert (closed["doc"], closed["closed"]) == (a, True)
    call.error("not_found", "get_document_info", doc=a)
    call.error("not_found", "fill", doc=a, layer="Background", x=1, y=1)
    assert a not in {d["doc"] for d in call("list_documents")["documents"]}
