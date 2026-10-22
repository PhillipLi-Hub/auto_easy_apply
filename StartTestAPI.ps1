param([ValidateRange(1, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$apiPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $apiPython)) {
    throw 'Run Setup.ps1 first to create the project environment.'
}
Write-Host 'Connect your phone to the same Wi-Fi as this PC, then open one of these URLs:'
Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' -and $_.AddressState -eq 'Preferred'
} | ForEach-Object { Write-Host ('  http://{0}:{1}/api/test' -f $_.IPAddress, $Port) }
Write-Host 'If Windows Firewall asks, allow Python on your trusted Private network.'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $apiPython -m easy_apply.test_api --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Test API exited with an error.' }
} finally {
    Pop-Location
}
