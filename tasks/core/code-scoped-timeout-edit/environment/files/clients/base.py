"""Shared HTTP plumbing for the partner-API clients."""

from dataclasses import dataclass


@dataclass
class HttpClient:
    base_url: str
    timeout: float

    def describe(self) -> str:
        return f"{self.base_url} (timeout {self.timeout:g}s)"
