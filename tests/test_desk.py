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
            "live": 4239.5, "verdict": "BUY-READY", "css": "good",
            "state": "at_pivot", "state_text": "At the pivot (4230.00)",
            "trend": {"stage2": True, "passed": 6, "total": 6, "failed": [],
                      "criteria": [], "rs_3m": 8.0},
            "base": {"type": "VCP", "grade": "A", "weeks": 6.6, "depth": 26.6,
                     "pivot": 4230.0, "from_pivot_pct": 0.2,
                     "cleared_on": "2026-11-02"},
            "tech": {"close": 4239.5, "change_pct": 1.9, "rsi": 54.8,
                     "atr_pct": 4.65, "adx": 16.0, "vol_x50": 0.6,
                     "avg_range_pct": 4.7, "position_52w_pct": 74.0,
                     "from_sma20_pct": 4.4, "from_sma50_pct": 0.7,
                     "from_sma200_pct": 25.7, "high_52w": 4999.5,
                     "low_52w": 2044.2, "distribution_pct": 30,
                     "measured_move_target": 5000.0,
                     "from_measured_move_pct": -17.0,
                     "days_since_breakout": 34,
                     "gain_since_breakout_pct": 5.0,
                     "breakout_date": "2026-09-22"},
            "gate": {"blockers": [], "notes": [], "band_pct": 20,
                     "series": "EQ"},
            "delivery": {"deliv_pct": 43, "multiple": None},
            "results": {}, "why": ["the base is tight"],
            "rates": {"market": {}, "symbol": {}, "matches": []},
            "held": {"status": "filled", "entry": 4238.5, "stop": 4008.4,
                     "t1": 5054.0},
            "plans": [{
                "kind": "breakout", "label": "Buy above the pivot",
                "entry": 4238.5, "entry_zone": [4238.5, 4356.9],
                "stop": 4008.4, "stop_pct": 5.4, "stop_pct_from_limit": 8.0,
                "stop_capped": True, "stop_quality": "capped inside the base",
                "qty": 13, "position_value": 56640, "allocation_pct": 11.3,
                "risk": 4667, "risk_budget": 5000,
                "targets": [{"r": 2.0, "price": 5054.0, "pct": 16.0},
                            {"r": 3.0, "price": 5402.5, "pct": 24.0}],
                "note": "a GTT fills only if the stock comes to it",
            }],
        }
        card = stock_check.card_call(r)
        for must in ("CENTUM", "Buy above", "Stop", "T1", "2R", "BUY-READY",
                     "not SEBI-registered advice", "Already on the book",
                     "At the pivot"):
            self.assertIn(must, card)
        detail = stock_check.card_detail(r)
        for must in ("RSI", "ADX", "ATR", "52-week", "Delivery",
                     "Stage 2", "VCP grade A", "Pivot"):
            self.assertIn(must, detail)
        # the base-rate block appears when the stock matches a signal
        r2 = dict(r)
        r2["rates"] = {"matches": ["up_day_volume"], "market": {}, "symbol": {}}
        self.assertIn("Base rates", stock_check.card_detail(r2))
        # the base-rate block appears when the stock matches a signal
        r2 = dict(r)
        r2["rates"] = {"matches": ["up_day_volume"], "market": {}, "symbol": {}}
        with_rates = stock_check.card_detail(r2)
        self.assertIn("Base rates", with_rates)

    def test_no_base_is_said_plainly_not_reported_as_a_grade(self):
        import stock_check
        r = {
            "symbol": "TCS", "name": "TCS", "close": 2442.4,
            "verdict": "WATCH ONLY", "css": "warn", "stage2": False,
            "state": "no_base", "state_text": "No base to trade",
            "trend": {"stage2": False, "passed": 1, "total": 5,
                      "failed": ["SMA200 rising"], "criteria": []},
            "base": {"type": None, "grade": None, "note": "no base yet"},
            "tech": {}, "why": [], "plans": [],
            "rates": {"market": {}, "symbol": {}, "matches": []},
            "gate": {"blockers": [], "notes": [], "series": "EQ"},
            "delivery": {}, "results": {}, "held": None,
        }
        card = stock_check.card_call(r)
        self.assertIn("no base yet", card)
        self.assertIn("No trade to take right now", card)
        self.assertNotIn("Buy above", card)
        detail = stock_check.card_detail(r)
        self.assertIn("no tight base", detail)

    def test_an_error_is_one_line_not_a_traceback(self):
        import stock_check
        self.assertIn("no data", stock_check.cards(
            {"error": "no data for NOPE — check the NSE symbol"})[0])

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


