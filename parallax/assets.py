from __future__ import annotations

import hashlib
from pathlib import Path


class AssetError(ValueError):
    pass


class TrialAssets:
    def __init__(self, root: Path) -> None:
        self.root = root / "assets"
        self.root.mkdir(parents=True, exist_ok=True)
        self._diagnostics: list[str] = []
        self._registered: dict[str, str] = {}

    def register(self, data: bytes, mime: str, *, digest: str | None = None) -> str:
        if not isinstance(data, bytes) or not data:
            self._diagnostics.append("invalid:data")
            raise AssetError("asset data must be non-empty bytes")
        if not mime or "/" not in mime or any(c.isspace() for c in mime):
            self._diagnostics.append("invalid:mime")
            raise AssetError(f"invalid asset MIME type {mime!r}")
        actual = hashlib.sha256(data).hexdigest()
        if digest is not None and digest != actual:
            self._diagnostics.append(f"digest:{digest}")
            raise AssetError(f"asset digest mismatch: declared {digest}, computed {actual}")
        path = self.root / actual
        mime_path = path.with_suffix(".mime")
        if not path.exists():
            path.write_bytes(data)
            mime_path.write_text(mime + "\n")
        elif mime_path.read_text().strip() != mime:
            self._diagnostics.append(f"mime:{actual}")
            raise AssetError(f"asset digest {actual} is already registered with another MIME type")
        self._registered[actual] = mime
        return f"assets/{actual}"

    def validate(self) -> tuple[str, ...]:
        """Verify every registered asset is still present and byte-identical before export."""
        for digest, mime in self._registered.items():
            try:
                data, found_mime = self.read(f"assets/{digest}")
            except AssetError:
                continue
            if found_mime != mime or hashlib.sha256(data).hexdigest() != digest:
                self._diagnostics.append(f"assets/{digest}")
        return self.diagnostics()

    def read(self, reference: str) -> tuple[bytes, str]:
        prefix, _, digest = reference.partition("/")
        if prefix != "assets" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            self._diagnostics.append(reference)
            raise AssetError(f"invalid asset reference {reference!r}")
        path = self.root / digest
        mime_path = path.with_suffix(".mime")
        if not path.is_file() or not mime_path.is_file():
            self._diagnostics.append(reference)
            raise AssetError(f"missing retained asset {reference!r}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            self._diagnostics.append(reference)
            raise AssetError(f"corrupt retained asset {reference!r}")
        return data, mime_path.read_text().strip()

    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)
