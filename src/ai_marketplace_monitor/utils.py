import copy
import datetime
import hashlib
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, fields
from enum import Enum
from logging import Logger
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple, TypeVar

import parsedatetime  # type: ignore
import requests  # type: ignore
import rich
from diskcache import Cache  # type: ignore
from playwright.sync_api import ProxySettings
from pyparsing import (
    CharsNotIn,
    Keyword,
    ParserElement,
    ParseResults,
    Word,
    alphanums,
    infix_notation,
    opAssoc,
)
from requests.exceptions import RequestException, Timeout  # type: ignore
from rich.pretty import pretty_repr

try:
    from pynput import keyboard  # type: ignore

    pynput_enabled = os.environ.get("DISABLE_PYNPUT", "").lower() not in ("1", "y", "true", "yes")
except ImportError:
    # some platforms are not supported
    pynput_enabled = False

import io

import rich.pretty
from PIL import Image
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# home directory for all settings and caches
amm_home = Path.home() / ".ai-marketplace-monitor"
amm_home.mkdir(parents=True, exist_ok=True)

cache = Cache(amm_home)

# Playwright storage_state (cookies + localStorage) for the marketplace session.
#
# Without this the browser is launched non-persistently and every context is
# blank, so each restart begins logged out and Facebook re-runs 2FA -- an SMS
# code that has to be typed by hand through the noVNC view, which makes an
# unattended restart impossible.
#
# SENSITIVE: this file is a live logged-in session. Anyone who can read it is
# you, without needing the password or the second factor. It is written 0600
# and lives in amm_home alongside the cache.
browser_state_file = amm_home / "browser-state.json"

# Operator state that MUST outlive the process: the manual pause and any
# marketplace block cooldown. Both used to be in-memory only, so a restart --
# or a container recreation, which happens on every deploy -- silently resumed
# searching. That resumed hitting Facebook while the account was blocked.
# Not sensitive: no credentials, just flags and timestamps.
monitor_state_file = amm_home / "monitor-state.json"


def read_monitor_state(path: Path | None = None) -> Dict[str, Any]:
    """Load persisted monitor state, or an empty dict if there is none.

    Never raises: a missing, unreadable, or malformed file just means "no
    remembered state", which is the same situation as a first run.
    """
    target = monitor_state_file if path is None else path
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except KeyboardInterrupt:
        raise
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_monitor_state(state: Dict[str, Any], path: Path | None = None) -> bool:
    """Persist monitor state atomically. Returns whether it was written.

    Written via a temp file and replaced, so a kill mid-write cannot leave a
    truncated file that reads as "not paused" on the next start.
    """
    target = monitor_state_file if path is None else path
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
        return True
    except KeyboardInterrupt:
        raise
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


TConfigType = TypeVar("TConfigType", bound="BaseConfig")


class SleepStatus(Enum):
    NOT_DISRUPTED = 0
    BY_KEYBOARD = 1
    BY_FILE_CHANGE = 2


def aimm_event(kind: str, **fields: Any) -> Dict[str, Any]:
    """Build a structured-event payload for a log call.

    Usage:
        logger.info(message, extra=aimm_event("ai_eval", score=5, ...))

    The web UI surfaces these structured fields in its filter dropdowns
    (kind / item / score) and in the expand-row detail pane.
    """
    return {"aimm": {"kind": kind, **fields}}


class CacheType(Enum):
    LISTING_DETAILS = "listing-details"
    AI_INQUIRY = "ai-inquiries"
    USER_NOTIFIED = "user-notifications"
    COUNTERS = "counters"
    # Per-listing state the user sets in the web UI: their own 1-5 rank and a
    # hidden flag ("off my radar, still tracked"). Key: (tag, marketplace, id).
    USER_FLAGS = "user-flags"
    # Direct (marketplace, listing_id, item) -> AIResponse mirror of AI_INQUIRY.
    # AI_INQUIRY is keyed by Listing.hash, which covers every field -- so any
    # drift between the listing as rated and the listing as cached (vehicle
    # pages yield no price to the detail scraper, for one) silently unlinks a
    # rating from its listing. This key cannot drift.
    AI_BY_LISTING = "ai-by-listing"


