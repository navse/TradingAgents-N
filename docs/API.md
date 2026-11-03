# TradingAgents HTTP API

A small HTTP API for starting an analysis from another application, then polling until
it's done and downloading the report as a zip.

```
POST /runs                → 202 {id, status: "queued", status_url, ...}
GET  /runs/{id}           → {status: queued|running|succeeded|failed|cancelled, decision, error, report_url}
GET  /runs/{id}/report    → report.zip   (409 + status JSON while not ready)
```

Every endpoint except `GET /health` needs the key, sent as `X-API-Key: <key>` or
`Authorization: Bearer <key>`. The interactive schema is at `/docs` (turn it off with `TA_API_DOCS=false`).

## Running it

```bash
pip install ".[api]"
TA_API_KEY=change-me OPENAI_API_KEY=... tradingagents-api      # listens on 0.0.0.0:8000
```

With Docker: `docker compose -f docker-compose.api.yml up -d --build` (reads `.env`).
For Dokploy see [DEPLOY_DOKPLOY.md](DEPLOY_DOKPLOY.md).

| Variable | Default | Meaning |
|---|---|---|
| `TA_API_KEY` | (required) | API key; several comma-separated keys are all accepted (for rotation) |
| `TA_API_DATA_DIR` | `~/.tradingagents/api` | Where runs, reports and the saved portfolio are kept |
| `TA_API_MAX_CONCURRENT_RUNS` | `1` | Runs executed at once; the rest wait in the queue |
| `TA_API_RUN_TIMEOUT_SECONDS` | `3600` | A run taking longer is killed and marked failed |
| `TA_API_RUN_TTL_HOURS` | `24` | Finished runs (and their zips) are deleted after this |
| `TA_API_CALLBACK_SECRET` | unset | When set, callbacks carry an HMAC-SHA256 signature |
| `TA_API_HOST` / `TA_API_PORT` | `0.0.0.0` / `8000` | Listen address |
| `TA_API_DOCS` | `true` | Serve `/docs` and `/openapi.json` |

The LLM provider, models, language and the rest come from the same `TRADINGAGENTS_*` variables
and provider keys as the CLI (see `.env.example`). Each request can override some of them.

## Start a run

```bash
curl -X POST https://ta.example.com/runs \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"ticker": "NVDA", "date": "2026-10-30", "analysts": ["market", "news", "fundamentals"]}'
```

Body fields (only `ticker` is required):

| Field | Default | |
|---|---|---|
| `ticker` | | Yahoo Finance symbol: `NVDA`, `0700.HK`, `MNS.WA`, `BTC-USD` |
| `date` | today (UTC) | `YYYY-MM-DD`, not in the future |
| `analysts` | all allowed | any of `market`, `sentiment`, `news`, `fundamentals` (no fundamentals for crypto) |
| `asset_type` | detected | `stock` or `crypto` |
| `portfolio` | none | inline portfolio, same shape as [`my_book.json.example`](../my_book.json.example) |
| `use_saved_portfolio` | `true` | with no inline `portfolio`, use the one saved via `PUT /portfolio` if there is one |
| `options` | `{}` | per-run overrides: `llm_provider`, `deep_think_llm`, `quick_think_llm`, `deep_think_provider`, `quick_think_provider`, `output_language`, `max_debate_rounds`, `max_risk_discuss_rounds`, `max_tool_rounds`, `temperature`, `openai_reasoning_effort`, `google_thinking_level`, `anthropic_effort` |
| `html` | `true` | also include `complete_report.html` |
| `callback_url` | none | gets a POST of the final status JSON when the run ends |

Response (`202`):

```json
{
  "id": "5e679710dcd545689110edbc2060992b",
  "status": "queued",
  "ticker": "NVDA", "date": "2026-10-30", "asset_type": "stock",
  "analysts": ["market", "news", "fundamentals"],
  "portfolio_source": "saved",
  "created_at": "2026-11-03T14:00:27+00:00", "started_at": null, "finished_at": null,
  "decision": null, "error": null,
  "status_url": "https://ta.example.com/runs/5e67...",
  "report_url": null
}
```

`portfolio_source` is `"request"`, `"saved"` or `null` (no portfolio was used).

## Poll and download

```bash
curl -H "X-API-Key: $KEY" https://ta.example.com/runs/$ID
curl -H "X-API-Key: $KEY" -o report.zip https://ta.example.com/runs/$ID/report
```

Once `status` is `succeeded`, `decision` holds the rating (`Buy`, `Overweight`, `Hold`,
`Underweight`, `Sell`, or `REVIEW`), and `report_url` is set. The zip contains:

- `complete_report.md` and `complete_report.html`, the full report
- one folder per stage (analysts, research, trading, risk, portfolio), the same tree the CLI saves
- `run.json`: ticker, date, analysts, decision, the model settings and the portfolio used

When `status` is `failed`, `error` says why, and `GET /runs/{id}/log` returns the worker's log.

Other calls:

- `GET /runs?limit=50` lists recent runs, newest first.
- `DELETE /runs/{id}` cancels the run if it's queued or running, and deletes its files.
  Call it after you've stored the zip, or leave it for the TTL cleanup.

## Callbacks instead of polling

When `callback_url` is set, the final status is POSTed there with a `X-TradingAgents-Run-Id`
header. Up to 3 attempts are made. If `TA_API_CALLBACK_SECRET` is set, the request also carries
`X-TradingAgents-Signature: sha256=<hex HMAC-SHA256 of the raw body>`. Verify that before you trust the body.
Then download the zip from `report_url`.

## Saved portfolio

```bash
curl -X PUT  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     --data-binary @my_book.json https://ta.example.com/portfolio
curl         -H "X-API-Key: $KEY" https://ta.example.com/portfolio
curl -X DELETE -H "X-API-Key: $KEY" https://ta.example.com/portfolio
```

The saved book stays on the data volume until you replace or delete it. Every run uses it unless
the request sends its own `portfolio` or sets `"use_saved_portfolio": false`.

## How it runs

Each run gets its own worker process (`python -m tradingagents_api.worker`). That way a crash,
hang or timeout in one analysis can't take down the server or leak config into another run.
State is kept as files under `TA_API_DATA_DIR/runs/<id>/`, with no database or Redis needed. After a
restart, queued runs are queued again, and runs that were mid-flight are marked failed.

The memory log (past decisions and lessons) lives on the same volume and is shared by all runs.
That's why `TA_API_MAX_CONCURRENT_RUNS` defaults to 1. Raise it only if your LLM rate limits
allow it and you accept that two simultaneous runs append to the same log.
