import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import goodreads_bot as bot  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "updates.xml").read_text()
READER = bot.Reader("96005733")
NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def feed(*items: str, title: str = "Sam's Updates") -> str:
    return f"<rss><channel><title>{title}</title>{''.join(items)}</channel></rss>"


def item(guid: str, title: str, desc: str, date: str = "Tue, 22 Sep 2026 17:00:00 -0700") -> str:
    return (
        f"<item><guid>{guid}</guid><pubDate>{date}</pubDate>"
        f"<title><![CDATA[{title}]]></title><link>https://www.goodreads.com/review/show/1</link>"
        f"<description><![CDATA[{desc}]]></description></item>"
    )


BOOK = (
    '<a href="/book/show/1-dune"><img alt="Dune by Frank Herbert" '
    'src="https://i.gr-assets.com/books/1l/1._SY75_.jpg" /></a> {verb} '
    '<a class="bookTitle" href="/book/show/1-dune">Dune</a> <span class="by">by</span> '
    '<a class="authorName" href="/author/show/1">Frank Herbert</a><br/>{after}'
)


class ParseFeedTest(unittest.TestCase):
    def test_real_feed(self):
        name, events = bot.parse_feed(FIXTURE, READER)
        self.assertEqual(name, "Aadarsh")
        self.assertEqual(
            [e.kind for e in events],
            ["want", "rated", "finished", "progress", "started", "want", "want"],
        )
        rated = events[1]
        self.assertEqual(rated.rating, 5)
        self.assertEqual(rated.book_title, "A Drop of Corruption (Ana and Din Mysteries, #2)")
        self.assertEqual(rated.author, "Robert Jackson Bennett")
        self.assertIsNone(rated.review)
        progress = events[3]
        self.assertEqual(progress.progress, "50% done")
        self.assertEqual(progress.book_title, "A Drop of Corruption")
        self.assertEqual(progress.book_url, "https://www.goodreads.com/book/show/213681682-a-drop-of-corruption")
        self.assertNotIn("_SY75_", progress.cover_url)

    def test_configured_name_wins(self):
        name, events = bot.parse_feed(FIXTURE, bot.Reader("96005733", name="Adi"))
        self.assertEqual(name, "Adi")
        self.assertTrue(all(e.reader_name == "Adi" for e in events))

    def test_page_progress(self):
        xml = feed(item(
            "UserStatus9", "Sam is on page 120 of 412 of Dune",
            '<a href="/user/show/5">Sam</a> is on page 120 of 412 of '
            '&lt;a href=&quot;/book/show/1-dune&quot;&gt;Dune&lt;/a&gt;.',
        ))
        _, [event] = bot.parse_feed(xml, READER)
        self.assertEqual(event.progress, "on page 120 of 412")
        self.assertEqual(event.book_title, "Dune")

    def test_review_text_and_bare_add(self):
        xml = feed(
            item("Review1", "Sam added 'Dune'", BOOK.format(verb="Sam gave 4 stars to", after="Loved the <b>sand</b>.")),
            item("Review2", "Sam added 'Dune'", BOOK.format(verb="Sam added", after="")),
        )
        _, events = bot.parse_feed(xml, READER)
        self.assertEqual(len(events), 1)  # the bare "added" is dropped
        self.assertEqual(events[0].rating, 4)
        self.assertEqual(events[0].review, "Loved the sand.")

    def test_alternate_shelf_wording(self):
        xml = feed(
            item("ReadStatus1", "Sam is currently reading 'Dune'", BOOK.format(verb="Sam is currently reading", after="")),
            item("ReadStatus2", "Sam has read 'Dune'", BOOK.format(verb="Sam has read", after="")),
        )
        self.assertEqual([e.kind for e in bot.parse_feed(xml, READER)[1]], ["started", "finished"])

    def test_fractional_ratings(self):
        xml = feed(
            item("Review1", "Sam added 'Dune'", BOOK.format(verb="Sam gave 3.5 stars to", after="")),
            item("Review2", "Sam added 'Dune'", BOOK.format(verb="Sam gave 3.75 stars to", after="")),
            item("Review3", "Sam added 'Dune'", BOOK.format(verb="Sam gave 1 star to", after="")),
        )
        self.assertEqual([e.rating for e in bot.parse_feed(xml, READER)[1]], [3.5, 3.75, 1])

    def test_duplicate_items_are_collapsed(self):
        rated = BOOK.format(verb="Sam gave 3.5 stars to", after="")
        xml = feed(item("Review1", "Sam added 'Dune'", rated), item("Review1", "Sam added 'Dune'", rated))
        self.assertEqual(len(bot.parse_feed(xml, READER)[1]), 1)

    def test_ignores_junk_items(self):
        xml = feed(
            item("Recommendation1", "&lt;Recommendation id=1&gt;", ""),
            item("UserFollowing1", "#&lt;UpdateArray:0x1&gt;", ""),
        )
        self.assertEqual(bot.parse_feed(xml, READER)[1], [])


