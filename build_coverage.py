"""Record per-test SQLGlot coverage on the unmodified competition revision."""

import argparse
import gzip
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import coverage
import pytest

_worker_coverage: coverage.Coverage | None = None
_worker_context = ""


def trace_worker(data_file, sources, nodeid, function, args, kwargs):
    """Run an original pool task under its submitting test's coverage context."""
    global _worker_coverage, _worker_context
    if _worker_coverage is None:
        _worker_coverage = coverage.Coverage(
            config_file=False,
            data_file=data_file,
            data_suffix=True,
            auto_data=True,
            source=sources,
        )
        _worker_coverage.start()
    if _worker_context != nodeid:
        _worker_coverage.switch_context(nodeid)
        _worker_context = nodeid
    return function(*args, **kwargs)


class TestContexts:
    """Attribute setup, the test body, and teardown to the same test."""

    def __init__(self, collector: coverage.Coverage, repo: Path):
        self.collector = collector
        self.repo = repo
        self.nodeid = ""
        self.fixtures: dict[str, set[str]] = {}

    def trace_open(self, event: str, arguments: tuple) -> None:
        if event != "open" or isinstance(arguments[0], int):
            return
        filename = os.fsdecode(arguments[0])
        if "fixtures" not in filename:
            return
        absolute = Path(os.path.abspath(filename))
        if not absolute.is_relative_to(self.repo / "tests" / "fixtures"):
            return
        path = absolute.relative_to(self.repo).as_posix()
        self.fixtures.setdefault(path, set()).add(self.nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item: pytest.Item):
        self.nodeid = item.nodeid
        self.collector.switch_context(self.nodeid)
        try:
            return (yield)
        finally:
            self.nodeid = ""
            self.collector.switch_context("")


def extract_map(data: coverage.CoverageData, contexts: TestContexts) -> dict:
    fixture_contexts = {
        nodeid for nodes in contexts.fixtures.values() for nodeid in nodes
    }
    nodeids = sorted((data.measured_contexts() | fixture_contexts) - {""})
    node_indexes = {nodeid: index for index, nodeid in enumerate(nodeids)}
    files = {}
    imports = {}
    for filename in sorted(data.measured_files()):
        path = Path(filename).relative_to(contexts.repo).as_posix()
        lines = data.contexts_by_lineno(filename)
        test_lines = {}
        collection_lines = []
        for line, line_contexts in sorted(lines.items()):
            if "" in line_contexts:
                collection_lines.append(line)
            indexes = sorted(
                node_indexes[context] for context in line_contexts if context
            )
            if indexes:
                test_lines[line] = indexes
        files[path] = test_lines
        if collection_lines:
            imports[path] = collection_lines
    fixtures = {
        path: sorted(node_indexes[nodeid] for nodeid in nodes if nodeid)
        for path, nodes in sorted(contexts.fixtures.items())
    }
    fixture_imports = sorted(
        path for path, nodes in contexts.fixtures.items() if "" in nodes
    )
    return {
        "nodeids": nodeids,
        "files": files,
        "imports": imports,
        "fixtures": fixtures,
        "fixture_imports": fixture_imports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="pristine pinned SQLGlot checkout")
    parser.add_argument(
        "output", type=Path, help="destination for the gzipped JSON map"
    )
    parser.add_argument(
        "--test-path", action="append", help="limit pytest to a path or node ID"
    )
    parser.add_argument(
        "--allow-test-failures",
        action="store_true",
        help="retain coverage despite baseline assertion or dependency failures",
    )
    args = parser.parse_args()
    repo = args.checkout.resolve()
    destination = args.output.resolve()
    if (
        not (repo / "sqlglot" / "__init__.py").is_file()
        or not (repo / "tests").is_dir()
    ):
        parser.error(f"expected a SQLGlot checkout at {repo}")

    os.chdir(repo)
    sys.path.insert(0, str(repo))
    os.environ["SKIP_INTEGRATION"] = "0"
    sources = (str(repo / "sqlglot"), str(repo / "tests"))
    pytest_args = ["-q", *(args.test_path or ["tests"])]
    original_submit = ProcessPoolExecutor.submit

    with TemporaryDirectory(dir=destination.parent) as temporary:
        data_file = str(Path(temporary) / ".coverage")
        collector = coverage.Coverage(
            config_file=False, data_file=data_file, source=sources
        )
        contexts = TestContexts(collector, repo)
        sys.addaudithook(contexts.trace_open)

        def submit(pool, fn, /, *args, **kwargs):
            if contexts.nodeid:
                return original_submit(
                    pool,
                    trace_worker,
                    data_file,
                    sources,
                    contexts.nodeid,
                    fn,
                    args,
                    kwargs,
                )
            return original_submit(pool, fn, *args, **kwargs)

        collector.start()
        try:
            with patch.object(ProcessPoolExecutor, "submit", submit):
                status = pytest.main(pytest_args, plugins=[contexts])
        finally:
            collector.stop()

        data = collector.get_data()
        try:
            for path in sorted(Path(temporary).glob(".coverage.*")):
                worker_data = coverage.CoverageData(basename=path)
                try:
                    worker_data.read()
                    data.update(worker_data)
                finally:
                    worker_data.close(force=True)
            test_map = extract_map(data, contexts)
        finally:
            data.close(force=True)

    if status == pytest.ExitCode.TESTS_FAILED and args.allow_test_failures:
        print("Retaining coverage despite baseline test failures", file=sys.stderr)
    elif status != pytest.ExitCode.OK:
        print(
            f"Baseline pytest run returned {status}; coverage map was not written",
            file=sys.stderr,
        )
        return int(status)

    if not test_map["nodeids"]:
        print("No SQLGlot coverage was recorded", file=sys.stderr)
        return 1
    with (
        destination.open("wb") as output,
        gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed,
    ):
        compressed.write(json.dumps(test_map, separators=(",", ":")).encode("utf-8"))
    print(f"Wrote coverage for {len(test_map['nodeids'])} tests to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
