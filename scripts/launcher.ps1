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
