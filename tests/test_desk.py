"""Tests for the desk itself: matcher, verdicts, calendar, costs, levels.

Every bug found while building this system was in a small pure function that
failed silently: a reversed tuple unpack, a regex gap that excluded '.', a
column index level dropped the wrong way. None of them raised. All of them
would have been caught here in under a second.

    python3 -m unittest discover -s tests -p 'test_desk*.py' -v
"""
import pathlib
import sys
import unittest

from _load import ROOT

sys.path.insert(0, str(ROOT / "tools"))

import alert_monitor
import bulletin_render
import market_calendar as cal
import recs
import subscribers


def setUpModule():
    """Point the subscriber store at a scratch file for the whole module.

    Reading is harmless; writing is not. A test that stubs load() but not
    save() silently replaced the real subscribers.json with its fixture users
    and the bot went from 2 people to 6. The store path is patched here so no
    test can reach the live address book by accident.
    """
    import tempfile
    global _ORIG_STORE
    _ORIG_STORE = subscribers.STORE
    subscribers.STORE = pathlib.Path(tempfile.mkdtemp()) / "subscribers.json"


def tearDownModule():
    subscribers.STORE = _ORIG_STORE


class TestHeadlineMatcher(unittest.TestCase):
    """A headline must resolve to the company it is about, or to nothing."""

    def test_resolves_common_names(self):
        cases = {
            "TCS Q2 net profit rises 8%, beats estimates": "TCS",
            "Tata Steel wins order worth Rs 1,200 crore": "TATASTEEL",
            "HDFC Bank raises Rs 10,000 crore via QIP": "HDFCBANK",
            "Axis Bank Q2 net profit beats estimates": "AXISBANK",
            "Coal India Q2 profit falls on lower offtake": "COALINDIA",
            "Vodafone Idea board approves fund raise": "IDEA",
        }
        for headline, want in cases.items():
            hit = alert_monitor.match_symbol(headline)
            self.assertIsNotNone(hit, f"no match: {headline}")
            self.assertEqual(hit[0], want, headline)

    def test_alias_beats_a_partial_name(self):
        # 'Mumbai metro' used to resolve to Metro Brands because 'metro' was a
        # key with the same weight as the L&T alias.
        hit = alert_monitor.match_symbol("L&T secures mega order for Mumbai metro")
        self.assertEqual(hit[0], "LT")

    def test_ordinary_words_are_not_companies(self):
        for headline in (
            "Idea of a rate cut lifts sentiment on Dalal Street",
            "Idea of a rate cut grows as Q2 results beat estimates across banks",
            "West Bengal government announces new industrial policy",
            "Metro services resume in Mumbai after heavy rain",
            "Power sector Q2 results: profit jumps for state utilities",
        ):
            self.assertIsNone(alert_monitor.match_symbol(headline), headline)


class TestEventClassifier(unittest.TestCase):
    def test_order_wins_with_an_amount_in_between(self):
        # The old gap class excluded '.' and ',', so these never matched.
        for headline in (
            "Infosys bags $1.5 billion deal from European client",
            "Suzlon wins 500 MW order from NTPC",
            "Reliance Power wins 1,200 MW contract",
        ):
            kind = alert_monitor.classify(headline)
            self.assertIsNotNone(kind, headline)
            self.assertEqual(kind[0], "EVENT")
            self.assertEqual(kind[1], "order win")

    def test_macro_lines_are_not_stock_events(self):
        for headline in (
            "Nifty ends flat as IT stocks drag",
            "India's GDP growth beats estimates at 7.2%",
            "Rupee ends 12 paise weaker against the US dollar",
        ):
            self.assertIsNone(alert_monitor.classify(headline), headline)


class TestVerdicts(unittest.TestCase):
    def test_buy_ready_needs_a_tight_base(self):
        plan = {"stage2": True, "from_pivot_pct": -1.0, "tt_passed": 6}
        self.assertEqual(
            bulletin_render.plan_verdict({**plan, "base_grade": "A"})[0], "BUY-READY")
        self.assertEqual(
            bulletin_render.plan_verdict({**plan, "base_grade": "B"})[0], "BUY-READY")
        # A textbook trend inside a loose base is the classic failed breakout.
        self.assertIn("WATCH",
                      bulletin_render.plan_verdict({**plan, "base_grade": "C"})[0])

    def test_missed_when_above_the_chase_limit(self):
        plan = {"stage2": True, "from_pivot_pct": 3.0, "base_grade": "A"}
        self.assertIn("MISSED", bulletin_render.plan_verdict(plan)[0])

    def test_results_note_is_loud_only_inside_the_window(self):
        far = bulletin_render.results_note(
            {"results_date": "2027-01-25", "results_sessions": 50,
             "results_source": "estimated"})
        near = bulletin_render.results_note(
            {"results_date": "2026-11-18", "results_sessions": 3,
             "results_source": "confirmed"})
        self.assertNotIn("⚠️", far)
        self.assertIn("⚠️", near)
        self.assertEqual(bulletin_render.results_note({"results_date": None}), "")


