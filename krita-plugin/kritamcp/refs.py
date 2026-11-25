"""Layer refs (D4): parsing and matching over a plain node index.

Pure: no krita / PyQt5 imports. ``nodes.py`` walks the Krita tree and
builds the ``NodeIndex``; everything here works on plain dicts.

A ref is tried as, in this order:

1. ``"/"``: the document root (only where ``allow_root``).
2. ``"$key"``: a batch-local key from earlier ``create_layers`` specs (D11).
3. A UUID, with or without braces, any case. UUID form wins: a layer
   literally named like a UUID is reached with a path, ``/{…}``.
4. A name path, if the ref contains an unescaped ``/``. It is anchored at
   the root and one leading ``/`` is optional (``/Face`` is the top-level
   ``Face`` only). Every node matching the whole path counts, so a name
   repeated at an intermediate level fans out.
5. Otherwise a bare name, matched against every node in the document.

In names, ``\\/`` is a literal slash and ``\\\\`` a literal backslash; any
other backslash is an error. Name matching is exact: case-sensitive, no
trimming. A layer whose name starts with ``$`` is reached with a path
(``/$name`` at the top level). The document's global selection mask is
never in the index, so no ref reaches it.
"""

import re

from .errors import CommandError

# The document root. A string so it is JSON-friendly; it can't collide with
# a braced UUID.
ROOT = "/"

