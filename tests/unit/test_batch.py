"""batch.py: validate-all, apply in order, rollback, dry_run, journal.

The fake "document" is a dict; the fake applier sets keys and returns an
inverse that restores the old value through the ctx it is called with.
"""

import json
import os
import subprocess
import sys

import pytest

from kritamcp import batch, config
from kritamcp.errors import CommandError

_MISSING = object()


class Ctx:
    def __init__(self, doc):
        self.doc = doc
        self.calls = []  # (event, key) in order


def apply_set(ctx, plan):
    """plan = (key, value); value "boom" raises a KeyError, "clash" a conflict."""
    key, value = plan
    if value == "boom":
        raise KeyError("no such node")
    if value == "clash":
        raise CommandError("conflict", "name taken", details={"name": key})
    old = ctx.doc.get(key, _MISSING)
    ctx.doc[key] = value
    ctx.calls.append(("apply", key))

    def inverse(c):
        c.calls.append(("undo", key))
        if old is _MISSING:
            del c.doc[key]
        else:
            c.doc[key] = old

    return f"set {key}", inverse


def validate_pair(i, item):
    if not (isinstance(item, list) and len(item) == 2):
        raise CommandError("bad_request", "need [key, value]", details={"got": item})
    return tuple(item)


# --- check_items / validate_all ----------------------------------------

@pytest.mark.parametrize("items", [None, {}, "ab", ()])
def test_non_list_is_bad_request(items):
    with pytest.raises(CommandError) as e:
        batch.check_items(items, "specs")
    assert e.value.code == "bad_request" and "specs" in e.value.message


def test_empty_list_is_bad_request():
    with pytest.raises(CommandError) as e:
        batch.validate_all([], validate_pair)
    assert e.value.code == "bad_request"


def test_cap_is_2000():
    assert batch.MAX_ITEMS == 2000
    batch.check_items([0] * 2000)
    with pytest.raises(CommandError) as e:
        batch.check_items([0] * 2001, "changes")
    assert e.value.code == "too_large"
    assert e.value.details == {"count": 2001, "limit": 2000}


def test_validate_all_returns_plans():
    assert batch.validate_all([["a", 1], ["b", 2]], validate_pair) == [("a", 1), ("b", 2)]


def test_validate_all_stops_at_first_bad_item_with_index():
    seen = []

    def v(i, item):
        seen.append(i)
        return validate_pair(i, item)

    with pytest.raises(CommandError) as e:
        batch.validate_all([["a", 1], "bad", "also bad"], v, field="changes")
    assert seen == [0, 1]
    assert e.value.code == "bad_request"
    assert e.value.message == "changes[1]: need [key, value]"
    assert e.value.details == {"got": "bad", "index": 1, "field": "changes"}


def test_positions_map_reported_index():
    # create_layers(manifest=…): item i came from layers[positions[i]].
    seen = []

    def v(i, item):
        seen.append(i)
        return validate_pair(i, item)

    with pytest.raises(CommandError) as e:
        batch.validate_all([["a", 1], "bad"], v, field="layers", positions=[0, 3])
    assert seen == [0, 1]  # validate_fn still gets the item's own position
    assert e.value.message == "layers[3]: need [key, value]"
    assert e.value.details == {"got": "bad", "index": 3, "field": "layers"}

    ctx = Ctx({})
    with pytest.raises(CommandError) as e:
        batch.run([("a", 1), ("b", "clash")], apply_set, ctx, label="create_layers",
                  field="layers", positions=[2, 5])
    assert e.value.message.startswith("create_layers: layers[5] failed: name taken")
    assert (e.value.details["index"], e.value.details["applied"],
            e.value.details["rolled_back"]) == (5, 1, 1)
    assert ctx.doc == {}


def test_validate_all_keeps_code_and_wraps_non_dict_details():
    def v(i, item):
        raise CommandError("ambiguous", "two layers", details=["{a}", "{b}"])

    with pytest.raises(CommandError) as e:
        batch.validate_all([1], v)
    assert e.value.code == "ambiguous"
    assert e.value.details == {"index": 0, "field": "specs", "cause": ["{a}", "{b}"]}


def test_validate_all_lets_bugs_through():
    def v(i, item):
        raise ZeroDivisionError("bug")

    with pytest.raises(ZeroDivisionError):
        batch.validate_all([1], v)


