# builder-service/main.py
from flask import Flask
import time

app = Flask(__name__)

@app.route('/baslat')
def baslat():
    # Burada normalde Docker build işlemi yapılacak.
    # Şimdilik taklit yapıyoruz.
    print("LOG: Kod indiriliyor...", flush=True)
    time.sleep(1)
    print("LOG: Docker imaji olusturuluyor...", flush=True)
    return "Basariyla Build Edildi! (Temsili)"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)