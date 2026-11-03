"""pl_fundamentals: Warsaw listings' statements from BiznesRadar.pl, free rows only,
point-in-time by statutory reporting deadlines."""

import copy
from types import SimpleNamespace
from unittest import mock

import pytest

import tradingagents.dataflows.vendors.pl_fundamentals as pl
from tradingagents import default_config
from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

TODAY = "2026-10-19"


def _cell(value, premium=False, newest=False, compare=True):
    """A value cell as BiznesRadar marks it up, comparisons under the figure included."""
    inner = (f'<span data-products="1,3" class="premium-value premium-value--1 premium-value--3"><span>{value}</span></span>'
             if premium else f'<span class="pv"><span>{value}</span></span>')
    extra = ('<div class="changeyy">r/r <span class="pv"><span><span class="q_ch_per cplus">+24.43%</span></span></span>'
             '<span class="sectorv">~branża <span class="pv"><span><span class="q_ch_per cminus">-2.04%</span></span></span></span></div>'
             if compare else "")
    return f'<td class="h{" newest" if newest else ""}"><span class="value">{inner}</span>{extra}</td>'


def _row(field, label, values, bold=False, premium=False):
    classes = " ".join(c for c in ("bold" if bold else "", "premium-row premium-row--1" if premium else "") if c)
    name = f'<a href="/x" title="{label}">{label}</a>'
    name = f"<strong>{name}</strong>" if bold else name
    cells = "".join(_cell(v, premium, i == len(values) - 1) for i, v in enumerate(values))
    return f'<tr class="{classes}" data-field="{field}"><td class="f">{name}</td>{cells}<td class="ch"><canvas></canvas></td></tr>\n'


def _table(headers, rows, report_type="Q"):
    ths = "".join(f'<th class="thq h">\n{h.split(" ")[0]}\n<span>{h[h.index("("):]}</span></th>\n' for h in headers)
    return (f'<html><body><table class="other"><tr><td>menu</td></tr></table>'
            f'<table class="report-table" data-symbol="MEX" data-report-type="{report_type}">\n<tbody><tr>\n'
            f'<th class="thname"><a class="prev_link" href="javascript:void(0)"></a></th>\n{ths}'
            f'<th class="thchart"><a class="next_link" href="javascript:void(0)"></a></th>\n</tr>\n'
            + "".join(rows) + "</tbody></table></body></html>")


QUARTERS = ["2025/Q4 (gru 25)", "2026/Q1 (mar 26)", "2026/Q2 (cze 26)"]

INCOME_Q = _table(QUARTERS, [
    _row("PrimaryReport", "Data publikacji", ["2026-04-15", "2026-05-27", "2026-09-23"], premium=True),
    _row("IncomeRevenues", "Przychody ze sprzedaży", ["34 036", "29 535", "34 541"], bold=True),
    _row("IncomeEBIT", "Zysk operacyjny (EBIT)", ["1 604", "-118", "69"], bold=True),
    _row("IncomeFinanceCosts", "Koszty finansowe", ["868", "1 090", "1 048"]),
    _row("IncomeNetProfit", "Zysk netto", ["367", "-1 189", "-795"], bold=True),
    _row("IncomeEBITDA", "EBITDA", ["4 661", "2 948", "3 298"], bold=True, premium=True),
])

BALANCE_Q = _table(QUARTERS, [
    _row("BalanceTotalAssets", "Aktywa razem", ["85 298", "85 834", "90 094"], bold=True),
    _row("BalanceRightToUseAssets", "Aktywa z tytułu prawa do użytkowania", ["44 330", "45 073", "47 566"], premium=True),
    _row("BalanceNoncurrentLiabilities", "Zobowiązania długoterminowe", ["43 279", "43 996", "46 670"], bold=True),
    _row("BalanceNoncurrentBorrowings", "Kredyty i pożyczki", ["257", "315", "602"]),
    _row("BalanceNoncurrentLeasing", "Zobowiązania z tytułu leasingu finansowego", ["41 249", "42 065", "44 616"]),
    _row("BalanceNoncurrentSomethingNew", "Z tytułu instrumentów pochodnych", ["1", "2", "3"]),
    _row("BalanceCurrentLiabilities", "Zobowiązania krótkoterminowe", ["25 429", "26 436", "29 484"], bold=True),
    _row("BalanceCurrentLeasing", "Zobowiązania z tytułu leasingu finansowego", ["8 436", "8 844", "8 890"]),
])

INCOME_Y = _table(["2024 (gru 24)", "2025 (gru 25)"], [
    _row("IncomeRevenues", "Przychody ze sprzedaży", ["119 470", "135 012"], bold=True),
], report_type="Y")


