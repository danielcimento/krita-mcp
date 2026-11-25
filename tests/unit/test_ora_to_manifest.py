"""tools/ora_to_manifest.py (P2.6) on small synthetic .ora files."""

import importlib.util
import json
import os
import struct
import subprocess
import sys
import zipfile
import zlib

import pytest

from kritamcp.refs import escape_name as refs_escape_name

TOOL = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "tools", "ora_to_manifest.py")
_spec = importlib.util.spec_from_file_location("ora_to_manifest", TOOL)
otm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otm)


def png_bytes(w, h):
    """A w×h transparent RGBA PNG, written by hand."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x00" * (4 * w) for _ in range(h))
    return (otm.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_ora(path, stack, w=64, h=48, pngs=None, image_attrs=""):
    """Write an .ora with ``stack`` as the root stack's inner XML."""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<image version="0.0.3" w="{w}" h="{h}"{image_attrs}><stack>{stack}</stack></image>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "image/openraster", compress_type=zipfile.ZIP_STORED)
        zf.writestr("stack.xml", xml)
        for name, data in (pngs or {}).items():
            zf.writestr(name, data)
    return path


def layer(name, src=None, **attrs):
    a = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    s = f' src="{src}"' if src else ""
    return f'<layer name="{name}"{s}{a}/>'


def by_name(manifest):
    return {l["path"]: l for l in manifest["layers"]}


# Top to bottom as in stack.xml: Top, Face/(Eyes/(Pupil/Dot, Iris), Face), Bg.
NESTED = (
    layer("Top", "data/top.png", x=5, y=6, opacity="0.5", composite_op="svg:multiply")
    + '<stack name="Face" isolation="auto">'
    + '<stack name="Eyes" isolation="isolate" visibility="hidden">'
    + '<stack name="Pupil">' + layer("Dot", "data/dot.png") + "</stack>"
    + layer("Iris", "data/iris.png", composite_op="svg:plus")
    + "</stack>"
    + layer("Face", "data/face.png", composite_op="krita:linear_dodge")
    + "</stack>"
    + layer("Bg", "data/bg.png", visibility="hidden")
)
NESTED_PNGS = {f"data/{n}.png": png_bytes(i + 1, i + 2)
               for i, n in enumerate(["top", "dot", "iris", "face", "bg"])}


@pytest.fixture
def nested(tmp_path):
    ora = make_ora(tmp_path / "rig.ora", NESTED, pngs=NESTED_PNGS, image_attrs=' xres="300" yres="300"')
    out = tmp_path / "out"
    return otm.convert(str(ora), str(out)), out


def test_create_order_keys_parents_and_stack_index(nested):
    m, _ = nested
    rows = [(l["key"], l["path"], l["type"], l["parent"], l["stack_index"]) for l in m["layers"]]
    assert rows == [
        ("L0001", "Bg", "paint", "/", 0),
        ("L0002", "Face", "group", "/", 1),
        ("L0003", "Face/Face", "paint", "$L0002", 2),
        ("L0004", "Face/Eyes", "group", "$L0002", 3),
        ("L0005", "Face/Eyes/Iris", "paint", "$L0004", 4),
        ("L0006", "Face/Eyes/Pupil", "group", "$L0004", 5),
        ("L0007", "Face/Eyes/Pupil/Dot", "paint", "$L0006", 6),
        ("L0008", "Top", "paint", "/", 7),
    ]


def test_document_and_manifest_file(nested):
    m, out = nested
    assert m["version"] == 1 and m["generator"] == "krita-mcp ora_to_manifest"
    assert m["document"] == {"name": "rig", "width": 64, "height": 48, "resolution": 300}
    assert json.loads((out / "manifest.json").read_text()) == m


def test_attributes(nested):
    m, _ = nested
    L = by_name(m)
    assert (L["Top"]["opacity"], L["Top"]["x"], L["Top"]["y"]) == (0.5, 5, 6)
    assert (L["Bg"]["opacity"], L["Bg"]["x"], L["Bg"]["y"]) == (1.0, 0, 0)
    assert [p for p, l in L.items() if not l["visible"]] == ["Bg", "Face/Eyes"]
    assert L["Top"]["blend_mode"] == "multiply"
    assert L["Face/Eyes/Iris"]["blend_mode"] == "add"
    assert L["Face/Face"]["blend_mode"] == "linear_dodge"
    assert L["Bg"]["blend_mode"] == "normal"
    for l in m["layers"]:
        if l["type"] == "paint":
            assert (l["inherit_alpha"], l["alpha_locked"], l["locked"]) == (False, False, False)
        else:
            assert "png" not in l and "x" not in l


