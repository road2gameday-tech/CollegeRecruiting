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

# Data + admin
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "college_baseball_programs_merged_1000.csv")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Gameday2025!!")

# Email (optional server-side submission email)
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM")
SUBMISSION_TO = os.environ.get("SUBMISSION_TO", "road2gameday@gmail.com")

# UI lists
DIVISIONS = ["D1", "D2", "D3", "NAIA", "JUCO"]
REGIONS = ["West", "Southwest", "Midwest", "South", "Southeast", "Northeast", "Central", "Mid-Atlantic", "Pacific"]  # no Rockies

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def load_colleges():
    """Read CSV case-insensitively and map common header variants to our keys.
       Also parses a single Location field like 'City, State (Region)'. """
    import re

    def first_match(row, *candidates):
        # exact
        for c in candidates:
            if c in row and row[c]:
                return str(row[c]).strip()
        # case-insensitive
        lower = {k.lower(): v for k, v in row.items()}
        for c in candidates:
            v = lower.get(c.lower())
            if v:
                return str(v).strip()
        return ""

    def parse_location(loc: str):
        """Parse 'City, State (Region)' or 'City, State' into parts."""
        loc = (loc or "").strip()
        if not loc:
            return "", "", ""
        m = re.match(r"^\s*([^,]+)\s*,\s*([^(]+?)(?:\s*\(([^)]+)\))?\s*$", loc)
        if m:
            city = m.group(1).strip()
            state = m.group(2).strip()
            region = (m.group(3) or "").strip()
            return city, state, region
        return "", "", ""

    rows = []
    with open(DATA_PATH, newline='', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            school_name = first_match(
                raw, "school_name", "school", "name", "college", "college_name",
                "university", "university_name", "institution", "program", "team", "program_name"
            )
            division    = first_match(raw, "division", "level", "ncaa_division", "assoc", "association", "league")
            region      = first_match(raw, "region", "geo", "geography", "area", "territory")
            location    = first_match(raw, "location", "city_state", "city_state_region", "school_location")

            city        = first_match(raw, "city", "town")
            state       = first_match(raw, "state", "st", "province")

            # Unpack a combined location if needed
            if (not city or not state) and location:
                c2, s2, r2 = parse_location(location)
                city = city or c2
                state = state or s2
                region = region or r2

            coach_name  = first_match(
                raw, "coach_name", "head_coach", "coach", "contact_name",
                "recruiting_coordinator", "assistant_coach"
            )
            coach_email = first_match(
                raw, "coach_email", "email", "coach email", "head_coach_email", "contact_email",
                "recruiting_email", "assistant_email", "primary_email"
            )
            min_gpa     = first_match(raw, "min_gpa", "minimum_gpa", "gpa_min", "gpa_requirement", "gpa")
            majors      = first_match(raw, "majors", "programs", "fields_of_study", "degree_programs")

            # Normalize division
            div_map = {
                "ncaa d1": "D1", "ncaa division i": "D1", "division i": "D1", "d1": "D1", "div i": "D1",
                "ncaa d2": "D2", "ncaa division ii": "D2", "division ii": "D2", "d2": "D2", "div ii": "D2",
                "ncaa d3": "D3", "ncaa division iii": "D3", "division iii": "D3", "d3": "D3", "div iii": "D3",
                "naia": "NAIA", "juco": "JUCO", "njcaa": "JUCO"
            }
            div_norm = div_map.get(_norm(division), (division or "").strip())

            rows.append({
                "school_name": school_name,
                "division": div_norm,
                "region": region,
                "city": city,
                "state": state,
                "coach_name": coach_name,
                "coach_email": coach_email,
                "min_gpa": min_gpa or "0",
                "majors": majors or "",
            })
    return rows

def compute_match_score(player, row):
    score = 0.0
    weights = {"division": 0.35, "region": 0.25, "academics": 0.25, "major": 0.15}

    if player['division'] and _norm(row['division']) == _norm(player['division']):
        score += weights["division"]
    if player['region'] and _norm(row['region']) == _norm(player['region']):
        score += weights["region"]

    try:
        min_gpa = float(row.get('min_gpa') or 0)
    except (ValueError, TypeError):
        min_gpa = 0.0
    try:
        gpa = float(player.get('gpa') or 0)
    except (ValueError, TypeError):
        gpa = 0.0

    if gpa >= min_gpa:
        diff = max(0.0, min(gpa - min_gpa, 1.0))
        score += weights["academics"] * (0.6 + 0.4 * diff)

    majors = [m.strip().lower() for m in (row.get('majors') or '').split("|") if m.strip()]
    intended_major = (player.get('major') or '').strip().lower()
    if intended_major and intended_major in majors:
        score += weights["major"]

    return round(score * 100, 2)

def send_submission_email(player):
    # Optional server-side email of player submissions
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

        all_rows = load_colleges()
        rows = list(all_rows)

        # Progressive filtering: only keep a filter if it yields matches
        if player["division"]:
            f_div = [r for r in rows if _norm(r.get("division")) == _norm(player["division"])]
            if f_div:
                rows = f_div
        if player["region"]:
            f_reg = [r for r in rows if _norm(r.get("region")) == _norm(player["region"])]
            if f_reg:
                rows = f_reg

        if not rows:
            rows = all_rows  # show something, still ranked by match

        # Score + mailto
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

        rows.sort(key=lambda x: x.get('match_score', 0), reverse=True)
        # Note: the template label changed from "Score" to "Best Match"; values stay the same.
        return render_template("results.html", player=player, results=rows[:200])

    return render_template("index.html", divisions=DIVISIONS, regions=REGIONS)

# ----- Admin -----
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
    # keep filename 'colleges.csv' for the download UX
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
