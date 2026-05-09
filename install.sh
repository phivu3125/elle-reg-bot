#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SKIP_FETCH=0
NO_PROMPT=0
EDIT_ENV=0

for arg in "$@"; do
  case "$arg" in
    --skip-fetch) SKIP_FETCH=1 ;;
    --no-prompt) NO_PROMPT=1 ;;
    --edit-env) EDIT_ENV=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./install.sh [--skip-fetch] [--no-prompt] [--edit-env]

Creates .venv, installs requirements, fetches Camoufox browser assets,
and creates .env from .env.example only if .env does not exist.

Options:
  --skip-fetch  Skip `python -m camoufox fetch`
  --no-prompt   Do not prompt to edit .env
  --edit-env    Open .env after install when EDITOR is available
EOF
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

suggest_python_install() {
  echo ""
  echo "Python 3 not found. Install it with the command for your OS, then rerun ./install.sh:"
  echo ""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1; then
      echo "  brew install python@3.12"
    else
      echo "  Install Homebrew first: https://brew.sh"
      echo "  Then: brew install python@3.12"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    echo "  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null 2>&1; then
    echo "  sudo dnf install -y python3 python3-virtualenv python3-pip"
  elif command -v pacman >/dev/null 2>&1; then
    echo "  sudo pacman -S --needed python python-virtualenv python-pip"
  elif command -v zypper >/dev/null 2>&1; then
    echo "  sudo zypper install -y python3 python3-venv python3-pip"
  elif command -v apk >/dev/null 2>&1; then
    echo "  sudo apk add python3 py3-virtualenv py3-pip"
  else
    echo "  Install Python 3.11+ from https://www.python.org/downloads/"
  fi
  echo ""
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  suggest_python_install >&2
  exit 1
}

open_env_file() {
  local env_path="$ROOT/.env"
  if [[ -n "${EDITOR:-}" ]]; then
    "$EDITOR" "$env_path"
  elif command -v nano >/dev/null 2>&1; then
    nano "$env_path"
  elif command -v vim >/dev/null 2>&1; then
    vim "$env_path"
  else
    echo "Edit this file before running: $env_path"
  fi
}

echo "== ELLE Reg-Bot Linux install =="

PYTHON_BIN="$(find_python)"
echo "Using Python: $PYTHON_BIN"

if [[ ! -d .venv ]]; then
  echo "Creating virtual environment: .venv"
  "$PYTHON_BIN" -m venv .venv
else
  echo "Using existing virtual environment: .venv"
fi

VENV_PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Virtual environment Python not found: $VENV_PYTHON" >&2
  exit 1
fi

echo "Installing Python packages..."
"$VENV_PYTHON" -m pip install -U pip
"$VENV_PYTHON" -m pip install -r requirements.txt

if [[ "$SKIP_FETCH" -eq 0 ]]; then
  echo "Fetching Camoufox browser assets..."
  "$VENV_PYTHON" -m camoufox fetch
else
  echo "Skipping Camoufox fetch because --skip-fetch was set."
fi

CREATED_ENV=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  CREATED_ENV=1
  echo "Created .env from .env.example"
else
  echo "Keeping existing .env (not overwritten)"
fi

if [[ "$EDIT_ENV" -eq 1 ]]; then
  open_env_file
elif [[ "$CREATED_ENV" -eq 1 && "$NO_PROMPT" -eq 0 ]]; then
  read -r -p "Open .env now? [Y/n] " answer
  answer="${answer:-Y}"
  if [[ "${answer,,}" == y* ]]; then
    open_env_file
  fi
fi

cat <<EOF

Install finished. Edit .env anytime; values are loaded at runtime.
Run: ./run.sh
EOF
