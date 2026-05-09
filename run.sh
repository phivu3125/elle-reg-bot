#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

EDIT_ENV=0
for arg in "$@"; do
  case "$arg" in
    --edit-env) EDIT_ENV=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./run.sh [--edit-env]

Runs the terminal UI with the local .venv Python.
Environment values are read from .env at runtime.
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

VENV_PYTHON="$ROOT/.venv/bin/python"
ENV_PATH="$ROOT/.env"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing .venv. Run ./install.sh first." >&2
  exit 1
fi

if [[ ! -f "$ENV_PATH" ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "Created .env from .env.example. Edit it before running jobs."
  else
    echo "Missing .env and .env.example." >&2
    exit 1
  fi
fi

if [[ "$EDIT_ENV" -eq 1 ]]; then
  if [[ -n "${EDITOR:-}" ]]; then
    "$EDITOR" "$ENV_PATH"
  elif command -v nano >/dev/null 2>&1; then
    nano "$ENV_PATH"
  elif command -v vim >/dev/null 2>&1; then
    vim "$ENV_PATH"
  else
    echo "Edit this file: $ENV_PATH"
  fi
  exit 0
fi

exec "$VENV_PYTHON" main.py
