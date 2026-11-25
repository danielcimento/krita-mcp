"""batch.py: validate-all, apply in order, rollback, dry_run, journal.

The fake "document" is a dict; the fake applier sets keys and returns an
inverse that restores the old value through the ctx it is called with.
"""

import json

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
