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
    assert store.diagnostics() == ("assets/" + "0" * 64,)
