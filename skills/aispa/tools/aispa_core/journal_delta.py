"""Deterministic JSON state deltas for the change journal (deltaJournal runs).

An operation is one of ``["set", path, value]``, ``["del", path]`` or
``["add", path, items]`` (append ``items`` to the list at ``path``); ``path``
is a list of dict keys (str) and list indexes (int). ``apply`` never mutates
its input: containers on a changed path are copied, untouched subtrees are
shared, so callers must treat every returned state as read-only or copy it.
"""

OPERATIONS = ("set", "del", "add")


def diff(old, new):
    """Operations that turn ``old`` into ``new``; the same inputs give the same list."""
    ops = []
    _diff(old, new, [], ops)
    return ops


def _container(value):
    return isinstance(value, (dict, list))


def _equal_leaf(old, new):
    return type(old) is type(new) and old == new


def _child(old, new, path, ops):
    if old is new:
        return
    if _container(new) and type(old) is type(new):
        # A deep C-level comparison first; a hidden bool/int or int/float swap
        # is still caught by the writer, which replays and checks the digest.
        if old != new:
            _diff(old, new, path, ops)
    elif not _equal_leaf(old, new):
        ops.append(["set", path, new])


def _diff(old, new, path, ops):
    if type(old) is not type(new):
        ops.append(["set", path, new])
    elif isinstance(new, dict):
        for key in old:
            if key not in new:
                ops.append(["del", path + [key]])
        for key, value in new.items():
            if key not in old:
                ops.append(["set", path + [key], value])
            else:
                _child(old[key], value, path + [key], ops)
    elif isinstance(new, list):
        if len(new) < len(old):
            ops.append(["set", path, new])
            return
        for index in range(len(old)):
            _child(old[index], new[index], path + [index], ops)
        if len(new) > len(old):
            ops.append(["add", path, new[len(old):]])
    elif not _equal_leaf(old, new):
        ops.append(["set", path, new])


def _step(container, step):
    if isinstance(container, dict):
        if type(step) is not str:
            raise ValueError("delta: dict step must be a string")
        return step
    if isinstance(container, list):
        if type(step) is not int or not 0 <= step < len(container):
            raise ValueError("delta: list step out of range")
        return step
    raise ValueError("delta: path crosses a scalar")


def apply(state, ops):
    """Replay operations on a copy-on-write view of ``state``."""
    if not isinstance(ops, list):
        raise ValueError("delta: operations must be a list")
    fresh, alive = set(), []  # ``alive`` pins ids of containers created here

    def own(value):
        if id(value) in fresh:
            return value
        copied = dict(value) if isinstance(value, dict) else list(value)
        fresh.add(id(copied))
        alive.append(copied)
        return copied

    root = state
    for op in ops:
        if (not isinstance(op, list) or not op or op[0] not in OPERATIONS
                or len(op) != (2 if op[0] == "del" else 3) or not isinstance(op[1], list)):
            raise ValueError("delta: malformed operation")
        kind, path = op[0], op[1]
        if not path:
            if kind != "set":
                raise ValueError("delta: only set may replace the root")
            root = op[2]
            continue
        if not _container(root):
            raise ValueError("delta: path crosses a scalar")
        root = own(root)
        parent = root
        walk = path if kind == "add" else path[:-1]
        for step in walk:
            key = _step(parent, step)
            child = parent[key]
            if not _container(child):
                raise ValueError("delta: path crosses a scalar")
            child = own(child)
            parent[key] = child
            parent = child
        if kind == "add":
            if not isinstance(parent, list) or not isinstance(op[2], list):
                raise ValueError("delta: add needs a list target and list items")
            parent.extend(op[2])
            continue
        last = path[-1]
        if isinstance(parent, dict):
            if type(last) is not str:
                raise ValueError("delta: dict step must be a string")
            if kind == "del" and last not in parent:
                raise ValueError("delta: deleted key is absent")
        else:
            last = _step(parent, last)
        if kind == "set":
            parent[last] = op[2]
        else:
            del parent[last]
    return root
