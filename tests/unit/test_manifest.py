"""kritamcp.manifest (P2.4): manifest v1 structure checks and spec conversion."""

import importlib.util
import json
import os

import pytest

from kritamcp import batch, manifest
from kritamcp.errors import CommandError

TOOLS = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "tools")
_spec = importlib.util.spec_from_file_location("ora_to_manifest",
                                               os.path.join(TOOLS, "ora_to_manifest.py"))
otm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otm)

DIR = "/work/export"


def doc(*layers, **top):
    return {"version": 1, "generator": "test", "layers": list(layers), **top}


def group(key, parent="/", **kw):
    return {"key": key, "name": key, "type": "group", "parent": parent, **kw}


def paint(key, parent="/", **kw):
    return {"key": key, "name": key, "type": "paint", "parent": parent, **kw}


def err(m, d=DIR):
    with pytest.raises(CommandError) as e:
        manifest.to_specs(m, d)
    return e.value


# --- happy path ------------------------------------------------------------

def test_happy_path():
    m = doc(
        group("G", id="{a}", path="G", stack_index=0, pass_through=True),
        group("H", "$G", path="G/H", stack_index=1),
        paint("A", "$H", png="sub/../0003_a.png", x=3, y=4, w=10, h=20, opacity=0.5,
              blend_mode="multiply", stack_index=2, id="{b}"),
        paint("B", "$H", png=None, above="$A", stack_index=3, x=7, y=8),
        {"key": "P", "name": "G", "type": "group", "parent": "/", "projection": True,
         "png": "0005_g.png", "x": 0, "y": 0},
        paint("C", visible=False, locked=True, alpha_locked=True, inherit_alpha=True),
        document={"name": "mao", "width": 64, "height": 48},
    )
    specs, skipped, document = manifest.to_specs(m, DIR + "/")
    assert [s["key"] for s in specs] == ["G", "H", "A", "B", "C"]
    assert specs[0] == {"key": "G", "name": "G", "type": "group", "parent": "/",
                        "pass_through": True}
    assert specs[2] == {"key": "A", "name": "A", "type": "paint", "parent": "$H",
                        "png": "/work/export/0003_a.png", "x": 3, "y": 4, "opacity": 0.5,
                        "blend_mode": "multiply"}
    assert "png" not in specs[3] and specs[3]["above"] == "$A"
    assert "x" not in specs[3] and "y" not in specs[3]  # x/y without a png are dropped
    assert specs[4]["visible"] is False and specs[4]["inherit_alpha"] is True
    for s in specs:
        assert set(s) <= manifest.SPEC_FIELDS
    assert skipped == [{"index": 4, "key": "P", "reason": skipped[0]["reason"]}]
    assert "projection" in skipped[0]["reason"]
    assert document == {"name": "mao", "width": 64, "height": 48}


def test_defaults_document_optional_and_relative_dir():
    specs, skipped, document = manifest.to_specs(
        {"version": 1, "layers": [{"key": "a", "name": "a", "png": "x.png"}]}, "rel/dir")
    assert document == {} and skipped == []
    assert specs == [{"key": "a", "name": "a", "png": os.path.join(os.getcwd(), "rel/dir/x.png")}]


def test_png_not_realpathed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    specs, _, _ = manifest.to_specs(doc(paint("a", png="a.png")), str(link))
    assert specs[0]["png"] == str(link / "a.png")


def test_field_values_not_checked_here():
    specs, _, _ = manifest.to_specs(doc(paint("a", opacity=7, blend_mode="nonsense")), DIR)
    assert specs[0]["opacity"] == 7 and specs[0]["blend_mode"] == "nonsense"


# --- top-level errors --------------------------------------------------------

@pytest.mark.parametrize("m", [{"layers": [paint("a")]}, doc(paint("a"), version=2),
                               doc(paint("a"), version="1"), doc(paint("a"), version=True)])
def test_version(m):
    e = err(m)
    assert e.code == "bad_request" and "supported versions: 1" in e.message
    assert e.details["supported"] == [1]


@pytest.mark.parametrize("m,code", [
    ([], "bad_request"),
    (doc(), "bad_request"),
    ({"version": 1}, "bad_request"),
    ({"version": 1, "layers": {}}, "bad_request"),
    (doc(paint("a"), extra=1), "bad_request"),
    (doc(paint("a"), document=[]), "bad_request"),
    (doc(paint("a"), generator=3), "bad_request"),
])
def test_top_level_errors(m, code):
    assert err(m).code == code


def test_too_many_layers():
    m = doc(*[paint(f"k{i}") for i in range(batch.MAX_ITEMS + 1)])
    e = err(m)
    assert e.code == "too_large" and e.details["limit"] == batch.MAX_ITEMS


