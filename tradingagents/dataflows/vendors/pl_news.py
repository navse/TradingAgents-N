"""Polish company news for Warsaw (GPW, ``.WA``) listings.

Yahoo tags almost no articles with Warsaw symbols, so its feed leaves the news
analyst without news for most of the market. This vendor reads two keyless
Polish sources instead:

- Bankier.pl's feed of company filings (ESPI/EBI current reports), matched to
  the company by the issuer name that opens each title. The feed holds only the
  latest filings, so it serves recent windows.
- Google News search in Polish for the company name, limited to the window by
  the ``after:``/``before:`` operators, so historical windows are served too.

Any ticker that is not a Warsaw listing raises ``VendorUnavailableError``, which
hands it to the next vendor in the chain (``news_data = "pl_news,yfinance"``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import quote_plus

import requests
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

logger = logging.getLogger(__name__)

ESPI_FEED_URL = "https://www.bankier.pl/rss/espi.xml"
GOOGLE_NEWS_URL = (
    "https://news.google.com/rss/search?q={query}&hl=pl&gl=PL&ceid=PL:pl"
)
_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0 (TradingAgents pl_news)"}

# Legal-form words that differ between Yahoo's name and a filing's issuer name
# ("CD PROJEKT SA" vs "CD PROJEKT S.A."), dropped before names are compared.
_LEGAL_FORMS = {
    "SA", "S", "A", "SPOLKA", "AKCYJNA", "SE", "NV", "N", "V", "PLC", "AG",
    "SP", "Z", "O", "OO", "SKA", "ASI", "GROUP", "GRUPA",
}


def _fold(text: str) -> str:
    """Upper-case ASCII words only: diacritics, punctuation and legal forms dropped."""
    text = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    words = re.findall(r"[A-Z0-9]+", text)
    return " ".join(w for w in words if w not in _LEGAL_FORMS)


def search_term(name: str) -> str:
    """``name`` without its legal form, diacritics kept: what the press writes."""
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", name)
    kept = [w for w in words if _fold(w)]
    return " ".join(kept) or name


def _same_issuer(a: str, b: str) -> bool:
    """Whether two folded names are the same issuer, one possibly abbreviated."""
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def is_warsaw_listing(ticker: str) -> bool:
    return normalize_symbol(ticker).endswith(".WA")


@lru_cache(maxsize=256)
def _yahoo_short_name(canonical: str) -> str | None:
    info = yf_retry(lambda: yf.Ticker(canonical).info) or {}
    return info.get("shortName") or info.get("longName")


def company_name(ticker: str) -> str:
    """The name to search the Polish press for: configured, then Yahoo's, then the code."""
    canonical = normalize_symbol(ticker)
    configured = get_config().get("pl_news_company_names") or {}
    for key in (ticker, ticker.upper(), canonical):
        if key in configured:
            return configured[key]
    try:
        name = _yahoo_short_name(canonical)
    except Exception as exc:  # a missing name only narrows the search
        logger.warning("pl_news: no Yahoo name for %s: %s", canonical, exc)
        name = None
    return name or canonical.split(".")[0]


def _parse_rss(payload: bytes) -> list[dict]:
    """Items of an RSS 2.0 document. Documents declaring a DTD are refused, so no
    entity expansion can happen while parsing."""
    head = payload[:2048].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise VendorUnavailableError("RSS feed declares a DTD; refused")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise VendorUnavailableError(f"unreadable RSS feed: {exc}") from exc
    items = []
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        pub_date = None
        if pub:
            try:
                pub_date = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                pub_date = None
        source = item.find("source")
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "summary": re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip(),
            "publisher": source.text.strip() if source is not None and source.text else "",
            "pub_date": pub_date,
        })
    return items


def _fetch(url: str) -> bytes:
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"{url.split('?')[0]}: {exc}") from exc
    return response.content


def espi_filings(name: str) -> list[dict]:
    """Recent ESPI/EBI filings whose issuer (the title before ':') is ``name``."""
    wanted = _fold(name)
    filings = []
    for item in _parse_rss(_fetch(ESPI_FEED_URL)):
        issuer, sep, subject = item["title"].partition(":")
        if sep and _same_issuer(_fold(issuer), wanted):
            filings.append({
                **item,
                "title": subject.strip() or item["title"],
                "publisher": "ESPI/EBI (Bankier.pl)",
                "summary": "",
            })
    return filings


def press_articles(name: str, start_date: str, end_date: str) -> list[dict]:
    """Google News (Polish edition) articles naming the company inside the window."""
    before = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    query = quote_plus(f'"{search_term(name)}" after:{start_date} before:{before}')
    articles = []
    for item in _parse_rss(_fetch(GOOGLE_NEWS_URL.format(query=query))):
        title = item["title"]
        # Google appends " - Publisher" to every title; keep it once, as the source.
        if item["publisher"] and title.endswith(f" - {item['publisher']}"):
            title = title[: -len(item["publisher"]) - 3]
        articles.append({**item, "title": title, "summary": ""})
    return articles


def _format(articles: list[dict]) -> str:
    out = ""
    for a in articles:
        when = a["pub_date"].strftime("%Y-%m-%d") if a["pub_date"] else "undated"
        out += f"### {a['title']} (source: {a['publisher'] or 'unknown'}, {when})\n"
        if a["summary"]:
            out += f"{a['summary']}\n"
        if a["link"]:
            out += f"Link: {a['link']}\n"
        out += "\n"
    return out


def get_news_pl(ticker: str, start_date: str, end_date: str) -> str:
    """ESPI/EBI filings and Polish press about a Warsaw-listed company in the window."""
    if not is_warsaw_listing(ticker):
        raise VendorUnavailableError(f"pl_news serves Warsaw (.WA) listings only, not {ticker}")

    name = company_name(ticker)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    limit = get_config().get("news_article_limit", 20)

    filings, press, errors = [], [], []
    try:
        filings = [a for a in espi_filings(name) if in_window(a["pub_date"], start_dt, end_dt)]
    except VendorUnavailableError as exc:
        errors.append(f"ESPI feed: {exc}")
    try:
        press = [a for a in press_articles(name, start_date, end_date)
                 if in_window(a["pub_date"], start_dt, end_dt)]
    except VendorUnavailableError as exc:
        errors.append(f"Google News: {exc}")
    if len(errors) == 2:
        raise VendorUnavailableError("; ".join(errors))

    newest_first = lambda a: a["pub_date"].timestamp() if a["pub_date"] else 0.0  # noqa: E731
    filings.sort(key=newest_first, reverse=True)
    press.sort(key=newest_first, reverse=True)

    header = f"## {ticker} ({name}) news from Polish sources, {start_date} to {end_date}"
    body = ""
    if filings:
        body += "### Company filings (ESPI/EBI current reports)\n\n" + _format(filings[:limit])
    if press:
        body += "### Polish press\n\n" + _format(press[: max(limit - len(filings), 5)])
    notes = "".join(f"\n<{e} unavailable; this is not an absence of news>" for e in errors)
    if not body:
        return (f"No Polish news or filings found for {ticker} ({name}) between "
                f"{start_date} and {end_date}. The filings feed covers only recent days.{notes}")
    return f"{header}:\n\n{body}{notes}".rstrip() + "\n"
