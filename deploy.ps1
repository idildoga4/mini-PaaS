Write-Host "MiniPaaS Deployment Baslatiliyor..." -ForegroundColor Cyan

Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}

docker stack deploy -c docker-stack.yml mini-paas

Write-Host "Deployment Basarili! Servisler ayaga kalkiyor..." -ForegroundColor Green