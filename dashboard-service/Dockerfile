FROM nginx:alpine

# index.html'i nginx'in default web root'una kopyala
COPY index.html /usr/share/nginx/html/index.html

# Nginx config: SPA için tüm route'ları index.html'e yönlendir
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
