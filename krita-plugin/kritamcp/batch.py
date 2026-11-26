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
   per call). ``krita_undo`` / ``krita_redo`` (P5.1) apply the newest
   entry of one stack with ``apply_entry`` and put its reverse on the other.

INVERSES TAKE THE CONTEXT AT CALL TIME. An inverse is a callable
``inverse(ctx)``; ``ctx`` is whatever the tool passes, normally holding the
live ``Document`` just looked up in the registry. An inverse must never
close over a ``Document`` wrapper (or anything that reaches one): journal
entries outlive the call, the document can be closed in between, and
calling a method on a closed document's wrapper segfaults Krita (D3). Close
over plain data only: UUIDs, names, positions, property values, pixel
bytes. Holding a detached ``Node`` for a delete's re-insert is fine (D6,
Q18); the inverse still looks up the parent through ``ctx``.

The inverse protocol (P5.1, D6):

- ``inverse(ctx)`` applies it and returns the op that reverses it again
  (the redo of an undo, the undo of a redo), an inverse of the same kind,
  or None when there is nothing to reverse. ``rollback`` ignores it.
- ``check(ctx)`` (optional) returns None if the inverse can be applied
  now, else the reason (a node it must touch is gone, a re-insert's node
  is already back, ...). ``apply_entry`` runs it just before applying.
- ``node_ids``: the layer ids it touches.
- ``nbytes`` (optional int): what it holds, pixel snapshots and detached
  layers. ``Outcome.nbytes`` sums them for the journal's caps.
- ``blobs`` (optional): the ``Blob``s holding its pixel snapshots, which
  the journal may spill to disk. ``nbytes`` minus their size can't be
  spilled.
"""

import atexit
import dataclasses
import os
import shutil
import stat
import tempfile
import zlib

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




# --- Spillable pixel data (D6) ----------------------------------------------

DEFAULT_MAX_DISK_BYTES = 4 * 1024 ** 3  # journal.max_disk_bytes when the config has none
SPILL_PREFIX = "kmcp-journal-"

_spill_root = None  # the process's spill dir, made on first use


def process_spill_dir():
    """This process's spill dir, ``<tmp>/kmcp-journal-<pid>-<random>``, made
    on first use and removed with everything in it at exit (``atexit``).
    Embedded interpreters may skip ``atexit`` (kritarunner does), so making
    one also removes the dirs of processes that are gone (``sweep_stale``)."""
    global _spill_root
    if _spill_root is None or not os.path.isdir(_spill_root):
        sweep_stale(tempfile.gettempdir())
        _spill_root = tempfile.mkdtemp(prefix=f"{SPILL_PREFIX}{os.getpid()}-")
        atexit.register(shutil.rmtree, _spill_root, True)
    return _spill_root


def sweep_stale(parent):
    """Remove the spill dirs in ``parent`` whose process (the pid in the
    name) no longer runs. Only real directories owned by this user; any
    other entry, or one whose name has no pid, is left alone."""
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        if not name.startswith(SPILL_PREFIX):
            continue
        pid = name[len(SPILL_PREFIX):].split("-", 1)[0]
        if not pid.isdigit() or int(pid) == os.getpid() or _alive(int(pid)):
            continue
        path = os.path.join(parent, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            shutil.rmtree(path, ignore_errors=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, as someone else's
        return True
    return True


class Blob:
    """Bytes kept in memory, or spilled: zlib-compressed (level 1) into a
    file of their own. ``nbytes`` is the uncompressed size either way.

    An inverse that holds pixel data keeps it in Blobs and lists them in a
    ``blobs`` attribute; the journal spills them when its memory cap is
    reached and discards their files when the entry goes. ``load`` reads a
    spilled blob back without deleting the file (an entry that fails to
    apply stays on the stack).
    """

    __slots__ = ("nbytes", "_data", "_path")

    def __init__(self, data):
        self._data = bytes(data)
        self.nbytes = len(self._data)
        self._path = None

    @property
    def in_memory(self):
        return self._data is not None

    @property
    def path(self):
        """The spill file, or None while in memory."""
        return self._path

    def spill(self, directory):
        """Write the bytes to a new file in ``directory`` and drop them from
        memory. Raises ``OSError`` (and stays in memory) if that fails."""
        if self._data is None:
            return
        fd, path = tempfile.mkstemp(suffix=".z", dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(zlib.compress(self._data, 1))
        except BaseException:
            _unlink(path)
            raise
        self._path, self._data = path, None

    def load(self):
        """The bytes, from memory or the spill file."""
        if self._data is not None:
            return self._data
        if self._path is None:
            raise OSError("pixel snapshot was discarded")
        with open(self._path, "rb") as f:
            data = zlib.decompress(f.read())
        if len(data) != self.nbytes:
            raise OSError(f"spill file {self._path} holds {len(data)} bytes, not {self.nbytes}")
        return data

    def discard(self):
        """Delete the spill file, if any. The blob is unusable afterwards
        if it was spilled."""
        if self._path is not None:
            _unlink(self._path)
            self._path = None

    def __repr__(self):
        return f"Blob({self.nbytes} bytes, {'memory' if self.in_memory else self._path})"


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[KritaMCP] journal: could not delete {path}: {e}")


def blobs_of(inverse):
    """The inverse's spillable ``Blob``s (its ``blobs`` attribute), or ()."""
    found = getattr(inverse, "blobs", ())
    return tuple(b for b in found if isinstance(b, Blob))


