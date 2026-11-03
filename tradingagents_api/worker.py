"""Runs one analysis in its own process: ``python -m tradingagents_api.worker RUN_DIR``.

A process per run keeps each run's configuration, memory and failures apart from
the server and from other runs, and lets the server stop a run that hangs.

Reads ``RUN_DIR/job.json`` (written by the server, already validated) and writes
``RUN_DIR/report/``, ``RUN_DIR/report.zip`` and ``RUN_DIR/result.json``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import zipfile
from pathlib import Path


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _zip_dir(source: Path, target: Path, extra: dict[str, Path]) -> None:
    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source).as_posix())
        for name, path in extra.items():
            if path.is_file():
                zf.write(path, name)
    os.replace(tmp, target)


def run(run_dir: Path) -> dict:
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))

    from tradingagents.default_config import build_default_config
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    config = build_default_config()
    config.update(job["options"])
    # Runs from the API never resume each other's checkpoints.
    config["checkpoint_enabled"] = False

    portfolio = (PortfolioContext.model_validate(job["portfolio"])
                 if job.get("portfolio") is not None else None)

    graph = TradingAgentsGraph(job["analysts"], config=config)
    state, decision = graph.propagate(job["ticker"], job["date"], asset_type=job["asset_type"],
                                      portfolio=portfolio)

    report_dir = run_dir / "report"
    graph.save_reports(state, job["ticker"], save_path=report_dir, html=job.get("html", True))

    summary = {
        "ticker": job["ticker"],
        "date": job["date"],
        "asset_type": job["asset_type"],
        "analysts": job["analysts"],
        "decision": decision,
        "settings": graph.run_settings(),
        "portfolio": job.get("portfolio"),
    }
    _write_json(report_dir / "run.json", summary)
    _zip_dir(report_dir, run_dir / "report.zip", {})
    return {"decision": decision}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tradingagents_api.worker RUN_DIR", file=sys.stderr)
        return 2
    run_dir = Path(argv[1])
    try:
        result = run(run_dir)
    except BaseException as exc:  # noqa: BLE001 - every failure must reach result.json
        traceback.print_exc()
        _write_json(run_dir / "result.json", {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _write_json(run_dir / "result.json", {"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
