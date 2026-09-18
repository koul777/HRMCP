"""Run one deterministic shard of the unittest suite.

Every test module belongs to exactly one shard so parallel CI jobs cover the
full suite without overlap. Modules are packed longest-first using recorded
runtimes (`tests/fixtures/test_shard_weights.json`), because hashing alone left
one shard at 21 minutes against 13 for another and the job is only as fast as
its slowest shard. A module with no recorded time falls back to CRC32 of its
name, so a newly added test file still lands somewhere deterministic. Tests
that fail to import are always kept in shard 0 so an import error cannot go
unreported.
"""
from __future__ import annotations

import argparse
import json
import sys
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_PATH = ROOT / "tests" / "fixtures" / "test_shard_weights.json"
# A module we have never timed is assumed average rather than free, so a new
# test file cannot quietly pile onto one shard.
DEFAULT_MODULE_WEIGHT = 12.0


def iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def load_weights(path: Path = WEIGHTS_PATH) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    weights = payload.get("weights")
    return weights if isinstance(weights, dict) else {}


def assign_modules(modules: list[str], count: int, weights: dict[str, float]) -> dict[str, int]:
    """Pack modules into shards, longest first, always filling the lightest."""
    ordered = sorted(
        modules,
        key=lambda name: (-float(weights.get(name, DEFAULT_MODULE_WEIGHT)), name),
    )
    loads = [0.0] * count
    assignment: dict[str, int] = {}
    for name in ordered:
        target = min(range(count), key=lambda index: (loads[index], index))
        assignment[name] = target
        loads[target] += float(weights.get(name, DEFAULT_MODULE_WEIGHT))
    return assignment


def shard_of(test: unittest.TestCase, count: int, assignment: dict[str, int] | None = None) -> int:
    module = type(test).__module__
    if module == "unittest.loader":  # _FailedTest for a module that did not import
        return 0
    if assignment and module in assignment:
        return assignment[module]
    return zlib.crc32(module.encode("utf-8")) % count


def select_shard(
    suite: unittest.TestSuite,
    index: int,
    count: int,
    weights: dict[str, float] | None = None,
) -> unittest.TestSuite:
    tests = list(iter_tests(suite))
    modules = sorted(
        {
            type(test).__module__
            for test in tests
            if type(test).__module__ != "unittest.loader"
        }
    )
    assignment = assign_modules(modules, count, weights if weights is not None else load_weights())
    return unittest.TestSuite(
        test for test in tests if shard_of(test, count, assignment) == index
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--start-dir", default=str(ROOT / "tests"))
    args = parser.parse_args(argv)
    if args.count < 1 or not 0 <= args.index < args.count:
        parser.error("--index must be in [0, --count)")
    suite = unittest.defaultTestLoader.discover(args.start_dir)
    selected = select_shard(suite, args.index, args.count)
    print(f"shard {args.index + 1}/{args.count}: {selected.countTestCases()} tests", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(selected)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
