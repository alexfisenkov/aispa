#!/usr/bin/env python3
"""Run unchanged unittest discovery, optionally partitioned by occurrence.

Every discovered leaf, including repeated IDs and repeated instances, keeps its
original position. Standard module/class fixtures apply to the selected order;
each shard is a separate process and therefore owns its own fixture lifecycle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import traceback
import unittest


def flatten_suite(suite):
    """Yield leaves in discovery order without collapsing duplicate IDs."""
    if isinstance(suite, unittest.BaseTestSuite):
        for child in suite:
            yield from flatten_suite(child)
    else:
        yield suite


def validate_partition(shards, shard_index):
    if type(shards) is not int or shards < 1:
        raise ValueError("--shards must be a positive integer")
    if type(shard_index) is not int or not 0 <= shard_index < shards:
        raise ValueError("--shard-index must satisfy 0 <= index < shards")


def select_occurrences(cases, shards, shard_index):
    """Return (original index, case) pairs; identity alone is not an occurrence."""
    validate_partition(shards, shard_index)
    return [(index, test) for index, test in enumerate(cases) if index % shards == shard_index]


def manifest_sha256(cases):
    ordered_ids = [test.id() for test in cases]
    raw = json.dumps(ordered_ids, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ImmediateTextResult(unittest.TextTestResult):
    """Keep normal result accounting and flush diagnostics before later tests."""
    def _immediate(self, kind, test, formatted):
        self.stream.writeln(self.separator1)
        self.stream.writeln("IMMEDIATE " + kind + ": " + self.getDescription(test))
        self.stream.writeln(self.separator2)
        self.stream.writeln(formatted.rstrip("\n"))
        self.stream.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._immediate("FAIL", test, self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        self._immediate("ERROR", test, self.errors[-1][1])

    def addSubTest(self, test, subtest, err):
        failures, errors = len(self.failures), len(self.errors)
        super().addSubTest(test, subtest, err)
        if len(self.failures) > failures:
            self._immediate("FAIL", subtest, self.failures[-1][1])
        elif len(self.errors) > errors:
            self._immediate("ERROR", subtest, self.errors[-1][1])


class _DiscoveryErrors(list):
    """TestLoader's public error list also emits each diagnostic immediately."""
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def append(self, error):
        super().append(error)
        self.stream.write("DISCOVERY ERROR:\n" + error.rstrip("\n") + "\n")
        self.stream.flush()


def run_contract_tests(project_root, *, shards=1, shard_index=0, stream=None):
    """Discover the normal tests root and execute every selected real leaf."""
    validate_partition(shards, shard_index)
    stream = sys.stderr if stream is None else stream
    project_root = Path(project_root).resolve()
    # ``python -m unittest`` starts with the project root importable. A tools/
    # script instead starts with tools/; restore that context without changing
    # discover's top-level directory, module IDs or occurrence order.
    sys.path.insert(0, str(project_root))
    loader = unittest.TestLoader()
    loader.errors = _DiscoveryErrors(stream)
    try:
        discovered = loader.discover(str(project_root / "tests"))
        cases = list(flatten_suite(discovered))
        manifest = manifest_sha256(cases)
    except Exception:
        stream.write("Contract discovery failed:\n")
        traceback.print_exc(file=stream)
        stream.flush()
        return 1
    selected = select_occurrences(cases, shards, shard_index)
    stream.write("Contract tests: discovered=" + str(len(cases))
                 + " ordered_ids_sha256=" + manifest + " selected=" + str(len(selected))
                 + " shard_index=" + str(shard_index) + " shards=" + str(shards) + "\n")
    stream.flush()
    if not selected:
        stream.write("Contract tests refused: selected shard is empty.\n")
        stream.flush()
        return 2
    # _FailedTest remains in its original occurrence. Other shards must also
    # refuse a passing exit when discovery could not enumerate a whole module.
    result = unittest.TextTestRunner(stream=stream, verbosity=2,
        resultclass=ImmediateTextResult, tb_locals=False).run(
            unittest.TestSuite(test for _index, test in selected))
    if loader.errors:
        stream.write("Contract discovery is incomplete; shard exit is unsuccessful.\n")
        stream.flush()
    return 0 if result.wasSuccessful() and not loader.errors else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        validate_partition(args.shards, args.shard_index)
    except ValueError as error:
        parser.error(str(error))
    return run_contract_tests(Path(__file__).resolve().parents[1],
                              shards=args.shards, shard_index=args.shard_index)


if __name__ == "__main__":
    raise SystemExit(main())
