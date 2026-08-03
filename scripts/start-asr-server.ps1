$projectRoot = Split-Path -Parent $PSScriptRoot
$server = Join-Path $projectRoot 'models\llama.cpp\llama-server.exe'
$model = Join-Path $projectRoot 'models\qwen3-asr\Qwen3-ASR-1.7B.Q8_0.gguf'
$mmproj = Join-Path $projectRoot 'models\qwen3-asr\mmproj-Qwen3-ASR-1.7B-Q8_0.gguf'
Start-Process -FilePath $server -ArgumentList @('-m', $model, '--mmproj', $mmproj, '--host', '127.0.0.1', '--port', '8099') -WindowStyle Hidden
Write-Output 'ASR-SERVER-STARTED'
