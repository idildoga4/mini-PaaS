Write-Host ">>> .env dosyasi okunuyor ve degiskenler yukleniyor..." -ForegroundColor Cyan
if (Test-Path ".env") {
    Get-Content .env | Where-Object { $_ -match "^[^#]" -and $_ -match "=" } | ForEach-Object {
        $name, $value = $_.Split("=", 2)
        Set-Item -Path "env:\$($name.Trim())" -Value $value.Trim()
    }
} else {
    Write-Host "!!! UYARI: .env dosyasi bulunamadi!" -ForegroundColor Red
}


# 1. Notification
Write-Host "--- Starting Mini PaaS Smart Deploy ---" -ForegroundColor Cyan

$services = @("builder-service", "auth-service", "deploy-service", "github-service", "dashboard-service")

foreach ($service in $services) {
    # This file keeps track of the last build time for the service
    $trackerFile = "./$service/.lastbuild"
    
    # Find the most recently modified file in the service directory (excluding the tracker file)
    $latestFile = Get-ChildItem -Path "./$service" -Recurse -File | 
                  Where-Object { $_.Name -ne ".lastbuild" } | 
                  Sort-Object LastWriteTime -Descending | 
                  Select-Object -First 1

    $needsBuild = $true

    # Compare dates if we have built this service before
    if (Test-Path $trackerFile) {
        $lastBuildTime = [datetime](Get-Content $trackerFile)
        # If the latest file modification is older than or equal to our last build, no need to build
        if ($latestFile.LastWriteTime -le $lastBuildTime) {
            $needsBuild = $false
        }
    }

    if ($needsBuild) {
        Write-Host ">>> [NEW CODE] Changes detected! Building and updating: $service" -ForegroundColor Yellow
        
        # 1. Build only the changed service
        docker build -t "mini-paas/${service}:latest" "./$service"
        
        if ($LASTEXITCODE -ne 0) {
            Write-Host "!!! Error: Failed to build $service!" -ForegroundColor Red
            exit
        }
        
        # 2. Force update the changed service on Swarm
        docker service update --image "mini-paas/${service}:latest" --force "mini-paas_${service}"
        
        # 3. Save the current time to the tracker file if successful
        $latestFile.LastWriteTime.ToString("o") | Out-File $trackerFile
    } else {
        # Skip if no changes
        Write-Host "--- [SKIP] No changes detected, skipping: $service" -ForegroundColor DarkGray
    }
}

# Ensure stack configurations (ports, environment vars, etc.) are up to date
Write-Host ">>> Verifying stack configurations..." -ForegroundColor Cyan
docker stack deploy -c docker-stack.yml mini-paas

Write-Host "--- Smart Deploy Completed successfully! ---" -ForegroundColor Green