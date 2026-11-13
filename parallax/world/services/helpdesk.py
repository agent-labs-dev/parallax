"""Helpdesk: customer conversations, support tickets and the replies sent on them.

Tables:
- conversations: {id, customer, customer_name, subject, status, messages[], ticket, updated}
  `messages` is the customer-side history, e.g. [{from, body, at}]; `ticket` links an escalation
- tickets:       {id, subject, description, requester, priority, status, conversation, created, updated}
- replies:       {id, target, kind, body, author, created}; kind is conversation | ticket
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, Limit, Offset, get_str, json_list, now_iso, page, require, text_hit


class Status(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    SNOOZED = "snoozed"  # conversations only
    SOLVED = "solved"  # tickets only
    CLOSED = "closed"


CONVERSATION_STATUSES = frozenset({Status.OPEN, Status.PENDING, Status.SNOOZED, Status.CLOSED})
TICKET_STATUSES = frozenset({Status.OPEN, Status.PENDING, Status.SOLVED, Status.CLOSED})


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ListArgs(Args):
    status: Status | None = None
    query: str | None = Field(None, description="Words that must appear in the subject, customer or messages")
    limit: Limit = 50
    offset: Offset = 0


class IdArgs(Args):
    id: str


class ReplyArgs(Args):
    target: str = Field(description="Conversation id or ticket id")
    body: str = Field(min_length=1)


class CreateTicketArgs(Args):
    subject: str = Field(min_length=1)
    description: str = ""
    conversation: str | None = Field(None, description="Conversation this ticket escalates, if any")
    priority: Priority = Priority.NORMAL


class StatusArgs(Args):
    target: str = Field(description="Conversation id or ticket id")
    status: Status = Field(
        description="Conversations: open|pending|snoozed|closed; tickets: open|pending|solved|closed"
    )


class HelpdeskService(Service):
    name: ClassVar[str] = "helpdesk"
    tables: ClassVar[tuple[str, ...]] = ("conversations", "tickets", "replies")

    @tool(mutates=False, description="List customer conversations.")
    async def list_conversations(self, args: ListArgs) -> JsonValue:
        return self._page("conversations", args)

    @tool(mutates=False, description="Read a conversation with every reply sent on it.")
    async def get_conversation(self, args: IdArgs) -> JsonValue:
        row = require(self.world, "helpdesk.conversations", args.id, "conversation")
        return {**row, "replies": json_list(self._replies(args.id))}

    @tool(mutates=False, description="List support tickets.")
    async def list_tickets(self, args: ListArgs) -> JsonValue:
        return self._page("tickets", args)

    @tool(mutates=False, description="Read a ticket with every reply sent on it.")
    async def get_ticket(self, args: IdArgs) -> JsonValue:
        row = require(self.world, "helpdesk.tickets", args.id, "ticket")
        return {**row, "replies": json_list(self._replies(args.id))}

    @tool(mutates=True, additive=True, description="Send a reply to the customer on a conversation or ticket.")
    async def reply(self, args: ReplyArgs) -> JsonValue:
        table, _ = self._target(args.target)
        row: JsonObject = {
            "id": self.world.new_id("reply_"),
            "target": args.target,
            "kind": table.removeprefix("helpdesk.").removesuffix("s"),
            "body": args.body,
            "author": self.world.persona.email,
            "created": now_iso(self.world),
        }
        self.world.table("helpdesk.replies")[get_str(row, "id")] = row
        return row

    @tool(
        mutates=True,
        description="Open a ticket, optionally escalating a conversation (the two are linked).",
    )
    async def create_ticket(self, args: CreateTicketArgs) -> JsonValue:
        now = now_iso(self.world)
        convo = (
            require(self.world, "helpdesk.conversations", args.conversation, "conversation")
            if args.conversation
            else None
        )
        ticket_id = self.world.new_id("TKT-")
        row: JsonObject = {
            "id": ticket_id,
            "subject": args.subject,
            "description": args.description,
            "requester": convo.get("customer") if convo else None,
            "priority": args.priority.value,
            "status": Status.OPEN.value,
            "conversation": args.conversation,
            "created": now,
            "updated": now,
        }
        self.world.table("helpdesk.tickets")[ticket_id] = row
        if convo is not None and args.conversation is not None:
            self.world.table("helpdesk.conversations")[args.conversation] = {
                **convo,
                "ticket": ticket_id,
                "updated": now,
            }
        return row

    @tool(mutates=True, description="Close a conversation.")
    async def close(self, args: IdArgs) -> JsonValue:
        return self._set("helpdesk.conversations", args.id, Status.CLOSED)

    @tool(mutates=True, description="Set the status of a conversation or ticket.")
    async def set_status(self, args: StatusArgs) -> JsonValue:
        table, _ = self._target(args.target)
        allowed = CONVERSATION_STATUSES if table == "helpdesk.conversations" else TICKET_STATUSES
        if args.status not in allowed:
            names = "|".join(sorted(s.value for s in allowed))
            raise ServiceError("validation", f"status: {args.target} accepts {names}")
        return self._set(table, args.target, args.status)

    def _page(self, kind: str, args: ListArgs) -> JsonValue:
        rows = [
            r
            for r in self.world.table(f"helpdesk.{kind}").values()
            if (args.status is None or r.get("status") == args.status.value)
            and text_hit(
                r, ("subject", "customer", "customer_name", "requester", "messages", "description"), args.query
            )
        ]
        chunk, total, next_offset = page(rows, args.offset, args.limit)
        return {kind: json_list(chunk), "total": total, "next_offset": next_offset}

    def _replies(self, target: str) -> list[JsonObject]:
        return [r for r in self.world.table("helpdesk.replies").values() if r.get("target") == target]

    def _target(self, ref: str) -> tuple[str, JsonObject]:
        for table in ("helpdesk.conversations", "helpdesk.tickets"):
            row = self.world.table(table).get(ref)
            if row is not None:
                return table, row
        raise ServiceError("not_found", f"no conversation or ticket with id {ref!r}")

    def _set(self, table: str, row_id: str, status: Status) -> JsonObject:
        row = {**require(self.world, table, row_id, table.removeprefix("helpdesk.")[:-1])}
        row |= {"status": status.value, "updated": now_iso(self.world)}
        self.world.table(table)[row_id] = row
        return row
