"""Batch executor (D11) and the per-document agent journal (D6).

Pure: no krita / PyQt5 imports. A mutating batch tool does::

    plans = batch.validate_all(params["specs"], validate_one, field="specs")
    out = batch.run(plans, apply_one, ctx, dry_run=dry_run, describe_fn=describe)
    if not out.dry_run:
        batch.JOURNALS.get(handle).push("create_layers", out.inverses, out.nbytes)

1. ``validate_all`` checks the list (non-empty, at most ``MAX_ITEMS``) and
   validates every item before anything changes. The first bad item fails
   the call with ``details.index``.
2. ``run`` applies the plans in order. ``apply_fn(ctx, plan)`` returns
   ``(result, inverse)``. If an item fails, the inverses recorded so far run
   in reverse order and the call fails with ``details.applied`` /
   ``details.rolled_back``. ``apply_fn`` must leave nothing behind for the
   item that raised: only the items before it are rolled back.
3. On success the inverses go onto the document's ``Journal`` (one entry
   per call). ``krita_undo`` (P5.1) pops an entry and passes its inverses
   to ``rollback``.

INVERSES TAKE THE CONTEXT AT CALL TIME. An inverse is a callable
``inverse(ctx)``; ``ctx`` is whatever the tool passes, normally holding the
live ``Document`` just looked up in the registry. An inverse must never
close over a ``Document`` wrapper (or anything that reaches one): journal
entries outlive the call, the document can be closed in between, and
calling a method on a closed document's wrapper segfaults Krita (D3). Close
over plain data only: UUIDs, names, positions, property values, pixel
bytes. Holding a detached ``Node`` for a delete's re-insert is fine (D6,
Q18); the inverse still looks up the parent through ``ctx``.

An inverse may carry an ``nbytes`` attribute (an int: the size of the pixel
snapshot it holds). ``Outcome.nbytes`` sums them for the journal's byte cap.
"""

import collections
import dataclasses

from . import config
from .errors import CommandError

MAX_ITEMS = 2000  # D8: specs per call


def check_items(items, field="specs"):
    """Raise unless ``items`` is a non-empty list of at most ``MAX_ITEMS``."""
    if not isinstance(items, list):
        raise CommandError("bad_request", f"{field} must be a list")
    if not items:
        raise CommandError("bad_request", f"{field} must not be empty")
    if len(items) > MAX_ITEMS:
        raise CommandError("too_large", f"{field} has {len(items)} items; the limit is {MAX_ITEMS}",
                           details={"count": len(items), "limit": MAX_ITEMS})


def validate_all(items, validate_fn, field="specs", positions=None):
    """Return ``[validate_fn(i, item) for each item]``, the plans.

    Runs ``check_items`` first. A ``CommandError`` from item ``i`` is
    re-raised with its code, the message prefixed ``"{field}[{i}]: "`` and
    ``index`` / ``field`` merged into its details. Any other exception is a
    bug and propagates unchanged.

    ``positions`` (a list as long as ``items``) reports item ``i`` as
    ``positions[i]`` in errors instead, for items that came from another
    list (``create_layers(manifest=…)``: the manifest's ``layers``, minus
    skipped entries). ``validate_fn`` still gets ``i``.
    """
    check_items(items, field)
    plans = []
    for i, item in enumerate(items):
        try:
            plans.append(validate_fn(i, item))
        except CommandError as e:
            raise _at_index(e, field, _position(positions, i)) from e
    return plans


def _position(positions, i):
    return i if positions is None else positions[i]


def _at_index(e, field, i):
    if isinstance(e.details, dict):
        details = {**e.details, "index": i, "field": field}
    elif e.details is None:
        details = {"index": i, "field": field}
    else:
        details = {"index": i, "field": field, "cause": e.details}
    return CommandError(e.code, f"{field}[{i}]: {e.message}", details=details)


@dataclasses.dataclass
class Outcome:
    """What ``run`` did, or would do with ``dry_run``.

    ``results`` are in item order; ``inverses`` in apply order (no-op
    ``None`` inverses are dropped), empty for a dry run.
    """

    results: list
    inverses: list
    nbytes: int = 0
    dry_run: bool = False


def run(plans, apply_fn, ctx, *, dry_run=False, describe_fn=None, label="batch", field="specs",
        positions=None):
    """Apply ``plans`` in order; roll back and raise if one fails.

    ``apply_fn(ctx, plan) -> (result, inverse)``, where ``inverse`` is a
    callable ``inverse(ctx)`` or ``None``. With ``dry_run`` nothing is
    applied and ``results`` is ``[describe_fn(plan)]`` (or the plans).

    If item ``i`` raises (or returns something that isn't a pair), the
    recorded inverses run in reverse order and a ``CommandError`` is raised:
    the item's own code if it raised a ``CommandError`` (a conflict found
    mid-apply stays a conflict), else ``krita_error``. Its details hold
    ``index``, ``field``, ``applied`` (items done before ``i``),
    ``rolled_back`` (inverses that ran cleanly) and ``rollback_errors``.
    ``positions`` maps ``index`` as in ``validate_all``; ``applied`` stays
    a count of plans.
    """
    if dry_run:
        results = [describe_fn(p) for p in plans] if describe_fn else list(plans)
        return Outcome(results=results, inverses=[], dry_run=True)

    results, inverses = [], []
    for i, plan in enumerate(plans):
        try:
            result, inverse = apply_fn(ctx, plan)
            if inverse is not None and not callable(inverse):
                raise TypeError(f"inverse for item {i} is not callable: {inverse!r}")
        except Exception as e:
            raise _rollback_error(e, i, inverses, ctx, label, field,
                                  _position(positions, i)) from e
        results.append(result)
        if inverse is not None:
            inverses.append(inverse)
    return Outcome(results=results, inverses=inverses, nbytes=sum(nbytes_of(f) for f in inverses))