def test_validation_failure_applies_nothing():
    ctx = Ctx({"x": 0})
    with pytest.raises(CommandError):
        plans = batch.validate_all([["a", 1], 5], validate_pair)
        batch.run(plans, apply_set, ctx)
    assert ctx.doc == {"x": 0} and ctx.calls == []


# --- run -------------------------------------------------------------------

def test_run_applies_in_order():
    ctx = Ctx({})
    out = batch.run([("a", 1), ("b", 2), ("a", 3)], apply_set, ctx)
    assert ctx.doc == {"a": 3, "b": 2}
    assert ctx.calls == [("apply", "a"), ("apply", "b"), ("apply", "a")]
    assert out.results == ["set a", "set b", "set a"]
    assert len(out.inverses) == 3 and not out.dry_run


def test_failure_rolls_back_in_reverse_and_restores_document():
    initial = {"a": "old", "z": 9}
    ctx = Ctx(dict(initial))
    plans = [("a", 1), ("b", 2), ("c", 3), ("d", "boom"), ("e", 5)]
    with pytest.raises(CommandError) as e:
        batch.run(plans, apply_set, ctx, label="update_layers", field="changes")
    assert ctx.doc == initial
    assert ctx.calls[3:] == [("undo", "c"), ("undo", "b"), ("undo", "a")]
    assert ("apply", "e") not in ctx.calls
    err = e.value
    assert err.code == "krita_error"
    assert err.details == {"index": 3, "field": "changes", "applied": 3, "rolled_back": 3,
                           "rollback_errors": []}
    assert err.message.startswith("update_layers: changes[3] failed: KeyError")
    assert "rolled back 3 of 3" in err.message and "partly" not in err.message


def test_failure_at_first_item_rolls_back_nothing():
    ctx = Ctx({})
    with pytest.raises(CommandError) as e:
        batch.run([("a", "boom")], apply_set, ctx)
    assert e.value.details["applied"] == 0 and e.value.details["rolled_back"] == 0
    assert ctx.calls == []


def test_mid_apply_command_error_keeps_its_code():
    ctx = Ctx({})
    with pytest.raises(CommandError) as e:
        batch.run([("a", 1), ("b", "clash")], apply_set, ctx)
    assert e.value.code == "conflict"
    assert "name taken" in e.value.message
    assert e.value.details["cause"] == {"name": "b"}
    assert ctx.doc == {}


def test_failing_inverse_does_not_stop_the_others():
    ctx = Ctx({})

    def apply(c, plan):
        result, inv = apply_set(c, plan)
        if plan[0] == "b":
            def bad(c2):
                raise RuntimeError("node gone")
            return result, bad
        return result, inv

    with pytest.raises(CommandError) as e:
        batch.run([("a", 1), ("b", 2), ("c", 3), ("d", "boom")], apply, ctx)
    d = e.value.details
    assert d["applied"] == 3 and d["rolled_back"] == 2
    assert d["rollback_errors"] == [{"step": 1, "error": "RuntimeError: node gone"}]
    assert "partly changed" in e.value.message
    assert ctx.doc == {"b": 2}  # a and c were undone despite b's failure


def test_none_inverse_is_a_no_op():
    ctx = Ctx({})

    def apply(c, plan):
        if plan[0] == "noop":
            return "nothing", None
        return apply_set(c, plan)

    out = batch.run([("a", 1), ("noop", 0), ("b", 2)], apply, ctx)
    assert len(out.inverses) == 2
    with pytest.raises(CommandError) as e:
        batch.run([("a", 1), ("noop", 0), ("c", "boom")], apply, Ctx({}))
    assert e.value.details["applied"] == 2 and e.value.details["rolled_back"] == 1


def test_bad_apply_return_is_rolled_back_as_krita_error():
    ctx = Ctx({})

    def apply(c, plan):
        apply_set(c, plan)
        return "r", "not callable"

    with pytest.raises(CommandError) as e:
        batch.run([("a", 1)], apply, ctx)
    assert e.value.code == "krita_error" and "TypeError" in e.value.message


