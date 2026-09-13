#!/usr/bin/env python3
"""
Capture shelf membership for the editorial rooms Pulsatio can't reach through
the Apple Music API, and emit it as a static JSON feed (feed.json).

Six families of shelves are captured, all from Apple's own server-rendered
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
  · CURATOR_ROOMS: extra shelves on 17 bespoke curator/room pages (Boiler
    Room, Cercle, Defected, Tomorrowland, Beats in Space, …) plus a couple of
    GenreRoomBuilder "canon" shelves (Fitness, Sports) — all OPTIONAL, merged
    into `rooms` alongside whatever a room already has from the families
    above. Beats in Space's "episodes" shelf is the one case where two
    on-page titles feed a single shelf key: "Latest Show" alone comes in
    under MIN_ITEMS, so it's concatenated with "Tim Sweeney + Guest DJ Mixes"
    and deduped.
  · GENRE_PAGES: the top-level `pages` object — every shelf, in page order,
    on each of the 82 genre curator pages (`/curator/x/<id>`) the macOS app's
    Genres grid links to (see `GENRE_PAGES` for the source). Unlike the other
    five families this isn't merged into `rooms`: it's a separate, page-shaped
    `pages.<curatorID>` object the app uses to mirror Apple's own curator page
    layout exactly, hero carousel included. A page capture that comes back
    with 0 shelves is carried forward from the previous feed.json instead of
    dropping the page.

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
shelf could be captured at all, or if NO genre page could be captured at
all — so a broken parse never replaces last-good.
"""
import json, os, re, html, sys, time, urllib.request
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

