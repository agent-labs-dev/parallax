from clients.base import HttpClient

BASE_URL = "https://inventory.partner.example/api/v2"
TIMEOUT_SECONDS = 10


class InventoryClient(HttpClient):
    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        super().__init__(BASE_URL, timeout)