class TestReportSurvivesABrokenSection(unittest.TestCase):
    """A None in an f-string once cost the whole 09:00 plan.

    The renderer died, the job died with it, and nothing was sent. The levels
    are the part that cannot be optional, so the fallback carries them.
    """

    def _data(self):
        return {
            "mode": "daily", "generated_at": "2026-11-13T09:00:00",
            "index": {"close": 24500.0, "change_pct": 0.4},
            "regime": {"label": "Constructive", "risk_score": 40,
                       "exposure_pct": 80},
            "plans": [{
                "symbol": "CENTUM", "name": "Centum",
                "trigger": 4238.5, "limit": 4356.9, "stop": 4008.4,
                "stop_method": "structure", "qty": 13,
                "position_value": 56640.0, "risk_if_stopped": 2991.0,
                "targets": [{"price": 5054.0, "r": 2.0},
                            {"price": 5402.5, "r": 3.0}],
            }],
        }

    def test_the_fallback_carries_entry_stop_and_both_targets(self):
        import bulletin_render as br
        out = br._fallback_md(self._data(), TypeError("unsupported format string"))
        for must in ("CENTUM", "4,238.50", "4,356.90", "4,008.40",
                     "5,054.00", "5,402.50", "SEBI-registered"):
            self.assertIn(must, out)

    def test_render_md_safe_returns_a_report_when_the_renderer_breaks(self):
        import bulletin_render as br
        orig = br.render_md
        br.render_md = lambda d: (_ for _ in ()).throw(TypeError("boom"))
        try:
            out = br.render_md_safe(self._data())
        finally:
            br.render_md = orig
        self.assertIn("CENTUM", out)
        self.assertIn("4,008.40", out)

    def test_a_good_render_is_untouched(self):
        import bulletin_render as br
        # The real renderer must still work on a real bulletin.
        import json
        from pathlib import Path
        files = sorted((Path(__file__).resolve().parents[1] / "reports")
                       .glob("bulletin_daily_*.json"))
        if not files:
            self.skipTest("no bulletin on disk")
        d = json.loads(files[-1].read_text())
        self.assertIn("Educational", br.render_md_safe(d)[:400])

    def test_one_broken_card_does_not_cost_the_others(self):
        import telegram_bulletin as tb
        orig = tb.msg_plan
        tb.msg_plan = lambda p: (_ for _ in ()).throw(TypeError("boom"))
        try:
            msgs = tb.build_messages(self._data())
        finally:
            tb.msg_plan = orig
        self.assertTrue(msgs)
        joined = "\n".join(msgs)
        self.assertIn("CENTUM", joined)
        self.assertIn("Buy above", joined)
        self.assertIn("4,008.40", joined)

    def test_the_plan_fallback_has_the_levels(self):
        import telegram_bulletin as tb
        card = tb._plan_fallback(self._data()["plans"][0])
        self.assertIn("4,238.50", card)
        self.assertIn("4,008.40", card)
        self.assertIn("5,402.50", card)


