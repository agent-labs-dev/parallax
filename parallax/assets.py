"""Trial-owned binary evidence. Harnesses supply bytes, never source paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Bare MIME type/subtype tokens, as defined by the media-type grammar.
_MIME = re.compile(r"[!#$%&'\*+.^_`|~0-9A-Za-z-]+/[!#$%&'\*+.^_`|~0-9A-Za-z-]+")


class AssetError(ValueError):
    """Registration or retrieval could not preserve replayable evidence."""


class TrialAssets:
    def __init__(self, root: Path) -> None:
        self.root = root / "assets"
        self._diagnostics: list[str] = []
        self._registered: dict[str, str] = {}

    def _failure(self, message: str) -> AssetError:
        if message not in self._diagnostics:
            self._diagnostics.append(message)
        return AssetError(message)

    def register(self, data: bytes, mime: str, *, digest: str | None = None) -> str:
        if not isinstance(data, bytes) or not data:
            raise self._failure("asset data must be non-empty bytes")
        if _MIME.fullmatch(mime) is None:
            raise self._failure(f"invalid asset MIME type {mime!r}")
        actual = hashlib.sha256(data).hexdigest()
        if digest is not None and digest != actual:
            raise self._failure(f"asset digest mismatch: declared {digest}, computed {actual}")
        reference = f"assets/{actual}"
        path = self.root / actual
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if path.exists():
                _, found_mime = self.read(reference)
                if found_mime != mime:
                    raise self._failure(f"asset {reference} is already registered with another MIME type")
            else:
                path.write_bytes(data)
                path.with_suffix(".mime").write_text(mime + "\n", encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise self._failure(f"cannot retain asset {reference}: {exc}") from exc
        self._registered[actual] = mime
        return reference

    def validate(self) -> tuple[str, ...]:
        """Check retained content after harness cleanup and before the trial is exported."""
        for digest, mime in self._registered.items():
            reference = f"assets/{digest}"
            try:
                _, found_mime = self.read(reference)
                if found_mime != mime:
                    self._failure(f"retained asset {reference} MIME type changed")
            except AssetError:
                pass  # read recorded the failure, including when the harness swallowed an earlier error
        return self.diagnostics()

    def read(self, reference: str) -> tuple[bytes, str]:
        prefix, _, digest = reference.partition("/")
        if prefix != "assets" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise self._failure(f"invalid asset reference {reference!r}")
        path = self.root / digest
        mime_path = path.with_suffix(".mime")
        try:
            data = path.read_bytes()
            mime = mime_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise self._failure(f"missing retained asset {reference!r}") from exc
        except (OSError, UnicodeError) as exc:
            raise self._failure(f"unreadable retained asset {reference!r}: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise self._failure(f"corrupt retained asset {reference!r}")
        return data, mime

    def diagnostics(self) -> tuple[str, ...]:
        return tuple(self._diagnostics)
