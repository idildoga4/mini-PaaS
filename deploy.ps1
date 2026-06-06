Get-Content .env | Where-Object { $_ -match '^\w' } | ForEach-Object {
    $key, $val = $_ -split '=', 2
    [System.Environment]::SetEnvironmentVariable($key, $val, 'Process')
}
docker stack deploy -c docker-stack.yml mini-paas
Write-Host "Stack deploy edildi."
