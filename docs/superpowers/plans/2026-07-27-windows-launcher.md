# Windows One-Click Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native Windows control panel that starts the local Docker stack, opens the review console, sends the weekly digest on demand, shows status, and safely stops services.

**Architecture:** A root `.cmd` file launches one PowerShell WinForms script. The PowerShell script also exposes non-GUI actions so long-running Docker work can run in a hidden child process while the window remains responsive. Existing Compose, `scripts/setup.ps1`, health endpoint, review authentication, and Celery task are reused.

**Tech Stack:** Windows PowerShell 5.1, .NET WinForms, Docker Compose, existing FastAPI/Celery services.

---

### Task 1: Testable launcher helpers

**Files:**
- Create: `scripts/launcher.ps1`

- [ ] **Step 1: Write the failing self-test entry point**

Create `scripts/launcher.ps1` with parameters and a self-test that calls helpers before they exist:

```powershell
param(
    [ValidateSet("Gui", "StartServices", "WeeklyDigest", "SelfTest")]
    [string]$Action = "Gui"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if ($Action -eq "SelfTest") {
    $fixture = Join-Path ([IO.Path]::GetTempPath()) "firefly-launcher-selftest.env"
    try {
        "CONSOLE_TOKEN=abc 123&x" | Set-Content -LiteralPath $fixture -Encoding UTF8
        $actual = Get-ReviewUrl -EnvPath $fixture
        $expected = "http://127.0.0.1:8000/review?token=abc%20123%26x"
        if ($actual -ne $expected) {
            throw "URL test failed: expected '$expected', got '$actual'"
        }
        Write-Host "[OK] launcher self-test passed"
        exit 0
    }
    finally {
        Remove-Item -LiteralPath $fixture -Force -ErrorAction SilentlyContinue
    }
}
```

- [ ] **Step 2: Run the self-test and verify it fails**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\launcher.ps1 -Action SelfTest
```

Expected: FAIL because `Get-ReviewUrl` is not defined.

- [ ] **Step 3: Implement minimal `.env` parsing and URL generation**

Insert before the self-test block:

```powershell
function Get-DotEnvValue {
    param([string]$Path, [string]$Name)

    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
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
```

- [ ] **Step 4: Run the self-test and verify it passes**

Run the Step 2 command.

Expected: `[OK] launcher self-test passed`.

- [ ] **Step 5: Commit**

```powershell
git add scripts/launcher.ps1
git commit -m "test: add launcher configuration self-check"
```

### Task 2: Docker and weekly-report actions

**Files:**
- Modify: `scripts/launcher.ps1`

- [ ] **Step 1: Extend the self-test with the wait helper and empty-token branch**

Before the self-test success message, add:

```powershell
Wait-Until -Condition { $true } -TimeoutSeconds 1 -FailureMessage "unexpected timeout"
"APP_ENV=dev" | Set-Content -LiteralPath $fixture -Encoding UTF8
$actual = Get-ReviewUrl -EnvPath $fixture
if ($actual -ne "http://127.0.0.1:8000/review") {
    throw "Empty-token URL test failed: got '$actual'"
}
```

- [ ] **Step 2: Run the self-test and verify it fails**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\launcher.ps1 -Action SelfTest
```

Expected: FAIL because `Wait-Until` is not defined.

- [ ] **Step 3: Add native command actions**

Add these functions after `Get-ReviewUrl`:

```powershell
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
    if (-not (Test-DockerEngine)) { throw "Docker 未运行，请先启动服务。" }
    & docker compose exec -T worker python -c "from app.worker.tasks_sentinel import send_weekly_digest; print(send_weekly_digest.run())"
    if ($LASTEXITCODE -ne 0) { throw "周报发送失败，请查看 worker 日志。" }
}
```

Route non-GUI actions immediately after the self-test:

```powershell
if ($Action -eq "StartServices") {
    Start-ProjectServices
    exit 0
}

if ($Action -eq "WeeklyDigest") {
    Send-WeeklyDigest
    exit 0
}
```

- [ ] **Step 4: Run the self-test and syntax check**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\launcher.ps1 -Action SelfTest
powershell.exe -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw 'scripts\launcher.ps1')); '[OK] syntax'"
```

Expected: both commands print `[OK]`.

- [ ] **Step 5: Commit**

```powershell
git add scripts/launcher.ps1
git commit -m "feat: add launcher service actions"
```

### Task 3: Native Windows control panel

**Files:**
- Modify: `scripts/launcher.ps1`
- Create: `启动记账系统.cmd`

- [ ] **Step 1: Add a hidden child-process helper and WinForms UI**

Append the following GUI implementation:

```powershell
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object Windows.Forms.Form
$form.Text = "记账系统控制台"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object Drawing.Size(620, 430)
$form.MinimumSize = New-Object Drawing.Size(620, 430)
$form.Font = New-Object Drawing.Font("Microsoft YaHei UI", 10)

