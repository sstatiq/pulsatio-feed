# pulsatio-feed

Static JSON feed of shelf membership for editorial rooms whose composition
isn't reachable through the Apple Music API. A daily GitHub Action reads the
public server-rendered curator page and refreshes `radio.json` when the shelf
membership changes.

The feed carries **catalog IDs only** — consumers hydrate titles, artwork and
content live through the official Apple Music catalog API.

- `radio.json` — the Radio room (Artists Take Over, Latest Episodes,
  Listen to Interviews, …).
- `capture_radio_feed.py` — the capture script the workflow runs.
