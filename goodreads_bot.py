#!/usr/bin/env python3
"""Post Goodreads activity (started / progress / finished / ratings / quotes) to a Discord channel.

Reads each reader's public Goodreads profile feed, reading-status page and
shelf widget, figures out which events are new since the last run (tracked in
state.json), and posts them to a Discord webhook as embeds. Standard library
only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

GOODREADS = "https://www.goodreads.com"
# The "Recent Updates" section of a profile page, which the page loads separately after it opens.
FEED_URL = GOODREADS + "/user/delayable_user_show/{user_id}"
STATUS_URL = GOODREADS + "/user_status/list/{user_id}"  # reading progress
UPDATES_URL = GOODREADS + "/widgets/user_update_widget?num_updates=20&user={user_id}"  # fallback for the feed
# The shelf widget ignores its query options and always returns the reader's
# recent "read" books (5-30 of them, depending on their saved widget settings).
SHELF_URL = GOODREADS + "/review/custom_widget/{user_id}.read?num_books=30&shelf=read"
STATE_VERSION = 2  # 1 = RSS guids (before Goodreads removed RSS)
USER_AGENT = "goodreads-discord-bot/1.0 (+https://github.com)"
try:
    PACIFIC = ZoneInfo("America/Los_Angeles")  # Goodreads shows signed-out visitors Pacific time
except ZoneInfoNotFoundError:  # no tz database installed
    PACIFIC = timezone(timedelta(hours=-8))

ALL_EVENTS = ("started", "progress", "finished", "rated", "want", "quote")
EVENT_STYLE = {
    # kind: (verb shown in the embed header, embed color)
    "started": ("started reading", 0x3B82F6),
    "progress": ("made progress on", 0x8B5CF6),
    "finished": ("finished reading", 0x22C55E),
    "rated": ("rated", 0xF59E0B),
    "want": ("wants to read", 0x94A3B8),
    "quote": ("shared a quote", 0x14B8A6),
}
SEEN_LIMIT = 300  # keys remembered per reader: ~10 updates + 30 statuses + up to 30 shelf entries per run


# --------------------------------------------------------------------------- config


@dataclass
class Reader:
    user_id: str
    name: str | None = None


@dataclass
class Config:
    readers: list[Reader]
    events: tuple[str, ...] = ALL_EVENTS
    max_age_days: int = 3
    bot_username: str | None = None
    bot_avatar_url: str | None = None


def parse_user_id(value: str | int) -> str:
    """Accept a bare id, '96005733-name', or any goodreads profile/feed/widget URL."""
    value = str(value)
    match = (
        re.search(r"[?&]user=(\d+)", value)
        or re.search(r"(\d+)", value.split("?")[0].rsplit("/", 1)[-1])
        or re.search(r"(\d+)", value)
    )
    if not match:
        raise ValueError(f"can't find a Goodreads user id in {value!r}")
    return match.group(1)


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    readers = [
        Reader(user_id=parse_user_id(r["goodreads"]), name=r.get("name"))
        for r in data.get("readers", [])
    ]
    if not readers:
        raise ValueError(f"{path}: add at least one [[readers]] entry")
    settings = data.get("settings", {})
    events = tuple(settings.get("events", ALL_EVENTS))
    unknown = set(events) - set(ALL_EVENTS)
    if unknown:
        raise ValueError(f"{path}: unknown events {sorted(unknown)}; choose from {list(ALL_EVENTS)}")
    return Config(
        readers=readers,
        events=events,
        max_age_days=int(settings.get("max_age_days", 3)),
        bot_username=settings.get("bot_username"),
        bot_avatar_url=settings.get("bot_avatar_url"),
    )


# --------------------------------------------------------------------------- page parsing
#
# Goodreads removed its public RSS feeds in October 2026, so the bot reads
# pages that work without signing in:
#   * the profile feed - what someone did (started / finished / rated / wants
#     to read / shared a quote), their last 10 updates, with exact times;
#   * the status page - their reading progress ("is 50% done with ...");
#   * the shelf widget - their most recent "read" books with exact ratings,
#     including half and quarter stars, and stable review ids;
#   * the updates widget - like the profile feed but dated by day only, used
#     when the feed can't be read.


@dataclass
class Event:
    key: str  # what state.json remembers so the event is only posted once
    kind: str
    reader_name: str
    reader_id: str
    day: date  # Pacific, like the dates Goodreads shows
    book_title: str
    book_id: str | None = None
    author: str | None = None
    cover_url: str | None = None
    rating: float | None = None  # Goodreads allows quarter stars, e.g. 3.75
    rating_approx: bool = False  # only the updates widget's lossy star fill was available
    progress: str | None = None
    review: str | None = None
    quote: str | None = None
    note: str | None = None  # what the reader wrote with a quote or progress update
    link: str | None = None
    time: datetime | None = None  # exact time, when the source has one

    @property
    def book_url(self) -> str | None:
        return f"{GOODREADS}/book/show/{self.book_id}" if self.book_id else None


@dataclass
class ShelfBook:
    review_id: str
    title: str
    book_id: str | None
    author: str | None
    rating: float
    cover_url: str | None


def _strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _large_cover(url: str | None) -> str | None:
    # Goodreads serves thumbnails like "12345._SY75_.jpg"; dropping the size suffix gives full size.
    return re.sub(r"\._S[XY]\d+_(?=\.)", "", url) if url else None


def _norm_title(title: str) -> str:
    return re.sub(r"\s*\(.*$", "", html.unescape(title)).strip().casefold()


def _parse_day(label: str, today: date) -> date | None:
    """'October 26' (this year) or 'August 24, 2025'."""
    label = " ".join(label.split())
    try:
        return datetime.strptime(label, "%B %d, %Y").date()
    except ValueError:
        pass
    for year in (today.year, today.year - 1):  # "December 30" seen in early January is last year's
        try:
            parsed = datetime.strptime(f"{label}, {year}", "%B %d, %Y").date()
        except ValueError:
            return None
        if parsed <= today + timedelta(days=1):
            return parsed
    return None


_UPDATE_BLOCK = re.compile(r'<div class="update(?:\s[^"]*)?"\s*>')


def _update_kind(action: str) -> str | None:
    if "rated a book" in action:
        return "rated"
    # Goodreads words the same event differently depending on how it was logged.
    if "started reading" in action or "is currently reading" in action:
        return "started"
    if "finished reading" in action or "has read" in action:
        return "finished"
    if "wants to read" in action:
        return "want"
    return None


def parse_updates_widget(page: str, reader: Reader, today: date) -> tuple[str, list[Event]]:
    """Return (display name, events newest-first) from a user_update_widget page."""
    m = re.search(r'class="gr_reviewer_name">\s*<a[^>]*>(.*?)</a>', page, re.S)
    name = reader.name or (_strip_tags(m.group(1)) if m else "") or reader.user_id

    events: list[Event] = []
    day = None
    for block in _UPDATE_BLOCK.split(page)[1:]:
        # Only the first update of each day carries the date.
        m = re.match(r"\s*<p>([^<]+)</p>", block)
        if m:
            day = _parse_day(m.group(1), today) or day
        if day is None:
            continue
        event = _parse_update(block, name, reader.user_id, day)
        if event and all(e.key != event.key for e in events):  # Goodreads sometimes lists an update twice
            events.append(event)
    return name, events


def _parse_update(block: str, name: str, reader_id: str, day: date) -> Event | None:
    def find(pattern: str) -> str | None:
        m = re.search(pattern, block, re.S)
        return (_strip_tags(m.group(1)) or None) if m else None

    common = {"reader_name": name, "reader_id": reader_id, "day": day}

    quote_id = re.search(r'href="/quotes/(\d+)[^"]*"[^>]*>\s*quote\s*</a>', block)
    if quote_id:
        quote = find(r'class="quoteText"[^>]*>(.*?)</span>')
        if not quote:
            return None
        return Event(
            key=f"quote:{quote_id.group(1)}",
            kind="quote",
            book_title="",  # the widget doesn't name the book; add_quote_book() fills it in
            quote=quote.strip("“”\"\n "),
            author=find(r'href="/author/quotes/[^"]*"[^>]*>(.*?)</a>'),
            link=f"{GOODREADS}/quotes/{quote_id.group(1)}",
            **common,
        )

    m = re.search(r'<div class="updateAction">(.*?)class="bookTitle"', block, re.S)
    kind = _update_kind(" ".join(_strip_tags(m.group(1)).split())) if m else None
    book = re.search(r'class="bookTitle"[^>]*href="/book/show/(\d+)[^"]*"[^>]*>(.*?)</a>', block, re.S)
    if not kind or not book:
        return None  # follows, likes, giveaways, ...
    cover = re.search(r'<img[^>]*\bsrc="([^"]+/books/[^"]+)"', block)
    event = Event(
        key=f"{kind}:{book.group(1)}:{day.isoformat()}",
        kind=kind,
        book_title=_strip_tags(book.group(2)),
        book_id=book.group(1),
        author=find(r'class="authorName"[^>]*>(.*?)</a>'),
        cover_url=_large_cover(cover.group(1)) if cover else None,
        **common,
    )

    # Long reviews come as a truncated "freeTextContainer" plus the full text in a hidden "freeText" span.
    review = re.search(r'id="freeTextreview(\d+)"[^>]*>(.*?)</span>', block, re.S) or re.search(
        r'id="freeTextContainerreview(\d+)"[^>]*>(.*?)</span>', block, re.S
    )
    if review:
        event.review = _strip_tags(review.group(2)) or None
        event.link = f"{GOODREADS}/review/show/{review.group(1)}"

    if kind == "rated" and not _set_star_rating(event, block):
        return None
    return event


def _set_star_rating(event: Event, html_text: str) -> bool:
    """Read a rating from the star images. Returns False if there's no rating.

    Each star is drawn filled 0-10. The partial fill can't tell 2.5 from 2.75,
    so this is replaced by the shelf widget's exact rating when possible.
    """
    fills = [int(p) for p in re.findall(r'class="staticStar p(\d+)"', html_text)]
    if not any(fills):
        return False
    whole = sum(f == 10 for f in fills)
    partial = any(0 < f < 10 for f in fills)
    event.rating = whole + (0.5 if partial else 0)
    event.rating_approx = partial
    return True


_FEED_ROW = re.compile(r"<tr class='update' data-timestamp='([^']+)' id='update_\d+'>")


def parse_profile_feed(page: str, reader: Reader) -> tuple[str, list[Event]]:
    """Return (display name, events newest-first) from a profile's Recent Updates section."""
    if "Recent Updates" not in page:
        raise ValueError("no Recent Updates section")
    m = re.search(rf'<a href="/user/show/{reader.user_id}(?:-[^"]*)?">([^<]+)</a>', page)
    name = reader.name or (" ".join(html.unescape(m.group(1)).split()) if m else "") or reader.user_id

    events: list[Event] = []
    parts = _FEED_ROW.split(page)
    for stamp, block in zip(parts[1::2], parts[2::2]):
        try:
            when = datetime.strptime(stamp, "%B %d, %Y %H:%M").replace(tzinfo=PACIFIC)
        except ValueError:
            continue
        event = _parse_feed_row(block, name, reader.user_id, when)
        if event and all(e.key != event.key for e in events):
            events.append(event)
    return name, events