class TestTheStockStateIsNotAStalePivot(unittest.TestCase):
    """The card used to offer a level the market had cleared weeks ago.

    "Pivot 328.70 · now +17.1% from it" and then "Buy above 329.40" for a
    stock at 384.80. Arithmetically right, practically useless, and it looked
    actionable. These tests pin the fix.
    """

    def _detail(self):
        return {
            "close": 383.75, "change_pct": 8.4, "vol_x50": 1.86,
            "ext_above_20d_high_pct": 5.4, "avg_range_pct": 4.4,
            "sma20": 336.52, "hi20_prev": 364.0,
            "days_since_breakout": 0, "gain_since_breakout_pct": 0.0,
            "rsi": 73.3, "atr_pct": 4.12, "adx": 16.9,
        }

    def _rows(self):
        """60 bars of history, then a shelf, then the breakout bar."""
        base = []
        for i in range(60):
            c = 250.0 + i * 0.2
            base.append({"date": f"2026-08-{i + 1:02d}", "open": c,
                         "high": c + 2, "low": c - 2, "close": c,
                         "volume": 4_000_000})
        for i, (h, l, c) in enumerate([
            (341.0, 329.60, 338.50), (344.0, 335.00, 341.25),
            (342.0, 335.00, 338.90), (344.9, 335.65, 342.85),
            (357.0, 343.25, 354.00),
        ]):
            base.append({"date": f"2026-11-{3 + i:02d}", "open": c, "high": h,
                         "low": l, "close": c, "volume": 5_000_000})
        base.append({"date": "2026-11-12", "open": 360.0, "high": 389.0,
                     "low": 356.0, "close": 383.75, "volume": 34_757_896})
        return base

    def test_a_breakout_is_recognised_as_a_breakout_not_a_missed_pivot(self):
        import forward_plan as fp
        rows = self._rows()
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", self._detail(), {"pivot": 328.70},
                             500_000, 1.0)
        finally:
            fp._rows = orig
        self.assertEqual(res["state"], "breakout_today")
        self.assertTrue(res["plans"])
        best = res["plans"][0]
        # the entry must be near the shelf, not 17% below the price
        self.assertLess(abs(best["entry"] - 357.0), 1.0)
        self.assertGreater(best["entry"], 340)
        self.assertLess(best["stop"], best["entry"])
        self.assertTrue(best["targets"])
        for t in best["targets"]:
            self.assertGreater(t["price"], best["entry"])

    def test_an_extended_stock_gets_no_chase_entry(self):
        import forward_plan as fp
        detail = dict(self._detail(), vol_x50=0.8, change_pct=0.2)
        rows = self._rows()
        # the pivot was cleared a long time ago and the stock is far above it
        rows[-1]["close"] = 384.0
        rows[-1]["high"] = 389.0
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", detail, {"pivot": 328.70}, 500_000, 1.0)
        finally:
            fp._rows = orig
        self.assertIn(res["state"], ("extended", "breakout_running"))
        for p in res["plans"]:
            # any entry offered must not be the stale pivot
            self.assertNotEqual(p["entry"], 328.70)
            self.assertGreater(p["entry"], 300)

    def test_the_verdict_says_what_the_trade_is(self):
        import bulletin_render as br
        v, css = br.state_verdict("breakout_today", stage2=True,
                                   base_grade="A", plans=[{}])
        self.assertIn("BREAKOUT", v)
        v2, _ = br.state_verdict("extended", stage2=True, base_grade="A",
                                 plans=[])
        self.assertIn("ALREADY RAN", v2)
        v3, _ = br.state_verdict("at_pivot", stage2=True, base_grade="A",
                                 tt_passed=6, from_pivot_pct=0.1)
        self.assertEqual(v3, "BUY-READY")

    def test_a_stop_that_had_to_be_pulled_in_says_so(self):
        import forward_plan as fp
        p = fp._plan("x", 100.0, 80.0, 500_000, 1.0, "test")
        self.assertTrue(p["stop_capped"])
        self.assertEqual(p["stop_quality"], "capped inside the base")
        self.assertAlmostEqual(p["stop_pct"], 8.0, places=2)
        self.assertEqual(p["structural_stop"], 80.0)

    def test_a_stop_under_the_structure_is_not_capped(self):
        import forward_plan as fp
        p = fp._plan("x", 100.0, 94.0, 500_000, 1.0, "test")
        self.assertFalse(p["stop_capped"])
        self.assertEqual(p["stop_quality"], "structure")
        self.assertEqual(p["stop"], 94.0)

    def test_the_size_risk_is_the_budget_and_the_qty_is_whole(self):
        import forward_plan as fp
        p = fp._plan("x", 357.0, 329.60, 500_000, 1.0, "test")
        self.assertGreater(p["qty"], 0)
        self.assertEqual(p["qty"], int(p["qty"]))
        self.assertLessEqual(p["risk"], p["risk_budget"] * 1.02)


class TestBaseRates(unittest.TestCase):
    """Statistics, not hunches — and honest about the sample."""

    def test_the_market_table_is_built_from_the_cache(self):
        import base_rates
        data = base_rates.load_cache()
        if not data:
            self.skipTest("base rates cache not built")
        market = data.get("market") or {}
        self.assertTrue(market)
        for sig, entry in market.items():
            self.assertIn("h10", entry)
            self.assertGreater(entry["h10"]["n"], 0)
            self.assertIn("median", entry["h10"])
            self.assertIn("hit", entry["h10"])

    def test_a_signal_is_recognised_from_the_detail(self):
        import base_rates
        self.assertIn("up_day_volume", base_rates.matches(
            {"change_pct": 8.4, "vol_x50": 1.9}))
        self.assertIn("breakout_20d", base_rates.matches(
            {"change_pct": 8.4, "vol_x50": 1.9, "close": 383.75,
             "hi20_prev": 364.0}))
        self.assertNotIn("up_day_volume", base_rates.matches(
            {"change_pct": 1.0, "vol_x50": 0.9}))

    def test_the_numbers_are_a_distribution_not_a_point(self):
        import base_rates
        st = base_rates._stats([1.0, -2.0, 3.0, 4.0])
        self.assertEqual(st["n"], 4)
        self.assertEqual(st["hit"], 75.0)
        self.assertAlmostEqual(st["median"], 2.0)
        self.assertLessEqual(st["p25"], st["median"])
        self.assertGreaterEqual(st["p75"], st["median"])

    def test_an_empty_sample_is_not_a_statistic(self):
        import base_rates
        self.assertEqual(base_rates._stats([]), {"n": 0})


class TestTechnicalDetail(unittest.TestCase):
    def test_atr_is_a_share_of_price_not_a_change(self):
        import tech_detail
        d = tech_detail.detail("CUPID")
        if d.get("error"):
            self.skipTest("no cache")
        self.assertGreater(d["atr_pct"], 0)
        self.assertLess(d["atr_pct"], 30)
        self.assertGreater(d["vol_x50"], 0)
        self.assertIsNotNone(d["rsi"])

    def test_the_report_carries_the_numbers_not_just_a_label(self):
        import stock_check
        r = stock_check.check("CUPID")
        if r.get("error"):
            self.skipTest("no data")
        detail = stock_check.card_detail(r)
        for must in ("RSI", "ADX", "ATR", "52-week", "Delivery"):
            self.assertIn(must, detail)
        # the base-rate block appears when the stock matches a signal
        r2 = dict(r)
        r2["rates"] = {"matches": ["up_day_volume"], "market": {}, "symbol": {}}
        self.assertIn("Base rates", stock_check.card_detail(r2))
        # the stale pivot must not be presented as an entry
        self.assertNotIn("Buy above 329", detail)


