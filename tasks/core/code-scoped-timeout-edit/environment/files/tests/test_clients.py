from clients.billing import BillingClient
from clients.inventory import InventoryClient
from clients.shipping import ShippingClient


def test_each_client_has_its_own_base_url():
    urls = {c().base_url for c in (BillingClient, ShippingClient, InventoryClient)}
    assert len(urls) == 3


def test_timeout_can_be_overridden():
    assert ShippingClient(timeout=2).timeout == 2
