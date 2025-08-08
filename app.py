import os
import smtplib
import csv
from email.message import EmailMessage
from urllib.parse import quote
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from email_validator import validate_email, EmailNotValidError

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev_key_change_me')

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "college_baseball_programs_merged_1000.csv")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Gameday2025!!")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM")
SUBMISSION_TO = os.environ.get("SUBMISSION_TO", "road2gameday@gmail.com")

DIVISIONS = ["D1", "D2", "D3", "NAIA", "JUCO"]
REGIONS = ["West", "Southwest", "Midwest", "South", "Southeast", "Northeast", "Central", "Mid-Atlantic", "Pacific"]

def load_colleges():
    rows = []
    with open(DATA_PATH, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            # Normalize fields
            r['division'] = (r.get('division') or '').strip()
            div_map = {
                'NCAA D1': 'D1', 'NCAA Division I': 'D1', 'Division I': 'D1',
                'NCAA D2': 'D2', 'NCAA Division II': 'D2', 'Division II': 'D2',
                'NCAA D3': 'D3', 'NCAA Division III': 'D3', 'Division III': 'D3'
            }
            r['division'] = div_map.get(r['division'], r['division'])
            r['region'] = (r.get('region') or '').strip()
            r['majors'] = r.get('majors') or ''
            rows.append(r)
    return rows

def compute_match_score(player, row):
    score = 0.0
    weights = {"division": 0.35, "region": 0.25, "academics": 0.25, "major": 0.15}
    if player['division'] and row['division'].lower() == player['division'].lower():
        score += weights["division"]
    if player['region'] and row['region'].lower() == player['region'].lower():
        score += weights["region"]
    try:
        min_gpa = float(row.get('min_gpa') or 0)
    except ValueError:
        min_gpa = 0.0
    try:
        gpa = float(player.get('gpa') or 0)
    except ValueError:
        gpa = 0.0
    if gpa >= min_gpa:
        diff = max(0.0, min(gpa - min_gpa, 1.0))
        score += weights["academics"] * (0.6 + 0.4 * diff)
    majors = [m.strip().lower() for m in row['majors'].split("|") if m.strip()]
    intended_major = (player.get('major') or '').strip().lower()
    if intended_major and intended_major in majors:
        score += weights["major"]
    return round(score * 100, 2)

def send_submission_email(player):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS, SMTP_FROM]):
        return False, "SMTP not configured; skipped server-side email."
    try:
        msg = EmailMessage()
        msg["Subject"] = f"Gameday Recruiting Submission: {player.get('name','(no name)')}"
        msg["From"] = SMTP_FROM
        msg["To"] = SUBMISSION_TO
        body = f"""
Player Name: {player.get('name')}
GPA: {player.get('gpa')}
SAT: {player.get('sat')}
Preferred Region: {player.get('region')}
Preferred Division: {player.get('division')}
Intended Major: {player.get('major')}
Hudl/Video: {player.get('video_link')}

-- Sent automatically by Gameday Recruiting Matchmaker
"""
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True, "Submission email sent."
    except Exception as e:
        return False, f"Email error: {e}"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        player = {
            "name": request.form.get("name","").strip(),
            "gpa": request.form.get("gpa","").strip(),
            "sat": request.form.get("sat","").strip(),
            "region": request.form.get("region","").strip(),
            "division": request.form.get("division","").strip(),
            "major": request.form.get("major","").strip(),
            "video_link": request.form.get("video_link","").strip(),
        }
        send_submission_email(player)

        rows = load_colleges()
        # Filter by division & region
        if player["division"]:
            rows = [r for r in rows if r['division'].lower() == player['division'].lower()]
        if player["region"]:
            rows = [r for r in rows if r['region'].lower() == player['region'].lower()]

        # Compute scores & mailto
        for r in rows:
            r['match_score'] = compute_match_score(player, r)
            email = (r.get("coach_email") or "").strip()
            try:
                validate_email(email)
                subject = f"Recruiting Interest: {player['name']} – {r.get('school_name','')} Baseball"
                body = f"""Coach {r.get('coach_name','')},

My name is {player['name']}. I'm interested in {r.get('school_name','')} ({r.get('division','')}) and would love to learn more about your program.

Player Snapshot:
• GPA: {player['gpa']}
• SAT: {player['sat']}
• Intended Major: {player['major']}
• Region Preference: {player['region']}
• Video/Hudl: {player['video_link']}

I would appreciate any guidance on your evaluation process and upcoming opportunities to be seen.

Thank you for your time,
{player['name']}
"""
                r['mailto'] = f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"
            except EmailNotValidError:
                r['mailto'] = None

        # Sort by match_score
        rows.sort(key=lambda x: x['match_score'], reverse=True)
        return render_template("results.html", player=player, results=rows)

    return render_template("index.html", divisions=DIVISIONS, regions=REGIONS)

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password","")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        else:
            flash("Invalid password.", "error")
    return render_template("admin_login.html")

@app.route("/admin/panel")
def admin_panel():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return render_template("admin_panel.html")

@app.route("/admin/export")
def admin_export():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    return send_file(DATA_PATH, as_attachment=True, download_name="colleges.csv")

@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    file = request.files.get("file")
    if not file:
        flash("No file uploaded.", "error")
        return redirect(url_for("admin_panel"))
    file.save(DATA_PATH)
    flash("College list updated.", "success")
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    app.run(debug=True)
