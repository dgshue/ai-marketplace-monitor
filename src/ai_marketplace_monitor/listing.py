import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple, Type
from urllib.parse import urlsplit

from diskcache import Cache  # type: ignore

from .utils import CacheType, cache, hash_dict

# Fields that must never reach ``Listing.hash``.
#
# The hash is the join key between a cached listing and its cached AI rating,
# so anything added to it after the fact orphans every rating recorded before
# the field existed. `image`/`images` were excluded for that reason and
# because the search page and the listing page disagree about them; the three
# timestamp fields are excluded for the same reason plus a sharper one --
# `first_seen` is bookkeeping about *us*, not about the listing, and a hash
# that moved when a listing was re-cached would re-rate the whole cache.
UNHASHED_FIELDS: Tuple[str, ...] = (
    "image",
    "images",
    "first_seen",
    "listed_at",
    "listed_text",
)


def canonical_url(marketplace: str, url: str, listing_id: str = "") -> str:
    """The plain, shareable address of a listing.

    Cached URLs carry whatever the search page attached on the day they were
    scraped -- Facebook adds `?ref=…&__tn__=!%3AD`, eBay a page-sized `_trk`
    string -- and those parameters are session junk: they identify the click
    that produced the link, they change on every load, and pasting one into a
    message shares that trail along with the listing. The canonical form is
    the one Facebook and eBay themselves resolve to.

    The original host is kept for eBay, so a `.co.uk` or `.de` listing does
    not get rewritten to the US site. Anything unrecognised is simply stripped
    of its query and fragment, which is the safe subset of the same idea.
    """
    base = (url or "").split("#")[0].split("?")[0]
    market = (marketplace or "").lower()
    listing_id = (listing_id or "").strip()
    if market == "facebook" and listing_id:
        return f"https://www.facebook.com/marketplace/item/{listing_id}/"
    if market == "ebay" and listing_id:
        host = ""
        try:
            host = urlsplit(base).netloc
        except ValueError:  # pragma: no cover - urlsplit is forgiving
            host = ""
        if not host.lower().endswith("ebay.com") and ".ebay." not in host.lower():
            host = "www.ebay.com"
        return f"https://{host}/itm/{listing_id}"
    return base


@dataclass
class Listing:
    marketplace: str
    name: str
    # unique identification
    id: str
    title: str
    image: str
    price: str
    post_url: str
    location: str
    seller: str
    condition: str
    description: str
    # Every photo in the listing's gallery, in page order, largest known
    # variant per photo. `image` is kept as the primary and is always
    # images[0] when the gallery is non-empty, so anything that only knows
    # about `image` (notifications, the email template, older caches) keeps
    # working unchanged. Defaulted because cached rows written before
    # multi-photo support have no `images` key at all.
    images: List[str] = field(default_factory=list)
    # When the seller posted it, as an absolute epoch. Marketplaces word this
    # relatively ("Listed 3 days ago"), which rots in a cache, so it is
    # resolved at scrape time and only the absolute value is kept. None means
    # the source did not say -- an old cached row, a page that omits it, or a
    # backend whose search tiles carry no date.
    listed_at: Optional[float] = None
    # The relative phrase the page actually used, kept for the record: it is
    # what a human would have read, and it is the only way to tell "Facebook
    # said 'over a week ago'" from "we computed a week".
    listed_text: str = ""
    # When *we* first saw it: stamped the first time the listing's details are
    # written to the cache and never moved afterwards. Answers "how long has
    # this been sitting in my queue", which is a different question from
    # `listed_at` and, together with it, says how quickly the monitor caught a
    # listing after it went up.
    first_seen: Optional[float] = None

    @property
    def photos(self: "Listing") -> List[str]:
        """The gallery as the UI and the photo proxy index it.

        Falls back to the single primary photo, so index 0 means the same
        thing for a row cached last month and one cached today.
        """
        urls = [url for url in (self.images or []) if url]
        if urls:
            return urls
        return [self.image] if self.image else []

    @property
    def canonical_url(self: "Listing") -> str:
        """This listing's shareable address. See the module-level function."""
        return canonical_url(self.marketplace, self.post_url, self.id)

    @property
    def content(self: "Listing") -> Tuple[str, str, str]:
        return (self.title, self.description, self.price)

    @property
    def hash(self: "Listing") -> str:
        # we need to normalize post_url before hashing because post_url will be different
        # each time from a search page. We also does not count image
        #
        # See UNHASHED_FIELDS for why the excluded ones are excluded.
        return hash_dict(
            {
                x: (y.split("?")[0] if x == "post_url" else y)
                for x, y in asdict(self).items()
                if x not in UNHASHED_FIELDS
            }
        )

    @classmethod
    def from_cache(
        cls: Type["Listing"],
        post_url: str,
        local_cache: Cache | None = None,
    ) -> Optional["Listing"]:
        try:
            # details could be a different datatype, miss some key etc.
            # and we have recently changed to save Listing as a dictionary
            return cls(
                **(cache if local_cache is None else local_cache).get(
                    (CacheType.LISTING_DETAILS.value, post_url.split("?")[0])
                )
            )
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

    def to_cache(
        self: "Listing",
        post_url: str,
        local_cache: Cache | None = None,
    ) -> None:
        store = cache if local_cache is None else local_cache
        key = (CacheType.LISTING_DETAILS.value, post_url.split("?")[0])
        record = asdict(self)
        # `first_seen` means "first", so the value already on disk wins over
        # anything this write carries. Re-caching happens on every price or
        # title change and every backfill, and each of those would otherwise
        # reset the clock and make an old listing look like a new find.
        stamp = self.first_seen
        try:
            previous = store.get(key)
        except KeyboardInterrupt:
            raise
        except Exception:
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("first_seen"), (int, float)):
            stamp = float(previous["first_seen"])
        if not isinstance(stamp, (int, float)):
            stamp = time.time()
        record["first_seen"] = float(stamp)
        self.first_seen = record["first_seen"]
        store.set(key, record, tag=CacheType.LISTING_DETAILS.value)
