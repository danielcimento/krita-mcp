"""krita_mcp_server/models.py: pydantic models for tool list items."""

import pytest
from pydantic import ValidationError

from krita_mcp_server.models import LayerSpec


def test_minimal_spec_sends_only_name():
    assert LayerSpec(name="A").to_params() == {"name": "A"}


def test_false_and_zero_are_sent():
    spec = LayerSpec(name="A", visible=False, opacity=0, locked=False, x=0, y=0, png="a.png")
    assert spec.to_params() == {"name": "A", "visible": False, "opacity": 0.0, "locked": False,
                                "x": 0, "y": 0, "png": "a.png"}


def test_full_spec_round_trip():
    data = {"key": "k", "name": "A", "type": "group", "parent": "$p", "above": "B",
            "visible": True, "opacity": 0.5, "blend_mode": "add", "locked": True,
            "alpha_locked": False, "inherit_alpha": True, "pass_through": True,
            "png": "/x.png", "x": -3, "y": 4}
    assert LayerSpec.model_validate(data).to_params() == data


@pytest.mark.parametrize("data", [
    {"name": "A", "opactiy": 0.5},          # typo: extra=forbid
    {"name": "A", "blend": "add"},
    {},                                      # name required
    {"name": "A", "type": "paintlayer"},
    {"name": "A", "opacity": 1.5},
    {"name": "A", "opacity": -0.1},
    {"name": "A", "visible": 1},             # strict bools
    {"name": "A", "visible": "true"},
    {"name": "A", "x": 1.5},
    {"name": "A", "x": "3"},
])
def test_rejects(data):
    with pytest.raises(ValidationError):
        LayerSpec.model_validate(data)


def test_schema_forbids_extra():
    schema = LayerSpec.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name"]
