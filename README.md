# review-bot
A bot that does PR reviews.

`review_bot.py` is a standalone, single-run script designed to be executed by cron every 60 minutes. It:

- Acts under your personal GitHub account via a PAT (`GITHUB_TOKEN`).
- Searches for open PRs in `GITHUB_ORG` where `GITHUB_USER` is a requested reviewer (`is:open is:pr org:... review-requested:...`), skipping your own PRs.
- Tracks processed states as `owner/repo#PR:COMMIT_SHA` in `~/reviewed_prs.txt`, so pushed updates / re-requested reviews are re-reviewed automatically.
- Uses the local clone in `LOCAL_WORKSPACE_PATH`, fetches the PR head, and checks it out into a temporary git worktree under `/tmp/` (guaranteed cleanup via `git worktree remove --force` in a `try/finally`).
- Reads the diff locally (`git diff origin/main...HEAD`) and cross-references architectural/API files from the sibling repos in the workspace, truncating the combined payload at 30,000 characters.
- Sends the payload to a local Ollama instance (`OLLAMA_URL`, `OLLAMA_MODEL`) and posts the resulting markdown review as a PR comment.
- Fails gracefully (Ollama outage, missing clone, network drop) so the next cron cycle retries.

## Requirements

```bash
pip install pygithub
```

## Production Cron Configuration

```cron
# Automated local PR review bot — runs on the hour, every hour.
0 * * * * GITHUB_TOKEN="ghp_your_pat_here" GITHUB_USER="your-github-username" GITHUB_ORG="your-org-name" LOCAL_WORKSPACE_PATH="$HOME/workspace/your-org-name" OLLAMA_URL="http://localhost:11434" OLLAMA_MODEL="llama3" /usr/bin/python3 $HOME/review-bot/review_bot.py >> $HOME/review-bot/review_bot.log 2>&1
```