class TestCalendar(unittest.TestCase):
    def test_pre_open_uses_yesterdays_bar(self):
        from datetime import date, datetime
        self.assertEqual(
            cal.last_session_bar(date(2026, 11, 12), datetime(2026, 11, 12, 9, 0)),
            date(2026, 11, 11))

    def test_after_close_uses_todays_bar(self):
        from datetime import date, datetime
        self.assertEqual(
            cal.last_session_bar(date(2026, 11, 12), datetime(2026, 11, 12, 16, 0)),
            date(2026, 11, 12))

    def test_weekend_falls_back_to_friday(self):
        from datetime import date
        self.assertEqual(cal.last_session_bar(date(2026, 11, 14), None),
                         cal.previous_trading_day(date(2026, 11, 14)))


class TestPlanTiming(unittest.TestCase):
    def test_after_close_plan_is_for_the_next_session(self):
        # A 16:00 plan graded against its own bar is look-ahead.
        self.assertEqual(recs.live_from("2026-11-12T16:05:00"), "2026-11-13")
        # A plan published before the close can fill that same day.
        self.assertEqual(recs.live_from("2026-11-12T09:00:00"), "2026-11-12")


class TestCosts(unittest.TestCase):
    def test_costs_reduce_r_but_do_not_invent_it(self):
        rec = {"status": "stopped", "r_realized": -1.0, "fill_price": 100.0,
               "stop": 95.0, "exit_price": 95.0, "qty": 200}
        net = recs.net_r(rec)
        self.assertIsNotNone(net)
        self.assertLess(net, -1.0)          # a loss plus costs is a bigger loss
        self.assertGreater(net, -1.5)

    def test_no_exit_price_means_no_net_r(self):
        rec = {"status": "filled", "r_realized": None, "fill_price": 100.0,
               "stop": 95.0, "exit_price": None, "qty": 10}
        self.assertIsNone(recs.net_r(rec))


class TestLevelWatch(unittest.TestCase):
    def _rec(self, **kw):
        base = {"symbol": "X", "status": "filled", "entry": 100.0,
                "fill_price": 100.0, "stop": 95.0, "t1": 110.0, "t2": 120.0}
        base.update(kw)
        return base

    def test_stop_breaks_first(self):
        import levels_watch
        self.assertEqual(levels_watch._candidate(self._rec(), 94.0)[0], "stop")

    def test_targets_are_reported_in_order(self):
        import levels_watch
        self.assertEqual(levels_watch._candidate(self._rec(), 111.0)[0], "t1")
        self.assertEqual(levels_watch._candidate(self._rec(), 121.0)[0], "t2")

    def test_no_alert_between_levels(self):
        import levels_watch
        self.assertIsNone(levels_watch._candidate(self._rec(), 103.0))

    def test_an_unfilled_plan_has_no_stop(self):
        # Nothing is at risk before the trigger, so a stop breach is noise.
        import levels_watch
        rec = self._rec(status="pending_fill", entry=100.0, fill_price=None)
        self.assertIsNone(levels_watch._candidate(rec, 94.0))
        self.assertEqual(levels_watch._candidate(rec, 101.0)[0], "entry")


