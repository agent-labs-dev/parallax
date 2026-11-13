import pytest


@pytest.fixture
def exports(tmp_path):
    (tmp_path / "entries.csv").write_text("account,pence\nrent,120000\nfood,4550\nrent,5000\n", encoding="utf-8")
    return tmp_path
