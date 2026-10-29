#!/usr/bin/env python3
"""Fast parallel test runner for team-sync.

Discovers the same tests as `python3 -m unittest discover -s tests` and runs
them across worker processes (one test method per task), so the suite's wall
time drops from "sum of every test" to "sum of every test / workers". Each
worker process builds its own git sandbox template once (see
tests/test_team.py's _get_template()) and reuses it for every test it runs.

Usage:
    python3 tools/run-tests.py                # all tests, os.cpu_count() workers
    python3 tools/run-tests.py -j 4            # pin worker count
    python3 tools/run-tests.py -k Overlap      # filter by substring/fnmatch, like unittest -k
    python3 tools/run-tests.py -v              # print one line per test as it finishes

Exit code 0 if every test passed, 1 otherwise. Stdlib only, Python 3.8+.
"""
import argparse
import fnmatch
import multiprocessing
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
TESTS_DIR = os.path.join(REPO_ROOT, "tests")


def discover_ids():
    """Full dotted test ids (e.g. "test_team.TeamSyncTests.test_foo"), in the
    same module-naming scheme as `python3 -m unittest discover -s tests`."""
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS_DIR)
    ids = []
    _flatten(suite, ids)
    return ids


def _flatten(suite, out):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            _flatten(item, out)
        else:
            out.append(item.id())


def matches(test_id, pattern):
    if not pattern:
        return True
    name = test_id.rsplit(".", 1)[-1]
    if "*" in pattern:
        return fnmatch.fnmatch(test_id, pattern) or fnmatch.fnmatch(name, pattern)
    return pattern in test_id


def _worker_init(tests_dir):
    sys.path.insert(0, tests_dir)


def _run_one(test_id):
    """Runs one test by its full dotted id. Must be a module-level function so
    it can be pickled and sent to worker processes."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(test_id)
    result = unittest.TestResult()
    t0 = time.time()
    suite.run(result)
    dt = time.time() - t0
    if result.errors:
        return (test_id, "error", dt, result.errors[0][1])
    if result.failures:
        return (test_id, "failure", dt, result.failures[0][1])
    if result.skipped:
        return (test_id, "skipped", dt, result.skipped[0][1])
    return (test_id, "ok", dt, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-j", type=int, default=os.cpu_count() or 1,
                        help="number of worker processes (default: os.cpu_count())")
    parser.add_argument("-k", dest="pattern", default=None,
                        help="only run tests whose id matches this substring/fnmatch pattern")
    parser.add_argument("-v", action="store_true", help="print one line per test as it finishes")
    args = parser.parse_args(argv)

    ids = [i for i in discover_ids() if matches(i, args.pattern)]
    if not ids:
        print("no tests matched")
        return 1

    workers = max(1, min(args.j, len(ids)))
    start = time.time()
    results = []
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=workers, initializer=_worker_init, initargs=(TESTS_DIR,)) as pool:
        for res in pool.imap_unordered(_run_one, ids):
            results.append(res)
            if args.v:
                test_id, outcome, dt, _ = res
                print("{} ... {} ({:.3f}s)".format(test_id, outcome, dt))

    total = time.time() - start
    failures = [r for r in results if r[1] == "failure"]
    errors = [r for r in results if r[1] == "error"]
    skipped = [r for r in results if r[1] == "skipped"]

    if failures or errors:
        print()
        for test_id, outcome, _, detail in results:
            if outcome in ("failure", "error"):
                print("=" * 70)
                print("{}: {}".format(outcome.upper(), test_id))
                print("-" * 70)
                print(detail)

    print()
    print("-" * 70)
    print("Ran {} tests in {:.3f}s using {} workers".format(len(results), total, workers))
    print()
    if failures or errors:
        print("FAILED (failures={}, errors={}, skipped={})".format(
            len(failures), len(errors), len(skipped)))
        return 1
    print("OK" + (" (skipped={})".format(len(skipped)) if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
