"""Select tests affected by independent patches against the pinned checkout."""

import argparse
import ast
import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DIALECT_PACKAGES = {"dialects", "generators", "parsers", "typing"}


@dataclass
class Change:
    path: str
    edits: list[tuple[int, list[str], list[str]]] = field(default_factory=list)
    structural: bool = False


def read_patch(patch):
    """Collect contiguous edit blocks keyed by their zero-based old offset."""
    changes = []
    current = None
    block = None
    old_left = new_left = offset = 0
    for line in patch.split("\n"):
        if old_left or new_left:
            marker, content = line[:1], line[1:]
            if marker == "\\":
                continue
            if marker == " ":
                block = None
            elif marker in ("-", "+"):
                if block is None:
                    block = (offset, [], [])
                    current.edits.append(block)
                block[1 if marker == "-" else 2].append(content)
            else:
                raise ValueError("Invalid hunk line")
            if marker != "+":
                offset += 1
                old_left -= 1
            if marker != "-":
                new_left -= 1
            if old_left < 0 or new_left < 0:
                raise ValueError("Invalid hunk size")
            continue
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match:
                raise ValueError("Unsupported path")
            old, new = match.groups()
            path = PurePosixPath(old)
            if path.is_absolute() or ".." in path.parts or "\\" in old:
                raise ValueError("Unsupported path")
            current = Change(old, structural=old != new)
            changes.append(current)
        elif current is not None:
            if match := HUNK.match(line):
                start, old_count, _, new_count = match.groups()
                old_left = int(old_count or "1")
                new_left = int(new_count or "1")
                offset = int(start) - bool(old_left)
                block = None
            elif line.startswith(
                ("new file", "deleted file", "old mode", "GIT binary", "Binary files")
            ):
                current.structural = True
    if not changes or old_left or new_left:
        raise ValueError("Incomplete patch")
    return changes


