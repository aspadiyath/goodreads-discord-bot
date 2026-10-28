import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import goodreads_bot as bot  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
UPDATES = (FIXTURES / "updates_aadarsh.html").read_text()
UPDATES_OTIS = (FIXTURES / "updates_otis.html").read_text()
UPDATES_ELIZABETH = (FIXTURES / "updates_elizabeth.html").read_text()
SHELF = (FIXTURES / "shelf_aadarsh.js").read_text()
SHELF_OTIS = (FIXTURES / "shelf_otis.js").read_text()
EMPTY_UPDATES = (
    '<div class="goodreads_container" id="gr_reviews_widget"><div class="gr_reviews_header">'
    '<div class="gr_reviewer_name"><a href="/user/show/7-roy">Roy</a></div></div>'
    '<div class="gr_review_container"></div></div>'
)
QUOTE_PAGE = """<meta content='A quote from The City &amp; The City' property='og:title'>
<meta content='https://www.goodreads.com/book/show/40212456-the-city-the-city' property='og:see_also'>"""

READER = bot.Reader("96005733")
TODAY = date(2026, 10, 27)


def fake_fetch(updates=UPDATES, shelf=SHELF):
    def fetch(url):
        if "user_update_widget" in url:
            return updates
        if "custom_widget" in url:
            return shelf
        if "/quotes/" in url:
            return QUOTE_PAGE
        raise AssertionError(f"unexpected fetch {url}")
    return fetch


def update_block(day, action, book_id="1", title="Dune", stars=""):
    return (
        f'<div class="update "  ><p>{day}</p><div class="updateAction">{action} {stars}'
        f'<a class="bookTitle" rel="nofollow" href="/book/show/{book_id}-x">{title}</a>'
        '<span class="by smallText">by</span><a class="authorName" href="/author/show/1">Frank Herbert</a></div></div>'
    )


def star_fills(*fills):
    return '<span class=" staticStars notranslate">' + "".join(
        f'<span size="15x15" class="staticStar p{f}"></span>' for f in fills
    ) + "</span>"


class UpdatesWidgetTest(unittest.TestCase):
    def parse(self, page=UPDATES, reader=READER):
        return bot.parse_updates_widget(page, reader, TODAY)

    def test_real_updates(self):
        name, events = self.parse()
        self.assertEqual(name, "Aadarsh")
        self.assertEqual(
            [e.kind for e in events],
            ["quote", "started", "rated", "want", "want", "want", "want", "want", "want", "want"],
        )
        started = events[1]
        self.assertEqual(started.book_title, "The City & The City")
        self.assertEqual(started.author, "China Miéville")
        self.assertEqual(started.book_url, "https://www.goodreads.com/book/show/40212456")
        self.assertEqual(started.day, date(2026, 10, 17))
        self.assertNotIn("_SY75_", started.cover_url)
        self.assertEqual(started.key, "started:40212456:2026-10-17")

    def test_quote(self):
        quote = self.parse()[1][0]
        self.assertEqual(quote.key, "quote:12357143")
        self.assertEqual(
            quote.quote,
            "The aim of a PhD’s to ensure that no one, including your advisor, "
            "understands what you’re doing after the first couple of years.",
        )
        self.assertEqual(quote.author, "China Miéville")
        self.assertEqual(quote.link, "https://www.goodreads.com/quotes/12357143")

    def test_partial_star_fill_is_approximate(self):
        rated = self.parse()[1][2]
        self.assertEqual(rated.book_title, "A Trade of Blood (Ana and Din Mysteries, #3)")
        self.assertEqual((rated.rating, rated.rating_approx), (3.5, True))

    def test_whole_star_rating_and_review(self):
        _, events = self.parse(UPDATES_OTIS, bot.Reader("1"))
        rated = next(e for e in events if e.kind == "rated")
        self.assertEqual(rated.book_title, "The Long Ships")
        self.assertEqual((rated.rating, rated.rating_approx), (5, False))
        self.assertTrue(rated.review.startswith("I love good historical fiction"))

    def test_full_review_text_not_the_truncated_preview(self):
        _, events = self.parse(UPDATES_ELIZABETH, bot.Reader("5"))
        hagwon = next(e for e in events if e.book_title == "American Hagwon" and e.kind == "finished")
        self.assertTrue(hagwon.review.endswith("Another book I was grateful to have read."))
        self.assertIn('The "devil" femme fatal.', hagwon.review)
        self.assertEqual(hagwon.link, "https://www.goodreads.com/review/show/9025709180")

    def test_ignores_follows_likes_and_news(self):
        _, events = self.parse(UPDATES_OTIS, bot.Reader("1"))
        self.assertEqual([e.kind for e in events], ["want"] * 6 + ["started", "rated"])

    def test_configured_name_wins(self):
        name, events = self.parse(reader=bot.Reader("96005733", name="Adi"))
        self.assertEqual(name, "Adi")
        self.assertTrue(all(e.reader_name == "Adi" for e in events))

    def test_dates_carry_forward_and_duplicates_collapse(self):
        page = (
            update_block("October 20", "Sam started reading")
            + update_block("", "Sam started reading").replace("<p></p>", "")
            + update_block("", "Sam has read", book_id="2", title="Emma").replace("<p></p>", "")
        )
        _, events = self.parse(page)
        self.assertEqual([(e.kind, e.day) for e in events], [("started", date(2026, 10, 20)), ("finished", date(2026, 10, 20))])

    def test_parse_day(self):
        self.assertEqual(bot._parse_day("October 26", TODAY), date(2026, 10, 26))
        self.assertEqual(bot._parse_day("August 24, 2025", TODAY), date(2025, 8, 24))
        self.assertEqual(bot._parse_day("December 30", date(2027, 1, 2)), date(2026, 12, 30))
        self.assertIsNone(bot._parse_day("yesterday", TODAY))

    def test_empty_widget(self):
        self.assertEqual(self.parse(EMPTY_UPDATES, bot.Reader("7")), ("Roy", []))