$title = New-Object Windows.Forms.Label
$title.Text = "记账系统"
$title.Font = New-Object Drawing.Font("Microsoft YaHei UI", 20, [Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object Drawing.Point(24, 20)
$form.Controls.Add($title)

$status = New-Object Windows.Forms.Label
$status.Text = "状态：尚未检查"
$status.AutoSize = $true
$status.Location = New-Object Drawing.Point(28, 72)
$form.Controls.Add($status)

$output = New-Object Windows.Forms.TextBox
$output.Multiline = $true
$output.ReadOnly = $true
$output.ScrollBars = "Vertical"
$output.Location = New-Object Drawing.Point(28, 250)
$output.Size = New-Object Drawing.Size(548, 115)
$form.Controls.Add($output)

function Add-Button {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width = 170)
    $button = New-Object Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object Drawing.Point($X, $Y)
    $button.Size = New-Object Drawing.Size($Width, 48)
    $form.Controls.Add($button)
    return $button
}

function Start-LauncherAction {
    param([string]$ChildAction, [string]$BusyText, [scriptblock]$OnSuccess)

    $status.Text = "状态：$BusyText"
    $output.Clear()
    $stdout = Join-Path ([IO.Path]::GetTempPath()) "firefly-copilot-launcher.out.log"
    $stderr = Join-Path ([IO.Path]::GetTempPath()) "firefly-copilot-launcher.err.log"
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
        "-Action", $ChildAction
    ) -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

    $timer = New-Object Windows.Forms.Timer
    $timer.Interval = 1000
    $handler = {
        if (-not $process.HasExited) { return }
        $timer.Stop()
        $text = @(
            if (Test-Path -LiteralPath $stdout) { Get-Content -Raw -LiteralPath $stdout }
            if (Test-Path -LiteralPath $stderr) { Get-Content -Raw -LiteralPath $stderr }
        ) -join [Environment]::NewLine
        $output.Text = $text.Trim()
        if ($process.ExitCode -eq 0) {
            $status.Text = "状态：完成"
            & $OnSuccess
        }
        else {
            $status.Text = "状态：失败，请查看下方信息"
        }
        $timer.Dispose()
    }.GetNewClosure()
    $timer.Add_Tick($handler)
    $timer.Start()
}

$startButton = Add-Button "启动并打开复核台" 28 110 270
$reviewButton = Add-Button "打开复核台" 306 110 270
$fireflyButton = Add-Button "打开 Firefly III" 28 168
$weeklyButton = Add-Button "立即补发本周周报" 210 168 190
$statusButton = Add-Button "查看运行状态" 412 168 164
$stopButton = Add-Button "停止服务" 28 218 170

$startButton.Add_Click({
    Start-LauncherAction "StartServices" "正在启动 Docker 和服务…" {
        Start-Process (Get-ReviewUrl)
        $status.Text = "状态：服务正常；定时推送依赖电脑和 Docker 保持运行"
    }
})
$reviewButton.Add_Click({ Start-Process (Get-ReviewUrl) })
$fireflyButton.Add_Click({ Start-Process "http://127.0.0.1:8080" })
$weeklyButton.Add_Click({
    Start-LauncherAction "WeeklyDigest" "正在发送本周周报…" {
        $status.Text = "状态：本周周报已发送"
    }
})
$statusButton.Add_Click({
    Set-Location $ProjectRoot
    $output.Text = (& docker compose ps 2>&1 | Out-String)
    $status.Text = if ($LASTEXITCODE -eq 0) { "状态：已刷新" } else { "状态：Docker 未运行" }
})
$stopButton.Add_Click({
    Set-Location $ProjectRoot
    $output.Text = (& docker compose stop 2>&1 | Out-String)
    $status.Text = if ($LASTEXITCODE -eq 0) { "状态：服务已停止，数据已保留" } else { "状态：停止失败" }
})

[void]$form.ShowDialog()
```

Create `启动记账系统.cmd`:

```batch
@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0scripts\launcher.ps1"
if errorlevel 1 pause
```

- [ ] **Step 2: Run automated checks**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\launcher.ps1 -Action SelfTest
powershell.exe -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw 'scripts\launcher.ps1')); '[OK] syntax'"
```

Expected: both commands print `[OK]`.

- [ ] **Step 3: Commit**

```powershell
git add scripts/launcher.ps1 "启动记账系统.cmd"
git commit -m "feat: add Windows launcher control panel"
```

### Task 4: Usage documentation and end-to-end verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the Windows entry point**

Add a short Windows section near the quick-start instructions:

```markdown
### Windows 一键启动

已完成 `.env` 配置后，双击根目录的 `启动记账系统.cmd`，点击“启动并打开复核台”即可。控制面板还可打开 Firefly III、立即补发周报、查看容器状态和停止服务。

> 每周一 09:00 的自动周报依赖本机和 Docker Desktop 保持运行；错过后可点击“立即补发本周周报”。
```

- [ ] **Step 2: Run repository verification**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\launcher.ps1 -Action SelfTest
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
```

Expected: launcher self-test passes, all Python tests pass, Ruff reports no errors.

- [ ] **Step 3: Perform the Windows smoke test**

Run `启动记账系统.cmd`, click “启动并打开复核台”, and verify:

- Docker Desktop starts if necessary.
- `http://127.0.0.1:8000/healthz` returns HTTP 200.
- The browser opens `/review` and existing账目 remain present.
- “查看运行状态” lists `api`, `worker`, `beat`, `redis`, and both Postgres services.
- “立即补发本周周报” produces one WeCom message.
- “停止服务” stops containers without deleting volumes.

- [ ] **Step 4: Commit**

```powershell
git add README.md
git commit -m "docs: explain Windows one-click startup"
```

### Task 5: Rollback note

**Files:**
- No code changes.

- [ ] **Step 1: Record recovery commands in the handoff**

Report:

```powershell
git switch --detach backup-before-windows-launcher-20260727
```

This checks out the backed-up code. It does not delete Docker volumes. To return to the new version, run `git switch main`.