# --- Applying an entry: undo / redo (D6) -----------------------------------

def check_of(inverse, ctx):
    """``inverse.check(ctx)``: None if it can be applied, else the reason."""
    check = getattr(inverse, "check", None)
    return None if check is None else check(ctx)


def apply_entry(entry, ctx, verb="undo"):
    """Apply ``entry``'s inverses newest first and return the ops that
    reverse them (the redo ops of an undo, or the undo ops of a redo), in
    the order produced. Each inverse's ``check(ctx)`` runs just before it
    is applied, against the state the inverses before it left.

    A failed check is a ``conflict``; an inverse that raises keeps its
    ``CommandError`` code, else ``krita_error``. Either way the inverses
    already applied are reversed again (the returned ops, newest first),
    so the document is as before the call, and the error's details say so:
    ``label``, ``step`` (the inverse's position), ``applied``,
    ``rolled_forward`` and ``rollback_errors``. When putting them back
    fails too, ``rollback_errors`` is non-empty: the document may be partly
    changed.
    """
    produced = []
    inverses = entry.inverses
    for step in range(len(inverses) - 1, -1, -1):
        inverse = inverses[step]
        try:
            reason = check_of(inverse, ctx)
            if reason is not None:
                raise CommandError("conflict", str(reason))
            back = inverse(ctx)
        except Exception as e:
            raise _apply_error(e, entry, verb, step, produced, ctx) from e
        produced.append(back)
    return [p for p in produced if p is not None]


def _apply_error(e, entry, verb, step, produced, ctx):
    done = [p for p in produced if p is not None]
    ok, errors = rollback(done, ctx)
    if isinstance(e, CommandError):
        code, why = e.code, e.message
    else:
        code, why = "krita_error", f"{type(e).__name__}: {e}"
    msg = f"can't {verb} {entry.label!r}: {why}"
    if not produced:
        msg += "; nothing was changed"
    elif not errors:
        msg += f"; the {len(produced)} step(s) already applied were put back"
    else:
        msg += (f"; {len(errors)} of the {len(done)} step(s) already applied could not be put "
                "back, the document may be partly changed")
    details = {"label": entry.label, "step": step, "applied": len(produced),
               "rolled_forward": ok, "rollback_errors": errors}
    ids = getattr(entry.inverses[step], "node_ids", None)
    if ids:
        details["layers"] = list(ids)
    if isinstance(e, CommandError) and e.details is not None:
        details["cause"] = e.details
    return CommandError(code, msg, details=details)


# --- Journal (D6) --------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class JournalEntry:
    """One tool call: its inverses in apply order (``rollback`` and
    ``apply_entry`` run them newest first). ``nbytes``: what it holds
    (pixel snapshots and detached layers), in memory or spilled."""

    label: str
    inverses: tuple
    nbytes: int

    def blobs(self):
        return [b for inv in self.inverses for b in blobs_of(inv)]

    @property
    def fixed_bytes(self):
        """The part that can't be spilled (detached layers)."""
        return max(0, self.nbytes - sum(b.nbytes for b in self.blobs()))

    @property
    def memory_bytes(self):
        return self.fixed_bytes + sum(b.nbytes for b in self.blobs() if b.in_memory)

    @property
    def disk_bytes(self):
        """Spilled bytes, uncompressed."""
        return sum(b.nbytes for b in self.blobs() if not b.in_memory)

    def node_ids(self):
        """Every layer id the inverses touch, in apply order, no repeats."""
        seen = {}
        for inv in self.inverses:
            for nid in getattr(inv, "node_ids", ()) or ():
                seen.setdefault(nid, None)
        return list(seen)

    def discard(self):
        """Delete its spill files. Touches nothing else: the detached nodes
        it may hold are just dropped (they may belong to a closed document)."""
        for b in self.blobs():
            b.discard()


