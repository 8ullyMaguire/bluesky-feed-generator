# bluesky-feed-generator

A self-contained ATProto (Bluesky) custom feed generator. It builds several
ranked feeds out of one curate list, learns what each viewer likes from their
public activity, and serves it all from a single stdlib HTTP service with a
SQLite file behind it.

No framework, no build step, no external service: `feedgen.py` is the whole
server. `ranking_core.py` holds the pure scoring math and is what the test
suite exercises hardest.

```
curate list ──► posts table ──► ranking (per feed) ──► per-user layer ──► getFeedSkeleton
   (scan)         (SQLite)       velocity/trending/        likes, hidden,
                                  for-you/topic            seen memory
```

## What it gives you

- **Many feeds from one pool.** Each feed is a config entry: its own age
  window, engagement gates, ranking mode, dedup window and size limits.
- **A reader-facing page at `/`.** Every feed of a deployment is described there:
  what it is for, when to use it, an open-in-Bluesky link, a deployment FAQ and a
  "star on GitHub" link. Copy lives in `config.json` (`page` at the top level,
  `page` per feed) and is deliberately generic — no threshold, ranking mode,
  window, TTL or board size is ever published, so the page cannot advertise a
  rule the ranker does not apply. The operator view (board sizes, gate counts,
  cache state) moved to `/status` and `/health`.
