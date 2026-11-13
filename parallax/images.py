"""Images in judge evidence: lifted out of the JSON as content parts, losslessly, and sized the provider's way.

A hosted browser session's screenshot is a base64 `data:image/...` string inside its steps. Sent as JSON text it costs
about 0.93 tokens per byte (2026-10-16: 2.07 MB of PNG text billed ~1.9M tokens); sent as an image it costs at most a
few thousand visual tokens. `split` replaces each such string with a stable reference and returns every distinct image
once (keyed by its SHA-256), so the judge receives the same bytes as images.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
import struct
from dataclasses import dataclass

from pydantic import JsonValue

DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/]+={0,2})")
REFERENCE = "image:sha256:"


@dataclass(frozen=True)
class Image:
    sha256: str
    media_type: str
    data_url: str
    width: int | None
    height: int | None

    @property
    def reference(self) -> str:
        return REFERENCE + self.sha256

    @property
    def base64_bytes(self) -> int:
        return len(self.data_url) - self.data_url.index(",") - 1


@dataclass(frozen=True)
class Vision:
    """A fallback model's documented image limits and visual-token rule.

    Claude (platform.claude.com/docs/en/build-with-claude/vision, checked 2026-10-16): JPEG, PNG, GIF and WebP; at most
    600 images a request on a 1M-context model; at most 8000x8000 px; above 20 images in one request, at most 2000 px a
    side; 10 MB of base64 an image on the Claude API but 5 MB on Amazon Bedrock and Google Cloud, which OpenRouter may
    route to, so the stricter limit holds. Claude 4.7 and later downscale an image past a 2576 px long edge or 4784
    visual tokens, and an image costs ceil(width / 28) x ceil(height / 28) visual tokens. No minimum size or limit on
    a request's total image data was verified, so neither is checked.

    DeepSeek V4.1 Flash (api-docs.deepseek.com/guides/vision, checked 2026-11-13): JPEG, PNG, GIF and WebP; at most 600
    images a request; at most 8192 px a side, 4096 px once a request has 15 or more images; 32 MiB an inline image and
    a 48 MiB request body. Two of its OpenRouter endpoints are stricter, measured 2026-11-13: DeepInfra refuses a data
    URL over 16 MiB, and Venice refuses an image under 64x64 px. DeepSeek publishes no token formula, only a cap of 1024
    an image. Measured the same day, its own serving billed 184 tokens for any image up to 544x544 px and up to 1001
    above, and Google Vertex billed 182 to 382 for the same images. `visual_tokens` is a floor for the window check, so
    every image counts at 182, the fewest any endpoint billed. The limits hold for the 1017 release: DeepSeek
    documents one set for V4.1 Flash, and its OpenRouter endpoints refuse an over-limit image themselves (checked
    2026-11-13).
    """

    max_images: int
    max_side: int
    many_images: int
    many_images_side: int
    max_base64_bytes: int
    min_side: int
    max_request_base64_bytes: int | None
    """All of a request's images together; None where no such limit was verified."""
    native_long_edge: int
    native_tokens: int
    patch: int


VISION: dict[str, Vision] = {
    "anthropic/claude-opus-5.5": Vision(
        max_images=600,
        max_side=8000,
        many_images=20,
        many_images_side=2000,
        max_base64_bytes=5 * 1024 * 1024,
        min_side=1,
        max_request_base64_bytes=None,
        native_long_edge=2576,
        native_tokens=4784,
        patch=28,
    ),
    "deepseek/deepseek-v4.1-flash-1017": Vision(
        max_images=600,
        max_side=8192,
        many_images=14,
        many_images_side=4096,
        max_base64_bytes=16 * 1024 * 1024 - len("data:image/jpeg;base64,"),
        min_side=64,
        max_request_base64_bytes=48 * 1024 * 1024,
        native_long_edge=8192,
        native_tokens=182,
        patch=1,
    ),
}


