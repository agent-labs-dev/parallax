"""Chat: team channels with threaded replies, plus direct messages.

Tables:
- channels: {id, name, members[], topic, created}
- messages: {id, channel, author, text, ts, thread_id}; thread_id is null for a top-level post,
  else the id of the thread's root message
- work:     {id, channel, instruction, status, created}; queued work owned by a channel
- dms:      {id, to, from, text, ts}; `to` is the recipient's email, resolved via contacts
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
    page,
    require,
    resolve,
    text_hit,
)

ChannelRef = Field(description="Channel id or name (with or without '#')")


class ReadArgs(Args):
    channel: str = ChannelRef
    limit: Limit = 50
    offset: int = Field(0, ge=0, description="Skip this many of the newest messages; pass next_offset for older ones")


class PostArgs(Args):
    channel: str = ChannelRef
    text: str = Field(min_length=1)


class ReplyArgs(Args):
    message_id: str = Field(description="Id of any message in the thread to reply to")
    text: str = Field(min_length=1)


class DmArgs(Args):
    to: str = Field(description="Recipient's email address or full name")
    text: str = Field(min_length=1)


class CreateChannelArgs(Args):
    name: str = Field(pattern=r"^#?[a-z0-9][a-z0-9_-]{0,79}$", description="Lowercase, digits, '-' or '_'")
    members: list[Email] = []
    topic: str = ""
    work: str | None = Field(default=None, min_length=1, description="Optional work to start inside the new channel")


class StartWorkArgs(Args):
    channel: str = ChannelRef
    instruction: str = Field(min_length=1)


class SearchArgs(Args):
    query: str = Field(min_length=1, description="Words that must all appear in the message")
    limit: Limit = 25


class ChatService(Service):
    name: ClassVar[str] = "chat"
    tables: ClassVar[tuple[str, ...]] = ("channels", "messages", "dms", "work")

    @tool(mutates=False, description="List the team's chat channels.")
    async def list_channels(self, args: Args) -> JsonValue:
        return {"channels": json_list(self.world.table("chat.channels").values())}

    @tool(
        mutates=False,
        description="Read the newest messages in a channel, oldest first, including thread replies. "
        "next_offset reads the page before them.",
    )
    async def read_channel(self, args: ReadArgs) -> JsonValue:
        channel = self._channel(args.channel)
        rows = [m for m in self.world.table("chat.messages").values() if m.get("channel") == channel["id"]]
        rows.sort(key=lambda m: get_str(m, "ts"))
        newest_first = rows[::-1]
        chunk, total, next_offset = page(newest_first, args.offset, args.limit)
        return {
            "channel": channel["id"],
            "messages": json_list(chunk[::-1]),
            "total": total,
            "next_offset": next_offset,
        }

    @tool(mutates=True, additive=True, description="Post a new top-level message to a channel.")
    async def post_message(self, args: PostArgs) -> JsonValue:
        channel = self._channel(args.channel)
        return self._post(get_str(channel, "id"), args.text, thread_id=None)

    @tool(mutates=True, additive=True, description="Reply inside the thread of an existing channel message.")
    async def reply_in_thread(self, args: ReplyArgs) -> JsonValue:
        parent = require(self.world, "chat.messages", args.message_id, "message")
        root = opt_str(parent, "thread_id") or get_str(parent, "id")
        return self._post(get_str(parent, "channel"), args.text, thread_id=root)

    @tool(mutates=True, additive=True, description="Send a private direct message to one person.")
    async def send_dm(self, args: DmArgs) -> JsonValue:
        person = resolve(self.world, "contacts.people", args.to, "person", keys=("email", "name"))
        row: JsonObject = {
            "id": self.world.new_id("dm_"),
            "to": get_str(person, "email"),
            "from": self.world.persona.email,
            "text": args.text,
            "ts": now_iso(self.world),
        }
        self.world.table("chat.dms")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, additive=True, description="Create a new channel, optionally starting work inside it.")
    async def create_channel(self, args: CreateChannelArgs) -> JsonValue:
        name = args.name.lstrip("#")
        if any(get_str(c, "name").lstrip("#") == name for c in self.world.table("chat.channels").values()):
            raise ServiceError("conflict", f"a channel named {name!r} already exists")
        members = [self.world.persona.email, *(m for m in args.members if m != self.world.persona.email)]
        row: JsonObject = {
            "id": self.world.new_id("ch_"),
            "name": name,
            "members": json_list(members),
            "topic": args.topic,
            "created": now_iso(self.world),
        }
        self.world.table("chat.channels")[get_str(row, "id")] = row
        if args.work is not None:
            return {**row, "work": self._start_work(get_str(row, "id"), args.work)}
        return row

    @tool(mutates=True, additive=True, description="Start work inside a channel; this queues a work item there.")
    async def start_work(self, args: StartWorkArgs) -> JsonValue:
        channel = self._channel(args.channel)
        return self._start_work(get_str(channel, "id"), args.instruction)

    def _start_work(self, channel: str, instruction: str) -> JsonObject:
        row: JsonObject = {
            "id": self.world.new_id("work_"),
            "channel": channel,
            "instruction": instruction,
            "status": "queued",
            "created": now_iso(self.world),
        }
        self.world.table("chat.work")[get_str(row, "id")] = row
        return row

    @tool(mutates=False, description="Search channel messages and direct messages.")
    async def search(self, args: SearchArgs) -> JsonValue:
        rows = [
            {**m, "kind": kind}
            for kind, table in (("channel", "chat.messages"), ("dm", "chat.dms"))
            for m in self.world.table(table).values()
            if text_hit(m, ("text", "author", "from", "to"), args.query)
        ]
        rows.sort(key=lambda m: get_str(m, "ts"), reverse=True)
        return {"messages": json_list(rows[: args.limit])}

    def _channel(self, ref: str) -> JsonObject:
        return resolve(self.world, "chat.channels", ref, "channel")

    def _post(self, channel: str, text: str, *, thread_id: str | None) -> JsonObject:
        row: JsonObject = {
            "id": self.world.new_id("msg_"),
            "channel": channel,
            "author": self.world.persona.email,
            "text": text,
            "ts": now_iso(self.world),
            "thread_id": thread_id,
        }
        self.world.table("chat.messages")[get_str(row, "id")] = row
        return row
