import os, threading, webview
from pathlib import Path
from subprocess import Popen

BASE = Path(__file__).resolve().parent
WEB_APP = (BASE / ".." / "web_app").resolve()

def run_flask():
    env = os.environ.copy()
    env["FLASK_APP"] = str(WEB_APP / "app.py")
    os.chdir(str(WEB_APP))
    Popen(["python", "app.py"], env=env)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    window = webview.create_window("Gameday Recruiting Matchmaker", "http://127.0.0.1:5000", width=1200, height=800)
    webview.start()