def _dimensions(raw: bytes, media_type: str) -> tuple[int, int] | None:
    """Width and height from the image's own header, or None when it cannot be read."""
    try:
        if media_type == "image/png" and raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
            return struct.unpack(">II", raw[16:24])
        if media_type == "image/gif" and raw[:4] == b"GIF8":
            return struct.unpack("<HH", raw[6:10])
        if media_type == "image/webp" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8X":
                return 1 + int.from_bytes(raw[24:27], "little"), 1 + int.from_bytes(raw[27:30], "little")
            if chunk == b"VP8L":
                bits = int.from_bytes(raw[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", raw[26:30])
                return width & 0x3FFF, height & 0x3FFF
        if media_type == "image/jpeg" and raw[:2] == b"\xff\xd8":
            at = 2
            while at + 9 < len(raw):
                if raw[at] != 0xFF:
                    return None
                marker = raw[at + 1]
                length = struct.unpack(">H", raw[at + 2 : at + 4])[0]
                if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                    height, width = struct.unpack(">HH", raw[at + 5 : at + 9])
                    return width, height
                at += 2 + length
    except struct.error:
        return None
    return None


def split(value: JsonValue) -> tuple[JsonValue, dict[str, Image]]:
    """`value` with every string that is wholly an image data URL replaced by its reference, and each distinct image
    once, in order of first appearance. Nothing else changes."""
    found: dict[str, Image] = {}

    def walk(item: JsonValue) -> JsonValue:
        if isinstance(item, str):
            match = DATA_URL.fullmatch(item)
            if match is None:
                return item
            try:
                raw = base64.b64decode(match.group(2), validate=True)
            except binascii.Error:
                return item
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in found:
                width, height = _dimensions(raw, match.group(1)) or (None, None)
                found[digest] = Image(digest, match.group(1), item, width, height)
            return found[digest].reference
        if isinstance(item, list):
            return [walk(part) for part in item]
        if isinstance(item, dict):
            return {key: walk(part) for key, part in item.items()}
        return item

    return walk(value), found


def visual_tokens(image: Image, vision: Vision) -> int:
    """The fewest visual tokens the provider can bill for the image after its own resizing; its cap when the size
    cannot be read."""
    if image.width is None or image.height is None:
        return vision.native_tokens
    scale = min(1.0, vision.native_long_edge / max(image.width, image.height))
    width, height = image.width * scale, image.height * scale
    tokens = math.ceil(width / vision.patch) * math.ceil(height / vision.patch)
    return min(tokens, vision.native_tokens)


def over_limits(images: dict[str, Image], vision: Vision) -> str | None:
    """Why these images break a documented limit, or None. Nothing is downscaled or dropped to fit; the request is
    not sent and the reason is the judgment's."""
    if len(images) > vision.max_images:
        return f"{len(images)} distinct images; the fallback accepts {vision.max_images} a request"
    total = sum(image.base64_bytes for image in images.values())
    if vision.max_request_base64_bytes is not None and total > vision.max_request_base64_bytes:
        return f"{total} base64 bytes of images; the fallback accepts {vision.max_request_base64_bytes} a request"
    for image in images.values():
        if image.base64_bytes > vision.max_base64_bytes:
            return f"image {image.sha256[:12]} is {image.base64_bytes} base64 bytes; the limit is {vision.max_base64_bytes}"
        side = max(image.width or 0, image.height or 0)
        if image.width is not None and image.height is not None and min(image.width, image.height) < vision.min_side:
            return (
                f"image {image.sha256[:12]} is {image.width}x{image.height} px; the minimum is {vision.min_side} a side"
            )
        if side > vision.max_side:
            return f"image {image.sha256[:12]} is {side} px on a side; the limit is {vision.max_side}"
        if len(images) > vision.many_images and side > vision.many_images_side:
            return (
                f"{len(images)} images with image {image.sha256[:12]} at {side} px; above {vision.many_images} images "
                f"the limit is {vision.many_images_side} px a side, and no image is downscaled to fit"
            )
    return None