def test_dry_run_applies_nothing():
    ctx = Ctx({"a": 0})

    def never(c, plan):
        raise AssertionError("applied during dry run")

    out = batch.run([("a", 1), ("b", 2)], never, ctx, dry_run=True)
    assert out.dry_run and out.inverses == [] and out.results == [("a", 1), ("b", 2)]
    out = batch.run([("a", 1)], never, ctx, dry_run=True, describe_fn=lambda p: {"would_set": p[0]})
    assert out.results == [{"would_set": "a"}]
    assert ctx.doc == {"a": 0}


def test_inverses_use_the_ctx_given_at_call_time():
    apply_ctx = Ctx({})
    out = batch.run([("a", 1), ("b", 2)], apply_set, apply_ctx)
    # Later (krita_undo): a fresh ctx with the live document from the registry.
    later = Ctx(apply_ctx.doc)
    ok, errors = batch.rollback(out.inverses, later)
    assert (ok, errors) == (2, [])
    assert later.calls == [("undo", "b"), ("undo", "a")]
    assert all(ev == "apply" for ev, _ in apply_ctx.calls)
    assert later.doc == {}


def test_outcome_sums_inverse_nbytes():
    def apply(c, plan):
        def inv(c2):
            pass
        inv.nbytes = plan
        return None, inv

    assert batch.run([100, 0, 23], apply, None).nbytes == 123


def test_nbytes_of_ignores_junk():
    def f(c):
        pass
    assert batch.nbytes_of(f) == 0
    for junk in (-5, "12", 1.5, True):
        f.nbytes = junk
        assert batch.nbytes_of(f) == 0


# --- Journal ---------------------------------------------------------------

def _inv(tag):
    def inv(ctx):
        ctx.append(tag)
    return inv


def test_journal_pop_order_and_peek():
    j = batch.Journal(5, 1000)
    assert j.pop() is None and j.peek() is None and len(j) == 0
    j.push("one", [_inv(1)], 10)
    j.push("two", [_inv(2), _inv(3)], 20)
    assert len(j) == 2 and j.total_bytes == 30 and j.peek().label == "two"
    e = j.pop()
    assert e.label == "two" and e.nbytes == 20 and j.total_bytes == 10
    seen = []
    batch.rollback(e.inverses, seen)
    assert seen == [3, 2]
    assert j.pop().label == "one" and j.pop() is None and j.total_bytes == 0


def test_journal_evicts_oldest_by_count():
    j = batch.Journal(3, 10**9)
    for n in range(5):
        j.push(f"a{n}", [_inv(n)])
    assert len(j) == 3
    assert [j.pop().label for _ in range(3)] == ["a4", "a3", "a2"]


def test_journal_evicts_oldest_by_bytes():
    j = batch.Journal(20, 100)
    j.push("a", [_inv(0)], 40)
    j.push("b", [_inv(0)], 40)
    j.push("c", [_inv(0)], 40)  # 120 > 100: a goes
    assert len(j) == 2 and j.total_bytes == 80
    j.push("d", [_inv(0)], 100)  # only d fits
    assert len(j) == 1 and j.total_bytes == 100 and j.peek().label == "d"


def test_journal_refuses_oversized_entry_and_clears():
    j = batch.Journal(20, 100)
    j.push("a", [_inv(0)], 10)
    assert j.push("huge", [_inv(0)], 101) is False
    assert len(j) == 0 and j.total_bytes == 0


def test_journal_skips_empty_entries():
    j = batch.Journal(20, 100)
    assert j.push("nothing", []) is True
    assert len(j) == 0


def test_journal_clear():
    j = batch.Journal(20, 100)
    j.push("a", [_inv(0)], 10)
    j.clear()
    assert len(j) == 0 and j.total_bytes == 0


@pytest.mark.parametrize("args", [(0, 1), (1, 0), (-1, 1), (1.5, 1), (True, 1)])
def test_journal_rejects_bad_limits(args):
    with pytest.raises(ValueError):
        batch.Journal(*args)


def test_journals_per_handle_and_forget(monkeypatch):
    monkeypatch.setattr(config, "current", lambda: config.load(config_file="/nonexistent", env={}))
    js = batch.Journals()
    a, b = js.get("doc-1"), js.get("doc-2")
    assert a is not b and js.get("doc-1") is a
    assert (a.max_actions, a.max_bytes) == (20, 1073741824)
    a.push("x", [_inv(0)])
    js.forget("doc-1")
    assert "doc-1" not in js and len(js) == 1
    assert len(js.get("doc-1")) == 0
    js.forget("doc-9")  # unknown: no error


