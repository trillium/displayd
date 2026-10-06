"""The two-pane chat panel: the present-viewer roster left, the chat right.

Covers the two things the captain asked for and the two things the beads
pinned, so a later change cannot quietly undo either:

- joins appear beside the messages (task-vd6qk), and the roster pane
  distinguishes who is in the channel right now (task-3yep9);
- a message still never ages out -- it leaves state only on a moderation
  delete (project-a4t.8);
- with nothing pushed the panel still says it is waiting, and it does not
  frame an empty roster column and silence (project-a4t.8.1).

Presence is fetched ONCE per cycle by the bridge and pushed: the tests here
pin that single read (a fake Firebot HTTP server counts the requests), that
the join diff and the pane come off it, and that the daemon side never
fetches anything at all.

The panel is a template surface, so the tests also pin what that migration can
silently break: the shipped template and the renderer's variables are the same
set, the one raw slot is the pane layer, a caller-supplied name or message
arrives escaped, and no view module draws with Pillow or opens a socket.

Run from the repo root:  python3 -m unittest tests.test_chat -v
"""

import json
import os
import re
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "renderers"))
sys.path.insert(0, os.path.join(ROOT, "bridges"))

import displayd
import _html_native
import _html_templates as templates
import chat
import chat_fit as fit
import chat_panes as panes
import firebot_roster as roster

TEMPLATE_PATH = os.path.join(ROOT, "html-templates", "chat.html")
ERROR_RULE = (214, 74, 74)
MSG = {"id": "m1", "author": "testuser", "display_name": "TestUser",
       "text": "hello wall", "color": "#2E8B57", "timestamp": 1789801169829}

native_built = unittest.skipUnless(
    any(os.path.exists(os.path.join(_html_native.NATIVE_DIR, name))
        for name in _html_native.LIB_NAMES),
    "native library not built (tools/build_litehtml.sh)")


class FakeFb:
    def __init__(self, w=960, h=540):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


def make_screen(w=1920, h=1080):
    return displayd.Screen(FakeFb(w, h))


def feed_screen(events, deletes=(), w=1920, h=1080):
    """A screen with the real chat feed store and these events pushed."""
    screen = make_screen(w, h)
    store = displayd.FeedStore()
    found = displayd.load_renderers(displayd.RENDERER_DIR)
    store.declare("chat", "message", found["chat"]["inputs"]["message"])
    store.declare("chat", "delete", found["chat"]["inputs"]["delete"])
    for event in events:
        store.push("chat", "message", event,
                   found["chat"]["inputs"]["message"])
    for gone in deletes:
        store.push("chat", "delete", gone, found["chat"]["inputs"]["delete"])
    screen.feeds = store
    return screen


class StubScreen:
    """A screen whose feed hands back whatever a test says, so the
    renderer's own patience can be tested past the validating door."""

    def __init__(self, payloads, w=1920, h=1080):
        self.W, self.H = w, h
        self.feeds = self
        self._payloads = list(payloads)

    def get(self, _renderer, _name):
        return self._payloads

    def get_input(self, _renderer, _name):
        return list(self._payloads)


def viewer(name, vid=None):
    return {"id": vid or name.lower(), "username": name.lower(),
            "display_name": name}


def document(screen, events=(), viewers=(), max_lines=7, state="none", age=None,
             title="CHAT", fg=chat.TEXT, bg=(10, 10, 14)):
    """The panel's document for this state, the way chat.run builds it."""
    return chat._document(screen, title, list(events), max_lines,
                          list(viewers), chat._ink(fg, bg), state, age)[0]


