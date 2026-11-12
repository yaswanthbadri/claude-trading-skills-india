"""Tests for the desk itself: matcher, verdicts, calendar, costs, levels.

Every bug found while building this system was in a small pure function that
failed silently: a reversed tuple unpack, a regex gap that excluded '.', a
column index level dropped the wrong way. None of them raised. All of them
would have been caught here in under a second.

    python3 -m unittest discover -s tests -p 'test_desk*.py' -v
"""
import sys
import unittest

from _load import ROOT

sys.path.insert(0, str(ROOT / "tools"))

import alert_monitor
import bulletin_render
import market_calendar as cal
import recs


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


if __name__ == "__main__":
    unittest.main()
