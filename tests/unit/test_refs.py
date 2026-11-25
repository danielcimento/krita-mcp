"""Layer refs (D4) over a fake node index."""

import os
import subprocess
import sys

import pytest

from kritamcp.errors import CommandError
from kritamcp.refs import (ROOT, NodeIndex, escape_name, join_path, normalize_uuid,
                           resolve, resolve_many, split_path)


def uid(n):
    return f"{{{n:08x}-0000-4000-8000-{n:012x}}}"


SEL, SEL_CHILD = uid(1), uid(2)
HEAD, EYES, PUPILS, PUPIL_L, EYEBALL_L = uid(10), uid(11), uid(12), uid(13), uid(14)
HAIR1, G_FACE, L_FACE, LR_MARK, SHADOW1, SHADOW2 = uid(15), uid(16), uid(17), uid(18), uid(19), uid(20)
SIDE_A, HAND, SIDE_B, FOOT, LOCAL_SEL = uid(30), uid(31), uid(32), uid(33), uid(34)
BACK, HAIR2, BACKSLASH, DOLLAR = uid(40), uid(41), uid(42), uid(43)
UUID_NAMED, BG = uid(50), uid(51)
UUID_LIKE_NAME = "{abcdef01-2345-4678-9abc-def012345678}"

# Global stacking order, top to bottom, depth-first.
ENTRIES = [
    (SEL, "Selection Mask", None, "selectionmask"),
    (SEL_CHILD, "Under Selection", SEL, "paintlayer"),
    (HEAD, "Head", None, "grouplayer"),
    (EYES, "Eyes", HEAD, "grouplayer"),
    (PUPILS, "Pupils", EYES, "grouplayer"),
    (PUPIL_L, "Pupil L", PUPILS, "paintlayer"),
    (EYEBALL_L, "Eyeball L", EYES, "paintlayer"),
    (HAIR1, "Hair", HEAD, "paintlayer"),
    (G_FACE, "Face", HEAD, "grouplayer"),
    (L_FACE, "Face", G_FACE, "paintlayer"),
    (LR_MARK, "L/R Mark", G_FACE, "paintlayer"),
    (SHADOW1, "Shadow", G_FACE, "paintlayer"),
    (SHADOW2, "Shadow", G_FACE, "paintlayer"),
    (SIDE_A, "Side", None, "grouplayer"),
    (HAND, "Hand", SIDE_A, "paintlayer"),
    (SIDE_B, "Side", None, "grouplayer"),
    (FOOT, "Foot", SIDE_B, "paintlayer"),
    (LOCAL_SEL, "Local Sel", FOOT, "selectionmask"),
    (BACK, "Back", None, "grouplayer"),
    (HAIR2, "Hair", BACK, "paintlayer"),
    (BACKSLASH, "Back\\Slash", BACK, "paintlayer"),
    (DOLLAR, "$money", BACK, "paintlayer"),
    (UUID_NAMED, UUID_LIKE_NAME, None, "paintlayer"),
    (BG, "Background", None, "paintlayer"),
]


@pytest.fixture
def idx():
    return NodeIndex([{"id": i, "name": n, "parent": p, "type": t} for i, n, p, t in ENTRIES])


def err(fn, *args, **kw):
    with pytest.raises(CommandError) as ei:
        fn(*args, **kw)
    return ei.value


# --- normalize_uuid ------------------------------------------------------

@pytest.mark.parametrize("s", [
    "{c0cc968f-d678-4a81-88fd-c9210f662cea}",
    "c0cc968f-d678-4a81-88fd-c9210f662cea",
    "{C0CC968F-D678-4A81-88FD-C9210F662CEA}",
    "C0cc968f-D678-4a81-88fd-c9210f662CEA",
])
def test_normalize_uuid(s):
    assert normalize_uuid(s) == "{c0cc968f-d678-4a81-88fd-c9210f662cea}"


@pytest.mark.parametrize("s", [
    "{c0cc968f-d678-4a81-88fd-c9210f662cea", "c0cc968f-d678-4a81-88fd-c9210f662cea}",
    "c0cc968fd6784a8188fdc9210f662cea", "{g0cc968f-d678-4a81-88fd-c9210f662cea}",
    "c0cc968f-d678-4a81-88fd-c9210f662ce", " c0cc968f-d678-4a81-88fd-c9210f662cea",
    "c0cc968f-d678-4a81-88fd-c9210f662cea\n", "", None, 5,
])
def test_normalize_uuid_rejects(s):
    assert normalize_uuid(s) is None