def test_isolation_maps_like_krita(nested):
    """Krita's importer: only isolation="auto" is pass-through."""
    L = by_name(nested[0])
    assert L["Face"]["pass_through"] is True
    assert L["Face/Eyes"]["pass_through"] is False
    assert L["Face/Eyes/Pupil"]["pass_through"] is False  # no attribute


def test_pngs_copied_with_ihdr_size(nested):
    m, out = nested
    L = by_name(m)
    assert (L["Top"]["png"], L["Top"]["w"], L["Top"]["h"]) == ("0008_top.png", 1, 2)
    assert (L["Bg"]["w"], L["Bg"]["h"]) == (5, 6)
    assert (out / "0008_top.png").read_bytes() == NESTED_PNGS["data/top.png"]
    assert sorted(os.listdir(out)) == sorted(
        [l["png"] for l in m["layers"] if l["type"] == "paint"] + ["manifest.json"])


def test_duplicate_names_get_unique_pngs_and_escaped_paths(tmp_path):
    stack = ('<stack name="a/b">' + layer("x\\y", "data/1.png") + layer("x\\y", "data/2.png")
             + "</stack>" + layer("a/b", "data/1.png"))
    ora = make_ora(tmp_path / "d.ora", stack, pngs={"data/1.png": png_bytes(1, 1),
                                                    "data/2.png": png_bytes(2, 2)})
    m = otm.convert(str(ora), str(tmp_path / "out"))
    assert [l["path"] for l in m["layers"]] == ["a\\/b", "a\\/b", "a\\/b/x\\\\y", "a\\/b/x\\\\y"]
    pngs = [l["png"] for l in m["layers"] if l["type"] == "paint"]
    assert pngs == ["0001_a-b.png", "0003_x-y.png", "0004_x-y.png"]
    assert len(set(os.listdir(tmp_path / "out"))) == 4


def test_escape_matches_refs():
    for name in ["a/b", "x\\y", "\\/", "plain", "$k"]:
        assert otm.escape_name(name) == refs_escape_name(name)


def test_slug():
    assert otm.slug("  Hair Back / Shadow (Multiply)!! ") == "hair-back-shadow-multiply"
    assert otm.slug("日本") == "layer"
    assert len(otm.slug("ab " * 50)) <= 60 and not otm.slug("ab " * 50).endswith("-")


@pytest.mark.parametrize("op,mode", sorted(otm.BLEND_OPS.items()) + [("krita:linear_dodge", "linear_dodge"),
                                                                     ("krita:soft_light", "soft_light")])
def test_composite_op_mapping(tmp_path, op, mode):
    ora = make_ora(tmp_path / "b.ora", layer("L", composite_op=op))
    assert otm.convert(str(ora), str(tmp_path / "o"))["layers"][0]["blend_mode"] == mode


def test_required_mappings_present():
    for op, mode in {"svg:src-over": "normal", "svg:multiply": "multiply", "svg:plus": "add",
                     "svg:soft-light": "soft_light_svg", "svg:luminosity": "luminize",
                     "svg:difference": "diff", "svg:color-dodge": "dodge"}.items():
        assert otm.BLEND_OPS[op] == mode


def test_src_atop_alpha_preserve_edit_locked(tmp_path):
    stack = (layer("A", composite_op="svg:src-atop")
             + layer("B", composite_op="svg:multiply", alpha_preserve="true", edit_locked="true"))
    L = by_name(otm.convert(str(make_ora(tmp_path / "a.ora", stack)), str(tmp_path / "o")))
    assert (L["A"]["blend_mode"], L["A"]["alpha_locked"], L["A"]["locked"]) == ("normal", True, False)
    assert (L["B"]["blend_mode"], L["B"]["alpha_locked"], L["B"]["locked"]) == ("multiply", True, True)


def test_unknown_ops_all_listed(tmp_path):
    stack = (layer("A", composite_op="svg:exclusion") + '<stack name="G" composite-op="weird">'
             + layer("B", composite_op="krita:Bad Id") + "</stack>")
    out = tmp_path / "o"
    with pytest.raises(otm.OraError) as ei:
        otm.convert(str(make_ora(tmp_path / "u.ora", stack)), str(out))
    msgs = ei.value.problems
    assert len(msgs) == 3
    assert any("'A'" in p and "svg:exclusion" in p for p in msgs)
    assert any("'G'" in p and "'weird'" in p for p in msgs)
    assert any("'G/B'" in p and "krita:Bad Id" in p for p in msgs)
    assert not out.exists()