def _rollback_error(e, i, inverses, ctx, label, field, where):
    rolled_back, errors = rollback(inverses, ctx)
    if isinstance(e, CommandError):
        code, why = e.code, e.message
    else:
        code, why = "krita_error", f"{type(e).__name__}: {e}"
    msg = f"{label}: {field}[{where}] failed: {why}; rolled back {rolled_back} of {len(inverses)} changes"
    if errors:
        msg += f"; {len(errors)} rollback step(s) failed, document may be partly changed"
    details = {"index": where, "field": field, "applied": i, "rolled_back": rolled_back,
               "rollback_errors": errors}
    if isinstance(e, CommandError) and e.details is not None:
        details["cause"] = e.details
    return CommandError(code, msg, details=details)


def rollback(inverses, ctx):
    """Run ``inverses`` newest first, each with ``ctx``; keep going past
    failures. Returns ``(count_ok, errors)``, where each error is
    ``{"step": position in inverses, "error": "Type: message"}``.
    """
    ok, errors = 0, []
    for step in range(len(inverses) - 1, -1, -1):
        try:
            inverses[step](ctx)
            ok += 1
        except Exception as e:
            errors.append({"step": step, "error": f"{type(e).__name__}: {e}"})
    return ok, errors


def nbytes_of(inverse):
    n = getattr(inverse, "nbytes", 0)
    return n if type(n) is int and n > 0 else 0


# --- Journal (D6) --------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class JournalEntry:
    """One tool call: its inverses in apply order (``rollback`` reverses)."""

    label: str
    inverses: tuple
    nbytes: int


class Journal:
    """The agent undo journal of one document: newest entry last.

    Keeps at most ``max_actions`` entries and ``max_bytes`` of snapshots;
    the oldest go first. Not thread-safe: use it from the main thread, as
    the commands do.

    P5.1 refuses an entry whose nodes no longer exist (that needs Krita, so
    it lives in the undo command, not here). Redo is P5.1's too: give it a
    separate stack of the same entries, filled by undo and cleared by
    ``push``; this class needs no change for that beyond the caller
    clearing it.
    """

    def __init__(self, max_actions, max_bytes):
        for name, v in (("max_actions", max_actions), ("max_bytes", max_bytes)):
            if type(v) is not int or v <= 0:
                raise ValueError(f"{name} must be a positive int, got {v!r}")
        self.max_actions = max_actions
        self.max_bytes = max_bytes
        self._entries = collections.deque()
        self._bytes = 0

    def push(self, label, inverses, nbytes=0):
        """Record one call. Returns ``False`` if the call can't be undone.

        An empty ``inverses`` records nothing and returns ``True`` (there is
        nothing to undo). An entry over ``max_bytes`` on its own is not
        stored: the journal is cleared, because undoing older entries past
        a change it doesn't know about would not restore their state, and
        ``False`` is returned so the caller can warn.
        """
        inverses = tuple(inverses)
        if not inverses:
            return True
        if nbytes > self.max_bytes:
            self.clear()
            return False
        self._entries.append(JournalEntry(label, inverses, nbytes))
        self._bytes += nbytes
        while len(self._entries) > self.max_actions or self._bytes > self.max_bytes:
            self._bytes -= self._entries.popleft().nbytes
        return True

    def pop(self):
        """Remove and return the newest entry, or ``None``."""
        if not self._entries:
            return None
        entry = self._entries.pop()
        self._bytes -= entry.nbytes
        return entry

    def peek(self):
        return self._entries[-1] if self._entries else None

    def clear(self):
        self._entries.clear()
        self._bytes = 0

    @property
    def total_bytes(self):
        return self._bytes

    def __len__(self):
        return len(self._entries)


class Journals:
    """Document handle -> ``Journal``, created on first use with the limits
    in ``config.current().journal`` at that time.

    Integration point: when the registry prunes a handle (its document
    closed), it must call ``forget(handle)`` so the entries, and any
    detached nodes or snapshots they hold, are dropped.
    """

    def __init__(self):
        self._by_handle = {}

    def get(self, handle):
        j = self._by_handle.get(handle)
        if j is None:
            limits = config.current().journal
            j = self._by_handle[handle] = Journal(limits["max_actions"], limits["max_bytes"])
        return j

    def forget(self, handle):
        self._by_handle.pop(handle, None)

    def __contains__(self, handle):
        return handle in self._by_handle

    def __len__(self):
        return len(self._by_handle)


JOURNALS = Journals()  # the plugin's; tests make their own
