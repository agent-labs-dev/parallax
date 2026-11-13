"""Tracker: issues and their comments.

Tables:
- issues:   {id, title, body, status, assignee, labels[], created, updated, url}
- comments: {id, issue, author, body, created}
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, tool
from parallax.world.services.common import (
    Args,
    Email,
    Limit,
    Offset,
    get_str,
    json_list,
    now_iso,
    page,
    require,
    str_list,
    text_hit,
)

ISSUE_URL = "https://tracker.example/issues/{id}"


class Status(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SearchArgs(Args):
    query: str | None = Field(None, description="Words that must appear in the title or body")
    status: Status | None = None
    assignee: Email | None = None
    label: str | None = None
    limit: Limit = 50
    offset: Offset = 0


class IdArgs(Args):
    id: str = Field(description="Issue id")


class CreateArgs(Args):
    title: str = Field(min_length=1)
    body: str = ""
    assignee: Email | None = None
    labels: list[str] = Field([], description="e.g. bug, feature")


class UpdateArgs(Args):
    id: str = Field(description="Issue id")
    title: str | None = None
    body: str | None = None
    status: Status | None = None
    assignee: Email | None = None
    labels: list[str] | None = Field(None, description="Replaces the label list")


class CloseArgs(Args):
    id: str = Field(description="Issue id")
    comment: str | None = Field(None, description="Optional closing comment")


class CommentArgs(Args):
    issue: str = Field(description="Issue id")
    body: str = Field(min_length=1)


class TrackerService(Service):
    name: ClassVar[str] = "tracker"
    tables: ClassVar[tuple[str, ...]] = ("issues", "comments")

    @tool(mutates=False, description="Search issues.")
    async def search_issues(self, args: SearchArgs) -> JsonValue:
        rows = [
            i
            for i in self.world.table("tracker.issues").values()
            if text_hit(i, ("title", "body"), args.query)
            and (args.status is None or i.get("status") == args.status.value)
            and (args.assignee is None or str(i.get("assignee", "")).casefold() == args.assignee.casefold())
            and (args.label is None or args.label.casefold() in {lbl.casefold() for lbl in str_list(i, "labels")})
        ]
        chunk, total, next_offset = page(rows, args.offset, args.limit)
        return {"issues": json_list(chunk), "total": total, "next_offset": next_offset}

    @tool(mutates=False, description="Get an issue with its comments.")
    async def get_issue(self, args: IdArgs) -> JsonValue:
        issue = require(self.world, "tracker.issues", args.id, "issue")
        comments = [c for c in self.world.table("tracker.comments").values() if c.get("issue") == args.id]
        return {**issue, "comments": json_list(comments)}

    @tool(mutates=True, additive=True, description="File a new issue. Returns it with its link.")
    async def create_issue(self, args: CreateArgs) -> JsonValue:
        issue_id = self.world.new_id("ISS-")
        now = now_iso(self.world)
        row: JsonObject = {
            "id": issue_id,
            "title": args.title,
            "body": args.body,
            "status": Status.OPEN.value,
            "assignee": args.assignee,
            "labels": json_list(args.labels),
            "created": now,
            "updated": now,
            "url": ISSUE_URL.format(id=issue_id),
        }
        self.world.table("tracker.issues")[issue_id] = row
        return row

    @tool(mutates=True, description="Change an issue's title, body, status, assignee or labels.")
    async def update_issue(self, args: UpdateArgs) -> JsonValue:
        row = dict(require(self.world, "tracker.issues", args.id, "issue"))
        if args.title is not None:
            row["title"] = args.title
        if args.body is not None:
            row["body"] = args.body
        if args.status is not None:
            row["status"] = args.status.value
        if args.assignee is not None:
            row["assignee"] = args.assignee
        if args.labels is not None:
            row["labels"] = json_list(args.labels)
        row["updated"] = now_iso(self.world)
        self.world.table("tracker.issues")[args.id] = row
        return row

    @tool(mutates=True, description="Close an issue, optionally with a closing comment.")
    async def close_issue(self, args: CloseArgs) -> JsonValue:
        row = {**require(self.world, "tracker.issues", args.id, "issue"), "status": Status.CLOSED.value}
        row["updated"] = now_iso(self.world)
        self.world.table("tracker.issues")[args.id] = row
        if args.comment:
            self._comment(args.id, args.comment)
        return row

    @tool(mutates=True, additive=True, description="Comment on an issue.")
    async def comment(self, args: CommentArgs) -> JsonValue:
        require(self.world, "tracker.issues", args.issue, "issue")
        return self._comment(args.issue, args.body)

    def _comment(self, issue_id: str, body: str) -> JsonObject:
        row: JsonObject = {
            "id": self.world.new_id("cmt_"),
            "issue": issue_id,
            "author": self.world.persona.email,
            "body": body,
            "created": now_iso(self.world),
        }
        self.world.table("tracker.comments")[get_str(row, "id")] = row
        return row
