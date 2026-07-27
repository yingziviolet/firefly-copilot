param(
    [ValidateSet("Gui", "StartServices", "WeeklyDigest", "SelfTest")]
    [string]$Action = "Gui"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Get-DotEnvValue {
    param([string]$Path, [string]$Name)

    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed -split "=", 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim([char]34).Trim([char]39)
        }
    }
    return ""
}

function Get-ReviewUrl {
    param([string]$EnvPath = (Join-Path $ProjectRoot ".env"))

    $token = Get-DotEnvValue -Path $EnvPath -Name "CONSOLE_TOKEN"
    if (-not $token) { return "http://127.0.0.1:8000/review" }
    return "http://127.0.0.1:8000/review?token=$([Uri]::EscapeDataString($token))"
}

function Test-DockerEngine {
    & docker version *> $null
    return $LASTEXITCODE -eq 0
}

function Wait-Until {
    param([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$FailureMessage)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) { return }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw $FailureMessage
}

function Test-ApiHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/healthz" -TimeoutSec 3
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Start-ProjectServices {
    Set-Location $ProjectRoot
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env"))) {
        throw ".env 不存在，请先按 .env.example 完成配置。"
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "未安装 Docker Desktop。"
    }

    if (-not (Test-DockerEngine)) {
        $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
        if (-not (Test-Path -LiteralPath $dockerDesktop)) {
            throw "Docker 引擎未运行，并且未找到 Docker Desktop。"
        }
        Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
        Wait-Until -Condition { Test-DockerEngine } -TimeoutSeconds 120 -FailureMessage "Docker Desktop 启动超时。"
    }

    & docker image inspect firefly-copilot-api *> $null
    $needsSetup = $LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env.firefly"))
    if ($needsSetup) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "setup.ps1")
        if ($LASTEXITCODE -ne 0) { throw "首次构建失败，请查看上方信息。" }
    }
    else {
        & docker compose up -d
        if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败。" }
    }

    Wait-Until -Condition { Test-ApiHealth } -TimeoutSeconds 120 -FailureMessage "服务已启动，但 API 健康检查超时。"
    Write-Host "[OK] 服务已就绪"
}

function Send-WeeklyDigest {
    Set-Location $ProjectRoot
    if (-not (Get-Command docker -ErrorAction SilentlyContinue) -or -not (Test-DockerEngine)) {
        throw "Docker 未运行，请先启动服务。"
    }
    & docker compose exec -T worker python -c "from app.worker.tasks_sentinel import send_weekly_digest; print(send_weekly_digest.run())"
    if ($LASTEXITCODE -ne 0) { throw "周报发送失败，请查看 worker 日志。" }
}

if ($Action -eq "SelfTest") {
    $fixture = Join-Path ([IO.Path]::GetTempPath()) "firefly-launcher-selftest.env"
    try {
        "CONSOLE_TOKEN=abc 123&x" | Set-Content -LiteralPath $fixture -Encoding UTF8
        $actual = Get-ReviewUrl -EnvPath $fixture
        $expected = "http://127.0.0.1:8000/review?token=abc%20123%26x"
        if ($actual -ne $expected) {
            throw "URL test failed: expected '$expected', got '$actual'"
        }
        Wait-Until -Condition { $true } -TimeoutSeconds 1 -FailureMessage "unexpected timeout"
        "APP_ENV=dev" | Set-Content -LiteralPath $fixture -Encoding UTF8
        $actual = Get-ReviewUrl -EnvPath $fixture
        if ($actual -ne "http://127.0.0.1:8000/review") {
            throw "Empty-token URL test failed: got '$actual'"
        }
        Write-Host "[OK] launcher self-test passed"
        exit 0
    }
    finally {
        Remove-Item -LiteralPath $fixture -Force -ErrorAction SilentlyContinue
    }
}

if ($Action -eq "StartServices") {
    Start-ProjectServices
    exit 0
}

if ($Action -eq "WeeklyDigest") {
    Send-WeeklyDigest
    exit 0
}