def _parse_feed_row(block: str, name: str, reader_id: str, when: datetime) -> Event | None:
    def find(pattern: str, text: str = block) -> str | None:
        m = re.search(pattern, text, re.S)
        return (_strip_tags(m.group(1)) or None) if m else None

    action = re.search(r"<div class='updateAction'>(.*?)(?:<div class='quoteNote'>|</div>)", block, re.S)
    if not action:
        return None
    action_html = action.group(1)
    common = {"reader_name": name, "reader_id": reader_id, "day": when.date(), "time": when}
    cover = re.search(r'<img[^>]*\bsrc="([^"]+/books/[^"]+)"', block)
    cover_url = _large_cover(cover.group(1)) if cover else None

    quote_id = re.search(r'href="/quotes/(\d+)[^"]*">\s*quote\s*</a>', action_html)
    if quote_id:
        quote = find(r"<span class='quoteText'>(.*?)</span>\s*</span>")
        if not quote:
            return None
        author = find(r'class="quoteAuthor"[^>]*>(.*?)</a>')
        book_id = re.search(r"data-resource-id='(\d+)' data-resource-type='Book'", block)
        title = re.search(r'<img alt="([^"]*)"', block) if book_id else None
        book_title = html.unescape(title.group(1)) if title else ""
        if author:
            book_title = book_title.removesuffix(f" by {author}")
        return Event(
            key=f"quote:{quote_id.group(1)}",
            kind="quote",
            book_title=book_title,
            book_id=book_id.group(1) if book_id else None,
            quote=quote.strip("“”\"\n "),
            author=author,
            note=find(r"<div class='quoteNote'>(.*?)</div>"),
            cover_url=cover_url,
            link=f"{GOODREADS}/quotes/{quote_id.group(1)}",
            **common,
        )

    kind = _update_kind(" ".join(_strip_tags(action_html).split()))
    book = re.search(r'class="bookTitle" href="/book/show/(\d+)[^"]*">(.*?)</a>', block, re.S)
    if not kind or not book:
        return None  # follows, likes, comments, progress (read from the status page), ...
    event = Event(
        key=f"{kind}:{book.group(1)}:{when.date().isoformat()}",
        kind=kind,
        book_title=_strip_tags(book.group(2)),
        book_id=book.group(1),
        author=find(r'class="authorName"[^>]*>(.*?)</a>'),
        cover_url=cover_url,
        **common,
    )
    # The feed only has the start of a review, with a "Read more" link.
    review = re.search(r"id='reviewTextContainer(\d+)'[^>]*>(.*?)</span>", block, re.S)
    if review:
        text, more = re.subn(r'<a class="readMoreLink.*?</a>', "", review.group(2), flags=re.S)
        text = _strip_tags(text).strip('"“” \n')
        event.review = (text + "…" if more else text) or None
        event.link = f"{GOODREADS}/review/show/{review.group(1)}"
    if kind == "rated" and not _set_star_rating(event, action_html):
        return None
    return event


