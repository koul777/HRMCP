"""Run one deterministic shard of the unittest suite.

Every test module belongs to exactly one shard, chosen by CRC32 of its module
name, so parallel CI jobs cover the full suite without overlap. Tests that fail
to import are always kept in shard 0 so an import error cannot go unreported.
"""
from __future__ import annotations

import argparse
import sys
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def iter_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def shard_of(test: unittest.TestCase, count: int) -> int:
    module = type(test).__module__
    if module == "unittest.loader":  # _FailedTest for a module that did not import
        return 0
    return zlib.crc32(module.encode("utf-8")) % count


def select_shard(suite: unittest.TestSuite, index: int, count: int) -> unittest.TestSuite:
    return unittest.TestSuite(t for t in iter_tests(suite) if shard_of(t, count) == index)


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
