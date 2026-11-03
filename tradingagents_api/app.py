"""The HTTP API. Run with ``tradingagents-api`` (or ``uvicorn tradingagents_api.app:app``)."""

from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from cli.models import AnalystType, AssetType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type, parse_analysts
from tradingagents import __version__
from tradingagents.portfolio import PortfolioContext
from tradingagents_api.jobs import JobManager, public_status
from tradingagents_api.models import RunRequest
from tradingagents_api.settings import Settings, load_settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    manager = JobManager(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        yield
        manager.shutdown()

    docs = {} if settings.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="TradingAgents API", version=__version__, lifespan=lifespan, **docs)
    app.state.manager = manager
    app.state.settings = settings

    def require_key(
        key: Annotated[str | None, Security(_api_key_header)],
        bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
    ) -> None:
        supplied = key or (bearer.credentials if bearer else None)
        if not supplied or not any(hmac.compare_digest(supplied, k) for k in settings.api_keys):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid API key")

    auth = [Depends(require_key)]

    def base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def found(run_id: str) -> dict:
        status_ = manager.get(run_id)
        if status_ is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
        return status_

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    # -- runs ------------------------------------------------------------------

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED, dependencies=auth)
    def create_run(body: RunRequest, request: Request) -> dict:
        kind = AssetType(body.asset_type) if body.asset_type else detect_asset_type(body.ticker)
        try:
            analysts = (parse_analysts(",".join(body.analysts), kind) if body.analysts
                        else filter_analysts_for_asset_type(list(AnalystType), kind))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

        trade_date = body.date or datetime.now(UTC).date().isoformat()
        if trade_date > datetime.now(UTC).date().isoformat():
            raise HTTPException(422, "date cannot be in the future")

        portfolio, source = body.portfolio, "request" if body.portfolio else None
        if portfolio is None and body.use_saved_portfolio and settings.portfolio_path.is_file():
            portfolio, source = _load_saved(settings), "saved"

        job = {
            "ticker": body.ticker,
            "date": trade_date,
            "asset_type": kind.value,
            "analysts": [a.value for a in analysts],
            "portfolio": portfolio.model_dump() if portfolio else None,
            "portfolio_source": source,
            "options": body.options.overrides(),
            "html": body.html,
        }
        return public_status(manager.submit(job, body.callback_url), base_url(request))

    @app.get("/runs", dependencies=auth)
    def list_runs(request: Request, limit: int = 50) -> list[dict]:
        return [public_status(s, base_url(request)) for s in manager.list(max(1, min(limit, 500)))]

    @app.get("/runs/{run_id}", dependencies=auth)
    def get_run(run_id: str, request: Request) -> dict:
        return public_status(found(run_id), base_url(request))

    @app.get("/runs/{run_id}/report", dependencies=auth, response_model=None)
    def get_report(run_id: str, request: Request) -> FileResponse | JSONResponse:
        status_ = found(run_id)
        if status_["status"] != "succeeded":
            return JSONResponse(public_status(status_, base_url(request)),
                                status_code=status.HTTP_409_CONFLICT)
        filename = f"{status_['ticker']}_{status_['date']}_{run_id[:8]}.zip"
        return FileResponse(manager.run_dir(run_id) / "report.zip",
                            media_type="application/zip", filename=filename)

    @app.get("/runs/{run_id}/log", dependencies=auth, response_model=None)
    def get_log(run_id: str) -> FileResponse:
        found(run_id)
        path = manager.run_dir(run_id) / "worker.log"
        if not path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no log yet")
        return FileResponse(path, media_type="text/plain")

    @app.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=auth)
    def delete_run(run_id: str) -> None:
        found(run_id)
        manager.delete(run_id)

    # -- saved portfolio ---------------------------------------------------------

    @app.get("/portfolio", dependencies=auth)
    def get_portfolio() -> dict:
        if not settings.portfolio_path.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no portfolio saved")
        return _load_saved(settings).model_dump()

    @app.put("/portfolio", dependencies=auth)
    def put_portfolio(body: PortfolioContext) -> dict:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = settings.portfolio_path.with_name("portfolio.json.tmp")
        tmp.write_text(json.dumps(body.model_dump(), indent=2), encoding="utf-8")
        os.replace(tmp, settings.portfolio_path)
        return body.model_dump()

    @app.delete("/portfolio", status_code=status.HTTP_204_NO_CONTENT, dependencies=auth)
    def delete_portfolio() -> None:
        settings.portfolio_path.unlink(missing_ok=True)

    return app


def _load_saved(settings: Settings) -> PortfolioContext:
    try:
        return PortfolioContext.model_validate_json(settings.portfolio_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"saved portfolio is not usable: {exc}") from None


def __getattr__(name: str):
    # ``uvicorn tradingagents_api.app:app`` builds the app on first access, so
    # importing this module (e.g. in tests) needs no TA_API_KEY.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("TA_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("TA_API_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
