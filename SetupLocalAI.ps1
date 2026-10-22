param([string]$Model = 'qwen3:4b-instruct')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$localAIPath = Join-Path $PSScriptRoot 'data\ollama'
$localAIArchive = Join-Path $localAIPath 'runtime.zip'
$localAIRuntime = Join-Path $localAIPath 'runtime'
New-Item -ItemType Directory -Path $localAIPath -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $localAIRuntime 'ollama.exe'))) {
    if (-not (Test-Path -LiteralPath $localAIArchive)) {
        curl.exe -L --fail --retry 2 -o $localAIArchive 'https://github.com/ollama/ollama/releases/download/v0.40.3/ollama-windows-amd64.zip'
        if ($LASTEXITCODE -ne 0) { throw 'Ollama download failed. Remove the partial runtime.zip before retrying.' }
    }
    $localAIHash = (Get-FileHash -LiteralPath $localAIArchive -Algorithm SHA256).Hash
    if ($localAIHash -ne 'e5ecff02dfc7cae8ddf6d6b7a7ac5f727ffeedf4491f44ae75ff5b0820dbbbd5') {
        throw 'Ollama download checksum mismatch. Remove runtime.zip and retry.'
    }
    & '.\.venv\Scripts\python.exe' -m zipfile -e $localAIArchive $localAIRuntime
    if ($LASTEXITCODE -ne 0) { throw 'Ollama extraction failed' }
}
& '.\.venv\Scripts\python.exe' -m easy_apply.setup_local --model $Model
if ($LASTEXITCODE -ne 0) { throw 'Local model setup failed' }
