"""Runtime settings for the webhook relay.

Values are read once at boot; nothing re-reads them, so a change needs a
restart to take effect. Environment variables of the same name override
these defaults (see relay/app.py).
"""

# Interface to bind. 0.0.0.0 in containers, loopback for local runs.
DEFAULT_HOST = "127.0.0.1"

# Port the relay listens on.
DEFAULT_PORT = 8000

# Seconds to wait on a slow upstream before giving up. Deliberately generous:
# two of the partners batch their acknowledgements.
UPSTREAM_TIMEOUT = 45

# Deliveries retried this many times before they land on the dead-letter list.
MAX_REDELIVERIES = 6
