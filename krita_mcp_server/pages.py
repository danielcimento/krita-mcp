"""Keeping list results under the 8 KB budget (F005 K12). Pure: stdlib only,
so the headless suite can apply the same guard to the plugin's results."""

import json

# F005 K12: a page whose JSON would pass this is cut short (next_offset
# says where to go on), so the result plus its summary stays under 8 KB.
LIST_MAX_BYTES = 7 * 1024 + 512
# Dropped from the rows (and named in "trimmed") only when a single row
# would not fit otherwise; child_count stays.
LIST_DROPPABLE = ("children",)


def json_size(value):
    return len(json.dumps(value))


def fit_page(result, max_bytes=LIST_MAX_BYTES):
    """Cut ``result["layers"]`` (a list_layers page) so the result's JSON
    stays within ``max_bytes`` (F005 K12): keep the first rows that fit and
    move ``next_offset`` to the first row left out. Only when not even one
    row fits are ``LIST_DROPPABLE`` fields dropped from the rows. Returns
    ``{rows, of, max_bytes, dropped_fields}`` (also set as
    ``result["trimmed"]``) when anything changed, else None."""
    layers = result.get("layers")
    if not isinstance(layers, list) or not layers or "offset" not in result:
        return None
    if json_size(result) <= max_bytes:
        return None
    base = json_size({**result, "layers": [], "next_offset": 10**9,
                       "trimmed": {"rows": 10**6, "of": 10**6, "max_bytes": max_bytes,
                                   "dropped_fields": list(LIST_DROPPABLE)}})

    def fitting(rows):
        total, kept = base, 0
        for row in rows:
            total += json_size(row) + 2  # ", " between rows
            if total > max_bytes:
                break
            kept += 1
        return kept

    dropped = []
    kept = fitting(layers)
    if kept == 0:
        dropped = [f for f in LIST_DROPPABLE if any(isinstance(r, dict) and f in r for r in layers)]
        layers = [{k: v for k, v in r.items() if k not in dropped} if isinstance(r, dict) else r
                  for r in layers]
        kept = max(1, fitting(layers))
    trimmed = {"rows": kept, "of": len(layers), "max_bytes": max_bytes, "dropped_fields": dropped}
    if kept < len(layers):
        result["next_offset"] = (result.get("offset") or 0) + kept
    result["layers"] = layers[:kept]
    result["trimmed"] = trimmed
    return trimmed
