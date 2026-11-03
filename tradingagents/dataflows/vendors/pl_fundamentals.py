"""Financial statements of Warsaw (GPW, ``.WA``) listings, from BiznesRadar.pl.

Yahoo's statements for Warsaw listings lag and mislead: quarters go missing, the
latest reports arrive months late, and IFRS 16 lease liabilities move in and out
of "total debt" between periods, so a restaurant chain's leases read as a loan
taken one quarter and repaid the next. BiznesRadar.pl publishes every Warsaw
company's statements standardized from its periodic reports, in thousands of
PLN, with lease liabilities on their own lines and each quarter on its own.

Only the free part of each page is read: rows BiznesRadar reserves for its
paid plan (EBITDA, free cash flow, right-of-use assets, publication dates)
are skipped, even where the page's markup carries them. Each page is fetched
at most once a day per company and served from the cache otherwise.

The pages give no publication date in their free part, so a past run sees a
period only once the statutory deadline for its report has passed (60 days
after a quarter, 3 months after a half-year, 4 months after a year): a report
filed early is withheld until then, but none is served before it was public.

Any ticker that is not a Warsaw listing raises ``NoMarketDataError``, which
hands it to the next vendor in the chain.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

BASE_URL = "https://www.biznesradar.pl"
_PAGES = {
    "balance_sheet": "raporty-finansowe-bilans",
    "income_statement": "raporty-finansowe-rachunek-zyskow-i-strat",
    "cashflow": "raporty-finansowe-przeplywy-pieniezne",
}
_TITLES = {
    "balance_sheet": "Balance Sheet",
    "income_statement": "Income Statement",
    "cashflow": "Cash Flow Statement",
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (TradingAgents pl_fundamentals; personal research)"}
_TIMEOUT = 20
_CACHE_TTL_SECONDS = 24 * 60 * 60
_PERIODS_SHOWN = {"quarterly": 8, "annual": 5}

_MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
           "lip": 7, "sie": 8, "wrz": 9, "paz": 10, "paź": 10, "lis": 11, "gru": 12}
# "2026/Q2 (cze 26)" for a quarter, "2025 (gru 25)" for a year.
_PERIOD = re.compile(r"(\d{4})(?:\s*/\s*Q([1-4]))?\s*\(\s*(\w+)\s+(\d{2})\s*\)")

# Days after a period's end by which its report must be public (Polish rules
# for issuers on the regulated market): a quarter 60, a half-year (the second
# quarter) 3 months, a year 4 months.
_DEADLINE_DAYS = {"1": 60, "2": 92, "3": 60, "4": 122, None: 122}

# English names for BiznesRadar's fields; a field missing here keeps its Polish
# name, prefixed with its section where the name alone repeats across sections.
_LABELS = {
    # balance sheet
    "BalanceNoncurrentAssets": "Non-current assets",
    "BalanceIntangibleAssets": "Intangible assets",
    "BalanceProperty": "Property, plant and equipment",
    "BalanceNoncurrentReceivables": "Non-current receivables",
    "BalanceNoncurrInvestments": "Non-current investments",
    "BalanceOtherNoncurrentAssets": "Other non-current assets",
    "BalanceCurrentAssets": "Current assets",
    "BalanceInventory": "Inventories",
    "BalanceCurrentReceivables": "Current receivables",
    "BalanceCurrentInvestments": "Current investments",
    "BalanceCash": "Cash and cash equivalents",
    "BalanceOtherCurrentAssets": "Other current assets",
    "BalanceAssetsForSale": "Non-current assets held for sale",
    "BalanceTotalAssets": "Total assets",
    "BalanceCapital": "Equity attributable to owners of the parent",
    "BalanceShareCapital": "Share capital",
    "BalanceOwnShares": "Treasury shares",
    "BalanceReserve": "Supplementary capital",
    "BalanceNonshareCapital": "Non-controlling interests",
    "BalanceNoncurrentLiabilities": "Non-current liabilities",
    "BalanceNoncurrentTradeLiabilities": "Non-current liabilities: trade payables",
    "BalanceNoncurrentBorrowings": "Non-current liabilities: bank loans and borrowings",
    "BalanceNoncurrentObligations": "Non-current liabilities: bonds issued",
    "BalanceNoncurrentLeasing": "Non-current liabilities: lease liabilities",
    "BalanceNoncurrentOtherLiabilities": "Non-current liabilities: other",
    "BalanceCurrentLiabilities": "Current liabilities",
    "BalanceCurrentTradePayablesv": "Current liabilities: trade payables",
    "BalanceCurrentBorrowings": "Current liabilities: bank loans and borrowings",
    "BalanceCurrentObligations": "Current liabilities: bonds issued",
    "BalanceCurrentLeasing": "Current liabilities: lease liabilities",
    "BalanceCurrentOtherLiabilities": "Current liabilities: other",
    "BalanceReckoning": "Accruals and deferred income",
    "BalanceTotalEquityAndLiabilities": "Total equity and liabilities",
    # income statement
    "IncomeRevenues": "Revenue",
    "IncomeCostOfSales": "Cost of sales",
    "IncomeDistributionExpenses": "Selling expenses",
    "IncomeAdministrativExpenses": "General and administrative expenses",
    "IncomeGrossProfit": "Profit on sales",
    "IncomeOtherOperatingIncome": "Other operating income",
    "IncomeOtherOperatingCosts": "Other operating expenses",
    "IncomeEBIT": "Operating profit (EBIT)",
    "IncomeFinanceIncome": "Finance income",
    "IncomeFinanceCosts": "Finance costs",
    "IncomeOtherIncome": "Other income (expenses)",
    "IncomeNetGrossProfit": "Profit on business activity",
    "IncomeExtraordinarProfit": "Extraordinary items",
    "IncomeBeforeTaxProfit": "Profit before tax",
    "IncomeDiscontinuedProfit": "Net profit (loss) from discontinued operations",
    "IncomeNetProfit": "Net profit",
    "IncomeShareholderNetProfit": "Net profit attributable to owners of the parent",
    # cash flow
    "CashflowOperatingCashflow": "Operating cash flow",
    "CashflowAmortization": "Depreciation and amortization",
    "CashflowInvestingCashflow": "Investing cash flow",
    "CashflowCapex": "Capital expenditure (intangible and tangible assets)",
    "CashflowFinancingCashflow": "Financing cash flow",
    "CashflowShareCapitalCash": "Share issuance",
    "CashflowDividend": "Dividends paid",
    "CashflowNetCashflow": "Net cash flow",
}

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class _ReportTable(HTMLParser):
    """The first ``table.report-table`` of a page: its period headers and rows.

    A row is ``(field, label, premium, bold, cells)``; ``bold`` marks a total
    that opens a section of the statement. A cell holds the text of its
    ``.value`` element only, so the year-on-year and sector comparisons printed
    under each figure are left out.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[tuple[str, str, bool, bool, list[str | None]]] = []
        self.found = False
        self._stack: list[tuple[str, bool]] = []    # (tag, opens a .value)
        self._in_table = False
        self._done = False
        self._row: dict | None = None
        self._cell: dict | None = None
        self._header: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if self._done or tag in _VOID_TAGS:
            return
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "table" and not self._in_table and "report-table" in classes:
            self._in_table = self.found = True
            self._stack = []
        if not self._in_table:
            return
        self._stack.append((tag, "value" in classes))
        if tag == "tr":
            self._row = {"field": attrs.get("data-field") or "", "premium": "premium-row" in classes,
                         "bold": bool({"bold", "strong"} & set(classes)), "label": [], "cells": []}
        elif tag == "th" and "thq" in classes:
            self._header = []
        elif tag == "td" and self._row is not None:
            kind = "label" if "f" in classes else "value" if "h" in classes else None
            self._cell = {"kind": kind, "text": [], "premium": False}
        elif self._cell is not None and any(c.startswith("premium-value") for c in classes):
            self._cell["premium"] = True

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing tags carry no text

    def handle_endtag(self, tag):
        if self._done or not self._in_table or tag in _VOID_TAGS:
            return
        while self._stack:                      # tolerate unclosed inner tags
            open_tag, _ = self._stack.pop()
            if open_tag == tag:
                break
        if tag == "th" and self._header is not None:
            self.headers.append(" ".join("".join(self._header).split()))
            self._header = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            text = " ".join("".join(self._cell["text"]).split())
            if self._cell["kind"] == "label":
                self._row["label"].append(text)
            elif self._cell["kind"] == "value":
                self._row["cells"].append(None if self._cell["premium"] else text)
                self._row["premium"] |= self._cell["premium"]
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row["field"]:
                self.rows.append((self._row["field"], " ".join(self._row["label"]),
                                  self._row["premium"], self._row["bold"], self._row["cells"]))
            self._row = None
        elif tag == "table":
            self._in_table, self._done = False, True

    def handle_data(self, data):
        if self._header is not None:
            self._header.append(data)
        elif self._cell is not None and (
                self._cell["kind"] == "label" or any(is_value for _, is_value in self._stack)):
            self._cell["text"].append(data)


