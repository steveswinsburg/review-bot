<div align="center">
  <img src="assets/review-bot-banner.svg" alt="Review Bot — thoughtful pull request reviews, on autopilot" width="100%">

  <p><strong>Your AI reviewer, right where your team works.</strong></p>
  <p>Automatically review requested pull requests, with repository context and a provider you choose.</p>
</div>

---

## What it does

Review Bot is a small Python script built to run on a schedule. It finds open pull requests in a GitHub organization or personal account that have been assigned to you for review, asks an AI provider to review the changes, and posts the result as a pull request comment.

- **Fits your workflow:** runs as a single process, once per invocation—cron-ready.
- **Understands the codebase:** checks out each PR into a temporary Git worktree and can add architecture and API context from sibling repositories.
- **Avoids duplicate reviews:** tracks each PR commit locally and in a hidden marker on GitHub, so new commits are reviewed while already-reviewed commits are skipped.
- **Choose where AI runs:** use local Ollama by default, or configure a compatible remote provider.

## How a review happens

`Find assigned PRs` → `check out the latest commit` → `collect the diff and project context` → `ask your AI provider` → `post a review comment`

If a provider or network is temporarily unavailable, the run fails gracefully; the next scheduled run can try again.

## Get started

### 1. Install

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) if it
is not already available, then sync the runtime dependencies declared in
`pyproject.toml`. This creates the `.venv` environment:

```bash
uv sync
```

Run the bot with the environment managed by uv:

```bash
uv run python review_bot.py
```

