# pintxos

Bite-sized, honest RSS feeds. Pintxos re-publishes any RSS feed with clickbait
titles rewritten into plain facts, plus a short factual summary — so you can
tell what happened without opening the article.

## What / why

RSS titles are increasingly written to be clicked, not read. A real example
from this tool:

- Screenrant's teaser title: *"Netflix's Renewed Sci-Fi Thriller With Perfect
  Rotten Tomatoes Score Officially Hits A Filming Milestone"*
- Pintxos' rewritten headline: *"Netflix's Supacell completes season 2
  filming"*

Same story, no guessing games. Point pintxos at a feed once, and every new
item gets the same treatment automatically.

## How it works

1. Poll each subscribed feed on a schedule.
2. For each new item, fetch the article's own URL and extract the body text
   with [trafilatura](https://github.com/adbar/trafilatura).
3. If the page can't be fetched or extraction comes back too thin, fall back
   to the feed entry's own content (or, as a last resort, its title) — a
   `fallback` flag is kept on the item so you know which path was used.
4. Send the text to Claude to produce a factual headline and a short summary.
   This happens **once per item**, ever — the result is stored, and items are
   never re-summarized.
5. Serve the result back out as a clean RSS 2.0 feed, one output feed per
   subscribed input feed.

## Quickstart

```yaml
services:
  pintxos:
    image: ghcr.io/janw76/pintxos:latest
    container_name: pintxos
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      # PINTXOS_MODEL: claude-haiku-4-5-20251001
      # PINTXOS_POLL_MINUTES: 30
      # PINTXOS_ITEMS_PER_FEED: 50
      # PINTXOS_BASE_URL: https://pintxos.example.com
    restart: unless-stopped
```

Put your key in a `.env` file next to `docker-compose.yml` (see
`.env.example`), then:

```bash
docker compose up -d
```

Open `http://localhost:8000`, add a feed URL, and copy the generated output
feed URL into your RSS reader.

## Configuration

Model, poll interval, items per feed and the API key can be set via environment
variable, or (if unset) via the Settings page in the web UI, which persists them
to the database. `PINTXOS_BASE_URL` and `PINTXOS_DATA_DIR` are environment-only.

| Env var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(none)* | Anthropic API key. See note below. |
| `PINTXOS_MODEL` | `claude-haiku-4-5-20251001` | Claude model used to summarize. |
| `PINTXOS_POLL_MINUTES` | `30` | How often feeds are polled, in minutes. |
| `PINTXOS_ITEMS_PER_FEED` | `50` | Items kept per output feed (older ones pruned). |
| `PINTXOS_BASE_URL` | *(none, inferred from the request)* | Base URL used to build output feed links, e.g. `https://pintxos.example.com`. |
| `PINTXOS_DATA_DIR` | `./data` | Directory for the SQLite database. |

## Security warning

**Pintxos has no authentication.** Anyone who can reach the web UI can add,
delete, or repoll feeds, and anyone who can reach an output feed URL can read
it — this is by design, so RSS readers can fetch feeds without credentials.
Only run pintxos on `localhost`, over Tailscale/a VPN, or behind a reverse
proxy that handles authentication for you. Do not expose it directly to the
public internet.

## API key

The primary way to configure `ANTHROPIC_API_KEY` is the environment variable
(shown in the Quickstart above). If — and only if — the environment variable
is not set, the Settings page lets you store a key in the database instead.

## Cost

Pintxos uses Claude Haiku and makes exactly one API call per new article,
never more: items are summarized once and stored, and are never
re-summarized on subsequent polls.

## Usage

1. Open the web UI and add a feed URL.
2. Wait for the next poll (or click "poll now").
3. Copy the feed's output URL — pattern `/feeds/<id>.xml` — into your RSS
   reader.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[dev]
.venv/bin/pytest
docker build -t pintxos .
```

## Prior art

See [docs/prior-art.md](docs/prior-art.md) for a full survey. The closest
existing match is [RSSbrew](https://github.com/yinan-c/RSSbrew) (Docker,
web UI, persisted store, real republished feed), but it never fetches the
original article page and prepends a summary rather than rewriting the
headline — which is why pintxos was built from scratch rather than forked.

## License

MIT — see [LICENSE](LICENSE).
