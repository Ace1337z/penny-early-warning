"""Shariah screening: combine rules, cache, modes and outage handling."""

from __future__ import annotations

from penny.shariah import (COMPLIANT, DOUBTFUL, ERROR, FakeShariahSource, NON_COMPLIANT,
                           NOT_SCREENED, UNKNOWN, ShariahService, combine)


def test_combine_rules():
    assert combine([COMPLIANT, COMPLIANT]) == COMPLIANT
    assert combine([COMPLIANT, NON_COMPLIANT]) == NON_COMPLIANT
    assert combine([NON_COMPLIANT, COMPLIANT]) == NON_COMPLIANT
    assert combine([COMPLIANT, DOUBTFUL]) == DOUBTFUL
    assert combine([COMPLIANT, NOT_SCREENED]) == COMPLIANT
    assert combine([NOT_SCREENED, ERROR]) == UNKNOWN
    assert combine([]) == UNKNOWN


def _service(cfg, state, mode="tag"):
    cfg.set("SHARIAH_MODE", mode)
    sources = [
        FakeShariahSource("halalterminal", {"AAA": COMPLIANT, "BBB": DOUBTFUL,
                                            "CCC": NON_COMPLIANT}),
    ]
    return ShariahService(cfg, state, sources), sources


def test_agreement_and_disagreement(cfg, state):
    svc, _ = _service(cfg, state)
    assert svc.status("AAA", fetch=True)["combined"] == COMPLIANT
    assert svc.status("BBB", fetch=True)["combined"] == DOUBTFUL
    assert svc.status("CCC", fetch=True)["combined"] == NON_COMPLIANT
    assert svc.status("ZZZ", fetch=True)["combined"] == UNKNOWN


def test_result_is_cached(cfg, state):
    svc, sources = _service(cfg, state)
    svc.status("AAA", fetch=True)
    calls_before = len(sources[0].calls)
    svc.status("AAA", fetch=True)
    assert len(sources[0].calls) == calls_before  # served from cache


def test_source_outage_uses_stale_cache(cfg, state):
    cfg.set("SHARIAH_TTL_DAYS", "0")  # force the cache to be considered stale
    svc, _ = _service(cfg, state)
    first = svc.status("AAA", fetch=True)
    assert first["combined"] == COMPLIANT
    again = svc.status("AAA", fetch=True)
    assert again["combined"] == COMPLIANT


def test_modes_suppress_as_defined(cfg, state):
    tag, _ = _service(cfg, state, mode="tag")
    assert tag.should_alert(tag.status("CCC", fetch=True))[0] is True

    only, _ = _service(cfg, state, mode="only_compliant")
    assert only.should_alert(only.status("AAA", fetch=True))[0] is True
    assert only.should_alert(only.status("CCC", fetch=True))[0] is False

    hide, _ = _service(cfg, state, mode="hide_noncompliant")
    assert hide.should_alert(hide.status("CCC", fetch=True))[0] is False
    assert hide.should_alert(hide.status("BBB", fetch=True))[0] is True

    off, _ = _service(cfg, state, mode="off")
    assert off.enabled is False


def test_display_lines(cfg, state):
    svc, _ = _service(cfg, state)
    status = svc.status("AAA", fetch=True)
    assert "COMPLIANT" in svc.one_line(status)
    lines = svc.detail_lines(status)
    assert any("halalterminal" in line for line in lines)
    assert any("not a religious ruling" in line for line in lines)
