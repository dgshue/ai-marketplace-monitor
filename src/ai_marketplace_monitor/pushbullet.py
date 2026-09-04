from dataclasses import dataclass
from logging import Logger
from typing import ClassVar, List

from pushbullet import Pushbullet  # type: ignore

from .notification import ListingNotice, PushNotificationConfig
from .utils import hilight


@dataclass
class PushbulletNotificationConfig(PushNotificationConfig):
    notify_method = "pushbullet"
    required_fields: ClassVar[List[str]] = ["pushbullet_token"]

    pushbullet_token: str | None = None
    pushbullet_proxy_type: str | None = None
    pushbullet_proxy_server: str | None = None

    def handle_pushbullet_token(self: "PushbulletNotificationConfig") -> None:
        if self.pushbullet_token is None:
            return

        if not isinstance(self.pushbullet_token, str) or not self.pushbullet_token:
            raise ValueError("An non-empty pushbullet_token is needed.")
        self.pushbullet_token = self.pushbullet_token.strip()

    def handle_pushbullet_proxy_type(self: "PushbulletNotificationConfig") -> None:
        if self.pushbullet_proxy_type is None:
            return
        if not isinstance(self.pushbullet_proxy_type, str) or not self.pushbullet_proxy_type:
            raise ValueError("user requires an non-empty pushbullet_proxy_type.")
        self.pushbullet_proxy_type = self.pushbullet_proxy_type.strip()

    def handle_pushbullet_proxy_server(self: "PushbulletNotificationConfig") -> None:
        # pushbullet_proxy_server and pushbullet_proxy_type are both required to be set
        # if either of them is set, then both of them must be set
        if self.pushbullet_proxy_type is None and self.pushbullet_proxy_server is not None:
            raise ValueError(
                "user requires an non-empty pushbullet_proxy_type when pushbullet_proxy_server is set."
            )
        # if pushbullet_proxy_type is set, then pushbullet_proxy_server must be set
        if self.pushbullet_proxy_type is not None and self.pushbullet_proxy_server is None:
            raise ValueError(
                "user requires an non-empty pushbullet_proxy_server when pushbullet_proxy_type is set."
            )
        if self.pushbullet_proxy_server is None:
            return
        if not isinstance(self.pushbullet_proxy_server, str) or not self.pushbullet_proxy_server:
            raise ValueError("user requires an non-empty pushbullet_proxy_server.")
        self.pushbullet_proxy_server = self.pushbullet_proxy_server.strip()

    def handle_message_format(self: "PushbulletNotificationConfig") -> None:
        self.message_format = "plain_text"

    def _client(self: "PushbulletNotificationConfig") -> Pushbullet:
        return Pushbullet(
            self.pushbullet_token,
            proxy=(
                {self.pushbullet_proxy_type: self.pushbullet_proxy_server}
                if self.pushbullet_proxy_server and self.pushbullet_proxy_type
                else None
            ),
        )

    def send_listing(
        self: "PushbulletNotificationConfig",
        notice: ListingNotice,
        logger: Logger | None = None,
    ) -> bool:
        """One listing as a Pushbullet *link* push, not a note.

        A link push makes the whole notification tappable and opens the
        listing; the app's own address goes in the body, since Pushbullet
        carries exactly one URL per push.
        """
        body = notice.message
        if notice.app_link:
            body = f"{body}\n\nOpen in AIMM: {notice.app_link}" if body else notice.app_link
        self._client().push_link(notice.title, notice.listing_url, body)
        if logger:
            logger.info(
                f"""{hilight("[Notify]", "succ")} Sent {self.name} a link push {hilight(notice.title)}"""
            )
        return True

    def send_message(
        self: "PushbulletNotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        pb = self._client()
        pb.push_note(
            title, message + "\n\nSent by https://github.com/BoPeng/ai-marketplace-monitor"
        )
        if logger:
            logger.info(
                f"""{hilight("[Notify]", "succ")} Sent {self.name} a message with title {hilight(title)}"""
            )
        return True
