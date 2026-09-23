"""Static check: no function in the daemon package references an unbound global.

Regression guard for a bug that hid for weeks: handlers.py passed
`session_rec.session_id` to honcho_client.submit_turn after a refactor had
moved `session_rec` into agent_runtime. The NameError was swallowed by a bare
`except Exception: pass`, so every Discord mention silently skipped the Honcho
write while the daemon reported nothing.

This uses the stdlib symbol table on the source text, so it needs neither
discord.py nor an importable package. For each function, every name the
compiler resolves as global must be bound at module level (import, assignment,
def, class) or be a builtin.
"""

from __future__ import annotations

import builtins
import symtable
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}


def _module_bound_names(table: symtable.SymbolTable) -> set[str]:
    bound = set()
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            bound.add(sym.get_name())
    return bound


def _walk_functions(table: symtable.SymbolTable):
    for child in table.get_children():
        if child.get_type() == "function":
            yield child
        yield from _walk_functions(child)


def unbound_globals(source: str, filename: str) -> list[tuple[str, str, int]]:
    top = symtable.symtable(source, filename, "exec")
    bound = _module_bound_names(top)
    problems = []
    for fn in _walk_functions(top):
        for sym in fn.get_symbols():
            if sym.is_global() and not sym.is_assigned():
                name = sym.get_name()
                if name not in bound and name not in BUILTINS:
                    problems.append((fn.get_name(), name, fn.get_lineno()))
    return problems


class TestNoUnboundGlobals(unittest.TestCase):
    def test_daemon_modules_reference_only_bound_globals(self):
        modules = sorted(
            p for p in PKG_DIR.glob("*.py")
            if not p.name.startswith("test_") and p.name != "__init__.py"
        )
        self.assertTrue(modules)
        for path in modules:
            with self.subTest(module=path.name):
                problems = unbound_globals(path.read_text(encoding="utf-8"), path.name)
                self.assertEqual(
                    problems, [],
                    f"{path.name}: functions reference names never bound at module "
                    f"level (function, name, line): {problems}",
                )

    def test_detects_the_original_bug_shape(self):
        src = (
            "def outer():\n"
            "    try:\n"
            "        use(session_rec.session_id)\n"
            "    except Exception:\n"
            "        pass\n"
        )
        problems = unbound_globals(src, "synthetic.py")
        names = {name for _, name, _ in problems}
        self.assertIn("session_rec", names)
        self.assertIn("use", names)


if __name__ == "__main__":
    unittest.main()
