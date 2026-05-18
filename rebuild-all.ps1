# Tüm servisleri rebuild edip güncelle
docker build -t mini-paas/auth-service:latest    .\auth-service
docker build -t mini-paas/deploy-service:latest  .\deploy-service
docker build -t mini-paas/github-service:latest  .\github-service
docker build -t mini-paas/builder-service:latest .\builder-service
docker stack deploy -c docker-stack.yml mini-paas