@pytest.fixture
def site(monkeypatch, tmp_path):
    """BiznesRadar served from fixtures: MEX redirects to MEX-POLSKA, XYZ is unknown."""
    pages = {
        "raporty-finansowe-bilans/MEX": ("https://www.biznesradar.pl/raporty-finansowe-bilans/MEX-POLSKA", BALANCE_Q),
        "raporty-finansowe-bilans/MEX-POLSKA,Q": (None, BALANCE_Q),
        "raporty-finansowe-rachunek-zyskow-i-strat/MEX-POLSKA,Q": (None, INCOME_Q),
        "raporty-finansowe-rachunek-zyskow-i-strat/MEX-POLSKA": (None, INCOME_Y),
    }
    calls = []

    def fake_get(url):
        calls.append(url)
        path = url.removeprefix(pl.BASE_URL + "/")
        if path not in pages:
            return SimpleNamespace(status_code=404, text="<html>404</html>", url=url)
        final, html = pages[path]
        return SimpleNamespace(status_code=200, text=html, url=final or url)

    monkeypatch.setattr(pl, "_get", fake_get)
    monkeypatch.setattr(pl, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(pl, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(date_window, "get_current_date", lambda: TODAY)
    return calls


@pytest.mark.unit
def test_income_statement_reads_free_rows_only(site):
    out = pl.get_income_statement("MEX.WA", "quarterly", TODAY)
    assert "# Income Statement for MEX.WA (quarterly), PLN thousands" in out
    assert ",2025-12-31 (2025/Q4),2026-03-31 (2026/Q1),2026-06-30 (2026/Q2)" in out
    assert "Revenue,34036,29535,34541" in out
    assert "Net profit,367,-1189,-795" in out
    assert "each quarter's flows cover that quarter alone" in out
    # Paid-plan rows stay out even though the markup carries them.
    assert "EBITDA" not in out and "4661" not in out and "2026-09-23" not in out
    # The comparisons printed under each figure are not figures.
    assert "24.43" not in out and "r/r" not in out


@pytest.mark.unit
def test_balance_sheet_shows_leases_apart_from_loans(site):
    out = pl.get_balance_sheet("MEX.WA", "quarterly", TODAY)
    assert "Non-current liabilities: bank loans and borrowings,257,315,602" in out
    assert "Non-current liabilities: lease liabilities,41249,42065,44616" in out
    assert "Current liabilities: lease liabilities,8436,8844,8890" in out
    assert "prawa do użytkowania" not in out and "44330" not in out          # paid row
    # A field without an English name keeps its Polish one under its section.
    assert "Zobowiązania długoterminowe: Z tytułu instrumentów pochodnych,1,2,3" in out


@pytest.mark.unit
def test_xtb_symbol_and_slug_cache(site):
    pl.get_balance_sheet("MEX.PL", "quarterly", TODAY)
    pl.get_balance_sheet("MEX.WA", "quarterly", TODAY)
    assert site.count(pl.BASE_URL + "/raporty-finansowe-bilans/MEX") == 1          # name resolved once
    assert site.count(pl.BASE_URL + "/raporty-finansowe-bilans/MEX-POLSKA,Q") == 1  # page fetched once a day


@pytest.mark.unit
def test_annual_statement(site):
    out = pl.get_income_statement("MEX.WA", "annual", TODAY)
    assert ",2024-12-31 (2024),2025-12-31 (2025)" in out and "Revenue,119470,135012" in out
    assert "that quarter alone" not in out


@pytest.mark.unit
def test_past_run_sees_a_period_only_after_its_reporting_deadline(site):
    # Q2 2026 (a half-year) is due by 2026-09-30; Q1 by 2026-05-30; Q4 2025 by 2026-05-02.
    out = pl.get_income_statement("MEX.WA", "quarterly", "2026-08-15")
    assert "Point-in-time as of 2026-08-15" in out
    assert "Revenue,34036,29535\n" in out and "2026-06-30" not in out
    out = pl.get_income_statement("MEX.WA", "quarterly", "2026-05-10")
    assert ",2025-12-31 (2025/Q4)\n" in out and "Revenue,34036\n" in out


@pytest.mark.unit
def test_past_run_before_any_public_period_has_no_data(site):
    with pytest.raises(NoMarketDataError):
        pl.get_income_statement("MEX.WA", "quarterly", "2026-04-01")


@pytest.mark.unit
def test_other_listings_and_unknown_codes_pass_to_the_next_vendor(site):
    with pytest.raises(NoMarketDataError):
        pl.get_balance_sheet("AAPL", "quarterly", TODAY)
    with pytest.raises(NoMarketDataError):
        pl.get_balance_sheet("XYZ.WA", "quarterly", TODAY)


@pytest.mark.unit
def test_a_page_in_another_layout_is_unavailable_not_misread(site, monkeypatch):
    monkeypatch.setattr(pl, "_page", lambda kind, slug, quarterly: INCOME_Y)   # years where quarters were asked
    with pytest.raises(VendorUnavailableError):
        pl.get_income_statement("MEX.WA", "quarterly", TODAY)
    monkeypatch.setattr(pl, "_page", lambda kind, slug, quarterly: "<html><table></table></html>")
    with pytest.raises(VendorUnavailableError):
        pl.get_income_statement("MEX.WA", "quarterly", TODAY)


@pytest.mark.unit
def test_default_chain_serves_warsaw_statements_from_biznesradar():
    set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    def edgar(ticker, *a, **k):
        raise NoMarketDataError(ticker, ticker, "not a US SEC filer")

    chain = {"sec_edgar": edgar, "pl_fundamentals": lambda *a, **k: "biznesradar",
             "yfinance": lambda *a, **k: "yahoo"}
    with mock.patch.dict(router.VENDOR_METHODS, {"get_balance_sheet": chain}):
        assert router.route_to_vendor("get_balance_sheet", "MEX.WA", "quarterly", TODAY) == "biznesradar"


@pytest.mark.unit
@pytest.mark.parametrize("header, end, quarter, due", [
    ("2026/Q2 (cze 26)", "2026-06-30", "2", "2026-09-30"),
    ("2026/Q1 (mar 26)", "2026-03-31", "1", "2026-05-30"),
    ("2025/Q4 (gru 25)", "2025-12-31", "4", "2026-05-02"),
    ("2025 (gru 25)", "2025-12-31", None, "2026-05-02"),
    ("2025/Q3 (wrz 25)", "2025-09-30", "3", "2025-11-29"),
    ("2024 (lut 24)", "2024-02-29", None, "2024-06-30"),        # a fiscal year ending in February
])
def test_period_headers(header, end, quarter, due):
    period_end, q = pl.parse_period(header)
    assert period_end.isoformat() == end and q == quarter
    assert pl.public_by(period_end, q).isoformat() == due