class TestIntegrity(unittest.TestCase):
    """A false price jump silently corrupts every indicator downstream.

    These are the three shapes found in the live cache. Getting the
    classification wrong is expensive in both directions: deleting a real
    move rewrites history, and reading through a break invents a pivot.
    """

    def _series(self, closes, volumes=None):
        n = len(closes)
        vols = volumes or [1000] * n
        return [{"Date": f"2026-01-{i + 1:02d}", "Open": c, "High": c,
                 "Low": c, "Close": c, "Volume": v}
                for i, (c, v) in enumerate(zip(closes, vols))]

    def test_single_bad_print_is_spurious(self):
        import integrity
        rs = self._series([100, 101, 20505, 102, 101, 100, 99])
        hits = integrity.detect(rs)
        self.assertEqual(len(hits), 2)          # up to it, and back down
        self.assertTrue(all(h["kind"] == "spurious" for h in hits))

    def test_a_new_price_level_is_a_break(self):
        import integrity
        rs = self._series([700, 690, 200, 198, 196, 195, 194])
        hits = integrity.detect(rs)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "break")

    def test_ordinary_moves_are_ignored(self):
        import integrity
        rs = self._series([100, 96, 103, 99, 101, 97, 102])
        self.assertEqual(integrity.detect(rs), [])

    def test_rescale_moves_price_and_volume_inversely(self):
        import integrity
        rs = self._series([500, 505, 510, 102, 103, 104])
        integrity.rescale(rs, 3, 5.0)          # 1:5 split at index 3
        self.assertAlmostEqual(rs[0]["Close"], 100.0)
        self.assertAlmostEqual(rs[0]["Volume"], 5000.0)
        self.assertAlmostEqual(rs[3]["Close"], 102.0)   # post-split bar untouched
        # and the seam is now continuous instead of a 80% cliff
        self.assertLess(abs(rs[3]["Close"] / rs[2]["Close"] - 1), 0.05)


class TestPortfolioHeat(unittest.TestCase):
    """Per-trade sizing cannot see the total. Heat is the total."""

    def _rows(self):
        return [
            {"symbol": "A", "kind": "open", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "Energy"},
            {"symbol": "B", "kind": "open", "qty": 100, "entry": 200.0,
             "stop": 190.0, "risk": 1000.0, "sector": "Energy"},
            {"symbol": "C", "kind": "pending", "qty": 50, "entry": 300.0,
             "stop": 285.0, "risk": 750.0, "sector": "IT"},
        ]

    def test_heat_sums_only_open_and_commits_the_rest(self):
        import heat
        orig_p, orig_s = heat.positions, heat._sector_map
        heat.positions = lambda: self._rows()
        heat._sector_map = lambda: {}
        try:
            h = heat.compute(capital=100_000, risk_pct=1.0, cap_pct=6.0)
        finally:
            heat.positions, heat._sector_map = orig_p, orig_s
        self.assertEqual(h["at_risk"], 1500)        # open only
        self.assertEqual(h["committed"], 750)
        self.assertAlmostEqual(h["at_risk_pct"], 1.5)
        self.assertEqual(h["status"], "ok")
        # 6% cap = 6000; 1500 used; 4500 / 1000 per trade = 4 slots
        self.assertEqual(h["slots"], 4)

    def test_a_full_book_reports_hot(self):
        import heat
        rows = self._rows()
        rows[0]["risk"] = 5_900.0
        orig_p, orig_s = heat.positions, heat._sector_map
        heat.positions = lambda: rows
        heat._sector_map = lambda: {}
        try:
            h = heat.compute(capital=100_000, risk_pct=1.0, cap_pct=6.0)
        finally:
            heat.positions, heat._sector_map = orig_p, orig_s
        self.assertEqual(h["status"], "hot")
        self.assertEqual(h["slots"], 0)

    def test_two_names_in_one_sector_is_a_cluster(self):
        import heat
        rows = self._rows() + [
            {"symbol": "D", "kind": "open", "qty": 10, "entry": 100.0,
             "stop": 90.0, "risk": 100.0, "sector": "Energy"}]
        orig_p = heat.positions
        heat.positions = lambda: rows
        try:
            h = heat.compute(capital=100_000)
        finally:
            heat.positions = orig_p
        self.assertTrue(any(c["sector"] == "Energy" and c["count"] == 3
                            for c in h["clusters"]))


