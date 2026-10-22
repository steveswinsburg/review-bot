<div align="center">
  <img src="assets/review-bot-banner.svg" alt="Review Bot — thoughtful pull request reviews, on autopilot" width="100%">

  <p><strong>Your AI reviewer, right where your team works.</strong></p>
  <p>Automatically review requested pull requests, with repository context and a provider you choose.</p>
</div>

---

## What it does

Review Bot is a small Python script built to run on a schedule. It finds open pull requests in your GitHub organization that have been assigned to you for review, asks an AI provider to review the changes, and posts the result as a pull request comment.

- **Fits your workflow:** runs as a single process, once per invocation—cron-ready.
- **Understands the codebase:** checks out each PR into a temporary Git worktree and can add architecture and API context from sibling repositories.
- **Avoids duplicate reviews:** tracks each PR commit locally and in a hidden marker on GitHub, so new commits are reviewed while already-reviewed commits are skipped.
- **Choose where AI runs:** use local Ollama by default, or configure a compatible remote provider.

## How a review happens

`Find assigned PRs` → `check out the latest commit` → `collect the diff and project context` → `ask your AI provider` → `post a review comment`

If a provider or network is temporarily unavailable, the run fails gracefully; the next scheduled run can try again.

## Get started

### 1. Install

```bash
pip install -r requirements.txt
```

Install and start [Ollama](https://ollama.com/) locally, then download the default model:

```bash
ollama pull llama3
```

### 2. Prepare your workspace

Clone this bot and the repositories you want it to review side by side under one workspace directory. The bot uses those sibling clones to find optional architecture, API, schema, contribution, and README context.

```text
workspace/
└── your-org/
    ├── review-bot/
    ├── service-one/
    └── service-two/
```

### 3. Configure and run

Set the required environment variables, then run the script:

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Personal access token with access to the repositories and permission to read pull requests and post comments. |
| `GITHUB_USER` | GitHub username the bot acts as. |
| `GITHUB_ORG` | Organization whose pull requests to check. |
| `LOCAL_WORKSPACE_PATH` | Parent directory containing your side-by-side repository clones. |

The default provider is local Ollama (`llama3` at `http://localhost:11434`). For example:

```bash
GITHUB_TOKEN="your-token" \
GITHUB_USER="your-username" \
GITHUB_ORG="your-org" \
LOCAL_WORKSPACE_PATH="$HOME/workspace/your-org" \
python review_bot.py
```

## Run it on a schedule

For hourly reviews, add a cron entry. Keep credentials private: use your system's protected environment or secret-management facility rather than committing real values to a file.

```cron
0 * * * * GITHUB_TOKEN="your-token" GITHUB_USER="your-username" GITHUB_ORG="your-org" LOCAL_WORKSPACE_PATH="$HOME/workspace/your-org" /usr/bin/python3 $HOME/review-bot/review_bot.py >> $HOME/review-bot/review_bot.log 2>&1
```

The bot records processed `owner/repo#PR:COMMIT_SHA` states in `~/reviewed_prs.txt`. Each posted comment also contains a hidden state marker, allowing the bot to recover if the local state file is lost. A new commit on a PR is treated as a new review.

## Choose an AI provider

Provider settings live in JSON files in `providers/`. Select one with `AI_PROVIDER` (default: `ollama`); override the config directory with `AI_PROVIDER_CONFIG_DIR`.

| Provider | Config | API style | API key environment variable |
|---|---|---|---|
| Ollama (local, default) | `providers/ollama.json` | `ollama` | — |
| OpenAI | `providers/openai.json` | `openai` | `OPENAI_API_KEY` |
| Cursor API | `providers/cursor.json` | `openai` | `CURSOR_API_KEY` |
| Anthropic | `providers/anthropic.json` | `anthropic` | `ANTHROPIC_API_KEY` |

For example, use Cursor by setting `AI_PROVIDER=cursor` and providing `CURSOR_API_KEY`. The provider configs are templates for endpoint and model settings; **API keys belong in environment variables, never in config files.**

OpenAI-compatible endpoints—including Azure OpenAI gateways, OpenRouter, vLLM, and LM Studio—can use the `openai` API style. Add a provider by adding a `<name>.json` config and setting `AI_PROVIDER=<name>`.

Provider configs support these fields:

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

`max_payload_chars` limits the combined diff and context sent to the model. The Ollama provider also supports the legacy `OLLAMA_URL` and `OLLAMA_MODEL` environment overrides.

> **Privacy note:** a review sends the PR diff and selected repository context to the configured AI provider. Choose a provider and settings that meet your organization's data-handling requirements.

## Development

Install development dependencies, then run the linter and test suite:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

CI runs on pushes and pull requests, validating provider configs, compiling the script, and running lint and tests across Python 3.9–3.12. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
