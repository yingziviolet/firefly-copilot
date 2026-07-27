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

function New-LauncherForm {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object Windows.Forms.Form
    $form.Text = "记账系统控制台"
    $form.StartPosition = "CenterScreen"
    $form.Size = New-Object Drawing.Size(620, 450)
    $form.MinimumSize = New-Object Drawing.Size(620, 450)
    $form.Font = New-Object Drawing.Font("Microsoft YaHei UI", 10)
    $form.Tag = New-Object Collections.ArrayList

    $title = New-Object Windows.Forms.Label
    $title.Text = "记账系统"
    $title.Font = New-Object Drawing.Font("Microsoft YaHei UI", 20, [Drawing.FontStyle]::Bold)
    $title.AutoSize = $true
    $title.Location = New-Object Drawing.Point(24, 20)
    $form.Controls.Add($title)

    $status = New-Object Windows.Forms.Label
    $status.Text = "状态：尚未检查"
    $status.AutoSize = $true
    $status.MaximumSize = New-Object Drawing.Size(548, 42)
    $status.Location = New-Object Drawing.Point(28, 72)
    $form.Controls.Add($status)

    $output = New-Object Windows.Forms.TextBox
    $output.Multiline = $true
    $output.ReadOnly = $true
    $output.ScrollBars = "Vertical"
    $output.Location = New-Object Drawing.Point(28, 280)
    $output.Size = New-Object Drawing.Size(548, 105)
    $form.Controls.Add($output)

    $addButton = {
        param([string]$Name, [string]$Text, [int]$X, [int]$Y, [int]$Width)
        $button = New-Object Windows.Forms.Button
        $button.Name = $Name
        $button.Text = $Text
        $button.Location = New-Object Drawing.Point($X, $Y)
        $button.Size = New-Object Drawing.Size($Width, 48)
        $form.Controls.Add($button)
        return $button
    }.GetNewClosure()

    $runAction = {
        param([string]$ChildAction, [string]$BusyText, [scriptblock]$OnSuccess)

        $status.Text = "状态：$BusyText"
        $output.Clear()
        $id = [Guid]::NewGuid().ToString("N")
        $stdout = Join-Path ([IO.Path]::GetTempPath()) "firefly-copilot-$id.out.log"
        $stderr = Join-Path ([IO.Path]::GetTempPath()) "firefly-copilot-$id.err.log"
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $ChildAction"
        try {
            $process = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments `
                -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        }
        catch {
            $status.Text = "状态：启动失败"
            $output.Text = $_.Exception.Message
            return
        }

        $timer = New-Object Windows.Forms.Timer
        $timer.Interval = 1000
        [void]$form.Tag.Add($timer)
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
            Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
            [void]$form.Tag.Remove($timer)
            $timer.Dispose()
        }.GetNewClosure()
        $timer.Add_Tick($handler)
        $timer.Start()
    }.GetNewClosure()

    $startButton = & $addButton "startButton" "启动并打开复核台" 28 112 270
    $reviewButton = & $addButton "reviewButton" "打开复核台" 306 112 270
    $fireflyButton = & $addButton "fireflyButton" "打开 Firefly III" 28 170 170
    $weeklyButton = & $addButton "weeklyButton" "立即补发本周周报" 210 170 190
    $statusButton = & $addButton "statusButton" "查看运行状态" 412 170 164
    $stopButton = & $addButton "stopButton" "停止服务" 28 228 170

    $startButton.Add_Click({
        $success = {
            Start-Process (Get-ReviewUrl)
            $status.Text = "状态：服务正常；定时推送依赖电脑和 Docker 保持运行"
        }.GetNewClosure()
        & $runAction "StartServices" "正在启动 Docker 和服务…" $success
    }.GetNewClosure())
    $reviewButton.Add_Click({ Start-Process (Get-ReviewUrl) }.GetNewClosure())
    $fireflyButton.Add_Click({ Start-Process "http://127.0.0.1:8080" })
    $weeklyButton.Add_Click({
        $success = { $status.Text = "状态：本周周报已发送" }.GetNewClosure()
        & $runAction "WeeklyDigest" "正在发送本周周报…" $success
    }.GetNewClosure())
    $statusButton.Add_Click({
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $status.Text = "状态：未安装 Docker Desktop"
            return
        }
        Set-Location $ProjectRoot
        $output.Text = (& docker compose ps 2>&1 | Out-String)
        $status.Text = if ($LASTEXITCODE -eq 0) { "状态：已刷新" } else { "状态：Docker 未运行" }
    }.GetNewClosure())
    $stopButton.Add_Click({
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $status.Text = "状态：未安装 Docker Desktop"
            return
        }
        Set-Location $ProjectRoot
        $output.Text = (& docker compose stop 2>&1 | Out-String)
        $status.Text = if ($LASTEXITCODE -eq 0) { "状态：服务已停止，数据已保留" } else { "状态：停止失败" }
    }.GetNewClosure())

    return $form
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
        $form = New-LauncherForm
        try {
            foreach ($name in "startButton", "weeklyButton", "stopButton") {
                if ($form.Controls.Find($name, $true).Count -ne 1) {
                    throw "Missing control: $name"
                }
            }
        }
        finally {
            $form.Dispose()
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

if ($Action -eq "Gui") {
    $launcherForm = New-LauncherForm
    [void]$launcherForm.ShowDialog()
}

