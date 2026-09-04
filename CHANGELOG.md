# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `with_description` is now **off by default** for push notifications. A per-listing message already
  carries the AI's one-line verdict; the full marketplace description under it turned a glanceable card
  into a wall of text. Set it to a character count, or to `true`, to get it back. Email is unaffected.
- Notification bookkeeping now follows the sends rather than the batch: a listing no backend managed
  to deliver stays un-notified and is retried on the next search, instead of being marked notified
  because a different listing in the same search succeeded. If a backend exhausts its retries on one
  listing it stops trying the rest of that batch — on the defaults each failure costs five minutes of
  sleeping, and repeating that per listing stalled the search loop.
- `geo.py` moved from `webui/` to the package root: distances are no longer only the activity view's
  business, and a notification should not have to import the whole FastAPI app to measure one.
- Web UI rebuilt mobile-first around a review queue: one listing at a time, swipe right to keep / left to
  dismiss, tap for details, Queue / Reviewed / All views, keyboard triage on desktop (`J`/`K`, `→`/`←`,
  `1`–`5`, `Enter`, `O`, `C`, `Z`, `H`, `R`, `?`), grouped-list Items / Sources / Status screens, and a web
  app manifest so it can be pinned to a phone home screen. Every previous capability (TOML editor, section
  forms, sources set-up, status, logs, CSV export, noVNC link) is carried over.

### Added
- **One notification per listing**, with a link to the listing *and* a link into this app. Push backends
  (ntfy, Pushover, Pushbullet, Telegram) no longer batch a search's finds into a single digest: each
  listing is its own notification, titled `$4,500 · 2014 Acura RLX SH-AWD`, with `5/5 Great deal ·
  notify ≥ 4`, straight-line distance from the marketplace's `home_location`, the location, `listed 3d
  ago`, and the AI's one-line comment. Email still digests — a separate email per listing is the failure
  mode there, not the fix. The wording is built once, in `build_listing_notice`, so every backend says
  the same thing and only the transport differs.
- `app_url` (new, optional, on `[user.*]` or any `[notification.*]` section): the public address of this
  web UI. With it set, every notification carries a second link — `<app_url>/#listing/<marketplace>/<id>`
  — and the web UI's router opens that listing's detail directly, in whichever tier it now lives (queue,
  reviewed, hidden, low, or under a paused item), clearing the filters that would otherwise hide it. An
  unknown id shows a "Listing not found" toast. The hash survives the login redirect, including
  proxy-auth deployments that have no login form.