class TestTheGate(unittest.TestCase):
    """One gate, used by the report, the card and the alerts.

    A suggestion that cannot be acted on today is not a suggestion. It is a
    level the market left behind, and publishing it is worse than publishing
    nothing.
    """

    def _rows(self):
        # The highs stay under the 101 trigger, so no bar ever touched it:
        # this is a level the market has not tested.
        base = []
        for i in range(60):
            c = 100.0
            base.append({"date": f"2026-08-{i + 1:02d}", "open": c, "high": c + 0.4,
                         "low": c - 1, "close": c, "volume": 1000})
        base += [
            {"date": "2026-11-02", "open": 100, "high": 100.4, "low": 99, "close": 100, "volume": 1000},
            {"date": "2026-11-03", "open": 100, "high": 100.4, "low": 99, "close": 100, "volume": 1000},
        ]
        return base

    def test_an_unreachable_trigger_is_not_published(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 110, "stop": 95, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])
        self.assertTrue(any("nobody is getting filled" in r for r in g["reasons"]))

    def test_a_stop_above_the_price_is_nonsense(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 105, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])
        self.assertTrue(any("stop" in r and "above" in r for r in g["reasons"]))

    def test_a_trigger_below_the_price_means_the_breakout_happened(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 95, "stop": 90, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])

    def test_a_clean_setup_passes(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 95, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            from_pivot_pct=0.5, rows=self._rows())
        self.assertTrue(g["ok"])
        self.assertEqual(g["verdict"], "BUY-READY")

    def test_a_grade_c_base_is_not_a_setup(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 95, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="C",
                            rows=self._rows())
        self.assertFalse(g["ok"])

    def test_a_pullback_entry_is_allowed_to_be_below_the_price(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 95, "stop": 88, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            rows=self._rows(), kind="pullback")
        self.assertTrue(g["ok"], g["reasons"])

    def test_a_pullback_entry_that_is_a_collapse_is_not(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 70, "stop": 65, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            rows=self._rows(), kind="pullback")
        self.assertFalse(g["ok"])

    def test_a_fresh_failure_is_rejected_and_an_old_one_is_only_a_note(self):
        import plan_gate
        rows = self._rows()
        rows.append({"date": "2026-11-04", "open": 100, "high": 104,
                     "low": 99, "close": 99.5, "volume": 2000})
        g = plan_gate.gate({"trigger": 102, "stop": 95, "close": 100},
                            "TEST", close=100, rows=rows)
        self.assertFalse(g["ok"])
        self.assertEqual(g["verdict"], "SETUP FAILED")


class TestTheStateMachine(unittest.TestCase):
    """The call has to describe what the price is doing now."""

    def _rows(self, closes):
        out = []
        for i, (h, l, c) in enumerate(closes):
            out.append({"date": f"2026-{9 + i // 25:02d}-{i % 25 + 1:02d}",
                        "open": c, "high": h, "low": l, "close": c,
                        "volume": 1_000_000})
        return out

    def test_a_stock_below_its_pivot_is_waiting_not_buyable(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60 + [(101, 99, 100), (101, 99, 100)])
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", {"close": 100, "vol_x50": 1.0},
                              {"pivot": 110, "since": "2026-09-01"},
                              500_000, 1.0, stage2=True, base_grade="A")
        finally:
            fp._rows = orig
        self.assertEqual(res["state"], "pre_breakout")
        self.assertTrue(res["state_text"].startswith("Inside the base"))

    def test_a_breakout_that_already_ran_is_not_offered_again(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60 + [(101, 99, 100), (101, 99, 100)])
        # today closed far above the pivot: the trade ran without us
        rows[-1].update({"close": 120.0, "high": 121.0, "low": 99.0})
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", {"close": 120, "vol_x50": 1.0},
                              {"pivot": 100, "since": "2026-09-01"},
                              500_000, 1.0, stage2=True, base_grade="A")
        finally:
            fp._rows = orig
        self.assertIn(res["state"], ("extended", "breakout_running"))
        for p in res["plans"]:
            self.assertNotEqual(p.get("entry"), 100.0)

    def test_the_breakout_bar_uses_the_close_not_the_high(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60)
        # a bar that spikes to 120 and closes at 100 is not a breakout
        rows.append({"date": "2026-12-01", "open": 100, "high": 120, "low": 99,
                     "close": 100, "volume": 1_000_000})
        self.assertIsNone(fp._breakout_bar(rows))


