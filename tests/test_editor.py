"""Open in editor: the user writes the editor command, with {file} and {line} where
the editor wants them. pdx-audit names no editor of its own."""
import pytest

from pdxaudit import editor


def test_placeholders_fill_each_argument_and_a_path_with_spaces_stays_one():
    argv = editor.command("myedit --goto {file}:{line}", "/m/a b/x.txt", 12)
    assert argv == ["myedit", "--goto", "/m/a b/x.txt:12"]


def test_a_command_without_the_file_placeholder_gets_the_file_last():
    assert editor.command("myedit", "/m/x.txt", 3) == ["myedit", "/m/x.txt"]


def test_a_location_without_a_line_opens_at_line_one():
    assert editor.command("ed +{line} {file}", "/m/x.txt", None) == ["ed", "+1", "/m/x.txt"]


def test_a_quoted_windows_program_path_is_one_argument():
    argv = editor.command('"C:\\Program Files\\Ed\\ed.exe" -n{line} "{file}"', "C:\\m\\x.txt", 7,
                          windows=True)
    assert argv == ["C:\\Program Files\\Ed\\ed.exe", "-n7", "C:\\m\\x.txt"]


@pytest.mark.parametrize("bad", ["myedit {path}", "myedit {file", '"myedit {file}', "  "])
def test_a_command_that_cannot_run_is_refused(bad):
    with pytest.raises(ValueError):
        editor.check(bad)



# --- suggestions: the editors installed on this machine --------------------------

def _machine(monkeypatch, on_path=(), files=(), wsl=False, windows=False):
    monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}" if name in on_path else None)
    monkeypatch.setattr(editor.os.path, "isfile", lambda p: p in files)
    monkeypatch.setattr(editor, "_is_wsl", lambda: wsl)
    monkeypatch.setattr(editor, "_is_windows", lambda: windows)


def test_an_editor_on_the_path_is_suggested_by_its_command_name(monkeypatch):
    _machine(monkeypatch, on_path={"code"})
    assert ("VS Code", "code --goto {file}:{line}") in editor.suggestions()


def test_a_windows_editor_is_found_at_its_install_folder_from_wsl(monkeypatch):
    npp = "/mnt/c/Program Files/Notepad++/notepad++.exe"
    _machine(monkeypatch, files={npp}, wsl=True)
    assert ("Notepad++", f'"{npp}" -n{{line}} "{{file}}"') in editor.suggestions()


def test_a_windows_editor_is_found_at_its_install_folder_on_windows(monkeypatch):
    npp = "C:/Program Files/Notepad++/notepad++.exe"
    _machine(monkeypatch, files={npp}, windows=True)
    assert ("Notepad++", f'"{npp}" -n{{line}} "{{file}}"') in editor.suggestions()


def test_nothing_installed_suggests_nothing(monkeypatch):
    _machine(monkeypatch)
    assert editor.suggestions() == []


def test_every_suggestion_passes_the_check(monkeypatch):
    names = {n for _label, names, _paths, _tpl in editor.KNOWN for n in names}
    paths = {editor._local(p) for _label, _names, ps, _tpl in editor.KNOWN for p in ps}
    _machine(monkeypatch, on_path=names, files=paths, wsl=True)
    for _label, template in editor.suggestions():
        editor.check(template, windows=False)


def test_a_windows_program_from_wsl_gets_a_windows_path(monkeypatch):
    monkeypatch.setattr(editor, "_is_wsl", lambda: True)
    monkeypatch.setattr(editor, "_windows_path", lambda p: "C:\\m\\x.txt")
    argv = editor.command('"/mnt/c/Ed/ed.exe" -n{line} "{file}"', "/mnt/c/m/x.txt", 4, windows=False)
    assert argv == ["/mnt/c/Ed/ed.exe", "-n4", "C:\\m\\x.txt"]


def test_a_linux_program_from_wsl_keeps_the_linux_path(monkeypatch):
    monkeypatch.setattr(editor, "_is_wsl", lambda: True)
    assert editor.command("code --goto {file}:{line}", "/mnt/c/m/x.txt", 4, windows=False) == \
        ["code", "--goto", "/mnt/c/m/x.txt:4"]