class ConfigTest(unittest.TestCase):
    def test_parse_user_id(self):
        for value in (
            "96005733",
            96005733,
            "96005733-aadarsh",
            "https://www.goodreads.com/user/show/96005733-aadarsh",
            "https://www.goodreads.com/user/updates_rss/96005733-aadarsh",
            "https://www.goodreads.com/review/list_rss/96005733?shelf=read",
        ):
            self.assertEqual(bot.parse_user_id(value), "96005733", value)

    def test_example_config_loads(self):
        config = bot.load_config(Path(__file__).resolve().parent.parent / "config.example.toml")
        self.assertTrue(config.readers)

    def test_rejects_unknown_event(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[settings]\nevents = ["finished", "nope"]\n[[readers]]\ngoodreads = "1"\n')
        with self.assertRaises(ValueError):
            bot.load_config(Path(f.name))


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.config = bot.Config(readers=[READER], max_age_days=30)

    def collect(self, state, xml=FIXTURE, now=NOW):
        with mock.patch.object(bot, "fetch", return_value=xml):
            return bot.collect_new_events(self.config, state, now)

    def test_new_reader_is_seeded_silently(self):
        state = {"readers": {}}
        events, failures = self.collect(state)
        self.assertEqual((events, failures), ([], 0))
        self.assertEqual(len(state["readers"]["96005733"]["seen"]), 7)

    def test_only_unseen_events_are_returned_oldest_first(self):
        state = {"readers": {}}
        self.collect(state)
        seen = state["readers"]["96005733"]["seen"]
        seen.remove("Review8940644436=5")
        seen.remove("ReadStatus11418184814")
        events, _ = self.collect(state)
        self.assertEqual([e.guid for e in events], ["Review8940644436", "ReadStatus11418184814"])

    def test_disabled_and_stale_events_are_marked_seen_not_posted(self):
        self.config.events = ("finished",)
        self.config.max_age_days = 1
        state = {"readers": {"96005733": {"seen": []}}}
        events, _ = self.collect(state)
        self.assertEqual([e.kind for e in events], [])  # the only "finished" is 3 days old
        self.assertEqual(len(state["readers"]["96005733"]["seen"]), 7)

    def rating_feed(self, rating):
        return feed(item("Review1", "Sam added 'Dune'", BOOK.format(verb=f"Sam gave {rating} stars to", after="")))

    def test_rerating_posts_again_but_resaving_does_not(self):
        state = {"readers": {"96005733": {"seen": ["Review1=4"]}}}
        self.assertEqual(self.collect(state, self.rating_feed(4), NOW), ([], 0))
        events, _ = self.collect(state, self.rating_feed(3.5), NOW)
        self.assertEqual([e.rating for e in events], [3.5])

    def test_legacy_state_only_covers_whole_star_ratings(self):
        # Before ratings were keyed by score, state.json held bare guids.
        state = {"readers": {"96005733": {"seen": ["Review1"]}}}
        self.assertEqual(self.collect(state, self.rating_feed(3), NOW), ([], 0))
        events, _ = self.collect(state, self.rating_feed(3.5), NOW)
        self.assertEqual([e.rating for e in events], [3.5])

    def test_feed_failure_keeps_going(self):
        with mock.patch.object(bot, "fetch", side_effect=OSError("boom")):
            events, failures = bot.collect_new_events(self.config, {"readers": {}}, NOW)
        self.assertEqual((events, failures), ([], 1))


class EmbedTest(unittest.TestCase):
    def test_rated_embed(self):
        _, events = bot.parse_feed(FIXTURE, READER)
        embed = bot.build_embed(events[1])
        self.assertEqual(embed["author"]["name"], "Aadarsh rated")
        self.assertIn("★★★★★", embed["description"])
        self.assertIn("Robert Jackson Bennett", embed["description"])
        self.assertTrue(embed["thumbnail"]["url"].startswith("https://"))
        json.dumps(embed)  # must be serialisable

    def test_stars(self):
        self.assertEqual(bot.stars(5.0), "★★★★★")
        self.assertEqual(bot.stars(3.0), "★★★☆☆")
        self.assertEqual(bot.stars(3.5), "★★★½☆ (3.5)")
        self.assertEqual(bot.stars(3.75), "★★★¾☆ (3.75)")
        self.assertEqual(bot.stars(4.25), "★★★★¼ (4.25)")


class MainTest(unittest.TestCase):
    def test_posts_new_events_and_saves_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config.toml").write_text('[settings]\nmax_age_days = 3650\n[[readers]]\ngoodreads = "96005733"\n')
            state = tmp / "state.json"
            state.write_text(json.dumps({"readers": {"96005733": {"seen": []}}}))
            posted = []
            with mock.patch.object(bot, "fetch", return_value=FIXTURE), \
                 mock.patch.object(bot, "post_to_discord", side_effect=lambda url, embeds, cfg: posted.append(embeds)), \
                 mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.invalid"}):
                self.assertEqual(bot.main(["--config", str(tmp / "config.toml"), "--state", str(state)]), 0)
                self.assertEqual(sum(map(len, posted)), 7)
                self.assertEqual(len(json.loads(state.read_text())["readers"]["96005733"]["seen"]), 7)
                posted.clear()
                bot.main(["--config", str(tmp / "config.toml"), "--state", str(state)])
                self.assertEqual(posted, [])  # nothing new the second time

    def test_missing_config_is_a_noop(self):
        self.assertEqual(bot.main(["--config", "/nonexistent/config.toml"]), 0)


if __name__ == "__main__":
    unittest.main()
