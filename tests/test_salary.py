"""
tests/test_salary.py

hh is the only source that states a salary, so everywhere else silence is the
norm. The whole point of these tests is that silence never costs a job: no
number, no rate, no rejection.
"""

import core.fx as fx
import pipeline.brain1 as b1
from ui.helpers import fmt_salary

RATES = {"fetched_at": 2e9, "as_of": "test",
         "rates": {"RUB": 80.0, "UZS": 12000.0, "KZT": 470.0, "BYN": 3.0,
                   "EUR": 0.86}}


def _rates(monkeypatch, payload=RATES):
    monkeypatch.setattr(fx, "load", lambda force=False: payload)


class TestLegacyCurrencyCodes:
    """hh quotes RUR and BYR. Those were replaced in 1998 and 2016, and no rate
    table carries them, so without aliasing every RUR row converts to nothing."""

    def test_rur_resolves_through_to_rub(self, monkeypatch):
        _rates(monkeypatch)
        assert fx.to_usd(80_000, "RUR") == 1000.0

    def test_byr_resolves_through_to_byn(self, monkeypatch):
        _rates(monkeypatch)
        assert fx.to_usd(3_000, "BYR") == 1000.0

    def test_the_modern_codes_still_work(self, monkeypatch):
        _rates(monkeypatch)
        assert fx.to_usd(80_000, "RUB") == 1000.0


class TestRefusesRatherThanGuesses:
    def test_usd_needs_no_rate_at_all(self, monkeypatch):
        monkeypatch.setattr(fx, "load", lambda force=False: None)
        assert fx.to_usd(5000, "USD") == 5000

    def test_no_rate_table_means_none_not_zero(self, monkeypatch):
        monkeypatch.setattr(fx, "load", lambda force=False: None)
        assert fx.to_usd(80_000, "RUR") is None

    def test_unknown_currency_is_none(self, monkeypatch):
        _rates(monkeypatch)
        assert fx.to_usd(100, "XYZ") is None

    def test_junk_amounts_are_none(self, monkeypatch):
        _rates(monkeypatch)
        assert fx.to_usd(None, "RUR") is None
        assert fx.to_usd("abc", "RUR") is None
        assert fx.to_usd(0, "RUR") is None
        assert fx.to_usd(500, "") is None


class TestSalaryFloor:
    """The floor may only fire on a number I actually have and could convert."""

    def _job(self, lo=None, hi=None, cur="RUR", gross=""):
        return {"salary_min": lo, "salary_max": hi, "currency": cur,
                "salary_gross": gross}

    def test_no_salary_is_never_rejected(self, monkeypatch):
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(), 4500) is None

    def test_unconvertible_currency_is_never_rejected(self, monkeypatch):
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(hi=100, cur="XYZ"), 4500) is None

    def test_dead_rate_table_never_rejects(self, monkeypatch):
        monkeypatch.setattr(fx, "load", lambda force=False: None)
        assert b1.below_salary_floor(self._job(hi=8_000, cur="RUR"), 4500) is None

    def test_floor_of_zero_disables_it(self, monkeypatch):
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(hi=8_000), 0) is None

    def test_clearly_below_is_rejected(self, monkeypatch):
        _rates(monkeypatch)
        why = b1.below_salary_floor(self._job(hi=80_000), 4500)   # $1000
        assert why and "1000" in why and "4500" in why

    def test_above_the_floor_survives(self, monkeypatch):
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(hi=800_000), 4500) is None

    def test_the_top_of_the_range_decides(self, monkeypatch):
        """A 40k-800k range can pay well; judging it on the bottom would bin it."""
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(lo=40_000, hi=800_000), 4500) is None

    def test_a_lone_minimum_is_used_when_there_is_no_max(self, monkeypatch):
        _rates(monkeypatch)
        assert b1.below_salary_floor(self._job(lo=80_000), 4500) is not None

    def test_gross_or_net_is_stated_in_the_reason(self, monkeypatch):
        _rates(monkeypatch)
        why = b1.below_salary_floor(self._job(hi=80_000, gross="net"), 4500)
        assert "net" in why


class TestFormatting:
    def test_no_numbers_render_empty_so_callers_decide(self):
        assert fmt_salary(None, None, "RUR") == ""

    def test_a_range_carries_the_usd_equivalent(self, monkeypatch):
        _rates(monkeypatch)
        out = fmt_salary(83_000, 220_000, "RUR", "net")
        assert "83 000-220 000" in out and "₽" in out and "net" in out
        assert "$2,750" in out

    def test_usd_is_not_converted_to_itself(self, monkeypatch):
        _rates(monkeypatch)
        assert "~$" not in fmt_salary(5000, 9000, "USD")

    def test_one_sided_ranges_say_which_side(self, monkeypatch):
        _rates(monkeypatch)
        assert fmt_salary(80_000, None, "RUR").startswith("from")
        assert fmt_salary(None, 80_000, "RUR").startswith("up to")


class TestGrossIsCarried:
    def test_hh_reports_gross_and_net_distinctly(self):
        from pipeline.sources import hh
        base = {"vacancyId": 1, "name": "x", "area": {}}
        assert hh.parse_vacancy(
            {**base, "compensation": {"gross": True}})["salary_gross"] == "gross"
        assert hh.parse_vacancy(
            {**base, "compensation": {"gross": False}})["salary_gross"] == "net"

    def test_unstated_gross_stays_empty_not_guessed(self):
        from pipeline.sources import hh
        row = hh.parse_vacancy({"vacancyId": 1, "name": "x", "area": {},
                                "compensation": {"from": 1}})
        assert row["salary_gross"] == ""

    def test_the_column_reaches_the_insert(self):
        assert "salary_gross" in b1.JOB_INSERT_COLS
