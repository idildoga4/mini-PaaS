#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  Mini PaaS — Swarm Kurulum ve Deploy Scripti
#  Kullanım: bash swarm-deploy.sh
# ══════════════════════════════════════════════════════════════

set -e  # Hata olursa dur

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[→]${NC} $1"; }

echo ""
echo "╔══════════════════════════════════════╗"
echo "║     Mini PaaS — Swarm Deploy         ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. .env kontrolü ──────────────────────────────────────────
if [ ! -f ".env" ]; then
    warn ".env dosyası bulunamadı, .env.example'dan kopyalanıyor..."
    cp .env.example .env
    err ".env dosyasını düzenleyin ve scripti tekrar çalıştırın: nano .env"
fi
log ".env dosyası mevcut"

# ── 2. Docker Swarm init ───────────────────────────────────────
SWARM_STATUS=$(docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || echo "inactive")

if [ "$SWARM_STATUS" = "inactive" ]; then
    info "Docker Swarm başlatılıyor..."
    docker swarm init
    log "Swarm başlatıldı (manager node)"
else
    log "Swarm zaten aktif (durum: $SWARM_STATUS)"
fi

# Worker node token'ını göster
echo ""
warn "Worker node eklemek istiyorsanız aşağıdaki komutu diğer makinede çalıştırın:"
echo "─────────────────────────────────────────────────────"
docker swarm join-token worker 2>/dev/null | grep "docker swarm join" || true
echo "─────────────────────────────────────────────────────"
echo ""

# ── 3. Docker image'ları build et ─────────────────────────────
info "API Gateway image build ediliyor..."
docker build -t mini-paas-api:latest ./api-gateway
log "API Gateway image hazır"

info "Builder Service image build ediliyor..."
docker build -t mini-paas-builder:latest ./builder-service
log "Builder Service image hazır"

# ── 4. paas-net overlay network oluştur ───────────────────────
if ! docker network ls | grep -q "paas-net"; then
    info "paas-net overlay network oluşturuluyor..."
    docker network create --driver overlay --attachable paas-net
    log "paas-net oluşturuldu"
else
    log "paas-net zaten mevcut"
fi

# ── 5. Stack deploy ───────────────────────────────────────────
info "Stack deploy ediliyor..."
docker stack deploy -c docker-stack.yml paas --with-registry-auth
log "Stack deploy edildi"

# ── 6. Servislerin ayağa kalkmasını bekle ─────────────────────
echo ""
info "Servisler başlatılıyor, bekleniyor..."
sleep 8

# ── 7. Durum raporu ───────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════╗"
echo "║         Servis Durumu                ║"
echo "╚══════════════════════════════════════╝"
docker stack services paas

echo ""
log "Deploy tamamlandı!"
echo ""
echo "  🌐  Dashboard   → http://localhost:8090"
echo "  🔌  API         → http://localhost:5001"
echo "  📊  Grafana     → http://localhost:3000  (admin / \${GRAFANA_PASSWORD:-admin})"
echo "  🔥  Prometheus  → http://localhost:9090"
echo "  🚦  Traefik UI  → http://localhost:8080"
echo ""
echo "  Logları görmek için:"
echo "  docker service logs paas_api-gateway -f"
echo "  docker service logs paas_builder-service -f"
echo ""