class CounterItem(Enum):
    SEARCH_PERFORMED = "Search performed"
    LISTING_EXAMINED = "Total listing examined"
    LISTING_QUERY = "New listing fetched"
    EXCLUDED_LISTING = "Listing excluded"
    NEW_VALIDATED_LISTING = "New validated listing"
    AI_QUERY = "Total AI Queries"
    NEW_AI_QUERY = "New AI Queries"
    FAILED_AI_QUERY = "Failed AI Queries)"
    NOTIFICATIONS_SENT = "Notifications sent"
    REMINDERS_SENT = "Reminders sent"


class Currency(Enum):
    USD = "USD"
    JPY = "JPY"
    BGN = "BGN"
    CYP = "CYP"
    EUR = "EUR"
    CZK = "CZK"
    DKK = "DKK"
    EEK = "EEK"
    GBP = "GBP"
    HUF = "HUF"
    LTL = "LTL"
    LVL = "LVL"
    MTL = "MTL"
    PLN = "PLN"
    ROL = "ROL"
    RON = "RON"
    SEK = "SEK"
    SIT = "SIT"
    SKK = "SKK"
    CHF = "CHF"
    ISK = "ISK"
    NOK = "NOK"
    HRK = "HRK"
    RUB = "RUB"
    TRL = "TRL"
    TRY = "TRY"
    AUD = "AUD"
    BRL = "BRL"
    CAD = "CAD"
    CNY = "CNY"
    HKD = "HKD"
    IDR = "IDR"
    ILS = "ILS"
    INR = "INR"
    KRW = "KRW"
    MXN = "MXN"
    MYR = "MYR"
    NZD = "NZD"
    PHP = "PHP"
    SGD = "SGD"
    THB = "THB"
    ZAR = "ZAR"
    ARS_unsupported = "ARS"


class KeyboardMonitor:
    confirm_character = "c"

    def __init__(self: "KeyboardMonitor") -> None:
        self._paused: bool = False
        self._listener: keyboard.Listener | None = None
        self._sleeping: bool = False
        self._confirmed: bool | None = None

    def start(self: "KeyboardMonitor") -> None:
        if pynput_enabled:
            self._listener = keyboard.Listener(on_press=self.handle_key_press)
            self._listener.start()  # start to listen on a separate thread

    def stop(self: "KeyboardMonitor") -> None:
        if self._listener:
            self._listener.stop()  # stop the listener

    def start_sleeping(self: "KeyboardMonitor") -> None:
        self._sleeping = True

    def confirm(self: "KeyboardMonitor", msg: str | None = None) -> bool:
        self._confirmed = False
        rich.print(
            msg
            or f"Press {hilight(self.confirm_character)} to enter interactive mode in 10 seconds: ",
            end="",
            flush=True,
        )
        try:
            count = 0
            while self._confirmed is False:
                time.sleep(0.1)
                if self._confirmed:
                    return True
                count += 1
                # wait a total of 10s
                if count > 100:
                    break
            return self._confirmed
        finally:
            # whether or not confirm is successful, reset paused and confirmed flag
            self._paused = False
            self._confirmed = None

    def is_sleeping(self: "KeyboardMonitor") -> bool:
        return self._sleeping

    def is_paused(self: "KeyboardMonitor") -> bool:
        return self._paused

    def is_confirmed(self: "KeyboardMonitor") -> bool:
        return self._confirmed is True

    def set_paused(self: "KeyboardMonitor", paused: bool = True) -> None:
        self._paused = paused

    if pynput_enabled:

        def handle_key_press(
            self: "KeyboardMonitor", key: keyboard.Key | keyboard.KeyCode | None
        ) -> None:
            # is sleeping, wake up
            if self._sleeping:
                if key == keyboard.Key.esc:
                    self._sleeping = False
                    return
            # if waiting for confirmation, set confirm
            if self._confirmed is False:
                if getattr(key, "char", "") == self.confirm_character:
                    self._confirmed = True
                    return
            # if being paused
            if self.is_paused():
                if key == keyboard.Key.esc:
                    print("Still searching ... will pause as soon as I am done.")
                    return
            if key == keyboard.Key.esc:
                print("Pausing search ...")
                self._paused = True


