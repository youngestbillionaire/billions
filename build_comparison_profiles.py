"""Measure when baseline identity and equality predicates have the same result."""

import argparse
import ast
import atexit
import gzip
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from enum import Enum
from itertools import accumulate
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

_unscoped = set()
_results: dict[str, dict[tuple[str, int, int], bool]] = {}
_current_test: dict[tuple[str, int, int], bool] | None = None
_worker_registered = False
_primitives = (bool, int, float, complex, str, bytes)


def equality_matches(left, right, identical, negate):
    """Evaluate equality only for domains without user-defined comparisons."""
    if left is None or right is None:
        partner = right if left is None else left
        partner_type = type(partner)
        if partner is None or any(
            partner_type is primitive for primitive in _primitives
        ):
            return True
        return type.__getattribute__(partner_type, "__eq__") is object.__eq__ and (
            not negate or type.__getattribute__(partner_type, "__ne__") is object.__ne__
        )

    value_type = type(left)
    if value_type is not type(right) or not isinstance(left, Enum):
        return False
    equality = type.__getattribute__(value_type, "__eq__")
    if negate:
        inequality = type.__getattribute__(value_type, "__ne__")
        if equality is object.__eq__:
            return inequality is object.__ne__
        if inequality is str.__ne__ or inequality is int.__ne__:
            return inequality(left, right) is (not identical)
        return False
    if equality is object.__eq__:
        return True
    if equality is str.__eq__ or equality is int.__eq__:
        return equality(left, right) is identical
    return False


def observe_identity(site, negate, left, right):
    identical = left is right
    if _current_test is None:
        _unscoped.add(site)
    elif _current_test.get(site) is not False:
        _current_test[site] = equality_matches(left, right, identical, negate)
    return not identical if negate else identical


def serialize_results(path: Path) -> None:
    contents = {
        "unscoped": list(_unscoped),
        "tests": {
            test: [(*site, result) for site, result in sites.items()]
            for test, sites in _results.items()
        },
    }
    path.write_text(json.dumps(contents), encoding="utf-8")


def profile_task(directory, nodeid, function, args, kwargs):
    """Propagate the submitting test's context into an unmodified pool task."""
    global _current_test, _worker_registered
    if not _worker_registered:
        atexit.register(
            serialize_results, Path(directory) / f"worker-{os.getpid()}.json"
        )
        _worker_registered = True
    _current_test = _results.setdefault(nodeid, {})
    try:
        return function(*args, **kwargs)
    finally:
        _current_test = None


def instrument_file(path: Path, relative_path: str) -> None:
    source = path.read_bytes()
    nodes = ast.parse(source)
    offsets = tuple(
        accumulate((0, *(len(line) for line in source.splitlines(keepends=True))))
    )
    edits = []
    for node in ast.walk(nodes):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        operator = node.ops[0]
        if not isinstance(operator, (ast.Is, ast.IsNot)):
            continue
        left, right = node.left, node.comparators[0]
        if not isinstance(left, ast.Attribute) and not isinstance(right, ast.Attribute):
            continue
        if any(
            isinstance(operand, ast.Compare)
            for expr in (left, right)
            for operand in ast.walk(expr)
        ):
            continue

        def span(expression):
            start = offsets[expression.lineno - 1] + expression.col_offset
            end = offsets[expression.end_lineno - 1] + expression.end_col_offset
            return source[start:end]

        position = (relative_path, node.lineno, node.col_offset)
        negate = isinstance(operator, ast.IsNot)
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        call = f"_rwx_observe_identity({position!r}, {negate!r}, (".encode()
        edits.append((start, end, call + span(left) + b"), (" + span(right) + b"))"))

    if not edits:
        return
    first = 0
    body = nodes.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        first = body[0].end_lineno
        body = body[1:]
    for statement in body:
        if (
            not isinstance(statement, ast.ImportFrom)
            or statement.module != "__future__"
        ):
            break
        first = statement.end_lineno
    imports = b"from build_comparison_profiles import observe_identity as _rwx_observe_identity\n"
    edits.append((offsets[first], offsets[first], imports))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    compile(source, str(path), "exec")
    path.write_bytes(source)


class TestContexts:
    nodeid = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item):
        global _current_test
        self.nodeid = item.nodeid
        _current_test = _results.setdefault(item.nodeid, {})
        try:
            return (yield)
        finally:
            self.nodeid = None
            _current_test = None


def merged_profiles(directory: Path) -> dict:
    unscoped = set()
    tests = {}
    for path in sorted(directory.glob("*.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        unscoped.update(map(tuple, contents["unscoped"]))
        for test, records in contents["tests"].items():
            by_site = tests.setdefault(test, {})
            for file, line, column, equivalent in records:
                site = (file, line, column)
                by_site[site] = by_site.get(site, True) and equivalent

    safe = {}
    for test, sites in sorted(tests.items()):
        for (file, line, column), equivalent in sorted(sites.items()):
            if equivalent and (file, line, column) not in unscoped:
                safe.setdefault(file, {}).setdefault(f"{line}:{column}", []).append(
                    test
                )
    return {"safe": safe}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="pristine pinned SQLGlot checkout")
    parser.add_argument(
        "output", type=Path, help="destination for the gzipped JSON profile"
    )
    parser.add_argument("test", nargs="+", help="pytest paths or node IDs to profile")
    args = parser.parse_args()
    repo = args.checkout.resolve()
    destination = args.output.resolve()
    os.chdir(repo)
    os.environ["SKIP_INTEGRATION"] = "0"
    original_submit = ProcessPoolExecutor.submit
    contexts = TestContexts()

    with TemporaryDirectory(dir=destination.parent) as temporary:
        work = Path(temporary)
        package = work / "sqlglot"
        shutil.copytree(
            repo / "sqlglot", package, ignore=shutil.ignore_patterns("__pycache__")
        )
        for file in sorted(package.rglob("*.py")):
            instrument_file(file, file.relative_to(work).as_posix())
        sys.path[:0] = [temporary, str(repo)]

        def submit(pool, fn, /, *args, **kwargs):
            if contexts.nodeid is not None:
                return original_submit(
                    pool, profile_task, temporary, contexts.nodeid, fn, args, kwargs
                )
            return original_submit(pool, fn, *args, **kwargs)

        with patch.object(ProcessPoolExecutor, "submit", submit):
            status = pytest.main(
                ["-q", "--import-mode=importlib", *args.test], plugins=[contexts]
            )
        if status != pytest.ExitCode.OK:
            return int(status)

        serialize_results(work / "main.json")
        profiles = merged_profiles(work)

    data = json.dumps(profiles, separators=(",", ":"), sort_keys=True).encode()
    destination.write_bytes(gzip.compress(data, mtime=0))
    print(
        f"Wrote comparison profiles for {len(profiles['safe'])} files to {destination}"
    )
    return 0


if __name__ == "__main__":
    from build_comparison_profiles import main as build_profiles

    raise SystemExit(build_profiles())
