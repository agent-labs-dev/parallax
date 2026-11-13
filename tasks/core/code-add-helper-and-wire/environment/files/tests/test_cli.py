from jotter.cli import main


def test_list_shows_titles(tmp_path, capsys):
    (tmp_path / "a.md").write_text("# Alpha note\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Beta note with a very long title indeed\n", encoding="utf-8")
    assert main(["--dir", str(tmp_path), "list", "--width", "12"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["a.md\tAlpha note", "b.md\tBeta note w…"]


def test_new_writes_heading(tmp_path, capsys):
    assert main(["--dir", str(tmp_path), "new", "Groceries"]) == 0
    assert (tmp_path / "groceries.md").read_text(encoding="utf-8") == "# Groceries\n"
