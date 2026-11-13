"""Drive: documents in folders, with per-person sharing.

Tables:
- folders:     {id, name, parent}
- files:       {id, name, folder, content, owner, created, modified}
- permissions: {id, file, email, role}; role is viewer | commenter | editor; the owner needs none
"""

from __future__ import annotations

from enum import StrEnum
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
    resolve,
    snippet,
    text_hit,
)


class Role(StrEnum):
    VIEWER = "viewer"
    COMMENTER = "commenter"
    EDITOR = "editor"


class SearchArgs(Args):
    query: str | None = Field(None, description="Words that must appear in the file name or content")
    folder: str | None = Field(None, description="Only files directly in this folder (id or name)")
    limit: Limit = 25


class FileIdArgs(Args):
    id: str = Field(description="File id")


class CreateFileArgs(Args):
    name: str = Field(min_length=1)
    content: str = ""
    folder: str | None = Field(None, description="Folder id or name; omit for the top level")


class UpdateFileArgs(Args):
    id: str = Field(description="File id")
    name: str | None = None
    content: str | None = Field(None, description="Replaces the whole content")
    append: str | None = Field(None, description="Text appended to the end of the content")


class CreateFolderArgs(Args):
    name: str = Field(min_length=1)
    parent: str | None = Field(None, description="Parent folder id or name; omit for the top level")


class MoveArgs(Args):
    id: str = Field(description="File id")
    folder: str | None = Field(description="Destination folder id or name; null for the top level")


class ShareArgs(Args):
    id: str = Field(description="File id")
    emails: list[Email] = Field(min_length=1)
    role: Role


class SetPermissionArgs(Args):
    id: str = Field(description="File id")
    email: Email
    role: Role | None = Field(description="New role, or null to remove this person's access")


class DriveService(Service):
    name: ClassVar[str] = "drive"
    tables: ClassVar[tuple[str, ...]] = ("folders", "files", "permissions")

    @tool(mutates=False, description="Search files by name and content. Returns summaries without full content.")
    async def search(self, args: SearchArgs) -> JsonValue:
        folder = self._folder(args.folder) if args.folder else None
        rows = [
            f
            for f in self.world.table("drive.files").values()
            if text_hit(f, ("name", "content"), args.query) and (folder is None or f.get("folder") == folder)
        ]
        return {"files": json_list(_summary(f) for f in rows[: args.limit])}

    @tool(mutates=False, description="List folders.")
    async def list_folders(self, args: Args) -> JsonValue:
        return {"folders": json_list(self.world.table("drive.folders").values())}

    @tool(mutates=False, description="Read a file, including its content and who it is shared with.")
    async def get_file(self, args: FileIdArgs) -> JsonValue:
        row = require(self.world, "drive.files", args.id, "file")
        return {**row, "permissions": json_list(self._permissions(args.id))}

    @tool(mutates=True, additive=True, description="Create a document.")
    async def create_file(self, args: CreateFileArgs) -> JsonValue:
        now = now_iso(self.world)
        row: JsonObject = {
            "id": self.world.new_id("file_"),
            "name": args.name,
            "folder": self._folder(args.folder) if args.folder else None,
            "content": args.content,
            "owner": self.world.persona.email,
            "created": now,
            "modified": now,
        }
        self.world.table("drive.files")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Rename a file, replace its content, or append to it.")
    async def update_file(self, args: UpdateFileArgs) -> JsonValue:
        if args.content is not None and args.append is not None:
            raise ServiceError("validation", "content, append: pass one or the other")
        row = dict(require(self.world, "drive.files", args.id, "file"))
        if args.name is not None:
            row["name"] = args.name
        if args.content is not None:
            row["content"] = args.content
        if args.append is not None:
            row["content"] = get_str(row, "content") + args.append
        row["modified"] = now_iso(self.world)
        self.world.table("drive.files")[args.id] = row
        return row

    @tool(mutates=True, description="Delete a file permanently, along with its sharing.")
    async def delete_file(self, args: FileIdArgs) -> JsonValue:
        require(self.world, "drive.files", args.id, "file")
        self.world.table("drive.files").pop(args.id)
        perms = self.world.table("drive.permissions")
        for perm in self._permissions(args.id):
            perms.pop(get_str(perm, "id"))
        return {"deleted": args.id}

    @tool(mutates=True, additive=True, description="Create a folder.")
    async def create_folder(self, args: CreateFolderArgs) -> JsonValue:
        row: JsonObject = {
            "id": self.world.new_id("folder_"),
            "name": args.name,
            "parent": self._folder(args.parent) if args.parent else None,
        }
        self.world.table("drive.folders")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Move a file to another folder.")
    async def move(self, args: MoveArgs) -> JsonValue:
        row = {**require(self.world, "drive.files", args.id, "file")}
        row |= {"folder": self._folder(args.folder) if args.folder else None, "modified": now_iso(self.world)}
        self.world.table("drive.files")[args.id] = row
        return row

    @tool(
        mutates=True,
        description="Share a file with people at a role; existing access is changed to that role.",
    )
    async def share(self, args: ShareArgs) -> JsonValue:
        require(self.world, "drive.files", args.id, "file")
        return {"permissions": json_list(self._grant(args.id, email, args.role) for email in args.emails)}

    @tool(mutates=True, description="Change or remove one person's access to a file.")
    async def set_permission(self, args: SetPermissionArgs) -> JsonValue:
        require(self.world, "drive.files", args.id, "file")
        if args.role is not None:
            return self._grant(args.id, args.email, args.role)
        existing = self._permission_for(args.id, args.email)
        if existing is None:
            raise ServiceError("not_found", f"{args.email} has no access to {args.id}")
        self.world.table("drive.permissions").pop(get_str(existing, "id"))
        return {"removed": existing}

    def _folder(self, ref: str) -> str:
        return get_str(resolve(self.world, "drive.folders", ref, "folder"), "id")

    def _permissions(self, file_id: str) -> list[JsonObject]:
        return [p for p in self.world.table("drive.permissions").values() if p.get("file") == file_id]

    def _permission_for(self, file_id: str, email: str) -> JsonObject | None:
        found = [p for p in self._permissions(file_id) if get_str(p, "email").casefold() == email.casefold()]
        return found[0] if found else None

    def _grant(self, file_id: str, email: str, role: Role) -> JsonObject:
        existing = self._permission_for(file_id, email)
        perm_id = get_str(existing, "id") if existing else self.world.new_id("perm_")
        row: JsonObject = {"id": perm_id, "file": file_id, "email": email, "role": role.value}
        self.world.table("drive.permissions")[perm_id] = row
        return row


def _summary(f: JsonObject) -> JsonObject:
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "folder": f.get("folder"),
        "owner": f.get("owner"),
        "modified": f.get("modified"),
        "snippet": snippet(opt_str(f, "content") or ""),
    }