class TestTradability(unittest.TestCase):
    """A setup you cannot fill is not a plan."""

    def _lookup(self, securities, banned=()):
        import tradability
        orig = tradability.store
        tradability.store = lambda force=False: {
            "at": "2026-11-12T09:00:00", "securities": securities,
            "fo_ban": list(banned)}
        try:
            return tradability.lookup(["X", "Y", "Z"])
        finally:
            tradability.store = orig

    def test_a_two_percent_band_blocks_the_plan(self):
        rows = self._lookup({"X": {"series": "EQ", "name": "X",
                                   "band_pct": 2, "remarks": "-"}})
        self.assertTrue(rows["X"]["blockers"])

    def test_a_twenty_percent_band_is_clear(self):
        rows = self._lookup({"Y": {"series": "EQ", "name": "Y",
                                   "band_pct": 20, "remarks": "-"}})
        self.assertFalse(rows["Y"]["blockers"])
        self.assertEqual(rows["Y"]["notes"], [])

    def test_trade_for_trade_and_ban_are_flagged_not_blocked(self):
        rows = self._lookup({"X": {"series": "BE", "name": "X",
                                   "band_pct": 20, "remarks": "-"}},
                            banned=["X"])
        self.assertFalse(rows["X"]["blockers"])
        self.assertTrue(any("trade-for-trade" in n for n in rows["X"]["notes"]))
        self.assertTrue(any("ban" in n for n in rows["X"]["notes"]))

    def test_an_unknown_symbol_is_not_called_safe(self):
        rows = self._lookup({})
        self.assertFalse(rows["Z"]["known"])
        self.assertTrue(rows["Z"]["notes"])

    def test_blocked_beats_a_perfect_chart(self):
        verdict = bulletin_render.plan_verdict(
            {"stage2": True, "from_pivot_pct": -1.0, "base_grade": "A",
             "blockers": ["2% price band"]})
        self.assertIn("NOT TRADABLE", verdict[0])


class TestAlertOutcomes(unittest.TestCase):
    def test_alerts_are_judged_by_what_happened_next(self):
        import alert_outcomes as ao
        rows = [
            {"symbol": "A", "event": "order win", "t1": 1.0, "t5": 3.0},
            {"symbol": "B", "event": "order win", "t1": 0.5, "t5": 2.0},
            {"symbol": "C", "event": "analyst action", "t1": -0.2, "t5": -0.3},
            {"symbol": "D", "event": "analyst action", "t1": -0.1, "t5": -0.4},
        ]
        res = ao.summarise(rows, min_n=2)
        self.assertEqual(res["buckets"][0]["event"], "order win")
        self.assertEqual(res["buckets"][0]["avg_t5"], 2.5)
        self.assertIn("analyst action", [b["event"] for b in res["weak"]])

    def test_a_recent_alert_is_not_scored_yet(self):
        res = __import__("alert_outcomes").summarise(
            [{"symbol": "A", "event": "x", "t1": 0.5, "t5": None}], min_n=1)
        self.assertEqual(res["scored"], 0)
        self.assertEqual(res["awaiting"], 1)


class TestSubscriberPreferences(unittest.TestCase):
    """Silence should be a setting, not an unsubscribe."""

    def _store(self):
        return {"version": 1, "users": {
            "1": {"name": "owner", "role": "owner", "active": True, "mode": "all"},
            "2": {"name": "quiet", "active": True, "mode": "quiet"},
            "3": {"name": "alerts", "active": True, "mode": "alerts"},
            "4": {"name": "off", "active": True, "mode": "none"},
            "5": {"name": "paused", "active": False, "mode": "all"},
            "6": {"name": "snoozed", "active": True, "mode": "all",
                  "snooze_until": "2999-01-01T00:00:00"},
        }}

    def test_each_mode_gets_only_what_it_asked_for(self):
        import subscribers as sub
        orig_load, orig_save = sub.load, sub.save
        import os
        orig_env = os.environ.pop("TELEGRAM_CHAT_ID", None)
        sub.load, sub.save = (lambda: self._store()), (lambda s: None)
        try:
            # quiet (2) still wants the plan and the review; a snoozed person
            # (6) keeps the plan too — only the alerts go quiet.
            day = ["1", "2", "6"]
            self.assertEqual(sub.audience("daily"), day)
            self.assertEqual(sub.audience("eod"), day)
            self.assertEqual(sub.audience("intraday"), ["1", "6"])
            # a snooze silences alerts but never the plan
            self.assertEqual(sub.audience("alert"), ["1", "3"])
            self.assertEqual(sub.audience("level"), ["1", "3"])
            self.assertNotIn("4", sub.audience("daily"))   # none means none
            self.assertNotIn("5", sub.audience("daily"))   # inactive is inactive
        finally:
            sub.load, sub.save = orig_load, orig_save
            if orig_env is not None:
                os.environ["TELEGRAM_CHAT_ID"] = orig_env

    def test_snooze_expires(self):
        import subscribers as sub
        orig_load, orig_save = sub.load, sub.save
        store = self._store()
        sub.load, sub.save = (lambda: store), (lambda s: None)
        try:
            self.assertTrue(sub.snoozed("6"))
            self.assertFalse(sub.snoozed("1"))
            self.assertFalse(sub.wants("6", "alert"))
            self.assertTrue(sub.wants("6", "daily"))
        finally:
            sub.load, sub.save = orig_load, orig_save

    def test_set_mode_rejects_nonsense(self):
        import subscribers as sub
        with self.assertRaises(ValueError):
            sub.set_mode("1", "loud")


