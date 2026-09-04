from dataclasses import dataclass
from logging import Logger
from typing import Any, ClassVar, Dict, List, Tuple

import requests  # type: ignore

from .notification import ListingNotice, PushNotificationConfig, app_status_link
from .utils import hilight


@dataclass
class NtfyNotificationConfig(PushNotificationConfig):
    notify_method = "ntfy"
    required_fields: ClassVar[List[str]] = ["ntfy_server", "ntfy_topic"]

    message_format: str | None = None
    ntfy_server: str | None = None
    ntfy_topic: str | None = None
    # An access token for a server that does not allow anonymous publishing.
    # Sent as `Authorization: Bearer <token>`, which is ntfy's documented
    # scheme: https://docs.ntfy.sh/publish/#access-tokens
    ntfy_token: str | None = None

    def handle_ntfy_server(self: "NtfyNotificationConfig") -> None:
        if self.ntfy_server is None:
            return
        if not isinstance(self.ntfy_server, str) or not self.ntfy_server:
            raise ValueError("An non-empty ntfy_server is needed.")

        if not self.ntfy_server.startswith("https://") and not self.ntfy_server.startswith(
            "http://"
        ):
            raise ValueError("ntfy_server must start with https:// or http://")

    def handle_ntfy_topic(self: "NtfyNotificationConfig") -> None:
        if self.ntfy_topic is None:
            return

        if not isinstance(self.ntfy_topic, str) or not self.ntfy_topic:
            raise ValueError("user requires an non-empty ntfy_topic.")

        self.ntfy_topic = self.ntfy_topic.strip()

    def handle_ntfy_token(self: "NtfyNotificationConfig") -> None:
        if self.ntfy_token is None:
            return
        if not isinstance(self.ntfy_token, str) or not self.ntfy_token.strip():
            raise ValueError("ntfy_token must be a non-empty string.")
        self.ntfy_token = self.ntfy_token.strip()

    # -----------------------------------------------------------------
    # Endpoint and auth
    # -----------------------------------------------------------------
    def _endpoint(self: "NtfyNotificationConfig") -> Tuple[str, str, Dict[str, str]]:
        """(url, topic, headers) for a JSON publish.

        The JSON API posts to the server root with the topic in the body:
        https://docs.ntfy.sh/publish/#publish-as-json

        Two auth paths. The documented one is ``Authorization: Bearer <token>``
        from ``ntfy_token``. The other is backwards compatibility: this client
        used to send the body as a raw POST and could not attach a header, so
        the working deployment smuggles auth in as ``?auth=`` on the topic
        (``NTFY_TOPIC=mytopic?auth=QmVhcmVy...``), which ntfy accepts as a
        base64url-encoded Authorization header value. That query string is
        pulled off the topic here and re-attached to the request URL, so an
        existing config keeps publishing untouched until it is migrated.
        """
        server = (self.ntfy_server or "").rstrip("/")
        topic = self.ntfy_topic or ""
        query = ""
        # The query may be smuggled on either half of the old-style URL.
        if "?" in server:
            server, _, query = server.partition("?")
            server = server.rstrip("/")
        if "?" in topic:
            topic, _, topic_query = topic.partition("?")
            query = topic_query or query
        url = f"{server}/?{query}" if query else server + "/"
        headers = {"Content-Type": "application/json"}
        if self.ntfy_token:
            headers["Authorization"] = f"Bearer {self.ntfy_token}"
        return url, topic.strip("/"), headers

    def _publish(
        self: "NtfyNotificationConfig",
        payload: Dict[str, Any],
        logger: Logger | None = None,
    ) -> bool:
        url, topic, headers = self._endpoint()
        body = {"topic": topic, **payload}
        response = requests.post(url, json=body, headers=headers, timeout=10)
        # A deny-all server answers 401/403 rather than dropping the message;
        # surfacing it lets _execute_with_retry log and retry instead of
        # reporting a delivery that never happened.
        response.raise_for_status()
        if logger:
            logger.info(
                f"""{hilight("[Notify]", "succ")} Sent {self.name} an ntfy message {hilight(str(body.get("title", "")))}"""
            )
        return True

    # -----------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------
    def send_message(
        self: "NtfyNotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """A plain message -- block alerts and anything else not about a listing."""
        payload: Dict[str, Any] = {
            "title": title,
            "message": f"{message}\n\nSent by https://github.com/BoPeng/ai-marketplace-monitor",
            "markdown": self.message_format == "markdown",
        }
        # An alert is about the monitor, so it opens the monitor's own Status
        # screen -- where the block it is reporting can be cleared.
        status = app_status_link(self.app_url)
        if status:
            payload["click"] = status
            payload["actions"] = [{"action": "view", "label": "Open AIMM", "url": status}]
        return self._publish(payload, logger=logger)

    def send_listing(
        self: "NtfyNotificationConfig",
        notice: ListingNotice,
        logger: Logger | None = None,
    ) -> bool:
        """One listing as one ntfy notification, with both links as buttons."""
        message = notice.message
        if self.message_format == "markdown":
            # A lone newline is not a line break in Markdown, so the verdict,
            # the distance line and the AI comment would render as one run-on
            # paragraph in the ntfy web app. Two trailing spaces are Markdown's
            # hard break, and are invisible everywhere that shows plain text.
            message = message.replace("\n", "  \n")
        payload: Dict[str, Any] = {
            "title": notice.title,
            "message": message,
            "priority": notice.priority,
            "tags": notice.tags,
            # Tapping the body goes wherever the decision gets made: this app
            # when it is reachable, the marketplace otherwise.
            "click": notice.app_link or notice.listing_url,
            "actions": [
                {"action": "view", "label": "Open listing", "url": notice.listing_url},
            ],
        }
        if notice.app_link:
            payload["actions"].append(
                {"action": "view", "label": "Open in AIMM", "url": notice.app_link}
            )
        if notice.photo_url:
            # attach renders the photo inline; icon puts it beside the title
            # in the notification shade.
            payload["attach"] = notice.photo_url
            payload["icon"] = notice.photo_url
        if self.message_format == "markdown":
            payload["markdown"] = True
        return self._publish(payload, logger=logger)
