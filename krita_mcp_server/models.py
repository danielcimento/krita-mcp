"""Pydantic models for list items in tool arguments (plan § Target layout).

Every model forbids unknown keys, so a typo such as ``"opactiy"`` fails
validation instead of being silently dropped. Optional fields default to
None, meaning "not set": ``to_params`` leaves them out, so the plugin applies
its own defaults (api-design.md) and only what the caller set is sent.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class Point(BaseModel):
    """A canvas point ``{x, y}`` in whole pixels."""

    model_config = ConfigDict(extra="forbid")

    x: StrictInt
    y: StrictInt


class LayerSpec(BaseModel):
    """One layer for ``krita_create_layers`` (api-design § krita_create_layers)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Layer name (required)")
    key: Optional[str] = Field(
        None, description="Batch-local name: later specs in the same call refer to this "
                          "layer as parent/above with \"$<key>\". No '$' or '/'")
    type: Optional[Literal["paint", "group"]] = Field(None, description='"paint" (default) or "group"')
    parent: Optional[str] = Field(
        None, description='A group: layer ref (UUID, name path, unique name), "$key", or "/" '
                          "(the root, default)")
    above: Optional[str] = Field(
        None, description="Insert directly above this sibling (ref or \"$key\"); default on top "
                          "of the parent's children")
    visible: Optional[StrictBool] = Field(None, description="Default true")
    opacity: Optional[float] = Field(None, ge=0, le=1, description="0.0-1.0, default 1.0")
    blend_mode: Optional[str] = Field(
        None, description='Krita blend-mode id, default "normal" (e.g. "multiply", "add", '
                          '"linear_dodge", "screen")')
    locked: Optional[StrictBool] = Field(None, description="Default false")
    alpha_locked: Optional[StrictBool] = Field(None, description="Default false")
    inherit_alpha: Optional[StrictBool] = Field(None, description="Clipping; default false")
    pass_through: Optional[StrictBool] = Field(None, description="Groups only; default false")
    png: Optional[str] = Field(
        None, description="Paint layers only: PNG file (inside an allowed directory) for the "
                          "layer's pixels")
    x: Optional[StrictInt] = Field(None, description="Canvas x of the PNG's top-left, default 0; "
                                                     "only with png")
    y: Optional[StrictInt] = Field(None, description="Canvas y of the PNG's top-left, default 0; "
                                                     "only with png")
    device_origin: Optional[Point] = Field(
        None, description="Paint layers only: the paint device's origin {x, y} "
                          "(krita_export_layers records it), set before the pixels are "
                          "written so the rebuild composites exactly like the source; "
                          "default (0, 0)")

    def to_params(self) -> dict:
        """The fields the caller set to a value, for the plugin. ``False``
        and ``0`` are values and are sent; ``None`` means unset."""
        return self.model_dump(exclude_none=True)


class LayerChange(BaseModel):
    """One change for ``krita_update_layers`` (api-design § krita_update_layers).
    Only the fields set are changed."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="The layer to change: UUID, name path or unique name")
    name: Optional[str] = Field(None, description="New name")
    visible: Optional[StrictBool] = None
    opacity: Optional[float] = Field(None, ge=0, le=1, description="0.0-1.0")
    blend_mode: Optional[str] = Field(
        None, description='Krita blend-mode id (e.g. "normal", "multiply", "add", "screen")')
    locked: Optional[StrictBool] = None
    alpha_locked: Optional[StrictBool] = None
    inherit_alpha: Optional[StrictBool] = Field(None, description="Clipping")
    pass_through: Optional[StrictBool] = Field(None, description="Groups only")

    def to_params(self) -> dict:
        """The fields the caller set (``False`` and ``0`` are sent)."""
        return self.model_dump(exclude_none=True)


class Move(BaseModel):
    """One move for ``krita_move_layers`` (api-design § krita_move_layers).
    At most one of ``above``, ``below`` and ``index``; none puts the layer
    on top of its (new) parent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="The layer to move: UUID, name path or unique name")
    parent: Optional[str] = Field(
        None, description='Target group (ref) or "/" for the root; default the current parent')
    above: Optional[str] = Field(None, description="Go directly above this sibling (ref)")
    below: Optional[str] = Field(None, description="Go directly below this sibling (ref)")
    index: Optional[StrictInt] = Field(
        None, ge=0, description="Position from the bottom (0) among the target parent's other "
                                "children")

    @model_validator(mode="after")
    def _one_position(self):
        given = [k for k in ("above", "below", "index") if getattr(self, k) is not None]
        if len(given) > 1:
            raise ValueError(f"give at most one of above, below and index, not {' and '.join(given)}")
        return self

    def to_params(self) -> dict:
        """The fields the caller set (``index=0`` is sent)."""
        return self.model_dump(exclude_none=True)


class Rect(BaseModel):
    """A rectangle in canvas pixels, origin top-left (``krita_render``'s
    ``region``)."""

    model_config = ConfigDict(extra="forbid")

    x: StrictInt = Field(description="Left edge in canvas pixels; may be negative")
    y: StrictInt = Field(description="Top edge in canvas pixels; may be negative")
    w: StrictInt = Field(ge=1, description="Width in pixels, at least 1")
    h: StrictInt = Field(ge=1, description="Height in pixels, at least 1")

    def to_params(self) -> dict:
        return self.model_dump()
