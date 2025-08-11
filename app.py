import os
import smtplib
import csv
import re
from email.message import EmailMessage
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from email_validator import validate_email, EmailNotValidError

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev_key_change_me')

# Paths / config
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "college_baseball_programs_merged_1000.csv")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Gameday2025!!")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM")
SUBMISSION_TO = os.environ.get("SUBMISSION_TO", "road2gameday@gmail.com")

DIVISIONS = ["D1", "D2", "D3", "NAIA", "JUCO"]
REGIONS = ["West", "Southwest", "Midwest", "South", "Southeast", "Northeast", "Central", "Mid-Atlantic", "Pacific"]  # no Rockies

# ---------- helpers ----------
def _norm(s: str) -> str:
    return (s or "").strip().lower()

NAME_HINTS = (
    "university", "college", "state", "tech", "institute", "polytechnic",
    "community", "cc", "academy", "school"
)

def _parse_location(loc: str):
    """Parse 'City, State (Region)' or 'City, State' -> (city, state, region)."""
    loc = (loc or "").strip()
    if not loc:
        return "", "", ""
    m = re.match(r"^\s*([^,]+)\s*,\s*([^(]+?)(?:\s*\(([^)]+)\))?\s*$", loc)
    if m:
        return m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
    return "", "", ""

def _first_match(row, *cands):
    # exact
    for c in cands:
        if c in row and row[c]:
            return str(row[c]).strip()
    # case-insensitive
    lower = {k.lower(): v for k, v in row.items()}
    for c in cands:
        v = lower.get(c.lower())
        if v:
            return str(v).strip()
    return ""

def _derive_school_name(row):
    """Heuristic: scan all values and pick the most school-like string."""
    best = ""
    for _, v in row.items():
        val = str(v or "").strip()
        if not val:
            continue
        low = val.lower()
        if any(h in low for h in NAME_HINTS) and len(val) >= len(best):
            if "@" in val or val.startswith("http"):
                continue
            best = val
    return best

def _parse_min_gpa(value):
    """Extract a float like 3.2 from '>=3.2', '3.0 preferred', etc. Return None if missing."""
    s = str(value or "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        g = float(m.group(1))
        return g if 0.0 <= g <= 5.0 else None
    except ValueError:
        return None

# --- US state helpers for city/state guessing ---
US_STATES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado",
    "CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho",
    "IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
    "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
    "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
    "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"District of Columbia"
}
STATE_ABBRS = set(US_STATES.keys())
STATE_NAMES = {name.lower(): abbr for abbr, name in US_STATES.items()}

def _guess_city_state_from_row(raw):
    """
    Scan any column for something that looks like City, ST or City, State
    (optionally with ZIP or extra text) and return (City, ST).
    """
    def abbr_from(name_or_abbr: str):
        up = name_or_abbr.strip().upper()
        if up in STATE_ABBRS:
            return up
        low = name_or_abbr.strip().lower()
        return STATE_NAMES.get(low, "")

    patterns = [
        r"(?P<city>[A-Za-z .'-]+),\s*(?P<st>[A-Z]{2})(?:\s*\d{5}(?:-\d{4})?)?",
        r"(?P<city>[A-Za-z .'-]+),\s*(?P<state>[A-Za-z .'-]+)",
    ]

    for v in raw.values():
        s = str(v or "").strip()
        if not s or "@" in s or s.startswith("http"):
            continue

        # try structured parse first
        city, state_txt, _ = _parse_location(s)
        if city and state_txt:
            st = abbr_from(state_txt)
            if st:
                return city.strip(), st

        # regex inside the string
        for pat in patterns:
            m = re.search(pat, s)
            if not m:
                continue
            city = (m.groupdict().get("city") or "").strip()
            st = m.groupdict().get("st")
            if not st:
                state_name = (m.groupdict().get("state") or "").strip()
                st = abbr_from(state_name)
            if city and st:
                return city, st

    return "", ""

