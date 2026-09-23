# Goodreads → Discord

Post your friend group's Goodreads activity into a Discord channel.

> **Sam started reading** *Dune* — by **Frank Herbert**
> **Sam made progress on** *Dune* — 📖 on page 120 of 412
> **Sam finished reading** *Dune*
> **Sam rated** *Dune* — ★★★★☆ *"Loved the sand."*

- **No server needed.** A GitHub Actions workflow checks everyone's feed every hour.
- **No Goodreads API key.** It reads each person's public updates RSS feed.
- **No dependencies.** It's one Python file that uses only the standard library.

## How it works

1. Every hour, the workflow fetches `goodreads.com/user/updates_rss/<id>` for each person in `config.toml`.
2. It posts any events it hasn't seen before to your Discord webhook as embeds with book covers.
3. It records the IDs of events it has already posted in `state.json`, and the workflow commits that file back to the repo.

The first time the bot sees a new person, it marks their current feed as already posted instead of posting it. Adding a friend won't flood the channel with their history.

## Setup (about 5 minutes)

### 1. Get your own copy of this repo

Choose one of these:

- **Public:** click **Fork**. Your friends' Goodreads IDs will be visible, but those profiles are public anyway. The webhook URL stays private because it's stored as a secret.
- **Private:** GitHub doesn't allow private forks of public repos. Make a private copy that can still pull updates from this repo instead:

  ```sh
  # Create an empty private repo on GitHub first (e.g. my-book-club-bot), then:
  git clone https://github.com/<upstream-owner>/goodreads-discord-bot.git my-book-club-bot
  cd my-book-club-bot
  git remote rename origin upstream
  git remote add origin https://github.com/<you>/my-book-club-bot.git
  git push -u origin main

  # Later, to pick up improvements:
  git pull upstream main && git push
  ```

  With the `gh` CLI, create the empty repo with `gh repo create my-book-club-bot --private`.

### 2. Create a Discord webhook

In Discord, open **Channel settings → Integrations → Webhooks → New Webhook**. Choose the channel, then click **Copy Webhook URL**.

In your GitHub repo, open **Settings → Secrets and variables → Actions → New repository secret**:

- Name: `DISCORD_WEBHOOK_URL`
- Value: the webhook URL

### 3. Add your readers

```sh
cp config.example.toml config.toml
```

Edit `config.toml` so there's one `[[readers]]` block per person. For `goodreads`, paste the person's profile URL (for example `https://www.goodreads.com/user/show/96005733-aadarsh`) or just their numeric ID:

```toml
[[readers]]
name = "Aadarsh"
goodreads = "https://www.goodreads.com/user/show/96005733-aadarsh"

[[readers]]
name = "Sam"
goodreads = "12345678"
```

Each person's Goodreads profile must be public. Commit and push `config.toml`.

### 4. Turn it on

1. Go to the **Actions** tab. On a fork, click **I understand my workflows, go ahead and enable them**.
2. Open **Check Goodreads feeds → Run workflow** and set **replay** to `2`. This posts each person's two most recent events, so you can check that the webhook works.
3. Run it again with replay set to `0`. This first normal run marks everyone's current feed as posted. From then on, the bot only posts new activity.

## Configuration

You can change these in the `[settings]` section of `config.toml`:

| Setting | Default | What it does |
|---|---|---|
| `events` | all five | Which kinds of event to post: `started`, `progress`, `finished`, `rated`, `want` |
| `max_age_days` | `3` | Events older than this are never posted, so there's no backlog after a pause |
| `bot_username` / `bot_avatar_url` | webhook's own | Overrides the name and avatar shown in Discord |

To change how often it runs, edit the `cron` line in [`.github/workflows/check-feeds.yml`](.github/workflows/check-feeds.yml).

## Running locally

Requires Python 3.11+.

```sh
python3 goodreads_bot.py --dry-run             # print what would be posted; doesn't save state
python3 goodreads_bot.py --dry-run --replay 3  # preview each reader's last 3 events
DISCORD_WEBHOOK_URL=... python3 goodreads_bot.py
python3 -m unittest discover -s tests          # run the tests
```

## Limitations

- **Short feeds.** Goodreads updates feeds only hold about the last 10 items per person. If someone logs more than 10 updates between checks, the older ones are missed. With hourly checks this rarely happens.
- **Scheduling delays.** GitHub may start scheduled runs a few minutes late, or skip some when it's busy.
- **Actions minutes.** Public repos get Actions minutes free. Private repos get 2,000 free minutes a month, and an hourly run uses about 720 of them.
- **Public repos go idle.** GitHub pauses scheduled workflows in public repos after 60 days with no activity. The bot's own state commits count as activity, so this only happens if nobody reads anything for two months.
- **Items that aren't posted:** Goodreads recommendations, follows, and "added a book" entries that have no rating or review.

## License

MIT
