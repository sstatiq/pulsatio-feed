#!/usr/bin/env python3
"""
Capture shelf membership for the editorial rooms Pulsatio can't reach through
the Apple Music API, and emit it as a static JSON feed (feed.json).

Two families of shelves are captured, both from Apple's own server-rendered
public curator pages (the embedded `serialized-server-data` JSON):

  · the Radio room's rotating shelves (Artists Take Over, Latest Episodes, …);
  · each genre curator's "New Releases" shelf — an editorial ROOM of albums.
    Probed 2026-09-07: `/v1/catalog/{sf}/rooms/{id}` returns 400 "Unknown
    catalog resource type", and `/v1/editorial/{sf}/rooms/{id}` returns 400
    40012 "'RoomsResource' entities require permissions that are not in the
    request" — so a room's membership genuinely cannot be fetched with an app
    developer token, unlike a playlist's. Genres that DO have a "New in X"
    playlist (Classical, Rock, Alternative, Christian, Anime, Latin, Pop
    Latino) are served live in-app and are deliberately absent here.

This feed carries membership only — catalog IDs, no content. Consumers hydrate
titles, artwork and playback through the official Apple Music catalog API.

Run by the scheduled GitHub Action (see .github/workflows/update-feed.yml).
When membership is unchanged an output file is left byte-identical (old
`capturedAt` included) so the workflow makes no commit.

Usage:  python3 capture_feed.py [feed.json] [radio.json]

`radio.json` is written as well, carrying the radio room alone in the original
shape: app builds shipped before the combined feed still read that URL.

Exits non-zero if a REQUIRED radio shelf is missing/small, or if NO genre
shelf could be captured at all — so a broken parse never replaces last-good.
"""
import json, re, html, sys, time, urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

RADIO_URL = "https://music.apple.com/us/curator/apple-music-radio/1531543191"

# SSR section title -> (feed shelf key, SSR content kind, required).
# Required shelves have stable names; a miss means the parse broke and the
# run should fail. Optional shelves (seasonal names, e.g. "Summertime
# Sounds Playlists") are included when present and silently skipped when
# Apple renames them — consumers fall back to their baked snapshot.
RADIO_SHELVES = {
    "Artists Take Over":           ("artists-take-over",    "radioStation", True),
    "Latest Episodes":             ("latest-episodes",      "radioStation", True),
    "Listen to Interviews":        ("listen-to-interviews", "radioStation", True),
    "Apple Music Club DJ Mixes":   ("club-dj-mixes",        "album",        False),
    "Summertime Sounds Playlists": ("summertime-sounds",    "playlist",     False),
}

# Pulsatio room id -> genre curator id. ONLY genres whose New Releases exist
# solely as a room; anything with a "New in X" playlist is served live in-app
# by `RoomShelfSpec.newReleases` and must not be listed here.
GENRE_CURATORS = {
    "blues":           "976439528",
    "country":         "976439534",
    "jazz":            "976439542",
    "reggae":          "976439552",
    "bollywood":       "982307152",
    "pop-italiano":    "982348865",
    "musica-tropical": "976439545",
    "musica-mexicana": "976439544",
    "urbano-latino":   "976439553",
    "worldwide":       "976439587",
    "live-music":      "1526866649",
}
# Apple titles this shelf per storefront language; all of these curators are
# on the US storefront, but the Spanish/Italian genre pages title it locally.
NEW_RELEASES_TITLES = {"new releases", "nuevos lanzamientos", "nuove uscite", "lo nuevo"}

KIND_JSON = {"radioStation": "station", "album": "album", "playlist": "playlist"}
MIN_ITEMS = 5   # fewer than this in a required shelf = broken capture


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def serialized(doc, what):
    m = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
                  doc, re.S)
    if not m:
        sys.exit(f"ERROR: no serialized-server-data on the {what} page (layout changed?)")
    return json.loads(html.unescape(m.group(1)))


def find_sections(data):
    out = []
    def rec(o):
        if isinstance(o, dict):
            if isinstance(o.get("sections"), list):
                out.append(o["sections"])
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for x in o:
                rec(x)
    rec(data)
    return out[0] if out else []


