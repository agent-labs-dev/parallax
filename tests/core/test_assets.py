import hashlib

import pytest
from parallax.assets import AssetError, TrialAssets


def test_assets_are_content_addressed_and_deduplicated(tmp_path):
    store = TrialAssets(tmp_path)
    data = b"png bytes"
    ref = store.register(data, "image/png")
    assert ref == store.register(data, "image/png")
    assert store.read(ref) == (data, "image/png")


def test_registration_rejects_wrong_digest_or_mime(tmp_path):
    store = TrialAssets(tmp_path)
    digest = hashlib.sha256(b"png bytes").hexdigest()
    with pytest.raises(AssetError, match="digest mismatch"):
        store.register(b"other", "image/png", digest=digest)
    with pytest.raises(AssetError, match="MIME"):
        store.register(b"png bytes", "image png")


def test_missing_reference_is_diagnostic(tmp_path):
    store = TrialAssets(tmp_path)
    with pytest.raises(AssetError, match="missing retained asset"):
        store.read("assets/" + "0" * 64)
    assert "missing retained asset" in store.diagnostics()[0]


def test_validate_detects_cleanup_or_corruption_before_export(tmp_path):
    store = TrialAssets(tmp_path)
    ref = store.register(b"png bytes", "image/png")
    (tmp_path / ref).write_bytes(b"changed")
    assert any(ref in message and "corrupt" in message for message in store.validate())


@pytest.mark.parametrize("damage", ["missing", "non-utf8", "changed"])
def test_retained_mime_damage_is_diagnostic(tmp_path, damage):
    store = TrialAssets(tmp_path)
    ref = store.register(b"png bytes", "image/png")
    sidecar = (tmp_path / ref).with_suffix(".mime")
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.write_bytes(b"\xff" if damage == "non-utf8" else b"text/plain\n")
    with pytest.raises(AssetError):
        store.register(b"png bytes", "image/png")
    assert store.validate()


def test_write_failure_is_recorded_when_harness_catches_it(tmp_path, monkeypatch):
    store = TrialAssets(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("fixture denied")

    monkeypatch.setattr(type(tmp_path), "write_bytes", denied)
    with pytest.raises(AssetError, match="cannot retain asset"):
        store.register(b"png bytes", "image/png")
    assert "fixture denied" in store.validate()[0]


@pytest.mark.parametrize("mime", ["/", "image/", "image/\ud800", "image png"])
def test_registration_requires_mime_type_and_subtype(tmp_path, mime):
    store = TrialAssets(tmp_path)
    with pytest.raises(AssetError, match="MIME"):
        store.register(b"png bytes", mime)
    assert store.validate()
