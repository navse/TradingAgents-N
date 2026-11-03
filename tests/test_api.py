"""The HTTP API: auth, run lifecycle, report download, saved portfolio.

The worker process is replaced by a stand-in script so no LLM is called.
"""

import sys
import textwrap
import time
import zipfile
from io import BytesIO

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradingagents_api.app import create_app  # noqa: E402
from tradingagents_api.jobs import JobManager  # noqa: E402
from tradingagents_api.settings import Settings  # noqa: E402

KEY = "test-key"
AUTH = {"X-API-Key": KEY}

FAKE_WORKER = textwrap.dedent("""
    import json, sys, zipfile
    from pathlib import Path
    run_dir = Path(sys.argv[1])
    job = json.loads((run_dir / "job.json").read_text())
    if job["ticker"] == "FAIL":
        (run_dir / "result.json").write_text(json.dumps({"ok": False, "error": "boom"}))
        sys.exit(1)
    with zipfile.ZipFile(run_dir / "report.zip", "w") as zf:
        zf.writestr("complete_report.md", "# report")
        zf.writestr("job.json", json.dumps(job))
    (run_dir / "result.json").write_text(json.dumps({"ok": True, "decision": "Buy"}))
""")


@pytest.fixture
def client(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setattr(JobManager, "worker_command", (sys.executable, str(script)))
    settings = Settings(api_keys=(KEY,), data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        yield c


def _wait(client, run_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}", headers=AUTH).json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.1)
    raise AssertionError("run did not finish")


@pytest.mark.unit
def test_health_needs_no_key(client):
    assert client.get("/health").json()["ok"] is True


@pytest.mark.unit
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer wrong"}])
def test_runs_need_a_valid_key(client, headers):
    assert client.post("/runs", json={"ticker": "NVDA"}, headers=headers).status_code == 401


@pytest.mark.unit
def test_bearer_token_is_accepted(client):
    assert client.get("/runs", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


@pytest.mark.unit
def test_run_lifecycle_and_report_download(client):
    created = client.post("/runs", json={"ticker": "NVDA", "date": "2026-09-01",
                                         "analysts": ["market", "sentiment"]}, headers=AUTH)
    assert created.status_code == 202
    run = created.json()
    assert run["status"] == "queued"
    assert run["analysts"] == ["market", "social"]

    done = _wait(client, run["id"])
    assert done["status"] == "succeeded"
    assert done["decision"] == "Buy"
    assert done["report_url"].endswith(f"/runs/{run['id']}/report")

    report = client.get(f"/runs/{run['id']}/report", headers=AUTH)
    assert report.status_code == 200
    assert report.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(BytesIO(report.content)).namelist()
    assert "complete_report.md" in names

    assert client.delete(f"/runs/{run['id']}", headers=AUTH).status_code == 204
    assert client.get(f"/runs/{run['id']}", headers=AUTH).status_code == 404


@pytest.mark.unit
def test_failed_run_reports_its_error(client):
    run = client.post("/runs", json={"ticker": "FAIL", "date": "2026-09-01"}, headers=AUTH).json()
    done = _wait(client, run["id"])
    assert done["status"] == "failed"
    assert done["error"] == "boom"
    assert client.get(f"/runs/{run['id']}/report", headers=AUTH).status_code == 409


@pytest.mark.unit
@pytest.mark.parametrize("body", [
    {"ticker": "../etc"},
    {"ticker": "NVDA", "date": "01-09-2026"},
    {"ticker": "NVDA", "date": "2999-01-01"},
    {"ticker": "NVDA", "analysts": ["astrology"]},
    {"ticker": "BTC-USD", "analysts": ["fundamentals"]},
    {"ticker": "NVDA", "options": {"backend_url": "http://evil"}},
])
def test_bad_requests_are_rejected(client, body):
    assert client.post("/runs", json=body, headers=AUTH).status_code == 422


@pytest.mark.unit
def test_unknown_or_malformed_run_id_is_404(client):
    assert client.get("/runs/../../etc", headers=AUTH).status_code == 404
    assert client.get("/runs/" + "0" * 32, headers=AUTH).status_code == 404


@pytest.mark.unit
def test_saved_portfolio_is_used_unless_one_is_sent(client):
    book = {"cash": 1000.0, "currency": "USD",
            "positions": [{"ticker": "NVDA", "quantity": 10, "average_price": 100.0}]}
    assert client.get("/portfolio", headers=AUTH).status_code == 404
    assert client.put("/portfolio", json=book, headers=AUTH).status_code == 200
    assert client.get("/portfolio", headers=AUTH).json()["cash"] == 1000.0

    saved = client.post("/runs", json={"ticker": "NVDA", "date": "2026-09-01"}, headers=AUTH).json()
    assert saved["portfolio_source"] == "saved"

    inline = {**book, "cash": 5.0}
    sent = client.post("/runs", json={"ticker": "NVDA", "date": "2026-09-01", "portfolio": inline},
                       headers=AUTH).json()
    assert sent["portfolio_source"] == "request"

    none = client.post("/runs", json={"ticker": "NVDA", "date": "2026-09-01",
                                      "use_saved_portfolio": False}, headers=AUTH).json()
    assert none["portfolio_source"] is None

    assert client.delete("/portfolio", headers=AUTH).status_code == 204
    assert client.get("/portfolio", headers=AUTH).status_code == 404


@pytest.mark.unit
def test_restart_fails_interrupted_runs_and_requeues_waiting_ones(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setattr(JobManager, "worker_command", (sys.executable, str(script)))
    settings = Settings(api_keys=(KEY,), data_dir=tmp_path / "data")

    first = JobManager(settings)
    monkeypatch.setattr(first._pool, "submit", lambda *a, **k: None)  # nothing runs
    job = {"ticker": "NVDA", "date": "2026-09-01", "asset_type": "stock", "analysts": ["market"],
           "portfolio": None, "portfolio_source": None, "options": {}, "html": True}
    waiting = first.submit(job, None)["id"]
    interrupted = first.submit(job, None)["id"]
    first._update(interrupted, status="running")

    with TestClient(create_app(settings)) as c:
        assert _wait(c, waiting)["status"] == "succeeded"
        body = c.get(f"/runs/{interrupted}", headers=AUTH).json()
        assert body["status"] == "failed"
        assert "restarted" in body["error"]


@pytest.mark.unit
def test_worker_writes_report_zip(tmp_path, monkeypatch):
    """The real worker, with the graph replaced."""
    import json

    from tradingagents_api import worker

    class FakeGraph:
        def __init__(self, analysts, config):
            assert config["checkpoint_enabled"] is False
            assert config["max_debate_rounds"] == 2

        def propagate(self, ticker, date, asset_type, portfolio):
            assert portfolio.cash == 1.0
            return {"state": 1}, "Hold"

        def save_reports(self, state, ticker, save_path, html):
            save_path.mkdir(parents=True)
            (save_path / "complete_report.md").write_text("# r")

        def run_settings(self):
            return {"provider": "fake"}

    monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", FakeGraph)
    (tmp_path / "job.json").write_text(json.dumps({
        "ticker": "NVDA", "date": "2026-09-01", "asset_type": "stock", "analysts": ["market"],
        "portfolio": {"cash": 1.0, "positions": []}, "options": {"max_debate_rounds": 2}, "html": False,
    }))
    assert worker.main(["worker", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "result.json").read_text()) == {"ok": True, "decision": "Hold"}
    names = zipfile.ZipFile(tmp_path / "report.zip").namelist()
    assert set(names) == {"complete_report.md", "run.json"}
