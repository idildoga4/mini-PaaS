FROM python:3.9-slim

WORKDIR /app

# Flask kütüphanesini kuruyoruz
RUN pip install flask requests

# main.py dosyasını içeri atıyoruz
COPY main.py .

# Uygulamayı başlatıyoruz
CMD ["python", "main.py"]