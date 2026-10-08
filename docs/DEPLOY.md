# Deploying Hindsight on Nebius

Hindsight is one container that serves the UI and the API on port 7860. Model
calls go to Nebius Token Factory; the container itself needs no GPU.

The commands below follow the Nebius Serverless AI documentation
([quickstart](https://docs.nebius.com/serverless/quickstart/endpoints),
[managing endpoints](https://docs.nebius.com/serverless/endpoints/manage)).
They have not been run against a live account from this repository yet, so
treat the first deployment as a dry run and check each step.

## 1. Publish the image

The repository has a manual workflow that builds the Dockerfile and pushes it
to GitHub Container Registry.

1. On GitHub: **Actions → Publish image → Run workflow**.
2. When it finishes, open the repository's **Packages**, select the image and
   set its visibility to **Public** so Nebius can pull it without credentials.

The image is `ghcr.io/jacklachan/nebius-x-nvidia:latest`.

To build locally instead:

```bash
docker build -t hindsight .
docker run --rm -p 7860:7860 --env-file .env hindsight
```

## 2. Create the endpoint

Install and configure the Nebius CLI first (project ID in
`~/.nebius/config.yaml`). You need a subnet ID from your project.

```bash
nebius ai endpoint create \
  --name hindsight \
  --image ghcr.io/jacklachan/nebius-x-nvidia:latest \
  --platform cpu-d3 \
  --preset 4vcpu-16gb \
  --public \
  --container-port 7860 \
  --env NEBIUS_API_KEY=<token_factory_key>,TAVILY_API_KEY=<tavily_key> \
  --subnet-id <subnet_ID>
```

Notes:

- `--auth token` is left out on purpose. Judges must be able to open the demo
  URL in a browser, and a browser cannot send a bearer token. The app limits
  what an anonymous visitor can spend (see section 5).
- Prefer `--env-secret NEBIUS_API_KEY=<secret_selector>` over `--env` once the
  keys are stored in SecretStash, so they do not sit in shell history.
- `cpu-d3` / `4vcpu-16gb` is the CPU shape used in the Nebius quickstart. A
  smaller preset is enough if your project offers one.

## 3. Get the URL and check it

```bash
export ENDPOINT_ID=$(nebius ai endpoint get-by-name \
  --name hindsight --format jsonpath='{.metadata.id}')

export ENDPOINT_URL=$(nebius ai endpoint get "$ENDPOINT_ID" --format json \
  | jq -r '.status.public_endpoints[] | select(startswith("https://"))' | head -1)

curl "$ENDPOINT_URL/health"
curl "$ENDPOINT_URL/api/copilot/status"
```

`/api/copilot/status` should report `"ready": true`. If it reports `false`,
the key did not reach the container.

```bash
nebius ai endpoint logs $ENDPOINT_ID
```

## 4. MCP endpoint

The deployed app also answers MCP clients at `$ENDPOINT_URL/api/copilot/mcp`.
The MCP transport refuses requests whose `Host` header it does not expect, so
add the endpoint's hostname when you create it:

```bash
  --env COPILOT_MCP_ALLOWED_HOSTS=<endpoint_hostname>
```

Without it the UI and API work and MCP calls get `421 Misdirected Request`.

## 5. Spending limits

The demo runs on your Token Factory key, so the server caps usage. Both are
environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `COPILOT_MAX_CONCURRENT` | 2 | Investigations running at once |
| `COPILOT_MAX_RUNS_PER_DAY` | 200 | New investigations per day, per container |

The limits cover the UI, the HTTP API and MCP together. Uploads over 4 MB are
refused.

Recorded investigations in `copilot/recordings/` open without spending
anything, so the demo still shows real runs if the limit is reached or the
credits run out.

## 6. Stop or remove

```bash
nebius ai endpoint stop --id $ENDPOINT_ID      # no compute charge while stopped
nebius ai endpoint start --id $ENDPOINT_ID
nebius ai endpoint delete --id $ENDPOINT_ID
```