class TestWatchlist(unittest.TestCase):
    """A watch is a promise about one level, kept once a day."""

    def _run(self):
        import subscribers as sub
        store = {"version": 1, "users": {
            "9": {"name": "me", "active": True, "watch": {}}}}
        orig_load, orig_save = sub.load, sub.save
        sub.load = lambda: store
        sub.save = lambda s: None
        return sub, store, (orig_load, orig_save)

    def test_add_read_remove(self):
        sub, store, orig = self._run()
        try:
            self.assertTrue(sub.watch("9", "centum", 4230.0))
            self.assertEqual(sub.watchlist("9")["CENTUM"]["pivot"], 4230.0)
            self.assertEqual(sub.watchlist("9")["CENTUM"]["kind"], "pivot")
            self.assertTrue(sub.unwatch("9", "CENTUM"))
            self.assertFalse(sub.unwatch("9", "CENTUM"))
        finally:
            sub.load, sub.save = orig

    def test_a_kind_outside_the_three_is_dropped(self):
        sub, store, orig = self._run()
        try:
            sub.watch("9", "X", 10.0, kind="whatever")
            self.assertEqual(sub.watchlist("9")["X"]["kind"], "pivot")
            sub.watch("9", "Y", 10.0, kind="above")
            self.assertEqual(sub.watchlist("9")["Y"]["kind"], "above")
        finally:
            sub.load, sub.save = orig

    def test_once_a_day_is_one_date_prefix(self):
        import watch_scan
        self.assertTrue(watch_scan._already_today(
            {"notified": watch_scan._today() + "T10:00:00"}))
        self.assertFalse(watch_scan._already_today(
            {"notified": "2020-01-01T10:00:00"}))
        self.assertFalse(watch_scan._already_today({}))

    def test_the_alert_says_what_the_level_was(self):
        import watch_scan
        msg = watch_scan._message("CENTUM", 4230.0, 4260.0, "pivot")
        self.assertIn("CENTUM", msg)
        self.assertIn("4,230.00", msg)
        self.assertIn("+0.7%", msg)
        self.assertIn("not advice", msg)
        # an unknown kind still reads as a cleared level, never a crash
        self.assertIn("cleared your level",
                      watch_scan._message("X", 1.0, 2.0, "weird"))


class TestButtonPayloads(unittest.TestCase):
    """A button that sends a verb the dispatcher does not know is a dead button."""

    def test_every_callback_fits_telegrams_limit(self):
        import buttons
        for kb in (buttons.alert("BAJAJ-AUTO"), buttons.levels("BAJAJ-AUTO"),
                   buttons.watch("BAJAJ-AUTO"), buttons.alert(None)):
            for row in kb["inline_keyboard"]:
                for b in row:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)
                    verb, arg = buttons.parse(b["callback_data"])
                    self.assertIn(verb, {"book", "heat", "check", "unwatch",
                                         "snooze", "status"})

    def test_parse_splits_on_the_first_colon_only(self):
        import buttons
        self.assertEqual(buttons.parse("check:M&M"), ("check", "M&M"))
        self.assertEqual(buttons.parse("book"), ("book", ""))
        self.assertEqual(buttons.parse(""), ("", ""))