Install and start [Ollama](https://ollama.com/) locally, then download the default model:

```bash
ollama pull llama3
```

### 2. Prepare your workspace

Clone each repository you want the bot to review directly inside a workspace
directory. The directory name and location are up to you; it does not need to
match the GitHub organization name. Each clone must be named after its GitHub
repository, because the bot looks for it at
`LOCAL_WORKSPACE_PATH/<repository-name>`. The bot also reads optional
architecture, API, schema, contribution, and README context from other
repository clones in that directory.

```text
~/code/review-workspace/       # LOCAL_WORKSPACE_PATH
├── service-one/               # clone of your-org/service-one
└── service-two/               # clone of your-org/service-two
```

### 3. Configure and run

Set the required environment variables, then run the script. Set either
`GITHUB_OWNER` to the login of an organization or personal account (Review Bot
detects the account type), or set `GITHUB_ORG` when you know the owner is an
organization. No special organization-side installation or configuration is
needed. The token must be able to access the target repositories, and
`GITHUB_USER` must be the account that is requested for review on the PRs.

| Environment variable | When to set it | Example |
|---|---|---|
| `GITHUB_TOKEN` | Required. Personal access token with access to the target repositories and permission to read pull requests and post comments. | `GITHUB_TOKEN="github_pat_..."` |
| `GITHUB_USER` | Required. GitHub username the token belongs to and the username assigned as reviewer. | `GITHUB_USER="octocat"` |
| `GITHUB_OWNER` | Required if `GITHUB_ORG` is not set. Organization or personal-account login whose open PRs are searched; account type is detected automatically. | `GITHUB_OWNER="octocat"` or `GITHUB_OWNER="your-org"` |
| `GITHUB_ORG` | Required if `GITHUB_OWNER` is not set. Organization login whose open PRs are searched. | `GITHUB_ORG="your-org"` |
| `LOCAL_WORKSPACE_PATH` | Required. Existing directory containing local clones of the repositories to review, each directly inside it. | `LOCAL_WORKSPACE_PATH="$HOME/code/review-workspace"` |
| `AI_PROVIDER` | Optional; defaults to `ollama`. Selects a provider config such as `ollama`, `openai`, `cursor`, or `anthropic`. | `AI_PROVIDER="openai"` |
| `AI_PROVIDER_CONFIG_DIR` | Optional; defaults to this project's `providers/` directory. Directory containing `<AI_PROVIDER>.json`. | `AI_PROVIDER_CONFIG_DIR="$HOME/review-bot/providers"` |
| `OLLAMA_URL` | Optional; defaults to `http://localhost:11434`. Overrides the Ollama endpoint. | `OLLAMA_URL="http://localhost:11434"` |
| `OLLAMA_MODEL` | Optional; defaults to `llama3`. Overrides the Ollama model name. | `OLLAMA_MODEL="llama3"` |
| `OPENAI_API_KEY` | Required when using the bundled `openai` provider. | `OPENAI_API_KEY="your-openai-api-key"` |
| `CURSOR_API_KEY` | Required when using the bundled `cursor` provider. | `CURSOR_API_KEY="your-cursor-api-key"` |
| `ANTHROPIC_API_KEY` | Required when using the bundled `anthropic` provider. | `ANTHROPIC_API_KEY="your-anthropic-api-key"` |

The default provider is local Ollama (`llama3` at `http://localhost:11434`).
For an organization, you can use either configuration:

```bash
GITHUB_TOKEN="your-token" \
GITHUB_USER="your-username" \
GITHUB_ORG="your-org" \
LOCAL_WORKSPACE_PATH="$HOME/code/review-workspace" \
uv run python review_bot.py
```

For repositories owned by a personal account, use `GITHUB_OWNER` with the
account's login:

```bash
GITHUB_TOKEN="your-token" \
GITHUB_USER="your-username" \
GITHUB_OWNER="your-username" \
LOCAL_WORKSPACE_PATH="$HOME/code/review-workspace" \
uv run python review_bot.py
```

For a remote provider, set its selector and API key as well. For example:

```bash
GITHUB_TOKEN="your-token" \
GITHUB_USER="your-username" \
GITHUB_OWNER="your-org" \
LOCAL_WORKSPACE_PATH="$HOME/code/review-workspace" \
AI_PROVIDER="openai" \
OPENAI_API_KEY="your-openai-api-key" \
uv run python review_bot.py
```

## Run it on a schedule

For hourly reviews, add a cron entry. Keep credentials private: use your system's protected environment or secret-management facility rather than committing real values to a file.

The `--locked` flag in this cron command (and in CI) makes uv use the committed
`uv.lock` exactly. If the lockfile and `pyproject.toml` are out of sync, uv
fails instead of silently changing dependency versions. For normal local setup
and development, omit `--locked` so uv can update the lockfile when dependencies
change.

```cron
0 * * * * cd "$HOME/review-bot" && GITHUB_TOKEN="your-token" GITHUB_USER="your-username" GITHUB_OWNER="your-org" LOCAL_WORKSPACE_PATH="$HOME/code/review-workspace" uv run --locked python review_bot.py >> "$HOME/review-bot/review_bot.log" 2>&1
```

The bot records processed `owner/repo#PR:COMMIT_SHA` states in `~/reviewed_prs.txt`. Each posted comment also contains a hidden state marker, allowing the bot to recover if the local state file is lost. A new commit on a PR is treated as a new review.

## Choose an AI provider

Provider settings live in JSON files in `providers/`. Select one with
`AI_PROVIDER` (default: `ollama`); override the config directory with
`AI_PROVIDER_CONFIG_DIR`. The API key environment variable is named in each
provider config's `api_key_env` field. The bundled remote providers require
their matching key variable from the table above; keep keys in your protected
environment or secret manager, never in config files.

| Provider | Config | API style | API key environment variable |
|---|---|---|---|
| Ollama (local, default) | `providers/ollama.json` | `ollama` | — |
| OpenAI | `providers/openai.json` | `openai` | `OPENAI_API_KEY` (example: `OPENAI_API_KEY="your-openai-api-key"`) |
| Cursor API | `providers/cursor.json` | `openai` | `CURSOR_API_KEY` (example: `CURSOR_API_KEY="your-cursor-api-key"`) |
| Anthropic | `providers/anthropic.json` | `anthropic` | `ANTHROPIC_API_KEY` (example: `ANTHROPIC_API_KEY="your-anthropic-api-key"`) |

For example, use Cursor by setting `AI_PROVIDER="cursor"` and
`CURSOR_API_KEY="your-cursor-api-key"`. The provider configs are templates for
endpoint and model settings; **API keys belong in environment variables, never
in config files.**

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

`max_payload_chars` limits the combined diff and context sent to the model.
`OLLAMA_URL` and `OLLAMA_MODEL` override the Ollama config's endpoint and model.

> **Privacy note:** a review sends the PR diff and selected repository context to the configured AI provider. Choose a provider and settings that meet your organization's data-handling requirements.

## Development

Development dependencies are declared in the `dev` group in `pyproject.toml`.
Sync them and run the linter and tests with uv:

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

CI runs on pushes and pull requests, validating provider configs, compiling the script, and running lint and tests across Python 3.9–3.12. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