_UUID_RE = re.compile(
    r"\{(?P<a>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}"
    r"|(?P<b>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def normalize_uuid(s):
    """Braced lowercase form of ``s`` if it is a UUID, else ``None``."""
    if not isinstance(s, str):
        return None
    m = _UUID_RE.fullmatch(s)
    if m is None:
        return None
    return "{" + (m.group("a") or m.group("b")).lower() + "}"


def escape_name(name):
    """Escape one layer name for use as a path component."""
    return name.replace("\\", "\\\\").replace("/", "\\/")


def split_path(path, *, field="ref"):
    """Split an escaped name path into raw names.

    ``"/"`` is the root and gives ``[]``. One leading ``/`` is ignored.
    Raises ``bad_request`` on an empty path, an empty component, or a
    backslash not followed by ``/`` or ``\\``.
    """
    if not isinstance(path, str) or path == "":
        raise CommandError("bad_request", f"{field} must be a non-empty string")
    if path == "/":
        return []
    body = path[1:] if path.startswith("/") else path
    names, cur, i = [], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if nxt not in ("/", "\\"):
                raise CommandError(
                    "bad_request",
                    f"{field} '{path}': bad escape at position {i + (len(path) - len(body))}; "
                    r"only '\/' and '\\' are allowed",
                )
            cur.append(nxt)
            i += 2
            continue
        if c == "/":
            names.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    names.append("".join(cur))
    if any(n == "" for n in names):
        raise CommandError("bad_request", f"{field} '{path}': empty name in path")
    return names


def join_path(names):
    """Join raw names into an escaped path; ``[]`` is the root ``"/"``."""
    if not names:
        return ROOT
    return "/".join(escape_name(n) for n in names)


def _has_unescaped_slash(s):
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "/":
            return True
        i += 1
    return False


class NodeIndex:
    """The document's node tree as plain data.

    ``entries``: dicts ``{"id", "name", "parent", "type"}`` in global
    stacking order, top to bottom and depth-first (the Layers docker
    order). ``parent`` is ``None`` for top-level nodes, and a parent comes
    before its children. A root-level ``selectionmask`` (the global
    selection, D4) and anything under it is skipped.

    Methods take canonical (braced lowercase) ids. ``parent`` returns
    ``ROOT`` for top-level nodes, so it compares directly with ``resolve``
    results.
    """

    def __init__(self, entries):
        self._nodes = {}
        self._children = {ROOT: []}
        self._order = {}
        skipped = set()
        for e in entries:
            nid = normalize_uuid(e["id"])
            if nid is None:
                raise ValueError(f"bad node id: {e['id']!r}")
            par = e.get("parent")
            par = ROOT if par is None else normalize_uuid(par)
            if par is None:
                raise ValueError(f"bad parent id: {e['parent']!r}")
            if par in skipped or (par == ROOT and e["type"] == "selectionmask"):
                skipped.add(nid)
                continue
            if nid in self._nodes:
                raise ValueError(f"duplicate node id: {nid}")
            if par not in self._children:
                raise ValueError(f"parent {par} of {nid} is not listed before it")
            node = {"id": nid, "name": e["name"], "parent": par, "type": e["type"]}
            self._nodes[nid] = node
            self._children[nid] = []
            self._children[par].append(nid)
            self._order[nid] = len(self._order)
        self._by_name = {}
        for nid, node in self._nodes.items():  # insertion order = stacking order
            self._by_name.setdefault(node["name"], []).append(nid)

    def __contains__(self, nid):
        return nid in self._nodes

    def __len__(self):
        return len(self._nodes)

    def __iter__(self):
        """Ids in stacking order."""
        return iter(self._nodes)

    def get(self, nid):
        """The entry for ``nid`` (with ``parent`` as ``ROOT`` at top level), or None."""
        return self._nodes.get(nid)

    def children(self, nid):
        """Child ids of a node or ``ROOT``, top to bottom."""
        return list(self._children[nid])

    def parent(self, nid):
        """Parent id, or ``ROOT`` for a top-level node."""
        return self._nodes[nid]["parent"]

    def order(self, nid):
        """Global stacking position, 0 at the top of the Layers docker."""
        return self._order[nid]

    def ancestors(self, nid):
        """Ancestor ids, nearest first, excluding ``ROOT``."""
        out = []
        p = self._nodes[nid]["parent"]
        while p != ROOT:
            out.append(p)
            p = self._nodes[p]["parent"]
        return out

    def is_descendant(self, a, b):
        """True if ``a`` is strictly inside ``b`` (``b`` may be ``ROOT``)."""
        if a == ROOT or a not in self._nodes:
            return False
        if b == ROOT:
            return True
        return b in self.ancestors(a)

    def path_of(self, nid):
        """Escaped name path from the root, without a leading ``/``.

        A top-level node's path is its bare name; prefix ``/`` to anchor
        it when the name is not unique. ``path_of(ROOT)`` is ``"/"``.
        """
        if nid == ROOT:
            return ROOT
        names = [self._nodes[a]["name"] for a in reversed(self.ancestors(nid))]
        names.append(self._nodes[nid]["name"])
        return join_path(names)

    def by_name(self, name):
        """Ids of nodes named exactly ``name``, in stacking order."""
        return list(self._by_name.get(name, ()))


def resolve(index, ref, *, keys=None, allow_root=False, field="ref"):
    """Resolve one layer ref to a canonical id, or ``ROOT``.

    ``keys`` maps batch keys (without ``$``) to ids; ``None`` means batch
    keys are not valid here. Key ids are returned as given, unchecked,
    since they may name layers a batch has yet to create.

    Raises ``CommandError``: ``bad_request`` for a malformed ref,
    ``not_found``, or ``ambiguous`` with ``details.matches`` (ids in
    stacking order).
    """
    if not isinstance(ref, str) or ref == "":
        raise CommandError("bad_request", f"{field} must be a non-empty string (a layer id, path or name)")

    if ref == ROOT:
        if allow_root:
            return ROOT
        raise CommandError("bad_request", f"{field}: the root '/' is only valid as a parent")

    if ref.startswith("$"):
        key = ref[1:]
        if key == "":
            raise CommandError("bad_request", f"{field}: '$' needs a key name")
        if keys is None:
            raise CommandError("bad_request", f"{field}: batch keys like '{ref}' are not valid here")
        if key not in keys:
            raise CommandError("not_found", f"No batch key '{ref}' defined by an earlier spec",
                               {"key": key})
        return keys[key]

    nid = normalize_uuid(ref)
    if nid is not None:
        if nid not in index:
            raise CommandError("not_found", f"No layer with id {nid}")
        return nid

    if _has_unescaped_slash(ref):
        return _resolve_path(index, ref, field)

    (name,) = split_path(ref, field=field)
    matches = index.by_name(name)
    if not matches:
        raise CommandError("not_found", f"No layer named '{ref}'")
    if len(matches) > 1:
        raise CommandError("ambiguous", f"{len(matches)} layers named '{ref}': use an id or a path",
                           {"matches": matches})
    return matches[0]


def _resolve_path(index, ref, field):
    names = split_path(ref, field=field)
    frontier = [ROOT]
    for name in names:
        frontier = [c for p in frontier for c in index.children(p) if index.get(c)["name"] == name]
        if not frontier:
            break
    if not frontier:
        msg = f"No layer at path '{ref}'"
        if index.by_name(ref):
            msg += f" (a layer is named '{ref}': write '{escape_name(ref)}')"
        raise CommandError("not_found", msg)
    if len(frontier) > 1:
        frontier.sort(key=index.order)
        raise CommandError("ambiguous", f"{len(frontier)} layers at path '{ref}': use an id",
                           {"matches": frontier})
    return frontier[0]


def resolve_many(index, refs, *, keys=None, allow_root=False, field="ids"):
    """Resolve a list of refs. A failure adds ``details.index`` (the list position)."""
    if not isinstance(refs, list):
        raise CommandError("bad_request", f"{field} must be a list of layer refs")
    out = []
    for i, ref in enumerate(refs):
        try:
            out.append(resolve(index, ref, keys=keys, allow_root=allow_root, field=f"{field}[{i}]"))
        except CommandError as e:
            raise CommandError(e.code, e.message, {**(e.details or {}), "index": i}) from None
    return out
