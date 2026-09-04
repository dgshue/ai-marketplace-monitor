import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields
from enum import Enum
from logging import Logger
from typing import Any, Callable, ClassVar, Deque, Dict, List, Optional, Set, Tuple, Type

from .ai import AIResponse  # type: ignore
from .geo import distance_from, resolve
from .listing import Listing
from .utils import BaseConfig, hilight


class NotificationStatus(Enum):
    NOT_NOTIFIED = 0
    EXPIRED = 1
    NOTIFIED = 2
    LISTING_CHANGED = 3
    LISTING_DISCOUNTED = 4


# ---------------------------------------------------------------------------
# One notification per listing
# ---------------------------------------------------------------------------
#
# Push backends used to send one digest per search -- "Found 6 new cars", six
# listings concatenated into a single body. A digest is unreadable on a phone
# (one thing to tap, six things behind it) and it has nowhere to put the two
# links that matter: the listing itself, and this app's own view of it. Every
# push backend now sends one notification per listing, built from the same
# ``ListingNotice``, so the wording is identical whichever backend a user has
# configured and only the transport differs. Email keeps digesting: six
# separate emails is the failure mode there, not the fix.


@dataclass
class NotifyContext:
    """What a notification needs that neither the listing nor the rating knows.

    ``notify_threshold`` is the item's notify rating, so a message can say
    "notify >= 4" and a 4/5 stops looking like an arbitrary number.
    ``home_location`` is the marketplace's ``home_location`` string; it is
    resolved once per run so every listing can report straight-line miles.

    Drive time is deliberately absent. The web UI has it, from a routing
    service call per listing, and a notification must not depend on that
    service being reachable -- nor wait on it while a search finishes.
    """

    notify_threshold: int | None = None
    home_location: str | None = None
    # Filled in by the backends: (marketplace, id) for every listing that
    # actually got through. ``User.notify`` records exactly these, so a
    # listing no backend managed to send is retried on the next search rather
    # than marked notified because a *different* listing succeeded. Empty
    # after a digest-only run (email reports no per-listing detail), which
    # the caller reads as "no opinion" and falls back to the old rule.
    sent: Set[Tuple[str, str]] = field(default_factory=set)


# A marketplace title is a keyword salad ("2014 Acura RLX SH-AWD w/Advance Pkg
# Navigation Sunroof CLEAN CARFAX") and a notification title gets one line on a
# lock screen. Truncate the title; never the price.
TITLE_MAX = 64

# The price leads, because it is the one field that decides whether the rest is
# worth reading. Facebook writes this when a listing has no price at all.
UNSPECIFIED_PRICE = "**unspecified**"

# A re-notification is not a new find, and the title is the only place that
# distinction survives a lock screen. NOT_NOTIFIED gets no prefix -- the common
# case stays "$4,500 - 2014 Acura RLX SH-AWD".
STATUS_PREFIX: Dict[NotificationStatus, str] = {
    NotificationStatus.EXPIRED: "Still listed",
    NotificationStatus.LISTING_CHANGED: "Updated",
    NotificationStatus.LISTING_DISCOUNTED: "Price drop",
    NotificationStatus.NOTIFIED: "Resend",
}


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ago(epoch: float, now: float | None = None) -> str:
    """Compact age: "3d", "5h", "20m" -- the web UI's fmtDur vocabulary."""
    seconds = max(0.0, (time.time() if now is None else now) - epoch)
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 172800:
        hours = f"{seconds / 3600:.1f}"
        return (hours[:-2] if hours.endswith(".0") else hours) + "h"
    return f"{round(seconds / 86400)}d"


def app_deep_link(app_url: str | None, marketplace: str, listing_id: str) -> str | None:
    """This listing's address inside the web UI, or None when no app_url is set.

    Mirrors the hash route the Triage UI parses in app-core.js. A hash route
    rather than a path, so the link needs no reverse-proxy rewrite and so an
    unauthenticated visit lands on the login screen with the target still in
    the URL, ready to open once the session exists.
    """
    if not app_url:
        return None
    return f"{app_url.rstrip('/')}/#listing/{marketplace}/{listing_id}"


def app_status_link(app_url: str | None) -> str | None:
    """The Status screen -- where a block alert is actually acted on."""
    return f"{app_url.rstrip('/')}/#status" if app_url else None