- **Five ranking modes** — see [Ranking modes](#ranking-modes). A time-decayed
  velocity ranking (Hacker-News style), a two-track *trending* builder that
  mixes fresh posts with posts being rediscovered right now, a topic tilt, and
  a per-user *for you* mode.
- **Per-user personalization** — genuine per-viewer feeds, not a global board.
  The signed-in viewer's own likes and "show less like this" feedback shape
  their feed, and posts they already saw are not shown twice. See
  [Per-user layer](#per-user-layer).
- **Bounded dedup.** A post is suppressed for a configurable window
  (`seen_ttl_hours`), never permanently — a permanent seen set drains a finite
  candidate pool to nothing.
- **Cheap.** Sub-10 ms serves on a few-hundred-post board, one SQLite file,
  hourly engagement snapshots for the trending track.

## Requirements

- Python **3.9+** (the code uses `dict | dict` and f-strings).
- `base58` and `cryptography` (JWT signature verification):

  ```sh
  pip install -r requirements.txt
  ```

- Outbound HTTPS to your PDS, the public AppView, and (for JWT verification)
  DID documents.

## Quickstart

```sh
git clone <this repo> && cd bluesky-feed-generator
pip install -r requirements.txt

cp config.example.json config.json     # edit: list_uri, publisher_did, owner, feeds
cp .env.example .env                   # edit: BSKY_HANDLE, BSKY_APP_PASSWORD

python3 feedgen.py                     # serves on 127.0.0.1:8080
curl -s localhost:8080/xrpc/app.bsky.feed.describeFeedGenerator | head
```

`config.json` must exist or the service exits with a message telling you so.
`.env` holds the account credentials used to read the list (an app password is
enough; it is never used to post). Keep both files out of version control —
`.gitignore` already does.

Once the feeds rank sensibly on a local port, expose the service and publish
the feed records — see [Deploying](#deploying).

## Config reference

### Top level

| key | meaning |
| --- | --- |
| `hostname` | public host; also becomes `did:web:<hostname>` and the service endpoint in `/.well-known/did.json` |
| `port` | HTTP port (default 8080) |
| `svc_tag` | log prefix in the journal (default `feedgen`) — how you tell two deployments apart |
| `publisher_did` | account that owns the published feed records; feed URIs are `at://<publisher_did>/app.bsky.feed.generator/<rkey>` |
| `refresh_secs` | how often the pool is rescanned (default 600) |
| `source` | `list_uri` (the curate list), `pds_host`, `appview_host`, `member_page_limit`, `author_feed_limit`, `max_workers`, `include_reposts` |
| `scoring` | `w_likes`, `w_reposts`, `w_quotes`, `w_replies`, `w_saves` — base engagement weights |
| `owner` | optional featured account: `did`, `handle`, `always_include`, `boost`, `bonus`, `max_ratio` |
| `blocked_dids` | authors hard-skipped everywhere (never list `owner.did` here) |
| `topic_keywords` | terms defining this deployment's topic tilt (empty = tilt inert) |
| `topic_seeds` | trusted handles/DIDs pinned to affinity 1.0 for the tilt |
| `keyword_weights` | optional adaptive keyword weights (learn from engaged posts, decay over time); off unless `enabled: true`, and a reporting signal only — ranking never reads it |
| `topic_keyword_weight_factor` | hashtag weight vs plain-text weight for topic terms |
| `feeds` | the feed list, see below |
| `tension` | the per-user/curator-taste block, see [Per-user layer](#per-user-layer) |
| `hide_min_board` | anti-starvation floor: per-user hiding never shrinks a board below this many posts (default 20) |
| `hide_seen_ttl_h` | how long a served post stays hidden from that viewer, across every feed of this deployment (default 24) |
| `snapshot_interval_hours` / `snapshot_prune_hours` | engagement-snapshot cadence and retention |
| `page` | copy for the public page at `/`: `lang`, `title`, `tagline`, `how[]`, `faq[]{q,a}`, `community_feeds[]{name,handle,rkey}`, `labels{}` (button and heading strings), `show_descriptions`, `credits`, `repo`, `github` (repo URL; renders a "star on GitHub" link). All optional — anything missing falls back to English engine defaults. Keep it generic: this text is public, so it must not restate the knobs above |

Every deployment of this code is independent: separate config, separate
SQLite file, separate `serverInteractions` state. Running two of them (say, two
topical communities) is just two configs on two ports, and their seen memories
never mix.

### Per feed

| key | meaning |
| --- | --- |
| `rkey` | feed record id; the public feed URI ends with it |
| `display_name`, `description` | published on the feed record |
| `min_age_hours` / `max_age_hours` | age window a post must fall in to be eligible |
| `min_likes`, `min_score`, `min_reposts_share` | engagement gates (a post must pass the enabled ones; `gate_mode` picks `all` vs `any`) |
| `max_per_author` | per-feed author cap, the main diversity knob |
| `max_posts` | board size |
| `adaptive_target` | size the arena is tuned toward before limits are applied |
| `ranking` | `velocity` (default `flat`), `deep`, `foryou`, `topic`, `trending` |
| `gravity` / `decay_factor` | time-decay strength (bigger = fresher) |
| `age_floor_hours` | age below which a post gets no freshness boost (anti-reflex) |
| `taste_weight` | how much the curator's taste lifts an author here |
| `topic_weight`, `topic_keyword_bonus`, `topic_seed_bonus`, `keyword_saturate` | topic-tilt knobs |
| `hide_seen`, `seen_ttl_hours` | opt this feed into per-viewer dedup, and for how long |
| `window_hours`, `half_life_hours`, `vintage_slot_fraction`, `vintage_min_age_hours`, `vintage_max_age_hours` | `trending` only: the fresh track's velocity window and the rediscovery track's age band and slot share |
| `page` | optional `{tagline, use_when}` shown on the public page for this feed; without it the published `description` is used as the tagline |

## Ranking modes

- **`flat`** — engagement only. The default when no mode is set.
- **`velocity`** — engagement decayed by age with a half-life floor
  (`gravity`). What the Top-N windows use: 6 h, 24 h, 3 d, week, month.
- **`deep`** — same math as `velocity`; a name long-window boards can use so
  the intent is visible in the config.
- **`trending`** — two tracks, interleaved: fresh posts ranked by velocity
  through a short `window_hours`, plus every `vintage_slot_fraction`-th slot
  given to an older post ranked by its *recent* engagement rate (from the
  hourly `engagement_snapshots` table) rather than its lifetime total. This is
  what surfaces a post that is being rediscovered today over one that was big
  last month.
- **`foryou`** — per-viewer: engagement × the viewer's own author affinity ×
  the curator/topic terms, with exploration slots woven in. Feeds listed in
  `tension.personal_feeds` get serve-time re-ranking.
- **`topic`** — this deployment's editorial tilt: posts weighted by how much
  their author writes about `topic_keywords`, with an optional bonus for
  `topic_seeds` accounts. The tilt is inferred from the stored corpus
  (`author_topic`) and from per-post keyword hits, so it stays meaningful as
  the community's vocabulary drifts.

## Per-user layer

A request authenticated with a service JWT (`Authorization: Bearer …`) is
attributed to the viewer's DID, verified against the signing key's DID
document (ES256K, fail-closed: anything unverifiable is served
unauthenticated). Anonymous requests get the shared board.

For an identified viewer:

- **Likes** — their public likes are read (bounded pages) and used both to
  seed affinity on first sight and to hide what they already liked.
- **`sendInteractions`** — the "show less like this" feedback loop:
  `feedContext` / `reason` values are folded into a negative-term model, so
  similar posts sink for that viewer and the author's affinity takes the
  `less` weight (-5.0, clipped into the `[-1, 2]` affinity range). This is the
  endpoint that makes the feed *learn*.
- **Seen memory** — a post served to them in *any* feed of this deployment is
  not served again in *any* other feed for `hide_seen_ttl_h` (24 h default).
  `hide_min_board` (20) is the floor: when hidden posts would drain a board
  below it, already-seen posts come back at the tail, so a feed is never
  empty. Ordering is therefore "unseen first, repeats after".
- **Owner protection** — the configured `owner` is never excluded, so a viewer
  who scrolls a lot cannot wipe the curator out of their own bandwidth.
- **Curator gravity** — a viewer's affinity drifts slowly toward the curator's
  taste (`owner_gravity_daily`), while fresh interactions re-assert their own
  shape; `extended_gravity_daily` does the same for the slower
  `taste_sources` vector.
- **Exploration** — `explore_share` of a personal feed's page is filled from
  curator-taste authors the viewer has no signal for yet, so the feed does not
  collapse into a filter bubble.

Background work, all bounded by config budgets: polling the AppView for likes
and reposts of posts this service served (to attribute interactions to the
viewers who saw them), refreshing each active viewer's likes on a schedule,
and hourly engagement snapshots for the trending track.

## Running several deployments from one checkout

The engine is deployment-agnostic, so the same files can serve several
communities at once — one config, one database, one port each. Two ways to wire
it, both used in practice:

**Point the service at the shared code** (nothing is copied):

```ini
[Service]
WorkingDirectory=/srv/community-a
ExecStart=/usr/bin/python3 /opt/bluesky-feed-generator/feedgen.py
Environment=FEEDGEN_CONFIG=/srv/community-a/config.json
Environment=FEEDGEN_DB=/var/lib/community-a/feeds.sqlite
```

`FEEDGEN_ENV` has no override: the `.env` with the account credentials is read
from the directory the *code* lives in, so with this layout the credentials go
in `/opt/bluesky-feed-generator/.env` — or, cleaner, keep them out of the code
directory by giving each deployment its own checkout/symlink.

**Symlink the engine into each deployment directory** (what the repository this
was extracted from does): `ln -s /opt/bluesky-feed-generator/feedgen.py
/srv/community-a/feedgen.py`. `feedgen.py` resolves its config, `.env` and
legacy state relative to the path it was *invoked* through (a symlink is not
resolved), so each deployment keeps its own `config.json`, `.env` and state
next to the link while the code itself exists exactly once. Give every
deployment its own `FEEDGEN_DB` — two services must never share one SQLite
file — and its own `svc_tag` so the journal stays readable.

Either way, the databases stay independent: seen memory, interactions and
affinity are per-deployment by construction.

## Deploying

1. **Serve it.** Run `python3 feedgen.py` under a service manager. It binds
   `127.0.0.1`, so put a TLS reverse proxy in front of it, and make sure
   `https://<hostname>/xrpc/app.bsky.feed.getFeedSkeleton` and
   `/.well-known/did.json` are reachable from the internet.

   ```ini
   # /etc/systemd/system/feedgen.service
   [Unit]
   Description=Bluesky feed generator
   After=network-online.target

   [Service]
   WorkingDirectory=/srv/bluesky-feed-generator
   ExecStart=/usr/bin/python3 feedgen.py
   Restart=always
   Environment=FEEDGEN_DB=/var/lib/feedgen/feedgen.sqlite

   [Install]
   WantedBy=multi-user.target
   ```

2. **Publish the records.** `publish_feeds.py` creates the
   `app.bsky.feed.generator` record for every feed in `config.json` that is
   missing on-chain (idempotent; `--dry-run` to preview, `--force` to
   overwrite). Feed metadata changes need a re-publish.

   ```sh
   python3 publish_feeds.py --dry-run
   python3 publish_feeds.py
   ```

3. **Check it.** `GET /` is the public page: what each feed of this deployment is
   for, with an open-in-Bluesky link each. `GET /status` (and `GET /health`)
   renders the operator page: board size, cache generation, gate counts, last
   refresh.

Environment variables: `FEEDGEN_CONFIG` (config path, default
`./config.json`), `FEEDGEN_DB` (SQLite path, default
`$XDG_DATA_HOME/feedgen/feedgen.sqlite`), `FEEDGEN_STATE_PATH` (legacy JSON
state to migrate, optional).

## Tests

No test framework needed — each file is a runnable script that prints one line
per test and exits non-zero on failure.

```sh
python3 test_ranking_core.py         # pure ranking math
python3 test_ranking.py feedgen.py   # ranking, gates, dedup, per-user layer, JWT,
                                     # keyword weights
python3 test_feedgen_auth.py         # authenticated getFeedSkeleton path
python3 test_page.py                 # public page: no config value on it, escaping,
                                     # routes, feed API regression
```

56 checks in total. They run against a throwaway SQLite file and (unless a local `config.json`
exists) the example config, so a fresh clone is green. One auth test performs a
single outbound request for a non-existent DID to exercise the cold-start path;
if it fails, it degrades gracefully and the test still passes.

## Diagnostics

```sh
# Prove the seen memory really is shared across this deployment's feeds:
python3 diagnostics/seen_scope_check.py [requester_did]

# Show exactly what the per-user exclusion layer hides for one viewer,
# feed by feed:
python3 diagnostics/simulate_user_exclusions.py [requester_did] [rkey]
```

Both are read-only and are the fastest way to answer "why is my feed short?"
or "did the feedback I sent actually land?".

## Design notes and limitations

- **Bookmark counts are structurally unavailable.** Bluesky bookmarks are
  private; no AppView response exposes a public save count, so `w_saves`
  always multiplies zero. It stays in the config so the term starts working if
  that ever changes.
- **Interaction attribution is best-effort.** Likes/reposts are attributed to
  viewers by polling the AppView with a budget; a viewer who never appears in
  that sample keeps a neutral affinity, which is the safe default.
- **Dedup is a window, not a promise.** "Already seen" is bounded by
  `hide_seen_ttl_h` and floored by `hide_min_board`. Both are there on purpose:
  a permanent, unfloored seen set drained these feeds to a handful of posts.
- **JWT verification accepts ES256K over DID documents only** (multibase or
  JWK), with a 10-minute document cache. `alg` is not trusted from the token.
- **The pool is one curate list.** Discovery is a scan of list members'
  recent posts, so feed quality is mostly list quality. Cold start on a large
  list takes minutes; the HTTP port opens immediately and serves empty until
  the first refresh lands.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. Note the network clause: if you run a
modified version as a network service, you must offer the corresponding source to its users.