def title_of(sec):
    found = []
    def rec(o):
        if found:
            return
        if isinstance(o, dict):
            if "titleLink" in o and isinstance(o["titleLink"], dict):
                t = o["titleLink"].get("title")
                if t:
                    found.append(t); return
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for x in o:
                rec(x)
    rec(sec.get("header"))
    return found[0] if found else ""


def ids_of(sec, want_kind):
    out = []
    for it in sec.get("items") or []:
        cd = it.get("contentDescriptor") or {}
        if cd.get("kind") != want_kind:
            continue
        ids = cd.get("identifiers") or {}
        aid = ids.get("storeAdamID") or ids.get("id")
        if aid:
            out.append(aid)
    return out


def capture_radio():
    secs = find_sections(serialized(fetch(RADIO_URL), "radio curator"))
    if not secs:
        sys.exit("ERROR: no sections found on the radio curator page.")
    shelves, seen = {}, set()
    for sec in secs:
        t = title_of(sec)
        if t in RADIO_SHELVES and t not in seen:
            seen.add(t)
            key, kind, required = RADIO_SHELVES[t]
            ids = ids_of(sec, kind)
            if len(ids) < MIN_ITEMS:
                if required:
                    sys.exit(f"ERROR: required shelf '{t}' has only {len(ids)} items.")
                continue
            shelves[key] = {"kind": KIND_JSON[kind], "ids": ids}
    missing = [t for t, (_, _, req) in RADIO_SHELVES.items() if req and t not in seen]
    if missing:
        sys.exit(f"ERROR: required radio shelves not found (renamed/removed?): {missing}")
    return shelves


def capture_genre_new_releases(room, curator_id):
    """The genre's "New Releases" shelf, or None when Apple isn't showing one."""
    doc = fetch(f"https://music.apple.com/us/curator/x/{curator_id}")
    for sec in find_sections(serialized(doc, f"{room} curator")):
        if title_of(sec).strip().lower() not in NEW_RELEASES_TITLES:
            continue
        ids = ids_of(sec, "album")
        if len(ids) >= MIN_ITEMS:
            return {"kind": "album", "ids": ids}
    return None


def write_if_changed(path, feed):
    """Keep the old file byte-identical when membership is unchanged, so the
    workflow's `git diff --quiet` skips the commit."""
    try:
        with open(path) as f:
            old = json.load(f)
        if old.get("rooms") == feed["rooms"] and old.get("version") == feed["version"]:
            print(f"{path}: membership unchanged, keeping existing file.")
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    with open(path, "w") as f:
        json.dump(feed, f, indent=1)
        f.write("\n")
    total = sum(len(s["ids"]) for r in feed["rooms"].values() for s in r["shelves"].values())
    print(f"{path}: wrote {len(feed['rooms'])} rooms, {total} ids.")


def main():
    combined_path = sys.argv[1] if len(sys.argv) > 1 else "feed.json"
    radio_path = sys.argv[2] if len(sys.argv) > 2 else "radio.json"

    rooms = {"radio": {"shelves": capture_radio()}}

    captured, skipped = 0, []
    for room, curator_id in GENRE_CURATORS.items():
        try:
            shelf = capture_genre_new_releases(room, curator_id)
        except Exception as e:                      # one dead page must not sink the run
            shelf = None
            print(f"WARN {room}: fetch/parse failed ({e})")
        if shelf:
            rooms[room] = {"shelves": {"new-releases": shelf}}
            captured += 1
            print(f"  {room:16s} new-releases albums={len(shelf['ids'])}")
        else:
            skipped.append(room)
        time.sleep(1.0)

    if skipped:
        print(f"NOTE: no New Releases shelf for {skipped} (consumers keep their baked seed).")
    # Every genre failing at once means the parse broke, not that Apple pulled
    # eleven shelves on the same day.
    if captured == 0:
        sys.exit("ERROR: no genre New Releases shelf captured at all — parse broken?")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_if_changed(combined_path, {"version": 1, "capturedAt": now, "rooms": rooms})
    # Legacy single-room file: app builds shipped before feed.json read this.
    write_if_changed(radio_path, {"version": 1, "capturedAt": now,
                                  "rooms": {"radio": rooms["radio"]}})


if __name__ == "__main__":
    main()