def parse_report(html: str) -> tuple[list[str], list[tuple[str, str, bool, bool, list[str | None]]]]:
    """(period headers, rows) of a BiznesRadar report page."""
    parser = _ReportTable()
    parser.feed(html)
    parser.close()
    if not parser.found:
        raise VendorUnavailableError("BiznesRadar page has no report table; its layout may have changed")
    return parser.headers, parser.rows


def parse_period(header: str) -> tuple[date, str | None] | None:
    """(period end, fiscal quarter or None for a year) of a column header."""
    match = _PERIOD.search(header)
    if not match:
        return None
    _, quarter, month_name, year = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    year = 2000 + int(year)
    return date(year, month, calendar.monthrange(year, month)[1]), quarter


def public_by(period_end: date, quarter: str | None) -> date:
    """The day by which the report for a period must have been published."""
    return period_end + timedelta(days=_DEADLINE_DAYS[quarter])


def _number(text: str | None) -> str:
    """A figure as a plain CSV number: '-1 206' -> '-1206'; blank when absent."""
    if not text:
        return ""
    cleaned = re.sub(r"[\s  ]", "", text).replace(",", ".")
    return cleaned if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned) else ""


def _cache_path(name: str) -> Path:
    return Path(get_config()["data_cache_dir"]) / "biznesradar" / name