class Counter:
    def increment(self: "Counter", counter_key: CounterItem, item_name: str, by: int = 1) -> None:
        key = (CacheType.COUNTERS.value, counter_key.value, item_name)
        try:
            cache.incr(key, by, default=None)
        except KeyError:
            # if key does not exist, set it to by, and set tag
            cache.set(key, by, tag=CacheType.COUNTERS.value)

    def __str__(self: "Counter") -> str:
        """Return pretty form of all non-zero counters"""
        # this is super inefficient. Thankfully we are not calling this often.
        # See https://github.com/grantjenks/python-diskcache/issues/341
        # for details
        counters = {
            key: cache.get(key) for key in cache.iterkeys() if key[0] == CacheType.COUNTERS.value
        }
        item_names = {x[2] for x in counters.keys()}
        cnts = {}
        for item_name in item_names:
            # per-item statistics
            cnts[item_name] = {
                x.value: counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for x in CounterItem
                if counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
            }
        # total statistics
        cnts["Total"] = {
            x.value: sum(
                counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for item_name in item_names
            )
            for x in CounterItem
            if sum(
                counters.get((CacheType.COUNTERS.value, x.value, item_name), 0)
                for item_name in item_names
            )
        }
        return pretty_repr(cnts)


counter = Counter()


def hash_dict(obj: Dict[str, Any]) -> str:
    """Hash a dictionary to a string."""
    dict_string = json.dumps(obj).encode("utf-8")
    return hashlib.sha256(dict_string).hexdigest()


@dataclass
class BaseConfig:
    name: str
    enabled: bool | None = None

    def __post_init__(self: "BaseConfig") -> None:
        """Handle all methods that start with 'handle_' in the dataclass."""
        for f in fields(self):
            # test the type of field f, if it is a string or a list of string
            # try to expand the string with environment variables
            fvalue = getattr(self, f.name)
            if isinstance(fvalue, str):
                setattr(self, f.name, self._value_from_environ(fvalue))
            elif isinstance(fvalue, list) and all(isinstance(x, str) for x in fvalue):
                setattr(self, f.name, [self._value_from_environ(x) for x in fvalue])

            handle_method = getattr(self, f"handle_{f.name}", None)
            if handle_method:
                handle_method()

    def _value_from_environ(self: "BaseConfig", key: str) -> str | None:
        """Replace key with value from an environment variable if it has a format of ${KEY}.

        Returns None (with a warning) when the variable is not set, so
        that optional credentials degrade gracefully to anonymous mode.
        """
        if not isinstance(key, str) or not key.startswith("${") or not key.endswith("}"):
            return key
        var_name = key[2:-1]
        if var_name not in os.environ:
            import warnings

            warnings.warn(
                f"Environment variable {var_name} is not set — ignored.",
                stacklevel=2,
            )
            return None
        return os.environ[var_name]

    def handle_enabled(self: "BaseConfig") -> None:
        if self.enabled is None:
            return
        if not isinstance(self.enabled, bool):
            raise ValueError(f"Item {hilight(self.name)} enabled must be a boolean.")

    @property
    def hash(self: "BaseConfig") -> str:
        return hash_dict(asdict(self))


