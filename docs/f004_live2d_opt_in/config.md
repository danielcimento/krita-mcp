# F004: the config for CONVENTIONS.md

Since plugin 0.4.0 the Live2D conventions are off by default
([survey.md](survey.md), category 3). To keep everything in
`live2d-workspace/CONVENTIONS.md` applying, turn them on in
`~/.krita-mcp-config.json`.

## The minimal change

Your current file:

```json
{
  "allowed_dirs": ["~/krita-mcp-output", "~/smoke-out"]
}
```

becomes:

```json
{
  "allowed_dirs": ["~/krita-mcp-output", "~/smoke-out"],
  "live2d": {"enabled": true}
}
```

The defaults of every other knob are CONVENTIONS.md as written, so this
gives the same behaviour as plugin 0.3.0, plus K8's
`reference-unbracketed`:

- **The `default` set:** the core rules plus `node-type`, `layer-style`,
  `inherit-alpha`, `blend-mode`, `side-token` and `color-space`.
- **`duplicate-name`:** an error, with pose-suffix `suggested_names`.
- **The `conventions` set:** opt-in per call, as before. Pass
  `rules: ["all"]` or `["default", "conventions"]`, as the smoke test
  does.
- **`set_canvas_size`:** warns `rig_breaking`.

The plugin re-reads the file on the next call, with no restart needed.
`krita_health` shows `live2d.enabled: true`. It also shows any problem in
`config_errors`.

## The same, with every knob spelled out

For reference, or as a starting point for changing one. The values are
the defaults:

```json
{
  "allowed_dirs": ["~/krita-mcp-output", "~/smoke-out"],
  "lint": {
    "stray_alpha_below": 8,
    "stray_alpha_distance": 4,
    "stray_speck_max_px": 64,
    "reference_pattern": "^\\[.*\\]$",
    "disable": []
  },
  "live2d": {
    "enabled": true,
    "additive_mode": "either",
    "allowed_blend_modes": ["normal", "multiply", "add", "linear_dodge"],
    "max_nesting": 3,
    "pose_pattern": "^(?P<stem>.*\\S) (?P<pose>[A-KM-QS-Z])$",
    "default_pose": "A"
  }
}
```

| Knob | CONVENTIONS.md source |
|---|---|
| `lint.reference_pattern` | § Naming, Reference material: a group name wrapped in brackets. The pattern is matched against the trimmed name |
| `live2d.additive_mode` | D17: `add` and `linear_dodge` are one mode. `"add"` or `"linear_dodge"` requires one spelling |
| `live2d.allowed_blend_modes` | § Layer types: Normal, Multiply, Add (the legacy Cubism modes). For a rig that deliberately targets moc3 5.3, add the extended modes |
| `live2d.max_nesting` | § Structure: at most three levels |
| `live2d.pose_pattern`, `default_pose` | § Structure, Alternate poses: sibling groups suffixed ` A`, ` B`, …, pose A visible. The default pattern excludes `L` and `R`, the side tokens. It is the same definition as umamo U11 |

## If you had `lint.additive_mode`

F003 put `additive_mode` under `lint`. It is still read, with a note in
`config_errors`, but its place is now `live2d.additive_mode`. Your
current file doesn't set it.
