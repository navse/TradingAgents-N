"""pl_news: Polish filings and press for Warsaw listings, XTB-style symbols, and
the Warsaw benchmark."""

import pytest

import tradingagents.dataflows.vendors.pl_news as pl
from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory.settlement import resolve_benchmark

ESPI = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Bankier.pl - Komunikaty ze spolek</title>
<item><title>AUTO PARTNER S.A.: Otrzymanie zawiadomienia - zmiana stanu posiadania akcji</title>
<link>https://www.bankier.pl/wiadomosc/a-1.html</link>
<description>tresc</description><pubDate>Mon, 19 Oct 2026 15:05:00 +0100</pubDate></item>
<item><title>PGE POLSKA GRUPA ENERGETYCZNA S.A.: Korekta raportu biezacego</title>
<link>https://www.bankier.pl/wiadomosc/b-2.html</link>
<pubDate>Mon, 19 Oct 2026 14:25:00 +0100</pubDate></item>
<item><title>AUTO PARTNER S.A.: Raport miesieczny za wrzesien</title>
<link>https://www.bankier.pl/wiadomosc/c-3.html</link>
<pubDate>Fri, 02 Oct 2026 08:00:00 +0100</pubDate></item>
</channel></rss>"""

PRESS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Auto Partner zwiekszyl sprzedaz we wrzesniu - Bankier.pl</title>
<link>https://news.google.com/x/1</link><source url="https://www.bankier.pl">Bankier.pl</source>
<pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Spolki motoryzacyjne pod presja - Parkiet</title>
<link>https://news.google.com/x/2</link><source url="https://www.parkiet.com">Parkiet</source>
<pubDate>Sat, 17 Oct 2026 07:00:00 GMT</pubDate></item>
<item><title>Artykul spoza okna - Puls Biznesu</title>
<link>https://news.google.com/x/3</link><source url="https://www.pb.pl">Puls Biznesu</source>
<pubDate>Tue, 01 Sep 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""


@pytest.fixture
def feeds(monkeypatch):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == pl.ESPI_FEED_URL:
            return ESPI
        if url.startswith("https://news.google.com/"):
            return PRESS
        raise AssertionError(url)

    monkeypatch.setattr(pl, "_fetch", fake_fetch)
    monkeypatch.setattr(pl, "_yahoo_short_name", lambda canonical: "AUTO PARTNER SA")
    # The name comes from Yahoo here, whatever names the local config lists.
    monkeypatch.setattr(pl, "get_config", lambda: {"pl_news_company_names": {}})
    return calls


@pytest.mark.unit
def test_filings_and_press_for_a_warsaw_listing(feeds):
    out = pl.get_news_pl("APR.WA", "2026-10-01", "2026-10-19")
    assert "Company filings (ESPI/EBI" in out
    assert "Raport miesieczny za wrzesien" in out
    assert "Otrzymanie zawiadomienia" in out
    assert "PGE" not in out                      # another issuer's filing
    assert "Auto Partner zwiekszyl sprzedaz we wrzesniu (source: Bankier.pl" in out
    assert "Artykul spoza okna" not in out       # outside the window
    google = next(u for u in feeds if "news.google.com" in u)
    assert "%22AUTO+PARTNER%22" in google        # legal form dropped from the search
    assert "after%3A2026-10-01" in google and "before%3A2026-10-20" in google


@pytest.mark.unit
def test_xtb_symbol_is_served_as_warsaw(feeds):
    assert "Raport miesieczny" in pl.get_news_pl("APR.PL", "2026-10-01", "2026-10-19")


@pytest.mark.unit
def test_window_excludes_future_filings(feeds):
    out = pl.get_news_pl("APR.WA", "2026-10-01", "2026-10-10")
    assert "Raport miesieczny" in out
    assert "Otrzymanie zawiadomienia" not in out  # filed on 2026-10-19


@pytest.mark.unit
def test_other_listings_pass_to_the_next_vendor():
    with pytest.raises(VendorUnavailableError):
        pl.get_news_pl("AAPL", "2026-10-01", "2026-10-19")


@pytest.mark.unit
def test_one_source_down_is_reported_not_hidden(monkeypatch):
    def fake_fetch(url):
        if url == pl.ESPI_FEED_URL:
            raise VendorUnavailableError("timeout")
        return PRESS

    monkeypatch.setattr(pl, "_fetch", fake_fetch)
    monkeypatch.setattr(pl, "_yahoo_short_name", lambda canonical: "AUTO PARTNER SA")
    out = pl.get_news_pl("APR.WA", "2026-10-01", "2026-10-19")
    assert "Polish press" in out
    assert "ESPI feed" in out and "not an absence of news" in out


@pytest.mark.unit
def test_both_sources_down_hand_over_to_next_vendor(monkeypatch):
    def down(url):
        raise VendorUnavailableError("down")

    monkeypatch.setattr(pl, "_fetch", down)
    monkeypatch.setattr(pl, "_yahoo_short_name", lambda canonical: "X")
    with pytest.raises(VendorUnavailableError):
        pl.get_news_pl("APR.WA", "2026-10-01", "2026-10-19")


@pytest.mark.unit
def test_configured_name_wins(monkeypatch):
    monkeypatch.setattr(pl, "get_config", lambda: {"pl_news_company_names": {"MNS.WA": "Mennica Skarbowa"}})
    monkeypatch.setattr(pl, "_yahoo_short_name", lambda canonical: pytest.fail("Yahoo asked"))
    assert pl.company_name("MNS.WA") == "Mennica Skarbowa"
    assert pl.company_name("MNS.PL") == "Mennica Skarbowa"


@pytest.mark.unit
def test_dtd_documents_are_refused():
    with pytest.raises(VendorUnavailableError):
        pl._parse_rss(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>')


@pytest.mark.unit
def test_router_chain_falls_through_for_non_warsaw(monkeypatch):
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a: "yahoo news")
    monkeypatch.setattr(router, "get_config", lambda: {"data_vendors": {"news_data": "pl_news,yfinance"}, "tool_vendors": {}})
    assert router.route_to_vendor("get_news", "AAPL", "2026-10-01", "2026-10-19") == "yahoo news"


@pytest.mark.unit
@pytest.mark.parametrize("raw, canonical", [
    ("CDR.PL", "CDR.WA"), ("BN.FR", "BN.PA"), ("PRX.NL", "PRX.AS"), ("RACE.IT", "RACE.MI"),
    ("AIR.ES", "AIR.MC"), ("JMT.PT", "JMT.LS"), ("NDA.FI", "NDA.HE"), ("SMSN.UK", "SMSN.L"),
    ("ADEN.CH", "ADEN.SW"), ("AAPL.US", "AAPL"),
    # Yahoo-native and unmapped symbols are left alone.
    ("CDR.WA", "CDR.WA"), ("VWCE.DE", "VWCE.DE"), ("SAABB.SE", "SAABB.SE"), ("BRK.B", "BRK.B"),
])
def test_xtb_suffixes_map_to_yahoo(raw, canonical):
    assert normalize_symbol(raw) == canonical


@pytest.mark.unit
def test_warsaw_benchmark():
    assert resolve_benchmark("CDR.WA", DEFAULT_CONFIG) == "WIG20.WA"
    assert resolve_benchmark("CDR.PL", DEFAULT_CONFIG) == "WIG20.WA"
    assert resolve_benchmark("AAPL", DEFAULT_CONFIG) == "SPY"


@pytest.mark.unit
def test_yahoo_feed_codes_are_not_used_as_names(monkeypatch):
    class FakeTicker:
        def __init__(self, info):
            self.info = info

    pl._yahoo_short_name.cache_clear()
    monkeypatch.setattr(pl.yf, "Ticker", lambda s: FakeTicker({"shortName": "APR C.XWAR PLN", "longName": "Auto Partner SA"}))
    assert pl._yahoo_short_name("APR.WA") == "Auto Partner SA"
    pl._yahoo_short_name.cache_clear()
    monkeypatch.setattr(pl.yf, "Ticker", lambda s: FakeTicker({"shortName": "APR C.XWAR PLN"}))
    assert pl._yahoo_short_name("APR.WA") is None
    monkeypatch.setattr(pl, "get_config", lambda: {})
    assert pl.company_name("APR.WA") == "APR"
    pl._yahoo_short_name.cache_clear()