- ntfy notifications are now published through the [JSON publish API](https://docs.ntfy.sh/publish/#publish-as-json)
  rather than as a raw POST body, which is what makes the rest possible: two `view` action buttons
  (**Open listing**, **Open in AIMM**), a `click` target, the listing's first photo as `attach` and
  `icon`, `tags` for the item and marketplace, and priority `4` for a 5/5 (`3` otherwise). Block alerts
  keep their single-message shape and gain a click through to the Status page.
- `ntfy_token` (new, optional): sent as `Authorization: Bearer <token>`, ntfy's documented scheme. A
  token smuggled onto the topic as `?auth=…` — the only option before this client could send headers —
  is still honoured: the query string is moved from the topic to the request URL, so an existing
  deployment keeps publishing until it migrates.

- Every listing now carries two clocks: **found** (when the monitor first cached its details) and
  **listed** (when the seller posted it). Facebook's listing page supplies the second one from the
  page's own inline `creation_time`, falling back to parsing the rendered "Listed 3 days ago in
  High Point, NC" line; eBay supplies it from `itemCreationDate` in API mode and from the tile's date
  on newest-first browser searches; Depop and Poshmark search tiles carry none. Relative wording is
  resolved to an absolute timestamp at scrape time (via `parsedatetime`) so it cannot drift as the
  cache ages, and non-English deployments can add the unit words to their `[translation.*]` section.
  `Listing` gains `listed_at`, `listed_text` and `first_seen`, none of which enter `Listing.hash`, so
  existing AI ratings keep their join. Review cards show `found 2h ago · listed 3d ago`, the detail
  view gains **Found** and **Listed** tiles with the absolute local time plus a `Caught in 1h 12m`
  line, and a **Recently listed** sort orders by the listing time with unknowns last. Listings cached
  before this shipped show `—` until the monitor re-reads their page.
- **Share** a listing out of the app: a button in the detail action bar and the `C` key. On a phone
  with `navigator.share` this opens the system share sheet with the price, title, location and
  distance; everywhere else it copies the link and confirms with a toast (with a textarea fallback for
  plain-http LAN addresses, where the clipboard API does not exist). The link is canonical —
  `https://www.facebook.com/marketplace/item/<id>/` or `https://<host>/itm/<id>` for eBay — so the
  `?ref=…&__tn__=…` click-tracking Facebook attaches never leaves the app. **Open ↗** uses the same
  URL; activity rows expose it as `url` and keep the original as `raw_url`.
- Facebook listings now carry their whole photo gallery instead of one thumbnail. The listing page's
  photos are read from the page already being visited (its inline `listing_photos` JSON, falling back
  to the rendered gallery), de-duplicated to the largest variant of each photo and capped at 12. The
  web UI's detail view shows them as a swipeable scroll-snap carousel with dots, desktop arrows, a
  tap-to-open full-screen lightbox and lazy slides; `,` / `.` (or `Shift`+`←`/`→`) move between
  photos, while `←` / `→` keep their meaning of dismiss / keep. `Listing` gains `images`, activity
  rows gain `images` and `image_count`, and `/api/listing-image` takes `i=<index>`.
- Option `max_images` (marketplace-level, overridable per item, default 6, maximum 12, `0` to
  disable): how many of a listing's photos to copy to disk once it reaches the review threshold.
  Facebook's image links are signed and expire within days, so photos are snapshotted in the
  background — two workers, off the search loop — while the listing is still fresh. eBay, Depop and
  Poshmark are read from search tiles that carry one photo each, so they are unaffected.
- Option `review_rating` (marketplace-level, overridable per item, 1-5, default 3) splitting the AI score
  into three tiers: below `review_rating` a listing is rated once, cached and only tracked; at or above it
  the listing enters the web UI's review queue; at or above `rating` it also sends a notification.
  A config with `review_rating` above `rating` is rejected with an error naming both. Activity rows and the
  per-item summary carry `review_threshold`, the new verdict `low` covers the bottom tier, and the review
  queue, the Reviewed list, the tab badge, the "N to review" line and the day counts all exclude it — the
  All view's new **Low** chip is the one place it surfaces. Item cards show both thresholds as steppers.
- `/api/listing/flag` accepts `kept`; user flags now carry `reviewed_at`, and activity rows expose both,
  so the review queue is derived from the user's own decisions (kept, hidden, or rated).

### Fixed
- Listing cards showed a **person instead of the item** — a Facebook friend's profile picture, or a
  family photo on a car listing. Both the search-tile parser and the listing-page scraper took the
  first `<img>` they found, which is the signed-in account's avatar whenever Facebook renders the
  page chrome and the seller's avatar on tiles that carry one. Of 204 listings cached before this
  fix, 49 had a profile picture as their photo and 24 had a video URL. Photos are now identified by
  their Facebook CDN photo type — profile pictures use the `-1` subtype (`t39.30808-1`, `t1.6435-1`)
  and videos come from `video-*.fbcdn.net` — and images inside a seller-profile link, a link to a
  different listing, or requested at icon size are excluded. A listing with no usable photo now
  shows no photo rather than a stranger's face. Email notifications, which embed `image`, were
  showing the same wrong pictures and are fixed by the same change. Already-cached listings are
  repaired on the next search pass, from the search tile and without an extra page load — so they
  regain a correct photo, but only one: the rest of the gallery exists only on the listing page,
  which a cached listing never revisits.
- Facebook vehicle listings cached a wrong price: vehicle detail pages have no price element, so the
  detail scraper fell back to the first `$...` in the seller's description — the down payment on a dealer
  listing (`$550` for a $5,500 Civic). The value is plausible, so the junk-artifact filter let it through.
  The search tile's price, which is what the AI was rated against, now wins outright whenever the tile has
  one. Already-cached listings self-heal on their next encounter.

## [0.10.2] - 2026-07-17

### Added
- Option `sort_by` to order Facebook search results by `suggested`, `new` (newest first), `price_ascend`, `price_descend`, or `distance_ascend` ([#323](https://github.com/BoPeng/ai-marketplace-monitor/issues/323))
- Web UI "Export CSV" button that downloads all found (notified) listings with link, price, rating, and details ([#334](https://github.com/BoPeng/ai-marketplace-monitor/issues/334))
- Docker image bundling Xvfb + Playwright Chromium + noVNC, with a "Browser" button in the web UI that exposes the live Chromium session for solving Facebook CAPTCHA / interactive logins ([#310](https://github.com/BoPeng/ai-marketplace-monitor/issues/310))
- GitHub Actions workflow publishing multi-arch (amd64/arm64) images to `ghcr.io/bopeng/ai-marketplace-monitor`

### Fixed
- WebUI startup failure on older FastAPI versions ([#315](https://github.com/BoPeng/ai-marketplace-monitor/pull/315))
- Stale runtime version reporting ([#314](https://github.com/BoPeng/ai-marketplace-monitor/pull/314))

### Documentation
- Note Python 3.10+ requirement in Quick Start ([#311](https://github.com/BoPeng/ai-marketplace-monitor/pull/311))
- Fix broken WEBUI.md link in README

## [0.10.1]

### Added
- Built-in web UI for config editing and live monitoring (FastAPI + CodeMirror)
- TOML syntax highlighting in config editor
- Live log streaming with filtering by level, item, AI score, and text
- Guided forms for adding/editing AI backends, items, users, and marketplaces
- `--webui-host` and `--webui-port` CLI options for remote access
- No password required on localhost; credentials required for remote access
- `FACEBOOK_USERNAME` / `FACEBOOK_PASSWORD` environment variable fallback for credentials
- Graceful handling of missing `${ENV_VAR}` references (warning instead of error)

## [0.10.0]

### Added
- Anthropic/Claude as an AI backend provider with support for Claude models (default: `claude-sonnet-4-20250514`)
- [issue 235](https://github.com/BoPeng/ai-marketplace-monitor/issues/235) Configurable rate limiting framework for all notification types
  - Rate limiting infrastructure moved from Telegram-specific to base notification class
  - Automatic rate limiting for Telegram with intelligent chat type detection (1.1s individual, 3.0s group)
  - Configurable instance-level and global rate limiting for all notification methods
  - Opt-in rate limiting for email, PushBullet, PushOver, and other notification types
  - Comprehensive test coverage for rate limiting behavior
- Support for `FACEBOOK_USERNAME` and `FACEBOOK_PASSWORD` environment variables as fallback credentials
- PyPI trusted publisher (OIDC) for release workflow

## [0.9.12]

- [Issue 289](https://github.com/BoPeng/ai-marketplace-monitor/issues/289). Fix 30s timeout delay in get_seller for anonymous mode.
- Change release workflow trigger from tag push to release creation.

## [0.9.11]

- [Issue 264](https://github.com/BoPeng/ai-marketplace-monitor/pull/264). Support different browsers.

## [0.9.10]

- [Issue 264](https://github.com/BoPeng/ai-marketplace-monitor/pull/264). Validate `search_city`.

## [0.9.9]

- [Issue 259](https://github.com/BoPeng/ai-marketplace-monitor/pull/259). Disallow keyboard monitoring by default.

## [0.9.8]

- [Issue 248](https://github.com/BoPeng/ai-marketplace-monitor/pull/248). Fix an issue with premature keyword filtering. Thanks to @adawalli

## [0.9.7]

- Add support for telegram [PR 231](https://github.com/BoPeng/ai-marketplace-monitor/pull/231). thanks to @adawalli

## [0.9.6]

- Fix searching across regions.
- Switch from `poetry` to `uv` for development.

## [0.9.5]

- [issue 155](https://github.com/BoPeng/ai-marketplace-monitor/issues/155) Fix output of pushbullet
- [issue 150](https://github.com/BoPeng/ai-marketplace-monitor/issues/150) Support option `category`

## [0.9.4] - 2025-04-15

- [issue 132](https://github.com/BoPeng/ai-marketplace-monitor/issues/132) Improve PushOver notification

## [0.9.3] - 2025-04-15

- [issue 102](https://github.com/BoPeng/ai-marketplace-monitor/issues/102) Fix pushover support and add more documentation

## [0.9.2] - 2025-04-07

- [issue 122](https://github.com/BoPeng/ai-marketplace-monitor/issues/122) Support searching across regions with different currencies

## [0.9.1] - 2025-03-13

- Re-release AI Marketplace Monitor under a AGPL license

## [0.8.8] - 2025-03-12

- Allow option date_listed to accept numeric value #96
- Fix importing pushover #91

## [0.8.6] - 2025-03-03

- Allow support for multiple languages.

## [0.8.5] - 2025-03-03

- Allow [pushover](https://pushover.net/) notification

## [0.8.2] - 2025-03-02

- Reorganize notification settings
- Support the use of environment variables for passwords
- Support browser proxy

**BREAKING CHANGES**

- Rename `smtp` sections to `notification`
- Rename parameter `smtp` to `notify_with`

## [0.7.11] - 2025-03-01

- Fix a bug on the handling of logical expressions for `keywords` and `antikeywords`.
- Add support for another auto layout page

## [0.8.9] - 2025-02-21

- Add options `prompt`, `extra_prompt` and `rating_prompt`

## [0.7.7] - 2025-02-17

- Expand the use of `enabled=False` to all sections
- Allow complex `AND` `OR` and `NOT` operations for `keywords` and `antikeywords`.

## [0.7.4] - 2025-02-10

- Rename `keywords` to `search_phrases`, `include_keywords` to `keywords` and `exclude_keywords` to `antikeywords` [#45]
- Separate statistics by item name [#46]

## [0.7.3] - 2025-02-07

- Allow email notification

## [0.7.0] - 2025-02-06

- Re-retrieve details of listings if there are title or price change
- Allow sending reminders for available items after specified time. (#41)
- Display counters

## [0.6.5] - 2025-02-05

- Allow checking URLs during monitoring (#34)
- Add option `ai` that allows the specification of AI models to use for certain marketplaces or items.
- Support locally hosted Ollama models
- Support DeepSeek-r1 model with `<think>` tags.
- Add option `timeout` to AI request.
- Expand command line option `--clear-cache`

## [0.6.2] - 2025-02-03

- Support extracting details from automobile listings.

## [0.6.1] - 2025-02-02

- Allow multiple `start_at`

## [0.6.0] - 2025-02-01

- Allow some parameters to different from initial and subsequent searches.
- Allow the AI to return a rating and some comments, and use the rating to determine if the user should be notified.

## [0.5.3] - 2025-01-31

- Add command line option `--diable-javascript` which can be helpful in some cases.
- Add option `include_keywords` to fine-tune the behavior of `keywords`.
- Add option `provider` to allow the specfication of more AI service providers.
- Allow `market_type` to marketplaces and allow multiple marketplaces.

## [0.5.1] - 2025-01-30

- Change the unit of `search-interval` to seconds to allow for more frequent search, although that is not recommended.
- Rename option `acceptable_locations` to `seller_locations`

## [0.5.0] - 2025-01-29

- Allow each time to add its own `search_interval`
- Add options such as `delivery_method`, `radius`, and `condition`
- Add options to define and use regions for searching large regions

## [0.4.5] - 2025-01-27

- Add option `--check` and `--for` to check particular listings

## [0.4.3] - 2025-01-26

- Add support for DeepSeek

## [0.4.0] - 2025-01-25

- Allow section `[ai.openai]`
- Use openAI to confirm if the item matches what user requests
- Slightly better logging

## [0.3.3] - 2025-01-21

- Allow option `enabled` for items
- Notify all users if no `notify` is specified for item or marketplace
- Compare string after normalization (#8)
- Stop sleeping if config files are changed. Allowing more interactive modification of search terms.
- Give more time after logging in, allow option `login_wait_time`.
- Allow entering username and password manually

## [0.2.0] - 2025-01-21

- Allow the definition of a reusable config file from `~/.ai-marketplace-monitor/config.toml`
- Allow options `exclude_sellers` and `exclude_by_description`
- Fix a bug that prevents the sending of phone notification

## [0.1.0] - 2025-01-20

### Added

- First release on PyPI.

[Unreleased]: https://github.com/BoPeng/ai-marketplace-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/BoPeng/ai-marketplace-monitor/compare/releases/tag/v0.1.0
