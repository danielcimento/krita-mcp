"""F001 acceptance tests (docs/f001_buildout/acceptance.md), against the GUI
Krita through server.py's tools. Run with KRITA_MCP_LIVE=1.

Covered: 1 (Mao rebuild; also needs KRITA_MCP_MAO_ORA), 2 (round trip:
the small synthetic variant, and the Mao variant with KRITA_MCP_MAO_ORA),
3 (canvas cap), 4 (lint on the committed fixture, plus the Mao
duplicate-name check with KRITA_MCP_MAO_ORA), 5 (allowlist, export_layers
row included) and 6 (handles), plus a render smoke test.
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


def _rebuild_mao(call, ora, work, docs_to_close, record_property=None):
    """Acceptance § 1 steps 1-3: ``ora_to_manifest`` into ``<work>/manifest``,
    a transparent Mao-size canvas, one ``create_layers(manifest=…)`` call.
    Returns ``(doc, background layer id, manifest entries, create result,
    manifest dir)``."""
    out = os.path.join(work, "manifest")
    m = _ora_to_manifest().convert(ora, out)  # step 1
    entries = [e for e in m["layers"] if not e.get("projection")]
    assert (m["document"]["width"], m["document"]["height"]) == MAO_SIZE

    canvas = call("new_canvas", width=MAO_SIZE[0], height=MAO_SIZE[1],  # step 2
                  background="transparent", name="f001-mao")
    doc = canvas["doc"]
    docs_to_close.append(doc)

    created = call("create_layers", doc=doc, manifest=os.path.join(out, "manifest.json"),
                   timeout_s=600)  # step 3: one call
    if record_property is not None:
        record_property("create_layers_elapsed_ms", created["elapsed_ms"])
    print(f"create_layers: {created['elapsed_ms']} ms; {created['summary']}")
    assert created["counts"] == {"grouplayer": 38, "paintlayer": 184, "total": 222}
    assert (created["skipped"], created["warnings"]) == ([], [])
    return doc, canvas["background_layer"], entries, created, out


def test_mao_rebuild(call, mao_dir, docs_to_close, record_property):
    from PIL import Image

    ora, work = mao_dir
    doc, background, entries, created, out = _rebuild_mao(call, ora, work, docs_to_close,
                                                           record_property)

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


# --- 2. Round trip ------------------------------------------------------------

TREE_KEYS = ("path", "type", "visible", "opacity", "blend_mode")
RT_SIZE = (40, 30)


def _tree(call, doc, drop=None):
    """list_layers of ``doc`` as (path, type, visible, opacity, blend_mode)
    tuples, top to bottom, without the layer with id ``drop``."""
    return [tuple(l[k] for k in TREE_KEYS) for l in call("list_layers", doc=doc)["layers"]
            if l["id"] != drop]


def _rgba(path):
    """The PNG at ``path`` as an RGBA Pillow image (loaded, file closed)."""
    from PIL import Image
    with Image.open(path) as im:
        assert im.mode == "RGBA", (path, im.mode)
        im.load()
        return im.copy()


def _zero_alpha0_colour(im):
    """``im`` with every alpha-0 pixel set to (0, 0, 0, 0)."""
    from PIL import Image
    mask = im.getchannel("A").point(lambda v: 255 if v else 0)
    return Image.composite(im, Image.new("RGBA", im.size, (0, 0, 0, 0)), mask)


def _first_diff(a, b):
    if a.size != b.size:
        return f"sizes differ: {a.size} vs {b.size}"
    da, db = a.tobytes(), b.tobytes()
    for i in range(0, len(da), 4):
        if da[i:i + 4] != db[i:i + 4]:
            p = i // 4
            return (f"{sum(1 for j in range(0, len(da), 4) if da[j:j + 4] != db[j:j + 4])} pixels "
                    f"differ; first ({p % a.size[0]}, {p // a.size[0]}): "
                    f"{tuple(da[i:i + 4])} vs {tuple(db[i:i + 4])}")
    return "identical"


def _pattern_png(path, w, h, seed):
    """An RGBA PNG with varied colour and alpha (0, partial, 255; colour
    under alpha 0 inside it) whose corners are opaque, so its alpha > 0
    bounds are the whole image and export_layers' crop keeps every byte."""
    from PIL import Image
    data = bytearray((i * 37 + seed * 11 + (i // 4) * 5) % 256 for i in range(4 * w * h))
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        data[4 * (y * w + x) + 3] = 255
    Image.frombytes("RGBA", (w, h), bytes(data)).save(path)
    return path


def test_round_trip(call, scratch, docs_to_close):
    """Acceptance § 2, small variant: a synthetic document built through the
    tools, exported, rebuilt on a new canvas; the two renders are
    byte-identical (colour under zero alpha included) and the layer trees
    match."""
    work = os.path.join(scratch[0], "round-trip")
    os.makedirs(work)
    w, h = RT_SIZE

    def png(name, pw, ph, seed):
        return _pattern_png(os.path.join(work, f"{name}.png"), pw, ph, seed)

    canvas = call("new_canvas", width=w, height=h, background="transparent", name="f001-rt-a")
    a = canvas["doc"]
    docs_to_close.append(a)
    # Siblings bottom to top. Nested groups, Multiply / Add, a hidden group,
    # an empty layer, partial alpha, off-origin layers, one past the edge.
    specs = [
        {"name": "Body", "key": "body", "type": "group"},
        {"name": "Base", "parent": "$body", "png": png("base", 20, 15, 1), "x": 2, "y": 3},
        {"name": "Shade", "parent": "$body", "png": png("shade", 14, 12, 2), "x": 6, "y": 5,
         "blend_mode": "multiply", "opacity": 0.5},
        {"name": "Glow", "parent": "$body", "png": png("glow", 9, 7, 3), "x": 10, "y": 8,
         "blend_mode": "add"},
        {"name": "Head", "key": "head", "type": "group", "parent": "$body"},
        {"name": "Eyes", "key": "eyes", "type": "group", "parent": "$head", "opacity": 0.8},
        {"name": "Eye", "parent": "$eyes", "png": png("eye", 5, 4, 5), "x": 25, "y": 18},
        {"name": "Empty", "parent": "$head"},
        {"name": "Hidden", "key": "hidden", "type": "group", "visible": False},
        {"name": "Secret", "parent": "$hidden", "png": png("secret", 8, 8, 6)},
        {"name": "Edge", "png": png("edge", 12, 10, 7), "x": -5, "y": 22, "blend_mode": "screen"},
    ]
    created = call("create_layers", doc=a, specs=specs)
    assert created["counts"]["total"] == len(specs)

    exported = call("export_layers", doc=a, dir=os.path.join(work, "export"))
    assert exported["warnings"] == [] and exported["skipped"] == []
    other = call("new_canvas", width=w, height=h, background="transparent", name="f001-rt-b")
    b = other["doc"]
    docs_to_close.append(b)
    rebuilt = call("create_layers", doc=b, manifest=exported["manifest"])
    assert (rebuilt["skipped"], rebuilt["warnings"]) == ([], [])

    ra = call("render", doc=a, path=os.path.join(work, "a.png"))
    rb = call("render", doc=b, path=os.path.join(work, "b.png"))
    assert (ra["width"], ra["height"]) == (rb["width"], rb["height"]) == (w, h)
    ia, ib = _rgba(ra["path"]), _rgba(rb["path"])
    assert ia.tobytes() == ib.tobytes(), _first_diff(ia, ib)
    assert ia.getchannel("A").getextrema() == (0, 255)  # the comparison is not trivial
    raw = ia.tobytes()  # colour under alpha 0 is present, so the strict check covers it
    assert any(raw[i + 3] == 0 and raw[i:i + 3] != b"\0\0\0" for i in range(0, len(raw), 4))

    assert _tree(call, b, drop=other["background_layer"]) == _tree(call, a)


def test_round_trip_mao(call, mao_dir, docs_to_close):
    """Acceptance § 2, Mao variant: rebuild Mao (as test 1), export it,
    rebuild the export, and compare the renders with colour under zero
    alpha treated as equal (acceptance.md § 2)."""
    ora, work = mao_dir
    a, _, _, _, _ = _rebuild_mao(call, ora, work, docs_to_close)
    exported = call("export_layers", doc=a, dir=os.path.join(work, "export"), png_level=1,
                    timeout_s=900)
    print(f"export_layers: {exported['elapsed_ms']} ms; {exported['summary']}")
    assert (exported["skipped"], exported["warnings"]) == ([], [])
    other = call("new_canvas", width=MAO_SIZE[0], height=MAO_SIZE[1], background="transparent",
                 name="f001-mao-rt")
    b = other["doc"]
    docs_to_close.append(b)
    rebuilt = call("create_layers", doc=b, manifest=exported["manifest"], timeout_s=600)
    assert (rebuilt["skipped"], rebuilt["warnings"]) == ([], [])

    renders = []
    for doc, name in ((a, "a.png"), (b, "b.png")):
        r = call("render", doc=doc, path=os.path.join(work, name), png_level=1, timeout_s=600)
        print(f"render {name}: {r['elapsed_ms']} ms; {r['summary']}")
        assert (r["width"], r["height"]) == MAO_SIZE
        renders.append(_rgba(r["path"]))
    ia, ib = renders
    same = ia.tobytes() == ib.tobytes()
    print(f"byte-identical: {same}")
    na, nb = _zero_alpha0_colour(ia), _zero_alpha0_colour(ib)
    assert na.tobytes() == nb.tobytes(), "the rebuilt Mao renders differently from the export"
    assert ia.getchannel("A").tobytes() == ib.getchannel("A").tobytes()

    assert _tree(call, b, drop=other["background_layer"]) == _tree(call, a)


# --- render smoke ---------------------------------------------------------------

def test_render(call, scratch, docs_to_close, run_id, output_dir):
    """render's options on a small document, and that it changes nothing:
    only / hide (ancestors shown, a hidden child kept hidden), region, scale,
    background, png_level; and get_canvas as a full-canvas render."""
    work = os.path.join(scratch[0], "render")
    os.makedirs(work)
    from PIL import Image

    def solid(name, colour, size=(4, 4)):
        path = os.path.join(work, f"{name}.png")
        Image.new("RGBA", size, colour).save(path)
        return path

    canvas = call("new_canvas", width=24, height=16, background="transparent",
                  name="f001-render")
    doc = canvas["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[
        {"name": "K", "key": "k", "type": "group"},
        {"name": "B", "parent": "$k", "png": solid("b", (0, 255, 255, 255)), "x": 4, "y": 8,
         "visible": False},
        {"name": "A", "parent": "$k", "png": solid("a", (0, 0, 255, 255)), "x": 0, "y": 8},
        {"name": "G", "key": "g", "type": "group", "visible": False},
        {"name": "L", "parent": "$g", "png": solid("l", (0, 255, 0, 255)), "x": 4, "y": 0},
        {"name": "Top", "png": solid("top", (255, 0, 0, 255)), "x": 0, "y": 0},
    ])
    def state():  # elapsed_ms and summary vary between calls
        info = call("get_document_info", doc=doc)
        info = {k: v for k, v in info.items() if k not in ("elapsed_ms", "summary")}
        return call("list_layers", doc=doc)["layers"], info

    before = state()

    def render(name, **params):
        result = call("render", doc=doc, path=os.path.join(work, f"{name}.png"), **params)
        return result, _rgba(result["path"])

    _, full = render("full")
    assert full.getpixel((1, 1)) == (255, 0, 0, 255)  # Top
    assert full.getpixel((5, 1)) == (0, 0, 0, 0)  # L: G is hidden
    assert full.getpixel((1, 9)) == (0, 0, 255, 255)  # K/A
    assert full.getpixel((5, 9)) == (0, 0, 0, 0)  # K/B is hidden

    result, only = render("only", only=["G/L", "K"])
    assert only.getpixel((1, 1)) == (0, 0, 0, 0)  # Top hidden
    assert only.getpixel((5, 1)) == (0, 255, 0, 255)  # L shown, G with it
    assert only.getpixel((1, 9)) == (0, 0, 255, 255)
    assert only.getpixel((5, 9)) == (0, 0, 0, 0)  # B stays hidden
    assert len(result["only"]) == 2 and "only 2 layers" in result["summary"]

    _, hidden = render("hide", hide=["Top"])
    assert hidden.getpixel((1, 1)) == (0, 0, 0, 0) and hidden.getpixel((1, 9)) == (0, 0, 255, 255)

    result, part = render("region", region={"x": -2, "y": 6, "w": 8, "h": 100})
    assert result["region"] == {"x": 0, "y": 6, "w": 6, "h": 10} and part.size == (6, 10)
    assert part.getpixel((1, 3)) == (0, 0, 255, 255)

    result, half = render("half", scale=0.5, png_level=1)
    assert half.size == (12, 8) and result["png_level"] == 1
    assert half.getpixel((0, 0)) == (255, 0, 0, 255)  # inside Top's block

    _, under = render("bg", background="#336699")
    assert under.getpixel((20, 14)) == (0x33, 0x66, 0x99, 255)
    assert under.getpixel((1, 1)) == (255, 0, 0, 255)

    call.error("bad_request", "render", doc=doc, path=os.path.join(work, "x.png"), only=[])
    call.error("not_found", "render", doc=doc, path=os.path.join(work, "x.png"), hide=["Nope"])
    call.error("bad_request", "render", doc=doc, path=os.path.join(work, "x.jpg"))
    call.error("bad_request", "render", doc=doc, path=os.path.join(work, "x.png"),
               region={"x": 30, "y": 0, "w": 4, "h": 4})

    # Nothing changed in the document.
    assert state() == before

    # get_canvas: the same full render, into the output dir's top level
    # (a unique name, removed afterwards).
    name = f"kmcp-live-render-{run_id}"
    shot = call("get_canvas", doc=doc, filename=name)
    try:
        assert shot["path"] == os.path.join(output_dir, name + ".png")
        assert _rgba(shot["path"]).tobytes() == full.tobytes()
    finally:
        if os.path.exists(os.path.join(output_dir, name + ".png")):
            os.remove(os.path.join(output_dir, name + ".png"))


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


# --- 4. Lint ------------------------------------------------------------------

LINT_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "fixtures",
                            "lint_four_findings.kra")
LINT_EXPECTED = collections.Counter([("node-type", "Blur"), ("inherit-alpha", "Shade"),
                                     ("duplicate-name", "Back/Hair"), ("empty-layer", "Spare")])


def test_lint_fixture(call, scratch, docs_to_close, live2d):
    """Acceptance § 4: the committed fixture (a copy in the scratch dir)
    gives exactly the four findings, with the Live2D conventions on (F004)."""
    copy = os.path.join(scratch[0], "lint_four_findings.kra")
    shutil.copyfile(LINT_FIXTURE, copy)
    opened = call("open_file", path=copy)
    docs_to_close.append(opened["doc"])
    result = call("lint", doc=opened["doc"])
    print(result["summary"])
    findings = result["findings"]
    assert collections.Counter((f["rule"], f["path"]) for f in findings) == LINT_EXPECTED
    assert [f["path"] for f in findings] == ["Blur", "Back/Hair", "Shade", "Spare"]
    hairs = [l["id"] for l in call("list_layers", doc=opened["doc"])["layers"]
             if l["name"] == "Hair"]
    dup = next(f for f in findings if f["rule"] == "duplicate-name")
    assert sorted(dup["related_ids"]) == sorted(hairs) and len(hairs) == 2
    assert result["counts"] == {"error": 3, "warning": 1, "info": 0}
    assert result["summary"] == f"3 errors, 1 warning in {opened['doc']} (10 rules)"


def test_lint_mao(call, mao_dir, docs_to_close, live2d):
    """Acceptance § Fixtures, secondary check: the rebuilt Mao (without
    new_canvas's Background) has exactly 11 duplicate-name findings, all
    A/B pose pairs with pose-suffix suggested_names (F003 K6: a group may
    share its name with a layer, so F001's 17 dropped the 6 group-vs-layer
    clashes). The other rules' counts are reported, not asserted."""
    ora, work = mao_dir
    doc, background, _, _, _ = _rebuild_mao(call, ora, work, docs_to_close)
    call("delete_layers", doc=doc, ids=[background])
    result = call("lint", doc=doc, timeout_s=600)
    print(f"lint: {result['elapsed_ms']} ms; {result['summary']}")
    per_rule = collections.Counter(f["rule"] for f in result["findings"])
    print(f"lint per rule: {dict(sorted(per_rule.items()))}")
    assert per_rule["duplicate-name"] == 11
    dups = [f for f in result["findings"] if f["rule"] == "duplicate-name"]
    assert all(len(f["related_ids"]) == 2 for f in dups)
    assert all(sorted(s["name"][-1] for s in f["suggested_names"]) == ["A", "B"]
               for f in dups), [f["message"] for f in dups]
    # F004 K8: Expression References is the only unbracketed hidden group.
    refs = call("lint", doc=doc, rules=["reference-unbracketed"])["findings"]
    assert [f["path"] for f in refs] == ["Expression References"], [f["message"] for f in refs]


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


def test_allowlist_export_layers(call, repo_root, config_override, docs_to_close):
    # acceptance.md § 5, row `krita_export_layers(doc, dir="R/escape/out")`
    # -> forbidden_path (R/escape -> /tmp; the symlink is resolved).
    r = repo_root
    r_real = os.path.realpath(r)
    escape_out = os.path.realpath(os.path.join(r, "escape", "out"))
    for root in call.health()["allowed_dirs"]:  # precondition: /tmp/out is outside every root
        assert not _inside(escape_out, root), root
    dirs = list(config_override.original.get("allowed_dirs", []))
    config_override.write({**config_override.original, "allowed_dirs": dirs + [r]})
    assert r_real in call.health()["allowed_dirs"]
    doc = call("new_canvas", **SMALL, name="f001-allowlist-export")["doc"]
    docs_to_close.append(doc)

    existed = os.path.exists(escape_out)
    call.error("forbidden_path", "export_layers", doc=doc, dir=os.path.join(r, "escape", "out"))
    assert os.path.exists(escape_out) == existed  # nothing created outside the roots

    # Inside R it works, and the directory must then be empty.
    out = call("export_layers", doc=doc, dir=os.path.join(r, "out"))
    assert out["dir"] == os.path.join(r_real, "out")
    assert os.path.isfile(out["manifest"])
    call.error("conflict", "export_layers", doc=doc, dir=os.path.join(r, "out"))
    config_override.restore()


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