class TestResultsAhead(unittest.TestCase):
    """Buy the setup before the number, not the gap after it."""

    def test_the_calendar_reads_a_future_date_as_confirmed(self):
        import results_calendar as rc
        row = rc.fetch("TCS")
        if row.get("error") or not row.get("next"):
            self.skipTest("no earnings data available")
        self.assertTrue(row["confirmed"])
        self.assertTrue(row["next"])

    def test_a_print_outside_the_window_is_not_listed(self):
        import results_calendar as rc
        rows = rc.upcoming(0)
        for r in rows:
            self.assertLessEqual(r["sessions"], 0)

    def test_the_reaction_is_measured_not_guessed(self):
        import pre_results
        res = pre_results.reaction("TCS", ["2026-07-16", "2026-04-15"])
        self.assertIn("n", res)
        if res["n"]:
            self.assertIn("h5", res)
            self.assertIn("gap", res)

    def test_the_market_study_has_a_sample_size(self):
        import pre_results
        m = pre_results.market_reaction()
        self.assertIn("n", m)
        self.assertIn("h5", m)


class TestTheReportNeverPublishesAnUnreachableLevel(unittest.TestCase):
    def test_the_candidate_filter_is_tight(self):
        import bulletin_data
        import inspect
        src = inspect.getsource(bulletin_data.collect)
        self.assertIn("-4.0 <= s[\"from_pivot_pct\"] <= 1.5", src)

    def test_every_published_plan_goes_through_the_gate(self):
        import bulletin_data
        import inspect
        src = inspect.getsource(bulletin_data.collect)
        self.assertIn("plan_gate.gate(", src)


class TestTheDataIsFresh(unittest.TestCase):
    def test_the_pre_market_job_only_verifies(self):
        import inspect
        import scheduler
        jobs = {j[0]: j for j in scheduler.JOBS}
        self.assertIn("--verify", jobs["cache-premarket"][3])

    def test_the_watchdog_checks_delivery_not_just_the_file(self):
        import watchdog
        self.assertTrue(callable(watchdog._delivered))
        self.assertTrue(callable(watchdog._delivery_failures))

    def test_the_cache_verifier_finds_what_is_behind(self):
        import build_cache
        rows = build_cache.behind(build_cache.date.today())
        self.assertIsInstance(rows, list)


