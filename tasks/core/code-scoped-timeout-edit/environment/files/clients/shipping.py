from clients.base import HttpClient

BASE_URL = "https://shipping.partner.example/api/v2"
TIMEOUT_SECONDS = 10


class ShippingClient(HttpClient):
    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        super().__init__(BASE_URL, timeout)