class Journal:
    """The agent undo journal of one document: an undo stack and a redo
    stack, newest entry last in each.

    ``push`` records a tool call and clears the redo stack; ``undo`` /
    ``redo`` apply the newest entry of one stack (``apply_entry``) and put
    the ops that reverse it on the other.

    Caps: each stack keeps at most ``max_actions`` entries. ``max_bytes``
    (memory) and ``max_disk_bytes`` (spill files, counted uncompressed)
    cover both stacks together. Over the memory cap, pixel snapshots
    (``Blob``s) are spilled to files, the entries farthest from the
    present first (the bottoms of the two stacks); detached layers can't
    be spilled. Still over a cap, the farthest entries are dropped. An
    entry that can't fit even alone (detached layers over ``max_bytes``,
    or pixels over both caps) is not stored, and the stack it was meant
    for is cleared, since stepping past it would not restore the older
    entries' state: ``push`` (and ``undo`` / ``redo``, for the reverse
    entry) report that with ``False``.

    Not thread-safe: use it from the main thread, as the commands do.
    """

    def __init__(self, max_actions, max_bytes, max_disk_bytes=DEFAULT_MAX_DISK_BYTES,
                 spill_dir=None):
        for name, v in (("max_actions", max_actions), ("max_bytes", max_bytes),
                        ("max_disk_bytes", max_disk_bytes)):
            if type(v) is not int or v <= 0:
                raise ValueError(f"{name} must be a positive int, got {v!r}")
        self.max_actions = max_actions
        self.max_bytes = max_bytes
        self.max_disk_bytes = max_disk_bytes
        self._spill_dir = spill_dir  # None: process_spill_dir()
        self._undo = []
        self._redo = []

    # --- recording ---

    def push(self, label, inverses, nbytes=0):
        """Record one call. Returns ``False`` if the call can't be undone.

        An empty ``inverses`` records nothing and returns ``True`` (there is
        nothing to undo). Otherwise the redo stack is cleared. An entry that
        can't fit alone is not stored: the journal is cleared and ``False``
        is returned so the caller can warn (``not_undoable``).
        """
        inverses = tuple(inverses)
        if not inverses:
            return True
        self._drop_all(self._redo)
        return self._store(self._undo, JournalEntry(label, inverses, nbytes))

    def pop(self):
        """Remove and return the newest undo entry, or ``None``, without
        applying it. The caller owns it (``entry.discard()`` deletes its
        spill files)."""
        return self._undo.pop() if self._undo else None

    def peek(self):
        return self._undo[-1] if self._undo else None

    # --- undo / redo ---

    def undo(self, ctx, where=""):
        """Apply the newest undo entry (``apply_entry``) and move its reverse
        onto the redo stack. Returns ``(entry, reverse, kept)``; ``kept`` is
        False when the reverse could not be stored (the redo stack was
        cleared). Empty: ``conflict``. A failed apply leaves both stacks as
        they were, unless the document could not be put back, in which case
        the journal is cleared (its entries no longer match the document)."""
        return self._step(self._undo, self._redo, ctx, "undo", where)

    def redo(self, ctx, where=""):
        """``undo``'s mirror: the newest redo entry, reverse onto undo."""
        return self._step(self._redo, self._undo, ctx, "redo", where)

    def _step(self, source, target, ctx, verb, where):
        if not source:
            raise CommandError("conflict", f"nothing to {verb}{where}",
                               {"undo": len(self._undo), "redo": len(self._redo)})
        entry = source[-1]
        try:
            produced = apply_entry(entry, ctx, verb)
        except CommandError as e:
            if isinstance(e.details, dict) and e.details.get("rollback_errors"):
                self.clear()
                e.message += "; the agent undo journal was cleared"
                e.details["journal_cleared"] = True
            raise
        source.pop()
        entry.discard()
        reverse = JournalEntry(entry.label, tuple(produced), sum(nbytes_of(p) for p in produced))
        kept = self._store(target, reverse) if reverse.inverses else True
        return entry, reverse, kept

    # --- caps ---

    def _fits(self, entry):
        fixed = entry.fixed_bytes
        spillable = entry.nbytes - fixed
        if fixed > self.max_bytes:
            return False
        return fixed + spillable <= self.max_bytes or spillable <= self.max_disk_bytes

    def _store(self, stack, entry):
        if not self._fits(entry):
            entry.discard()
            self._drop_all(stack)
            return False
        stack.append(entry)
        if self._enforce(entry):
            return True
        stack.remove(entry)  # only when spilling failed (disk error)
        entry.discard()
        self._drop_all(stack)
        return False

    def _farthest_first(self):
        """``(entry, stack)`` of both stacks, farthest from the present
        first (redo before undo at the same distance)."""
        u, r = self._undo, self._redo
        ranked = [(len(r) - j, 0, j, e, r) for j, e in enumerate(r)]
        ranked += [(len(u) - i, 1, i, e, u) for i, e in enumerate(u)]
        ranked.sort(key=lambda t: (-t[0], t[1], t[2]))
        return [(e, s) for _, _, _, e, s in ranked]

    def _enforce(self, keep):
        """Apply the caps after ``keep`` was added; never drops ``keep``.
        Returns False if ``keep`` alone is still over the memory cap."""
        for stack in (self._undo, self._redo):
            while len(stack) > self.max_actions:
                self._drop(stack, stack[0])
        memory = self.memory_bytes
        if memory > self.max_bytes:
            for entry, _ in self._farthest_first():
                for blob in entry.blobs():
                    if memory <= self.max_bytes:
                        break
                    if blob.in_memory:
                        try:
                            blob.spill(self.spill_dir())
                        except OSError as e:
                            print(f"[KritaMCP] journal: could not spill a pixel snapshot: {e}")
                            continue
                        memory -= blob.nbytes
        while self.memory_bytes > self.max_bytes or self.disk_bytes > self.max_disk_bytes:
            victim = next(((e, s) for e, s in self._farthest_first() if e is not keep), None)
            if victim is None:
                break
            self._drop(victim[1], victim[0])
        return self.memory_bytes <= self.max_bytes

    def spill_dir(self):
        return self._spill_dir or process_spill_dir()

    def _drop(self, stack, entry):
        stack.remove(entry)
        entry.discard()

    def _drop_all(self, stack):
        for entry in stack:
            entry.discard()
        stack.clear()

    def clear(self):
        """Drop both stacks and delete their spill files."""
        self._drop_all(self._undo)
        self._drop_all(self._redo)

    # --- inspection ---

    @property
    def total_bytes(self):
        """What both stacks hold, in memory or spilled."""
        return sum(e.nbytes for e in self._undo + self._redo)

    @property
    def memory_bytes(self):
        return sum(e.memory_bytes for e in self._undo + self._redo)

    @property
    def disk_bytes(self):
        return sum(e.disk_bytes for e in self._undo + self._redo)

    def spill_files(self):
        return [b.path for e in self._undo + self._redo for b in e.blobs() if b.path]

    @property
    def redo_count(self):
        return len(self._redo)

    def entries(self, which="undo"):
        """The entries of one stack, the next one to apply first."""
        return list(reversed(self._undo if which == "undo" else self._redo))

    def describe(self):
        """Both stacks for ``krita_journal``: per entry its label, layer
        count and sizes, the next one to apply first; totals and caps."""
        def row(e):
            return {"label": e.label, "layer_count": len(e.node_ids()), "nbytes": e.nbytes,
                    "disk_bytes": e.disk_bytes, "on_disk": e.disk_bytes > 0}
        return {"undo": [row(e) for e in self.entries("undo")],
                "redo": [row(e) for e in self.entries("redo")],
                "memory_bytes": self.memory_bytes, "disk_bytes": self.disk_bytes,
                "limits": {"max_actions": self.max_actions, "max_bytes": self.max_bytes,
                           "max_disk_bytes": self.max_disk_bytes}}

    def __len__(self):
        """The number of undo entries."""
        return len(self._undo)