@dataclass
class ListingNotice:
    """One listing, formatted once, for whichever backends the user configured.

    Every field a push API might want is resolved here rather than inside the
    backends, so a Pushover user and an ntfy user read the same words and a
    change of wording is one edit instead of four.
    """

    listing: Listing
    status: NotificationStatus
    title: str
    message: str
    listing_url: str
    app_link: str | None
    photo_url: str | None
    priority: int
    tags: List[str]

    @property
    def links(self: "ListingNotice") -> List[str]:
        return [url for url in (self.listing_url, self.app_link) if url]

    @property
    def links_text(self: "ListingNotice") -> str:
        """The URLs as plain lines, for backends with no link fields."""
        return "\n".join(self.links)


def build_listing_notice(
    listing: Listing,
    rating: AIResponse,
    status: NotificationStatus = NotificationStatus.NOT_NOTIFIED,
    *,
    app_url: str | None = None,
    notify_threshold: int | None = None,
    distance_mi: float | None = None,
    description: str = "",
    now: float | None = None,
) -> ListingNotice:
    """Format one listing into the notification every push backend sends.

    Title:   ``$4,500 - 2014 Acura RLX SH-AWD``
    Message: ``5/5 Great deal - notify >= 4``
             ``12.4 mi - Asheboro, NC - listed 3d ago``
             the AI's one-line comment

    (the separator is actually a middle dot; ASCII here for the docstring.)
    """
    price = (listing.price or "").strip()
    if price == UNSPECIFIED_PRICE:
        price = ""
    title = " · ".join(
        part
        for part in (
            STATUS_PREFIX.get(status, ""),
            price,
            _truncate(listing.title, TITLE_MAX),
        )
        if part
    )

    lines: List[str] = []
    evaluated = rating is not None and rating.comment != AIResponse.NOT_EVALUATED
    if evaluated:
        verdict = f"{rating.score}/5 {rating.conclusion}"
        if notify_threshold:
            verdict += f" · notify ≥ {notify_threshold}"
        lines.append(verdict)

    meta: List[str] = []
    if distance_mi is not None:
        # "12.4 mi", but "12 mi" -- a trailing ".0" reads like false precision.
        meta.append(f"{distance_mi:g} mi")
    location = (listing.location or "").strip()
    if location and location != UNSPECIFIED_PRICE:
        meta.append(location)
    if isinstance(listing.listed_at, (int, float)) and listing.listed_at > 0:
        meta.append(f"listed {_ago(float(listing.listed_at), now)} ago")
    if meta:
        lines.append(" · ".join(meta))

    if evaluated and rating.comment:
        lines.append(rating.comment.strip())
    if description:
        lines.append(description.strip())

    photos = listing.photos
    return ListingNotice(
        listing=listing,
        status=status,
        title=title,
        message="\n".join(line for line in lines if line),
        listing_url=listing.canonical_url,
        app_link=app_deep_link(app_url, listing.marketplace, listing.id),
        # The listing's own CDN photo, not this app's snapshot proxy: the proxy
        # is behind the session cookie and the push service fetches the
        # attachment itself, with no cookie to present. Facebook's signed URLs
        # expire in hours, which is long after a notification has been read.
        photo_url=photos[0] if photos else None,
        # ntfy's scale: 3 is default, 4 breaks through Do Not Disturb on most
        # phones. Only a 5/5 earns that; everything else arrives quietly.
        priority=4 if evaluated and rating.score >= 5 else 3,
        tags=[tag for tag in (listing.name, listing.marketplace) if tag],
    )