class ShelfWidgetTest(unittest.TestCase):
    def test_real_shelf(self):
        shelf = bot.parse_shelf_widget(SHELF)
        self.assertEqual([b.rating for b in shelf], [3.0, 1.5, 3.75, 5.0, 0.0])
        trade = shelf[2]
        self.assertEqual(trade.title, "A Trade of Blood (Ana and Din Mysteries, #3)")
        self.assertEqual((trade.review_id, trade.book_id), ("8967278832", "239560521"))
        self.assertEqual(trade.author, "Robert Jackson Bennett")
        self.assertEqual(len(bot.parse_shelf_widget(SHELF_OTIS)), 30)

    def test_exact_rating_replaces_star_fill(self):
        _, events = bot.parse_updates_widget(UPDATES, READER, TODAY)
        bot.apply_shelf_ratings(events, bot.parse_shelf_widget(SHELF))
        rated = events[2]
        self.assertEqual((rated.rating, rated.rating_approx), (3.75, False))
        self.assertEqual(rated.link, "https://www.goodreads.com/review/show/8967278832")

    def test_rejects_unexpected_format(self):
        with self.assertRaises(ValueError):
            bot.parse_shelf_widget("<html>Sign in</html>")


class ConfigTest(unittest.TestCase):
    def test_parse_user_id(self):
        for value in (
            "96005733",
            96005733,
            "96005733-aadarsh",
            "https://www.goodreads.com/user/show/96005733-aadarsh",
            "https://www.goodreads.com/user/updates_rss/96005733-aadarsh",
            "https://www.goodreads.com/widgets/user_update_widget?num_updates=3&user=96005733",
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

    def collect(self, state, **pages):
        with mock.patch.object(bot, "fetch", side_effect=fake_fetch(**pages)):
            return bot.collect_new_events(self.config, state, TODAY)

    def seeded(self):
        state = {"version": bot.STATE_VERSION, "readers": {}}
        self.assertEqual(self.collect(state), ([], 0))
        return state, state["readers"]["96005733"]["seen"]

    def test_new_reader_is_seeded_silently(self):
        _, seen = self.seeded()
        self.assertEqual(len(seen), 10 + 5)  # updates + shelf books

    def test_only_unseen_events_are_returned_oldest_first(self):
        state, seen = self.seeded()
        seen.remove("quote:12357143")
        seen.remove("started:40212456:2026-10-17")
        events, _ = self.collect(state)
        self.assertEqual([e.key for e in events], ["started:40212456:2026-10-17", "quote:12357143"])

    def test_shelf_fills_gaps_in_the_updates_widget(self):
        state, seen = self.seeded()
        seen.remove("shelf:8996678277=3")  # The Lion's Tusk: on the shelf, not in the updates widget
        seen.remove("shelf:8967278832=3.75")  # A Trade of Blood: the updates widget already covers it
        events, _ = self.collect(state)
        self.assertEqual([(e.kind, e.book_title, e.rating) for e in events],
                         [("finished", "The Lion’s Tusk (Magic & Mystery, #3)", 3.0)])

    def test_shelf_rerating(self):
        state, seen = self.seeded()
        seen[seen.index("shelf:8996678277=3")] = "shelf:8996678277=2.5"
        events, _ = self.collect(state)
        self.assertEqual([(e.kind, e.rating) for e in events], [("rated", 3.0)])

    def test_empty_updates_widget_falls_back_to_shelf(self):
        state = {"version": bot.STATE_VERSION, "readers": {"96005733": {"seen": ["shelf:8969193595=1.5"]}}}
        events, _ = self.collect(state, updates=EMPTY_UPDATES)
        self.assertEqual([e.book_title.split(" (")[0] for e in events],
                         ["Steering The Craft: A Twenty-First-Century Guide to Sailing the Sea of Story",
                          "A Drop of Corruption", "A Trade of Blood", "The Lion’s Tusk"])
        self.assertEqual(events[0].rating, None)  # finished but unrated

    def test_disabled_and_stale_events_are_marked_seen_not_posted(self):
        self.config.events = ("started", "quote")
        self.config.max_age_days = 3
        state = {"version": bot.STATE_VERSION, "readers": {"96005733": {"seen": [
            "shelf:8996678277=3", "shelf:8969193595=1.5", "shelf:8967278832=3.75",
            "shelf:8940644436=5", "shelf:8927966371=0",
        ]}}}
        events, _ = self.collect(state)
        self.assertEqual([e.kind for e in events], ["quote"])  # "started" was on Oct 17
        self.assertEqual(len(state["readers"]["96005733"]["seen"]), 15 - 1)  # all but the pending quote

    def test_shelf_widget_failure_still_posts_updates(self):
        state, seen = self.seeded()
        seen.remove("rated:239560521:2026-10-17")
        events, _ = self.collect(state, shelf="<html>oops</html>")
        self.assertEqual([(e.rating, e.rating_approx) for e in events], [(3.5, True)])

    def test_reader_failure_keeps_going(self):
        with mock.patch.object(bot, "fetch", side_effect=OSError("boom")):
            events, failures = bot.collect_new_events(self.config, {"readers": {}}, TODAY)
        self.assertEqual((events, failures), ([], 1))


class StateTest(unittest.TestCase):
    def test_rss_era_state_is_reseeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"readers": {"96005733": {"seen": ["ReadStatus1", "Review2=4"]}}}))
            self.assertEqual(bot.load_state(path), {"version": bot.STATE_VERSION, "readers": {}})