_STATUS_BLOCK = re.compile(r'<div id="user_status_(\d+)">')


def parse_status_page(page: str, name: str, reader_id: str) -> list[Event]:
    """Reading-progress updates, newest first, from a /user_status/list page."""
    events = []
    parts = _STATUS_BLOCK.split(page)
    for status_id, block in zip(parts[1::2], parts[2::2]):
        header = re.search(r'user_status_header">(.*?)</span>', block, re.S)
        m = header and re.search(
            r"</a>\s*is\s+(on page \d+ of \d+|\d+% done)\s+(?:of|with)\s*"
            r'<a href="[^"]*/book/show/(\d+)[^"]*"[^>]*>(.*?)</a>',
            header.group(1), re.S,
        )
        stamp = re.search(rf'href="/user_status/show/{status_id}">([^<]+)</a>', block)
        if not m or not stamp:
            continue
        try:
            when = datetime.strptime(" ".join(stamp.group(1).split()), "%b %d, %Y %I:%M%p").replace(tzinfo=PACIFIC)
        except ValueError:
            continue
        comment = re.search(r'<div class="readable body">(.*?)</div>', block, re.S)
        cover = re.search(r'class="left bookCover".*?<img[^>]*\bsrc="([^"]+)"', block, re.S)
        events.append(Event(
            key=f"progress:{status_id}",
            kind="progress",
            reader_name=name,
            reader_id=reader_id,
            day=when.date(),
            time=when,
            book_title=_strip_tags(m.group(3)),
            book_id=m.group(2),
            progress=m.group(1),
            note=(_strip_tags(comment.group(1)) or None) if comment else None,
            cover_url=_large_cover(cover.group(1)) if cover else None,
            link=f"{GOODREADS}/user_status/show/{status_id}",
        ))
    return events


