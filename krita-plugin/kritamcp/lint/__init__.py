"""Lint rules over a document snapshot (D14; split into packs in F004).
Pure: stdlib only, no krita / PyQt5.

- ``engine``: registry, packs, sets, settings, ``run`` (the snapshot
  format is in its docstring).
- ``core``: rules for any Krita document (category 2: on by default,
  opt-out with the config's ``lint.disable``).
- ``live2d``: the Live2D rig conventions (category 3: opt-in with the
  config's ``live2d.enabled``).

F004 survey.md has the categories. Importing this package registers both
packs' rules, core first.
"""

from . import core, live2d  # noqa: F401  (registers the rules)
from .engine import (CORE, GROUP, MASK_TYPES, PACKS, PAINT, RULES, SETS, SEVERITIES, Pack,
                     Rule, check_args, checked_nodes, default_settings, is_reference_group,
                     pack_on, rule_enabled, run, select, set_members, settings, severity_of)

DEFAULT_SETTINGS = default_settings()
