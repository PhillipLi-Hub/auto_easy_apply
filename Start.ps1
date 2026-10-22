$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$appPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $appPython)) {
    throw 'Run Setup.ps1 first to create the project environment.'
}
Start-Process -FilePath $appPython -ArgumentList '-m','easy_apply' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
