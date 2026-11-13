"""Contacts: the user's address book.

Tables:
- people: {id, name, email, phone, company, title, notes}
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, Email, Limit, get_str, json_list, require, text_hit, updated

SEARCHED = ("name", "email", "phone", "company", "title", "notes")


class SearchArgs(Args):
    query: str | None = Field(None, description="Words matched against name, email, phone, company, title, notes")
    company: str | None = Field(None, description="Only people at this company")
    limit: Limit = 25


class IdArgs(Args):
    id: str = Field(description="Contact id")


class CreateArgs(Args):
    name: str = Field(min_length=1)
    email: Email
    phone: str | None = None
    company: str | None = None
    title: str | None = None
    notes: str | None = None


class UpdateArgs(Args):
    id: str = Field(description="Contact id")
    name: str | None = None
    email: Email | None = None
    phone: str | None = None
    company: str | None = None
    title: str | None = None
    notes: str | None = None


class ContactsService(Service):
    name: ClassVar[str] = "contacts"
    tables: ClassVar[tuple[str, ...]] = ("people",)

    @tool(mutates=False, description="Search the user's contacts.")
    async def search(self, args: SearchArgs) -> JsonValue:
        rows = [
            p
            for p in self.world.table("contacts.people").values()
            if text_hit(p, SEARCHED, args.query) and (args.company is None or text_hit(p, ("company",), args.company))
        ]
        return {"people": json_list(rows[: args.limit])}

    @tool(mutates=False, description="Get one contact by id.")
    async def get(self, args: IdArgs) -> JsonValue:
        return require(self.world, "contacts.people", args.id, "contact")

    @tool(mutates=True, additive=True, description="Add a new contact.")
    async def create(self, args: CreateArgs) -> JsonValue:
        table = self.world.table("contacts.people")
        if any(get_str(p, "email").casefold() == args.email.casefold() for p in table.values()):
            raise ServiceError("conflict", f"a contact with email {args.email} already exists")
        row: JsonObject = {"id": self.world.new_id("c_"), **args.model_dump()}
        table[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Change fields of a contact; omitted fields are kept.")
    async def update(self, args: UpdateArgs) -> JsonValue:
        row = updated(require(self.world, "contacts.people", args.id, "contact"), args.model_dump(exclude={"id"}))
        self.world.table("contacts.people")[args.id] = row
        return row
