#!/usr/bin/env python3
"""Firebot presence: who is in chat now, and who just arrived (stdlib only).

Single concept: turning Firebot's viewer database into a presence roster and
a join diff -- the pure half of the bridge's roster poll. The bridge owns the
HTTP call site, the timer and the displayd push; nothing here touches a
socket, a displayd payload shape, or a panel.

WHICH SOURCE, AND WHY (read off the Firebot 5.66.7 tree, not guessed):

  GET /api/v1/viewers         the viewer DATABASE. ``getAllUsernamesWithIds``
                              projects ``{_id, username, displayName}`` over
                              ``{twitch: true}`` -- every viewer Firebot has
                              ever recorded, with no presence field at all.
                              It is the endpoint that looks right, and it is
                              the wrong one: drawing it as "who is here now"
                              would fabricate presence.
  GET /api/v1/viewers/export  the same documents UNPROJECTED, so each one
                              carries Firebot's own ``online`` flag.

``online`` is Firebot's live presence model, not a stored preference.
``ActiveUserHandler`` keeps an online cache with a 450 s TTL, marks a user
online from the Helix chatter poll (every 5 minutes) and from every chat
message, and flips the viewer document's ``online`` field on the way in and
out. A roster filtered on ``online === true`` is therefore Firebot's own
answer to "who is presently in the channel", with that TTL as its documented
lag: an arrival shows as soon as Firebot sees the user (a chat message, or
the 5-minute poll at the latest), a quiet departure can take ~7.5 minutes.

The export is the whole viewer database, so it is the wrong thing to poll on
a large channel -- this is a small one (seven viewers in the captain's own
capture of it). ``--roster-url`` exists so a lighter shim can be pointed at
without touching this code.

DEPARTURES ARE NOT ANNOUNCED, deliberately. The request was for joins
("events where users join the chat as well as when they chat"); a departure
leaves the roster pane on its own, and a "left" line beside it would double
the same fact at twice the clutter on a panel read from across a room.

Stdlib only, no credentials: the API needs none, and no key is written to a
file or a log.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("firebot-chat-bridge")

VIEWERS_PATH = "/api/v1/viewers/export"
DEFAULT_TIMEOUT = 6.0

# How often the viewer list is read. Firebot's own presence TTL is 7.5 minutes
# (a 5-minute Helix chatter poll, plus chat activity), so a tighter cadence
# buys nothing but load, and a slower one delays a join showing up.
ROSTER_INTERVAL = 10.0


class RosterUnavailable(Exception):
    """One poll could not read the viewer list. The caller logs it and keeps
    the last-known roster, because an unreadable list is not evidence that
    nobody is present."""


def url_for(host, port, path=VIEWERS_PATH):
    """The roster endpoint on the same host and port as the overlay socket:
    Firebot's default web server serves both."""
    return "http://%s:%d%s" % (host, int(port), path)


def add_arguments(parser):
    """The presence flags, owned by the module that owns the poller: the
    loop and the way to point it somewhere else travel together."""
    parser.add_argument("--roster-url", default=os.environ.get("ROSTER_URL"),
                        help="viewer-list URL for presence (default: the "
                             "Firebot host + %s)" % VIEWERS_PATH)
    parser.add_argument("--roster-interval", type=float,
                        default=float(os.environ.get("ROSTER_INTERVAL",
                                                     ROSTER_INTERVAL)),
                        help="seconds between viewer-list reads "
                             "(default: %(default)s)")


def key_of(viewer):
    """Stable identity for one viewer: the Twitch id, else the username."""
    name = str(viewer.get("username") or "").strip().lower()
    return str(viewer.get("id") or "") or name


def parse_viewers(payload):
    """The export payload -> the present viewers, alphabetical by the name
    that gets drawn. Total: anything that is not a list of online viewers
    yields [] rather than raising, because this is fed by the network."""
    if isinstance(payload, dict):
        payload = payload.get("viewers")
    if not isinstance(payload, (list, tuple)):
        return []
    out, seen = [], set()
    for item in payload:
        if not isinstance(item, dict) or item.get("online") is not True:
            continue
        name = item.get("displayName") or item.get("username")
        if not isinstance(name, str) or not name.strip():
            continue
        viewer = {
            "id": str(item.get("_id") or item.get("id") or ""),
            "username": str(item.get("username") or ""),
            "display_name": name.strip(),
        }
        if key_of(viewer) in seen:
            continue
        seen.add(key_of(viewer))
        out.append(viewer)
    out.sort(key=lambda v: v["display_name"].lower())
    return out


