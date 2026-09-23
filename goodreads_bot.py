#!/usr/bin/env python3
"""Post Goodreads activity (started / finished / progress / ratings) to a Discord channel.

Reads each reader's public Goodreads updates RSS feed, figures out which
events are new since the last run (tracked in state.json), and posts them to
a Discord webhook as embeds. Standard library only, Python 3.11+.
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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

GOODREADS = "https://www.goodreads.com"
FEED_URL = GOODREADS + "/user/updates_rss/{user_id}"
USER_AGENT = "goodreads-discord-bot/1.0 (+https://github.com)"

ALL_EVENTS = ("started", "progress", "finished", "rated", "want")
EVENT_STYLE = {
    # kind: (verb shown in the embed header, embed color)
    "started": ("started reading", 0x3B82F6),
    "progress": ("made progress on", 0x8B5CF6),
    "finished": ("finished reading", 0x22C55E),
    "rated": ("rated", 0xF59E0B),
    "want": ("wants to read", 0x94A3B8),
}
SEEN_LIMIT = 200  # guids remembered per reader; the feed itself only holds ~10 items


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
    """Accept a bare id, '96005733-name', or any goodreads profile/feed URL."""
    match = re.search(r"(\d+)", str(value).rsplit("/", 1)[-1]) or re.search(r"(\d+)", str(value))
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


# --------------------------------------------------------------------------- feed parsing


@dataclass
class Event:
    guid: str
    kind: str
    reader_name: str
    reader_id: str
    published: datetime
    book_title: str
    book_url: str | None = None
    author: str | None = None
    cover_url: str | None = None
    rating: int | None = None
    progress: str | None = None
    review: str | None = None
    link: str | None = None


def _strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else GOODREADS + url


def _large_cover(url: str | None) -> str | None:
    # Goodreads serves thumbnails like "12345._SY75_.jpg"; dropping the size suffix gives full size.
    return re.sub(r"\._S[XY]\d+_(?=\.)", "", url) if url else None


def classify(guid: str, title: str) -> str | None:
    """Map a feed item to one of ALL_EVENTS, or None for items we ignore."""
    if guid.startswith("ReadStatus"):
        # Goodreads words the same event differently depending on how it was logged.
        if " started reading " in title or " is currently reading " in title:
            return "started"
        if " finished reading " in title or " has read " in title:
            return "finished"
        if " wants to read " in title:
            return "want"
    elif guid.startswith("UserStatus"):
        return "progress"
    elif guid.startswith("Review"):
        return "rated"
    return None


def parse_feed(xml_text: str, reader: Reader) -> tuple[str, list[Event]]:
    """Return (display name, events newest-first) for one reader's updates feed."""
    channel = ET.fromstring(xml_text).find("channel")
    if channel is None:
        raise ValueError("not an RSS feed")
    feed_title = channel.findtext("title") or ""
    name = reader.name or re.sub(r"'s Updates$", "", feed_title).strip() or reader.user_id

    events = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        title = " ".join((item.findtext("title") or "").split())
        kind = classify(guid, title)
        if not guid or not kind:
            continue
        # Progress updates double-escape their HTML, so unescape once before matching.
        desc = item.findtext("description") or ""
        if kind == "progress":
            desc = html.unescape(desc)

        def find(pattern: str) -> str | None:
            m = re.search(pattern, desc, re.S)
            return _strip_tags(m.group(1)) if m else None

        book_href = re.search(r'href="([^"]*/book/show/[^"]+)"', desc)
        img = re.search(r'<img[^>]*\balt="([^"]*)"[^>]*\bsrc="([^"]+)"', desc)
        book_title = find(r'class="bookTitle"[^>]*>(.*?)</a>') or find(r'href="[^"]*/book/show/[^"]*"[^>]*>([^<]+)</a>')
        author = find(r'class="authorName"[^>]*>(.*?)</a>')
        if img and " by " in img.group(1):
            alt_title, _, alt_author = html.unescape(img.group(1)).rpartition(" by ")
            book_title = book_title or alt_title
            author = author or alt_author
        if not book_title:
            m = re.search(r"'(.+)'$", title)
            book_title = m.group(1) if m else title

        event = Event(
            guid=guid,
            kind=kind,
            reader_name=name,
            reader_id=reader.user_id,
            published=parsedate_to_datetime(item.findtext("pubDate").strip()),
            book_title=book_title,
            book_url=_absolute(book_href.group(1)) if book_href else None,
            author=author,
            cover_url=_large_cover(img.group(2)) if img else None,
            link=(item.findtext("link") or "").strip() or None,
        )

        if kind == "progress":
            m = re.search(r"\bis (.+?)\s*<a[^>]*/book/show/", desc, re.S)
            if m:
                event.progress = re.sub(r"\s+(with|of)$", "", " ".join(m.group(1).split()))
        elif kind == "rated":
            m = re.search(r"gave (\d) stars?", desc)
            event.rating = int(m.group(1)) if m else None
            # Anything after the author link is the written review (if there is one).
            m = re.search(r'class="authorName"[^>]*>.*?</a>(.*)', desc, re.S)
            review = _strip_tags(m.group(1)) if m else ""
            event.review = review or None
            if not event.rating and not event.review:
                continue  # plain "added" with no rating/review duplicates the shelf event
        events.append(event)
    return name, events


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