def test_journals_take_limits_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"journal": {"max_actions": 3, "max_bytes": 500}}))
    cfg.chmod(0o600)
    settings = config.load(config_file=str(cfg), env={}, output_dir=str(tmp_path / "out"),
                           home=str(tmp_path / "home"))
    assert settings.config_loaded and not settings.errors
    monkeypatch.setattr(config, "current", lambda: settings)
    j = batch.Journals().get("doc-1")
    assert (j.max_actions, j.max_bytes) == (3, 500)


# --- P5.1: undo / redo stacks, caps, spill ----------------------------------
#
# The fake document is a dict. Each fake inverse returns the op that
# reverses it (the redo of an undo), as the real ones do.

class SetKey:
    """Set doc[key] = value (``_MISSING`` deletes); returns the SetKey that
    puts the previous value back. ``check`` refuses keys listed in
    doc["gone"]; ``fail`` raises when applied."""

    def __init__(self, key, value, nbytes=0, fail=None):
        self.key, self.value, self.nbytes, self.fail = key, value, nbytes, fail

    @property
    def node_ids(self):
        return (self.key,)

    def check(self, doc):
        return f"{self.key} is gone" if self.key in doc.get("gone", ()) else None

    def __call__(self, doc):
        if self.fail:
            raise self.fail
        old = doc.get(self.key, _MISSING)
        if self.value is _MISSING:
            doc.pop(self.key, None)
        else:
            doc[self.key] = self.value
        return SetKey(self.key, old, self.nbytes)


class SetPixels:
    """Like SetKey, but the value is pixel bytes held in a ``batch.Blob``."""

    def __init__(self, key, data):
        self.key = key
        self.blob = data if isinstance(data, batch.Blob) else batch.Blob(data)
        self.nbytes = self.blob.nbytes

    node_ids = property(lambda self: (self.key,))
    blobs = property(lambda self: (self.blob,))

    def check(self, doc):
        return None

    def __call__(self, doc):
        old = doc[self.key]
        doc[self.key] = self.blob.load()
        return SetPixels(self.key, old)


def tool_set(journal, doc, label, changes):
    """What a tool does: apply ``changes`` ({key: value}) and push one
    entry of SetKey inverses. Returns push's result."""
    inverses = [SetKey(k, _MISSING if v is _MISSING else v)(doc) for k, v in changes.items()]
    return journal.push(label, inverses, sum(batch.nbytes_of(i) for i in inverses))


def tool_paint(journal, doc, key, data):
    old = doc[key]
    doc[key] = data
    inv = SetPixels(key, old)
    return journal.push(f"paint {key}", [inv], inv.nbytes)


def test_undo_redo_round_trip():
    j, doc = batch.Journal(20, 10**6), {}
    tool_set(j, doc, "one", {"a": 1})
    tool_set(j, doc, "two", {"a": 2, "b": 3})
    states = [{}, {"a": 1}, {"a": 2, "b": 3}]
    assert doc == states[2] and (len(j), j.redo_count) == (2, 0)
    entry, reverse, kept = j.undo(doc)
    assert (entry.label, kept) == ("two", True)
    assert entry.node_ids() == ["a", "b"] and reverse.label == "two"
    assert doc == states[1] and (len(j), j.redo_count) == (1, 1)
    j.undo(doc)
    assert doc == states[0] and (len(j), j.redo_count) == (0, 2)
    with pytest.raises(CommandError) as e:
        j.undo(doc, where=" in doc-1")
    assert e.value.code == "conflict" and e.value.message == "nothing to undo in doc-1"
    assert j.redo(doc)[0].label == "one" and doc == states[1]
    assert j.redo(doc)[0].label == "two" and doc == states[2]
    assert (len(j), j.redo_count) == (2, 0)
    with pytest.raises(CommandError) as e:
        j.redo(doc)
    assert e.value.code == "conflict"
    j.undo(doc)
    j.undo(doc)
    assert doc == states[0]  # and again, from the redone entries