def test_opacity_clamped_and_bad_values(tmp_path):
    L = by_name(otm.convert(str(make_ora(tmp_path / "c.ora", layer("Hi", opacity="1.7")
                                         + layer("Lo", opacity="-2"))), str(tmp_path / "o")))
    assert (L["Hi"]["opacity"], L["Lo"]["opacity"]) == (1.0, 0.0)
    bad = layer("A", opacity="nan") + layer("B", x="1.5") + layer("C", visibility="maybe")
    with pytest.raises(otm.OraError) as ei:
        otm.convert(str(make_ora(tmp_path / "e.ora", bad)), str(tmp_path / "o2"))
    assert len(ei.value.problems) == 3


def test_layer_without_src_has_null_png(tmp_path):
    L = by_name(otm.convert(str(make_ora(tmp_path / "n.ora", layer("Empty", x=3))), str(tmp_path / "o")))
    assert (L["Empty"]["png"], L["Empty"]["w"], L["Empty"]["h"], L["Empty"]["x"]) == (None, None, None, 3)
    assert os.listdir(tmp_path / "o") == ["manifest.json"]


@pytest.mark.parametrize("src", ["../evil.png", "/etc/evil.png", "data/../../evil.png",
                                 "data\\evil.png", "C:/evil.png", "data/../evil.png"])
def test_path_traversal_rejected(tmp_path, src):
    ora = make_ora(tmp_path / "t.ora", layer("L", src), pngs={"data/ok.png": png_bytes(1, 1)})
    with pytest.raises(otm.OraError, match="unsafe src"):
        otm.convert(str(ora), str(tmp_path / "o"))
    assert not (tmp_path / "o").exists() and not (tmp_path / "evil.png").exists()


def test_missing_member_and_non_png(tmp_path):
    ora = make_ora(tmp_path / "m.ora", layer("A", "data/gone.png") + layer("B", "data/b.png"),
                   pngs={"data/b.png": b"not a png at all, just text......"})
    with pytest.raises(otm.OraError) as ei:
        otm.convert(str(ora), str(tmp_path / "o"))
    assert any("not in the archive" in p for p in ei.value.problems)
    assert any("not a PNG" in p for p in ei.value.problems)


def test_unreferenced_members_not_extracted(tmp_path):
    ora = make_ora(tmp_path / "x.ora", layer("A", "data/a.png"),
                   pngs={"data/a.png": png_bytes(1, 1), "data/other.png": png_bytes(1, 1)})
    otm.convert(str(ora), str(tmp_path / "o"))
    assert sorted(os.listdir(tmp_path / "o")) == ["0001_a.png", "manifest.json"]


def test_doctype_rejected(tmp_path):
    path = tmp_path / "x.ora"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("stack.xml", '<?xml version="1.0"?><!DOCTYPE image [<!ENTITY a "b">]>'
                                 '<image w="1" h="1"><stack/></image>')
    with pytest.raises(otm.OraError, match="DOCTYPE"):
        otm.convert(str(path), str(tmp_path / "o"))


def test_nonempty_out_dir_needs_overwrite(tmp_path):
    ora = make_ora(tmp_path / "o.ora", layer("A", "data/a.png"), pngs={"data/a.png": png_bytes(1, 1)})
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(otm.OraError, match="not empty"):
        otm.convert(str(ora), str(out))
    assert os.listdir(out) == ["keep.txt"]
    otm.convert(str(ora), str(out), overwrite=True)
    assert sorted(os.listdir(out)) == ["0001_a.png", "keep.txt", "manifest.json"]


def test_cli(tmp_path):
    ora = make_ora(tmp_path / "rig.ora", NESTED, pngs=NESTED_PNGS)
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-I", TOOL, str(ora), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "5 paint, 3 groups (max depth 3), 64x48" in r.stdout
    assert "2 hidden" in r.stdout and "1 duplicate names" in r.stdout
    r = subprocess.run([sys.executable, "-I", TOOL, str(ora), str(out)], capture_output=True, text=True)
    assert r.returncode == 1 and "not empty" in r.stderr
    r = subprocess.run([sys.executable, "-I", TOOL, str(ora), str(out), "--overwrite"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
