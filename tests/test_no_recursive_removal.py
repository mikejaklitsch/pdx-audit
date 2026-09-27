"""Guard: no recursive removal anywhere in pdx-audit, and every single-file
delete goes through pdxaudit/safety.py:remove_file.

The scan parses each source file with `ast`, so names inside strings, comments
and docstrings (including the lists below) never trigger it. It is a tripwire
for ordinary code, not a sandbox: deliberately obfuscated calls can get past."""
import ast
from pathlib import Path

import pdx_utilities

ROOT = Path(__file__).resolve().parent.parent
# pdx_utilities is the shared package (../pdx-utilities), not a vendored copy;
# pdx-audit runs its code, so it is scanned too.
SHARED = Path(pdx_utilities.__file__).resolve().parent

BANNED_CALLS = {"rmtree", "removedirs", "rmdir", "TemporaryDirectory"}
BANNED_FROM_IMPORTS = {
    "shutil": {"rmtree"},
    "os": {"remove", "unlink", "rmdir", "removedirs", "system"},
    "tempfile": {"TemporaryDirectory"},
}
SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}
SHELL_REMOVERS = {"rm", "rmdir", "del", "rd", "erase"}

ALLOWED_FILE = "pdxaudit/safety.py"
ALLOWED_FUNCTION = "remove_file"


def _sources():
    """(path, display path) for every file the guard covers."""
    files = [*ROOT.glob("pdxaudit/*.py"), *ROOT.glob("tests/*.py"),
             ROOT / "pdx-audit"]
    out = [(f, f.relative_to(ROOT).as_posix()) for f in files if f.is_file()]
    out += [(f, f"pdx_utilities/{f.name}") for f in SHARED.glob("*.py")]
    return sorted(out)


def _first_word(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.split()
        return words[0] if words else ""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _first_word(node.elts[0])
    return None


def violations(source, rel):
    """(line, message) for each banned construct in `source`."""
    found = []
    tree = ast.parse(source)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.funcs = []

        def _allowed(self):
            return rel == ALLOWED_FILE and ALLOWED_FUNCTION in self.funcs

        def visit_FunctionDef(self, node):
            self.funcs.append(node.name)
            self.generic_visit(node)
            self.funcs.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node):
            banned = BANNED_FROM_IMPORTS.get(node.module or "", set())
            for alias in node.names:
                if alias.name in banned:
                    found.append((node.lineno, f"from {node.module} import {alias.name}"))
            self.generic_visit(node)

        def visit_Call(self, node):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            owner = f.value.id if (isinstance(f, ast.Attribute)
                                   and isinstance(f.value, ast.Name)) else None
            if name in BANNED_CALLS:
                found.append((node.lineno, f"call to {name}"))
            if name == "unlink" and not self._allowed():
                found.append((node.lineno, "unlink outside the removal helper"))
            if owner == "os" and name == "remove" and not self._allowed():
                found.append((node.lineno, "os.remove outside the removal helper"))
            if owner == "os" and name == "system":
                found.append((node.lineno, "os.system"))
            if name in SUBPROCESS_FUNCS and (owner in (None, "subprocess")) and node.args:
                if _first_word(node.args[0]) in SHELL_REMOVERS:
                    found.append((node.lineno, "subprocess call to a shell remover"))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def test_codebase_has_no_recursive_or_unguarded_removal():
    problems = []
    for fp, rel in _sources():
        for line, msg in violations(fp.read_text(encoding="utf-8"), rel):
            problems.append(f"{rel}:{line}: {msg}")
    assert not problems, "banned removal constructs:\n" + "\n".join(problems)


def test_guard_catches_each_banned_construct():
    samples = [
        "import shutil\nshutil.rmtree(p)\n",
        "from shutil import rmtree\n",
        "import os\nos.removedirs(p)\n",
        "import os\nos.rmdir(p)\n",
        "p.rmdir()\n",
        "import tempfile\ntempfile.TemporaryDirectory()\n",
        "from tempfile import TemporaryDirectory\n",
        "p.unlink()\n",
        "import os\nos.remove(p)\n",
        "import os\nos.unlink(p)\n",
        "import os\nos.system('anything')\n",
        "import subprocess\nsubprocess.run(['rm', '-r', p])\n",
        "import subprocess\nsubprocess.run('rm -r x', shell=True)\n",
    ]
    for src in samples:
        assert violations(src, "pdxaudit/other.py"), f"guard missed: {src!r}"


def test_guard_allows_helper_and_ordinary_code():
    helper = "import os\ndef remove_file(p, d, pat):\n    os.unlink(p)\n"
    assert violations(helper, "pdxaudit/safety.py") == []
    # the same call anywhere else is flagged
    assert violations(helper, "pdxaudit/store.py")
    # list.remove and names inside strings are not removals
    assert violations("items.remove(x)\nmsg = 'shutil.rmtree'\n", "pdxaudit/x.py") == []
