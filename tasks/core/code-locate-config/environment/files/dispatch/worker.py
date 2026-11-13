from dispatch.settings import Settings, load


def should_retry(attempts: int, settings: Settings | None = None) -> bool:
    # Jobs get 3 attempts before they are parked (see settings).
    limit = (settings or load()).retry_limit
    return attempts < limit