def _limit(limits, key, default):
    try:
        value = limits.get(key, default)
    except AttributeError:
        return default
    return value if type(value) is int and value > 0 else default


class Journals:
    """Document handle -> ``Journal``, created on first use with the limits
    in ``config.current().journal`` at that time (``max_disk_bytes``
    defaults to ``DEFAULT_MAX_DISK_BYTES`` when the config has none).

    Integration point: when the registry prunes a handle (its document
    closed), it must call ``forget(handle)`` so the entries, and any
    detached nodes or snapshots they hold, are dropped and their spill
    files deleted. Nothing is called on the entries' nodes.
    """

    def __init__(self, spill_dir=None):
        self._by_handle = {}
        self._spill_dir = spill_dir

    def get(self, handle):
        j = self._by_handle.get(handle)
        if j is None:
            limits = config.current().journal
            j = self._by_handle[handle] = Journal(
                _limit(limits, "max_actions", config.DEFAULT_JOURNAL["max_actions"]),
                _limit(limits, "max_bytes", config.DEFAULT_JOURNAL["max_bytes"]),
                _limit(limits, "max_disk_bytes", DEFAULT_MAX_DISK_BYTES),
                spill_dir=self._spill_dir)
        return j

    def forget(self, handle):
        j = self._by_handle.pop(handle, None)
        if j is not None:
            j.clear()

    def __contains__(self, handle):
        return handle in self._by_handle

    def __len__(self):
        return len(self._by_handle)


JOURNALS = Journals()  # the plugin's; tests make their own
