from dispatch.settings import Settings
from dispatch.worker import should_retry

FAST = Settings(queue_name="test", retry_limit=2, backoff_s=0)


def test_gives_up_after_limit():
    assert should_retry(1, FAST)
    assert not should_retry(2, FAST)