class TestOnePositionPerStock(unittest.TestCase):
    """A second entry is a bigger bet, not a new idea.

    The dedupe key used to be (symbol, entry). The trigger moves with the
    price, so CENTUM's plan was 4,238.50 on one session and 4,274.60 on the
    next, and the same stock was booked twice: two positions, twice the risk,
    and a book that listed it twice.
    """

    def test_a_moved_trigger_does_not_book_the_stock_twice(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": []}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            data = {"generated_at": "2026-11-12T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 100.0, "limit": 103.0, "stop": 95.0,
                 "targets": [{"price": 110.0}, {"price": 120.0}], "qty": 10,
                 "base_type": "VCP", "base_grade": "A"}]}
            recs.record(data)
            # the next session the trigger has moved, but it is the same stock
            data2 = {"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}, {"price": 124.0}], "qty": 9,
                 "base_type": "VCP", "base_grade": "A"}]}
            recs.record(data2)
            live = [r for r in store["records"]
                    if r["status"] in ("pending_fill", "filled", "t1_hit")]
            self.assertEqual(len(live), 1, "the same stock was booked twice")
            # and the live order's levels are refreshed, not duplicated
            self.assertEqual(live[0]["entry"], 104.0)
        finally:
            recs.load, recs.save = orig_load, orig_save

    def test_a_second_position_in_a_filled_stock_is_refused(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": [{
            "id": "2026-11-12:TEST", "symbol": "TEST", "status": "filled",
            "entry": 100.0, "stop": 95.0, "qty": 10, "date": "2026-11-12",
            "live_from": "2026-11-12"}]}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            recs.record({"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}], "qty": 9}]})
            self.assertEqual(len(store["records"]), 1)
        finally:
            recs.load, recs.save = orig_load, orig_save

    def test_a_closed_trade_is_kept_for_the_scorecard(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": [{
            "id": "2026-11-12:TEST", "symbol": "TEST", "status": "stopped",
            "entry": 100.0, "stop": 95.0, "qty": 10, "date": "2026-11-12",
            "live_from": "2026-11-12", "r_realized": -1.0}]}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            recs.record({"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}], "qty": 9}]})
            self.assertEqual(len(store["records"]), 2)
        finally:
            recs.load, recs.save = orig_load, orig_save


class TestStaleTriggers(unittest.TestCase):
    """A trigger the market walked away from is not a commitment."""

    def test_a_stale_trigger_is_flagged_and_not_counted_as_risk(self):
        import heat
        orig_pos = heat.positions
        heat.positions = lambda: [
            {"symbol": "A", "kind": "open", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "Energy"},
            {"symbol": "B", "kind": "pending", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "IT",
             "last_close": 94.0, "below_trigger_pct": 6.4, "stale": True},
            {"symbol": "C", "kind": "pending", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "IT",
             "last_close": 99.5, "below_trigger_pct": 0.5, "stale": False},
        ]
        try:
            h = heat.compute(capital=100_000, risk_pct=1.0, cap_pct=6.0)
        finally:
            heat.positions = orig_pos
        self.assertEqual(h["committed"], 500)        # only the live one
        self.assertEqual(h["stale_count"], 1)
        self.assertEqual(h["stale_risk"], 500)
        # and the headline says which ones to cancel
        line = " ".join(heat.headline(h))
        self.assertIn("stale", line)
        self.assertIn("B", line)


class TestTheTrackRecordIsProtected(unittest.TestCase):
    """The book is the only memory the desk has.

    A bug that quietly drops half of it would not announce itself: the
    reports would simply stop mentioning trades the reader remembers being
    told about.
    """

    def test_a_shrink_is_refused_and_the_old_file_is_kept(self):
        import recs
        import tempfile
        import shutil
        orig_store = recs.STORE
        tmp = pathlib.Path(tempfile.mkdtemp()) / "recs.json"
        recs.STORE = tmp
        try:
            recs.save({"records": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
            ok = recs.save({"records": [{"id": "a"}]})
            self.assertFalse(ok, "a shrinking write was allowed")
            self.assertEqual(len(recs.load()["records"]), 3)
            backup = tmp.with_name("recs.bak.json")
            self.assertTrue(backup.exists())
        finally:
            recs.STORE = orig_store

    def test_a_shrink_can_be_forced_on_purpose(self):
        import recs
        import tempfile
        orig_store = recs.STORE
        tmp = pathlib.Path(tempfile.mkdtemp()) / "recs.json"
        recs.STORE = tmp
        try:
            recs.save({"records": [{"id": "a"}, {"id": "b"}]})
            ok = recs.save({"records": [{"id": "a"}]}, allow_shrink=True)
            self.assertTrue(ok)
            self.assertEqual(len(recs.load()["records"]), 1)
        finally:
            recs.STORE = orig_store


if __name__ == "__main__":
    unittest.main()

class TestTheGate(unittest.TestCase):
    """One gate, used by the report, the card and the alerts.

    A suggestion that cannot be acted on today is not a suggestion. It is a
    level the market left behind, and publishing it is worse than publishing
    nothing.
    """

    def _rows(self):
        # The highs stay under the 101 trigger, so no bar ever touched it:
        # this is a level the market has not tested.
        base = []
        for i in range(60):
            c = 100.0
            base.append({"date": f"2026-08-{i + 1:02d}", "open": c, "high": c + 0.4,
                         "low": c - 1, "close": c, "volume": 1000})
        base += [
            {"date": "2026-11-02", "open": 100, "high": 100.4, "low": 99, "close": 100, "volume": 1000},
            {"date": "2026-11-03", "open": 100, "high": 100.4, "low": 99, "close": 100, "volume": 1000},
        ]
        return base

    def test_an_unreachable_trigger_is_not_published(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 110, "stop": 95, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])
        self.assertTrue(any("nobody is getting filled" in r for r in g["reasons"]))

    def test_a_stop_above_the_price_is_nonsense(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 105, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])
        self.assertTrue(any("stop" in r and "above" in r for r in g["reasons"]))

    def test_a_trigger_below_the_price_means_the_breakout_happened(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 95, "stop": 90, "close": 100},
                            "TEST", close=100, rows=self._rows())
        self.assertFalse(g["ok"])

    def test_a_clean_setup_passes(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 95, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            from_pivot_pct=0.5, rows=self._rows())
        self.assertTrue(g["ok"])
        self.assertEqual(g["verdict"], "BUY-READY")

    def test_a_grade_c_base_is_not_a_setup(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 101, "stop": 95, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="C",
                            rows=self._rows())
        self.assertFalse(g["ok"])

    def test_a_pullback_entry_is_allowed_to_be_below_the_price(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 95, "stop": 88, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            rows=self._rows(), kind="pullback")
        self.assertTrue(g["ok"], g["reasons"])

    def test_a_pullback_entry_that_is_a_collapse_is_not(self):
        import plan_gate
        g = plan_gate.gate({"trigger": 70, "stop": 65, "close": 100},
                            "TEST", close=100, stage2=True, base_grade="A",
                            rows=self._rows(), kind="pullback")
        self.assertFalse(g["ok"])

    def test_a_fresh_failure_is_rejected_and_an_old_one_is_only_a_note(self):
        import plan_gate
        rows = self._rows()
        rows.append({"date": "2026-11-04", "open": 100, "high": 104,
                     "low": 99, "close": 99.5, "volume": 2000})
        g = plan_gate.gate({"trigger": 102, "stop": 95, "close": 100},
                            "TEST", close=100, rows=rows)
        self.assertFalse(g["ok"])
        self.assertEqual(g["verdict"], "SETUP FAILED")


class TestTheStateMachine(unittest.TestCase):
    """The call has to describe what the price is doing now."""

    def _rows(self, closes):
        out = []
        for i, (h, l, c) in enumerate(closes):
            out.append({"date": f"2026-{9 + i // 25:02d}-{i % 25 + 1:02d}",
                        "open": c, "high": h, "low": l, "close": c,
                        "volume": 1_000_000})
        return out

    def test_a_stock_below_its_pivot_is_waiting_not_buyable(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60 + [(101, 99, 100), (101, 99, 100)])
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", {"close": 100, "vol_x50": 1.0},
                              {"pivot": 110, "since": "2026-09-01"},
                              500_000, 1.0, stage2=True, base_grade="A")
        finally:
            fp._rows = orig
        self.assertEqual(res["state"], "pre_breakout")
        self.assertTrue(res["state_text"].startswith("Inside the base"))

    def test_a_breakout_that_already_ran_is_not_offered_again(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60 + [(101, 99, 100), (101, 99, 100)])
        # today closed far above the pivot: the trade ran without us
        rows[-1].update({"close": 120.0, "high": 121.0, "low": 99.0})
        orig = fp._rows
        fp._rows = lambda sym: rows
        try:
            res = fp.analyse("TEST", {"close": 120, "vol_x50": 1.0},
                              {"pivot": 100, "since": "2026-09-01"},
                              500_000, 1.0, stage2=True, base_grade="A")
        finally:
            fp._rows = orig
        self.assertIn(res["state"], ("extended", "breakout_running"))
        for p in res["plans"]:
            self.assertNotEqual(p.get("entry"), 100.0)

    def test_the_breakout_bar_uses_the_close_not_the_high(self):
        import forward_plan as fp
        rows = self._rows([(101, 99, 100)] * 60)
        # a bar that spikes to 120 and closes at 100 is not a breakout
        rows.append({"date": "2026-12-01", "open": 100, "high": 120, "low": 99,
                     "close": 100, "volume": 1_000_000})
        self.assertIsNone(fp._breakout_bar(rows))


