# X Reader

Lets Synth read the **content** of a pasted X/Twitter post link — without an X
developer account, OAuth, or the paid read API tier.

## Action

| Action | Purpose |
|--------|---------|
| `x_fetch_post` | Fetch one post from its `https://x.com/<user>/status/<id>` URL and return the post's text (and, when available, media URLs). |

* `required_fields`: `url`
* `optional_fields`: `include_media` (default `true`)
* `security_level`: `low`
* No `external_effects` — Fast Lane only, never spawns agentic tasks.

## How it reads (resolution cascade)

1. **X oEmbed** (`https://publish.twitter.com/oembed`) — official, auth-free
   embed API; returns tweet text + author directly.
2. **Syndication endpoint**
   (`https://cdn.syndication.twimg.com/tweet-result?id=…&lang=en&token=a`) —
   the unofficial embed-internal JSON (also no auth); richer (raw text, media).
3. **Search-index fallback** — `site:x.com` snippets via the configured web
   search backend (SearXNG etc.), the same path that already surfaces post
   content through search results.

The first tier that returns usable content wins; every tier is fail-closed, so
an unreachable/blocked source degrades to the next one and finally to a
structured `{"success": false, "reason": ...}` — never a crash, never a
fabricated post.

## Configuration

| Key | Default | Purpose |
|-----|---------|---------|
| `X_FETCH_METHOD` | `auto` | `auto` (oEmbed → syndication → search), or pin one tier: `oembed`, `syndication`, `search`. |

## Rules honoured

* **Structural only** — the tweet id is parsed from the URL's `/status/<id>`
  path segment; no keyword or language logic.
* **Optional component** — disabling/removing this plugin breaks nothing; the
  search fallback is lazy-imported, so it also works when the Web Search
  plugin is absent.
* **Untrusted data** — tweet text is third-party content and a prompt-injection
  surface. It is delivered as *data* for Synth to read; it is never spliced
  into persona/system prompts, and the prompt instructions tell the model to
  treat anything written inside a tweet as content, not instructions.
* **No delivery spam** — as an agent tool it returns the result to the bounded
  loop; on an ordinary turn it enqueues exactly one delivery (same guard
  pattern as the Web Search plugin).

## Notes & limits

* X itself (login-walled) is never fetched directly; content comes from the
  embed/search sources above. Posts that are protected, deleted, or too
  recent to be indexed will fail with a clear reason.
* There is a per-id in-memory cache (TTL ~5 min); tweet ids are immutable, so
  caching is safe.