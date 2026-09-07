# pulsatio-feed

Static JSON feed of shelf membership for editorial rooms whose composition
isn't reachable through the Apple Music API. A daily GitHub Action reads
Apple's public server-rendered curator pages and refreshes the feed when the
membership changes.

The feed carries **catalog IDs only** — consumers hydrate titles, artwork and
content live through the official Apple Music catalog API.

- `feed.json` — every room in one file (`rooms.<room>.shelves.<key>`):
  - `radio` — the Radio room (Artists Take Over, Latest Episodes, Listen to
    Interviews, …).
  - the per-genre `new-releases` shelves (blues, country, jazz, reggae,
    bollywood, pop-italiano, musica-tropical, musica-mexicana, urbano-latino,
    worldwide, live-music).
- `radio.json` — the radio room alone, in the original shape. Kept fresh for
  app builds shipped before `feed.json` existed; new consumers read
  `feed.json`.
- `capture_feed.py` — the capture script the workflow runs.

## Why genres are in here at all

A genre's "New Releases" is an editorial **room** of albums, and a room's
membership can't be fetched with an app developer token (probed 2026-09-07):
`/v1/catalog/{sf}/rooms/{id}` → 400 *"Unknown catalog resource type 'rooms'"*,
and `/v1/editorial/{sf}/rooms/{id}` → 400 40012 *"'RoomsResource' entities
require permissions that are not in the request"*.

Genres that publish a **"New in X" playlist** (Classical, Rock, Alternative,
Christian, Anime, Latin, Pop Latino) need none of this — a playlist *is*
reachable, so the app builds those shelves live and they are deliberately
absent from the feed.
