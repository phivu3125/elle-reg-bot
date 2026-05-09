param(
    [switch]$EditEnv
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$EnvPath = Join-Path $Root ".env"

if (-not (Test-Path $VenvPython)) {
    throw "Missing .venv. Run .\install.ps1 first."
}

if (-not (Test-Path $EnvPath)) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example. Edit it before running jobs."
    } else {
        throw "Missing .env and .env.example."
    }
}

if ($EditEnv) {
    Start-Process notepad $EnvPath
    exit 0
}

& $VenvPython main.py
