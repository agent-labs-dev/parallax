import os

from relay import settings


def bind_address() -> tuple[str, int]:
    host = os.environ.get("DEFAULT_HOST", settings.DEFAULT_HOST)
    port = int(os.environ.get("DEFAULT_PORT", settings.DEFAULT_PORT))
    return host, port