# --- escaping and paths ----------------------------------------------------

def test_escape_name():
    assert escape_name("L/R Mark") == r"L\/R Mark"
    assert escape_name("a\\b") == r"a\\b"
    assert escape_name(r"x\/y") == r"x\\\/y"


@pytest.mark.parametrize("names", [
    ["Head"], ["Head", "Eyes", "Eyeball L"], ["L/R Mark"], ["a\\b", "/", "\\/", "c\\"],
    ["$x", "{c0cc968f-d678-4a81-88fd-c9210f662cea}"], [" spaced "],
])
def test_split_join_round_trip(names):
    path = join_path(names)
    assert split_path(path) == names
    assert split_path("/" + path) == names


def test_split_path_forms():
    assert split_path("/") == []
    assert join_path([]) == "/"
    assert split_path("/Head/Eyes") == split_path("Head/Eyes") == ["Head", "Eyes"]
    assert split_path(r"Face/L\/R Mark") == ["Face", "L/R Mark"]
    assert split_path(r"Back/Back\\Slash") == ["Back", "Back\\Slash"]


@pytest.mark.parametrize("bad", [
    "", "a//b", "a/", "//a", "/a/", r"a\b", r"a\ b", "a\\", r"\n", r"a/b\x",
])
def test_split_path_errors(bad):
    assert err(split_path, bad).code == "bad_request"


def test_split_path_error_names_field():
    assert "parent" in err(split_path, "a//b", field="parent").message


# --- NodeIndex ---------------------------------------------------------

def test_selection_mask_is_not_indexed(idx):
    assert SEL not in idx and SEL_CHILD not in idx
    assert len(idx) == len(ENTRIES) - 2
    assert SEL not in idx.children(ROOT)
    assert idx.by_name("Selection Mask") == []
    # A selection mask below a layer is an ordinary node.
    assert LOCAL_SEL in idx and idx.children(FOOT) == [LOCAL_SEL]


def test_children_top_to_bottom(idx):
    assert idx.children(ROOT) == [HEAD, SIDE_A, SIDE_B, BACK, UUID_NAMED, BG]
    assert idx.children(HEAD) == [EYES, HAIR1, G_FACE]
    assert idx.children(G_FACE) == [L_FACE, LR_MARK, SHADOW1, SHADOW2]
    assert idx.children(BG) == []


def test_parent_get_order(idx):
    assert idx.parent(HEAD) == ROOT
    assert idx.parent(PUPIL_L) == PUPILS
    assert idx.get(HAIR2) == {"id": HAIR2, "name": "Hair", "parent": BACK, "type": "paintlayer"}
    assert idx.get(SEL) is None
    assert idx.order(HEAD) == 0
    assert [idx.order(i) for i in idx] == list(range(len(idx)))
    assert list(idx)[:3] == [HEAD, EYES, PUPILS]


def test_ancestors_and_is_descendant(idx):
    assert idx.ancestors(PUPIL_L) == [PUPILS, EYES, HEAD]
    assert idx.ancestors(HEAD) == []
    assert idx.is_descendant(PUPIL_L, HEAD)
    assert idx.is_descendant(PUPIL_L, PUPILS)
    assert idx.is_descendant(HEAD, ROOT)
    assert not idx.is_descendant(HEAD, HEAD)
    assert not idx.is_descendant(HEAD, PUPIL_L)
    assert not idx.is_descendant(HAIR2, HEAD)
    assert not idx.is_descendant(ROOT, HEAD)


def test_path_of(idx):
    assert idx.path_of(PUPIL_L) == "Head/Eyes/Pupils/Pupil L"
    assert idx.path_of(LR_MARK) == r"Head/Face/L\/R Mark"
    assert idx.path_of(BACKSLASH) == r"Back/Back\\Slash"
    assert idx.path_of(HEAD) == "Head"
    assert idx.path_of(ROOT) == "/"


def test_index_accepts_unbraced_upper_ids():
    raw = HEAD.strip("{}").upper()
    idx = NodeIndex([{"id": raw, "name": "Head", "parent": None, "type": "grouplayer"},
                     {"id": EYES.upper(), "name": "Eyes", "parent": raw, "type": "grouplayer"}])
    assert HEAD in idx and idx.parent(EYES) == HEAD


