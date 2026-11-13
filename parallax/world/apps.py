"""The connected app each mock service stands in for, so a product can mount one MCP server per app.

A product under test connects apps one by one (Gmail, Slack, HubSpot...), and its agent must pick the right
app and wire several together. The world server serves each service alone at `<mcp url>/<service>`
(`server.py`); a harness that registers one server per entry here gives the agent that same shape. The tool
names stay the world's own, so checks, oracles and the call log do not change.
"""

from __future__ import annotations

from dataclasses import dataclass

from parallax.world.services import SERVICES


@dataclass(frozen=True)
class App:
    service: str  # the world service, the `<service>` of its `<service>_<method>` tools
    slug: str  # lowercase, for a product's server slug
    name: str  # the display name a user sees in the product
    about: str  # one line on what the app holds


APPS: dict[str, App] = {
    a.service: a
    for a in (
        App("mail", "gmail", "Gmail", "the user's email: inbox, threads, drafts and sent mail"),
        App("chat", "slack", "Slack", "team chat: channels, threads and direct messages"),
        App("calendar", "google-calendar", "Google Calendar", "the user's calendar and colleagues' free/busy"),
        App("contacts", "google-contacts", "Google Contacts", "people, emails, phones and companies"),
        App("drive", "google-drive", "Google Drive", "files, folders and sharing"),
        App("sheets", "google-sheets", "Google Sheets", "spreadsheets with named columns"),
        App("tables", "airtable", "Airtable", "record bases such as the CRM"),
        App("tracker", "linear", "Linear", "issues, statuses and comments"),
        App("helpdesk", "zendesk", "Zendesk", "support conversations and tickets"),
        App("payments", "stripe", "Stripe", "customers, charges and refunds"),
        App("wiki", "notion", "Notion", "wiki pages and comments"),
        App("web", "web-search", "Web search", "search and fetch public web pages"),
        App("scheduler", "scheduler", "Scheduler", "scheduled jobs and their runs"),
        App("analytics", "posthog", "PostHog", "product analytics: events, trends, funnels and saved insights"),
        App("user", "profile", "Profile", "who the user is, the current time, and questions or approvals for them"),
    )
}

if set(APPS) != set(SERVICES):
    raise RuntimeError(f"apps and services differ: {sorted(set(APPS) ^ set(SERVICES))}")


def tool_names(service: str) -> tuple[str, ...]:
    return tuple(f"{service}_{method}" for method in SERVICES[service].tools)