class TestBotCards(unittest.TestCase):
    """The chat card and the report must agree, or people stop trusting both."""

    def test_check_card_shows_the_chain(self):
        import stock_check
        r = {
            "symbol": "CENTUM", "name": "CENTUM", "close": 4239.5,
            "verdict": "BUY-READY", "css": "ok",
            "stage2": True, "tt_passed": 6, "tt_total": 6, "tt_failed": [],
            "base": {"type": "VCP", "grade": "A", "weeks": 6.6, "depth": 26.6,
                     "pivot": 4230.0, "from_pivot_pct": 0.2},
            "gate": {"blockers": [], "notes": [], "band_pct": 20,
                     "series": "EQ"},
            "delivery": {"deliv_pct": 43, "multiple": None},
            "results": {},
            "held": {"status": "filled", "entry": 4238.5, "stop": 4008.4,
                     "t1": 5054.0},
            "plan": {"trigger": 4238.5, "limit": 4356.9, "stop": 4008.4,
                     "stop_method": "structure", "qty": 13,
                     "position_value": 56640, "allocation_pct": 11.3,
                     "warnings": ["stop is inside the base"],
                     "targets": [{"r": 2.0, "price": 5054.0, "pct": 16.0},
                                 {"r": 3.0, "price": 5402.5, "pct": 24.0}]},
        }
        card = stock_check.card(r)
        for must in ("CENTUM", "Stage 2", "VCP grade A", "Pivot", "Buy above",
                     "Stop", "T1", "2R", "BUY-READY", "not advice",
                     "Already on the book"):
            self.assertIn(must, card)

    def test_no_base_is_said_plainly_not_reported_as_a_grade(self):
        import stock_check
        card = stock_check.card({
            "symbol": "TCS", "name": "TCS", "close": 2442.4,
            "verdict": "WATCH ONLY", "css": "warn", "stage2": False,
            "tt_passed": 1, "tt_total": 5, "tt_failed": ["SMA200 rising"],
            "base": {"type": None, "grade": None, "note": "no base yet"},
            "gate": {"blockers": [], "notes": [], "series": "EQ"},
            "delivery": {}, "results": {}, "held": None, "plan": {},
        })
        self.assertIn("no tight base", card)
        self.assertIn("no base yet", card)
        self.assertNotIn("Buy above", card)

    def test_an_error_is_one_line_not_a_traceback(self):
        import stock_check
        self.assertIn("no data", stock_check.card(
            {"error": "no data for NOPE — check the NSE symbol"}))

    def test_book_and_heat_cards_survive_an_empty_book(self):
        import bot_listener as bot
        import recs
        orig_live, orig_load = bot._live, recs.load
        bot._live = lambda syms: {}
        recs.load = lambda: {"records": []}
        try:
            self.assertIn("Book is empty", bot.book_text())
        finally:
            bot._live, recs.load = orig_live, orig_load
        heat = bot.heat_text()
        self.assertIn("Portfolio heat", heat)
        self.assertIn("Cap", heat)


class TestDeliveryRouting(unittest.TestCase):
    """Who receives a message is decided in one place."""

    def test_kind_is_read_from_the_bulletin_mode(self):
        # 'daily' and 'eod' are promises; an intraday card is not.
        kinds = {"daily": "daily", "eod": "eod",
                 "intraday": "intraday", "": "intraday"}
        for mode, want in kinds.items():
            self.assertEqual({"daily": "daily", "eod": "eod"}.get(
                mode, "intraday"), want)

    def test_an_explicit_chat_id_bypasses_the_audience_filter(self):
        import notify
        sent = []
        orig_aud, orig_msg, orig_sleep = notify._audience, notify.send_message, notify.time.sleep
        notify._audience = lambda kind: ["1", "2"]
        notify.send_message = lambda tok, cid, text, **kw: sent.append(cid) or True
        notify.time.sleep = lambda s: None
        try:
            notify.token = lambda: "T"
            res = notify.send("hi", chat_id="7", kind="alert")
            self.assertEqual(sent, ["7"])
            self.assertEqual(res["sent"], 1)
            sent.clear()
            notify.send("hi", kind="alert")
            self.assertEqual(sent, ["1", "2"])
        finally:
            notify._audience, notify.send_message, notify.time.sleep = (
                orig_aud, orig_msg, orig_sleep)

    def test_a_keyboard_is_carried_through_to_the_api_call(self):
        import notify
        seen = {}
        orig_aud, orig_msg, orig_sleep = notify._audience, notify.send_message, notify.time.sleep
        notify._audience = lambda kind: ["1"]
        notify.time.sleep = lambda s: None

        def fake(tok, cid, text, **kw):
            seen.update(kw)
            return True
        notify.send_message = fake
        try:
            notify.token = lambda: "T"
            kb = {"inline_keyboard": [[{"text": "Book", "callback_data": "book"}]]}
            notify.send("hi", kind="alert", reply_markup=kb)
            self.assertEqual(seen.get("reply_markup"), kb)
        finally:
            notify._audience, notify.send_message, notify.time.sleep = (
                orig_aud, orig_msg, orig_sleep)


if __name__ == "__main__":
    unittest.main()
