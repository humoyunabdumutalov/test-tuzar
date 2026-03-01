from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Universal Test Boti 24/7 Onlayn! Server muvaffaqiyatli ishlayapti."

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()