class EmbedTest(unittest.TestCase):
    def events(self):
        _, events = bot.parse_updates_widget(UPDATES, READER, TODAY)
        return events

    def test_rated_embed(self):
        rated = self.events()[2]
        bot.apply_shelf_ratings([rated], bot.parse_shelf_widget(SHELF))
        embed = bot.build_embed(rated)
        self.assertEqual(embed["author"]["name"], "Aadarsh rated")
        self.assertIn("★★★¾☆ (3.75)", embed["description"])
        self.assertIn("Robert Jackson Bennett", embed["description"])
        self.assertEqual(embed["url"], "https://www.goodreads.com/book/show/239560521")
        self.assertTrue(embed["thumbnail"]["url"].startswith("https://"))
        json.dumps(embed)  # must be serialisable

    def test_quote_embed_with_book_lookup(self):
        quote = self.events()[0]
        with mock.patch.object(bot, "fetch", return_value=QUOTE_PAGE):
            bot.add_quote_book(quote)
        embed = bot.build_embed(quote)
        self.assertEqual(embed["author"]["name"], "Aadarsh shared a quote")
        self.assertEqual(embed["title"], "The City & The City")
        self.assertEqual(embed["url"], "https://www.goodreads.com/quotes/12357143")
        self.assertIn("> The aim of a PhD", embed["description"])
        self.assertIn("— **China Miéville**", embed["description"])

    def test_quote_embed_without_book_lookup(self):
        quote = self.events()[0]
        with mock.patch.object(bot, "fetch", side_effect=OSError("down")):
            bot.add_quote_book(quote)  # must not raise
        self.assertEqual(bot.build_embed(quote)["title"], "A quote by China Miéville")

    def test_stars(self):
        self.assertEqual(bot.stars(5.0), "★★★★★")
        self.assertEqual(bot.stars(3.0), "★★★☆☆")
        self.assertEqual(bot.stars(3.5), "★★★½☆ (3.5)")
        self.assertEqual(bot.stars(3.75), "★★★¾☆ (3.75)")
        self.assertEqual(bot.stars(4.25), "★★★★¼ (4.25)")
        self.assertEqual(bot.stars(3.5, approx=True), "★★★½☆ (between 3.25 and 3.75)")


class MainTest(unittest.TestCase):
    def test_posts_new_events_and_saves_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config.toml").write_text('[settings]\nmax_age_days = 3650\n[[readers]]\ngoodreads = "96005733"\n')
            state = tmp / "state.json"
            state.write_text(json.dumps({"version": bot.STATE_VERSION, "readers": {"96005733": {"seen": []}}}))
            posted = []
            with mock.patch.object(bot, "fetch", side_effect=fake_fetch()), \
                 mock.patch.object(bot, "post_to_discord", side_effect=lambda url, embeds, cfg: posted.append(embeds)), \
                 mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://example.invalid"}):
                self.assertEqual(bot.main(["--config", str(tmp / "config.toml"), "--state", str(state)]), 0)
                # 10 updates + 4 shelf books the updates widget doesn't mention, in 2 messages
                self.assertEqual([len(batch) for batch in posted], [10, 4])
                self.assertEqual(len(json.loads(state.read_text())["readers"]["96005733"]["seen"]), 15)
                posted.clear()
                bot.main(["--config", str(tmp / "config.toml"), "--state", str(state)])
                self.assertEqual(posted, [])  # nothing new the second time

    def test_missing_config_is_a_noop(self):
        self.assertEqual(bot.main(["--config", "/nonexistent/config.toml"]), 0)


if __name__ == "__main__":
    unittest.main()
