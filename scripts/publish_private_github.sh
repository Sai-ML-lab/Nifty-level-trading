#!/usr/bin/env bash
set -euo pipefail

OWNER="${GITHUB_OWNER:-Sai-ML-lab}"
REPO="${GITHUB_REPO_NAME:-nifty-level-trader}"
FULL_NAME="${OWNER}/${REPO}"
REMOTE_URL="https://github.com/${FULL_NAME}.git"

command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required. Install it and run: gh auth login" >&2; exit 1; }
gh auth status >/dev/null

cd "$(dirname "$0")/.."

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init -b main
fi

CURRENT_BRANCH="$(git branch --show-current)"
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  git branch -M main
fi

git add -A
if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  GH_LOGIN="$(gh api user --jq .login)"
  GH_ID="$(gh api user --jq .id)"
  git config user.name "${GH_LOGIN}"
  git config user.email "${GH_ID}+${GH_LOGIN}@users.noreply.github.com"
  git commit -m "Initial end-to-end NIFTY level trader"
elif ! git diff --cached --quiet; then
  git commit -m "Publish NIFTY level trader application"
fi

if gh repo view "$FULL_NAME" >/dev/null 2>&1; then
  echo "GitHub repository already exists: $FULL_NAME"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REMOTE_URL"
  else
    git remote add origin "$REMOTE_URL"
  fi
else
  gh repo create "$FULL_NAME" --private --source . --remote origin
fi

git push -u origin main
printf '\nPublished: https://github.com/%s\n' "$FULL_NAME"