class TestResultsAhead(unittest.TestCase):
    """Buy the setup before the number, not the gap after it."""

    def test_the_calendar_reads_a_future_date_as_confirmed(self):
        import results_calendar as rc
        row = rc.fetch("TCS")
        if row.get("error") or not row.get("next"):
            self.skipTest("no earnings data available")
        self.assertTrue(row["confirmed"])
        self.assertTrue(row["next"])

    def test_a_print_outside_the_window_is_not_listed(self):
        import results_calendar as rc
        rows = rc.upcoming(0)
        for r in rows:
            self.assertLessEqual(r["sessions"], 0)

    def test_the_reaction_is_measured_not_guessed(self):
        import pre_results
        res = pre_results.reaction("TCS", ["2026-07-16", "2026-04-15"])
        self.assertIn("n", res)
        if res["n"]:
            self.assertIn("h5", res)
            self.assertIn("gap", res)

    def test_the_market_study_has_a_sample_size(self):
        import pre_results
        m = pre_results.market_reaction()
        self.assertIn("n", m)
        self.assertIn("h5", m)


class TestTheReportNeverPublishesAnUnreachableLevel(unittest.TestCase):
    def test_the_candidate_filter_is_tight(self):
        import bulletin_data
        import inspect
        src = inspect.getsource(bulletin_data.collect)
        self.assertIn("-4.0 <= s[\"from_pivot_pct\"] <= 1.5", src)

    def test_every_published_plan_goes_through_the_gate(self):
        import bulletin_data
        import inspect
        src = inspect.getsource(bulletin_data.collect)
        self.assertIn("plan_gate.gate(", src)


class TestTheDataIsFresh(unittest.TestCase):
    def test_the_pre_market_job_only_verifies(self):
        import inspect
        import scheduler
        jobs = {j[0]: j for j in scheduler.JOBS}
        self.assertIn("--verify", jobs["cache-premarket"][3])

    def test_the_watchdog_checks_delivery_not_just_the_file(self):
        import watchdog
        self.assertTrue(callable(watchdog._delivered))
        self.assertTrue(callable(watchdog._delivery_failures))

    def test_the_cache_verifier_finds_what_is_behind(self):
        import build_cache
        rows = build_cache.behind(build_cache.date.today())
        self.assertIsInstance(rows, list)