@dataclass
class MonitorConfig(BaseConfig):
    proxy_server: List[str] | None = None
    proxy_bypass: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None

    def handle_proxy_server(self: "MonitorConfig") -> None:
        if self.proxy_server is None:
            return

        if isinstance(self.proxy_server, str):
            self.proxy_server = [self.proxy_server]

        if not all(isinstance(x, str) for x in self.proxy_server):
            raise ValueError(f"Item {hilight(self.name)} proxy_server must be a string.")
        if not all(x.startswith("http://") or x.startswith("https://") for x in self.proxy_server):
            raise ValueError(
                f"Item {hilight(self.name)} proxy_server must start with http:// or https://"
            )

    def handle_proxy_bypass(self: "MonitorConfig") -> None:
        if self.proxy_bypass is None:
            return
        if not isinstance(self.proxy_bypass, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_bypass must be a string.")

    def handle_proxy_username(self: "MonitorConfig") -> None:
        if self.proxy_username is None:
            return

        if not isinstance(self.proxy_username, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_username must be a string.")

    def handle_proxy_password(self: "MonitorConfig") -> None:
        if self.proxy_password is None:
            return

        if not isinstance(self.proxy_password, str):
            raise ValueError(f"Item {hilight(self.name)} proxy_password must be a string.")

    def get_proxy_options(self: "MonitorConfig") -> ProxySettings | None:
        if not self.proxy_server:
            return None
        res = ProxySettings(server=random.choice(self.proxy_server))
        if self.proxy_username and self.proxy_password:
            res["username"] = self.proxy_username
            res["password"] = self.proxy_password
        if self.proxy_bypass:
            res["bypass"] = self.proxy_bypass
        return res


def calculate_file_hash(file_paths: List[Path]) -> str:
    """Calculate the SHA-256 hash of the file content."""
    hasher = hashlib.sha256()
    # they should exist, just to make sure
    for file_path in file_paths:
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        #
        with open(file_path, "rb") as file:
            while chunk := file.read(8192):
                hasher.update(chunk)
    return hasher.hexdigest()


def merge_dicts(dicts: list) -> dict:
    """Merge a list of dictionaries into a single dictionary, including nested dictionaries.

    :param dicts: A list of dictionaries to merge.
    :return: A single merged dictionary.
    """

    def merge(d1: dict, d2: dict) -> dict:
        for key, value in d2.items():
            if key in d1:
                if isinstance(d1[key], dict) and isinstance(value, dict):
                    d1[key] = merge(d1[key], value)
                elif isinstance(d1[key], list) and isinstance(value, list):
                    d1[key].extend(value)
                else:
                    d1[key] = value
            else:
                d1[key] = value
        return d1

    result: Dict[str, Any] = {}
    for dictionary in dicts:
        result = merge(result, dictionary)
    return result


def normalize_string(string: str) -> str:
    """Normalize a string by replacing multiple spaces (including space, tab, and newline) with a single space."""
    return re.sub(r"\s+", " ", string).lower()


ParserElement.enable_packrat()
double_quoted_string = ('"' + CharsNotIn('"').leaveWhitespace() + '"').setParseAction(
    lambda t: t[1]
)  # removes quotes, keeps only the content
single_quoted_string = ("'" + CharsNotIn("'").leaveWhitespace() + "'").setParseAction(
    lambda t: t[1]
)  # removes quotes, keeps only the content

special_chars = "!@#$%^&*-_=+[]{}|;:'\",.<>?/\\`~"
unquoted_string = Word(alphanums + special_chars)

operand = double_quoted_string | single_quoted_string | unquoted_string
and_op = Keyword("AND")
or_op = Keyword("OR")
not_op = Keyword("NOT")

# Define the grammar for parsing
expr = infix_notation(
    operand,
    [
        (not_op, 1, opAssoc.RIGHT),
        (and_op, 2, opAssoc.LEFT),
        (or_op, 2, opAssoc.LEFT),
    ],
)


def is_substring(
    var1: str | List[str], var2: str | List[str], logger: Logger | None = None
) -> bool:
    """Check if var1 is a substring of var2, after normalizing both strings. One of them can be a list of strings.

    var1: can be a single string, or a list of string, for which a condition of OR is assumed.
          this program will parse var11 for "AND", "OR" and "NOT", and return the results of the
          logical expression.

    var2: one or more strings for testing if strings in  "var1" is a substring.
    """
    if isinstance(var1, list):
        return any(is_substring(x, var2, logger) for x in var1)

    # parse the expression
    parsed = ""
    try:
        parsed = expr.parseString(var1, parseAll=True)[0]
    except Exception:
        # treat var1 as literal string for searching.
        if any(x in var1 for x in (" AND ", " OR ", " NOT ", "(NOT ")) or var1.startswith("NOT "):
            if logger:
                logger.warning(
                    f"Failed to parse {var1} as a logical expression. Treating it as literal string."
                )
        if isinstance(var2, str):
            return normalize_string(var1) in normalize_string(var2)
        return any(normalize_string(var1) in normalize_string(s2) for s2 in var2)

    def evaluate_expression(parsed_expression: str | ParseResults) -> bool:
        if isinstance(parsed_expression, str):
            if isinstance(var2, str):
                return normalize_string(parsed_expression) in normalize_string(var2)
            return any(normalize_string(parsed_expression) in normalize_string(s) for s in var2)

        if len(parsed_expression) == 1:
            return evaluate_expression(parsed_expression[0])

        if parsed_expression[0] == "NOT":
            return not evaluate_expression(parsed_expression[1])

        if parsed_expression[-2] == "AND":
            return evaluate_expression(parsed_expression[:-2]) and evaluate_expression(
                parsed_expression[-1]
            )

        if parsed_expression[-2] == "OR":
            return evaluate_expression(parsed_expression[:-2]) or evaluate_expression(
                parsed_expression[-1]
            )
        if logger:
            logger.error(f"Invalid expression: {parsed_expression}")
        return False

    return evaluate_expression(parsed)


class ChangeHandler(FileSystemEventHandler):
    def __init__(self: "ChangeHandler", files: List[str]) -> None:
        self.changed = False
        # Normalize to real paths — on macOS /var/folders is a symlink
        # to /private/var/folders and watchdog reports the resolved form.
        self.files = {os.path.realpath(f) for f in files}

    def _mark_if_watched(self: "ChangeHandler", path: "str | bytes | None") -> None:
        if path and os.path.realpath(path) in self.files:
            self.changed = True

    def on_modified(self: "ChangeHandler", event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_created(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # Atomic writes via os.replace() may appear as a create on the
        # destination path (depending on platform + watchdog backend).
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_deleted(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # On macOS, os.replace() over an existing file fires a 'deleted'
        # event on the destination path, not 'moved'. Treat it as a change.
        if not event.is_directory:
            self._mark_if_watched(event.src_path)

    def on_moved(self: "ChangeHandler", event: FileSystemEvent) -> None:
        # On Linux (inotify), atomic writes via tempfile + os.replace()
        # land here: src_path is the temp file, dest_path is the real one.
        if not event.is_directory:
            self._mark_if_watched(getattr(event, "dest_path", None))
            self._mark_if_watched(event.src_path)


def doze(
    duration: int, files: List[Path] | None = None, keyboard_monitor: KeyboardMonitor | None = None
) -> SleepStatus:
    """Sleep for a specified duration while monitoring the change of files.

    Return:
        0: if doze was done naturally.
        1: if doze was disrupted by keyboard
        2: if doze was disrupted by file change
    """
    event_handler = ChangeHandler([str(x) for x in (files or [])])
    observers = []
    if keyboard_monitor:
        keyboard_monitor.start_sleeping()

    for filename in files or []:
        if not filename.exists():
            raise FileNotFoundError(f"File not found: {filename}")
        observer = Observer()
        # we can only monitor a directory
        observer.schedule(event_handler, str(filename.parent), recursive=False)
        observer.start()
        observers.append(observer)

    start_time = time.time()
    try:
        while time.time() - start_time < duration:
            if event_handler.changed:
                return SleepStatus.BY_FILE_CHANGE
            time.sleep(1)
            if keyboard_monitor and not keyboard_monitor.is_sleeping():
                return SleepStatus.BY_KEYBOARD
        return SleepStatus.NOT_DISRUPTED
    finally:
        for observer in observers:
            observer.stop()
            observer.join()


def extract_price(price: str) -> str:
    if not price or price == "**unspecified**":
        return price

    # extract leading non-numeric characters as currency symbol
    matched = re.match(r"(\D*)\d+", price)
    if matched:
        currency = matched.group(1).strip()
    else:
        currency = "$"

    matches = re.findall(currency.replace("$", r"\$") + r"[\d,]+(?:\.\d+)?", price)
    if matches:
        return " | ".join(matches[:2])
    return price


def convert_to_seconds(time_str: str) -> int:
    cal = parsedatetime.Calendar(version=parsedatetime.VERSION_CONTEXT_STYLE)
    time_struct, _ = cal.parse(time_str)
    return int(time.mktime(time_struct) - time.mktime(time.localtime()))


def hilight(text: str, style: str = "name") -> str:
    """Highlight the keywords in the text with the specified color."""
    color = {
        "name": "cyan",
        "fail": "red",
        "info": "blue",
        "succ": "green",
        "dim": "gray",
    }.get(style, "blue")
    return f"[{color}]{text}[/{color}]"


def fetch_with_retry(
    url: str,
    timeout: int = 10,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    logger: Logger | None = None,
) -> Tuple[bytes, str] | None:
    """Fetch URL content with retry logic

    Args:
        url: URL to fetch
        timeout: Timeout in seconds
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for exponential backoff
        logger: logger object

    Returns:
        Tuple of (content, content_type) if successful, None if failed
    """
    if logger:
        logger.debug(f"Fetching {url} with timeout {timeout}s")
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                stream=True,  # Good practice for downloading files
            )
            response.raise_for_status()  # Raises exception for 4XX/5XX status codes

            return response.content, response.headers["Content-Type"]

        except Timeout:
            wait_time = backoff_factor**attempt
            if logger:
                logger.warning(
                    f"Timeout fetching {url} (attempt {attempt + 1}/{max_retries}). "
                    f"Waiting {wait_time:.1f}s before retry"
                )

            if attempt < max_retries - 1:
                time.sleep(wait_time)

        except RequestException as e:
            if logger:
                logger.error(f"Error fetching {url}: {e!s}")
            return None

    if logger:
        logger.error(f"Failed to fetch {url} after {max_retries} attempts")
    return None


def resize_image_data(image_data: bytes, max_width: int = 800, max_height: int = 600) -> bytes:
    # Create image object from binary data
    try:
        image = Image.open(io.BytesIO(image_data))
        if image.format == "GIF":
            return image_data
    except Exception:
        # if unacceptable file format, just return
        return image_data

    # Calculate new dimensions maintaining aspect ratio
    width, height = image.size
    ratio = min(max_width / width, max_height / height)
    if ratio >= 1:
        return image_data

    new_width = int(width * ratio)
    new_height = int(height * ratio)

    # Resize image
    resized_image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Convert back to bytes
    buffer = io.BytesIO()
    resized_image.save(buffer, format=image.format)
    return buffer.getvalue()


class Translator:
    def __init__(
        self: "Translator", locale: str | None = None, dictionary: Dict[str, str] | None = None
    ) -> None:
        self.locale = locale
        self._dictionary: Dict[str, str] = copy.deepcopy(dictionary or {})

    def __call__(self: "Translator", word: str) -> str:
        """Return translated version"""
        return self._dictionary.get(word, word)


# ---------------------------------------------------------------------------
# "Listed 3 days ago" -> an absolute moment in time.
#
# Marketplaces word a listing's age relatively, and a relative phrase rots:
# "3 days ago" cached on Monday still reads "3 days ago" on Friday. So the
# phrase is resolved to an epoch at scrape time and only the epoch is stored.
#
# The arithmetic is not hand-rolled. `parsedatetime` is already a dependency
# (convert_to_seconds uses it) and is the mature, well-tested implementation
# of exactly this, month lengths and DST included. What is added here is
# recognition: a regex that pulls the time phrase out of the surrounding copy
# ("Listed 3 days ago in High Point, NC") before parsedatetime sees it, so a
# location that happens to read like a date -- "March, PA" -- cannot be
# mistaken for one. Recognition is also where translation belongs, because
# parsedatetime's own locale support needs PyICU while this scraper's
# translations come from the user's [translation.*] config.
# ---------------------------------------------------------------------------

# Word -> the canonical English plural parsedatetime is handed. Abbreviations
# are in the table because tiles use them ("2 hrs ago").
RELATIVE_UNITS: Dict[str, str] = {
    "second": "seconds",
    "seconds": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "hour": "hours",
    "hours": "hours",
    "hr": "hours",
    "hrs": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
    "year": "years",
    "years": "years",
}

# Quantity words meaning one. Facebook writes "Listed a week ago" far more
# often than "Listed 1 week ago".
RELATIVE_ONES: Tuple[str, ...] = ("a", "an", "one")

# Vagueness Facebook adds once a listing is old enough that it stops counting
# precisely ("Listed over 2 weeks ago"). Dropped: the number is the signal.
RELATIVE_HEDGES: Tuple[str, ...] = (
    "about",
    "almost",
    "approximately",
    "around",
    "more than",
    "nearly",
    "over",
    "roughly",
)

# Phrases meaning "moments ago" that carry no number. parsedatetime reports
# them unparseable, so they are resolved here to the reference time.
RELATIVE_NOW: Tuple[str, ...] = (
    "just now",
    "just listed",
    "a few seconds ago",
    "a few moments ago",
    "a moment ago",
    "moments ago",
    "seconds ago",
    "less than a minute ago",
    "today",
)

RELATIVE_YESTERDAY: Tuple[str, ...] = ("yesterday",)

# Keys a non-English deployment can define in [translation.<lang>] to make the
# relative form readable there too. A missing key translates to itself, which
# only means the English wording stays the only one matched -- and on Facebook
# the page's inline `creation_time` supplies the timestamp regardless.
RELATIVE_TRANSLATABLE: Tuple[str, ...] = (
    *RELATIVE_UNITS,
    "a",
    "ago",
    "yesterday",
    "just now",
)

_WHITESPACE = re.compile(r"[\s\u2009\u202f]+")


def normalize_relative_text(text: str) -> str:
    """Lower-cased, single-spaced, non-breaking spaces folded away."""
    # \s already covers U+00A0 for str patterns; the explicit fold is for the
    # narrow/thin spaces Facebook slips between a number and its unit.
    return _WHITESPACE.sub(" ", text or "").strip().lower()


def _relative_vocabulary(
    translator: "Translator | None" = None,
) -> Tuple[Dict[str, str], List[str], List[str], List[str], List[str]]:
    """(unit word -> canonical unit, one-words, ago-words, instants, yesterdays)."""
    units = dict(RELATIVE_UNITS)
    ones = list(RELATIVE_ONES)
    agos = ["ago"]
    instants = list(RELATIVE_NOW)
    yesterdays = list(RELATIVE_YESTERDAY)
    if translator is None:
        return units, ones, agos, instants, yesterdays
    for word in RELATIVE_TRANSLATABLE:
        local = normalize_relative_text(translator(word))
        if not local or local == word:
            continue
        if word in RELATIVE_UNITS:
            units[local] = RELATIVE_UNITS[word]
        elif word == "a":
            ones.append(local)
        elif word == "ago":
            agos.append(local)
        elif word == "yesterday":
            yesterdays.append(local)
        else:
            instants.append(local)
    return units, ones, agos, instants, yesterdays


def _alternation(words: Iterable[str]) -> str:
    """Longest-first alternation, so "minutes" is never cut short by "min"."""
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def relative_time_phrase(text: str, translator: "Translator | None" = None) -> Tuple[str, str]:
    """Pull a relative-time phrase out of the surrounding copy.

    Returns ``(canonical, spoken)``: the phrase rewritten in the English
    parsedatetime understands, and the phrase exactly as the page worded it.
    ``("", "")`` when the text carries no relative time at all -- a location,
    an empty node, or a locale whose words are not in the translation table.
    """
    haystack = normalize_relative_text(text)
    if not haystack:
        return "", ""
    units, ones, agos, instants, yesterdays = _relative_vocabulary(translator)

    hedge = _alternation(RELATIVE_HEDGES)
    body = (
        rf"(?:(?:{hedge})\s+)?"
        rf"(?P<qty>\d{{1,4}}|{_alternation(ones)})\s*"
        rf"(?P<unit>{_alternation(units)})\b"
    )
    ago = _alternation(agos)
    # The "ago" word follows the quantity in English and Swedish and leads it
    # in Spanish ("hace 3 dias"); one side or the other has to be there, or
    # "2 bedrooms" in a rental blurb would read as a timestamp. The numeric
    # forms are tried before the wordless ones because "seconds ago" is a
    # substring of "30 seconds ago".
    for pattern in (rf"{body}\s+(?:{ago})\b", rf"(?:{ago})\s+{body}"):
        match = re.search(pattern, haystack)
        if match is None:
            continue
        quantity = match.group("qty")
        count = 1 if quantity in ones else int(quantity)
        return f"{count} {units[match.group('unit')]} ago", match.group(0).strip()

    for phrase in sorted(set(instants), key=len, reverse=True):
        if phrase and phrase in haystack:
            return "now", phrase
    for phrase in sorted(set(yesterdays), key=len, reverse=True):
        if phrase and phrase in haystack:
            return "yesterday", phrase
    return "", ""


def parse_relative_time(
    text: str,
    now: float | None = None,
    translator: "Translator | None" = None,
) -> float | None:
    """Epoch seconds for a relative phrase, or None when there is not one.

    Never returns a moment in the future: a listing cannot have been posted
    after the page describing it was rendered, so a forward-looking parse is a
    misreading and is reported as "unknown" instead.
    """
    canonical, _ = relative_time_phrase(text, translator)
    if not canonical:
        return None
    reference = time.time() if now is None else float(now)
    if canonical == "now":
        return reference
    # Context style, like convert_to_seconds above: the flag style is
    # deprecated, and `hasDateOrTime` is the honest "did it parse anything"
    # answer -- the context object itself is always truthy.
    parsed, context = parsedatetime.Calendar(version=parsedatetime.VERSION_CONTEXT_STYLE).parseDT(
        canonical, sourceTime=datetime.datetime.fromtimestamp(reference)
    )
    if not context.hasDateOrTime:
        return None
    stamp = parsed.timestamp()
    # A minute of slack absorbs clock skew between the page's server and this
    # machine; anything further ahead is a misread, not a fresh listing.
    return None if stamp > reference + 60 else stamp


# ---------------------------------------------------------------------------
# Listing photo snapshots.
#
# Facebook's CDN URLs are signed and expire within days, so a listing rated on
# Monday shows a broken image by Friday. The web UI proxies photos through
# /api/listing-image, which fetches once and keeps the bytes here; the monitor
# pre-warms the same directory for listings worth reviewing so the photos are
# already on disk by the time the user opens the queue.
#
# Naming is load-bearing: photo 0 keeps the historical `<key>.img` name so
# every snapshot taken before multi-photo support still resolves, and photos
# 1..n append `-<i>`.
# ---------------------------------------------------------------------------
image_cache_dir = amm_home / "imgcache"


def image_cache_path(post_url: str, index: int = 0) -> Path:
    """On-disk snapshot path for the index-th photo of a listing."""
    key = hashlib.sha256(post_url.split("?")[0].encode()).hexdigest()[:32]
    return image_cache_dir / (f"{key}.img" if index <= 0 else f"{key}-{index}.img")


# 5 MB is well above any Marketplace photo and well below "someone linked a
# video file"; the proxy and the pre-warmer share the ceiling.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# A plain browser UA and no referrer is what the CDN expects from a direct
# visit; hotlink-looking requests get a 403.
IMAGE_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def fetch_image_snapshot(url: str, destination: Path, timeout: int = 15) -> bool:
    """Download one photo to `destination`. False on any failure, never raises.

    Expired CDN URLs are the common case, not an error worth logging loudly.
    """
    if not url.startswith(("http://", "https://")):
        return False
    try:
        image_cache_dir.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=timeout, headers=IMAGE_FETCH_HEADERS, stream=True)
        if resp.status_code != 200:
            return False
        content = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        if not content or len(content) > MAX_IMAGE_BYTES:
            return False
        # Write through a temp file so a torn download never becomes a
        # permanently cached zero-byte "photo".
        tmp = destination.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.replace(destination)
        return True
    except KeyboardInterrupt:
        raise
    except Exception:
        return False
