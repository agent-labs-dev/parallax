"""The 15 mock services, keyed by service name (the `<service>` in every `<service>_<method>` tool)."""

from __future__ import annotations

from parallax.world.service import Service
from parallax.world.services.analytics import AnalyticsService
from parallax.world.services.calendar import CalendarService
from parallax.world.services.chat import ChatService
from parallax.world.services.contacts import ContactsService
from parallax.world.services.drive import DriveService
from parallax.world.services.helpdesk import HelpdeskService
from parallax.world.services.mail import MailService
from parallax.world.services.payments import PaymentsService
from parallax.world.services.scheduler import SchedulerService
from parallax.world.services.sheets import SheetsService
from parallax.world.services.tables import TablesService
from parallax.world.services.tracker import TrackerService
from parallax.world.services.user import UserService
from parallax.world.services.web import WebService
from parallax.world.services.wiki import WikiService

SERVICES: dict[str, type[Service]] = {
    cls.name: cls
    for cls in (
        MailService,
        ChatService,
        CalendarService,
        ContactsService,
        DriveService,
        SheetsService,
        TablesService,
        TrackerService,
        HelpdeskService,
        PaymentsService,
        WikiService,
        WebService,
        AnalyticsService,
        SchedulerService,
        UserService,
    )
}

DEFAULT_SERVICES = ("mail", "chat", "calendar", "contacts", "user")
