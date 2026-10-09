"""Open a mod file at a line in the user's own editor.

The `editor` setting is a command with two placeholders: {file} for the file and
{line} for the line. pdx-audit names no editor. With no setting, the app opens the
file with the system's default program for it. `suggestions` lists the editors it
finds on this machine, so the user can pick one in place of writing the command."""
import os
import platform
import re
import shlex
import shutil
import subprocess

PLACEHOLDERS = ("{file}", "{line}")

# (label, command names on the path, Windows install paths, command). {program} is the
# command name or the install path the search found. Editors that run only in a
# terminal are left out: the app starts the editor without one.
KNOWN = (
    ("VS Code", ("code",), (), "{program} --goto {file}:{line}"),
    ("VSCodium", ("codium",), (), "{program} --goto {file}:{line}"),
    ("Cursor", ("cursor",), (), "{program} --goto {file}:{line}"),
    ("Windsurf", ("windsurf",), (), "{program} --goto {file}:{line}"),
    ("Zed", ("zed",), (), "{program} {file}:{line}"),
    ("Sublime Text", ("subl",), ("C:/Program Files/Sublime Text/subl.exe",
                                 "C:/Program Files/Sublime Text 3/subl.exe"), "{program} {file}:{line}"),
    ("Notepad++", ("notepad++",), ("C:/Program Files/Notepad++/notepad++.exe",
                                   "C:/Program Files (x86)/Notepad++/notepad++.exe"), "{program} -n{line} {file}"),
    ("IntelliJ IDEA", ("idea",), (), "{program} --line {line} {file}"),
    ("PyCharm", ("pycharm",), (), "{program} --line {line} {file}"),
    ("Kate", ("kate",), (), "{program} --line {line} {file}"),
    ("gedit", ("gedit",), (), "{program} +{line} {file}"),
    ("gVim", ("gvim",), (), "{program} +{line} {file}"),
    ("Emacs", ("emacs",), (), "{program} +{line} {file}"),
    ("Notepad, at the top of the file", ("notepad.exe",), ("C:/Windows/System32/notepad.exe",),
     "{program} {file}"),
)


def _is_windows():
    return os.name == "nt"


def _is_wsl():
    return "microsoft" in platform.release().lower() or bool(os.environ.get("WSL_DISTRO_NAME"))


def _local(win_path):
    """A Windows install path as this machine reads it: as it is on Windows, under
    /mnt/<drive> in WSL."""
    drive, rest = win_path.split(":", 1)
    return f"/mnt/{drive.lower()}{rest}"


def _quoted(program):
    return f'"{program}"' if " " in program else program


def suggestions():
    """[(label, command)] for the editors found on this machine, in KNOWN order. A
    command that holds a path with spaces quotes the path and the file."""
    out = []
    for label, names, paths, template in KNOWN:
        program = next((n for n in names if shutil.which(n)), None)
        if program is None:
            places = paths if _is_windows() else [_local(p) for p in paths] if _is_wsl() else []
            program = next((p for p in places if os.path.isfile(p)), None)
        if program is None:
            continue
        command = template.replace("{program}", _quoted(program))
        if " " in program:
            command = command.replace(" {file}", ' "{file}"')
        out.append((label, command))
    return out


def _windows_path(path):
    """The Windows form of a WSL path, for a Windows program."""
    r = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else str(path)
_FIELD = re.compile(r"\{[^{}]*\}")


def _split(template, windows):
    """The arguments of a command. On Windows a backslash is a path separator, not an
    escape, so the split keeps it and drops only the quotes around an argument."""
    if not windows:
        return shlex.split(template)
    lex = shlex.shlex(template, posix=False)
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    for token in lex:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


def check(template, windows=None):
    """The command, stripped. Raises ValueError for a command that cannot run."""
    text = str(template).strip()
    if not text:
        raise ValueError("the editor command is empty.")
    for field in _FIELD.findall(text):
        if field not in PLACEHOLDERS:
            raise ValueError(f"{field} is not a placeholder. Use {{file}} and {{line}}.")
    if "{" in _FIELD.sub("", text) or "}" in _FIELD.sub("", text):
        raise ValueError("a brace is not closed. Use {file} and {line}.")
    try:
        _split(text, os.name == "nt" if windows is None else windows)
    except ValueError as e:
        raise ValueError(f"the editor command does not parse: {e}.") from None
    return text


def command(template, path, line, windows=None):
    """The argument list that opens `path` at `line` (line 1 when it is None). A
    command with no {file} gets the file as its last argument."""
    windows = os.name == "nt" if windows is None else windows
    args = _split(check(template, windows), windows)
    if not windows and args and args[0].lower().endswith(".exe") and _is_wsl():
        path = _windows_path(path)          # a Windows program reads Windows paths
    filled = [a.replace("{file}", str(path)).replace("{line}", str(line or 1)) for a in args]
    if not any("{file}" in a for a in args):
        filled.append(str(path))
    return filled


def launch(argv):
    """Start the editor and do not wait for it. Raises OSError when the program
    cannot start."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)