@dataclass
class NotificationConfig(BaseConfig):
    required_fields: ClassVar[List[str]] = []

    max_retries: int = 5
    retry_delay: int = 60

    # Public address of this web UI, e.g. https://aimm.example.com. Lives on
    # the base class so it can be written once in ``[user.*]`` (UserConfig
    # inherits every notification config) and is still visible to each backend
    # object notify_all builds. Without it a notification still carries the
    # marketplace link -- it just has no second button back into the app.
    app_url: str | None = None

    # Rate limiting configuration (disabled by default, but public for user config)
    rate_limit_enabled: bool = False
    instance_rate_limit: float = 1.0  # seconds between sends per instance
    global_rate_limit: int = 10  # messages per second across all instances

    # Subclasses that handle rate limiting in their own send path (e.g.
    # Telegram's async _wait_for_rate_limit) should set this to True so
    # the base class _execute_with_retry does NOT also apply sync rate
    # limiting — preventing double-wait.
    _handles_own_rate_limiting: bool = False

    # Private tracking attributes
    _last_send_time: float | None = None

    # Class-level global tracking (shared across all notification types)
    _global_send_times: ClassVar[Deque[float]] = deque()
    _global_lock: ClassVar[threading.Lock] = threading.Lock()

    def handle_max_retries(self: "NotificationConfig") -> None:
        if not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be an integer.")

    def handle_retry_delay(self: "NotificationConfig") -> None:
        if not isinstance(self.retry_delay, int):
            raise ValueError("retry_delay must be an integer.")

    def handle_app_url(self: "NotificationConfig") -> None:
        if self.app_url is None:
            return
        if not isinstance(self.app_url, str) or not self.app_url.strip():
            raise ValueError("app_url must be a non-empty string.")
        self.app_url = self.app_url.strip().rstrip("/")
        if not self.app_url.startswith(("http://", "https://")):
            raise ValueError("app_url must start with http:// or https://")

    def _has_required_fields(self: "NotificationConfig") -> bool:
        return all(getattr(self, field, None) is not None for field in self.required_fields)

    @classmethod
    def get_config(
        cls: Type["NotificationConfig"], **kwargs: Any
    ) -> Optional["NotificationConfig"]:
        """Get the specific subclass name from the specified keys, for validation purposes"""
        for subclass in cls.__subclasses__():
            acceptable_keys = {field.name for field in fields(subclass)}
            if all(name in acceptable_keys for name in kwargs.keys()):
                return subclass(**{k: v for k, v in kwargs.items() if k != "type"})
            res = subclass.get_config(**kwargs)
            if res is not None:
                return res
        return None

    @classmethod
    def notify_all(
        cls: type["NotificationConfig"], config: "NotificationConfig", *args, **kwargs: Any
    ) -> bool:
        """Call the notify method of all subclasses"""
        succ = []
        for subclass in cls.__subclasses__():
            flds = {f.name for f in fields(subclass)}
            subclass_obj = subclass(**{k: getattr(config, k) for k in flds})
            if hasattr(subclass_obj, "notify") and subclass.__name__ not in [
                "UserConfig",
                "PushNotificationConfig",
            ]:
                assert hasattr(subclass_obj, "notify")
                succ.append(subclass_obj.notify(*args, **kwargs))
            # subclases
            if hasattr(subclass_obj, "notify_all"):
                succ.append(subclass.notify_all(config, *args, **kwargs))
        return any(succ)

    @classmethod
    def send_alert_all(
        cls: type["NotificationConfig"],
        config: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Send one plain message through every configured notifier.

        Separate from ``notify_all`` because an operational alert (the
        marketplace blocked us) has no listings and no AI ratings to format.
        Backends that never implemented ``send_message`` -- email builds its
        own MIME body instead -- are skipped rather than retried five times
        into a NotImplementedError.
        """
        succ = []
        for subclass in cls.__subclasses__():
            flds = {f.name for f in fields(subclass)}
            subclass_obj = subclass(**{k: getattr(config, k) for k in flds})
            sends_plain = (
                subclass.__name__ not in ("UserConfig", "PushNotificationConfig")
                and getattr(subclass, "send_message", None) is not NotificationConfig.send_message
            )
            if sends_plain and subclass_obj._has_required_fields():
                succ.append(subclass_obj.send_message_with_retry(title, message, logger=logger))
            succ.append(subclass.send_alert_all(config, title, message, logger=logger))
        return any(succ)

    def _execute_with_retry(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
        apply_rate_limiting: bool = False,
        send: Callable[[], bool] | None = None,
    ) -> bool:
        """Common retry logic for message sending with optional rate limiting.

        ``send`` overrides the call that is retried, so the per-listing path
        (``send_listing``) gets the same retry, rate-limit and logging
        treatment as the plain ``send_message`` one without duplicating it.
        ``title`` is still passed for the log line.
        """
        if not self._has_required_fields():
            return False

        for attempt in range(self.max_retries):
            try:
                # Apply rate limiting if requested
                if apply_rate_limiting and self.rate_limit_enabled:
                    self._wait_for_rate_limit_sync(logger)

                # Call the send_message method
                res = (
                    send()
                    if send is not None
                    else self.send_message(title=title, message=message, logger=logger)
                )

                if logger:
                    logger.info(
                        f"""{hilight("[Notify]", "succ")} Sent {self.name} a message with title {hilight(title)}"""
                    )
                return res
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if logger:
                    logger.debug(
                        f"""{hilight("[Notify]", "fail")} Attempt {attempt + 1} failed: {e}"""
                    )
                if attempt < self.max_retries - 1:
                    if logger:
                        logger.debug(
                            f"""{hilight("[Notify]", "fail")} Retrying in {self.retry_delay} seconds..."""
                        )
                    time.sleep(self.retry_delay)
                else:
                    if logger:
                        logger.error(
                            f"""{hilight("[Notify]", "fail")} Max retries reached. Failed to push note to {self.name}."""
                        )
                    return False
        return False

    def _send_message_with_rate_limiting_sync(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Sync version of send_message_with_retry with rate limiting support."""
        return self._execute_with_retry(title, message, logger, apply_rate_limiting=True)

    def send_message_with_retry(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        """Enhanced retry method with rate limiting support.

        Subclasses that set ``_handles_own_rate_limiting = True`` (e.g.
        Telegram, which applies async rate limiting inside its own
        ``send_message``) will NOT get sync rate limiting here —
        avoiding a double-wait.
        """
        apply = self.rate_limit_enabled and not self._handles_own_rate_limiting
        return self._execute_with_retry(title, message, logger, apply_rate_limiting=apply)

    def _get_wait_time(self: "NotificationConfig") -> float:
        """Calculate instance-level wait time. Override for custom logic."""
        if not self.rate_limit_enabled or self._last_send_time is None:
            return 0.0

        elapsed = time.time() - self._last_send_time
        return max(0.0, self.instance_rate_limit - elapsed)

    @classmethod
    def _get_global_wait_time(cls: Type["NotificationConfig"]) -> float:
        """Calculate global wait time across all instances.

        Note: this is only called from _wait_for_rate_limit[_sync] which
        already gates on rate_limit_enabled, so non-rate-limited instances
        never reach here and never populate _global_send_times.
        """
        with cls._global_lock:
            # Check if any instance has rate limiting enabled by checking if we have any tracked times
            # This is a more practical approach than checking class attributes
            if not cls._global_send_times:
                return 0.0

            current_time = time.time()

            # Remove timestamps older than 1 second
            while cls._global_send_times and current_time - cls._global_send_times[0] > 1.0:
                cls._global_send_times.popleft()

            # Use a reasonable default global rate limit (30 msg/sec like Telegram)
            # Individual classes can override this behavior
            global_rate_limit = getattr(cls, "global_rate_limit", 30)

            # If we have less than the rate limit, no wait needed
            if len(cls._global_send_times) < global_rate_limit:
                return 0.0

            # If we're at the limit, wait until the oldest message is more than 1 second old
            oldest_send_time = cls._global_send_times[0]
            wait_time = 1.0 - (current_time - oldest_send_time)
            return max(0.0, wait_time)

    @classmethod
    def _record_global_send_time(cls: Type["NotificationConfig"]) -> None:
        """Record the current time as a global send time."""
        with cls._global_lock:
            cls._global_send_times.append(time.time())

    def _wait_for_rate_limit_sync(
        self: "NotificationConfig", logger: Logger | None = None
    ) -> None:
        """Wait for rate limits and record send time (synchronous version)."""
        if not self.rate_limit_enabled:
            return

        # Check both per-instance and global rate limits
        instance_wait = self._get_wait_time()
        global_wait = self._get_global_wait_time()

        # Use the longer of the two wait times
        wait_time = max(instance_wait, global_wait)

        if wait_time > 0:
            if logger:
                if global_wait > instance_wait:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (global limit: {self.global_rate_limit}s)"
                    )
                else:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (instance limit: {self.instance_rate_limit}s)"
                    )

            time.sleep(wait_time)

        # Record both per-instance and global send times
        self._last_send_time = time.time()
        self._record_global_send_time()

    async def _wait_for_rate_limit(
        self: "NotificationConfig", logger: Logger | None = None
    ) -> None:
        """Wait for rate limits and record send time (async version for Telegram)."""
        if not self.rate_limit_enabled:
            return

        import asyncio

        # Check both per-instance and global rate limits
        instance_wait = self._get_wait_time()
        global_wait = self._get_global_wait_time()

        # Use the longer of the two wait times
        wait_time = max(instance_wait, global_wait)

        if wait_time > 0:
            if logger:
                if global_wait > instance_wait:
                    logger.debug(
                        f"Global rate limiting: waiting {wait_time:.1f} seconds (limit: {self.global_rate_limit} msg/sec)"
                    )
                else:
                    logger.debug(
                        f"Rate limiting: waiting {wait_time:.1f} seconds (limit: {self.instance_rate_limit}s)"
                    )

            await asyncio.sleep(wait_time)

        # Record both per-instance and global send times
        self._last_send_time = time.time()
        self._record_global_send_time()

    def send_message(
        self: "NotificationConfig",
        title: str,
        message: str,
        logger: Logger | None = None,
    ) -> bool:
        raise NotImplementedError("send_message needs to be defined.")