def parse_shelf_widget(js: str) -> list[ShelfBook]:
    """Parse the custom_widget script: the reader's recent "read" books, newest first."""
    m = re.search(r"var widget_code = '(.*?)'\n", js, re.S)
    if not m:
        raise ValueError("unexpected shelf widget format")
    code = re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t"}.get(e.group(1), e.group(1)), m.group(1))
    star_value = {"active": 1.0, "75pct": 0.75, "50pct": 0.5, "25pct": 0.25, "inactive": 0.0}

    books = []
    for entry in code.split('class="gr_custom_each_container_"')[1:]:
        review_id = re.search(r"/review/show/(\d+)", entry)
        title = re.search(r'<a title="([^"]+)"', entry)
        if not review_id or not title:
            continue
        cover = re.search(r'<img[^>]*\bsrc="([^"]+)"', entry)
        book_id = re.search(r"/books/\d+\w/(\d+)\.", cover.group(1)) if cover else None
        author = re.search(r'class="gr_custom_author_">.*?<a[^>]*>(.*?)</a>', entry, re.S)
        rating = re.search(r'class="gr_custom_rating_">(.*?)</div>', entry, re.S)
        books.append(ShelfBook(
            review_id=review_id.group(1),
            title=html.unescape(title.group(1)),
            book_id=book_id.group(1) if book_id else None,
            author=_strip_tags(author.group(1)) if author else None,
            rating=sum(star_value.get(s, 0) for s in re.findall(r"gr_flat_red_star_(\w+?)\.png", rating.group(1)))
            if rating else 0.0,
            cover_url=_large_cover(cover.group(1)) if cover and "/books/" in cover.group(1) else None,
        ))
    return books


