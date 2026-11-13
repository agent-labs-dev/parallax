"""Web: a read-only, seeded web. Search ranks pages by BM25; fetch returns a seeded page or not_found.

Tables:
- pages: {id, url, title, text, published, images[]}, loaded by the world from the seed's `web` list
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.match import normalise
from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, Limit, get_str, json_list, snippet

K1 = 1.5
B = 0.75


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", normalise(text))


def bm25(query: str, docs: Sequence[str]) -> list[float]:
    terms = set(tokens(query))
    bags = [Counter(tokens(d)) for d in docs]
    lengths = [sum(b.values()) for b in bags]
    avg = (sum(lengths) / len(lengths)) if lengths else 0.0
    scores = []
    for bag, length in zip(bags, lengths, strict=True):
        score = 0.0
        for term in terms:
            df = sum(1 for b in bags if term in b)
            if not bag[term]:
                continue
            idf = math.log(1 + (len(bags) - df + 0.5) / (df + 0.5))
            score += idf * bag[term] * (K1 + 1) / (bag[term] + K1 * (1 - B + B * length / (avg or 1)))
        scores.append(score)
    return scores


class SearchArgs(Args):
    query: str = Field(min_length=1)
    limit: Limit = 10


class FetchArgs(Args):
    url: str = Field(description="Absolute URL of the page")


class WebService(Service):
    name: ClassVar[str] = "web"
    tables: ClassVar[tuple[str, ...]] = ("pages",)

    @tool(mutates=False, description="Search the web. Returns the best-matching pages with a snippet each.")
    async def search(self, args: SearchArgs) -> JsonValue:
        pages = list(self.world.table("web.pages").values())
        scores = bm25(args.query, [f"{get_str(p, 'title')} {get_str(p, 'text')}" for p in pages])
        # sorted() is stable, so equal scores keep seed order and results are deterministic.
        ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])
        hits = [pages[i] for i in ranked[: args.limit]]
        return {"results": json_list(_result(p) for p in hits)}

    @tool(mutates=False, description="Fetch a web page's text by URL.")
    async def fetch(self, args: FetchArgs) -> JsonValue:
        wanted = _canonical(args.url)
        for page in self.world.table("web.pages").values():
            if _canonical(get_str(page, "url")) == wanted:
                return page
        raise ServiceError("not_found", f"could not fetch {args.url}: page not found")


def _canonical(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/").casefold()


def _result(p: JsonObject) -> JsonObject:
    return {
        "url": p.get("url"),
        "title": p.get("title"),
        "published": p.get("published"),
        "snippet": snippet(get_str(p, "text")),
    }