def stars(rating: int) -> str:
    return "★" * rating + "☆" * (5 - rating)


def build_embed(event: Event) -> dict:
    verb, color = EVENT_STYLE[event.kind]
    lines = []
    if event.author:
        lines.append(f"by **{event.author}**")
    if event.progress:
        lines.append(f"📖 {event.progress}")
    if event.rating:
        lines.append(stars(event.rating))
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
        "timestamp": event.published.astimezone(timezone.utc).isoformat(),
    }
    if event.book_url:
        embed["url"] = event.book_url
    if lines:
        embed["description"] = "\n".join(lines)
    if event.cover_url:
        embed["thumbnail"] = {"url": event.cover_url}
    return embed


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
    if path.exists():
        return json.loads(path.read_text())
    return {"readers": {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def remember(state: dict, reader_id: str, guids: list[str]) -> None:
    entry = state["readers"].setdefault(reader_id, {"seen": []})
    seen = [g for g in guids if g not in entry["seen"]] + entry["seen"]
    entry["seen"] = seen[:SEEN_LIMIT]


# --------------------------------------------------------------------------- main


def collect_new_events(config: Config, state: dict, now: datetime) -> tuple[list[Event], int]:
    """Fetch every feed; return (unseen events oldest-first, number of failed feeds).

    Readers we have never seen before are "seeded": their current feed is
    marked as seen without posting, so adding a friend doesn't dump their
    history into the channel.
    """
    new_events, failures = [], 0
    cutoff = now - timedelta(days=config.max_age_days)
    for reader in config.readers:
        try:
            name, events = parse_feed(fetch(FEED_URL.format(user_id=reader.user_id)), reader)
        except Exception as err:  # one broken/private feed shouldn't block everyone else
            print(f"! {reader.name or reader.user_id}: couldn't read feed ({err})", file=sys.stderr)
            failures += 1
            continue

        known = state["readers"].get(reader.user_id)
        if known is None:
            remember(state, reader.user_id, [e.guid for e in events])
            print(f"+ {name}: new reader, seeded {len(events)} existing events (not posted)")
            continue

        fresh = [
            e for e in events
            if e.guid not in known["seen"] and e.kind in config.events and e.published >= cutoff
        ]
        # Filtered-out events count as seen so they never get posted later.
        remember(state, reader.user_id, [e.guid for e in events if e not in fresh])
        print(f"· {name}: {len(fresh)} new")
        new_events.extend(fresh)
    new_events.sort(key=lambda e: e.published)
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

    if args.replay:
        events = []
        for reader in config.readers:
            _, feed = parse_feed(fetch(FEED_URL.format(user_id=reader.user_id)), reader)
            events.extend([e for e in feed if e.kind in config.events][: args.replay])
        events.sort(key=lambda e: e.published)
        failures = 0
        state = None
    else:
        state = load_state(args.state)
        events, failures = collect_new_events(config, state, datetime.now(timezone.utc))

    # Discord allows up to 10 embeds per message.
    for i in range(0, len(events), 10):
        batch = events[i : i + 10]
        embeds = [build_embed(e) for e in batch]
        if args.dry_run:
            print(json.dumps(embeds, indent=2, ensure_ascii=False))
        else:
            post_to_discord(webhook, embeds, config)
        if state is not None:
            for e in batch:
                remember(state, e.reader_id, [e.guid])
            if not args.dry_run:
                save_state(args.state, state)  # save per batch so a mid-run failure doesn't repost
    if state is not None and not args.dry_run:
        save_state(args.state, state)

    print(f"Posted {len(events)} event(s)." if not args.dry_run else f"Would post {len(events)} event(s).")
    return 1 if failures == len(config.readers) else 0


if __name__ == "__main__":
    sys.exit(main())