def apply_shelf_ratings(events: list[Event], shelf: list[ShelfBook]) -> None:
    """Swap the updates widget's approximate star fill for the shelf widget's exact rating."""
    for event in events:
        if event.kind != "rated":
            continue
        match = next((b for b in shelf if event.book_id and b.book_id == event.book_id), None) or next(
            (b for b in shelf if _norm_title(b.title) == _norm_title(event.book_title)), None
        )
        if match and match.rating:
            event.rating, event.rating_approx = match.rating, False
            event.link = event.link or f"{GOODREADS}/review/show/{match.review_id}"


def shelf_key(book: ShelfBook) -> str:
    return f"shelf:{book.review_id}={book.rating:g}"


def shelf_events(
    shelf: list[ShelfBook], updates: list[Event], name: str, reader_id: str, seen: list[str], today: date
) -> list[Event]:
    """New or re-rated "read" books that the updates widget doesn't mention.

    The updates widget leaves some activity out (and is empty for some privacy
    settings), so the shelf widget fills the gaps.

    The widget doesn't always return the same number of books, so a longer page
    can show older books we never recorded. The shelf is newest first, so only
    books above the newest one we've seen are new; below that, only re-ratings
    count. With no shelf history at all, nothing is new.
    """
    seen_ids = {k.split(":", 1)[1].split("=", 1)[0] for k in seen if k.startswith("shelf:")}
    newest_seen = next((i for i, b in enumerate(shelf) if b.review_id in seen_ids), 0)
    covered = {e.book_id for e in updates if e.kind in ("finished", "rated")} - {None}
    covered_titles = {_norm_title(e.book_title) for e in updates if e.kind in ("finished", "rated")}
    events = []
    for i, book in enumerate(shelf):
        if shelf_key(book) in seen or book.book_id in covered or _norm_title(book.title) in covered_titles:
            continue
        rerated = book.review_id in seen_ids
        if not rerated and i >= newest_seen:
            continue
        events.append(Event(
            key=shelf_key(book),
            kind="rated" if rerated else "finished",
            reader_name=name,
            reader_id=reader_id,
            day=today,
            book_title=book.title,
            book_id=book.book_id,
            author=book.author,
            cover_url=book.cover_url,
            rating=book.rating or None,
            link=f"{GOODREADS}/review/show/{book.review_id}",
        ))
    return events


