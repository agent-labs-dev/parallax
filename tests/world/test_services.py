"""Behaviour of each mock service over a minimal in-memory WorldState."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

import pytest
from parallax.models import JsonObject, Persona
from parallax.world.service import Service, ServiceError
from parallax.world.services import SERVICES
from pydantic import JsonValue

NOW = datetime(2026, 6, 3, 8, 12, tzinfo=UTC)  # Wednesday; 09:12 in London
ME = "alex@northwind.test"


class FakeWorld:
    def __init__(self, seed: dict[str, list[JsonObject]] | None = None) -> None:
        self.now = NOW
        self.persona = Persona(name="Alex Morgan", email=ME, timezone="Europe/London")
        self.tables: defaultdict[str, dict[str, JsonObject]] = defaultdict(dict)
        for name, rows in (seed or {}).items():
            self.tables[name] = {str(r["id"]): r for r in rows}
        self.counter = 0

    def table(self, name: str) -> dict[str, JsonObject]:
        return self.tables[name]

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"


async def call(world: FakeWorld, tool: str, /, **args: JsonValue) -> JsonObject:
    service, method = tool.split("_", 1)
    result = await SERVICES[service](world).call(method, args)
    assert isinstance(result, dict)
    return result


async def fails(world: FakeWorld, tool: str, error: str, /, **args: JsonValue) -> str:
    with pytest.raises(ServiceError) as err:
        await call(world, tool, **args)
    assert err.value.kind == error, err.value.message
    return err.value.message


def test_every_service_builds_specs() -> None:
    # Construction-time schema generation would otherwise fail only when a harness lists tools.
    world = FakeWorld()
    for cls in SERVICES.values():
        service: Service = cls(world)
        assert all(spec.name.startswith(f"{cls.name}_") for spec in service.specs())


# --- mail -------------------------------------------------------------------------------------

MAIL = {
    "mail.messages": [
        {"id": "m1", "thread": "t1", "from": "priya@northwind.test", "to": [ME], "subject": "Q3 shortlist",
         "body": "Can you review the vendor shortlist?", "date": "2026-06-02T16:40:00+01:00", "labels": ["inbox"]},
        {"id": "m2", "thread": "t2", "from": "news@deals.example", "to": [ME], "subject": "Weekly deals",
         "body": "Offers", "date": "2026-06-01T08:00:00+01:00", "labels": ["inbox"]},
    ]
}  # fmt: skip


async def test_mail_draft_then_send_keeps_thread_and_consumes_draft() -> None:
    world = FakeWorld(MAIL)
    draft = await call(world, "mail_create_draft", to=["priya@northwind.test"], body="Looks good", in_reply_to="m1")
    await call(world, "mail_update_draft", id=str(draft["id"]), cc=["sam@northwind.test"])
    sent = await call(world, "mail_send_draft", id=str(draft["id"]))
    assert (sent["thread"], sent["subject"], sent["cc"], sent["from"]) == (
        "t1",
        "Re: Q3 shortlist",
        ["sam@northwind.test"],
        ME,
    )
    assert world.tables["mail.drafts"] == {}
    # The agent can read back its own send (by a cc'd name) and only its own sends.
    listed = await call(world, "mail_list_sent", recipient="sam")
    assert [m["id"] for m in listed["sent"]] == [sent["id"]] and listed["sent"][0]["body"] == "Looks good"
    assert (await call(world, "mail_list_sent", query="shortlist unrelated"))["sent"] == []
    await call(world, "mail_archive", ids=["m2"])
    found = await call(world, "mail_search", label="inbox")
    assert [m["id"] for m in found["messages"]] == ["m1"]


async def test_mail_errors() -> None:
    world = FakeWorld(MAIL)
    await fails(world, "mail_get", "not_found", id="nope")
    await fails(world, "mail_send", "validation", to=["not-an-address"], subject="x", body="y")
    await fails(world, "mail_send", "validation", to=[ME], body="no subject and not a reply")
    await fails(world, "mail_label", "not_found", ids=["m1"], add=["vip"])


# --- chat -------------------------------------------------------------------------------------

CHAT = {
    "chat.channels": [{"id": "c1", "name": "ops", "members": [ME]}],
    "chat.messages": [
        {"id": "k1", "channel": "c1", "author": "kai@northwind.test", "text": "Incident!", "ts": "2026-06-03T08:00:00+00:00", "thread_id": None},
        {"id": "k2", "channel": "c1", "author": "kai@northwind.test", "text": "Paging", "ts": "2026-06-03T08:01:00+00:00", "thread_id": "k1"},
    ],
    "contacts.people": [{"id": "p1", "name": "Ana Silva", "email": "ana@northwind.test"}],
}  # fmt: skip


async def test_chat_threads_and_dms() -> None:
    world = FakeWorld(CHAT)
    reply = await call(world, "chat_reply_in_thread", message_id="k2", text="Fixed")
    assert (reply["channel"], reply["thread_id"]) == ("c1", "k1")
    top = await call(world, "chat_post_message", channel="#ops", text="All clear")
    assert top["thread_id"] is None
    dm = await call(world, "chat_send_dm", to="ana silva", text="The offer is 90k")
    assert dm["to"] == "ana@northwind.test"


async def test_chat_errors() -> None:
    world = FakeWorld(CHAT)
    await fails(world, "chat_post_message", "not_found", channel="launch", text="hi")
    await fails(world, "chat_create_channel", "conflict", name="ops")
    await fails(world, "chat_create_channel", "validation", name="Has Spaces")
    await fails(world, "chat_send_dm", "not_found", to="nobody@else.test", text="hi")


# --- calendar ---------------------------------------------------------------------------------

STANDUP = {
    "id": "e1", "calendar": "cal1", "title": "Standup", "start": "2026-03-23T09:00:00+00:00",
    "end": "2026-03-23T09:15:00+00:00", "tz": "Europe/London", "attendees": ["jo@northwind.test"],
    "rrule": "FREQ=WEEKLY;BYDAY=MO,TH", "exceptions": {}, "status": "confirmed",
}  # fmt: skip
CALENDAR = {"calendar.calendars": [{"id": "cal1", "name": "Alex", "owner": ME}], "calendar.events": [STANDUP]}


async def test_calendar_recurrence_keeps_local_time_across_dst() -> None:
    world = FakeWorld(CALENDAR)
    listed = await call(world, "calendar_list_events", start="2026-03-23T00:00", end="2026-04-03T00:00")
    starts = [e["start"] for e in listed["events"]]
    # London moves to BST on 29 March: still 09:00 local, one hour earlier in UTC.
    assert starts == [
        "2026-03-23T09:00:00+00:00",
        "2026-03-26T09:00:00+00:00",
        "2026-03-30T09:00:00+01:00",
        "2026-04-02T09:00:00+01:00",
    ]


async def test_calendar_occurrence_exceptions_restore_to_seed() -> None:
    world = FakeWorld(CALENDAR)
    await call(world, "calendar_update_occurrence", event_id="e1", date="2026-06-04", start="2026-06-04T10:00")
    await call(world, "calendar_cancel_occurrence", event_id="e1", date="2026-06-08")
    window = {"start": "2026-06-04T00:00", "end": "2026-06-09T00:00"}
    listed = await call(world, "calendar_list_events", **window)
    assert [(e["start"], e["moved"]) for e in listed["events"]] == [("2026-06-04T10:00:00+01:00", True)]
    await call(world, "calendar_restore_occurrence", event_id="e1", date="2026-06-04")
    await call(world, "calendar_update_occurrence", event_id="e1", date="2026-06-08", start="2026-06-08T09:00")
    assert world.tables["calendar.events"]["e1"] == STANDUP  # moving back to the series time leaves no exception


async def test_calendar_create_and_free_busy() -> None:
    world = FakeWorld(CALENDAR)
    event = await call(world, "calendar_create_event", title="Sync", start="2026-06-05T14:00", end="2026-06-05T14:30",
                       attendees=["ben@northwind.test"])  # fmt: skip
    assert (event["calendar"], event["start"]) == ("cal1", "2026-06-05T14:00:00+01:00")
    busy = await call(world, "calendar_free_busy", people=["ben@northwind.test", "jo@northwind.test"],
                      start="2026-06-05T00:00", end="2026-06-06T00:00")  # fmt: skip
    assert busy["busy"] == {
        "ben@northwind.test": [{"start": "2026-06-05T14:00:00+01:00", "end": "2026-06-05T14:30:00+01:00"}],
        "jo@northwind.test": [],
    }


async def test_calendar_errors() -> None:
    world = FakeWorld(CALENDAR)
    await fails(world, "calendar_cancel_occurrence", "not_found", event_id="e1", date="2026-06-03")  # a Wednesday
    await fails(world, "calendar_create_event", "validation", title="x", start="2026-06-05T10:00",
                end="2026-06-05T11:00", rrule="FREQ=MONTHLY")  # fmt: skip
    await fails(
        world, "calendar_create_event", "validation", title="x", start="2026-06-05T10:00", end="2026-06-05T09:00"
    )
    await fails(world, "calendar_get_event", "not_found", id="nope")


# --- contacts ---------------------------------------------------------------------------------

PEOPLE = {
    "contacts.people": [
        {"id": "p1", "name": "Chris Lee", "email": "chris@northwind.test", "company": "Northwind", "phone": "1"},
        {"id": "p2", "name": "Chris Lee", "email": "clee@contoso.test", "company": "Contoso", "phone": "2"},
    ]
}


async def test_contacts_update_one_namesake() -> None:
    world = FakeWorld(PEOPLE)
    found = await call(world, "contacts_search", query="chris lee", company="northwind")
    assert [p["id"] for p in found["people"]] == ["p1"]
    await call(world, "contacts_update", id="p1", phone="+44 20 7946 0000")
    assert world.tables["contacts.people"]["p1"]["phone"] == "+44 20 7946 0000"
    assert world.tables["contacts.people"]["p1"]["company"] == "Northwind"
    await fails(world, "contacts_update", "not_found", id="p9", phone="3")
    await fails(world, "contacts_create", "validation", name="X", email="bad")
    await fails(world, "chat_send_dm", "conflict", to="Chris Lee", text="hi")


# --- drive ------------------------------------------------------------------------------------

DRIVE = {
    "drive.folders": [{"id": "f1", "name": "Launch", "parent": None}],
    "drive.files": [{"id": "d1", "name": "Q3 deck", "folder": "f1", "content": "slides", "owner": ME}],
}


async def test_drive_share_then_restrict_reuses_permission() -> None:
    world = FakeWorld(DRIVE)
    shared = await call(world, "drive_share", id="d1", emails=["sam@northwind.test"], role="editor")
    perm_id = shared["permissions"][0]["id"]
    await call(world, "drive_set_permission", id="d1", email="sam@northwind.test", role="viewer")
    assert world.tables["drive.permissions"] == {
        perm_id: {"id": perm_id, "file": "d1", "email": "sam@northwind.test", "role": "viewer"}
    }
    await call(world, "drive_delete_file", id="d1")
    assert world.tables["drive.permissions"] == {}


async def test_drive_errors() -> None:
    world = FakeWorld(DRIVE)
    await fails(world, "drive_share", "validation", id="d1", emails=["sam@northwind.test"], role="owner")
    await fails(world, "drive_move", "not_found", id="d1", folder="Archive")
    await fails(world, "drive_update_file", "validation", id="d1", content="a", append="b")
    await fails(world, "drive_get_file", "not_found", id="d9")


# --- sheets -----------------------------------------------------------------------------------

SHEETS = {
    "sheets.sheets": [{"id": "s1", "title": "Expenses", "columns": ["date", "vendor", "amount"]}],
    "sheets.rows": [{"id": "r1", "sheet": "s1", "position": 1, "date": "2026-06-01", "vendor": "Figma", "amount": 45}],
}


async def test_sheets_append_and_fix_cell() -> None:
    world = FakeWorld(SHEETS)
    row = await call(world, "sheets_append_row", sheet="expenses", values={"vendor": "Notion", "amount": 12})
    assert (row["position"], row["date"]) == (2, None)
    await call(world, "sheets_update_cell", row_id="r1", column="amount", value=54)
    read = await call(world, "sheets_read", sheet="s1")
    assert [(r["vendor"], r["amount"]) for r in read["rows"]] == [("Figma", 54), ("Notion", 12)]
    await fails(world, "sheets_update_cell", "validation", row_id="r1", column="total", value=1)
    await fails(world, "sheets_delete_row", "not_found", row_id="r9")


# --- tables -----------------------------------------------------------------------------------

CRM = {
    "tables.bases": [{"id": "b1", "name": "Leads", "fields": ["company", "stage"]}],
    "tables.records": [
        {"id": "l1", "base": "b1", "company": "Acme", "stage": "Qualified"},
        {"id": "l2", "base": "b1", "company": "Initech", "stage": "New"},
    ],
}


async def test_tables_filter_and_update() -> None:
    world = FakeWorld(CRM)
    qualified = await call(world, "tables_list_records", base="Leads", where={"stage": "qualified"})
    assert [r["id"] for r in qualified["records"]] == ["l1"]
    created = await call(world, "tables_create_record", base="b1", fields={"company": "Ortiz Foods"})
    await call(world, "tables_update_record", id=str(created["id"]), fields={"stage": "Proposal"})
    assert world.tables["tables.records"][str(created["id"])]["stage"] == "Proposal"
    await fails(world, "tables_create_record", "validation", base="b1", fields={"owner": "me"})
    await fails(world, "tables_list_records", "not_found", base="Deals")


async def test_tables_pages_a_large_base() -> None:
    rows = [{"id": f"d{i}", "base": "b1", "company": f"Co {i}", "stage": "New"} for i in range(1, 131)]
    world = FakeWorld({"tables.bases": CRM["tables.bases"], "tables.records": rows})
    first = await call(world, "tables_list_records", base="Leads")
    assert (len(first["records"]), first["total"], first["next_offset"]) == (100, 130, 100)
    rest = await call(world, "tables_list_records", base="Leads", offset=100)
    assert ([r["id"] for r in rest["records"]][0], len(rest["records"]), rest["next_offset"]) == ("d101", 30, None)


# --- tracker ----------------------------------------------------------------------------------


async def test_tracker_create_close_and_link() -> None:
    world = FakeWorld()
    issue = await call(world, "tracker_create_issue", title="Login outage", labels=["bug"])
    assert issue["url"] == f"https://tracker.example/issues/{issue['id']}"
    await call(world, "tracker_close_issue", id=str(issue["id"]), comment="Resolved by rollback")
    got = await call(world, "tracker_get_issue", id=str(issue["id"]))
    assert got["status"] == "closed"
    assert [c["body"] for c in got["comments"]] == ["Resolved by rollback"]
    await fails(world, "tracker_update_issue", "validation", id=str(issue["id"]), status="done")
    await fails(world, "tracker_comment", "not_found", issue="ISS-999", body="x")


# --- helpdesk ---------------------------------------------------------------------------------

HELPDESK = {
    "helpdesk.conversations": [{"id": "cv1", "customer": "dana@client.example", "subject": "Billing", "status": "open"}]
}


async def test_helpdesk_escalate_reply_close() -> None:
    world = FakeWorld(HELPDESK)
    ticket = await call(world, "helpdesk_create_ticket", subject="Double billing", conversation="cv1", priority="high")
    assert world.tables["helpdesk.conversations"]["cv1"]["ticket"] == ticket["id"]
    assert ticket["requester"] == "dana@client.example"
    reply = await call(world, "helpdesk_reply", target="cv1", body=f"Escalated as {ticket['id']}")
    assert reply["kind"] == "conversation"
    await call(world, "helpdesk_close", id="cv1")
    await call(world, "helpdesk_set_status", target=str(ticket["id"]), status="pending")
    assert world.tables["helpdesk.conversations"]["cv1"]["status"] == "closed"
    await fails(world, "helpdesk_set_status", "validation", target="cv1", status="solved")
    await fails(world, "helpdesk_reply", "not_found", target="cv9", body="x")


# --- payments ---------------------------------------------------------------------------------

PAYMENTS = {"payments.customers": [{"id": "cus1", "name": "Acme", "email": "billing@acme.example"}]}


async def test_payments_idempotency_key_prevents_double_charge() -> None:
    world = FakeWorld(PAYMENTS)
    args: dict[str, JsonValue] = {"customer": "Acme", "amount": 300, "currency": "gbp", "idempotency_key": "wk-1"}
    first = await call(world, "payments_create_charge", **args)
    retry = await call(world, "payments_create_charge", **args)
    assert retry == first
    assert len(world.tables["payments.charges"]) == 1
    assert first["currency"] == "GBP"
    await fails(world, "payments_create_charge", "conflict", **{**args, "amount": 301})


async def test_payments_refunds() -> None:
    world = FakeWorld(PAYMENTS)
    charge = await call(world, "payments_create_charge", customer="cus1", amount=100, currency="GBP")
    await call(world, "payments_create_refund", charge=str(charge["id"]), amount=40)
    assert world.tables["payments.charges"][str(charge["id"])]["status"] == "partially_refunded"
    await fails(world, "payments_create_refund", "conflict", charge=str(charge["id"]), amount=61)
    await call(world, "payments_create_refund", charge=str(charge["id"]))
    assert world.tables["payments.charges"][str(charge["id"])]["status"] == "refunded"
    await fails(world, "payments_create_refund", "not_found", charge="ch_nope")
    await fails(world, "payments_create_charge", "validation", customer="cus1", amount=1.005, currency="GBP")


# --- wiki -------------------------------------------------------------------------------------


async def test_wiki_create_append_comment() -> None:
    world = FakeWorld()
    page = await call(world, "wiki_create_page", title="Q3 OKRs", body="Draft")
    await call(world, "wiki_update_page", id=str(page["id"]), append="\n## Budget")
    await call(world, "wiki_comment", page=str(page["id"]), body="Lee, please review", mentions=["lee@northwind.test"])
    got = await call(world, "wiki_get_page", id=str(page["id"]))
    assert got["body"] == "Draft\n## Budget"
    assert got["comments"][0]["mentions"] == ["lee@northwind.test"]
    await fails(world, "wiki_update_page", "validation", id=str(page["id"]), body="a", append="b")
    await fails(world, "wiki_comment", "not_found", page="page_x", body="hi")


# --- web --------------------------------------------------------------------------------------

WEB = {
    "web.pages": [
        {"id": "https://vendor-a.example/pricing", "url": "https://vendor-a.example/pricing", "title": "Vendor A pricing",
         "text": "Team plan costs $12 per seat. SSO is included."},
        {"id": "https://vendor-b.example/blog", "url": "https://vendor-b.example/blog", "title": "Vendor B blog",
         "text": "Our team retreat photos."},
    ]
}  # fmt: skip


async def test_web_search_ranks_and_fetch_resolves() -> None:
    world = FakeWorld(WEB)
    found = await call(world, "web_search", query="vendor a pricing sso")
    assert [r["url"] for r in found["results"]][0] == "https://vendor-a.example/pricing"
    assert (await call(world, "web_search", query="kubernetes"))["results"] == []
    page = await call(world, "web_fetch", url="vendor-a.example/pricing/")
    assert page["title"] == "Vendor A pricing"
    await fails(world, "web_fetch", "not_found", url="https://vendor-c.example/pricing")


# --- scheduler --------------------------------------------------------------------------------


async def test_scheduler_cron_once_watch() -> None:
    world = FakeWorld()
    digest = await call(
        world, "scheduler_create_job", kind="cron", spec="0  8 * * 1-5", title="Digest", instruction="Send it"
    )
    assert (digest["spec"], digest["tz"], digest["next_run"]) == (
        "0 8 * * 1-5",
        "Europe/London",
        "2026-06-04T08:00:00+01:00",
    )
    once = await call(world, "scheduler_create_job", kind="once", spec="2026-06-05T16:00", tz="America/New_York",
                      title="Call the bank", instruction="Remind me")  # fmt: skip
    assert once["spec"] == "2026-06-05T16:00:00-04:00"
    await call(
        world, "scheduler_create_job", kind="watch", spec="mail: from acme.example", title="Acme", instruction="Tell me"
    )
    await call(world, "scheduler_update_job", id=str(digest["id"]), active=False)
    assert [j["title"] for j in (await call(world, "scheduler_list_jobs", active=True))["jobs"]] == [
        "Call the bank",
        "Acme",
    ]


async def test_scheduler_errors() -> None:
    world = FakeWorld()
    base: dict[str, JsonValue] = {"title": "t", "instruction": "i"}
    await fails(world, "scheduler_create_job", "validation", kind="cron", spec="0 8 * *", **base)
    await fails(world, "scheduler_create_job", "validation", kind="once", spec="2026-06-01T09:00", **base)
    await fails(world, "scheduler_create_job", "validation", kind="watch", spec="stocks: ACME", **base)
    await fails(world, "scheduler_create_job", "validation", kind="cron", spec="0 8 * * *", tz="Mars/Base", **base)
    await fails(world, "scheduler_delete_job", "not_found", id="job_9")


# --- user -------------------------------------------------------------------------------------

RULES = {
    "user.rules": [
        {"id": "rule1", "tool": "approval", "when": {"scope.charge": "ch_B"}, "reply": "Yes, refund ch_B.",
         "decision": "approve", "uses": 1},
        {"id": "rule2", "tool": "ask", "when": {"question": {"contains": "which charge"}}, "reply": "The one from the 2nd."},
    ]
}  # fmt: skip


async def test_user_rules_scope_and_uses() -> None:
    world = FakeWorld(RULES)
    ok = await call(world, "user_request_approval", action="refund", scope={"charge": "ch_B"})
    assert (ok["decision"], ok["approved"]) == ("approve", True)
    again = await call(world, "user_request_approval", action="refund", scope={"charge": "ch_B"})
    assert again["decision"] == "deny"  # uses=1 exhausted: falls through to the default refusal
    other = await call(world, "user_request_approval", action="refund", scope={"charge": "ch_A"})
    assert other["reply"] == "No, hold off on that for now."
    answer = await call(world, "user_ask", question="Which charge is the duplicate?")
    assert answer["answer"] == "The one from the 2nd."
    default = await call(world, "user_ask", question="Any colour preference?")
    assert default["answer"] == "I don't have a preference - use your judgement."
    logged = list(world.tables["user.interactions"].values())
    assert [(i["kind"], i["rule"]) for i in logged] == [
        ("approval", "rule1"),
        ("approval", None),
        ("approval", None),
        ("ask", "rule2"),
        ("ask", None),
    ]
    await fails(world, "user_ask", "validation", question="")
    # the only tool that resolves "my ..." against service rows keyed by email
    assert await call(world, "user_whoami") == {"name": "Alex Morgan", "email": ME, "timezone": "Europe/London"}


async def test_create_channel_with_work_matches_create_then_start_work() -> None:
    atomic, separate = FakeWorld(CHAT), FakeWorld(CHAT)
    result = await call(atomic, "chat_create_channel", name="design", work="Review the mocks.")
    channel = await call(separate, "chat_create_channel", name="design")
    posted = await call(separate, "chat_start_work", channel=str(channel["id"]), instruction="Review the mocks.")
    assert result == {**channel, "work": posted}
    assert atomic.tables == separate.tables
    await fails(separate, "chat_start_work", "not_found", channel="missing", instruction="Review")
    await fails(separate, "chat_create_channel", "validation", name="empty", work="")
    assert separate.tables == atomic.tables


# --- analytics --------------------------------------------------------------------------------

PRODUCT = {
    "analytics.events": [
        {
            "id": "e1",
            "event": "signed_up",
            "person": "p1",
            "ts": "2026-06-01T09:00:00+01:00",
            "properties": {"source": "organic"},
        },
        {"id": "e2", "event": "onboarded", "person": "p1", "ts": "2026-06-02T09:00:00+01:00", "properties": {}},
        {
            "id": "e3",
            "event": "signed_up",
            "person": "p2",
            "ts": "2026-06-07T23:30:00+01:00",
            "properties": {"source": "partner"},
        },
        {"id": "e4", "event": "onboarded", "person": "p2", "ts": "2026-06-30T09:00:00+01:00", "properties": {}},
        {
            "id": "e5",
            "event": "signed_up",
            "person": "p3",
            "ts": "2026-06-08T00:30:00+01:00",
            "properties": {"source": "organic"},
        },
        {"id": "e6", "event": "onboarded", "person": "p4", "ts": "2026-06-03T09:00:00+01:00", "properties": {}},
    ],
    "analytics.insights": [
        {"id": "i1", "name": "Activation", "kind": "funnel", "steps": ["signed_up", "onboarded"], "window_days": 3},
        {"id": "i2", "name": "Weekly signups", "kind": "trend", "event": "signed_up", "interval": "week"},
    ],
}


async def test_analytics_trend_buckets_by_the_persona_week_and_splits() -> None:
    world = FakeWorld(PRODUCT)
    weekly = await call(
        world,
        "analytics_trend",
        event="signed_up",
        date_from="2026-06-01",
        date_to="2026-06-14",
        interval="week",
        breakdown="source",
    )
    assert weekly["series"] == [
        {"period": "2026-06-01", "count": 2, "breakdown": {"organic": 1, "partner": 1}},
        {"period": "2026-06-08", "count": 1, "breakdown": {"organic": 1}},
    ]
    assert weekly["total"] == 3
    await fails(world, "analytics_trend", "not_found", event="signup", date_from="2026-06-01", date_to="2026-06-07")
    await fails(world, "analytics_trend", "validation", event="signed_up", date_from="2026-06-07", date_to="2026-06-01")


async def test_analytics_funnel_counts_people_in_order_within_the_window() -> None:
    world = FakeWorld(PRODUCT)
    week = {"date_from": "2026-06-01", "date_to": "2026-06-07"}
    funnel = await call(world, "analytics_funnel", steps=["signed_up", "onboarded"], breakdown="source", **week)
    assert [s["count"] for s in funnel["steps"]] == [2, 1]  # p2 onboarded after the 14-day window; p4 never signed up
    assert funnel["breakdown"]["partner"][1]["count"] == 0
    saved = await call(world, "analytics_run_insight", id="i1", **week)
    assert [s["count"] for s in saved["steps"]] == [2, 1]
    trend = await call(world, "analytics_run_insight", id="i2", **week)
    assert trend["series"] == [{"period": "2026-06-01", "count": 2}]
    events = await call(world, "analytics_list_events")
    assert events["events"][1] == {"name": "signed_up", "volume": 3, "properties": ["source"]}


async def test_helpdesk_tracker_and_chat_lists_page() -> None:
    world = FakeWorld(
        {
            "helpdesk.tickets": [{"id": f"t{i}", "subject": f"S {i}", "status": "open"} for i in range(1, 121)],
            "tracker.issues": [{"id": f"I-{i}", "title": f"T {i}", "status": "open"} for i in range(1, 61)],
            "chat.channels": [{"id": "c1", "name": "eng", "members": []}],
            "chat.messages": [
                {"id": f"m{i}", "channel": "c1", "text": f"x{i}", "ts": f"2026-01-01T00:{i:02d}:00+00:00"}
                for i in range(1, 61)
            ],
        }
    )
    first = await call(world, "helpdesk_list_tickets", limit=100)
    assert (len(first["tickets"]), first["total"], first["next_offset"]) == (100, 120, 100)
    rest = await call(world, "helpdesk_list_tickets", limit=100, offset=100)
    assert ([t["id"] for t in rest["tickets"]][0], rest["next_offset"]) == ("t101", None)
    issues = await call(world, "tracker_search_issues")
    assert (len(issues["issues"]), issues["total"], issues["next_offset"]) == (50, 60, 50)
    newest = await call(world, "chat_read_channel", channel="eng")
    assert ([m["id"] for m in newest["messages"]][0], newest["total"], newest["next_offset"]) == ("m11", 60, 50)
    older = await call(world, "chat_read_channel", channel="eng", offset=50)
    assert ([m["id"] for m in older["messages"]], older["next_offset"]) == ([f"m{i}" for i in range(1, 11)], None)
