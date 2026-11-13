"""Wiki: team documentation pages and comments.

Tables:
- pages:    {id, title, body, space, parent, author, created, updated, url}
- comments: {id, page, author, body, mentions[], created}
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, Email, Limit, get_str, json_list, now_iso, require, snippet, text_hit

PAGE_URL = "https://wiki.example/pages/{id}"


class SearchArgs(Args):
    query: str | None = Field(None, description="Words that must appear in the title or body")
    space: str | None = None
    limit: Limit = 25


class IdArgs(Args):
    id: str = Field(description="Page id")


class CreateArgs(Args):
    title: str = Field(min_length=1)
    body: str = ""
    space: str | None = Field(None, description="Space (section) name")
    parent: str | None = Field(None, description="Parent page id")


class UpdateArgs(Args):
    id: str = Field(description="Page id")
    title: str | None = None
    body: str | None = Field(None, description="Replaces the whole body")
    append: str | None = Field(None, description="Text appended to the end of the body")


class CommentArgs(Args):
    page: str = Field(description="Page id")
    body: str = Field(min_length=1)
    mentions: list[Email] = Field([], description="People to notify")


class WikiService(Service):
    name: ClassVar[str] = "wiki"
    tables: ClassVar[tuple[str, ...]] = ("pages", "comments")

    @tool(mutates=False, description="Search documentation pages. Returns summaries with links.")
    async def search(self, args: SearchArgs) -> JsonValue:
        rows = [
            p
            for p in self.world.table("wiki.pages").values()
            if text_hit(p, ("title", "body"), args.query) and args.space in (None, p.get("space"))
        ]
        return {"pages": json_list(_summary(p) for p in rows[: args.limit])}

    @tool(mutates=False, description="Read a page with its comments.")
    async def get_page(self, args: IdArgs) -> JsonValue:
        page = require(self.world, "wiki.pages", args.id, "page")
        comments = [c for c in self.world.table("wiki.comments").values() if c.get("page") == args.id]
        return {**page, "comments": json_list(comments)}

    @tool(mutates=True, additive=True, description="Create a page. Returns it with its link.")
    async def create_page(self, args: CreateArgs) -> JsonValue:
        if args.parent is not None:
            require(self.world, "wiki.pages", args.parent, "page")
        page_id = self.world.new_id("page_")
        now = now_iso(self.world)
        row: JsonObject = {
            "id": page_id,
            "title": args.title,
            "body": args.body,
            "space": args.space,
            "parent": args.parent,
            "author": self.world.persona.email,
            "created": now,
            "updated": now,
            "url": PAGE_URL.format(id=page_id),
        }
        self.world.table("wiki.pages")[page_id] = row
        return row

    @tool(mutates=True, description="Edit a page: new title, replaced body, or text appended to the body.")
    async def update_page(self, args: UpdateArgs) -> JsonValue:
        if args.body is not None and args.append is not None:
            raise ServiceError("validation", "body, append: pass one or the other")
        row = dict(require(self.world, "wiki.pages", args.id, "page"))
        if args.title is not None:
            row["title"] = args.title
        if args.body is not None:
            row["body"] = args.body
        if args.append is not None:
            row["body"] = get_str(row, "body") + args.append
        row["updated"] = now_iso(self.world)
        self.world.table("wiki.pages")[args.id] = row
        return row

    @tool(mutates=True, additive=True, description="Comment on a page, optionally mentioning people.")
    async def comment(self, args: CommentArgs) -> JsonValue:
        require(self.world, "wiki.pages", args.page, "page")
        row: JsonObject = {
            "id": self.world.new_id("wcmt_"),
            "page": args.page,
            "author": self.world.persona.email,
            "body": args.body,
            "mentions": json_list(args.mentions),
            "created": now_iso(self.world),
        }
        self.world.table("wiki.comments")[get_str(row, "id")] = row
        return row


def _summary(p: JsonObject) -> JsonObject:
    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "space": p.get("space"),
        "url": p.get("url"),
        "updated": p.get("updated"),
        "snippet": snippet(get_str(p, "body")),
    }