def _meta(page: str, prop: str) -> str | None:
    for tag in re.findall(r"<meta\b[^>]*>", page):
        if re.search(rf"""property=['"]{re.escape(prop)}['"]""", tag):
            m = re.search(r"""content=(['"])(.*?)\1""", tag, re.S)
            if m:
                return html.unescape(m.group(2))
    return None


def add_quote_book(event: Event) -> None:
    """Fill in which book a quote is from, using the quote's own page. Best effort."""
    if not event.link:
        return
    try:
        page = fetch(event.link)
    except Exception as err:
        print(f"! couldn't look up the book for quote {event.link} ({err})", file=sys.stderr)
        return
    title = _meta(page, "og:title") or ""
    if title.startswith("A quote from "):
        event.book_title = title.removeprefix("A quote from ").strip()
    book = re.search(r"/book/show/(\d+)", _meta(page, "og:see_also") or "")
    if book:
        event.book_id = book.group(1)
    event.cover_url = event.cover_url or _meta(page, "og:image")


def fetch(url: str, retries: int = 3) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- discord


def stars(rating: float, approx: bool = False) -> str:
    """★★★½☆ (3.5) - Unicode has no partial stars, so fractions use ¼ ½ ¾."""
    whole = int(rating)
    fraction = {0.25: "¼", 0.5: "½", 0.75: "¾"}.get(round(rating - whole, 2), "")
    text = "★" * whole + fraction + "☆" * (5 - whole - bool(fraction))
    if approx:
        return f"{text} (between {whole + 0.25:g} and {whole + 0.75:g})"
    return text if rating.is_integer() else f"{text} ({rating:g})"


def build_embed(event: Event) -> dict:
    verb, color = EVENT_STYLE[event.kind]
    if event.kind == "quote":
        return _quote_embed(event, verb, color)
    lines = []
    if event.author:
        lines.append(f"by **{event.author}**")
    if event.progress:
        lines.append(f"📖 {event.progress}")
    if event.note:
        note = event.note if len(event.note) <= 600 else event.note[:600].rstrip() + "…"
        lines.append("💬 " + note.replace("\n\n", "\n"))
    if event.rating:
        lines.append(stars(event.rating, event.rating_approx))
    if event.review:
        review = event.review if len(event.review) <= 600 else event.review[:600].rstrip() + "…"
        lines.append("> " + review.replace("\n", "\n> "))
        if event.link:
            lines.append(f"[Read the full review]({event.link})")

    embed = {
        "author": {
            "name": f"{event.reader_name} {verb}",
            "url": f"{GOODREADS}/user/show/{event.reader_id}",
        },
        "title": event.book_title[:256],
        "color": color,
    }
    if event.book_url:
        embed["url"] = event.book_url
    if lines:
        embed["description"] = "\n".join(lines)
    if event.cover_url:
        embed["thumbnail"] = {"url": event.cover_url}
    if event.time:
        embed["timestamp"] = event.time.isoformat()
    return embed


