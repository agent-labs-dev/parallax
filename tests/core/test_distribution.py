"""Both distribution formats retain runtime data and its notices, without repository branding."""

import hashlib
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_distributions_keep_runtime_data_and_matching_notices(tmp_path: Path) -> None:
    # uv builds the wheel from the sdist, so this also checks an installer's source-build path.
    subprocess.run(["uv", "build", "--out-dir", str(tmp_path), str(ROOT)], check=True, capture_output=True)
    with tarfile.open(next(tmp_path.glob("*.tar.gz"))) as archive:
        source = {}
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                source[member.name.split("/", 1)[1]] = stream.read()
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as archive:
        wheel = {name: archive.read(name) for name in archive.namelist()}

    assert not any(name.startswith("assets/") for name in source)
    assert not any(name.startswith("assets/") for name in wheel)
    for name in ("LICENSE", "LICENSE-DATA", "NOTICE", "THIRD_PARTY.md"):
        assert source[name] == (ROOT / name).read_bytes()
        wheel_name = next(path for path in wheel if path.endswith(f".dist-info/licenses/{name}"))
        assert wheel[wheel_name] == source[name]
    notice = source["NOTICE"].decode()
    assert "ATIF" not in notice
    assert "assets/" not in notice
    # The provenance the notices name ships beside them, so an archive is complete without the repository.
    for name in ("NOTICE", "LICENSE-DATA"):
        assert b"THIRD_PARTY.md" in source[name] and b"github.com" not in source[name]
    metadata = next(content for name, content in wheel.items() if name.endswith(".dist-info/METADATA"))
    for content in (source["PKG-INFO"], metadata):
        assert b"License-Expression: MIT AND CC-BY-4.0\n" in content

    # Task data is relocated into the package in a wheel; its content must survive unchanged.
    expected = {
        (f"parallax/{name}" if name.startswith("tasks/") else name): hashlib.sha256(content).hexdigest()
        for name, content in source.items()
        if name.startswith(("parallax/", "tasks/"))
    }
    actual = {
        name: hashlib.sha256(content).hexdigest() for name, content in wheel.items() if name.startswith("parallax/")
    }
    assert expected
    assert any(name.startswith("parallax/tasks/") for name in expected)
    assert actual == expected
