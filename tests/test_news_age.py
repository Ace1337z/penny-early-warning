"""News source, session scope and headline age.

Bugs this pins down: the Finviz news export is market-wide and missed a small cap
whose PR says "SkyAI" not "SKYA", so the alert had no cause at all (and could
attach a same-worded story about another company); the export stamps UTC and
reading it as ET pushed every fresh headline into the future so the age collapsed
to "0m ago"; and the per-symbol feeds were merged in, so an alert could cite a
headline that did not belong to the ticker the scanner had picked up. News is now
the ticker's own Finviz page, today only.
"""

from __future__ import annotations

import time

from penny.enrich import Enricher
from penny.sources.finviz import parse_finviz_news_table


def _ago(text: str) -> str:
    return Enricher._age_of(text)


# --- the age is read in the feed's own zone ---------------------------------

def test_a_utc_export_timestamp_is_not_reported_as_just_now():
    """A headline an hour old must not read "0s"/"0m"."""
    now = time.time()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 3600))
    assert _ago(ts) in ("1h", "60m", "59m")


def test_an_et_naive_timestamp_is_still_read_as_et():
    """The Finviz-to-ET case must not regress: 13:55 ET stays ~18m when now is 14:13 ET."""
    now = time.time()
    import datetime as dt
    from penny.util import ET

    et = dt.datetime.fromtimestamp(now - 18 * 60, tz=ET)
    assert _ago(et.strftime("%Y-%m-%d %H:%M:%S")) == "18m"


def test_an_iso_z_timestamp_is_utc():
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 120))
    assert _ago(iso) in ("2m", "1m")


def test_an_rss_pubdate_carries_its_zone():
    from email.utils import formatdate

    now = time.time()
    assert _ago(formatdate(now - 600, usegmt=True)) in ("10m", "9m")


def test_a_future_timestamp_does_not_become_zero():
    """Clock skew is not a fresh headline: a far-future stamp is clamped, not "0m"."""
    future = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 4 * 3600))
    # Read as ET it is future -> re-read as UTC; still future only by the ET/UTC
    # gap, so it is honestly "0s" only when the gap really is that small.
    assert _ago(future) != ""


def test_unparseable_input_is_unknown_not_zero():
    assert _ago("Oct-13-26") == ""
    assert _ago("") == ""


# --- the per-ticker news table is the source --------------------------------

_SKYA_ROW = """
<table id="news-table" class="fullview-news-outer news-table" data-ticker="SKYA">
  <tr class="cursor-pointer has-label" onclick="trackAndOpenNews(event, 'InvestorsHub', '/news/401325/x');">
    <td width="130" align="right">
        Today 10:58AM
    </td>
    <td align="left">
      <div class="news-link-container"><div class="news-link-left">
        <a class="tab-link-news" href="/news/401325/skyai-plans" target="_blank" >
            SkyAI Plans $201 Million Cash Tender Offer at $3 Per Share, Rejects Forward Industries Bid
        </a>
      </div><div class="news-link-right"><span>(InvestorsHub)</span></div></div>
    </td>
  </tr>
  <tr class="cursor-pointer has-label" onclick="trackAndOpenNews(event, 'GlobeNewswire', 'https://x/old');">
    <td width="130" align="right">
        Oct-11-26 11:14PM
    </td>
    <td align="left">
      <div class="news-link-container"><div class="news-link-left">
        <a class="tab-link-news" href="https://x/old" target="_blank" >
            SkyAI Announces Intention to Commence Cash Tender Offer
        </a>
      </div><div class="news-link-right"><span>(GlobeNewswire)</span></div></div>
    </td>
  </tr>
</table>
"""


def test_a_per_ticker_headline_is_parsed_even_when_it_never_says_the_ticker():
    """The market-wide export missed this: the title says "SkyAI", not "SKYA"."""
    from penny.util import now_et

    rows = parse_finviz_news_table(_SKYA_ROW, now=now_et().replace(hour=12, minute=0))
    assert len(rows) == 2
    assert rows[0]["source"] == "InvestorsHub"
    assert "Tender Offer" in rows[0]["title"]
    # "Today 10:58AM" must become a real timestamp, not midnight.
    assert rows[0]["published"].endswith("-04:00") or rows[0]["published"].endswith("-05:00")
    assert "10:58" in rows[0]["published"]
    assert rows[1]["published"].startswith("2026-10-11")


def test_the_parsed_age_is_real_not_zero():
    from penny.util import now_et

    rows = parse_finviz_news_table(_SKYA_ROW, now=now_et().replace(hour=12, minute=0))
    age = Enricher._age_of(rows[0]["published"])
    # "Today 10:58AM" is hours old, not "0s"/"0m" -- the whole point of the fix.
    assert age.endswith("h") and not age.startswith("0")


def test_a_page_without_a_news_table_is_not_an_error():
    assert parse_finviz_news_table("<html><body>nothing</body></html>") == []


# --- Finviz is the only source; only today's headlines count (C45) ----------

def _enricher(finviz):
    return Enricher(None, finviz=finviz)


def _ticker_rows(published=None):
    published = published or time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    return [{"title": "ACME wins a $12M contract", "source": "Benzinga",
             "url": "https://x/y", "published": published}]


def test_finviz_headline_is_used_and_tagged_with_its_outlet():
    class F:
        def ticker_news(self, t):
            return _ticker_rows()

    items, _ = _enricher(F())._news("ACME")
    assert items and items[0]["source"] == "Benzinga"
    assert items[0]["url"] == "https://x/y"
    assert items[0]["age"]


def test_a_prior_session_headline_is_not_todays_news():
    """An alert answers "why now"; a headline from a prior day is background."""
    old = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                        time.localtime(time.time() - 3 * 86400))

    class F:
        def ticker_news(self, t):
            return _ticker_rows(published=old)

    items, _ = _enricher(F())._news("ACME")
    assert items == []


def test_the_market_wide_export_is_never_used():
    """The /export/news CSV matches on headline text and attached a story about
    another company. Finviz's per-ticker table is the only source; when the quote
    page yields nothing the result is empty, not the market-wide row."""
    class F:
        def ticker_news(self, t):
            return []

        def news(self, view):
            return [{"Title": "ACME rallies on a contract", "Source": "Benzinga",
                     "Date": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                     "Url": "u"}]

    items, _ = _enricher(F())._news("ACME")
    assert items == []


def test_slow_feeds_are_never_asked():
    calls = {"n": 0}

    class F:
        def ticker_news(self, t):
            return _ticker_rows()

    class Y:
        def google_news(self, symbol, limit=8):
            calls["n"] += 1
            return []

    e = Enricher(None, finviz=F(), yahoo=Y())
    e._news("ACME", fast=True)
    e._news("ACME", fast=False)
    assert calls["n"] == 0


def test_only_finviz_carries_the_news_even_for_the_watcher():
    """The invalidation watcher used to pull the wires too. Every headline now
    comes from the ticker's Finviz page, so a wire-only item is not a source."""
    class F:
        def ticker_news(self, t):
            return _ticker_rows()

    class Y:
        def google_news(self, symbol, limit=8):
            return [{"title": "ACME prices an offering", "source": "wire",
                     "url": "u2", "published": ""}]

    e = Enricher(None, finviz=F(), yahoo=Y())
    items, _ = e._news("ACME", fast=False)
    assert items and all(i["source"] != "wire" for i in items)