def _quote_embed(event: Event, verb: str, color: int) -> dict:
    quote = event.quote if len(event.quote) <= 1500 else event.quote[:1500].rstrip() + "…"
    lines = ["> " + quote.replace("\n", "\n> ")]
    if event.author:
        lines.append(f"— **{event.author}**")
    if event.note:
        lines.append(f"\n💬 **{event.reader_name}:** {event.note}")
    embed = {
        "author": {"name": f"{event.reader_name} {verb}", "url": f"{GOODREADS}/user/show/{event.reader_id}"},
        "title": (event.book_title or f"A quote by {event.author or 'someone'}")[:256],
        "description": "\n".join(lines),
        "color": color,
    }
    if event.link:
        embed["url"] = event.link
    if event.cover_url:
        embed["thumbnail"] = {"url": event.cover_url}
    if event.time:
        embed["timestamp"] = event.time.isoformat()
    return embed


def batch_events(events: list[Event]) -> list[list[Event]]:
    """Group events into Discord messages of up to 10 embeds.

    Discord merges embeds that share a url into one card (that's how its image
    galleries work), so e.g. "finished" and "rated" for the same book must go
    in separate messages or the second one disappears.
    """
    batches: list[list[Event]] = []
    urls: set = set()
    for event in events:
        url = build_embed(event).get("url")
        if not batches or len(batches[-1]) == 10 or (url and url in urls):
            batches.append([])
            urls = set()
        batches[-1].append(event)
        urls.add(url)
    return batches


def post_to_discord(webhook_url: str, embeds: list[dict], config: Config) -> None:
    payload: dict = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    if config.bot_username:
        payload["username"] = config.bot_username
    if config.bot_avatar_url:
        payload["avatar_url"] = config.bot_avatar_url
    body = json.dumps(payload).encode()
    for _ in range(5):
        request = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise RuntimeError(f"Discord rejected the post: {err.code} {err.read()[:300]!r}") from err
            retry_after = float(json.loads(err.read() or b"{}").get("retry_after", 2))
            time.sleep(retry_after + 0.5)
    raise RuntimeError("Discord kept rate-limiting us; giving up for this run")


# --------------------------------------------------------------------------- state


def load_state(path: Path) -> dict:
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get("version") != STATE_VERSION:
        if state.get("readers"):
            # Old RSS ids can't be matched against widget updates, so start
            # everyone fresh: current activity is recorded, not posted.
            print("State is from the RSS version of the bot; re-seeding every reader.")
        state = {"version": STATE_VERSION, "readers": {}}
    return state


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def remember(state: dict, reader_id: str, keys: list[str]) -> None:
    entry = state["readers"].setdefault(reader_id, {"seen": []})
    seen = [k for k in dict.fromkeys(keys) if k not in entry["seen"]] + entry["seen"]
    entry["seen"] = seen[:SEEN_LIMIT]


# --------------------------------------------------------------------------- main


def fetch_reader(reader: Reader, today: date) -> tuple[str, list[Event], list[ShelfBook], list[Event] | None]:
    """Return (name, updates, shelf books, progress updates or None if the status page failed)."""
    try:
        name, events = parse_profile_feed(fetch(FEED_URL.format(user_id=reader.user_id)), reader)
    except Exception as err:  # empty for some inactive profiles; the widget has the same updates by day
        print(f"! {reader.name or reader.user_id}: couldn't read profile feed ({err}); using the updates widget",
              file=sys.stderr)
        name, events = parse_updates_widget(fetch(UPDATES_URL.format(user_id=reader.user_id)), reader, today)
    try:
        shelf = parse_shelf_widget(fetch(SHELF_URL.format(user_id=reader.user_id)))
    except Exception as err:  # ratings fall back to the approximate star fill
        print(f"! {name}: couldn't read shelf widget ({err})", file=sys.stderr)
        shelf = []
    try:
        progress = parse_status_page(fetch(STATUS_URL.format(user_id=reader.user_id)), name, reader.user_id)
    except Exception as err:
        print(f"! {name}: couldn't read reading progress ({err})", file=sys.stderr)
        progress = None
    apply_shelf_ratings(events, shelf)
    return name, events, shelf, progress


def _posting_order(event: Event) -> datetime:
    # Events without a time (shelf books, widget updates) go after the timed ones from their day.
    return event.time or datetime.combine(event.day, dtime(23, 59), tzinfo=PACIFIC)


