param(
    [switch]$SkipSmtp,
    [switch]$SkipOpenRouter
)

$ErrorActionPreference = "Stop"
$envPath = Join-Path $PSScriptRoot ".env"

function Convert-FromSecureValue([SecureString]$Value) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$smtpUser = ""
$smtpPass = ""
$smtpFrom = ""
if (-not $SkipSmtp) {
    $smtpUser = Read-Host "Gmail address for login codes"
    if ($smtpUser) {
        $smtpPass = (Convert-FromSecureValue (Read-Host "Gmail App Password" -AsSecureString)).Replace(" ", "")
        $smtpFrom = $smtpUser
    }
}

$openRouterKey = ""
if (-not $SkipOpenRouter) {
    $openRouterKey = Convert-FromSecureValue (Read-Host "OpenRouter API key (Enter to skip)" -AsSecureString)
}

$authBytes = New-Object byte[] 48
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
$random.GetBytes($authBytes)
$random.Dispose()
$authSecret = [Convert]::ToBase64String($authBytes)

$lines = @(
    "APP_ENV=development"
    "SMTP_HOST=smtp.gmail.com"
    "SMTP_PORT=587"
    "SMTP_USER=$smtpUser"
    "SMTP_PASS=$smtpPass"
    "SMTP_FROM=$smtpFrom"
    "AUTH_SECRET=$authSecret"
    "AUTH_REQUIRED=true"
    "DEV_SHOW_OTP=$($smtpUser -eq '')"
    "SESSION_HOURS=24"
    "OTP_MINUTES=10"
    "OPENROUTER_API_KEY=$openRouterKey"
    "OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free"
    "OPENROUTER_MODELS=nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3-ultra-550b-a55b:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,nvidia/nemotron-nano-9b-v2:free"
)

[IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))
Write-Host "Configuration written to $envPath" -ForegroundColor Green
Write-Host "Restart FastAPI so it loads the new settings." -ForegroundColor Cyan
