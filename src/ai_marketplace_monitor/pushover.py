import html
import http.client
import json
import urllib
from dataclasses import dataclass
from logging import Logger
from typing import ClassVar, List

from .notification import ListingNotice, PushNotificationConfig
from .utils import hilight


@dataclass
class PushoverNotificationConfig(PushNotificationConfig):
    notify_method = "pushover"
    required_fields: ClassVar[List[str]] = ["pushover_user_key", "pushover_api_token"]

    pushover_user_key: str | None = None
    pushover_api_token: str | None = None

    def handle_pushover_user_key(self: "PushoverNotificationConfig") -> None:
        if self.pushover_user_key is None:
            return
        if not isinstance(self.pushover_user_key, str) or not self.pushover_user_key:
            raise ValueError("An non-empty pushover_user_key is needed.")
        self.pushover_user_key = self.pushover_user_key.strip()

    def handle_pushover_api_token(self: "PushoverNotificationConfig") -> None:
        if self.pushover_api_token is None:
            return

        if not isinstance(self.pushover_api_token, str) or not self.pushover_api_token:
            raise ValueError("user requires an non-empty pushover_api_token.")
        self.pushover_api_token = self.pushover_api_token.strip()

    def handle_message_format(self: "PushoverNotificationConfig") -> None:
        self.message_format = "html"

    # Pushover truncates a message at 1024 characters and a title at 250.
    MESSAGE_LIMIT: ClassVar[int] = 1024
    TITLE_LIMIT: ClassVar[int] = 250

    def _post(
        self: "PushoverNotificationConfig",
        params: dict,
        logger: Logger | None = None,
    ) -> bool:
        conn = http.client.HTTPSConnection("api.pushover.net:443")
        conn.request(
            "POST",
            "/1/messages.json",
            urllib.parse.urlencode(
                {"token": self.pushover_api_token, "user": self.pushover_user_key, **params}
            ),
            {"Content-type": "application/x-www-form-urlencoded"},
        )
        output = conn.getresponse().read().decode("utf-8")
        data = json.loads(output)
        if data["status"] != 1:
            raise RuntimeError(output)
        if logger:
            logger.info(
                f"""{hilight("[Notify]", "succ")} Sent {self.name} a message with title {hilight(str(params.get("title", "")))}"""
            )
        return True

    def send_listing(
        self: "PushoverNotificationConfig",
        notice: ListingNotice,
        logger: Logger | None = None,
    ) -> bool:
        """One listing, one push, with the listing as the notification's URL.

        Pushover gives a message exactly one first-class link (``url`` plus
        ``url_title``), so the marketplace listing takes it -- that is the
        thing you cannot get to any other way -- and the app link rides in the
        body as an anchor, which the Pushover clients render in HTML mode.
        """
        body = html.escape(notice.message).replace("\n", "<br>")
        if notice.app_link:
            body += f'<br><br><a href="{notice.app_link}">Open in AIMM</a>'
        return self._post(
            {
                "title": notice.title[: self.TITLE_LIMIT],
                "message": body[: self.MESSAGE_LIMIT],
                "html": 1,
                "url": notice.listing_url,
                "url_title": "Open listing",
                # Pushover's scale is -2..2, not ntfy's 1..5: 1 is "high",
                # which bypasses the user's quiet hours. Only a 5/5 gets it.
                "priority": 1 if notice.priority >= 4 else 0,
            },
            logger=logger,
        )

    def send_message(
        self: "PushoverNotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        # pushover has a limit of 1024 characters, so we will need to split the message
        # into multiple ones, by
        # 1. split by '\n\n' which separates listings
        # 2. put as many listings as possible into one message and continue
        msgs: List[str] = []
        signature = 'Sent by <a href="https://github.com/BoPeng/ai-marketplace-monitor">AI Marketplace Monitor</a>'
        for pieces in [*message.split("\n\n"), signature]:
            if len(pieces) > 1024:
                pieces = pieces[:1024]
            if not msgs:
                msgs.append(pieces)
                continue
            if len(msgs[-1] + "<br><br>" + pieces) > 1024:
                msgs.append(pieces)
            else:
                msgs[-1] += "<br><br>" + pieces

        conn = http.client.HTTPSConnection("api.pushover.net:443")
        for idx, msg in enumerate(msgs):
            conn.request(
                "POST",
                "/1/messages.json",
                urllib.parse.urlencode(
                    {
                        "token": self.pushover_api_token,
                        "user": self.pushover_user_key,
                        "message": msg,
                        "title": title + (f" ({idx + 1}/{len(msgs)})" if len(msgs) > 1 else ""),
                        "html": 1,
                    }
                ),
                {"Content-type": "application/x-www-form-urlencoded"},
            )

            output = conn.getresponse().read().decode("utf-8")
            data = json.loads(output)
            if data["status"] != 1:
                raise RuntimeError(output)

        if logger:
            logger.info(
                f"""{hilight("[Notify]", "succ")} Sent {self.name} a message {hilight(msg)}"""
            )
        return True
