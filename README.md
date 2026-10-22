# review-bot
A bot that does PR reviews.

`review_bot.py` is a standalone, single-run script designed to be executed by cron every 60 minutes. It:

- Acts under your personal GitHub account via a PAT (`GITHUB_TOKEN`).
- Searches for open PRs in `GITHUB_ORG` where `GITHUB_USER` is a requested reviewer (`is:open is:pr org:... review-requested:...`), skipping your own PRs.
- Tracks processed states as `owner/repo#PR:COMMIT_SHA` in `~/reviewed_prs.txt`, so pushed updates / re-requested reviews are re-reviewed automatically.
- Uses the local clone in `LOCAL_WORKSPACE_PATH`, fetches the PR head, and checks it out into a temporary git worktree under `/tmp/` (guaranteed cleanup via `git worktree remove --force` in a `try/finally`).
- Reads the diff locally (`git diff origin/main...HEAD`) and cross-references architectural/API files from the sibling repos in the workspace, truncating the combined payload (30,000 characters by default, configurable per provider).
- Sends the payload to a configurable AI provider — local Ollama by default, or remote providers such as the Cursor API, OpenAI, or Anthropic — and posts the resulting markdown review as a PR comment.
- Fails gracefully (AI provider outage, missing clone, network drop) so the next cron cycle retries.

## Requirements

```bash
pip install -r requirements.txt
```

## Development

Install dev dependencies, then lint and test:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI runs automatically on every push and pull request via [`.github/workflows/ci.yml`](.github/workflows/ci.yml): ruff linting, provider config validation, a compile check, and the pytest suite across Python 3.9–3.12.

## AI Provider Configuration

The AI backend is pluggable. Each provider has its own JSON config file in the `providers/` directory (override the location with `AI_PROVIDER_CONFIG_DIR`), and the active provider is selected with the `AI_PROVIDER` environment variable (default: `ollama`).

Bundled provider configs:

| Provider | Config file | API style | API key env var |
|---|---|---|---|
| Ollama (local, default) | `providers/ollama.json` | `ollama` | none |
| OpenAI | `providers/openai.json` | `openai` | `OPENAI_API_KEY` |
| Cursor API | `providers/cursor.json` | `openai` | `CURSOR_API_KEY` |
| Anthropic | `providers/anthropic.json` | `anthropic` | `ANTHROPIC_API_KEY` |

Config file schema:

```json
{
  "api_style": "ollama | openai | anthropic",
  "base_url": "https://api.example.com/v1",
  "model": "model-name",
  "api_key_env": "ENV_VAR_HOLDING_KEY",
  "max_payload_chars": 30000,
  "timeout_seconds": 600,
  "max_tokens": 4096,
  "extra_headers": {"Optional-Header": "value"}
}
```

- **API keys are never stored in config files.** `api_key_env` names the environment variable that holds the secret, which you inject via the crontab line.
- `api_style: "openai"` works with any OpenAI-compatible chat-completions endpoint (Cursor, Azure OpenAI gateways, OpenRouter, vLLM, LM Studio, ...) — just point `base_url` at it. Add new providers by dropping a new `<name>.json` file into `providers/` and setting `AI_PROVIDER=<name>`.
- `max_payload_chars` lets larger-context remote models receive more diff/context than the 30,000-character default used for local models.
- The `ollama` provider still honors the legacy `OLLAMA_URL` / `OLLAMA_MODEL` environment overrides.

## Production Cron Configuration

Local Ollama (default):

```cron
# Automated local PR review bot — runs on the hour, every hour.
0 * * * * GITHUB_TOKEN="ghp_your_pat_here" GITHUB_USER="your-github-username" GITHUB_ORG="your-org-name" LOCAL_WORKSPACE_PATH="$HOME/workspace/your-org-name" AI_PROVIDER="ollama" OLLAMA_URL="http://localhost:11434" OLLAMA_MODEL="llama3" /usr/bin/python3 $HOME/review-bot/review_bot.py >> $HOME/review-bot/review_bot.log 2>&1
```

Remote provider (e.g. Cursor API):

```cron
# Automated PR review bot using the Cursor API — runs on the hour, every hour.
0 * * * * GITHUB_TOKEN="ghp_your_pat_here" GITHUB_USER="your-github-username" GITHUB_ORG="your-org-name" LOCAL_WORKSPACE_PATH="$HOME/workspace/your-org-name" AI_PROVIDER="cursor" CURSOR_API_KEY="your_cursor_api_key" /usr/bin/python3 $HOME/review-bot/review_bot.py >> $HOME/review-bot/review_bot.log 2>&1
```
