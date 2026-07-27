param(
    [ValidateSet("Gui", "StartServices", "StartAndOpen", "WeeklyDigest", "WeeklyFromGui", "SelfTest")]
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

function Get-ConsoleUrl {
    param(
        [string]$Path = "/review",
        [string]$EnvPath = (Join-Path $ProjectRoot ".env")
    )

    $token = Get-DotEnvValue -Path $EnvPath -Name "CONSOLE_TOKEN"
    $url = "http://127.0.0.1:8000$Path"
    if (-not $token) { return $url }
    return "${url}?token=$([Uri]::EscapeDataString($token))"
}

function Test-DockerEngine {
    & cmd.exe /d /c "docker version >nul 2>&1"
    return $LASTEXITCODE -eq 0
}

function Test-DockerImage {
    & cmd.exe /d /c "docker image inspect firefly-copilot-api >nul 2>&1"
    return $LASTEXITCODE -eq 0
}

function Invoke-ComposeCapture {
    param([ValidateSet("ps", "stop")][string]$Command)

    Set-Location $ProjectRoot
    $lines = & cmd.exe /d /c "docker compose $Command 2>&1"
    $exitCode = $LASTEXITCODE
    return [PSCustomObject]@{
        Text = ($lines | Out-String).TrimEnd()
        ExitCode = $exitCode
    }
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

    $needsSetup = -not (Test-DockerImage) -or -not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env.firefly"))
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

function Get-GuiChildAction {
    param([ValidateSet("start", "weekly")][string]$Button)

    if ($Button -eq "start") { return "StartAndOpen" }
    return "WeeklyFromGui"
}

function Show-LauncherMessage {
    param([string]$Text, [string]$Title = "记账系统")

    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($Text, $Title)
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
        param([string]$ChildAction, [string]$BusyText)

        $status.Text = "状态：$BusyText"
        $output.Clear()
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action $ChildAction"
        try {
            Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WindowStyle Hidden
        }
        catch {
            $status.Text = "状态：启动失败"
            $output.Text = $_.Exception.Message
        }
    }.GetNewClosure()

    $startButton = & $addButton "startButton" "启动并打开财务 Agent" 28 112 270
    $reviewButton = & $addButton "reviewButton" "打开复核台" 306 112 270
    $fireflyButton = & $addButton "fireflyButton" "打开 Firefly III" 28 170 170
    $weeklyButton = & $addButton "weeklyButton" "立即补发本周周报" 210 170 190
    $statusButton = & $addButton "statusButton" "查看运行状态" 412 170 164
    $stopButton = & $addButton "stopButton" "停止服务" 28 228 170

    $startButton.Add_Click({
        & $runAction (Get-GuiChildAction -Button "start") "后台启动中，完成后自动打开财务 Agent"
    }.GetNewClosure())
    $reviewButton.Add_Click({ Start-Process (Get-ConsoleUrl -Path "/review") }.GetNewClosure())
    $fireflyButton.Add_Click({ Start-Process "http://127.0.0.1:8080" })
    $weeklyButton.Add_Click({
        & $runAction (Get-GuiChildAction -Button "weekly") "正在发送，完成后会弹出结果"
    }.GetNewClosure())
    $statusButton.Add_Click({
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $status.Text = "状态：未安装 Docker Desktop"
            return
        }
        $result = Invoke-ComposeCapture -Command "ps"
        $output.Text = $result.Text
        $status.Text = if ($result.ExitCode -eq 0) { "状态：已刷新" } else { "状态：Docker 未运行" }
    }.GetNewClosure())
    $stopButton.Add_Click({
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $status.Text = "状态：未安装 Docker Desktop"
            return
        }
        $result = Invoke-ComposeCapture -Command "stop"
        $output.Text = $result.Text
        $status.Text = if ($result.ExitCode -eq 0) { "状态：服务已停止，数据已保留" } else { "状态：停止失败" }
    }.GetNewClosure())

    return $form
}

if ($Action -eq "SelfTest") {
    $fixture = Join-Path ([IO.Path]::GetTempPath()) "firefly-launcher-selftest.env"
    try {
        "CONSOLE_TOKEN=abc 123&x" | Set-Content -LiteralPath $fixture -Encoding UTF8
        $actual = Get-ConsoleUrl -Path "/review" -EnvPath $fixture
        $expected = "http://127.0.0.1:8000/review?token=abc%20123%26x"
        if ($actual -ne $expected) {
            throw "URL test failed: expected '$expected', got '$actual'"
        }
        $actual = Get-ConsoleUrl -Path "/agent" -EnvPath $fixture
        $expected = "http://127.0.0.1:8000/agent?token=abc%20123%26x"
        if ($actual -ne $expected) {
            throw "Agent URL test failed: expected '$expected', got '$actual'"
        }
        Wait-Until -Condition { $true } -TimeoutSeconds 1 -FailureMessage "unexpected timeout"
        "APP_ENV=dev" | Set-Content -LiteralPath $fixture -Encoding UTF8
        $actual = Get-ConsoleUrl -Path "/review" -EnvPath $fixture
        if ($actual -ne "http://127.0.0.1:8000/review") {
            throw "Empty-token URL test failed: got '$actual'"
        }
        try {
            $null = Test-DockerEngine
        }
        catch {
            throw "Docker availability checks must not throw: $($_.Exception.Message)"
        }
        if ((Get-GuiChildAction -Button "start") -ne "StartAndOpen") {
            throw "Start button must use StartAndOpen"
        }
        try {
            $null = Invoke-ComposeCapture -Command "ps"
        }
        catch {
            throw "Compose capture must not throw on native stderr: $($_.Exception.Message)"
        }
        $form = New-LauncherForm
        try {
            foreach ($name in "startButton", "weeklyButton", "stopButton") {
                if ($form.Controls.Find($name, $true).Count -ne 1) {
                    throw "Missing control: $name"
                }
            }
            $start = $form.Controls.Find("startButton", $true)
            if ($start[0].Text -ne "启动并打开财务 Agent") {
                throw "Missing Agent launcher button"
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

if ($Action -eq "StartAndOpen") {
    try {
        Start-ProjectServices
        Start-Process (Get-ConsoleUrl -Path "/agent")
    }
    catch {
        Show-LauncherMessage -Text $_.Exception.Message -Title "启动失败"
        exit 1
    }
    exit 0
}

if ($Action -eq "WeeklyDigest") {
    Send-WeeklyDigest
    exit 0
}

if ($Action -eq "WeeklyFromGui") {
    try {
        Send-WeeklyDigest
        Show-LauncherMessage -Text "本周周报已发送。"
    }
    catch {
        Show-LauncherMessage -Text $_.Exception.Message -Title "周报发送失败"
        exit 1
    }
    exit 0
}

if ($Action -eq "Gui") {
    $launcherForm = New-LauncherForm
    [void]$launcherForm.ShowDialog()
}

