"""Every platform module registers its connector with core.connectors on
import. Order here is the order Settings lists them."""
from core.connectors.platforms import (  # noqa: F401
    telegram, slack, email_imap, signal, bluebubbles, matrix, mattermost, irc, webhooks, notify,
)
