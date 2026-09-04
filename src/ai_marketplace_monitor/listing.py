from dataclasses import asdict, dataclass, field
from typing import List, Optional, Tuple, Type

from diskcache import Cache  # type: ignore

from .utils import CacheType, cache, hash_dict


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
    def content(self: "Listing") -> Tuple[str, str, str]:
        return (self.title, self.description, self.price)

    @property
    def hash(self: "Listing") -> str:
        # we need to normalize post_url before hashing because post_url will be different
        # each time from a search page. We also does not count image
        #
        # `images` is excluded for the same reason as `image`, and for a
        # second one: the hash is the join key between a cached listing and
        # its cached AI rating, so letting a newly extracted gallery into it
        # would orphan every rating recorded before this field existed.
        return hash_dict(
            {
                x: (y.split("?")[0] if x == "post_url" else y)
                for x, y in asdict(self).items()
                if x not in ("image", "images")
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
        (cache if local_cache is None else local_cache).set(
            (CacheType.LISTING_DETAILS.value, post_url.split("?")[0]),
            asdict(self),
            tag=CacheType.LISTING_DETAILS.value,
        )
