"""The run-wide wall-clock limit is unlimited by default.

The user's own words: "as long as it is running there is no need for a limit on a round or
on the total, since tasks differ and a complex one could well run for tens of hours."

They were right, and for a harder reason than "tasks differ" — **the limit used to
contradict itself**:

    turn_timeout      = 1800s   at most 30 minutes per person per round
    max_wall_seconds  =  900s   at most 15 minutes for the whole run

A single turn's cap at twice the whole run's cap does not hold together at all.

That measured run: three parties over four rounds, the slowest turn each round taking
153–690s, so at least 2700s in total. They configured 2000s, so by the start of round 3 the
remainder was too small, every call's timeout was squeezed to 1 second, that round was
doomed, and it nearly buried the first three rounds' results with it.
"""

from __future__ import annotations

from sesa.config import Config


def test_there_is_no_wall_clock_cap_out_of_the_box():
    assert Config().max_wall_seconds is None


def test_the_structural_bound_still_exists():
    """The total is already bounded: at most max_rounds rounds, none longer than turn_timeout.

    **A wall-clock limit adds no protection at all**; it adds exactly one failure mode, "cut off
    part-way".
    """
    conf = Config()
    assert conf.max_rounds > 0
    assert conf.turn_timeout > 0


def test_the_per_turn_timeout_is_not_smaller_than_a_realistic_turn():
    """The slowest measured turn was 690s (claude with tools reviewing documents).

    The per-turn timeout has to leave room — it is the gate that actually works: a hung process
    is reaped by it and by idle_timeout, not by the total.
    """
    assert Config().turn_timeout >= 1800


def test_a_wall_cap_is_still_available_for_those_who_want_it():
    """Unset by default does not mean it cannot be set. Anyone who wants to control cost still can."""
    conf = Config()
    conf.max_wall_seconds = 600.0
    assert conf.max_wall_seconds == 600.0


def test_the_wizard_offers_unlimited_as_the_default():
    # **Check the output the user sees, not the source text.**
    # The previous version asserted `"0 = unlimited" in inspect.getsource(...)` — one move to i18n
    # split that sentence across several lines through t(), and the assertion went red while **the
    # behaviour had not changed at all**. A test that reads the source tests how I wrote it, not how
    # the product behaves.
    from sesa import i18n
    from sesa.locales.zh import CATALOGUE

    i18n.use("en")
    english = " ".join(i18n.t(k) for k in CATALOGUE)
    assert "0 = unlimited" in english
    assert "only cuts a run off halfway" in english

    # The Chinese path has to say the same thing — a translation that loses this leaves Chinese
    # users filling in a number out of old habit.
    chinese = " ".join(CATALOGUE.values())
    assert "不限" in chinese and "只会在跑到一半时砍断" in chinese
