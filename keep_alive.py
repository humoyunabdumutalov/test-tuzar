import os
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Universal Test Boti 24/7 Onlayn! Render serveri ishlayapti."

def run():
    # Render o'zi bergan portni aniqlaymiz, topolmasa 8080 ni ishlatamiz
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