def test_push_clears_redo():
    j, doc = batch.Journal(20, 10**6), {}
    tool_set(j, doc, "one", {"a": 1})
    tool_set(j, doc, "two", {"a": 2})
    j.undo(doc)
    assert j.redo_count == 1
    tool_set(j, doc, "three", {"c": 1})
    assert j.redo_count == 0 and [e.label for e in j.entries()] == ["three", "one"]
    with pytest.raises(CommandError):
        j.redo(doc)


def test_count_cap_per_stack():
    j, doc = batch.Journal(3, 10**6), {}
    for n in range(5):
        tool_set(j, doc, f"a{n}", {"k": n})
    assert [e.label for e in j.entries()] == ["a4", "a3", "a2"]
    for _ in range(3):
        j.undo(doc)
    assert doc == {"k": 1} and (len(j), j.redo_count) == (0, 3)


def test_byte_cap_covers_both_stacks_and_drops_the_farthest():
    j, doc = batch.Journal(20, 100), {"k": 0}
    for n in range(2):
        j.push(f"a{n}", [SetKey("k", n, nbytes=40)], 40)
    j.undo(doc)  # its reverse (40 bytes) goes onto redo: 80 in all
    assert (len(j), j.redo_count, j.total_bytes) == (1, 1, 80)
    j.push("b", [SetKey("k", 9, nbytes=40)], 40)  # clears redo: a0, b
    assert (len(j), j.redo_count, j.total_bytes) == (2, 0, 80)
    j.push("c", [SetKey("k", 9, nbytes=40)], 40)  # 120 > 100: a0 goes
    assert [e.label for e in j.entries()] == ["c", "b"]


def test_blob_spill_load_discard(tmp_path):
    data = bytes(range(256)) * 10
    blob = batch.Blob(data)
    assert blob.in_memory and blob.path is None and blob.nbytes == len(data)
    blob.spill(str(tmp_path))
    assert not blob.in_memory and blob.path.startswith(str(tmp_path))
    assert os.path.getsize(blob.path) < len(data)  # compressed
    assert blob.load() == data and blob.load() == data  # loading keeps the file
    path = blob.path
    blob.discard()
    assert not os.path.exists(path) and blob.path is None
    with pytest.raises(OSError):
        blob.load()
    blob.discard()  # twice: fine


def test_pixels_spill_to_disk_oldest_first_and_undo_exactly(tmp_path):
    j, doc = batch.Journal(20, 100, spill_dir=str(tmp_path)), {"p": b"\0" * 60}
    frames = [bytes([n]) * 60 for n in range(1, 4)]
    for frame in frames:
        assert tool_paint(j, doc, "p", frame) is True
    assert j.total_bytes == 180 and j.memory_bytes <= 100
    # The newest stays in memory; the older ones are on disk.
    assert [e.disk_bytes for e in j.entries()] == [0, 60, 60]
    files = j.spill_files()
    assert len(files) == 2 and all(os.path.exists(f) for f in files)
    assert sorted(os.listdir(tmp_path)) == sorted(os.path.basename(f) for f in files)
    for want in (frames[1], frames[0], b"\0" * 60):
        j.undo(doc)
        assert doc["p"] == want
        assert j.memory_bytes <= 100
    # Applied entries' files are gone; the redo entries may spill in turn.
    assert set(os.listdir(tmp_path)) == {os.path.basename(f) for f in j.spill_files()}
    for want in frames:
        j.redo(doc)
        assert doc["p"] == want
    j.clear()
    assert os.listdir(tmp_path) == [] and j.memory_bytes == 0 and j.disk_bytes == 0


def test_disk_cap_drops_the_oldest(tmp_path):
    j = batch.Journal(20, 50, max_disk_bytes=130, spill_dir=str(tmp_path))
    doc = {"p": b"\0" * 60}
    for n in range(1, 5):
        tool_paint(j, doc, "p", bytes([n]) * 60)
    # 60 in memory is over 50: everything spills; 130 on disk holds two.
    assert j.memory_bytes == 0 and j.disk_bytes == 120
    assert [e.label for e in j.entries()] == ["paint p", "paint p"]
    assert len(os.listdir(tmp_path)) == 2