def schedule(selected, durations):
    jobs = sorted(selected, key=lambda test: (-durations[test], test))
    if not jobs:
        return []
    total = sum(durations[test] for test in jobs)
    best = [jobs]
    cost = total + 2_000
    for count in range(2, min(8, len(jobs)) + 1):
        lower_bound = max(durations[jobs[0]], (total + count - 1) // count)
        if lower_bound + count * 2_000 >= cost:
            continue
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for test in jobs:
            worker = min(range(count), key=loads.__getitem__)
            loads[worker] += durations[test]
            workers[worker].append(test)
        candidate_cost = max(loads) + count * 2_000
        if candidate_cost < cost:
            best, cost = workers, candidate_cost
    return best


class Selector:
    def __init__(self, tests, coverage, source, comparison_profiles=None):
        self.all_tests = {test["number"] for test in tests}
        self.number_by_id = {test["id"]: test["number"] for test in tests}
        self.numbers = [self.number_by_id.get(nodeid) for nodeid in coverage["nodeids"]]
        self.unmapped = self.all_tests.difference(self.numbers)
        self.test_files = {}
        for test in tests:
            self.test_files.setdefault(test["file"], set()).add(test["number"])
        self.files = coverage["files"]
        self.imports = coverage["imports"]
        self.fixtures = coverage.get("fixtures", {})
        self.source_reads = coverage.get("source_reads", {})
        self.fixture_imports = set(coverage.get("fixture_imports", []))
        self.source = source
        self.comparison_profiles = comparison_profiles or {}
        self.method_coverage = {}

    def select(self, patch):
        try:
            changes = read_patch(patch)
            return set().union(
                *(
                    self.select_file(change, single_file=len(changes) == 1)
                    | self.translate(self.source_reads.get(change.path, ()))
                    for change in changes
                )
            )
        except (OSError, SyntaxError, ValueError):
            return self.all_tests

    def translate(self, indices):
        return {
            self.numbers[index] for index in indices if self.numbers[index] is not None
        }

    def select_file(self, change, *, single_file):
        path = change.path
        if change.structural or not change.edits:
            return self.all_tests
        if path in self.test_files:
            return self.test_files[path] | self.translate(self.module_users(path))
        if path in self.fixtures and path not in self.fixture_imports:
            return self.translate(self.fixtures[path])
        if path not in self.files:
            return self.all_tests

        old_lines = (self.source / path).read_text(encoding="utf-8").split("\n")
        new_lines = []
        cursor = 0
        for start, removed, added in change.edits:
            if start < cursor or old_lines[start : start + len(removed)] != removed:
                raise ValueError("Patch does not match baseline")
            new_lines.extend(old_lines[cursor:start])
            new_lines.extend(added)
            cursor = start + len(removed)
        new_lines.extend(old_lines[cursor:])
        old = ast.parse("\n".join(old_lines).encode(), type_comments=True)
        new_source = "\n".join(new_lines).encode()
        new = ast.parse(new_source, type_comments=True)
        # Parsing alone accepts statements that Python can reject on import.
        compile(new, path, "exec", dont_inherit=True)
        if ast.dump(old) == ast.dump(new):
            return set()
        if self.initialization_effects(old) != self.initialization_effects(new):
            factories = self.closure_factories(old)
            if not factories or self.initialization_effects(
                old, factories
            ) != self.initialization_effects(new, factories):
                return self.all_tests

        equivalent = set()
        if single_file and path in self.comparison_profiles:
            equivalent = self.equivalent_tests(old, new, self.comparison_profiles[path])

        if single_file and (name := self.changed_scalar_attribute(change, old, new)):
            readers = self.attribute_readers(name)
            if readers:
                return self.translate(readers) | self.unmapped

        coverage = self.files[path]
        import_lines = set(self.imports.get(path, []))
        functions = [node for node in ast.walk(old) if isinstance(node, FUNCTION)]
        statements = [node for node in ast.walk(old) if isinstance(node, ast.stmt)]
        boolean_expressions = [
            node for node in ast.walk(old) if isinstance(node, (ast.BoolOp, ast.IfExp))
        ]
        changed_methods = self.class_methods(old) ^ self.class_methods(new)
        selected = set().union(
            *(self.method_users(name) for _, name in changed_methods)
        )
        for offset, removed, added in change.edits:
            lines = range(offset + 1, offset + len(removed) + 1)
            if not removed:
                lines = (offset, offset + 1)
            for line in lines:
                selected.update(
                    self.covered_line(
                        path, line, coverage, import_lines, functions, statements
                    )
                )
                if len(removed) != len(added) or not single_file:
                    selected.update(self.scope_users(line, coverage, functions))
                for expression in boolean_expressions:
                    if expression.lineno < line <= expression.end_lineno:
                        # An edited operand may execute on a previously short-circuited path.
                        selected.update(
                            self.covered_range(
                                coverage, expression.lineno, expression.end_lineno
                            )
                        )
        return (self.translate(selected) - equivalent) | self.unmapped

    @staticmethod
    def initialization_effects(tree, factories=frozenset()):
        """Collect expressions capable of failing before a test reaches the module."""
        pending = [tree]
        effects = [
            ("definition", node.name)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        while pending:
            node = pending.pop()
            if isinstance(node, FUNCTION):
                pending.append(node.args)
                if not isinstance(node, ast.Lambda):
                    pending.extend(node.decorator_list)
                    if node.returns is not None:
                        pending.append(node.returns)
                continue
            if isinstance(node, ast.ClassDef):
                effects.append(("class", node.name))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in factories
                and all(isinstance(arg, ast.Constant) for arg in node.args)
                and all(
                    kw.arg is not None and isinstance(kw.value, ast.Constant)
                    for kw in node.keywords
                )
            ):
                # This call only binds literal inputs for execution later.
                effects.append(
                    (
                        node.func.id,
                        len(node.args),
                        tuple(kw.arg for kw in node.keywords),
                    )
                )
            elif isinstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.Call,
                    ast.Attribute,
                    ast.Subscript,
                    ast.BinOp,
                    ast.Assert,
                    ast.Raise,
                    ast.Name,
                ),
            ):
                effects.append(ast.dump(node))
            pending.extend(ast.iter_child_nodes(node))
        return effects

    def closure_factories(self, tree):
        """Resolve imported callbacks whose factory only creates a closure."""
        call_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and (node.args or node.keywords)
            and all(isinstance(arg, ast.Constant) for arg in node.args)
            and all(isinstance(kw.value, ast.Constant) for kw in node.keywords)
        }
        factories = set()
        for statement in tree.body:
            if not (
                isinstance(statement, ast.ImportFrom)
                and statement.level == 0
                and statement.module
                and statement.module.startswith("sqlglot.")
            ):
                continue
            names = {
                alias.name: alias.asname or alias.name
                for alias in statement.names
                if (alias.asname or alias.name) in call_names
            }
            if not names:
                continue
            source = self.source.joinpath(*statement.module.split(".")).with_suffix(
                ".py"
            )
            if not source.is_file():
                continue
            module = ast.parse(source.read_bytes())
            postponed_annotations = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
                for node in module.body
            )
            if postponed_annotations:
                factories.update(
                    names[node.name]
                    for node in module.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name in names
                    and self.is_closure_factory(node)
                )
        return factories

    @staticmethod
    def is_closure_factory(node):
        if node.decorator_list or len(node.body) != 2:
            return False
        callback, result = node.body
        args = node.args
        return (
            args.vararg is not None
            and args.kwarg is not None
            and not (args.posonlyargs or args.args or args.kwonlyargs)
            and isinstance(callback, ast.FunctionDef)
            and not (callback.decorator_list or callback.args.defaults)
            and all(default is None for default in callback.args.kw_defaults)
            and isinstance(result, ast.Return)
            and isinstance(result.value, ast.Name)
            and result.value.id == callback.name
        )

    @staticmethod
    def class_methods(tree):
        return {
            (owner.name, method.name)
            for owner in ast.walk(tree)
            if isinstance(owner, ast.ClassDef)
            for method in owner.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def method_users(self, name):
        # An added override can intercept a method that ran in a base module.
        if name not in self.method_coverage:
            users = set()
            pattern = re.compile(rf"\bdef\s+{re.escape(name)}\s*\(")
            for path, coverage in self.files.items():
                if not path.startswith("sqlglot/") or not path.endswith(".py"):
                    continue
                source = (self.source / path).read_text(encoding="utf-8")
                if not pattern.search(source):
                    continue
                for node in ast.walk(ast.parse(source)):
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == name
                    ):
                        users.update(
                            self.covered_range(coverage, node.lineno, node.end_lineno)
                        )
            self.method_coverage[name] = users
        return self.method_coverage[name]

    def scope_users(self, line, coverage, functions):
        scopes = [node for node in functions if node.lineno < line <= node.end_lineno]
        if not scopes:
            return set()
        scope = min(scopes, key=lambda node: node.end_lineno - node.lineno)
        return self.covered_range(coverage, scope.lineno + 1, scope.end_lineno)

    def equivalent_tests(self, old, new, profiles):
        # Only use observations if the entire AST differs by one comparison operator.
        swaps = []
        for before, after in zip(ast.walk(old), ast.walk(new)):
            if not (
                isinstance(before, ast.Compare)
                and isinstance(after, ast.Compare)
                and len(before.ops) == len(after.ops) == 1
            ):
                continue
            operators = type(before.ops[0]), type(after.ops[0])
            if operators in {(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)}:
                swaps.append(
                    (after, before.ops[0], f"{before.lineno}:{before.col_offset}")
                )
        if len(swaps) != 1:
            return set()
        comparison, old_operator, site = swaps[0]
        new_operator = comparison.ops[0]
        comparison.ops[0] = old_operator
        only_operator_changed = ast.dump(old) == ast.dump(new)
        comparison.ops[0] = new_operator
        if not only_operator_changed:
            return set()
        return {
            self.number_by_id[nodeid]
            for nodeid in profiles.get(site, [])
            if nodeid in self.number_by_id
        }

    def covered_line(self, path, line, coverage, import_lines, functions, statements):
        containing = [
            node for node in functions if node.lineno < line <= node.end_lineno
        ]
        if containing and line not in import_lines:
            direct = coverage.get(str(line), [])
            if direct:
                return direct
            scope = min(containing, key=lambda node: node.end_lineno - node.lineno)
            statement = min(
                (node for node in statements if node.lineno <= line <= node.end_lineno),
                key=lambda node: node.end_lineno - node.lineno,
                default=scope,
            )
            statement_hits = coverage.get(str(statement.lineno), [])
            if statement_hits and statement.lineno != scope.lineno:
                return statement_hits
            return self.scope_users(line, coverage, functions) or self.module_users(
                path
            )

        return self.module_users(path)

    @staticmethod
    def covered_range(coverage, first, last):
        return set().union(
            *(coverage.get(str(line), []) for line in range(first, last + 1))
        )

    @staticmethod
    def changed_scalar_attribute(change, old, new):
        if len(change.edits) != 1:
            return None
        start, removed, added = change.edits[0]
        if len(removed) != 1 or len(added) != 1:
            return None

        def assignment(tree):
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and item.lineno == item.end_lineno == start + 1
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                    ):
                        return item
            return None

        before, after = assignment(old), assignment(new)
        if before is None or after is None:
            return None
        try:
            old_value = ast.literal_eval(before.value)
            new_value = ast.literal_eval(after.value)
        except (ValueError, SyntaxError):
            return None
        if type(old_value) is not type(new_value) or type(old_value) not in (
            str,
            bytes,
            int,
            float,
            bool,
            type(None),
        ):
            return None
        after.value = before.value
        return before.targets[0].id if ast.dump(old) == ast.dump(new) else None

    def attribute_readers(self, name):
        readers = set()
        for path, coverage in self.files.items():
            if not path.endswith(".py"):
                continue
            text = (self.source / path).read_text(encoding="utf-8")
            if name not in text:
                continue
            import_lines = set(self.imports.get(path, []))
            tree = ast.parse(text)
            runtime_scopes = [
                node for node in ast.walk(tree) if isinstance(node, FUNCTION)
            ]
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == name:
                    # A string reference can be a dynamic lookup.
                    return set()
                symbol = None
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    symbol = node.attr
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    symbol = node.id
                if symbol == name:
                    if node.lineno in import_lines or not any(
                        scope.lineno < node.lineno <= scope.end_lineno
                        for scope in runtime_scopes
                    ):
                        return set()
                    readers.update(coverage.get(str(node.lineno), []))
        return readers

    def module_users(self, path):
        parts = PurePosixPath(path).parts
        files = [path]
        if (
            len(parts) == 3
            and parts[1] in DIALECT_PACKAGES
            and parts[2] not in {"dialect.py", "__init__.py"}
        ):
            files = [f"sqlglot/{package}/{parts[2]}" for package in DIALECT_PACKAGES]
        users = {
            index
            for file in files
            for indices in self.files.get(file, {}).values()
            for index in indices
        }
        return users or range(len(self.numbers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--coverage", type=Path, default=Path("/app/coverage.json.gz"))
    parser.add_argument(
        "--comparisons",
        type=Path,
        default=Path(__file__).with_name("comparison_profiles.json.gz"),
    )
    parser.add_argument("--source", type=Path, default=Path("/app/sqlglot"))
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    durations = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    with gzip.open(args.coverage, "rt", encoding="utf-8") as stream:
        coverage = json.load(stream)
    comparisons = {}
    if args.comparisons.is_file():
        with gzip.open(args.comparisons, "rt", encoding="utf-8") as stream:
            comparisons = json.load(stream)["safe"]
    selector = Selector(tests, coverage, args.source, comparisons)
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
        print(
            json.dumps(
                {"commit": patch.stem, "workers": schedule(selected, durations)},
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    main()
