"""Live checks of F005 (smoke test revision 3) against the GUI Krita,
through server.py's tools: painting reports what changed and whether it
shows (K11), lean list_layers rows (K12), "did you mean" on not_found
(K13), lint's fix once per group (K14) and the server's own version in
krita_health (K15). Run with KRITA_MCP_LIVE=1. The plugin features need
Krita restarted on this checkout's plugin (0.5.0); those tests fail and
say so on an older one.
"""

import pytest

from krita_mcp_server import version


@pytest.fixture
def plugin_05(call):
    found = call.health().get("version")
    if found != version.SERVER_VERSION:
        pytest.fail(f"Krita runs plugin {found}; restart Krita so it loads this checkout's "
                    f"plugin ({version.SERVER_VERSION})", pytrace=False)


def test_health_reports_the_server_version(call):
    health = call.health()
    assert health["server_version"] == version.SERVER_VERSION
    commit, dirty = version.read_commit()
    assert (health["server_commit"], health["server_dirty"]) == (commit, dirty)
    assert f"server {version.SERVER_VERSION}" in health["summary"]


def test_painting_a_covered_layer_says_so(call, docs_to_close, plugin_05):
    doc = call("new_canvas", width=64, height=64, background="transparent",
               name="f005-covered")["doc"]
    docs_to_close.append(doc)
    ids = {c["name"]: c["id"] for c in call("create_layers", doc=doc, specs=[
        {"name": "Choker"}, {"name": "Neck"}])["created"]}
    call("fill", doc=doc, layer="Neck", x=32, y=32, radius=30, color="#e0c0a0")
    painted = call("draw_shape", doc=doc, layer="Choker", shape="rectangle", x=24, y=24,
                   width=16, height=8, color="#00ff00")
    assert painted["changed"] == {"x": 24, "y": 24, "w": 16, "h": 8}
    assert (painted["changed_px"], painted["layer_visible"], painted["covered_pct"]) == (
        128, True, 1.0)
    assert painted["top_cover"] == {"id": ids["Neck"], "path": "Neck"}
    assert [n["code"] for n in painted["notes"]] == ["change_covered"]
    assert "isn't visible in the composite" in painted["summary"]
    shown = call("draw_shape", doc=doc, layer="Neck", shape="rectangle", x=0, y=0, width=4,
                 height=4, color="#000000")
    assert shown["covered_pct"] == 0.0 and "notes" not in shown


def test_list_layers_rows_and_not_found(call, docs_to_close, plugin_05):
    doc = call("new_canvas", width=32, height=32, name="f005-rows")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[{"key": "h", "name": "Hat", "type": "group"},
                                          {"name": "Hat Brim", "parent": "$h"},
                                          {"name": "Hat Top", "parent": "$h"}])
    rows = call("list_layers", doc=doc, fields=None, limit=30)["layers"]
    hat = next(r for r in rows if r["name"] == "Hat")
    assert "children" not in hat and hat["child_count"] == 2
    message = call.error("not_found", "occlusion_map", doc=doc, id="Hat/Hat",
                         path="f005-hat.png")
    assert "did you mean 'Hat/Hat Top', 'Hat/Hat Brim'" in message


def test_lint_fix_once_per_group(call, docs_to_close, plugin_05):
    doc = call("new_canvas", width=32, height=32, name="f005-lint")["doc"]
    docs_to_close.append(doc)
    call("create_layers", doc=doc, specs=[{"name": "Spare 1"}, {"name": "Spare 2"}])
    group = call("lint", doc=doc, rules=["empty-layer"], group_by="rule",
                 limit=5)["by_rule"]["empty-layer"]
    assert group["fix"] == "paint it or delete it"
    assert all("fix" not in item and item["message"].endswith("has no pixels")
               for item in group["items"])
