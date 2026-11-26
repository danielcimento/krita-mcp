"""Core lint rules (F004 survey.md, category 2): problems for any agent
working in Krita, whatever the document is for. Pure: stdlib only.

All four are in the ``default`` set; the config's ``lint.disable`` takes
one out. ``duplicate-name`` is a warning (bare-name refs to the copies are
ambiguous, D4); a pack can raise it and suggest the renames (the Live2D
conventions make it an error with pose-suffix names, ``live2d.py``).
"""

from .engine import (GROUP, PACKS, PAINT, alpha, checked_nodes, finding, pack_on, plural, rule,
                     settings)


def _rename_suggestion(name, nodes, by_id, s):
    """``(text, [{id, name}] | None)``: how to tell the copies of ``name``
    apart. The first enabled pack with a suggestion wins; otherwise an index
    suffix, as text only (which copy is which is the artist's call)."""
    for p in PACKS.values():
        if p.suggest_renames and pack_on(s, p.name):
            got = p.suggest_renames(name, nodes, by_id, s)
            if got:
                return got
    shown = " / ".join(f"'{name} {i}'" for i in range(1, min(len(nodes), 3) + 1))
    return (f"rename them to say what differs, or add an index: {shown}"
            f"{' / …' if len(nodes) > 3 else ''}", None)


@rule("duplicate-name", "warning")
def duplicate_name(snapshot):
    """Names must be unique among groups and, separately, among layers
    (paint layers and the other layer types): a group may share its name
    with a layer, e.g. the Face group holding the Face layer (F003 K6). One
    finding per clashing name and kind; the message lists the copies'
    paths and a fix, ``suggested_names`` the new names when a pack can
    work them out."""
    s = settings(snapshot)
    by_id = {n["id"]: n for n in snapshot["nodes"]}
    by_name = {}
    for n in checked_nodes(snapshot):
        kind = "group" if n.get("type") == GROUP else "layer"
        by_name.setdefault((kind, n.get("name")), []).append(n)
    out = []
    for (kind, name), nodes in by_name.items():
        if len(nodes) < 2:
            continue
        if kind == "group":
            what, rule_text = "groups", "group names must be unique (a layer may share one)"
        else:
            what = "paint layers" if all(n.get("type") == PAINT for n in nodes) else "layers"
            rule_text = f"{what[:-1]} names must be unique (a group may share one)"
        paths = ", ".join(f"'{n.get('path')}'" for n in nodes)
        fix, suggested = _rename_suggestion(name, nodes, by_id, s)
        f = finding("duplicate-name", nodes[1], (
            f"Name '{name}' is used by {len(nodes)} {what} ({paths}); {rule_text}: {fix}"),
            [n["id"] for n in nodes])
        if suggested:
            f["suggested_names"] = suggested
        out.append(f)
    return out


@rule("empty-layer", "warning")
def empty_layer(snapshot):
    out = []
    for n in checked_nodes(snapshot):
        a = alpha(n)
        if a is not None and a.get("pixel_count") == 0:
            out.append(finding("empty-layer", n, f"Paint layer '{n.get('path')}' has no pixels; "
                                                 "paint it or delete it"))
    return out


@rule("out-of-canvas", "warning")
def out_of_canvas(snapshot):
    doc = snapshot["document"]
    w, h = doc.get("width"), doc.get("height")
    out = []
    for n in checked_nodes(snapshot):
        a = alpha(n)
        r = a.get("opaque_bounds") if a else None
        if not r or w is None or h is None:
            continue
        over = [("left", -r["x"]), ("above", -r["y"]),
                ("right", r["x"] + r["w"] - w), ("below", r["y"] + r["h"] - h)]
        over = [f"{px} px {side}" for side, px in over if px > 0]
        if over:
            out.append(finding("out-of-canvas", n, (
                f"Paint layer '{n.get('path')}' has pixels outside the {w}x{h} canvas "
                f"({', '.join(over)}); move them in or trim them")))
    return out


@rule("stray-alpha", "warning")
def stray_alpha(snapshot):
    """Stray specks (P5.10): ``alpha.stray_specks.count > 0``. Larger stray
    regions (soft glows) are not flagged. ``truncated`` (too many runs to
    label) is a finding of its own. Without ``stray_specks`` (an older
    collector): ``stray_count > 0``."""
    s = settings(snapshot)
    below, dist = s["stray_alpha_below"], s["stray_alpha_distance"]
    stray = f"alpha below {below}, more than {dist} px from any pixel with alpha {below} or more"
    out = []
    for n in checked_nodes(snapshot):
        a = alpha(n)
        if not a:
            continue
        path = n.get("path")
        specks = a.get("stray_specks")
        if specks is None:
            count = a.get("stray_count")
            if count:
                out.append(finding("stray-alpha", n, (
                    f"Paint layer '{path}' has {plural(count, 'stray pixel')} ({stray}); erase them")))
        elif specks.get("truncated"):
            out.append(finding("stray-alpha", n, (
                f"Paint layer '{path}' has {plural(a.get('stray_count') or 0, 'stray pixel')} ({stray}), "
                "too fragmented to analyse for specks; look for noise and erase it")))
        elif specks.get("count"):
            count, shown = specks["count"], (specks.get("samples") or [])[:3]
            where = ", ".join(f"({r['x']}, {r['y']}) {r['w']}x{r['h']}" for r in shown)
            if where:
                where = f", {'e.g. ' if count > len(shown) else ''}at {where}"
            box = specks.get("bounds")
            within = (f", all within ({box['x']}, {box['y']}) {box['w']}x{box['h']}"
                      if box and count > len(shown) else "")
            f = finding("stray-alpha", n, (
                f"Paint layer '{path}' has {plural(count, 'stray speck')}, {specks.get('pixels', 0)} px "
                f"(groups of up to {s['stray_speck_max_px']} px with {stray}){where}{within}; "
                f"erase them, or clear them with krita_trim_layers(ids=[...], "
                f"alpha_threshold={below}, near_px={dist})"))
            # K7: enough to decide on a trim without rendering.
            f["pixels"] = specks.get("pixels", 0)
            f["bounds"] = box
            out.append(f)
    return out