def test_entry_too_big_for_every_cap_is_refused(tmp_path):
    j = batch.Journal(20, 50, max_disk_bytes=50, spill_dir=str(tmp_path))
    doc = {"p": b"\0" * 40, "q": b"\0" * 60}
    assert tool_paint(j, doc, "p", b"\1" * 40) is True
    assert tool_paint(j, doc, "q", b"\1" * 60) is False  # 60 > 50 and > 50
    assert len(j) == 0 and os.listdir(tmp_path) == []
    # Detached layers (no blobs) can't spill: only the memory cap counts.
    assert j.push("delete", [SetKey("x", 1, nbytes=51)], 51) is False
    assert j.push("delete", [SetKey("x", 1, nbytes=50)], 50) is True


def test_reverse_too_big_drops_the_redo_stack():
    class Grows(SetKey):  # its reverse holds more than it does (an undone create)
        def __call__(self, doc):
            back = super().__call__(doc)
            back.nbytes = 500
            return back

    j, doc = batch.Journal(20, 100), {}
    tool_set(j, doc, "one", {"a": 1})
    j.undo(doc)
    assert j.redo_count == 1
    j.push("create", [Grows("b", _MISSING)], 0)
    doc["b"] = 1
    entry, reverse, kept = j.undo(doc)
    assert (kept, reverse.nbytes) == (False, 500)
    assert "b" not in doc and (len(j), j.redo_count) == (0, 0)


def test_failed_check_changes_nothing():
    j, doc = batch.Journal(20, 10**6), {}
    tool_set(j, doc, "one", {"a": 1})
    tool_set(j, doc, "two", {"b": 2, "c": 3})
    doc["gone"] = ["c"]  # checked first: newest inverse first
    before = dict(doc)
    with pytest.raises(CommandError) as e:
        j.undo(doc)
    assert e.value.code == "conflict"
    assert "c is gone" in e.value.message and "nothing was changed" in e.value.message
    assert e.value.details == {"label": "two", "step": 1, "applied": 0, "rolled_forward": 0,
                               "rollback_errors": [], "layers": ["c"]}
    assert doc == before and (len(j), j.redo_count) == (2, 0)
    del doc["gone"]
    j.undo(doc)  # the entry stayed: fixed, it can be retried
    assert doc == {"a": 1}


def test_later_failed_check_rolls_forward():
    j, doc = batch.Journal(20, 10**6), {}
    tool_set(j, doc, "two", {"b": 2, "c": 3})
    doc["gone"] = ["b"]  # c (newest) applies, then b's check fails
    before = dict(doc)
    with pytest.raises(CommandError) as e:
        j.undo(doc)
    assert e.value.code == "conflict"
    assert (e.value.details["applied"], e.value.details["rolled_forward"]) == (1, 1)
    assert doc == before and len(j) == 1


def test_mid_apply_failure_rolls_forward_and_keeps_the_entry():
    j, doc = batch.Journal(20, 10**6), {"a": 0, "b": 0, "c": 0}
    entry_invs = [SetKey("a", 0), SetKey("b", 0, fail=RuntimeError("Krita said no")),
                  SetKey("c", 0)]
    doc.update(a=1, b=1, c=1)
    j.push("three", entry_invs)
    with pytest.raises(CommandError) as e:
        j.undo(doc)
    assert e.value.code == "krita_error" and "Krita said no" in e.value.message
    assert e.value.details["step"] == 1 and e.value.details["applied"] == 1
    assert e.value.details["rolled_forward"] == 1 and e.value.details["rollback_errors"] == []
    assert doc == {"a": 1, "b": 1, "c": 1} and (len(j), j.redo_count) == (1, 0)


def test_mid_apply_command_error_keeps_its_code():
    j, doc = batch.Journal(20, 10**6), {}
    j.push("x", [SetKey("a", 0, fail=CommandError("conflict", "busy", {"why": 1}))])
    with pytest.raises(CommandError) as e:
        j.undo(doc)
    assert e.value.code == "conflict" and e.value.details["cause"] == {"why": 1}


def test_failed_roll_forward_clears_the_journal():
    class BadBack(SetKey):
        def __call__(self, doc):
            super().__call__(doc)
            return SetKey(self.key, 0, fail=RuntimeError("can't go back"))

    j, doc = batch.Journal(20, 10**6), {}
    tool_set(j, doc, "older", {"z": 1})
    j.push("bad", [SetKey("a", 0, fail=RuntimeError("boom")), BadBack("b", 0)])
    with pytest.raises(CommandError) as e:
        j.undo(doc)
    assert e.value.details["journal_cleared"] is True
    assert len(e.value.details["rollback_errors"]) == 1
    assert "partly changed" in e.value.message and "journal was cleared" in e.value.message
    assert (len(j), j.redo_count) == (0, 0)


