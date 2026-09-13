#!/usr/bin/env python3
"""
Capture shelf membership for the editorial rooms Pulsatio can't reach through
the Apple Music API, and emit it as a static JSON feed (feed.json).

Four families of shelves are captured, all from Apple's own server-rendered
public pages (the embedded `serialized-server-data` JSON):

  · the Radio room's rotating shelves (Artists Take Over, Latest Episodes, …);
  · each genre curator's "New Releases" shelf — an editorial ROOM of albums.
    Probed 2026-09-07: `/v1/catalog/{sf}/rooms/{id}` returns 400 "Unknown
    catalog resource type", and `/v1/editorial/{sf}/rooms/{id}` returns 400
    40012 "'RoomsResource' entities require permissions that are not in the
    request" — so a room's membership genuinely cannot be fetched with an app
    developer token, unlike a playlist's. Genres that DO have a "New in X"
    playlist (Classical, Rock, Alternative, Christian, Anime, Latin, Pop
    Latino) are served live in-app and are deliberately absent here.
  · the "Music Videos" grouping page (music.apple.com/…/grouping/34) — every
    shelf, all OPTIONAL: a miss just means consumers keep their baked seed.
  · Radio's "Watch Interviews" shelf (music.apple.com/…/room/6749860083) —
    also optional, captured separately from the required radio shelves so a
    miss here can never block those.

Video shelves carry `kind: "video"` with each id prefixed `mv.` (a catalog
music video) or `uv.` (an Apple "uploaded video" — interviews/clips with no
MusicKit type) per its own `contentDescriptor.kind`; unlike stations/albums,
Apple's SSR id for these isn't already prefixed, so the prefix is added here.

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
MUSIC_VIDEOS_URL = "https://music.apple.com/us/grouping/34"
WATCH_INTERVIEWS_URL = "https://music.apple.com/us/room/6749860083"

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

# music.apple.com/…/grouping/34 shelf title -> feed shelf key. "video" shelves
# mix musicVideo + artistUploadedVideo items (see `video_ids_of`); "playlist"
# shelves use the same SSR content kind as the radio playlist shelf above.
# "Hero" and "Apple Music TV: Watch Now" (a livestream station MusicKit can't
# play) are deliberately not captured. Every entry here is OPTIONAL.
MUSIC_VIDEOS_SHELVES = {
    "New Music Videos":             ("new-music-videos",             "video"),
    "Music Video Playlists":        ("music-video-playlists",        "playlist"),
    "Artist Essentials":            ("artist-essentials",            "playlist"),
    "Our Exclusive Concert Series": ("our-exclusive-concert-series", "playlist"),
    "Latest Interviews":            ("latest-interviews",            "video"),
    "Pop Music Videos":             ("pop-music-videos",             "video"),
    "Hip-Hop Videos":               ("hip-hop-videos",               "video"),
    "R&B Videos":                   ("r-and-b-videos",               "video"),
    "Latin Videos":                 ("latin-videos",                 "video"),
    "Country Videos":               ("country-videos",               "video"),
    "Alternative Videos":           ("alternative-videos",           "video"),
    "Dance Videos":                 ("dance-videos",                 "video"),
    "Metal Videos":                 ("metal-videos",                 "video"),
    "Hard Rock Videos":             ("hard-rock-videos",             "video"),
    "U2 Live Videos":               ("u2-live-videos",               "video"),
    "Kids Music Videos":            ("kids-music-videos",            "video"),
    "Lyric Videos":                 ("lyric-videos",                 "video"),
    "Live Music Videos":            ("live-music-videos",            "video"),
}

KIND_JSON = {"radioStation": "station", "album": "album", "playlist": "playlist"}
MIN_ITEMS = 5   # fewer than this in a required shelf = broken capture

# contentDescriptor.kind -> the prefix VideoPlaybackItem.parse expects.
VIDEO_KIND_PREFIX = {"musicVideo": "mv.", "artistUploadedVideo": "uv."}


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


def video_ids_of(sec):
    """Like `ids_of`, but keeps musicVideo + artistUploadedVideo items
    (interleaved, in shelf order) and prefixes each id `mv.`/`uv.` per its own
    kind — the shape `VideoPlaybackItem.parse` expects."""
    out = []
    for it in sec.get("items") or []:
        cd = it.get("contentDescriptor") or {}
        prefix = VIDEO_KIND_PREFIX.get(cd.get("kind"))
        if not prefix:
            continue
        ids = cd.get("identifiers") or {}
        aid = ids.get("storeAdamID") or ids.get("id")
        if aid:
            out.append(f"{prefix}{aid}")
    return out


DURATION_RE = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?\s*(?:(\d+)\s*sec)?")


def uploaded_videos_of(sec):
    """Title / still / duration for each artistUploadedVideo item on the
    shelf, keyed by id. `uploaded-videos` is NOT a public catalog resource
    (`/v1/catalog/{sf}/uploaded-videos/{id}` answers 400 40008 "Unknown
    catalog resource type"), so this page is the only place an app can get
    the metadata for an interview clip — the feed carries it whole. Music
    videos (`mv.`) still hydrate from the catalog and need nothing here."""
    out = {}
    for it in sec.get("items") or []:
        cd = it.get("contentDescriptor") or {}
        if cd.get("kind") != "artistUploadedVideo":
            continue
        aid = (cd.get("identifiers") or {}).get("storeAdamID")
        title = ((it.get("titleLinks") or [{}])[0].get("title")) or it.get("title")
        if not aid or not title:
            continue
        entry = {"title": title}
        art = ((it.get("artwork") or {}).get("dictionary") or {}).get("url")
        if art:
            entry["artwork"] = art
        # The shelf's subtitle line for a clip is its length ("30 min 40 sec").
        sub = ((it.get("subtitleLinks") or [{}])[0].get("title")) or ""
        m = DURATION_RE.fullmatch(sub.strip())
        if m and any(m.groups()):
            entry["durationSeconds"] = (int(m.group(1) or 0) * 3600
                                        + int(m.group(2) or 0) * 60
                                        + int(m.group(3) or 0))
        if it.get("showExplicitBadge"):
            entry["explicit"] = True
        out[aid] = entry
    return out


def capture_music_videos():
    """Every shelf on the Music Videos grouping page. Optional end to end —
    a page-fetch failure or an individually missing/renamed shelf just means
    consumers keep their baked seed, never a broken run."""
    try:
        secs = find_sections(serialized(fetch(MUSIC_VIDEOS_URL), "music videos grouping"))
    except Exception as e:
        print(f"WARN music-videos: page fetch/parse failed ({e})")
        return {}, {}
    shelves, uploaded, seen = {}, {}, set()
    for sec in secs:
        t = title_of(sec)
        if t in MUSIC_VIDEOS_SHELVES and t not in seen:
            seen.add(t)
            key, kind = MUSIC_VIDEOS_SHELVES[t]
            ids = video_ids_of(sec) if kind == "video" else ids_of(sec, kind)
            if len(ids) >= MIN_ITEMS:
                shelves[key] = {"kind": kind, "ids": ids}
                if kind == "video":
                    uploaded.update(uploaded_videos_of(sec))
    return shelves, uploaded


def capture_watch_interviews():
    """Radio's "Watch Interviews" room (6749860083) — the latest 20 of a
    mixed music-video/uploaded-video room. Optional: captured separately from
    `capture_radio()` so a miss here can never block the required shelves."""
    try:
        secs = find_sections(serialized(fetch(WATCH_INTERVIEWS_URL), "watch interviews room"))
    except Exception as e:
        print(f"WARN watch-interviews: page fetch/parse failed ({e})")
        return None
    for sec in secs:
        ids = video_ids_of(sec)
        if len(ids) >= MIN_ITEMS:
            uploaded = uploaded_videos_of(sec)
            kept = ids[:20]
            return ({"kind": "video", "ids": kept},
                    {k: v for k, v in uploaded.items() if f"uv.{k}" in kept})
    return None, None


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

    watch_interviews, watch_uploaded = capture_watch_interviews()
    if watch_interviews:
        rooms["radio"]["shelves"]["watch-interviews"] = watch_interviews
        if watch_uploaded:
            rooms["radio"]["uploadedVideos"] = watch_uploaded
        print(f"  radio/watch-interviews videos={len(watch_interviews['ids'])} uploaded={len(watch_uploaded or {})}")
    else:
        print("NOTE: no Watch Interviews shelf captured (consumers keep their baked seed).")

    music_videos_shelves, music_videos_uploaded = capture_music_videos()
    if music_videos_shelves:
        rooms["music-videos"] = {"shelves": music_videos_shelves}
        if music_videos_uploaded:
            rooms["music-videos"]["uploadedVideos"] = music_videos_uploaded
        total_mv = sum(len(s["ids"]) for s in music_videos_shelves.values())
        print(f"  music-videos captured shelves={len(music_videos_shelves)} ids={total_mv} uploaded={len(music_videos_uploaded)}")
    else:
        print("NOTE: no music-videos shelves captured (consumers keep their baked seed).")

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
