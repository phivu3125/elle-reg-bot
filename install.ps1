param(
    [switch]$SkipFetch,
    [switch]$NoPrompt,
    [switch]$EditEnv,
    [switch]$NoAutoPython
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Test-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Exe = "py"; Args = @("-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Exe = "python"; Args = @() }
    }
    return $null
}

function Install-PythonViaWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        return $false
    }
    Write-Host ""
    Write-Host "Python not found. Trying to install via winget..."
    Write-Host "  winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements"
    try {
        winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
    } catch {
        Write-Host "winget install failed: $_"
        return $false
    }

    # Refresh PATH for current session
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
    return $true
}

function Get-Python {
    $python = Test-Python
    if ($python) { return $python }

    if ($NoAutoPython) {
        throw "Python not found. Install Python 3.11+ from https://www.python.org/downloads/ then rerun."
    }

    $ok = Install-PythonViaWinget
    if ($ok) {
        $python = Test-Python
        if ($python) { return $python }
    }

    throw @"
Python not found and auto-install failed.

Install Python 3.11+ manually, then rerun .\install.ps1:
  - winget install --id Python.Python.3.12 -e
  - or download from https://www.python.org/downloads/windows/
    (tick "Add python.exe to PATH" + "py launcher" during install)

After installing, OPEN A NEW POWERSHELL WINDOW so PATH refreshes.
"@
}

function Run-Python($Python, [string[]]$ArgsList) {
    & $Python.Exe @($Python.Args) @ArgsList
}

function Open-EnvFile {
    $envPath = Join-Path $Root ".env"
    if (Get-Command notepad -ErrorAction SilentlyContinue) {
        Start-Process notepad $envPath
    } else {
        Write-Host "Edit this file before running: $envPath"
    }
}

Write-Host "== ELLE Reg-Bot Windows install =="

$Python = Get-Python
Write-Host "Using Python launcher: $($Python.Exe) $($Python.Args -join ' ')"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment: .venv"
    Run-Python $Python @("-m", "venv", ".venv")
} else {
    Write-Host "Using existing virtual environment: .venv"
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python not found: $VenvPython"
}

Write-Host "Installing Python packages..."
& $VenvPython -m pip install -U pip
& $VenvPython -m pip install -r requirements.txt

if (-not $SkipFetch) {
    Write-Host "Fetching Camoufox browser assets..."
    & $VenvPython -m camoufox fetch
} else {
    Write-Host "Skipping Camoufox fetch because -SkipFetch was set."
}

$CreatedEnv = $false
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    $CreatedEnv = $true
    Write-Host "Created .env from .env.example"
} else {
    Write-Host "Keeping existing .env (not overwritten)"
}

if ($EditEnv -or ($CreatedEnv -and -not $NoPrompt)) {
    if ($EditEnv) {
        Open-EnvFile
    } else {
        $answer = Read-Host "Open .env in Notepad now? [Y/n]"
        if ($answer -eq "" -or $answer.ToLowerInvariant().StartsWith("y")) {
            Open-EnvFile
        }
    }
}

Write-Host ""
Write-Host "Install finished. Edit .env anytime; values are loaded at runtime."
Write-Host "Run: .\run.ps1"
