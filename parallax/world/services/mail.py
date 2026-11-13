"""Mail: an inbox with threads, drafts, sent mail and labels.

Tables (rows are what checks read):
- messages: {id, thread, from, to[], cc[], subject, body, date, labels[]}; `inbox` in labels = not archived
- drafts:   {id, thread, to[], cc[], bcc[], subject, body, in_reply_to, updated}
- sent:     {id, thread, from, to[], cc[], bcc[], subject, body, in_reply_to, date, draft}
- labels:   {id, name}: user labels; the system labels below always exist
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import (
    Args,
    Email,
    Limit,
    get_str,
    json_list,
    now_iso,
    opt_str,
    require,
    snippet,
    str_list,
    text_hit,
)

SYSTEM_LABELS = frozenset({"inbox", "starred", "important", "spam", "trash"})


class SearchArgs(Args):
    query: str | None = Field(None, description="Words that must all appear in sender, recipients, subject or body")
    label: str | None = Field(None, description="Only messages carrying this label, e.g. inbox")
    sender: str | None = Field(None, description="Substring of the sender address, e.g. a domain")
    limit: Limit = 25


class SentSearchArgs(Args):
    query: str | None = Field(None, description="Words that must all appear in recipients, subject or body")
    recipient: str | None = Field(None, description="Substring of a to/cc/bcc address, e.g. a name or domain")
    limit: Limit = 25


class IdArgs(Args):
    id: str = Field(description="Message id")


class Compose(Args):
    to: list[Email] = Field(min_length=1, description="Recipients")
    cc: list[Email] = []
    bcc: list[Email] = []
    subject: str | None = Field(None, description="Defaults to 'Re: <original subject>' when replying")
    body: str
    in_reply_to: str | None = Field(None, description="Message id being replied to; keeps the thread")


class DraftUpdateArgs(Args):
    id: str = Field(description="Draft id")
    to: list[Email] | None = None
    cc: list[Email] | None = None
    bcc: list[Email] | None = None
    subject: str | None = None
    body: str | None = None


class DraftIdArgs(Args):
    id: str = Field(description="Draft id")


class ArchiveArgs(Args):
    ids: list[str] = Field(min_length=1, description="Message ids to remove from the inbox")


class LabelArgs(Args):
    ids: list[str] = Field(min_length=1, description="Message ids")
    add: list[str] = []
    remove: list[str] = []


class MailService(Service):
    name: ClassVar[str] = "mail"
    tables: ClassVar[tuple[str, ...]] = ("messages", "drafts", "sent", "labels")

    @tool(mutates=False, description="Search the user's mailbox. Returns message summaries, newest first.")
    async def search(self, args: SearchArgs) -> JsonValue:
        rows = [
            m
            for m in self.world.table("mail.messages").values()
            if text_hit(m, ("from", "to", "cc", "subject", "body"), args.query)
            and (args.label is None or args.label.casefold() in {lbl.casefold() for lbl in str_list(m, "labels")})
            and (args.sender is None or args.sender.casefold() in get_str(m, "from").casefold())
        ]
        rows.sort(key=lambda m: get_str(m, "date"), reverse=True)
        return {"messages": json_list(_summary(m) for m in rows[: args.limit])}

    @tool(mutates=False, description="Read one message in full.")
    async def get(self, args: IdArgs) -> JsonValue:
        return require(self.world, "mail.messages", args.id, "message")

    @tool(
        mutates=False,
        description="List mail the user has already sent, in full, newest first. Use it to confirm a send landed.",
    )
    async def list_sent(self, args: SentSearchArgs) -> JsonValue:
        rows = [
            m
            for m in self.world.table("mail.sent").values()
            if text_hit(m, ("to", "cc", "bcc", "subject", "body"), args.query)
            and (args.recipient is None or _addressed_to(m, args.recipient))
        ]
        rows.sort(key=lambda m: get_str(m, "date"), reverse=True)
        return {"sent": json_list(rows[: args.limit])}

    @tool(mutates=False, description="List unsent drafts.")
    async def list_drafts(self, args: Args) -> JsonValue:
        return {"drafts": json_list(self.world.table("mail.drafts").values())}

    @tool(mutates=True, additive=True, description="Save a draft without sending it.")
    async def create_draft(self, args: Compose) -> JsonValue:
        row = {"id": self.world.new_id("draft_"), **self._compose(args), "updated": now_iso(self.world)}
        self.world.table("mail.drafts")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Change fields of an existing draft; omitted fields are kept.")
    async def update_draft(self, args: DraftUpdateArgs) -> JsonValue:
        row = dict(require(self.world, "mail.drafts", args.id, "draft"))
        changes = args.model_dump(exclude={"id"}, exclude_none=True)
        row.update({k: json_list(v) if isinstance(v, list) else v for k, v in changes.items()})
        row["updated"] = now_iso(self.world)
        self.world.table("mail.drafts")[args.id] = row
        return row

    @tool(mutates=True, description="Discard a draft.")
    async def delete_draft(self, args: DraftIdArgs) -> JsonValue:
        require(self.world, "mail.drafts", args.id, "draft")
        self.world.table("mail.drafts").pop(args.id)
        return {"deleted": args.id}

    @tool(mutates=True, additive=True, description="Send an email immediately.")
    async def send(self, args: Compose) -> JsonValue:
        return self._deliver(self._compose(args), draft=None)

    @tool(mutates=True, description="Send an existing draft; the draft is removed once sent.")
    async def send_draft(self, args: DraftIdArgs) -> JsonValue:
        draft = require(self.world, "mail.drafts", args.id, "draft")
        self.world.table("mail.drafts").pop(args.id)
        fields = {k: v for k, v in draft.items() if k not in ("id", "updated")}
        return self._deliver(fields, draft=args.id)

    @tool(mutates=True, description="Archive messages (remove them from the inbox; they stay searchable).")
    async def archive(self, args: ArchiveArgs) -> JsonValue:
        return self._relabel(args.ids, add=[], remove=["inbox"])

    @tool(mutates=True, description="Add or remove labels on messages. Labels must already exist.")
    async def label(self, args: LabelArgs) -> JsonValue:
        known = SYSTEM_LABELS | {get_str(r, "name").casefold() for r in self.world.table("mail.labels").values()}
        unknown = sorted({lbl.casefold() for lbl in args.add + args.remove} - known)
        if unknown:
            raise ServiceError("not_found", f"unknown labels: {', '.join(unknown)}")
        return self._relabel(args.ids, add=args.add, remove=args.remove)

    def _compose(self, args: Compose) -> JsonObject:
        subject, thread = args.subject, None
        if args.in_reply_to is not None:
            parent = require(self.world, "mail.messages", args.in_reply_to, "message")
            thread = opt_str(parent, "thread") or get_str(parent, "id")
            if subject is None:
                original = get_str(parent, "subject")
                subject = original if original.lower().startswith("re:") else f"Re: {original}"
        if subject is None:
            raise ServiceError("validation", "subject: required unless replying (in_reply_to)")
        return {
            "thread": thread,
            "to": json_list(args.to),
            "cc": json_list(args.cc),
            "bcc": json_list(args.bcc),
            "subject": subject,
            "body": args.body,
            "in_reply_to": args.in_reply_to,
        }

    def _deliver(self, fields: JsonObject, *, draft: str | None) -> JsonObject:
        sent_id = self.world.new_id("sent_")
        row: JsonObject = {"id": sent_id, "from": self.world.persona.email, **fields}
        if row.get("thread") is None:
            row["thread"] = sent_id
        row |= {"date": now_iso(self.world), "draft": draft}
        self.world.table("mail.sent")[sent_id] = row
        return row

    def _relabel(self, ids: list[str], *, add: list[str], remove: list[str]) -> JsonObject:
        table = self.world.table("mail.messages")
        rows = [require(self.world, "mail.messages", i, "message") for i in ids]
        drop = {r.casefold() for r in remove}
        for row in rows:
            labels = [lbl for lbl in str_list(row, "labels") if lbl.casefold() not in drop]
            labels += [lbl for lbl in add if lbl not in labels]
            table[get_str(row, "id")] = {**row, "labels": json_list(labels)}
        return {"updated": json_list(ids)}


def _addressed_to(m: JsonObject, needle: str) -> bool:
    folded = needle.casefold()
    return any(folded in addr.casefold() for field in ("to", "cc", "bcc") for addr in str_list(m, field))


def _summary(m: JsonObject) -> JsonObject:
    return {
        "id": m.get("id"),
        "thread": m.get("thread"),
        "from": m.get("from"),
        "to": m.get("to"),
        "subject": m.get("subject"),
        "date": m.get("date"),
        "labels": m.get("labels", []),
        "snippet": snippet(get_str(m, "body")),
    }
