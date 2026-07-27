#!/usr/bin/env python3
"""
Capture the current shelf membership of Apple Music's public "Radio" curator
page and emit it as a static JSON feed (radio.json).

The page is Apple's own server-rendered public web page; this reads the
embedded `serialized-server-data` JSON and extracts each shelf's ordered
catalog IDs (stations / albums / playlists). Consumers hydrate those IDs
through the official Apple Music catalog API — this feed carries membership
only, no content.

Run by the scheduled GitHub Action (see .github/workflows/update-feed.yml).
If the shelf membership is unchanged, the existing file (and its capturedAt)
is left untouched so the workflow makes no commit.

Usage:  python3 capture_radio_feed.py radio.json
Exits non-zero if a REQUIRED shelf is missing or suspiciously small, so a
broken capture never replaces the last good feed.
"""
import json, re, html, sys, urllib.request
from datetime import datetime, timezone

CURATOR_URL = "https://music.apple.com/us/curator/apple-music-radio/1531543191"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# SSR section title -> (feed shelf key, SSR content kind, required).
# Required shelves have stable names; a miss means the parse broke and the
# run should fail. Optional shelves (seasonal names, e.g. "Summertime
# Sounds Playlists") are included when present and silently skipped when
# Apple renames them — consumers fall back to their baked snapshot.
SHELVES = {
    "Artists Take Over":           ("artists-take-over",    "radioStation", True),
    "Latest Episodes":             ("latest-episodes",      "radioStation", True),
    "Listen to Interviews":        ("listen-to-interviews", "radioStation", True),
    "Apple Music Club DJ Mixes":   ("club-dj-mixes",        "album",        False),
    "Summertime Sounds Playlists": ("summertime-sounds",    "playlist",     False),
}
KIND_JSON = {"radioStation": "station", "album": "album", "playlist": "playlist"}
MIN_ITEMS = 5   # fewer than this in a required shelf = broken capture


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def serialized(doc):
    m = re.search(r'<script type="application/json" id="serialized-server-data">(.*?)</script>',
                  doc, re.S)
    if not m:
        sys.exit("ERROR: no serialized-server-data on the page (layout changed?)")
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


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "radio.json"
    secs = find_sections(serialized(fetch(CURATOR_URL)))
    if not secs:
        sys.exit("ERROR: no sections found.")

    shelves, seen = {}, set()
    for sec in secs:
        t = title_of(sec)
        if t in SHELVES and t not in seen:
            seen.add(t)
            key, kind, required = SHELVES[t]
            ids = ids_of(sec, kind)
            if len(ids) < MIN_ITEMS:
                if required:
                    sys.exit(f"ERROR: required shelf '{t}' has only {len(ids)} items.")
                continue
            shelves[key] = {"kind": KIND_JSON[kind], "ids": ids}

    missing = [t for t, (_, _, req) in SHELVES.items() if req and t not in seen]
    if missing:
        sys.exit(f"ERROR: required shelves not found (renamed/removed?): {missing}")

    feed = {
        "version": 1,
        "capturedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rooms": {"radio": {"shelves": shelves}},
    }

    # Unchanged membership -> keep the old file byte-identical (old capturedAt
    # included) so the workflow's `git diff --quiet` skips the commit.
    try:
        with open(out_path) as f:
            old = json.load(f)
        if old.get("rooms") == feed["rooms"] and old.get("version") == feed["version"]:
            print(f"{out_path}: membership unchanged, keeping existing file.")
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    with open(out_path, "w") as f:
        json.dump(feed, f, indent=1)
        f.write("\n")
    total = sum(len(s["ids"]) for s in shelves.values())
    print(f"{out_path}: wrote {len(shelves)} shelves, {total} ids.")


if __name__ == "__main__":
    main()