class TestOnePositionPerStock(unittest.TestCase):
    """A second entry is a bigger bet, not a new idea.

    The dedupe key used to be (symbol, entry). The trigger moves with the
    price, so CENTUM's plan was 4,238.50 on one session and 4,274.60 on the
    next, and the same stock was booked twice: two positions, twice the risk,
    and a book that listed it twice.
    """

    def test_a_moved_trigger_does_not_book_the_stock_twice(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": []}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            data = {"generated_at": "2026-11-12T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 100.0, "limit": 103.0, "stop": 95.0,
                 "targets": [{"price": 110.0}, {"price": 120.0}], "qty": 10,
                 "base_type": "VCP", "base_grade": "A"}]}
            recs.record(data)
            # the next session the trigger has moved, but it is the same stock
            data2 = {"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}, {"price": 124.0}], "qty": 9,
                 "base_type": "VCP", "base_grade": "A"}]}
            recs.record(data2)
            live = [r for r in store["records"]
                    if r["status"] in ("pending_fill", "filled", "t1_hit")]
            self.assertEqual(len(live), 1, "the same stock was booked twice")
            # and the live order's levels are refreshed, not duplicated
            self.assertEqual(live[0]["entry"], 104.0)
        finally:
            recs.load, recs.save = orig_load, orig_save

    def test_a_second_position_in_a_filled_stock_is_refused(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": [{
            "id": "2026-11-12:TEST", "symbol": "TEST", "status": "filled",
            "entry": 100.0, "stop": 95.0, "qty": 10, "date": "2026-11-12",
            "live_from": "2026-11-12"}]}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            recs.record({"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}], "qty": 9}]})
            self.assertEqual(len(store["records"]), 1)
        finally:
            recs.load, recs.save = orig_load, orig_save

    def test_a_closed_trade_is_kept_for_the_scorecard(self):
        import recs
        orig_load, orig_save = recs.load, recs.save
        store = {"version": 1, "records": [{
            "id": "2026-11-12:TEST", "symbol": "TEST", "status": "stopped",
            "entry": 100.0, "stop": 95.0, "qty": 10, "date": "2026-11-12",
            "live_from": "2026-11-12", "r_realized": -1.0}]}
        recs.load = lambda: store
        recs.save = lambda s: None
        try:
            recs.record({"generated_at": "2026-11-13T09:00:00", "plans": [
                {"symbol": "TEST", "trigger": 104.0, "limit": 107.0, "stop": 98.0,
                 "targets": [{"price": 114.0}], "qty": 9}]})
            self.assertEqual(len(store["records"]), 2)
        finally:
            recs.load, recs.save = orig_load, orig_save


class TestStaleTriggers(unittest.TestCase):
    """A trigger the market walked away from is not a commitment."""

    def test_a_stale_trigger_is_flagged_and_not_counted_as_risk(self):
        import heat
        orig_pos = heat.positions
        heat.positions = lambda: [
            {"symbol": "A", "kind": "open", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "Energy"},
            {"symbol": "B", "kind": "pending", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "IT",
             "last_close": 94.0, "below_trigger_pct": 6.4, "stale": True},
            {"symbol": "C", "kind": "pending", "qty": 100, "entry": 100.0,
             "stop": 95.0, "risk": 500.0, "sector": "IT",
             "last_close": 99.5, "below_trigger_pct": 0.5, "stale": False},
        ]
        try:
            h = heat.compute(capital=100_000, risk_pct=1.0, cap_pct=6.0)
        finally:
            heat.positions = orig_pos
        self.assertEqual(h["committed"], 500)        # only the live one
        self.assertEqual(h["stale_count"], 1)
        self.assertEqual(h["stale_risk"], 500)
        # and the headline says which ones to cancel
        line = " ".join(heat.headline(h))
        self.assertIn("stale", line)
        self.assertIn("B", line)


class TestTheTrackRecordIsProtected(unittest.TestCase):
    """The book is the only memory the desk has.

    A bug that quietly drops half of it would not announce itself: the
    reports would simply stop mentioning trades the reader remembers being
    told about.
    """

    def test_a_shrink_is_refused_and_the_old_file_is_kept(self):
        import recs
        import tempfile
        import shutil
        orig_store = recs.STORE
        tmp = pathlib.Path(tempfile.mkdtemp()) / "recs.json"
        recs.STORE = tmp
        try:
            recs.save({"records": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
            ok = recs.save({"records": [{"id": "a"}]})
            self.assertFalse(ok, "a shrinking write was allowed")
            self.assertEqual(len(recs.load()["records"]), 3)
            backup = tmp.with_name("recs.bak.json")
            self.assertTrue(backup.exists())
        finally:
            recs.STORE = orig_store

    def test_a_shrink_can_be_forced_on_purpose(self):
        import recs
        import tempfile
        orig_store = recs.STORE
        tmp = pathlib.Path(tempfile.mkdtemp()) / "recs.json"
        recs.STORE = tmp
        try:
            recs.save({"records": [{"id": "a"}, {"id": "b"}]})
            ok = recs.save({"records": [{"id": "a"}]}, allow_shrink=True)
            self.assertTrue(ok)
            self.assertEqual(len(recs.load()["records"]), 1)
        finally:
            recs.STORE = orig_store


if __name__ == "__main__":
    unittest.main()
