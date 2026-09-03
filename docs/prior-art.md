# Prior Art: LLM-Summarized RSS Republishers

Pintxøs' goal — read any RSS feed, fetch each article's original URL, extract
the body, send it to an LLM to produce a factual headline plus a short
summary, persist it once, and republish as a clean new RSS feed, all behind a
minimal self-hosted web UI with configurable model/API key — turns out to be
a fairly specific combination. Many self-hosted tools do parts of it (full-text
extraction, or AI summaries bolted onto a reader), but the search below found
no project that does the whole pipeline with a factual-headline rewrite and a
persisted, once-only summarize step behind a simple feed-management UI.
Facts below were pulled live from GitHub/PyPI APIs and repo READMEs/source on
2026-09-02.

## Candidates

| Name | URL | Stars | Last commit | Language | License |
|---|---|---|---|---|---|
| RSSbrew | github.com/yinan-c/RSSbrew | 293 | 2026-08-29 | Python (Django) | AGPL-3.0 |
| RSS-GPT | github.com/yinan-c/RSS-GPT | 355 | 2025-09-08 (superseded by RSSbrew) | Python | MIT |
| Qetesh/miniflux-ai | github.com/Qetesh/miniflux-ai | 253 | 2026-06-22 | Python | none declared |
| deimosfr/xExtension-AiSummary | github.com/deimosfr/xExtension-AiSummary | 23 | 2026-08-18 | PHP (FreshRSS ext.) | GPL-3.0 |
| cvlc/freshrss-ai-assistant | github.com/cvlc/freshrss-ai-assistant | 7 | 2025-03-13 | PHP (FreshRSS ext.) | BSD-2-Clause (low activity) |
| condenseit | github.com/wildlifechorus/condenseit | 80 | 2026-06-04 | Python | MIT |

## Checklist matrix

Legend: Y = yes, N = no, P = partial.

| Feature | RSSbrew | RSS-GPT | miniflux-ai | AiSummary (FreshRSS) | freshrss-ai-assistant | condenseit |
|---|---|---|---|---|---|---|
| Fetches & extracts original article body | N (uses feed's own content field via BeautifulSoup, not the linked page) | N | N | N (reader already has content) | N | Y (per-source scrapers) |
| LLM summary | Y | Y | Y | Y | Y | Y |
| Factual headline **rewrite** (replaces title, not just summary) | N (title kept, summary prepended) | N | N | N | Y ("retitling") | N |
| Republishes as a new RSS feed URL | Y | Y (static file + GH Pages) | N (modifies entries inside Miniflux's own DB/UI) | N (in-reader button, no new feed) | N | N (digest is a web page, not RSS) |
| Web UI for feed add/list/delete | Y (Django admin) | N (edit config.ini + GitHub Actions) | N (config file / Miniflux itself) | N (FreshRSS is the host app) | N | Y (own web UI, but for digest sources) |
| Summarize-once, persisted store | Y (SQLite) | Y (state file in repo) | Y (Miniflux DB) | N (re-run per click) | Y (FreshRSS DB) | Y (SQLite) |
| Docker image | Y | N (GitHub Actions, no server) | Y | N (plugin, needs FreshRSS) | N (plugin, needs FreshRSS) | Y |
| Configurable model + API key | Y (OpenAI-compatible) | Y (OpenAI only) | Y (OpenAI-compatible) | Y (several providers) | Y | Y (Ollama/OpenRouter/OpenAI-compatible) |
| Anthropic/Claude support | Via generic OpenAI-compatible endpoint only | N | Via OpenAI-compatible endpoint only | Y (explicit provider option) | Y (explicit Claude support) | Via OpenAI-compatible endpoint only |

## Per-candidate verdicts

**RSSbrew** — the closest match by far: Docker, Django web UI, persisted
SQLite store, and it does republish a genuinely new RSS feed URL per
configured pipeline. But it never fetches the original article page (it
works off whatever `<content>`/`<description>` the source feed already
provides via BeautifulSoup) and it prepends an AI summary rather than
rewriting the headline. No native Anthropic client, no full-text extraction.

**RSS-GPT** — the same author's earlier, now-deprecated project (README
explicitly redirects users to RSSbrew). Config-file + GitHub Actions
workflow, output feeds hosted via GitHub Pages; no server, no Docker, no web
UI, no article fetch beyond the feed's own content, OpenAI-only. Included as
useful prompt/summary-formatting reference, not as a live option.

**Qetesh/miniflux-ai** — solid recent project (253 stars) but it's a
companion service that writes AI summaries back into a Miniflux instance's
own database/UI; it doesn't fetch the original article body either and
produces no independent republished RSS feed — you still need Miniflux
running as the actual reader.

**FreshRSS extensions (xExtension-AiSummary, freshrss-ai-assistant)** —
reader-side plugins, not standalone tools; they require FreshRSS as host,
operate as one-click/on-demand summarizers inside the existing UI rather
than an automated once-only pipeline, and produce no separate output feed.
`freshrss-ai-assistant` is notable for explicit Claude support and headline
retitling, both close to what Pintxøs does, but it's low-activity (single commit cluster,
last pushed 2025-03) and entirely dependent on a FreshRSS install.

**condenseit** — a genuinely automated self-hosted pipeline with real
per-source scraping and a persisted SQLite store, but its output is a
browser-viewed daily digest page, not a republished RSS feed, and it has no
factual-headline-rewrite concept — it's a "digest" tool, not a "clean feed"
tool.

## Partial matches

- **Full-text extraction only**: `pictuga/morss` (785 stars, Python, AGPL-3.0,
  last commit 2024-04-27) and FiveFilters' Full-Text RSS (commercial,
  closed-source PHP product, self-hosted via Docker wrappers like
  `fkie/fivefilters-full-text-rss-docker`) turn partial feeds into full-text
  feeds but do zero summarization or rewriting. `RSS-Bridge/rss-bridge`
  (9.2k stars, PHP, Unlicense, active) generates RSS feeds for sites that
  lack one — a different problem (feed generation, not summarization) but a
  useful reference for its bridge-plugin architecture and its own web UI.
- **Summarize-only, in-reader, no republish**: the FreshRSS/Miniflux plugins
  above; TT-RSS has similar community plugins with the same shape (checked
  via TT-RSS plugin lists — no maintained AI-summary plugin with republish
  found at time of writing).
- **Commercial**: Kagi's "Kite"/small-web and news-digest features, and
  various closed SaaS "AI RSS summarizer" products, do parts of this but are
  not self-hostable and out of scope for a build-vs-adopt decision here.

## Library picks

- **Article extraction: trafilatura** over `readability-lxml` and
  `newspaper4k`. All three are still maintained on PyPI (trafilatura 2.2.0,
  readability-lxml 0.9, newspaper4k 0.9.6, all released within the last two
  months as of this writing), but trafilatura has the best precision/recall
  in published extraction benchmarks, handles more edge cases (metadata,
  language detection, feed-adjacent boilerplate removal) out of the box, and
  is the extractor RSSbrew-adjacent and morss-adjacent projects reference
  most often in this space.
- **Feed parsing: feedparser** — the de facto standard, actively maintained
  (6.0.14, released 2026-07-30), handles the long tail of malformed
  real-world RSS/Atom feeds better than anything else available.
- **Feed generation: hand-rolled RSS via `xml.etree.ElementTree`** over
  `feedgen`. `feedgen` works but has been essentially quiet since its 1.0.0
  release in December 2023; RSS 2.0 output is a small, stable, well-known
  format, and a ~50-line hand-rolled generator avoids taking on a
  low-activity dependency for something this simple.