def test_max_layers_ok():
    m = doc(*[paint(f"k{i}") for i in range(batch.MAX_ITEMS)])
    assert len(manifest.to_specs(m, DIR).specs) == batch.MAX_ITEMS


# --- entry errors ------------------------------------------------------------

@pytest.mark.parametrize("bad,needle", [
    ("nope", "must be an object"),
    ({"name": "a"}, "key is required"),
    ({"key": "", "name": "a"}, "key is required"),
    ({"key": 5, "name": "a"}, "key is required"),
    (paint("a", type="vector"), "type"),
    (paint("x"), "duplicate key"),
    (paint("b", parent="Face"), "by key only"),
    (paint("b", parent="Face/Eyes"), "by key only"),
    (paint("b", parent="{12345678-1234-1234-1234-123456789abc}"), "by key only"),
    (paint("b", parent=None), "by key only"),
    (paint("b", parent="$"), "by key only"),
    (paint("b", parent="$zzz"), "no earlier entry"),
    (paint("b", parent="$p"), "not a group"),
    (paint("b", above="Face"), "by key only"),
    (paint("b", above="$zzz"), "no earlier entry"),
    (paint("b", above="$p"), "not a sibling"),
    (paint("b", parent="$g", above="$x"), "not a sibling"),
    (paint("b", png=""), "png must be"),
    (paint("b", png=3), "png must be"),
    (paint("b", png="a\x00.png"), "png must be"),
    (paint("b", png="/abs/a.png"), "relative to the manifest"),
    (paint("b", projection="yes"), "projection must be"),
])
def test_entry_errors(bad, needle):
    m = doc(paint("x"), group("g"), paint("p", "$g"), bad)
    e = err(m)
    assert e.code == "bad_request"
    assert e.details["index"] == 3
    assert e.message.startswith("layers[3]: ")
    assert needle in e.message


def test_unknown_keys_listed():
    e = err(doc(paint("a"), paint("b", colour="red", Opacity=1)))
    assert e.details == {"index": 1, "field": "layers", "keys": ["Opacity", "colour"]}
    assert "unknown keys" in e.message


def test_parent_defined_later():
    e = err(doc(paint("a", parent="$g"), group("g")))
    assert e.details["index"] == 0 and "defined later" in e.message


def test_above_sibling_ok():
    specs, _, _ = manifest.to_specs(doc(group("g"), paint("a", "$g"), paint("b", "$g", above="$a"),
                                        paint("c", above="$g")), DIR)
    assert specs[2]["above"] == "$a" and specs[3]["above"] == "$g"


def test_above_defined_later():
    e = err(doc(paint("a", above="$b"), paint("b")))
    assert e.details["index"] == 0 and "defined later" in e.message


@pytest.mark.parametrize("field", ["parent", "above"])
def test_projection_cannot_be_referenced(field):
    proj = group("P", projection=True)
    e = err(doc(proj, paint("a", **{field: "$P"})))
    assert e.details["index"] == 1 and "projection" in e.message


def test_projection_entries_still_checked():
    # A skipped entry still needs a unique key and no unknown fields.
    assert err(doc(paint("a"), {"name": "P", "projection": True})).details["index"] == 1
    assert err(doc(paint("a"), group("a", projection=True))).details["index"] == 1
    assert err(doc(group("P", projection=True, bogus=1))).details["keys"] == ["bogus"]


def test_only_projections():
    specs, skipped, _ = manifest.to_specs(doc(group("P", projection=True)), DIR)
    assert specs == [] and len(skipped) == 1


# --- read --------------------------------------------------------------------

def read_err(path):
    with pytest.raises(CommandError) as e:
        manifest.read(str(path))
    return e.value


def test_read_bad_json(tmp_path):
    p = tmp_path / "m.json"
    p.write_text('{"version": 1,\n  "layers": [}')
    e = read_err(p)
    assert e.code == "bad_request" and e.details["line"] == 2 and "line 2" in e.message
    assert set(e.details) == {"line", "column", "pos"}


@pytest.mark.parametrize("text", ["[1, 2]", '"x"', "null", "3"])
def test_read_not_object(tmp_path, text):
    p = tmp_path / "m.json"
    p.write_text(text)
    e = read_err(p)
    assert e.code == "bad_request" and "object" in e.message


@pytest.mark.parametrize("text,needle", [
    ('{"version": 1, "version": 2}', "duplicate"),
    ('{"opacity": NaN}', "NaN"),
])
def test_read_rejects(tmp_path, text, needle):
    p = tmp_path / "m.json"
    p.write_text(text)
    e = read_err(p)
    assert e.code == "bad_request" and needle in e.message