def is_error_card(frame):
    return frame is not None and \
        frame.getpixel((frame.size[0] // 2, 1)) == ERROR_RULE


def lit_pixels(frame, floor=24):
    small = frame.resize((160, 90)).convert("L")
    return sum(1 for p in small.getdata() if p > floor)


def source(name):
    path = name if os.path.isabs(name) else os.path.join(ROOT, name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ------------------------------------------------------------------ contract


class TestAdvertised(unittest.TestCase):
    """The view contract: what a client may push and select."""

    def entry(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("chat", found)
        self.assertIn("module", found["chat"], "chat failed to import: %s"
                      % found["chat"].get("broken"))
        return found["chat"]

    def test_params_and_inputs_still_validate(self):
        entry = self.entry()
        for spec in list(entry["params"].values()) + \
                list(entry["inputs"].values()):
            self.assertIn(spec.get("type"), displayd.INPUT_TYPES)
        displayd.validate_params({}, chat.PARAMS)
        displayd.validate_params({"title": "CHAT", "lines": 5,
                                  "color": "#fff"}, chat.PARAMS)
        with self.assertRaises(ValueError):
            displayd.validate_params({"lines": "many"}, chat.PARAMS)

    def test_the_message_contract_is_unchanged(self):
        # The router and the two push endpoints are untouched: a message is
        # still {author, text, ...}, and a join is that plus a flag.
        spec = self.entry()["inputs"]["message"]
        self.assertEqual(spec["required"], ["author", "text"])
        displayd.validate_value(dict(MSG), spec, "chat.message")
        displayd.validate_value({"author": "a", "text": "", "join": True},
                                spec, "chat.message")
        with self.assertRaises(ValueError):
            displayd.validate_value({"author": "a"}, spec, "chat.message")

    def test_the_roster_input_is_a_one_deep_snapshot(self):
        spec = self.entry()["inputs"]["roster"]
        self.assertEqual(spec["buffer"], 1)
        displayd.validate_value({"viewers": [viewer("A")], "count": 1,
                                 "ts": 1.0}, spec, "chat.roster")
        with self.assertRaises(ValueError):
            displayd.validate_value({"viewers": "nope"}, spec, "chat.roster")

    def test_a_message_is_retained_unbounded(self):
        # project-a4t.8: the state is not aged out downstream either.
        self.assertEqual(self.entry()["inputs"]["message"]["buffer"], 0)
        self.assertEqual(self.entry()["inputs"]["delete"]["buffer"], 0)


class TestTemplateContract(unittest.TestCase):
    """chat.html and the renderer's variables cannot drift apart."""

    def declared(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            text = handle.read()
        body = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        return set(templates.PLACEHOLDER_RE.findall(body)), body, text

    def test_the_view_renders_through_the_shipped_template(self):
        self.assertIn(chat.TEMPLATE, source("renderers/chat.py"))
        self.assertTrue(os.path.isfile(TEMPLATE_PATH),
                        "html-templates/chat.html is the shipped surface")

    def test_the_shipped_template_matches_the_supplied_variables(self):
        declared, _body, _text = self.declared()
        supplied = set(panes.chrome(chat._ink(chat.TEXT), "CHAT", 3, "live",
                                    2.0, ""))
        self.assertEqual(declared, supplied)

    def test_exactly_one_raw_slot_and_it_is_the_pane_layer(self):
        _declared, body, _text = self.declared()
        slots = templates.RAW_RE.findall(body)
        self.assertEqual(slots, ["panes"])
        self.assertIn("chat_panes", source("html-templates/chat.html"))

    def test_no_javascript_and_no_remote_resources(self):
        _declared, body, _text = self.declared()
        lowered = body.lower()
        for banned in ("<script", "javascript:", "@import", "http://",
                       "https://"):
            self.assertNotIn(banned, lowered)

    def test_every_variable_is_documented_in_the_file(self):
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            head = handle.read(6000)
        for key in panes.chrome(chat._ink(chat.TEXT), "CHAT", 3, "live", 2.0,
                                ""):
            self.assertIn(key, head,
                          "%s is used but not documented in the header" % key)


class TestNoPillowAndNoFetchInTheView(unittest.TestCase):
    """Two hard constraints, as source rules."""

    def test_no_pillow_drawing_left_in_the_view(self):
        # The oracle for this increment: a stray ImageDraw means the template
        # path is decoration over a live Pillow draw loop.
        for name in ("renderers/chat.py", "renderers/chat_panes.py",
                     "renderers/chat_fit.py"):
            with self.subTest(module=name):
                text = source(name)
                self.assertNotIn("ImageDraw", text)
                self.assertNotIn("ImageFont", text)

    def test_the_view_never_opens_a_socket_or_fetches(self):
        # Content enters only through the doors the daemon already has: a
        # renderer that fetched would be a second, invisible ingress path.
        for name in ("renderers/chat.py", "renderers/chat_panes.py",
                     "renderers/chat_fit.py"):
            with self.subTest(module=name):
                text = source(name)
                for banned in ("urllib", "socket", "urlopen", "http.client"):
                    self.assertNotIn(banned, text)


# -------------------------------------------------------------------- roster


class TestRosterState(unittest.TestCase):
    def test_no_snapshot_is_none(self):
        self.assertEqual(panes.roster_state(None, 100.0), ("none", None))
        self.assertEqual(panes.roster_state([], 100.0), ("none", None))

    def test_fresh_and_stale(self):
        self.assertEqual(panes.roster_state({"ts": 99.0}, 100.0)[0], "live")
        state, age = panes.roster_state({"ts": 10.0}, 1000.0)
        self.assertEqual(state, "stale")
        self.assertAlmostEqual(age, 990.0)

    def test_an_unstamped_snapshot_reads_as_live(self):
        # The bridge always stamps one; inventing staleness from a missing
        # field would libel a bridge that is working.
        self.assertEqual(panes.roster_state({"viewers": []}, 100.0)[0], "live")

    def test_age_labels_are_coarse(self):
        self.assertEqual(panes.age_label(0), "just now")
        self.assertEqual(panes.age_label(14), "just now")
        self.assertEqual(panes.age_label(34), "30s")
        self.assertEqual(panes.age_label(200), "3m")
        self.assertEqual(panes.age_label(None), "just now")


class TestRosterPane(unittest.TestCase):
    """Who is here now, and only them."""

    def setUp(self):
        self.screen = make_screen(1920, 1080)

    def test_present_viewers_are_listed_alphabetically_with_the_count(self):
        doc = document(self.screen, [], [viewer("Zed"), viewer("Ada"),
                                         viewer("Bo")])
        self.assertIn("HERE NOW", doc)
        self.assertLess(doc.index("Ada"), doc.index("Bo"))
        self.assertLess(doc.index("Bo"), doc.index("Zed"))
        for name in ("Ada", "Bo", "Zed"):
            self.assertEqual(doc.count(name), 1, "%s drawn twice" % name)
        # The pane head's note is the count, so the pane cannot disagree with
        # itself about how many people are on the panel.
        self.assertIn(">3<", doc)

    def test_a_departed_viewer_leaves_the_pane(self):
        here = document(self.screen, [], [viewer("Ada"), viewer("Bo")])
        gone = document(self.screen, [], [viewer("Ada")])
        self.assertIn("Bo", here)
        self.assertNotIn("Bo", gone)
        self.assertIn(">1<", gone)

    def test_a_missing_roster_says_so_instead_of_an_empty_column(self):
        doc = document(self.screen, [dict(MSG)], [])
        self.assertNotIn("%d HERE" % 0, doc)
        self.assertIn(fit.NO_ROSTER, doc)
        self.assertIn("NO ROSTER", doc)

    def test_a_stale_roster_is_named_as_stale(self):
        doc = document(self.screen, [dict(MSG)], [viewer("Ada")],
                       state="stale", age=900.0)
        self.assertIn("ROSTER STALE", doc)
        self.assertIn("15m", doc)

    def test_a_long_roster_overflows_with_a_count(self):
        many = [viewer("Viewer %02d" % i) for i in range(40)]
        doc = document(self.screen, [], many)
        self.assertIn("+%d more" % (40 - doc.count("Viewer")), doc)
        self.assertLess(doc.count("Viewer"), 40)

    def test_a_viewer_without_a_display_name_falls_back_to_the_username(self):
        doc = document(self.screen, [], [{"id": "1", "username": "quietguy"}])
        self.assertIn("quietguy", doc)

    def test_a_viewer_name_cannot_add_markup(self):
        doc = document(self.screen, [], [viewer("<img src=x onerror=alert(1)>")])
        self.assertNotIn("<img", doc)
        self.assertIn("&lt;img", doc)


# --------------------------------------------------------------- join events


class TestJoinEvents(unittest.TestCase):
    """Join events ride the chat feed and are drawn as their own line."""

    def setUp(self):
        self.screen = make_screen(1920, 1080)

    def join(self, name="Newbie"):
        return {"id": "join:%s:1" % name.lower(), "author": name.lower(),
                "display_name": name, "text": "", "join": True,
                "timestamp": 1789801169829}

    def test_a_join_is_drawn_as_its_own_line_beside_the_messages(self):
        doc = document(self.screen, [dict(MSG), self.join()], [viewer("Ada")])
        self.assertIn("Newbie joined the chat", doc)
        self.assertIn("hello wall", doc)
        # Distinguishable from a message: the join line is drawn in the join
        # colour, not the author's.
        self.assertIn(templates.hex_colour(chat.JOIN), doc)
        self.assertIn(templates.hex_colour(chat.AUTHOR), doc)

    def test_events_stay_in_arrival_order(self):
        first = dict(MSG, id="a", text="first")
        last = dict(MSG, id="b", text="last")
        doc = document(self.screen, [first, self.join(), last])
        self.assertLess(doc.index("first"), doc.index("Newbie joined"))
        self.assertLess(doc.index("Newbie joined"), doc.index("last"))

    @native_built
    def test_the_frame_changes_when_somebody_arrives(self):
        events = [dict(MSG)]
        before = chat._draw(self.screen, "CHAT", events, 7, (10, 10, 14),
                            [viewer("Ada")])
        after = chat._draw(self.screen, "CHAT", events + [self.join()], 7,
                           (10, 10, 14), [viewer("Ada"), viewer("Newbie")])
        self.assertNotEqual(before.tobytes(), after.tobytes(),
                            "the panel did not change when a viewer joined")

    def test_a_join_is_retained_in_state_like_a_message(self):
        # Retention applies to join events exactly as it does to messages:
        # state never ages anything out, only the visible window is bounded.
        events = [self.join()] + [dict(MSG, id="m%d" % i) for i in range(80)]
        screen = feed_screen(events)
        snap = chat._snapshot(screen)
        self.assertEqual(len(snap), 81)
        self.assertTrue(snap[0]["join"])
        # And a join inside the window is drawn, however old the chat is.
        doc = document(self.screen, snap[-6:] + [self.join("Later")],
                       [viewer("Ada")], max_lines=7)
        self.assertIn("Later joined the chat", doc)


# -------------------------------------------------------------- the empty panel


class TestEmptyPanelStillSaysItIsWaiting(unittest.TestCase):
    """project-a4t.8.1, pinned: the healthy idle state says so."""

    def setUp(self):
        self.screen = make_screen(1920, 1080)

    def test_cold_panel_has_no_panes_and_names_the_waiting_state(self):
        doc = document(self.screen, [], [])
        self.assertIn("waiting for chat", doc)
        self.assertEqual(panes.panes([], [], panes.layout(1920, 1080),
                                     chat._ink(chat.TEXT), fit.WAITING, 7), "")
        # No pane layer at all: an empty roster column around the waiting
        # line is the worse panel this replaces.
        self.assertIn('<div class="layer"></div>', doc)
        self.assertNotIn("HERE NOW", doc)
        self.assertNotIn("in view", doc)

    def test_roster_alone_does_not_drop_the_waiting_line_from_the_feed(self):
        doc = document(self.screen, [], [viewer("Ada")])
        self.assertIn("HERE NOW", doc)
        self.assertIn("waiting for chat", doc)

    @native_built
    def test_the_empty_panel_is_a_frame_not_an_error_card(self):
        frame = chat._draw(self.screen, "CHAT", [], 7, (10, 10, 14))
        self.assertEqual(frame.size, (1920, 1080))
        self.assertFalse(is_error_card(frame), "empty panel drew an error")
        self.assertGreater(lit_pixels(frame), 20)


# ------------------------------------------------------------------ retention


class TestRetention(unittest.TestCase):
    """project-a4t.8: only a moderation delete removes anything."""

    def setUp(self):
        self.screen = make_screen(1920, 1080)

    def test_a_long_burst_is_all_still_in_state(self):
        events = [dict(MSG, id="m%d" % i, text="line %d" % i)
                  for i in range(120)]
        self.assertEqual(len(chat._snapshot(feed_screen(events))), 120)

    def test_the_visible_window_is_bounded_but_keeps_the_newest(self):
        events = [dict(MSG, id="m%d" % i, text="long message %d " % i * 12)
                  for i in range(12)]
        doc = document(self.screen, events, [viewer("Ada")], max_lines=7)
        self.assertIn("long message 11", doc, "the newest message is off screen")
        self.assertNotIn("long message 0", doc, "the window grew unbounded")

    def test_a_moderation_delete_is_the_only_removal(self):
        screen = feed_screen([dict(MSG, id="m1"), dict(MSG, id="m2")])
        self.assertEqual([m["id"] for m in chat._snapshot(screen)],
                         ["m1", "m2"])
        screen.feeds.push("chat", "delete", {"messageId": "m1"},
                          chat.INPUTS["delete"])
        self.assertEqual([m["id"] for m in chat._snapshot(screen)], ["m2"])

    def test_state_and_screen_are_not_the_same_question(self):
        events = [dict(MSG, id="m%d" % i) for i in range(120)]
        snap = chat._snapshot(feed_screen(events))
        document(self.screen, snap, [viewer("Ada")], max_lines=7)
        self.assertEqual(len(snap), 120, "drawing aged the state out")


# ----------------------------------------------------------------- the bridge


class FakeFirebotApi(BaseHTTPRequestHandler):
    """Firebot's viewer API: the export payload, and a request counter."""

    payload = []
    codes = []
    paths = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeFirebotApi.paths.append(self.path)
        code = FakeFirebotApi.codes.pop(0) if FakeFirebotApi.codes else 200
        if code != 200:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(FakeFirebotApi.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestRosterPoll(unittest.TestCase):
    """One fetch per cycle, shared by the diff and the pane."""

    def setUp(self):
        FakeFirebotApi.paths = []
        FakeFirebotApi.codes = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeFirebotApi)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = "http://127.0.0.1:%d/api/v1/viewers/export" \
            % self.httpd.server_address[1]
        self.posts = []

    def push(self, name, payload):
        self.posts.append((name, payload))

    def presence(self):
        return roster.Presence(roster.Roster(self.url), self.push)

    def export(self, *names, online=True):
        return [{"_id": name.lower(), "username": name.lower(),
                 "displayName": name, "online": online} for name in names]

    def test_the_first_poll_is_a_baseline_and_announces_nobody(self):
        FakeFirebotApi.payload = self.export("Ada", "Bo")
        self.assertEqual(self.presence().poll_once(), 0)
        self.assertEqual([name for name, _ in self.posts], ["roster"])
        self.assertEqual(self.posts[0][1]["count"], 2)

    def test_an_arrival_rides_the_chat_feed_and_the_pane_in_one_read(self):
        FakeFirebotApi.payload = self.export("Ada")
        presence = self.presence()
        presence.poll_once()
        self.posts[:] = []
        FakeFirebotApi.paths[:] = []
        FakeFirebotApi.payload = self.export("Ada", "Newbie")
        self.assertEqual(presence.poll_once(), 1)
        self.assertEqual(len(FakeFirebotApi.paths), 1,
                         "the roster was fetched more than once in a cycle")
        kinds = [name for name, _ in self.posts]
        self.assertEqual(kinds, ["roster", "message"])
        self.assertTrue(self.posts[1][1]["join"])
        self.assertEqual(self.posts[1][1]["display_name"], "Newbie")
        self.assertEqual(self.posts[0][1]["count"], 2)
        self.assertEqual([v["display_name"] for v in self.posts[0][1]["viewers"]],
                         ["Ada", "Newbie"])

    def test_a_departure_is_not_announced(self):
        # Deliberate: the pane drops the name, and the request named joins.
        FakeFirebotApi.payload = self.export("Ada", "Bo")
        presence = self.presence()
        presence.poll_once()
        self.posts[:] = []
        FakeFirebotApi.payload = self.export("Ada")
        self.assertEqual(presence.poll_once(), 0)
        self.assertEqual([name for name, _ in self.posts], ["roster"])

    def test_a_failed_read_pushes_nothing_and_keeps_the_last_roster(self):
        FakeFirebotApi.payload = self.export("Ada")
        presence = self.presence()
        presence.poll_once()
        self.posts[:] = []
        FakeFirebotApi.codes = [500]
        self.assertEqual(presence.poll_once(), 0)
        self.assertEqual(self.posts, [], "a failed read still pushed")
        # Recovery must not re-announce everybody who never left.
        FakeFirebotApi.payload = self.export("Ada", "Newbie")
        self.assertEqual(presence.poll_once(), 1)
        self.assertEqual(self.posts[1][1]["display_name"], "Newbie")

    def test_only_online_viewers_are_roster_and_only_the_new_one_joins(self):
        FakeFirebotApi.payload = self.export("Ada") + \
            self.export("Ghost", online=False)
        presence = self.presence()
        presence.poll_once()
        self.assertEqual([v["display_name"]
                          for v in self.posts[0][1]["viewers"]], ["Ada"])

    def test_the_export_shape_is_read_defensively(self):
        # Not the export's shape at all: nothing raised, nobody invented.
        self.assertEqual(roster.parse_viewers(None), [])
        self.assertEqual(roster.parse_viewers({"viewers": []}), [])
        self.assertEqual(roster.parse_viewers([{"online": True}]), [])
        self.assertEqual(roster.parse_viewers(["nope"]), [])
        self.assertEqual(len(roster.parse_viewers(self.export("Ada"))), 1)

    def test_a_broken_poller_never_kills_the_loop(self):
        # A programming error is logged and the loop keeps its cadence; the
        # socket half of the bridge must never be taken down by presence.
        class Broken:
            def poll(self):
                raise RuntimeError("kaboom")

        stop = threading.Event()
        presence = roster.Presence(Broken(), self.push, interval=0.01)
        thread = threading.Thread(target=presence.forever, args=(stop.is_set,))
        thread.start()
        time.sleep(0.1)
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive(), "the presence loop would not stop")

    def test_the_default_url_is_firebots_own_viewer_endpoint(self):
        import firebot_chat
        bridge = firebot_chat.Bridge("127.0.0.1", 7472, "http://d:8980")
        self.assertEqual(bridge.roster.url,
                         "http://127.0.0.1:7472" + roster.VIEWERS_PATH)
        self.assertEqual(bridge.presence.roster.url, bridge.roster.url)


class TestRosterInput(unittest.TestCase):
    """What chat.run does with the pushed roster."""

    def screen_with(self, viewers):
        screen = make_screen(960, 540)
        store = displayd.FeedStore()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        store.declare("chat", "roster", found["chat"]["inputs"]["roster"])
        store.push("chat", "roster",
                   roster.roster_payload(viewers), found["chat"]["inputs"]["roster"])
        screen.feeds = store
        return screen

    def test_the_latest_snapshot_wins(self):
        screen = self.screen_with([viewer("Ada")])
        screen.feeds.push("chat", "roster",
                          roster.roster_payload([viewer("Ada"), viewer("Bo")]),
                          chat.INPUTS["roster"])
        self.assertEqual(len(chat._roster(screen)[0]), 2)

    def test_a_junk_snapshot_is_not_drawn_as_an_empty_room(self):
        # The feed door refuses this shape (a schema test), but a payload
        # that arrived odd must still degrade to "no roster", not to a pane
        # claiming nobody is here.
        screen = StubScreen([{"viewers": "nope"}])
        self.assertEqual(chat._roster(screen), ([], {"viewers": "nope"}))
        screen = StubScreen([{"count": 3}, "not even a dict", None])
        self.assertEqual(chat._roster(screen), ([], None))

    def test_no_snapshot_at_all_is_the_none_state(self):
        self.assertEqual(chat._roster(make_screen(960, 540)), ([], None))
        self.assertEqual(chat._roster(StubScreen([])), ([], None))

    def test_a_pushed_message_cannot_add_markup_or_style(self):
        nasty = dict(MSG, display_name="<b>x</b>",
                     text="</div><script>alert(1)</script>",
                     color="red; background: url(evil)")
        doc = document(make_screen(1920, 1080), [nasty], [viewer("Ada")])
        self.assertNotIn("<script", doc)
        self.assertNotIn("</div><script", doc)
        self.assertNotIn("evil", doc)
        self.assertIn("&lt;script", doc)


@native_built
class TestPanelRenders(unittest.TestCase):
    """A real 1920x1080 render, the way the panel gets one."""

    def test_the_persistent_badges_never_land_on_the_panel(self):
        # The home and sleep badges are composited onto EVERY presented frame
        # in the top-left and top-right 160px squares. A title under a badge
        # is a title nobody can read, so the head band and both panes stay
        # clear of them -- the first live render taught this, and here it is
        # pinned. (The layer's margin and the template's padding are the same
        # number, which is what makes the head band part of the same rule.)
        import home_chrome
        import sleep_chrome

        badges = [home_chrome.home_rect(1920, 1080),
                  sleep_chrome.sleep_rect(1920, 1080)]
        lay = panes.layout(1920, 1080)
        self.assertIn("padding: 30px %dpx 0 %dpx;" % (panes.PAD, panes.PAD),
                      source("html-templates/chat.html"))
        for name in ("board", "feed"):
            px, py, pw, ph = lay[name]
            for bx, by, bw, bh in badges:
                with self.subTest(pane=name, badge=(bx, by)):
                    self.assertFalse(px < bx + bw and bx < px + pw
                                     and py < by + bh and by < py + ph,
                                     "%s pane overlaps the badge at %d,%d"
                                     % (name, bx, by))
        self.assertGreaterEqual(lay["board"][0], home_chrome.HOME_STRIP)

    def test_the_two_panes_are_where_the_layer_says_they_are(self):
        screen = make_screen(1920, 1080)
        lay = panes.layout(1920, 1080)
        frame = chat._draw(screen, "CHAT", [dict(MSG)], 7, (10, 10, 14),
                           [viewer("Ada")])
        self.assertFalse(is_error_card(frame))
        board_x, board_y, board_w, _bh = lay["board"]
        feed_x, _fy, _fw, _fh = lay["feed"]
        pane_bg = (17, 20, 27)
        self.assertEqual(frame.getpixel((board_x + 8, board_y + 8)), pane_bg)
        self.assertEqual(frame.getpixel((feed_x + 8, board_y + 8)), pane_bg)
        # Between the two panes is the panel's own background: two panes, not
        # one wide one.
        self.assertGreater(feed_x - (board_x + board_w), 10)
        self.assertEqual(frame.getpixel(((board_x + board_w + feed_x) // 2,
                                        board_y + 8)), (10, 10, 14))

    def test_no_row_is_placed_past_its_pane(self):
        # The layer's own budget, checked against the pane it draws into: a
        # row past the bottom would paint over the footer band.
        lay = panes.layout(1920, 1080)
        board = fit.roster_rows([viewer("Viewer %02d" % i) for i in range(40)],
                                lay["board"], lay)
        self.assertLessEqual(lay["ph_h"] + len(board) * lay["row_h"],
                             lay["board"][3])
        events = [dict(MSG, id="m%d" % i, text="long text " * 30)
                  for i in range(12)]
        lines = fit.feed_lines(events, lay["feed"], lay, 7, chat._ink(chat.TEXT))
        self.assertLessEqual(lay["ph_h"] + len(lines) * lay["line_h"],
                             lay["feed"][3])
        self.assertIn("long text", "".join(run[1] for line in lines
                                            for run in line["runs"]))

    def test_nothing_is_painted_below_the_panes(self):
        screen = make_screen(1920, 1080)
        lay = panes.layout(1920, 1080)
        frame = chat._draw(
            screen, "CHAT",
            [dict(MSG, id="m%d" % i, text="long text " * 30) for i in range(12)],
            7, (10, 10, 14), [viewer("Viewer %02d" % i) for i in range(40)])
        bottom = lay["board"][1] + lay["board"][3]
        for y in range(bottom + 1, bottom + 8):
            for x in (200, 700, 1000, 1850):
                self.assertEqual(frame.getpixel((x, y)), (10, 10, 14),
                                 "a row painted past its pane at %d,%d"
                                 % (x, y))

    def test_a_smaller_panel_still_draws_both_panes(self):
        frame = chat._draw(make_screen(960, 540), "CHAT", [dict(MSG)], 7,
                           (10, 10, 14), [viewer("Ada")])
        self.assertEqual(frame.size, (960, 540))
        self.assertFalse(is_error_card(frame))
        self.assertGreater(lit_pixels(frame), 20)

    def test_a_degenerate_panel_draws_no_rows_rather_than_past_them(self):
        # chat_panes/chat_fit are total on any size, and the fallback must be
        # "nothing fits" rather than a row painted over the footer band.
        for w, h in ((320, 180), (200, 120), (1, 1), (1920, 40)):
            with self.subTest(size=(w, h)):
                lay = panes.layout(w, h)
                self.assertEqual(fit.roster_rows([viewer("Ada")] * 40,
                                                 lay["board"], lay), [])
                self.assertEqual(
                    fit.feed_lines([dict(MSG)], lay["feed"], lay, 7,
                                   chat._ink(chat.TEXT)), [])
                self.assertEqual(chat._draw(make_screen(w, h), "CHAT",
                                            [dict(MSG)], 7, (10, 10, 14),
                                            [viewer("Ada")]).size, (w, h))

    def test_a_pane_with_room_for_one_line_draws_exactly_that(self):
        # 1920x290 leaves the feed pane one line of room; a wrapped message
        # must be truncated to it rather than overrun the pane.
        lay = panes.layout(1920, 290)
        lines = fit.feed_lines([dict(MSG, text="long message " * 20)],
                               lay["feed"], lay, 7, chat._ink(chat.TEXT))
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(lay["ph_h"] + len(lines) * lay["line_h"],
                             lay["feed"][3])


if __name__ == "__main__":
    unittest.main()