@pytest.mark.parametrize("entries", [
    [{"id": "nope", "name": "a", "parent": None, "type": "paintlayer"}],
    [{"id": HEAD, "name": "a", "parent": EYES, "type": "paintlayer"}],
    [{"id": HEAD, "name": "a", "parent": None, "type": "paintlayer"}] * 2,
])
def test_index_rejects_bad_entries(entries):
    with pytest.raises(ValueError):
        NodeIndex(entries)


# --- resolve: UUIDs ------------------------------------------------------

@pytest.mark.parametrize("form", [
    lambda u: u, lambda u: u.strip("{}"), lambda u: u.upper(), lambda u: u.strip("{}").upper(),
])
def test_resolve_uuid_forms(idx, form):
    assert resolve(idx, form(PUPIL_L)) == PUPIL_L


def test_resolve_unknown_uuid(idx):
    e = err(resolve, idx, uid(999))
    assert e.code == "not_found" and uid(999) in e.message


def test_selection_mask_unreachable(idx):
    for ref in (SEL, SEL.strip("{}"), "Selection Mask", "/Selection Mask", "Selection Mask/Under Selection",
                "Under Selection", SEL_CHILD):
        assert err(resolve, idx, ref).code == "not_found", ref


def test_uuid_wins_over_uuid_like_name(idx):
    # The name is a well-formed UUID, so the bare form is read as an id...
    assert err(resolve, idx, UUID_LIKE_NAME).code == "not_found"
    # ...and the layer is reached with a path.
    assert resolve(idx, "/" + UUID_LIKE_NAME) == UUID_NAMED


@pytest.mark.parametrize("ref,expected", [
    ("{c0cc968f-d678-4a81-88fd-c9210f662cea", "not_found"),   # malformed: a name, none such
    ("c0cc968fd6784a8188fdc9210f662cea", "not_found"),
])
def test_malformed_uuid_is_a_name(idx, ref, expected):
    e = err(resolve, idx, ref)
    assert e.code == expected and "named" in e.message


def test_malformed_uuid_name_resolves():
    name = "{c0cc968f-d678-4a81-88fd-c9210f662ce"
    idx = NodeIndex([{"id": HEAD, "name": name, "parent": None, "type": "paintlayer"}])
    assert resolve(idx, name) == HEAD


# --- resolve: paths ------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("Head/Eyes/Pupils/Pupil L", PUPIL_L),
    ("/Head/Eyes/Pupils/Pupil L", PUPIL_L),
    ("Head/Face", G_FACE),
    ("Head/Face/Face", L_FACE),
    ("/Head", HEAD),
    ("/Background", BG),
    (r"Head/Face/L\/R Mark", LR_MARK),
    (r"Back/Back\\Slash", BACKSLASH),
    ("Back/$money", DOLLAR),
    ("Back/Hair", HAIR2),
    ("Side/Hand", HAND),        # fans out over two `Side` groups, one match
    ("/Side/Foot", FOOT),
    ("Side/Foot/Local Sel", LOCAL_SEL),
])
def test_resolve_path(idx, ref, expected):
    assert resolve(idx, ref) == expected


def test_path_not_anchored_elsewhere(idx):
    # Paths start at the root: `Eyes/Pupils` is not found under Head.
    e = err(resolve, idx, "Eyes/Pupils")
    assert e.code == "not_found" and e.message == "No layer at path 'Eyes/Pupils'"
    assert err(resolve, idx, "/Face").code == "not_found"
    assert err(resolve, idx, "Head/head").code == "not_found"     # case-sensitive
    assert err(resolve, idx, "Head/Hair ").code == "not_found"    # no trimming


def test_unescaped_slash_in_name_hints(idx):
    e = err(resolve, idx, "Head/Face/L/R Mark")
    assert e.code == "not_found"
    e = err(resolve, NodeIndex([{"id": HEAD, "name": "L/R", "parent": None, "type": "paintlayer"}]), "L/R")
    assert e.code == "not_found" and r"L\/R" in e.message