def test_read_not_utf8(tmp_path):
    p = tmp_path / "m.json"
    p.write_bytes(b'{"name": "\xff"}')
    assert read_err(p).code == "bad_request"


def test_read_bom_ok(tmp_path):
    p = tmp_path / "m.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc(paint("a"))).encode())
    assert manifest.read(str(p))["version"] == 1


def test_read_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "MAX_BYTES", 100)
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"pad": "x" * 200}))
    e = read_err(p)
    assert e.code == "too_large" and e.details["limit"] == 100


def test_read_missing(tmp_path):
    assert read_err(tmp_path / "nope.json").code == "not_found"


# --- build / write -----------------------------------------------------------

def test_build_write_read_round_trip(tmp_path):
    layers = [group("G", stack_index=0), paint("Ä", "$G", png="0002_a.png", x=1, y=2,
                                              w=3, h=4, stack_index=1)]
    m = manifest.build({"name": "doc", "width": 8, "height": 8}, layers)
    assert m["version"] == 1 and m["generator"] == "krita-mcp"
    p = tmp_path / "manifest.json"
    manifest.write(str(p), m)
    assert "Ä" in p.read_text(encoding="utf-8")  # ensure_ascii=False
    assert os.listdir(tmp_path) == ["manifest.json"]  # no temp file left
    back = manifest.read(str(p))
    assert back == m
    specs, skipped, document = manifest.to_specs(back, str(tmp_path))
    assert specs[1]["png"] == str(tmp_path / "0002_a.png") and skipped == []
    assert document["width"] == 8


def test_write_replaces_and_cleans_up_on_failure(tmp_path):
    p = tmp_path / "manifest.json"
    manifest.write(str(p), manifest.build({}, [paint("a")]))
    with pytest.raises(TypeError):
        manifest.write(str(p), {"bad": object()})
    assert os.listdir(tmp_path) == ["manifest.json"]
    assert manifest.read(str(p))["layers"][0]["key"] == "a"  # old content kept


# --- integration with tools/ora_to_manifest ---------------------------------

def _png(w, h):
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x00" * (4 * w) for _ in range(h))
    return (otm.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_ora_to_manifest_output_loads(tmp_path):
    import zipfile
    stack = ('<layer name="Top" src="data/top.png" x="5" y="6" composite-op="svg:multiply"/>'
             '<stack name="Face" isolation="auto">'
             '<stack name="Eyes" visibility="hidden"><layer name="Dot" src="data/dot.png"/></stack>'
             '<layer name="Skin" src="data/skin.png" alpha-preserve="true"/>'
             '<layer name="Empty"/>'
             '</stack>'
             '<layer name="Bg" src="data/bg.png"/>')
    ora = tmp_path / "t.ora"
    with zipfile.ZipFile(ora, "w") as zf:
        zf.writestr("mimetype", "image/openraster", compress_type=zipfile.ZIP_STORED)
        zf.writestr("stack.xml", '<?xml version="1.0"?><image w="16" h="12"><stack>'
                    + stack + "</stack></image>")
        for n in ("top", "dot", "skin", "bg"):
            zf.writestr(f"data/{n}.png", _png(2, 3))
    out = tmp_path / "out"
    produced = otm.convert(str(ora), str(out))

    m = manifest.read(str(out / "manifest.json"))
    specs, skipped, document = manifest.to_specs(m, str(out))
    assert skipped == [] and document["width"] == 16
    assert len(specs) == len(produced["layers"]) == 7
    by_name = {s["name"]: s for s in specs}
    face, eyes = by_name["Face"], by_name["Eyes"]
    assert by_name["Dot"]["parent"] == "$" + eyes["key"]
    assert eyes["parent"] == "$" + face["key"]
    assert by_name["Skin"]["parent"] == "$" + face["key"]
    assert by_name["Top"]["parent"] == by_name["Bg"]["parent"] == "/"
    assert "png" not in by_name["Empty"]
    for s in specs:
        assert set(s) <= manifest.SPEC_FIELDS
        if "png" in s:
            assert os.path.isfile(s["png"]) and os.path.dirname(s["png"]) == str(out)


def test_slug_and_png_name_match_ora_to_manifest():
    for name in ("Hair Back Shadow Multiply", "Eye/L", "  ", "", "[ Guide Image ]",
                 "Ünïcödé", "a" * 80 + "-b", "x--y__z", "L0001"):
        assert manifest.slug(name) == otm.slug(name), name
    assert manifest.png_name(2, "Hair Back Shadow Multiply") == "0002_hair-back-shadow-multiply.png"
    assert manifest.png_name(12, "") == "0012_layer.png"