def test_apply_entry_without_check_and_none_reverse():
    doc = {}
    entry = batch.JournalEntry("x", (lambda d: d.setdefault("a", 1) and None,), 0)
    assert batch.apply_entry(entry, doc) == [] and doc == {"a": 1}


def test_rollback_ignores_returned_reverses():
    doc = {"a": 1}
    assert batch.rollback([SetKey("a", 5)], doc) == (1, [])
    assert doc == {"a": 5}


def test_forget_deletes_spill_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "current", lambda: config.load(config_file="/nonexistent", env={}))
    js = batch.Journals(spill_dir=str(tmp_path))
    j = js.get("doc-1")
    j.max_bytes = 10
    doc = {"p": b"\0" * 64}
    tool_paint(j, doc, "p", b"\1" * 64)
    assert j.disk_bytes == 64 and len(os.listdir(tmp_path)) == 1
    js.forget("doc-1")
    assert os.listdir(tmp_path) == [] and "doc-1" not in js


def test_journals_read_max_disk_bytes_defensively(monkeypatch):
    class S:
        journal = {"max_actions": 4, "max_bytes": 64}

    monkeypatch.setattr(config, "current", lambda: S)
    assert batch.Journals().get("doc-1").max_disk_bytes == batch.DEFAULT_MAX_DISK_BYTES == 4 << 30
    S.journal = {"max_actions": 4, "max_bytes": 64, "max_disk_bytes": 1000}
    assert batch.Journals().get("doc-1").max_disk_bytes == 1000
    S.journal = {"max_actions": 4, "max_bytes": 64, "max_disk_bytes": -1}
    assert batch.Journals().get("doc-1").max_disk_bytes == batch.DEFAULT_MAX_DISK_BYTES


def test_process_spill_dir():
    d = batch.process_spill_dir()
    assert os.path.isdir(d)
    assert os.path.basename(d).startswith(f"{batch.SPILL_PREFIX}{os.getpid()}-")
    assert batch.process_spill_dir() == d


def test_sweep_stale_removes_only_dead_processes_dirs(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # its pid is now (almost certainly) free
    dead = tmp_path / f"{batch.SPILL_PREFIX}{proc.pid}-abc"
    mine = tmp_path / f"{batch.SPILL_PREFIX}{os.getpid()}-abc"
    alive = tmp_path / f"{batch.SPILL_PREFIX}1-abc"  # launchd / init
    no_pid = tmp_path / f"{batch.SPILL_PREFIX}x1y2"
    other = tmp_path / "unrelated-123-abc"
    for d in (dead, mine, alive, no_pid, other):
        d.mkdir()
        (d / "f.z").write_bytes(b"x")
    a_file = tmp_path / f"{batch.SPILL_PREFIX}{proc.pid}-file"
    a_file.write_bytes(b"x")
    batch.sweep_stale(str(tmp_path))
    assert not dead.exists()
    assert mine.exists() and alive.exists() and no_pid.exists() and other.exists()
    assert a_file.exists()
    batch.sweep_stale(str(tmp_path / "missing"))  # no such dir: fine


def test_describe_lists_next_first(tmp_path):
    j, doc = batch.Journal(20, 70, spill_dir=str(tmp_path)), {"p": b"\0" * 40}
    tool_paint(j, doc, "p", b"\1" * 40)
    tool_set(j, doc, "props", {"a": 1})
    tool_paint(j, doc, "p", b"\2" * 40)
    j.undo(doc)
    d = j.describe()
    assert [r["label"] for r in d["undo"]] == ["props", "paint p"]
    assert [r["label"] for r in d["redo"]] == ["paint p"]
    assert d["undo"][1] == {"label": "paint p", "layer_count": 1, "nbytes": 40,
                            "disk_bytes": 40, "on_disk": True}
    assert d["memory_bytes"] == 40 and d["disk_bytes"] == 40
    assert d["limits"] == {"max_actions": 20, "max_bytes": 70,
                           "max_disk_bytes": batch.DEFAULT_MAX_DISK_BYTES}
