# Deploying the API with Dokploy on the Swarm worker `vmi1107937`

A Swarm worker node can only run an image it pulls from a registry, and Dokploy's
**Stack** compose type can't `build:`. So the image is built by GitHub Actions and
pushed to GHCR, and Dokploy deploys it as a stack pinned to the worker node.

```
push to dev ──► GitHub Actions (.github/workflows/docker-image.yml)
                 └─► ghcr.io/navse/tradingagents-n:dev
Dokploy (Stack, docker-compose.dokploy.yml) ──► pulls the image on node vmi1107937
```

## 1. Build the image

1. Push to `dev`. The **Docker image** workflow builds and pushes
   `ghcr.io/navse/tradingagents-n:dev` (plus `:sha-<commit>`). You can also run it by hand
   under **Actions → Docker image → Run workflow**.
2. The package is private by default. You have two options:
   - **Keep it private (recommended):** create a GitHub personal access token (classic) with
     `read:packages`. In Dokploy, go to **Settings → Registry → Add Registry** and fill in
     Registry URL `ghcr.io`, your GitHub username, and the token as the password. Use **Test**, then save.
   - **Or make it public:** on GitHub, open **Packages → tradingagents-n → Package settings → Change visibility**.

## 2. Create the stack in Dokploy

1. Open **Project → Create Service → Compose**, and set **Compose Type: Stack**.
2. Under **Provider**, choose GitHub, repo `navse/TradingAgents-N`, branch `dev`, and
   **Compose Path** `./docker-compose.dokploy.yml`.
3. If the image is private, open **Advanced** and add `--with-registry-auth` to the deploy
   command (this is what lets the worker node use the registry login).
4. Under **Environment**, add the following. Dokploy writes these to `.env` next to the compose file, and the
   service reads it through `env_file`:

   ```env
   # API
   TA_API_KEY=<long random string, e.g. `openssl rand -hex 32`>
   TA_API_DOMAIN=ta.example.com
   TA_API_RUN_TTL_HOURS=24
   TA_API_MAX_CONCURRENT_RUNS=1
   #TA_API_CALLBACK_SECRET=<another random string>
   #TA_IMAGE=ghcr.io/navse/tradingagents-n:sha-abc1234   # pin a build

   # LLM: the provider key(s) you use, plus model choices
   OPENAI_API_KEY=...
   TRADINGAGENTS_LLM_PROVIDER=openai
   TRADINGAGENTS_DEEP_THINK_LLM=gpt-6-sol
   TRADINGAGENTS_QUICK_THINK_LLM=gpt-6-luna
   TRADINGAGENTS_OUTPUT_LANGUAGE=English

   # Optional data sources
   #FRED_API_KEY=...
   #SEC_EDGAR_USER_AGENT=Your Name you@example.com
   ```

5. **Deploy.** The `deploy.placement.constraints: node.hostname == vmi1107937` setting in
   `docker-compose.dokploy.yml` puts the container on that node. The `tradingagents_data` volume
   is created on that node too, and it holds runs, zips, the saved portfolio, the memory log and the data cache.

### Domain / HTTPS

The compose file already carries Traefik labels under `deploy.labels`. These give
`Host(${TA_API_DOMAIN})` on `websecure` with the `letsencrypt` resolver, pointing at port 8000.
Point a DNS A record for `TA_API_DOMAIN` at the Swarm manager (Traefik runs there and routes over
`dokploy-network`).

If you'd rather use Dokploy's **Domains** tab (service `api`, port `8000`), delete the `traefik.*`
labels from the compose file first, so the router isn't defined twice. **Preview Compose** shows the final result.

If the other app runs in the same Swarm on `dokploy-network`, it can skip the public domain
and call `http://<stack-name>_api:8000` directly.

## 3. Check it

```bash
curl https://ta.example.com/health
curl -X POST https://ta.example.com/runs -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"ticker":"NVDA","analysts":["market"]}'
```

See [API.md](API.md) for the endpoints.

## Updating

Push to `dev`, wait for the workflow, then **Redeploy** in Dokploy (or switch on the
**Autodeploy** webhook). Because the tag `:dev` moves, a redeploy pulls the new build. To roll back,
set `TA_IMAGE` to an earlier `:sha-…` tag.

## Alternative: a Dokploy Application instead of a Stack

If you'd rather let Dokploy build the image:

1. **Create Service → Application**, GitHub repo `navse/TradingAgents-N`, branch `dev`,
   **Build Type: Dockerfile**.
2. **Advanced → Cluster Settings → Registry Selection**: pick a registry Dokploy can push to (for example
   GHCR with a token that has `write:packages`). The worker node pulls the build from there.
3. **Advanced → Swarm Settings → Placement**:
   ```json
   { "Constraints": ["node.hostname==vmi1107937"] }
   ```
4. **Advanced → Command**: `api` (the image's entrypoint is the `tradingagents` CLI; `tradingagents api` serves the API).
5. **Advanced → Volumes**: Volume Mount, name `tradingagents_data`, mount path `/home/appuser/.tradingagents`.
6. **Environment**: the same variables as above, plus `TA_API_DATA_DIR=/home/appuser/.tradingagents/api`.
7. **Domains**: your host, container port `8000`, HTTPS on.