@dataclass
class PushNotificationConfig(NotificationConfig):
    notify_method = "push_notification"
    message_format: str | None = None
    with_description: int | None = None

    def handle_message_format(self: "PushNotificationConfig") -> None:
        if self.message_format is None:
            self.message_format = "plain_text"

        if self.message_format not in ["plain_text", "markdown", "html"]:
            raise ValueError("message_format must be 'plain_text', 'markdown', or 'html'.")

    def handle_with_description(self: "PushNotificationConfig") -> None:
        if self.with_description is None:
            return

        if self.with_description is True:
            self.with_description = 1
        elif self.with_description is False:
            self.with_description = 0

        if not isinstance(self.with_description, int) or self.with_description < 0:
            raise ValueError("with_description must be a boolean or a positive integer number.")

    def description_for(self: "PushNotificationConfig", listing: Listing) -> str:
        """The listing's own text, only when the user asked for it.

        A per-listing notification already carries the AI's one-line verdict;
        pasting a full marketplace description under it turns a glanceable card
        into a wall of text. So unset now means "no description" -- in the
        digest era it meant "all of it", which was tolerable only because a
        digest was already a wall of text. Set it to a character count for a
        clipped version, or to true for the whole thing.
        """
        if not self.with_description:
            return ""
        text = listing.description or ""
        if self.with_description == 1 or len(text) <= self.with_description:
            return text
        return text[: self.with_description] + "..."

    def send_listing(
        self: "PushNotificationConfig",
        notice: ListingNotice,
        logger: Logger | None = None,
    ) -> bool:
        """Default transport for one listing: title, message, links as text.

        Backends with first-class link support -- ntfy's actions, Pushover's
        ``url``, Pushbullet's link push -- override this. Everything else
        (Telegram today, anything added later) still gets the one-per-listing
        shape, with both addresses spelled out at the bottom.
        """
        links = notice.links_text
        separator = "<br><br>" if self.message_format == "html" else "\n\n"
        body = f"{notice.message}{separator}{links}" if notice.message else links
        return self.send_message(title=notice.title, message=body, logger=logger)

    def send_listing_with_retry(
        self: "PushNotificationConfig",
        notice: ListingNotice,
        logger: Logger | None = None,
    ) -> bool:
        apply = self.rate_limit_enabled and not self._handles_own_rate_limiting
        return self._execute_with_retry(
            notice.title,
            notice.message,
            logger,
            apply_rate_limiting=apply,
            send=lambda: self.send_listing(notice, logger=logger),
        )

    def notify(
        self: "PushNotificationConfig",
        listings: List[Listing],
        ratings: List[AIResponse],
        notification_status: List[NotificationStatus],
        force: bool = False,
        logger: Logger | None = None,
        context: NotifyContext | None = None,
    ) -> bool:
        """One notification per listing, not one digest per search.

        Returns True if at least one listing got through, which is what
        ``User.notify`` needs in order to record the notification. A backend
        that fails on listing three still notified about one and two.
        """
        if not self._has_required_fields():
            if logger:
                logger.debug(
                    f"Missing required fields  {', '.join(self.required_fields)}. No {self.notify_method} notification sent."
                )
            return False

        ctx = context or NotifyContext()
        # Geocode the origin once per run, not once per listing: resolve()
        # builds a city index on first use and every listing shares it.
        home = resolve(ctx.home_location) if ctx.home_location else None

        pending = [
            (listing, rating, ns)
            for listing, rating, ns in zip(listings, ratings, notification_status)
            if force or ns != NotificationStatus.NOTIFIED
        ]
        if not pending:
            if logger:
                logger.debug("No new listings to notify.")
            return False

        sent = 0
        for listing, rating, ns in pending:
            notice = build_listing_notice(
                listing,
                rating,
                ns,
                app_url=self.app_url,
                notify_threshold=ctx.notify_threshold,
                distance_mi=distance_from(home, listing.location or ""),
                description=self.description_for(listing),
            )
            if self.send_listing_with_retry(notice, logger=logger):
                sent += 1
                ctx.sent.add((listing.marketplace, listing.id))
                continue
            # That listing just burned max_retries x retry_delay seconds --
            # five minutes on the defaults. The backend is down, not unlucky,
            # and repeating that for every remaining listing would stall the
            # search loop for as many minutes again. Stop here; the ones that
            # were not sent stay un-notified, so the next search retries them.
            if logger:
                logger.error(
                    f"""{hilight("[Notify]", "fail")} {self.notify_method}: {sent} of """
                    f"""{len(pending)} listings sent; giving up on the rest of this batch."""
                )
            break
        return sent > 0