def _fresh(path: Path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < _CACHE_TTL_SECONDS


def _get(url: str) -> requests.Response:
    try:
        response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"BiznesRadar request failed: {type(exc).__name__}") from exc
    if response.status_code != 404 and response.status_code >= 400:
        raise VendorUnavailableError(f"BiznesRadar answered {response.status_code} for {url}")
    return response


def company_slug(code: str) -> str | None:
    """BiznesRadar's name for a Warsaw ticker ('MEX' -> 'MEX-POLSKA'), None when unknown.

    The site redirects a ticker to the company's page, and a period suffix such
    as ',Q' is dropped by that redirect, so the name is resolved first.
    """
    path = _cache_path("slugs.json")
    try:
        slugs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        slugs = {}
    if code in slugs:
        return slugs[code]
    response = _get(f"{BASE_URL}/{_PAGES['balance_sheet']}/{code}")
    if response.status_code == 404 or "report-table" not in response.text:
        return None
    slug = urlparse(response.url).path.rstrip("/").rsplit("/", 1)[-1].split(",")[0] or code
    slugs[code] = slug
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(slugs), encoding="utf-8"))
    return slug


def _page(kind: str, slug: str, quarterly: bool) -> str:
    """A report page's HTML, from the day's cache when there is one."""
    suffix = ",Q" if quarterly else ""
    path = _cache_path(f"{slug}-{kind}{'-Q' if quarterly else '-Y'}.html")
    if _fresh(path):
        return path.read_text(encoding="utf-8")
    response = _get(f"{BASE_URL}/{_PAGES[kind]}/{slug}{suffix}")
    if response.status_code == 404:
        raise VendorUnavailableError(f"BiznesRadar has no {kind} page for {slug}")
    html = response.text
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(html, encoding="utf-8"))
    return html


def _label(field: str, polish: str, section: str) -> str:
    """The row's English name, else its Polish one under the section it sits in."""
    if field in _LABELS:
        return _LABELS[field]
    return f"{section}: {polish}" if section and section != polish else polish


def _statement(kind: str, ticker: str, freq: str, as_of_date: str | None) -> str:
    canonical = normalize_symbol(ticker)
    if not canonical.endswith(".WA"):
        raise NoMarketDataError(ticker, canonical, "not a Warsaw (.WA) listing")
    quarterly = (freq or "quarterly").lower() == "quarterly"
    freq = "quarterly" if quarterly else "annual"
    as_of = as_of_date or get_current_date()
    historical = is_historical(as_of_date)

    slug = company_slug(canonical[:-3])
    if slug is None:
        raise NoMarketDataError(ticker, canonical, "not covered by BiznesRadar")
    headers, rows = parse_report(_page(kind, slug, quarterly))

    periods = [parse_period(h) for h in headers]
    if not periods or any(p is None for p in periods) or any((p[1] is not None) != quarterly for p in periods):
        raise VendorUnavailableError(f"BiznesRadar {freq} {kind} columns unreadable for {slug}: {headers[:3]}")
    as_of_day = date.fromisoformat(as_of)
    columns = [i for i, (end, quarter) in enumerate(periods)
               if end <= as_of_day and (not historical or public_by(end, quarter) <= as_of_day)]
    columns = columns[-_PERIODS_SHOWN[freq]:]
    if not columns:
        raise NoMarketDataError(ticker, canonical, f"no {freq} {_TITLES[kind].lower()} public by {as_of}")

    title = _TITLES[kind]
    lines = [
        f"# {title} for {canonical} ({freq}), PLN thousands",
        "# Source: BiznesRadar.pl, standardized from the company's periodic reports"
        + ("; each quarter's flows cover that quarter alone" if quarterly and kind != "balance_sheet" else ""),
    ]
    if historical:
        lines.append(f"# Point-in-time as of {as_of}: a period is shown once its statutory "
                     f"reporting deadline has passed")
    lines.append("")
    lines.append(",".join([""] + [f"{periods[i][0].isoformat()} ({headers[i].split('(')[0].strip()})"
                                  for i in columns]))
    section = ""
    for field, polish, premium, bold, cells in rows:
        if bold:
            section = polish
        if premium:
            continue
        if len(cells) != len(headers):
            logger.warning("pl_fundamentals: row %s has %d cells for %d periods; skipped",
                           field, len(cells), len(headers))
            continue
        label = _label(field, polish, section).replace(",", " ")
        lines.append(",".join([label] + [_number(cells[i]) for i in columns]))
    return "\n".join(lines) + "\n"


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Balance sheet of a Warsaw listing, in PLN thousands."""
    return _statement("balance_sheet", ticker, freq, as_of_date)


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Income statement of a Warsaw listing, in PLN thousands."""
    return _statement("income_statement", ticker, freq, as_of_date)


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    """Cash flow statement of a Warsaw listing, in PLN thousands."""
    return _statement("cashflow", ticker, freq, as_of_date)