def collect_new_events(config: Config, state: dict, today: date) -> tuple[list[Event], int]:
    """Fetch every reader; return (unseen events oldest-first, number of failed readers).

    Readers we have never seen before are "seeded": their current activity is
    marked as seen without posting, so adding a friend doesn't dump their
    history into the channel.
    """
    new_events, failures = [], 0
    cutoff = today - timedelta(days=config.max_age_days)
    for reader in config.readers:
        try:
            name, events, shelf, progress = fetch_reader(reader, today)
        except Exception as err:  # one broken reader shouldn't block everyone else
            print(f"! {reader.name or reader.user_id}: couldn't read updates ({err})", file=sys.stderr)
            failures += 1
            continue

        all_keys = [e.key for e in events + (progress or [])] + [shelf_key(b) for b in shelf]
        known = state["readers"].get(reader.user_id)
        if known is None:
            remember(state, reader.user_id, all_keys)
            state["readers"][reader.user_id]["progress_seeded"] = progress is not None
            print(f"+ {name}: new reader, seeded {len(events)} updates, {len(progress or [])} progress "
                  f"updates and {len(shelf)} shelf books (not posted)")
            continue

        events = shelf_events(shelf, events, name, reader.user_id, known["seen"], today) + events
        if progress is not None:
            if known.get("progress_seeded"):
                events += progress
            else:  # first run since progress support was added: record it, like a new reader
                known["progress_seeded"] = True
        fresh = [
            e for e in events
            if e.key not in known["seen"] and e.kind in config.events and e.day >= cutoff
        ]
        # Everything not about to be posted counts as seen, so it never gets posted later.
        fresh_keys = {e.key for e in fresh}
        remember(state, reader.user_id, [k for k in all_keys if k not in fresh_keys])
        print(f"· {name}: {len(fresh)} new")
        new_events.extend(reversed(fresh))  # pages list newest first
    new_events.sort(key=_posting_order)  # stable, so untimed same-day events keep their order
    return new_events, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--dry-run", action="store_true", help="print embeds instead of posting; don't save state")
    parser.add_argument(
        "--replay", type=int, metavar="N",
        help="post each reader's N most recent events regardless of state (for testing); state is untouched",
    )
    args = parser.parse_args(argv)

    if not args.config.exists():
        print(f"No {args.config} found - copy config.example.toml to {args.config} to get started. Nothing to do.")
        return 0
    config = load_config(args.config)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is not set (add it as a repository secret).", file=sys.stderr)
        return 1

    today = datetime.now(PACIFIC).date()
    if args.replay:
        events = []
        for reader in config.readers:
            name, updates, shelf, progress = fetch_reader(reader, today)
            if not updates:
                updates = shelf_events(shelf, [], name, reader.user_id, [], today)
            updates = sorted(updates + (progress or []), key=_posting_order, reverse=True)
            events.extend(reversed([e for e in updates if e.kind in config.events][: args.replay]))
        events.sort(key=_posting_order)
        failures = 0
        state = None
    else:
        state = load_state(args.state)
        events, failures = collect_new_events(config, state, today)

    for event in events:
        if event.kind == "quote" and not event.book_title:
            add_quote_book(event)

    for batch in batch_events(events):
        embeds = [build_embed(e) for e in batch]
        if args.dry_run:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        else:
            post_to_discord(webhook, embeds, config)
        if state is not None:
            for e in batch:
                remember(state, e.reader_id, [e.key])
            if not args.dry_run:
                save_state(args.state, state)  # save per batch so a mid-run failure doesn't repost
    if state is not None and not args.dry_run:
        save_state(args.state, state)

    print(f"Posted {len(events)} event(s)." if not args.dry_run else f"Would post {len(events)} event(s).")
    return 1 if failures == len(config.readers) else 0


if __name__ == "__main__":
    sys.exit(main())