# One entry per curator/room page that carries extra editorial shelves the app
# has baked as static seeds (bespoke curator rooms like Boiler Room, Cercle,
# Defected, ...; a couple of GenreRoomBuilder "canon" shelves like Fitness and
# Sports). sourceURL -> (Pulsatio roomID, {on-page shelf title: (feed shelf
# key, JSON kind)}). Every shelf here is OPTIONAL, like MUSIC_VIDEOS_SHELVES:
# a page miss or a renamed title just means consumers keep their baked seed.
# A shelf title is matched after stripping whitespace (Apple's own SSR titles
# are sometimes padded, e.g. Beats in Space's "Latest Show" renders "Latest
# Show  " with trailing spaces). Two titles mapping to the same feed key are
# concatenated in page order and deduped — Beats in Space's "episodes" shelf
# needs both "Latest Show" (too few items alone) and "Tim Sweeney + Guest DJ
# Mixes" to clear MIN_ITEMS.
CURATOR_ROOMS = {
    "https://music.apple.com/us/curator/x/979231701": ("acoustic", {
        "Playlists": ("playlists", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/988656348": ("african", {
        "Daily Top 100": ("daily-top-100", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/1573950910": ("beats-in-space", {
        "Latest Show": ("episodes", "album"),
        "Tim Sweeney + Guest DJ Mixes": ("episodes", "album"),
        "Guest Interviews": ("guest-interviews", "station"),
        "Tim Sweeney Specials": ("tim-sweeney-specials", "album"),
        "Tim’s Current Obsessions": ("tim-s-current-obsessions", "album"),
    }),
    "https://music.apple.com/us/curator/x/1082539854": ("boiler-room", {
        "Now in Spatial Audio": ("now-in-spatial-audio", "album"),
        "Just Added": ("just-added", "album"),
        "Boiler Room: An Hour With": ("an-hour-with", "album"),
        "Boiler Room Radio: Extended Interviews": ("radio-extended-interviews", "station"),
        "Boiler Room Radio: DJ Mixes": ("radio-dj-mixes", "album"),
    }),
    "https://music.apple.com/us/curator/x/1576455458": ("cafe-del-mar", {
        "Sundown Mix": ("sundown-mix", "album"),
        "The Evolution of Chill": ("the-evolution-of-chill", "album"),
    }),
    "https://music.apple.com/us/curator/x/1558721971": ("cercle", {
        "DJ Mixes in Spatial Audio": ("dj-mixes-in-spatial-audio", "album"),
        "DJ Mixes & Live Sets": ("dj-mixes-live-sets", "album"),
        "Cercle Records": ("cercle-records", "album"),
        "Chill": ("chill", "album"),
        "House": ("house", "album"),
        "Melodic": ("melodic", "album"),
        "Techno": ("techno", "album"),
    }),
    "https://music.apple.com/us/curator/x/1558722078": ("defected", {
        "Defected Ibiza": ("defected-ibiza", "album"),
        "Glitterbox Ibiza": ("glitterbox-ibiza", "album"),
        "In The House": ("in-the-house", "album"),
        "Broadcasting House": ("broadcasting-house", "album"),
        "Defected Malta": ("defected-malta", "album"),
        "Defected Worldwide": ("defected-worldwide", "album"),
        "More DJ Mixes": ("more-dj-mixes", "album"),
    }),
    "https://music.apple.com/us/curator/x/1576455424": ("hi-ibiza", {
        "Playlists": ("playlists", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/1576454019": ("ministry-of-sound", {
        "Ibiza": ("ibiza", "album"),
        "Fitness": ("fitness", "album"),
        "The Annual": ("the-annual", "album"),
        "The Sessions": ("the-sessions", "album"),
        "More DJ Mixes": ("more-dj-mixes", "album"),
    }),
    "https://music.apple.com/us/curator/x/1774051496": ("naina-presents", {
        "NAINA in the mix": ("naina-in-the-mix", "album"),
    }),
    "https://music.apple.com/us/curator/x/1697427961": ("phantasy-sound", {
        "DJ Mixes": ("dj-mixes", "album"),
        "Releases": ("releases", "album"),
    }),
    "https://music.apple.com/us/curator/x/1668248223": ("rnb-only", {
        "Office Hours (Work, Study, Chill)": ("office-hours", "album"),
        "R&B ONLY SESSIONS": ("sessions", "album"),
        "Live Show Sets": ("live-show-sets", "album"),
    }),
    "https://music.apple.com/us/curator/x/993271379": ("soulection", {
        "Latest Episodes": ("latest-episodes", "station"),
        "Albums": ("albums", "album"),
        "DJ Mixes": ("dj-mixes", "album"),
    }),
    "https://music.apple.com/us/curator/x/1524337266": ("tomorrowland", {
        "Tomorrowland 2026": ("tomorrowland-2026", "album"),
        "Tomorrowland Winter 2026": ("tomorrowland-winter-2026", "album"),
        "Tomorrowland Playlists": ("playlists", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/1796068191": ("unvrs", {
        "Playlists": ("playlists", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/1558256909": ("fitness", {
        "Apple Fitness+": ("apple-fitness", "playlist"),
    }),
    "https://music.apple.com/us/curator/x/1555172867": ("sports", {
        "MLS Club Playlists": ("mls-club-playlists", "playlist"),
        "NFL Team Playlists": ("nfl-team-playlists", "playlist"),
        "MLB Walk-Up Playlists": ("mlb-walk-up-playlists", "playlist"),
    }),
}

# Every genre curator page (`/curator/x/<id>`) the macOS app's Genres grid
# links to, in the app's own display order — mirrors `featuredCurators` in
# UIModule/AMGenresView.swift (minus its "grouping-34" Music Videos pseudo
# entry, which has no curator page of its own). Captured whole into the
# top-level `pages` object by `capture_genre_pages`, independent of `rooms`.
GENRE_PAGES = {
    "979231701": "Acoustic",
    "988656348": "African",
    "1747003654": "Afrobeats",
    "1878215041": "Alpha Women",
    "976439526": "Alternative",
    "976439527": "Americana",
    "982302294": "Anime",
    "982302682": "Arabic",
    "1554941247": "Behind the Songs",
    "976439528": "Blues",
    "982307152": "Bollywood",
    "1482068485": "Christian",
    "976439531": "Classic Rock",
    "976439532": "Classical",
    "976439534": "Country",
    "976439535": "Dance",
    "1554938339": "Decades",
    "1526866135": "'60s",
    "1526866261": "'70s",
    "1526866189": "'80s",
    "1526866514": "'90s",
    "1526866702": "2010s",
    "1441811365": "DJ Mixes",
    "976439536": "Electronic",
    "1558256771": "Essentials",
    "1555173397": "Family",
    "976439586": "Film, TV & Stage",
    "982308048": "French Pop",
    "1482068827": "Gospel",
    "979231690": "Hard Rock",
    "976439539": "Hip-Hop",
    "1526756058": "Hits",
    "976439540": "Holiday",
    "976439541": "Indie",
    "1526867390": "Islamic",
    "976439542": "Jazz",
    "976439538": "Kids",
    "988658197": "K-Pop",
    "1531542847": "Latin",
    "1526866649": "Live Music",
    "1558257331": "Love",
    "976439543": "Metal",
    "976439544": "Música Mexicana",
    "976439545": "Música Tropical",
    "976439547": "Oldies",
    "976439548": "Pop",
    "982348865": "Pop Italiano",
    "976439549": "Pop Latino",
    "976439550": "Punk",
    "976439551": "R&B",
    "976439552": "Reggae",
    "976439554": "Rock",
    "988965390": "Rock y Alternativo",
    "976439585": "Soul/Funk",
    "1558257235": "Summertime Sounds",
    "1532467784": "Up Next",
    "976439553": "Urbano Latino",
    "976439587": "Worldwide",
    "1555172867": "Sports",
    "1558256909": "Fitness",
    "1558256251": "Chill",
    "1558257257": "Sleep",
    "1558257443": "Wellbeing",
    "1558256919": "Feel Good",
    "1558257035": "Party",
    "1558257095": "Focus",
    "1558256865": "Feeling Blue",
    "1558257146": "Motivation",
    "1555171646": "After Hours",
    "1555172841": "Alone Time",
    "1558257238": "Commuting",
    "1555172881": "Eating & Cooking",
    "1555171590": "Evening",
    "1558257191": "Gaming",
    "1555172807": "Heartbreak",
    "1558256170": "Home",
    "1555171966": "Morning",
    "1555172657": "Outdoors",
    "1555173047": "Social",
    "1555172036": "Vacation",
    "1555167098": "Weekend",
    "1555172573": "Work",
}

# GENRE_PAGES item contentDescriptor.kind -> pages-schema id prefix. Distinct
# from VIDEO_KIND_PREFIX/KIND_JSON below: the pages schema is a superset (it
# also carries songs, artists, and both curator flavors) because it mirrors a
# whole curator page rather than one named shelf.
GENRE_PAGE_KIND_PREFIXES = {
    "album": "al.", "song": "so.", "artist": "ar.",
    "musicVideo": "mv.", "artistUploadedVideo": "uv.",
    "appleCurator": "ac.", "curator": "cu.",
}
# playlist/radioStation ids are already prefixed `pl.`/`ra.` by Apple's SSR.
GENRE_PAGE_ALREADY_PREFIXED = {"playlist": "pl.", "radioStation": "ra."}

KIND_JSON = {"radioStation": "station", "album": "album", "playlist": "playlist"}
CURATOR_KIND_TO_SSR = {"station": "radioStation", "album": "album", "playlist": "playlist"}
MIN_ITEMS = 5   # fewer than this in a required shelf = broken capture

# contentDescriptor.kind -> the prefix VideoPlaybackItem.parse expects.
VIDEO_KIND_PREFIX = {"musicVideo": "mv.", "artistUploadedVideo": "uv."}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


# Per-run memo for `fetch`, keyed by URL. GENRE_PAGES pages overlap heavily
# with the legacy GENRE_CURATORS new-releases probe and 4 of the CURATOR_ROOMS
# pages (Acoustic 979231701, African 988656348, Fitness 1558256909, Sports
# 1555172867 all name the same curator URL) — every one of those call sites
# routes through `fetch_cached` instead of calling `fetch` directly, so a
# shared page is fetched, and slept for, at most once per run.
_fetch_memo = {}


def fetch_cached(url):
    """Like `fetch`, but memoized per run: a repeat call for a URL already
    fetched this run returns (or re-raises) instantly with no network hit and
    no sleep. A new URL sleeps 1.0s after the real fetch, success or failure,
    same as every other network call in this script."""
    if url in _fetch_memo:
        cached = _fetch_memo[url]
        if isinstance(cached, Exception):
            raise cached
        return cached
    try:
        doc = fetch(url)
    except Exception as e:
        _fetch_memo[url] = e
        time.sleep(1.0)
        raise
    _fetch_memo[url] = doc
    time.sleep(1.0)
    return doc


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


def genre_page_item_id(cd):
    """Map one GENRE_PAGES item's contentDescriptor to a prefixed pages-schema
    id (see GENRE_PAGE_KIND_PREFIXES / GENRE_PAGE_ALREADY_PREFIXED), or None
    to drop the item — a null contentDescriptor, or a kind the pages schema
    doesn't carry (e.g. a bare header item in the hero carousel)."""
    if not cd:
        return None
    ids = cd.get("identifiers") or {}
    aid = ids.get("storeAdamID") or ids.get("id")
    if not aid:
        return None
    kind = cd.get("kind")
    prefix = GENRE_PAGE_ALREADY_PREFIXED.get(kind)
    if prefix:
        return aid if aid.startswith(prefix) else f"{prefix}{aid}"
    prefix = GENRE_PAGE_KIND_PREFIXES.get(kind)
    return f"{prefix}{aid}" if prefix else None


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


def capture_curator_rooms():
    """Every shelf listed in CURATOR_ROOMS, across its 17 curator/room pages
    (one fetch per page — via `fetch_cached`, since 4 of these pages are also
    GENRE_PAGES pages captured by `capture_genre_pages`). Optional end to
    end like capture_music_videos: a page-fetch failure, a title Apple has
    renamed, or a shelf that comes back under MIN_ITEMS just means consumers
    keep their baked seed — never a broken run. Returns {roomID: {feed key:
    {kind, ids}}}."""
    by_room = {}
    for url, (room, shelf_map) in CURATOR_ROOMS.items():
        try:
            secs = find_sections(serialized(fetch_cached(url), f"curator room {room}"))
        except Exception as e:
            print(f"WARN {room}: page fetch/parse failed ({e})")
            continue

        titled = {}
        for sec in secs:
            t = title_of(sec).strip()
            if t and t not in titled:
                titled[t] = sec

        collected = {}   # feed key -> (kind, [ids...]) accumulated in page order
        for title, (feed_key, kind) in shelf_map.items():
            sec = titled.get(title)
            if sec is None:
                print(f"NOTE {room}/{feed_key}: shelf title not found on page: '{title}'")
                continue
            ids = video_ids_of(sec) if kind == "video" else ids_of(sec, CURATOR_KIND_TO_SSR[kind])
            kind_ids = collected.setdefault(feed_key, (kind, []))[1]
            kind_ids.extend(ids)

        shelves = {}
        for feed_key, (kind, ids) in collected.items():
            deduped = list(dict.fromkeys(ids))[:40]
            if len(deduped) < MIN_ITEMS:
                print(f"NOTE {room}/{feed_key}: only {len(deduped)} ids, skipping.")
                continue
            shelves[feed_key] = {"kind": kind, "ids": deduped}
        if shelves:
            by_room[room] = shelves
    return by_room


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
    """The genre's "New Releases" shelf, or None when Apple isn't showing one.
    Uses `fetch_cached`: this curator id is also a GENRE_PAGES page, captured
    separately by `capture_genre_pages` from the same page fetch."""
    doc = fetch_cached(f"https://music.apple.com/us/curator/x/{curator_id}")
    for sec in find_sections(serialized(doc, f"{room} curator")):
        if title_of(sec).strip().lower() not in NEW_RELEASES_TITLES:
            continue
        ids = ids_of(sec, "album")
        if len(ids) >= MIN_ITEMS:
            return {"kind": "album", "ids": ids}
    return None


def capture_one_genre_page(curator_id, name):
    """One GENRE_PAGES entry: every shelf on `/curator/x/<curator_id>`, in
    page order, as the pages-schema dict (`shelves`/`artwork`/
    `uploadedVideos`) — or raises if the fetch/parse failed or the page came
    back with 0 shelves, for `capture_genre_pages` to catch."""
    url = f"https://music.apple.com/us/curator/x/{curator_id}"
    secs = find_sections(serialized(fetch_cached(url), f"{name} genre page"))

    shelves, artwork, uploaded = [], {}, {}
    for sec in secs:
        uploaded.update(uploaded_videos_of(sec))
        if sec.get("itemKind") == "headerComponentModel":
            continue
        title = "" if sec.get("itemKind") == "flowcaseLockup" else title_of(sec).strip()
        rows = ((sec.get("presentation") or {}).get("layout") or {}).get("numberOfRows")
        rows = rows if isinstance(rows, int) and rows >= 1 else 1

        ids = []
        for it in sec.get("items") or []:
            aid = genre_page_item_id(it.get("contentDescriptor"))
            if not aid:
                continue
            ids.append(aid)
            if aid.startswith(("ac.", "cu.")):
                art = ((it.get("artwork") or {}).get("dictionary") or {}).get("url")
                if art:
                    artwork[aid] = art
        deduped = list(dict.fromkeys(ids))[:40]
        if deduped:
            shelves.append({"title": title, "rows": rows, "items": deduped})

    if not shelves:
        raise ValueError("0 shelves parsed")

    entry = {"shelves": shelves}
    if artwork:
        entry["artwork"] = artwork
    if uploaded:
        entry["uploadedVideos"] = uploaded
    return entry


def capture_genre_pages(old_pages):
    """The top-level `pages` object: every shelf, in page order, on each of
    the 82 GENRE_PAGES curator pages (one fetch per page, shared via
    `fetch_cached` with the legacy GENRE_CURATORS/CURATOR_ROOMS fetches of the
    same URLs). A page whose capture fails or comes back with 0 shelves is
    carried forward from `old_pages` (the previous feed.json) instead of
    dropping it — a transient miss should never blank out a page consumers
    already have. Exits non-zero only if NO page could be captured fresh at
    all, the same broken-parse signal `main` already uses for GENRE_CURATORS.
    Returns (pages, shelf_count, id_count)."""
    pages, fresh, shelf_count, id_count = {}, 0, 0, 0
    for curator_id, name in GENRE_PAGES.items():
        try:
            entry = capture_one_genre_page(curator_id, name)
        except Exception as e:
            print(f"WARN page {curator_id}: {name}: {e}")
            if curator_id in old_pages:
                pages[curator_id] = old_pages[curator_id]
                print(f"  page {curator_id} {name}: carried forward from previous feed.json")
            continue
        pages[curator_id] = entry
        fresh += 1
        n_shelves = len(entry["shelves"])
        n_ids = sum(len(s["items"]) for s in entry["shelves"])
        shelf_count += n_shelves
        id_count += n_ids
        print(f"  page {curator_id} {name} shelves={n_shelves} ids={n_ids}")

    if fresh == 0:
        sys.exit("ERROR: no genre page captured at all — parse broken?")
    return pages, shelf_count, id_count


def write_if_changed(path, feed):
    """Keep the old file byte-identical when membership is unchanged, so the
    workflow's `git diff --quiet` skips the commit. Compares `rooms` and
    `version` always, plus `pages` when `feed` carries one (feed.json does;
    radio.json never does — it stays `rooms.radio` only)."""
    try:
        with open(path) as f:
            old = json.load(f)
        unchanged = (old.get("rooms") == feed["rooms"] and old.get("version") == feed["version"])
        if unchanged and "pages" in feed:
            unchanged = old.get("pages") == feed["pages"]
        if unchanged:
            print(f"{path}: membership unchanged, keeping existing file.")
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    with open(path, "w") as f:
        json.dump(feed, f, indent=1)
        f.write("\n")
    total = sum(len(s["ids"]) for r in feed["rooms"].values() for s in r["shelves"].values())
    msg = f"{path}: wrote {len(feed['rooms'])} rooms, {total} ids"
    if "pages" in feed:
        page_ids = sum(len(s["items"]) for p in feed["pages"].values() for s in p["shelves"])
        msg += f", {len(feed['pages'])} pages, {page_ids} page ids"
    print(msg + ".")


def main():
    start = time.time()
    combined_path = sys.argv[1] if len(sys.argv) > 1 else "feed.json"
    radio_path = sys.argv[2] if len(sys.argv) > 2 else "radio.json"

    # Loaded up front so a page whose fresh capture fails can carry forward
    # its entry from the previous run instead of dropping out of the feed.
    old_pages = {}
    try:
        with open(combined_path) as f:
            old_pages = json.load(f).get("pages") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        pass

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

    if skipped:
        print(f"NOTE: no New Releases shelf for {skipped} (consumers keep their baked seed).")
    # Every genre failing at once means the parse broke, not that Apple pulled
    # eleven shelves on the same day.
    if captured == 0:
        sys.exit("ERROR: no genre New Releases shelf captured at all — parse broken?")

    curator_rooms = capture_curator_rooms()
    for room, shelves in curator_rooms.items():
        room_entry = rooms.setdefault(room, {"shelves": {}})
        for key, shelf in shelves.items():
            room_entry["shelves"][key] = shelf
        total_ids = sum(len(s["ids"]) for s in shelves.values())
        print(f"  {room:20s} curator shelves={len(shelves)} ids={total_ids}")
    if not curator_rooms:
        print("NOTE: no curator-room shelves captured (consumers keep their baked seed).")

    pages, page_shelf_count, page_id_count = capture_genre_pages(old_pages)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_if_changed(combined_path, {"version": 1, "capturedAt": now, "rooms": rooms, "pages": pages})
    # Legacy single-room file: app builds shipped before feed.json read this.
    # Carries `rooms.radio` only — no `pages` — same shape it has always had.
    write_if_changed(radio_path, {"version": 1, "capturedAt": now,
                                  "rooms": {"radio": rooms["radio"]}})

    try:
        out_bytes = os.path.getsize(combined_path)
    except OSError:
        out_bytes = 0
    print(f"TOTAL: {len(pages)} pages, {page_shelf_count} shelves, {page_id_count} ids, "
          f"{out_bytes} bytes, {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