def arrivals(previous, current):
    """Who is new since the previous read. ``previous is None`` is the
    baseline poll and reports nobody: a bridge restart must not announce the
    whole channel as having just walked in."""
    if previous is None:
        return []
    return [v for v in current if key_of(v) not in previous]


def join_event(viewer, now=None):
    """One arrival as a chat-feed payload.

    Joins ride the SAME input the messages do, so the panel has one ordered
    stream and interleaves them by arrival without a second clock. ``text``
    is an empty string on purpose: a join is not a message, and the panel
    draws it as its own kind of line.
    """
    stamp = time.time() if now is None else float(now)
    millis = int(stamp * 1000)
    return {
        "id": "join:%s:%d" % (key_of(viewer), millis),
        "author": str(viewer.get("username") or viewer.get("display_name") or "???"),
        "display_name": str(viewer.get("display_name")
                            or viewer.get("username") or "???"),
        "text": "",
        "join": True,
        "timestamp": millis,
    }


def roster_payload(viewers, now=None):
    """The snapshot the left pane draws. ``ts`` is what lets the renderer say
    the roster is stale instead of showing last-known presence as live."""
    rows = list(viewers or [])
    return {"viewers": rows, "count": len(rows),
            "ts": time.time() if now is None else float(now)}


class Roster:
    """One fetch per cycle, shared between the join diff and the pane.

    ``poll()`` reads the viewer list ONCE and answers both questions off
    that single read, so "who is here now" and "who arrived" can never
    disagree: they are not two reads that happened to agree, they are one.
    """

    def __init__(self, url, timeout=DEFAULT_TIMEOUT):
        self.url = url
        self.timeout = float(timeout)
        self.previous = None   # keys of the last successful read, or None

    def fetch(self):
        """One GET. Raises RosterUnavailable with the reason."""
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as err:
            raise RosterUnavailable("HTTP %d from %s: %s"
                                    % (err.code, self.url,
                                       err.read(200).decode("replace")))
        except Exception as err:
            raise RosterUnavailable("%s: %s" % (self.url, err))
        if not 200 <= int(status) < 300:
            raise RosterUnavailable("HTTP %s from %s" % (status, self.url))
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as err:
            raise RosterUnavailable("not JSON from %s: %s" % (self.url, err))
        return parse_viewers(payload)

    def poll(self):
        """-> (viewers, arrivals). A failed read keeps the previous roster,
        so a blip can never re-announce everyone still in the channel."""
        viewers = self.fetch()
        joined = arrivals(self.previous, viewers)
        self.previous = {key_of(v) for v in viewers}
        return viewers, joined


class Presence:
    """The bridge's presence loop: ONE roster read per cycle, both answers
    pushed to displayd through ``push(input_name, payload)`` -- the roster
    snapshot always, and one join event per arrival onto the chat feed.

    One read per cycle is the whole point: the pane's roster and the join
    diff come off the same fetch, so they cannot disagree.
    """

    def __init__(self, roster, push, interval=ROSTER_INTERVAL):
        self.roster = roster
        self.push = push
        self.interval = float(interval)

    def poll_once(self):
        """One cycle; returns the number of joins pushed. A failed read is a
        warning, never the end of the bridge: the socket half must stay up."""
        try:
            viewers, joined = self.roster.poll()
        except RosterUnavailable as err:
            LOG.warning("roster unavailable: %s", err)
            return 0
        self.push("roster", roster_payload(viewers))
        for viewer in joined:
            event = join_event(viewer)
            LOG.info("join: %s", event["display_name"])
            self.push("message", event)
        return len(joined)

    def forever(self, stop=None):
        while stop is None or not stop():
            try:
                self.poll_once()
            except Exception as err:  # never take the bridge down
                LOG.warning("roster poll failed: %s", err)
            if _nap(self.interval, stop):
                return


def _nap(seconds, stop):
    """Sleep in slices so a stop request is honoured promptly. True when
    the loop should return."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if stop is not None and stop():
            return True
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    return stop is not None and stop()
