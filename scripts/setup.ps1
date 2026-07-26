# firefly-copilot one-click bootstrap (Windows)
# Usage: right-click "Run with PowerShell", or in a terminal:  .\scripts\setup.ps1
# NOTE: messages are in English on purpose (old PowerShell consoles garble UTF-8 Chinese).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Ok($msg) { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "[ .. ] $msg" }
function Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

# --- 1. Docker available? ---
try { docker version *> $null } catch { Fail "Docker not found. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/" }
if ($LASTEXITCODE -ne 0) { Fail "Docker daemon is not running. Start Docker Desktop, wait for the whale icon, then re-run this script." }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "docker compose v2 not available. Update Docker Desktop." }
Ok "Docker is up"

# --- 2. .env ---
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Ok ".env created from .env.example (fill in your tokens later, see NEXT STEPS)"
} else {
    Info ".env already exists, keeping it"
}

# --- 3. .env.firefly with generated APP_KEY ---
if (-not (Test-Path ".env.firefly")) {
    $chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    $key = -join (1..32 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
    (Get-Content ".env.firefly.example") -replace "^APP_KEY=.*", "APP_KEY=$key" |
        Set-Content ".env.firefly" -Encoding ascii
    Ok ".env.firefly created with a generated APP_KEY"
} else {
    Info ".env.firefly already exists, keeping it"
}

# --- 4. Build the app image via stdin tar context ---
# Why not `docker compose build`: buildkit chokes on non-ASCII project paths
# (e.g. Chinese directory names) with a "x-docker-expose-session-sharedkey" gRPC
# error. Feeding the context as a tar stream avoids the path entirely.
Info "Building application image..."
$ctxTar = Join-Path $env:TEMP "ffcopilot-ctx.tar"
tar -cf $ctxTar --exclude=.venv --exclude=.git --exclude=.pytest_cache --exclude=.ruff_cache pyproject.toml app alembic.ini alembic docker
if ($LASTEXITCODE -ne 0) { Fail "tar failed (Windows 10+ ships tar.exe by default)" }
cmd /c "docker build -f docker/Dockerfile -t firefly-copilot-api - < `"$ctxTar`""
if ($LASTEXITCODE -ne 0) { Fail "docker build failed, see output above" }
Remove-Item $ctxTar -ErrorAction SilentlyContinue
docker tag firefly-copilot-api firefly-copilot-worker
docker tag firefly-copilot-api firefly-copilot-beat
Ok "Application image built (api/worker/beat share one image)"

# --- 5. Start everything (api runs DB migration automatically) ---
Info "Starting all services... (first run downloads Firefly/Postgres/Redis images)"
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed, see output above" }
Ok "All services started"
docker compose ps

Write-Host ""
Write-Host "=== NEXT STEPS (one-time setup) ===" -ForegroundColor Cyan
Write-Host "  1. Open http://localhost:8080  -> register your Firefly III account"
Write-Host "     Profile -> OAuth -> Personal Access Token -> create one"
Write-Host "  2. WeCom (WeChat Work) alerts: in any group chat -> Group Settings ->"
Write-Host "     Group Robot -> Add -> copy the webhook URL"
Write-Host "  3. Edit .env and fill in:"
Write-Host "       FIREFLY_PAT, ANTHROPIC_API_KEY, WECOM_WEBHOOK_URL"
Write-Host "       (set CONSOLE_TOKEN too if exposed to the internet)"
Write-Host "  4. Apply the new .env:   docker compose up -d --force-recreate api worker beat"
Write-Host "  5. Health check:         docker compose run --rm api python -m app.doctor"
Write-Host ""
Write-Host "Web console: http://localhost:8000/review   (quick add / CSV upload / review)"
Write-Host "Daily use: docker compose up -d   (start)   docker compose down   (stop)"
