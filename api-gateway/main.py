from flask import Flask, request
import requests

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Ana Sayfa: API Gateway Calisiyor!</h1>"

@app.route('/deploy')
def deploy():
    return "<h1>Talimat Verildi!</h1><p>Bu bir test mesajidir.</p>"

if __name__ == '__main__':
    # Kesinlikle 80 portunda çalışmalı!
    app.run(host='0.0.0.0', port=80)