def test_path_ambiguous(idx):
    e = err(resolve, idx, "Head/Face/Shadow")
    assert e.code == "ambiguous" and e.details == {"matches": [SHADOW1, SHADOW2]}
    e = err(resolve, idx, "/Side")
    assert e.code == "ambiguous" and e.details == {"matches": [SIDE_A, SIDE_B]}


@pytest.mark.parametrize("ref", ["Head//Eyes", "Head/", r"Head/E\yes", "Head/Eyes\\", "//"])
def test_path_malformed(idx, ref):
    assert err(resolve, idx, ref).code == "bad_request"


# --- resolve: bare names -------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("Pupil L", PUPIL_L), ("Head", HEAD), ("Hand", HAND), (r"L\/R Mark", LR_MARK),
    (r"Back\\Slash", BACKSLASH), ("Local Sel", LOCAL_SEL),
])
def test_resolve_bare_name(idx, ref, expected):
    assert resolve(idx, ref) == expected


def test_bare_name_ambiguous(idx):
    e = err(resolve, idx, "Face")
    assert e.code == "ambiguous" and e.details == {"matches": [G_FACE, L_FACE]}
    assert e.message == "2 layers named 'Face': use an id or a path"
    e = err(resolve, idx, "Hair")
    assert e.details == {"matches": [HAIR1, HAIR2]}


def test_bare_name_not_found(idx):
    e = err(resolve, idx, "Nose")
    assert e.code == "not_found" and e.message == "No layer named 'Nose'"
    assert err(resolve, idx, "face").code == "not_found"
    assert err(resolve, idx, " Head").code == "not_found"


@pytest.mark.parametrize("ref", [r"Back\Slash", "trailing\\"])
def test_bare_name_bad_escape(idx, ref):
    assert err(resolve, idx, ref).code == "bad_request"


# --- resolve: root, keys, bad input ---------------------------------------

def test_root(idx):
    assert resolve(idx, "/", allow_root=True) == ROOT
    e = err(resolve, idx, "/", field="above")
    assert e.code == "bad_request" and "above" in e.message and "parent" in e.message


def test_batch_keys(idx):
    keys = {"hair": HAIR2, "new": uid(777)}
    assert resolve(idx, "$hair", keys=keys) == HAIR2
    assert resolve(idx, "$new", keys=keys) == uid(777)   # not yet in the index: fine
    e = err(resolve, idx, "$nope", keys=keys)
    assert e.code == "not_found" and e.details == {"key": "nope"}
    assert err(resolve, idx, "$nope", keys={}).code == "not_found"
    e = err(resolve, idx, "$hair", field="parent")
    assert e.code == "bad_request" and "parent" in e.message
    assert err(resolve, idx, "$", keys=keys).code == "bad_request"


@pytest.mark.parametrize("ref", [None, 3, ["Head"], {"id": HEAD}, ""])
def test_bad_ref_type(idx, ref):
    e = err(resolve, idx, ref, field="parent")
    assert e.code == "bad_request" and "parent" in e.message


def test_resolve_many(idx):
    assert resolve_many(idx, ["Head", PUPIL_L, "Back/Hair"]) == [HEAD, PUPIL_L, HAIR2]
    e = err(resolve_many, idx, ["Head", "Face"])
    assert e.code == "ambiguous" and e.details == {"matches": [G_FACE, L_FACE], "index": 1}
    e = err(resolve_many, idx, ["Head", 7])
    assert e.details == {"index": 1} and "ids[1]" in e.message
    assert err(resolve_many, idx, "Head").code == "bad_request"


# --- round trip ----------------------------------------------------------

def test_path_of_round_trips(idx):
    paths = [idx.path_of(i) for i in idx]
    for nid, path in zip(idx, paths):
        if paths.count(path) > 1:
            assert err(resolve, idx, path).code == "ambiguous"
            continue
        assert resolve(idx, "/" + path) == nid, path
        # Unanchored works too unless a top-level name is shared elsewhere.
        if "/" in path.replace("\\/", "") or len(idx.by_name(idx.get(nid)["name"])) == 1:
            if normalize_uuid(path) is None:
                assert resolve(idx, path) == nid, path


def test_module_is_pure():
    code = ("import sys, kritamcp.refs\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('krita', 'PyQt5')]\n"
            "assert not bad, bad\n")
    plugin = os.path.join(os.path.dirname(__file__), "..", "..", "krita-plugin")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=plugin)