# ---------- data loader ----------
def load_colleges():
    rows = []
    with open(DATA_PATH, newline='', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            # SCHOOL NAME (many variants + heuristic fallback)
            school_name = _first_match(
                raw, "school_name", "school", "name", "college", "college_name",
                "university", "university_name", "institution", "program", "team", "program_name"
            )
            if not school_name:
                school_name = _derive_school_name(raw)

            # DIVISION
            division = _first_match(raw, "division", "level", "ncaa_division", "assoc", "association", "league")
            div_map = {
                "ncaa d1": "D1", "ncaa division i": "D1", "division i": "D1", "d1": "D1", "div i": "D1",
                "ncaa d2": "D2", "ncaa division ii": "D2", "division ii": "D2", "d2": "D2", "div ii": "D2",
                "ncaa d3": "D3", "ncaa division iii": "D3", "division iii": "D3", "d3": "D3", "div iii": "D3",
                "naia": "NAIA", "juco": "JUCO", "njcaa": "JUCO"
            }
            division = div_map.get(_norm(division), (division or "").strip())

            # REGION / CITY / STATE (accept combined fields)
            region   = _first_match(raw, "region", "geo", "geography", "area", "territory")
            location = _first_match(raw, "location", "city_state", "city_state_region", "school_location")
            city     = _first_match(raw,
                        "city","town","campus_city","school_city","hq_city",
                        "mailing_city","address_city","location_city")
            state    = _first_match(raw,
                        "state","st","province","campus_state","school_state","hq_state",
                        "mailing_state","address_state","location_state")

            # If REGION looks like "City, State (Region)", unpack it
            if region and (("," in region) or ("(" in region and ")" in region)):
                c2, s2, r2 = _parse_location(region)
                if c2 or s2 or r2:
                    city = city or c2
                    state = state or s2
                    region = r2 or region

            # If still missing city/state and we have a separate location field, unpack it
            if (not city or not state) and location:
                c2, s2, r2 = _parse_location(location)
                city = city or c2
                state = state or s2
                if not region:
                    region = r2

            # Final fallback: guess city/state from ANY column text
            if not city or not state:
                gc, gs = _guess_city_state_from_row(raw)
                city = city or gc
                state = state or gs

            # COACH / EMAIL
            coach_name  = _first_match(
                raw, "coach_name", "head_coach", "coach", "contact_name",
                "recruiting_coordinator", "assistant_coach"
            )
            coach_email = _first_match(
                raw, "coach_email", "email", "coach email", "head_coach_email", "contact_email",
                "recruiting_email", "assistant_email", "primary_email"
            )

            # MIN GPA (many variants, parsed to float or None)
            min_gpa_raw = _first_match(
                raw, "min_gpa", "minimum_gpa", "gpa_min", "gpa_requirement", "preferred_gpa",
                "academic_floor", "acad_floor", "gpa floor", "min. gpa", "min gpa",
                "req_gpa", "required_gpa", "admissions_gpa", "avg_gpa", "average_gpa", "gpa"
            )
            min_gpa = _parse_min_gpa(min_gpa_raw)

            # MAJORS
            majors = _first_match(raw, "majors", "programs", "fields_of_study", "degree_programs")

            rows.append({
                "school_name": school_name,
                "division": division,
                "region": region,
                "city": city,
                "state": state,
                "coach_name": coach_name,
                "coach_email": coach_email,
                "min_gpa": min_gpa,   # None if not present
                "majors": majors or "",
            })
    return rows

# ---------- scoring ----------
def compute_match_score(player, row):
    score = 0.0
    weights = {"division": 0.35, "region": 0.25, "academics": 0.25, "major": 0.15}

    if player['division'] and _norm(row['division']) == _norm(player['division']):
        score += weights["division"]
    if player['region'] and _norm(row['region']) == _norm(player['region']):
        score += weights["region"]

    try:
        gpa = float(player.get('gpa') or 0)
    except (ValueError, TypeError):
        gpa = 0.0

    # Treat None as 0 for comparison
    min_gpa_val = row.get('min_gpa')
    min_gpa_num = float(min_gpa_val) if isinstance(min_gpa_val, (int, float)) else 0.0
    if gpa >= min_gpa_num:
        diff = max(0.0, min(gpa - min_gpa_num, 1.0))
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

# ---------- routes ----------
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

My name is {r.get('school_name','')} ({r.get('division','')}) and would love to learn more about your program.

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
            except EmailNotValidError:
                r['mailto'] = None
            else:
                r['mailto'] = f"mailto:{email}?subject={quote(subject)}&body={quote(body)}"

        rows.sort(key=lambda x: x.get('match_score', 0), reverse=True)
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
# Add to app.py
@app.route("/admin/audit-missing-location")
def admin_audit_missing_location():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    rows = load_colleges()
    missing = [r for r in rows if not (r.get("city") and r.get("state"))]
    # write a small CSV for cleanup
    out_path = os.path.join(os.path.dirname(__file__), "data", "needs_location_cleanup.csv")
    import csv
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "school_name","division","region","city","state","coach_name","coach_email","min_gpa","majors"
        ])
        w.writeheader()
        for r in missing:
            w.writerow(r)
    return send_file(out_path, as_attachment=True, download_name="needs_location_cleanup.csv")
