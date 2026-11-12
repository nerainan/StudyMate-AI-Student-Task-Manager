"""
StudyMate v3 - multi-user planner with login

Run with:
    py -m pip install streamlit pandas
    py -m streamlit run studymate_v3.py
(single file - no other files needed)
(uses the same tasks.db as v1; old data keeps working)
"""

import calendar
import hashlib
import hmac
import html
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


st.set_page_config(page_title="StudyMate", page_icon="🎓", layout="wide")

DB_NAME = "tasks.db"
PRIORITIES = ["High", "Medium", "Low"]
PRIORITY_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}
PRIORITY_COLOR = {"High": "#EF4444", "Medium": "#F59E0B", "Low": "#22C55E"}
PRIORITY_XP = {"High": 30, "Medium": 20, "Low": 10}
SUBJECT_COLORS = ["#6366F1", "#EC4899", "#14B8A6", "#F97316",
                  "#8B5CF6", "#0EA5E9", "#84CC16", "#E11D48"]
THEMES = {                       # main colour, second colour, page background
    "🌊 Ocean": ("#2563EB", "#06B6D4", "#F0F7FF"),
    "🌅 Sunset": ("#F97316", "#EC4899", "#FFF6F0"),
    "🌿 Forest": ("#059669", "#84CC16", "#F1FBF4"),
    "🍇 Grape": ("#7C3AED", "#EC4899", "#F6F2FF"),
}
QUOTES = [
    "Small steps every day lead to big results.",
    "Don't watch the clock; do what it does. Keep going.",
    "Future you is cheering for you right now.",
    "Done is better than perfect.",
    "Discipline is choosing what you want most over what you want now.",
    "You don't have to be great to start, but you have to start to be great.",
    "Focus for 25 minutes. You can do anything for 25 minutes.",
]
COLUMNS = ["id", "title", "subject", "deadline", "priority", "completed",
           "est_hours", "notes", "completed_on"]
today = date.today()


# ================= LOGIN SYSTEM =================
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ITERATIONS = 200_000
MAX_ATTEMPTS = 5
LOCK_MINUTES = 5
GENERIC_FAIL = "Invalid email or password."


# ---------------- AUTH: DATABASE ----------------

def _run(query, params=(), fetch=False):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            cur = conn.execute(query, params)
            if fetch:
                return [dict(r) for r in cur.fetchall()]
    return None


def init_auth_tables():
    _run("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL,
        rc_hash TEXT NOT NULL, rc_salt TEXT NOT NULL,
        created_at TEXT NOT NULL,
        failed_attempts INTEGER DEFAULT 0,
        locked_until TEXT,
        daily_goal REAL DEFAULT 2,
        theme TEXT DEFAULT '🌊 Ocean')""")


def _row_by_email(email):
    rows = _run("SELECT * FROM users WHERE email=?", (email,), fetch=True)
    return rows[0] if rows else None


def _row_by_id(uid):
    rows = _run("SELECT * FROM users WHERE id=?", (uid,), fetch=True)
    return rows[0] if rows else None


def _public(row):
    return {k: row[k] for k in ("id", "email", "name", "daily_goal", "theme")}


def get_user(uid):
    return _public(_row_by_id(uid))


# ---------------- HASHING & RULES ----------------

def hash_secret(secret, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, ITERATIONS)
    return digest.hex(), salt.hex()


def verify_secret(secret, hash_hex, salt_hex):
    digest, _ = hash_secret(secret, salt_hex)
    return hmac.compare_digest(digest, hash_hex)


def password_error(password):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        return "Password needs at least one letter and one number."
    return None


def new_recovery_code():
    return "-".join(secrets.token_hex(2).upper() for _ in range(3))


def _locked_message(row):
    until = row["locked_until"]
    if until and datetime.fromisoformat(until) > datetime.now():
        secs = (datetime.fromisoformat(until) - datetime.now()).total_seconds()
        return f"Too many failed attempts. Try again in {int(secs // 60) + 1} min."
    return None


def _record_failure(row):
    attempts = row["failed_attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        until = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat()
        _run("UPDATE users SET failed_attempts=0, locked_until=? WHERE id=?",
             (until, row["id"]))
    else:
        _run("UPDATE users SET failed_attempts=? WHERE id=?", (attempts, row["id"]))


def _clear_failures(row):
    _run("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (row["id"],))


# ---------------- ACCOUNT ACTIONS ----------------

def register(email, name, password):
    """Returns (user, recovery_code) on success, or (None, error_message)."""
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return None, "Please enter a valid email address."
    error = password_error(password)
    if error:
        return None, error
    if _row_by_email(email):
        return None, "An account with this email already exists."

    name = name.strip()[:60] or email.split("@")[0]
    pw_hash, pw_salt = hash_secret(password)
    code = new_recovery_code()
    rc_hash, rc_salt = hash_secret(code)
    first_user = not _run("SELECT 1 FROM users LIMIT 1", fetch=True)
    try:
        _run("INSERT INTO users (email, name, pw_hash, pw_salt, rc_hash, rc_salt, "
             "created_at) VALUES (?,?,?,?,?,?,?)",
             (email, name, pw_hash, pw_salt, rc_hash, rc_salt,
              datetime.now().isoformat()))
    except sqlite3.IntegrityError:
        return None, "An account with this email already exists."

    user = _public(_row_by_email(email))
    if first_user:  # the first account inherits tasks created before logins existed
        _run("UPDATE tasks SET user_id=? WHERE user_id IS NULL", (user["id"],))
    return user, code


def login(email, password):
    """Returns (user, "") on success, or (None, error_message)."""
    row = _row_by_email(email.strip().lower())
    if not row:
        hash_secret(password)  # keeps response time similar for unknown emails
        return None, GENERIC_FAIL
    locked = _locked_message(row)
    if locked:
        return None, locked
    if verify_secret(password, row["pw_hash"], row["pw_salt"]):
        _clear_failures(row)
        return _public(row), ""
    _record_failure(row)
    return None, GENERIC_FAIL


def reset_password(email, code, new_password):
    """Returns (new_recovery_code, "") on success, or (None, error_message)."""
    row = _row_by_email(email.strip().lower())
    bad = "Email or recovery code is incorrect."
    if not row:
        hash_secret(code)
        return None, bad
    locked = _locked_message(row)
    if locked:
        return None, locked
    error = password_error(new_password)
    if error:
        return None, error
    if not verify_secret(code.strip().upper(), row["rc_hash"], row["rc_salt"]):
        _record_failure(row)
        return None, bad
    pw_hash, pw_salt = hash_secret(new_password)
    new_code = new_recovery_code()
    rc_hash, rc_salt = hash_secret(new_code)
    _run("UPDATE users SET pw_hash=?, pw_salt=?, rc_hash=?, rc_salt=? WHERE id=?",
         (pw_hash, pw_salt, rc_hash, rc_salt, row["id"]))
    _clear_failures(row)
    return new_code, ""


def change_password(uid, current, new):
    row = _row_by_id(uid)
    if not verify_secret(current, row["pw_hash"], row["pw_salt"]):
        return False, "Current password is incorrect."
    error = password_error(new)
    if error:
        return False, error
    pw_hash, pw_salt = hash_secret(new)
    _run("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?", (pw_hash, pw_salt, uid))
    return True, "Password updated 🔒"


def update_profile(uid, name, daily_goal, theme):
    _run("UPDATE users SET name=?, daily_goal=?, theme=? WHERE id=?",
         (name[:60], float(daily_goal), theme, uid))


def delete_account(uid, password):
    row = _row_by_id(uid)
    if not verify_secret(password, row["pw_hash"], row["pw_salt"]):
        return False, "Password is incorrect."
    _run("DELETE FROM tasks WHERE user_id=?", (uid,))
    _run("DELETE FROM study_logs WHERE user_id=?", (uid,))
    _run("DELETE FROM users WHERE id=?", (uid,))
    return True, ""


# ---------------- STREAMLIT UI ----------------

AUTH_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap');
.stApp { font-family: 'Poppins', sans-serif;
         background: linear-gradient(135deg, #EEF2FF, #FDF2F8); }
.authhero { background: linear-gradient(135deg, #6366F1, #EC4899); border-radius: 22px;
            padding: 28px; text-align: center; margin-bottom: 18px;
            box-shadow: 0 12px 30px rgba(0,0,0,.18); }
.authhero, .authhero * { color: white !important; }
.authhero h1 { margin: 0; }
p, label, li { color: #334155 !important; }
[data-testid="stFormSubmitButton"] > button, .stButton > button {
    border-radius: 10px; border: none;
    background: linear-gradient(135deg, #6366F1, #EC4899); }
[data-testid="stFormSubmitButton"] > button p, .stButton > button p {
    color: white !important; font-weight: 600; }
</style>
"""


def login_screen():
    st.markdown(AUTH_CSS, unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.markdown('<div class="authhero"><h1>🎓 StudyMate</h1>'
                    '<div>Plan smarter. Study better. Level up.</div></div>',
                    unsafe_allow_html=True)

        done = st.session_state.get("signup_done")
        if done:
            st.success("Account created! 🎉")
            st.warning("Save this recovery code now. It is shown only once and is "
                       "the only way to reset a forgotten password.")
            st.code(done["code"])
            if st.button("I saved it - continue", use_container_width=True):
                st.session_state["user"] = done["user"]
                st.session_state.pop("signup_done")
                st.rerun()
            return

        tab_in, tab_up, tab_reset = st.tabs(["🔑 Log in", "🆕 Sign up", "🛟 Forgot password"])

        with tab_in:
            with st.form("login_form"):
                email = st.text_input("Email")
                password = st.text_input("Password", type="password")
                if st.form_submit_button("Log in", use_container_width=True):
                    user, msg = login(email, password)
                    if user:
                        st.session_state["user"] = user
                        st.rerun()
                    st.error(msg)

        with tab_up:
            with st.form("signup_form"):
                name = st.text_input("Your name")
                email = st.text_input("Email", key="su_email")
                password = st.text_input("Password", type="password", key="su_pw",
                                         help="8+ characters with a letter and a number")
                confirm = st.text_input("Confirm password", type="password", key="su_pw2")
                if st.form_submit_button("Create account", use_container_width=True):
                    if password != confirm:
                        st.error("Passwords do not match.")
                    else:
                        user, result = register(email, name, password)
                        if user:
                            st.session_state["signup_done"] = {"user": user, "code": result}
                            st.rerun()
                        st.error(result)

        with tab_reset:
            st.caption("Use the recovery code you saved when you signed up.")
            with st.form("reset_form"):
                email = st.text_input("Email", key="rs_email")
                code = st.text_input("Recovery code", placeholder="A1B2-C3D4-E5F6")
                new_pw = st.text_input("New password", type="password", key="rs_pw")
                if st.form_submit_button("Reset password", use_container_width=True):
                    new_code, msg = reset_password(email, code, new_pw)
                    if new_code:
                        st.success("Password reset! Your NEW recovery code is below. "
                                   "Save it, then log in.")
                        st.code(new_code)
                    else:
                        st.error(msg)


def require_login():
    """Call at the top of the app. Shows the login screen and stops if logged out."""
    if "user" not in st.session_state:
        login_screen()
        st.stop()
    return st.session_state["user"]


def logout():
    st.session_state.clear()
    st.rerun()


# ---------------- DATABASE ----------------

def run(query, params=(), fetch=False):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            cur = conn.execute(query, params)
            if fetch:
                return [dict(r) for r in cur.fetchall()]
    return None


def initialize_db():
    run("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        subject TEXT NOT NULL, deadline TEXT NOT NULL, priority TEXT NOT NULL,
        completed INTEGER DEFAULT 0, est_hours REAL DEFAULT 1,
        notes TEXT DEFAULT '', completed_on TEXT)""")
    have = {r["name"] for r in run("PRAGMA table_info(tasks)", fetch=True)}
    for col, ddl in [("est_hours", "REAL DEFAULT 1"), ("notes", "TEXT DEFAULT ''"),
                     ("completed_on", "TEXT"), ("user_id", "INTEGER")]:
        if col not in have:
            run(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
    run("""CREATE TABLE IF NOT EXISTS study_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        subject TEXT NOT NULL, minutes INTEGER NOT NULL, day TEXT NOT NULL,
        note TEXT DEFAULT '')""")
    init_auth_tables()


def add_task(title, subject, deadline, priority, hours, notes):
    run("INSERT INTO tasks (title, subject, deadline, priority, est_hours, notes, "
        "user_id) VALUES (?,?,?,?,?,?,?)",
        (title, subject, deadline.isoformat(), priority, hours, notes, UID))


def update_task(tid, title, subject, deadline, priority, hours, notes):
    run("UPDATE tasks SET title=?, subject=?, deadline=?, priority=?, "
        "est_hours=?, notes=? WHERE id=? AND user_id=?",
        (title, subject, deadline.isoformat(), priority, hours, notes, tid, UID))


def update_status(tid, done):
    run("UPDATE tasks SET completed=?, completed_on=? WHERE id=? AND user_id=?",
        (int(done), date.today().isoformat() if done else None, tid, UID))


def delete_task(tid):
    run("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, UID))


def get_tasks():
    rows = run("SELECT id, title, subject, deadline, priority, completed, "
               "COALESCE(est_hours,1) AS est_hours, COALESCE(notes,'') AS notes, "
               "completed_on FROM tasks WHERE user_id=? ORDER BY deadline", (UID,), fetch=True)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["deadline"] = pd.to_datetime(df["deadline"]).dt.date
    df["completed_on"] = pd.to_datetime(df["completed_on"], errors="coerce").dt.date
    df["completed"] = df["completed"].astype(bool)
    df["est_hours"] = df["est_hours"].astype(float)
    return df


def add_log(subject, minutes, day, note):
    run("INSERT INTO study_logs (user_id, subject, minutes, day, note) VALUES (?,?,?,?,?)",
        (UID, subject, minutes, day.isoformat(), note))


def delete_log(lid):
    run("DELETE FROM study_logs WHERE id=? AND user_id=?", (lid, UID))


def get_logs():
    rows = run("SELECT id, subject, minutes, day, COALESCE(note,'') AS note "
               "FROM study_logs WHERE user_id=? ORDER BY day DESC, id DESC",
               (UID,), fetch=True)
    df = pd.DataFrame(rows, columns=["id", "subject", "minutes", "day", "note"])
    df["day"] = pd.to_datetime(df["day"]).dt.date
    df["minutes"] = df["minutes"].astype(int)
    return df


# ---------------- HELPERS ----------------

esc = html.escape


def subject_color(subject):
    return SUBJECT_COLORS[sum(map(ord, subject.lower())) % len(SUBJECT_COLORS)]


def chip(text, color):
    return f'<span class="chip" style="background:{color}">{esc(str(text))}</span>'


def urgency_score(row):
    days_left = (row["deadline"] - today).days
    time_score = 10 if days_left < 0 else 10 / (days_left + 1)
    return round(PRIORITY_WEIGHT[row["priority"]] * 2 + time_score, 2)


def with_urgency(df):
    df = df.copy()
    if df.empty:
        df["urgency"] = []
        return df
    df["urgency"] = df.apply(urgency_score, axis=1)
    return df.sort_values("urgency", ascending=False)


def status_info(t):
    if t["completed"]:
        return "✅ Done", "#10B981"
    if t["deadline"] < today:
        return "🔴 Overdue", "#EF4444"
    if t["deadline"] == today:
        return "🟠 Due today", "#F97316"
    return "🔵 Pending", "#3B82F6"


def task_card(t):
    label, color = status_info(t)
    days = (t["deadline"] - today).days
    when = f"{-days}d overdue" if days < 0 else "today" if days == 0 else f"{days}d left"
    style = "text-decoration:line-through;opacity:.55;" if t["completed"] else ""
    notes = f'<div style="color:#64748B;font-size:13px">📝 {esc(t["notes"])}</div>' if t["notes"] else ""
    hours = f'⏱ {t["est_hours"]:g}h'
    return (
        f'<div class="tcard" style="border-color:{PRIORITY_COLOR[t["priority"]]}">'
        f'<div style="font-size:18px;font-weight:700;color:#1E1B4B;{style}">{esc(t["title"])}</div>'
        f'{chip(t["subject"], subject_color(t["subject"]))}'
        f'{chip(t["priority"] + " priority", PRIORITY_COLOR[t["priority"]])}'
        f'{chip(hours, "#64748B")}{chip(label, color)}'
        f'<div style="color:#64748B;font-size:13px">📅 {t["deadline"]} ({when})</div>{notes}</div>'
    )


def streak_days(df):
    days = {d for d in df["completed_on"] if pd.notna(d)}
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def build_plan(df, hours, start, mode, block=90, brk=10):
    if mode == "⚡ Quick wins first":
        df = df.sort_values("est_hours")
    elif mode == "🧗 Hardest first":
        df = df.sort_values("est_hours", ascending=False)
    remaining = int(hours * 60)
    clock = datetime.combine(today, start)
    plan = []
    for _, t in df.iterrows():
        if remaining <= 0:
            break
        need = int(t["est_hours"] * 60)
        mins = min(need, remaining, block)
        end = clock + timedelta(minutes=mins)
        plan.append({"kind": "study", "time": f"{clock:%H:%M} - {end:%H:%M}",
                     "task": t["title"], "subject": t["subject"], "minutes": mins,
                     "note": "Complete" if mins >= need else "Finish later"})
        plan.append({"kind": "break", "minutes": brk})
        remaining -= mins
        clock = end + timedelta(minutes=brk)
    if plan and plan[-1]["kind"] == "break":
        plan.pop()
    return plan


def flash(msg, celebrate=False):
    st.session_state["flash"] = msg
    st.session_state["celebrate"] = celebrate


def show_flash():
    msg = st.session_state.pop("flash", None)
    if msg:
        st.success(msg)
    if st.session_state.pop("celebrate", False):
        st.balloons()


def metric_card(icon, label, value, c1, c2):
    return (f'<div class="mcard" style="background:linear-gradient(135deg,{c1},{c2})">'
            f'<div class="l">{icon} {label}</div><div class="v">{value}</div></div>')


def style_chart(chart):
    return chart.configure_view(strokeWidth=0).configure_axis(grid=False, domain=False)


# ---------------- LOAD DATA ----------------

initialize_db()
user = require_login()          # shows the login screen and stops if logged out
UID = user["id"]
tasks = get_tasks()
logs = get_logs()
goal_min = max(int(user["daily_goal"] * 60), 1)
today_min = int(logs.loc[logs["day"] == today, "minutes"].sum())
total = len(tasks)
completed = int(tasks["completed"].sum())
pending_count = total - completed
percent = int(completed / total * 100) if total else 0
pending_df = with_urgency(tasks[~tasks["completed"]])
xp = int(sum(PRIORITY_XP[p] for p in tasks.loc[tasks["completed"], "priority"]))
level, xp_in_level = xp // 100 + 1, xp % 100
streak = streak_days(tasks)
overdue_count = int((pending_df["deadline"] < today).sum()) if total else 0

# ---------------- SIDEBAR ----------------

st.sidebar.title("🎓 StudyMate")
name = user["name"]
st.sidebar.caption(f"👤 {name}  ·  {user['email']}")
C1, C2, BG = THEMES.get(user["theme"], THEMES["🌊 Ocean"])
page = st.sidebar.radio("Navigation", [
    "🏠 Dashboard", "📝 My Tasks", "📅 Calendar", "🧠 Study Planner",
    "⏱️ Focus Timer", "🕒 Study Log", "📊 Analytics", "👤 Profile"])
st.sidebar.divider()
st.sidebar.write(f"🏅 **Level {level}**  ·  {xp} XP")
st.sidebar.progress(xp_in_level / 100)
st.sidebar.caption(f"🔥 {streak}-day streak")
st.sidebar.write(f"🎯 Today: {today_min}/{goal_min} min")
st.sidebar.progress(min(today_min / goal_min, 1.0))
if st.sidebar.button("🚪 Log out", use_container_width=True):
    logout()

# ---------------- CUSTOM DESIGN ----------------

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap');
.stApp { font-family: 'Poppins', sans-serif; background: __BG__; }
h1, h2, h3, h4 { color: #1E1B4B !important; font-weight: 700 !important; }
p, label, li { color: #334155 !important; }
[data-testid="stSidebar"] { background: white; border-right: 3px solid __C1__; }
.hero { background: linear-gradient(135deg, __C1__, __C2__); border-radius: 22px;
        padding: 26px 32px; margin-bottom: 18px; box-shadow: 0 12px 30px rgba(0,0,0,.18); }
.hero, .hero * { color: white !important; }
.hero h1 { margin: 0; font-size: 32px; }
.xpbar { background: rgba(255,255,255,.3); border-radius: 99px; height: 12px; margin-top: 12px; }
.xpfill { background: #FDE047; height: 100%; border-radius: 99px; }
.mcard { border-radius: 18px; padding: 16px 20px; box-shadow: 0 8px 20px rgba(0,0,0,.15);
         transition: transform .2s; }
.mcard:hover { transform: translateY(-5px); }
.mcard, .mcard * { color: white !important; }
.mcard .l { font-size: 14px; opacity: .95; }
.mcard .v { font-size: 34px; font-weight: 700; line-height: 1.2; }
.tcard { background: white; border-radius: 14px; padding: 12px 16px; margin-bottom: 8px;
         border-left: 7px solid; box-shadow: 0 4px 14px rgba(0,0,0,.07); }
.chip { display: inline-block; padding: 2px 11px; border-radius: 99px; font-size: 12px;
        margin: 4px 5px 4px 0; font-weight: 600; }
.chip, .chip * { color: white !important; }
.quote { background: white; border-radius: 16px; padding: 16px 20px; margin: 10px 0;
         border: 2px dashed __C1__; font-style: italic; }
.cal { width: 100%; border-collapse: separate; border-spacing: 6px; table-layout: fixed; }
.cal th { background: linear-gradient(135deg, __C1__, __C2__); color: white; border-radius: 10px;
          padding: 6px; font-size: 13px; }
.cal td { background: white; border-radius: 10px; height: 96px; vertical-align: top;
          padding: 5px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.cal td.today { outline: 3px solid __C2__; }
.cal td.empty { background: transparent; box-shadow: none; }
.ev { color: white; border-radius: 6px; padding: 1px 6px; margin-top: 3px; font-size: 11px;
      overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.brk { text-align: center; color: #64748B !important; padding: 4px; font-size: 14px; }
.heat { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; max-width: 420px; }
.heat div { border-radius: 6px; height: 34px; font-size: 11px; text-align: center; line-height: 34px; }
.stButton > button, [data-testid="stFormSubmitButton"] > button,
.stDownloadButton > button { border-radius: 10px; border: none;
        background: linear-gradient(135deg, __C1__, __C2__); transition: transform .15s; }
.stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover { transform: scale(1.04); }
.stButton > button p, [data-testid="stFormSubmitButton"] > button p,
.stDownloadButton > button p { color: white !important; font-weight: 600; }
.stProgress > div > div > div { background: linear-gradient(90deg, __C1__, __C2__); }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 16px; }
</style>
"""
st.markdown(CSS.replace("__C1__", C1).replace("__C2__", C2).replace("__BG__", BG),
            unsafe_allow_html=True)


def hero(title, subtitle):
    return (f'<div class="hero"><h1>{title}</h1><div>{subtitle}</div>'
            f'<div class="xpbar"><div class="xpfill" style="width:{xp_in_level}%"></div></div>'
            f'<div style="font-size:13px;margin-top:4px">🏅 Level {level} · {xp_in_level}/100 XP '
            f'to next level · 🔥 {streak}-day streak</div></div>')


status_scale = alt.Scale(domain=["Completed", "Pending"], range=["#10B981", "#F59E0B"])

# ---------------- DASHBOARD ----------------

if page == "🏠 Dashboard":
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    st.markdown(hero(f"{greet}, {esc(name)}! 👋",
                     "Let's make today count. Here's your study snapshot."),
                unsafe_allow_html=True)
    show_flash()

    cols = st.columns(4)
    cards = [("📚", "Total Tasks", total, "#6366F1", "#8B5CF6"),
             ("⏳", "Pending", pending_count, "#F59E0B", "#F97316"),
             ("✅", "Completed", completed, "#10B981", "#14B8A6"),
             ("⚠️", "Overdue", overdue_count, "#EF4444", "#EC4899")]
    for col, c in zip(cols, cards):
        col.markdown(metric_card(*c), unsafe_allow_html=True)

    goal_pct = min(today_min / goal_min, 1.0)
    st.markdown(f'<div class="quote">🎯 Daily goal: <b>{today_min}</b> of {goal_min} minutes '
                f'studied today ({int(goal_pct * 100)}%)</div>', unsafe_allow_html=True)
    st.progress(goal_pct)

    st.markdown(f'<div class="quote">💡 {QUOTES[today.toordinal() % len(QUOTES)]}</div>',
                unsafe_allow_html=True)
    if overdue_count:
        st.error(f"⚠️ {overdue_count} overdue task(s) - tackle those first!")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("🎯 Do This Next")
        if pending_df.empty:
            st.success("Amazing! Nothing pending. 🎉")
        for _, t in pending_df.head(3).iterrows():
            st.markdown(task_card(t), unsafe_allow_html=True)

        st.subheader("📅 Workload - Next 7 Days")
        days = [today + timedelta(days=i) for i in range(7)]
        wl = pd.DataFrame({
            "Day": [d.strftime("%a %d") for d in days],
            "Hours": [float(pending_df.loc[pending_df["deadline"] == d, "est_hours"].sum())
                      for d in days]})
        st.altair_chart(style_chart(alt.Chart(wl).mark_bar(
            cornerRadiusTopLeft=8, cornerRadiusTopRight=8, color=C1).encode(
            x=alt.X("Day:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Hours:Q", axis=alt.Axis(tickMinStep=1)),
            tooltip=["Day", "Hours"])), use_container_width=True)

    with right:
        st.subheader("🍩 Progress")
        if total:
            dn = pd.DataFrame({"Status": ["Completed", "Pending"],
                               "Tasks": [completed, pending_count]})
            st.altair_chart(style_chart(alt.Chart(dn).mark_arc(innerRadius=70).encode(
                theta="Tasks:Q",
                color=alt.Color("Status:N", scale=status_scale,
                                legend=alt.Legend(orient="bottom")),
                tooltip=["Status", "Tasks"])), use_container_width=True)
            st.markdown(f"<h2 style='text-align:center'>{percent}% done</h2>",
                        unsafe_allow_html=True)
        else:
            st.info("Add tasks to see your progress.")

    st.subheader("🏆 Achievements")
    pend_only = tasks[~tasks["completed"]]
    badges = [("🌱 First Step", "Complete 1 task", completed >= 1),
              ("⭐ High Five", "Complete 5 tasks", completed >= 5),
              ("🏆 Task Master", "Complete 10 tasks", completed >= 10),
              ("🔥 On Fire", "3-day streak", streak >= 3),
              ("⏰ On Time", "Pending tasks, none overdue",
               len(pend_only) > 0 and overdue_count == 0)]
    for col, (b, hint, ok) in zip(st.columns(5), badges):
        with col.container(border=True):
            st.markdown(f"**{b}**" if ok else f"🔒 {b}")
            st.caption("Unlocked!" if ok else hint)

# ---------------- MY TASKS ----------------

elif page == "📝 My Tasks":
    st.markdown(hero("📝 My Tasks", "Complete tasks to earn XP: High +30, Medium +20, Low +10."),
                unsafe_allow_html=True)
    show_flash()

    with st.expander("➕ Add New Task", expanded=total == 0):
        with st.form("new_task", clear_on_submit=True):
            title = st.text_input("Task Name")
            subject = st.text_input("Subject")
            a, b, c = st.columns(3)
            deadline = a.date_input("Deadline", min_value=today)
            priority = b.selectbox("Priority", PRIORITIES)
            hours = c.number_input("Estimated hours", 0.5, 20.0, 1.0, 0.5)
            notes = st.text_area("Notes (optional)")
            if st.form_submit_button("Add Task", use_container_width=True):
                if title.strip() and subject.strip():
                    add_task(title.strip(), subject.strip(), deadline, priority, hours, notes.strip())
                    flash("Task added successfully! ✨")
                    st.rerun()
                else:
                    st.error("Please enter a task name and subject.")

    if tasks.empty:
        st.info("No tasks yet. Add your first one above.")
    else:
        f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
        search = f1.text_input("🔍 Search", placeholder="title or subject")
        status_f = f2.selectbox("Status", ["All", "Pending", "Completed"])
        prio_f = f3.selectbox("Priority", ["All"] + PRIORITIES)
        sort_f = f4.selectbox("Sort by", ["Deadline", "Urgency", "Priority"])

        view = tasks
        if search.strip():
            q = search.strip().lower()
            view = view[view["title"].str.lower().str.contains(q, regex=False)
                        | view["subject"].str.lower().str.contains(q, regex=False)]
        if status_f != "All":
            view = view[view["completed"] == (status_f == "Completed")]
        if prio_f != "All":
            view = view[view["priority"] == prio_f]
        if sort_f == "Urgency":
            view = with_urgency(view)
        elif sort_f == "Priority":
            view = view.assign(_p=view["priority"].map(PRIORITY_WEIGHT)).sort_values(
                "_p", ascending=False)

        export = tasks.drop(columns=["notes"]).copy()
        export["completed"] = export["completed"].map({True: "Completed", False: "Pending"})
        st.download_button("📥 Download CSV", export.to_csv(index=False),
                           file_name="StudyMate_Tasks.csv", mime="text/csv")
        if view.empty:
            st.info("No tasks match your filters.")

        for _, t in view.iterrows():
            tid = int(t["id"])
            with st.container(border=True):
                left, right = st.columns([5, 1.4])
                left.markdown(task_card(t), unsafe_allow_html=True)
                with right:
                    done = st.checkbox("Done", value=bool(t["completed"]), key=f"done_{tid}")
                    if done != bool(t["completed"]):
                        update_status(tid, done)
                        if done:
                            flash(f"🎉 +{PRIORITY_XP[t['priority']]} XP! Great job!", True)
                        st.rerun()
                    if st.session_state.get("confirm_delete") == tid:
                        st.warning("Delete?")
                        y, n = st.columns(2)
                        if y.button("Yes", key=f"yes_{tid}"):
                            delete_task(tid)
                            st.session_state.pop("confirm_delete", None)
                            flash("Task deleted.")
                            st.rerun()
                        if n.button("No", key=f"no_{tid}"):
                            st.session_state.pop("confirm_delete", None)
                            st.rerun()
                    elif st.button("🗑️ Delete", key=f"del_{tid}"):
                        st.session_state["confirm_delete"] = tid
                        st.rerun()
                with st.expander("✏️ Edit"):
                    with st.form(f"edit_{tid}"):
                        e_title = st.text_input("Task Name", t["title"])
                        e_subject = st.text_input("Subject", t["subject"])
                        g1, g2, g3 = st.columns(3)
                        e_deadline = g1.date_input("Deadline", t["deadline"])
                        e_prio = g2.selectbox("Priority", PRIORITIES,
                                              index=PRIORITIES.index(t["priority"]))
                        e_hours = g3.number_input("Estimated hours", 0.5, 20.0,
                                                  float(t["est_hours"]), 0.5)
                        e_notes = st.text_area("Notes", t["notes"])
                        if st.form_submit_button("Save changes"):
                            if e_title.strip() and e_subject.strip():
                                update_task(tid, e_title.strip(), e_subject.strip(),
                                            e_deadline, e_prio, e_hours, e_notes.strip())
                                flash("Task updated.")
                                st.rerun()
                            else:
                                st.error("Task name and subject are required.")

# ---------------- STUDY PLANNER ----------------

elif page == "🧠 Study Planner":
    st.markdown(hero("🧠 Smart Study Planner",
                     "A timed schedule built from your pending tasks."),
                unsafe_allow_html=True)
    a, b, c = st.columns(3)
    hours_avail = a.slider("Hours available today", 1, 10, 3)
    start_time = b.time_input("Start time", datetime.strptime("16:00", "%H:%M").time())
    mode = c.selectbox("Strategy", ["🎯 Smart urgency", "⚡ Quick wins first", "🧗 Hardest first"])

    if st.button("✨ Generate Study Plan", use_container_width=True):
        if pending_df.empty:
            st.success("All assignments are completed! 🎉")
        else:
            plan = build_plan(pending_df, hours_avail, start_time, mode)
            lines = []
            for p in plan:
                if p["kind"] == "break":
                    st.markdown(f'<div class="brk">☕ Break · {p["minutes"]} min</div>',
                                unsafe_allow_html=True)
                    continue
                col = subject_color(p["subject"])
                mins = f'{p["minutes"]} min'
                st.markdown(
                    f'<div class="tcard" style="border-color:{col}">'
                    f'<b>🕒 {p["time"]}</b> &nbsp; {esc(p["task"])}<br>'
                    f'{chip(p["subject"], col)}{chip(mins, "#64748B")}'
                    f'{chip(p["note"], "#10B981" if p["note"] == "Complete" else "#F59E0B")}</div>',
                    unsafe_allow_html=True)
                lines.append(f'{p["time"]}  {p["task"]} ({p["subject"]}) - {mins}')
            left_over = len(pending_df) - sum(1 for p in plan if p["kind"] == "study")
            if left_over:
                st.warning(f"{left_over} task(s) didn't fit today. Continue tomorrow.")
            st.download_button("📥 Download plan", "\n".join(lines), file_name="study_plan.txt")

# ---------------- FOCUS TIMER ----------------

elif page == "⏱️ Focus Timer":
    st.markdown(hero("⏱️ Focus Timer", "Work in focused blocks, then recharge."),
                unsafe_allow_html=True)
    options = ["Free study"] + [f"{r['title']} ({r['subject']})" for _, r in pending_df.iterrows()]
    chosen = st.selectbox("What are you working on?", options)
    a, b = st.columns(2)
    focus_min = a.number_input("Focus minutes", 5, 90, 25)
    break_min = b.number_input("Break minutes", 1, 30, 5)

    timer = """
    <div style="font-family:Poppins,sans-serif;text-align:center;padding:16px;background:white;
                border-radius:22px;box-shadow:0 8px 24px rgba(0,0,0,.1)">
      <svg width="260" height="260" viewBox="0 0 260 260">
        <circle cx="130" cy="130" r="110" stroke="#E5E7EB" stroke-width="14" fill="none"/>
        <circle id="ring" cx="130" cy="130" r="110" stroke="__C1__" stroke-width="14" fill="none"
                stroke-linecap="round" transform="rotate(-90 130 130)"
                stroke-dasharray="691.15" stroke-dashoffset="0"/>
        <text id="clock" x="130" y="140" text-anchor="middle" font-size="52" font-weight="700" fill="#1E1B4B">25:00</text>
        <text id="mode" x="130" y="172" text-anchor="middle" font-size="14" fill="#64748B"></text>
      </svg><br>
      <button onclick="start()" style="padding:9px 20px;margin:4px;border:0;border-radius:10px;background:__C1__;color:#fff;font-weight:600;cursor:pointer">▶ Start</button>
      <button onclick="pause()" style="padding:9px 20px;margin:4px;border:0;border-radius:10px;background:#64748B;color:#fff;font-weight:600;cursor:pointer">⏸ Pause</button>
      <button onclick="reset()" style="padding:9px 20px;margin:4px;border:0;border-radius:10px;background:#64748B;color:#fff;font-weight:600;cursor:pointer">↺ Reset</button>
    </div>
    <script>
      const focus = __F__ * 60, rest = __B__ * 60, C = 691.15;
      let left = focus, total = focus, mode = "Focus", sessions = 0, timer = null;
      function beep() {
        try { const a = new AudioContext(), o = a.createOscillator();
              o.connect(a.destination); o.frequency.value = 660; o.start();
              setTimeout(() => o.stop(), 400); } catch (e) {}
      }
      function render() {
        const m = String(Math.floor(left / 60)).padStart(2, "0");
        const s = String(left % 60).padStart(2, "0");
        document.getElementById("clock").textContent = m + ":" + s;
        document.getElementById("mode").textContent = mode + " · sessions: " + sessions;
        document.getElementById("ring").setAttribute("stroke-dashoffset", C * (1 - left / total));
        document.getElementById("ring").setAttribute("stroke", mode === "Focus" ? "__C1__" : "#10B981");
      }
      function tick() {
        left--;
        if (left <= 0) {
          beep();
          if (mode === "Focus") { sessions++; mode = "Break"; left = total = rest; }
          else { mode = "Focus"; left = total = focus; }
        }
        render();
      }
      function start() { if (!timer) timer = setInterval(tick, 1000); }
      function pause() { clearInterval(timer); timer = null; }
      function reset() { pause(); mode = "Focus"; left = total = focus; render(); }
      render();
    </script>
    """
    timer = (timer.replace("__C1__", C1).replace("__F__", str(int(focus_min)))
             .replace("__B__", str(int(break_min))))
    st.subheader(f"Now: {chosen}")
    components.html(timer, height=360)

# ---------------- CALENDAR ----------------

elif page == "📅 Calendar":
    st.markdown(hero("📅 Deadline Calendar",
                     "🔴 High · 🟠 Medium · 🟢 Low · ⚪ Done"), unsafe_allow_html=True)
    st.session_state.setdefault("cal_offset", 0)
    b1, b2, b3 = st.columns([1, 3, 1])
    if b1.button("◀ Prev", use_container_width=True):
        st.session_state["cal_offset"] -= 1
        st.rerun()
    if b3.button("Next ▶", use_container_width=True):
        st.session_state["cal_offset"] += 1
        st.rerun()
    year, month0 = divmod(today.year * 12 + today.month - 1 + st.session_state["cal_offset"], 12)
    month = month0 + 1
    b2.markdown(f"<h3 style='text-align:center'>{calendar.month_name[month]} {year}</h3>",
                unsafe_allow_html=True)

    by_day = {}
    for _, t in tasks.iterrows():
        by_day.setdefault(t["deadline"], []).append(t)

    rows = "<tr>" + "".join(f"<th>{d}</th>" for d in
                            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) + "</tr>"
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        rows += "<tr>"
        for d in week:
            if d == 0:
                rows += '<td class="empty"></td>'
                continue
            day = date(year, month, d)
            items = ""
            todays = by_day.get(day, [])
            for t in todays[:3]:
                col = "#94A3B8" if t["completed"] else PRIORITY_COLOR[t["priority"]]
                items += f'<div class="ev" style="background:{col}">{esc(t["title"])}</div>'
            if len(todays) > 3:
                items += f'<div style="font-size:11px;color:#64748B">+{len(todays) - 3} more</div>'
            cls = "today" if day == today else ""
            rows += f'<td class="{cls}"><b>{d}</b>{items}</td>'
        rows += "</tr>"
    st.markdown(f'<table class="cal">{rows}</table>', unsafe_allow_html=True)

# ---------------- STUDY LOG ----------------

elif page == "🕒 Study Log":
    st.markdown(hero("🕒 Study Log", "Log your study time and hit your daily goal."),
                unsafe_allow_html=True)
    show_flash()
    st.progress(min(today_min / goal_min, 1.0))
    st.caption(f"Today: {today_min} / {goal_min} min")

    known = sorted(tasks["subject"].unique())
    with st.form("log_form", clear_on_submit=True):
        a, b, c = st.columns(3)
        subj = a.text_input("Subject", placeholder=", ".join(known[:3]) or "e.g. Maths")
        mins = b.number_input("Minutes", 5, 480, 30, 5)
        when = c.date_input("Date", today, max_value=today)
        note = st.text_input("What did you study? (optional)")
        if st.form_submit_button("➕ Log session", use_container_width=True):
            if subj.strip():
                add_log(subj.strip(), int(mins), when, note.strip())
                reached = when == today and today_min < goal_min <= today_min + mins
                flash("🎯 Daily goal reached! Amazing!" if reached
                      else f"Logged {int(mins)} min of {subj.strip()} 📚", reached)
                st.rerun()
            else:
                st.error("Please enter a subject.")

    days7 = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = logs.groupby("day")["minutes"].sum().to_dict()
    wk = pd.DataFrame({"Day": [d.strftime("%a %d") for d in days7],
                       "Minutes": [int(per_day.get(d, 0)) for d in days7]})
    met = sum(1 for d in days7 if per_day.get(d, 0) >= goal_min)
    cols = st.columns(3)
    for col, c in zip(cols, [("📚", "This week", f"{wk['Minutes'].sum()} min", "#6366F1", "#8B5CF6"),
                             ("📈", "Daily average", f"{int(wk['Minutes'].mean())} min", "#0EA5E9", "#14B8A6"),
                             ("🎯", "Goal days", f"{met}/7", "#F59E0B", "#F97316")]):
        col.markdown(metric_card(*c), unsafe_allow_html=True)

    st.subheader("📊 Last 7 Days vs Goal")
    bars = alt.Chart(wk).mark_bar(cornerRadiusTopLeft=8, cornerRadiusTopRight=8, color=C1).encode(
        x=alt.X("Day:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("Minutes:Q"), tooltip=["Day", "Minutes"])
    goal_line = alt.Chart(pd.DataFrame({"goal": [goal_min]})).mark_rule(
        color="#EF4444", strokeDash=[6, 4], size=2).encode(y="goal:Q")
    st.altair_chart(style_chart(bars + goal_line), use_container_width=True)

    if not logs.empty:
        st.subheader("📚 Time by Subject")
        bs = logs.groupby("subject")["minutes"].sum().reset_index()
        st.altair_chart(style_chart(alt.Chart(bs).mark_bar(
            cornerRadiusTopRight=8, cornerRadiusBottomRight=8).encode(
            y=alt.Y("subject:N", title=None, sort="-x"), x=alt.X("minutes:Q", title="Minutes"),
            color=alt.Color("subject:N", legend=None, scale=alt.Scale(
                domain=list(bs["subject"]),
                range=[subject_color(s) for s in bs["subject"]])))),
            use_container_width=True)

        st.subheader("🗂️ Recent Sessions")
        for _, r in logs.head(15).iterrows():
            lid = int(r["id"])
            l, rr = st.columns([6, 1])
            extra = f" - {esc(r['note'])}" if r["note"] else ""
            l.markdown(f"**{r['day']}** · {chip(r['subject'], subject_color(r['subject']))} "
                       f"{int(r['minutes'])} min{extra}", unsafe_allow_html=True)
            if rr.button("🗑️", key=f"dlog_{lid}"):
                delete_log(lid)
                st.rerun()

# ---------------- PROFILE ----------------

elif page == "👤 Profile":
    st.markdown(hero("👤 My Profile", esc(user["email"])), unsafe_allow_html=True)
    show_flash()
    left, right = st.columns(2)

    with left:
        st.subheader("🎨 Preferences")
        with st.form("profile_form"):
            p_name = st.text_input("Display name", user["name"])
            p_goal = st.number_input("Daily study goal (hours)", 0.5, 12.0,
                                     float(user["daily_goal"]), 0.5)
            themes = list(THEMES)
            p_theme = st.selectbox("Theme", themes, index=themes.index(user["theme"])
                                   if user["theme"] in themes else 0)
            if st.form_submit_button("Save profile", use_container_width=True):
                if p_name.strip():
                    update_profile(UID, p_name.strip(), p_goal, p_theme)
                    st.session_state["user"] = get_user(UID)
                    flash("Profile saved ✨")
                    st.rerun()
                else:
                    st.error("Name can't be empty.")

    with right:
        st.subheader("🔒 Change Password")
        with st.form("pw_form", clear_on_submit=True):
            cur = st.text_input("Current password", type="password")
            new1 = st.text_input("New password", type="password")
            new2 = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Update password", use_container_width=True):
                if new1 != new2:
                    st.error("New passwords do not match.")
                else:
                    ok, msg = change_password(UID, cur, new1)
                    if ok:
                        flash(msg)
                        st.rerun()
                    st.error(msg)

    with st.expander("⚠️ Danger zone"):
        with st.form("delete_form"):
            pw = st.text_input("Confirm your password", type="password")
            sure = st.checkbox("I understand this permanently deletes all my data")
            if st.form_submit_button("Delete my account"):
                if not sure:
                    st.error("Please tick the confirmation box.")
                else:
                    ok, msg = delete_account(UID, pw)
                    if ok:
                        logout()
                    st.error(msg)

# ---------------- ANALYTICS ----------------

elif page == "📊 Analytics":
    st.markdown(hero("📊 Study Analytics", "See how your effort adds up."),
                unsafe_allow_html=True)
    if tasks.empty:
        st.info("Add some tasks to view analytics.")
    else:
        hrs_left = float(pending_df["est_hours"].sum()) if not pending_df.empty else 0.0
        cols = st.columns(4)
        for col, c in zip(cols, [("🎯", "Completion", f"{percent}%", "#6366F1", "#8B5CF6"),
                                 ("🏅", "Total XP", xp, "#F59E0B", "#F97316"),
                                 ("🔥", "Streak", f"{streak}d", "#EF4444", "#EC4899"),
                                 ("⏳", "Hours left", f"{hrs_left:g}", "#0EA5E9", "#14B8A6")]):
            col.markdown(metric_card(*c), unsafe_allow_html=True)

        st.write("")
        l, r = st.columns(2)
        with l:
            st.subheader("⚡ Priority Mix")
            pc = tasks["priority"].value_counts().reindex(PRIORITIES, fill_value=0)
            pdf = pd.DataFrame({"Priority": PRIORITIES, "Tasks": pc.values})
            st.altair_chart(style_chart(alt.Chart(pdf).mark_bar(
                cornerRadiusTopLeft=8, cornerRadiusTopRight=8).encode(
                x=alt.X("Priority:N", sort=PRIORITIES, title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Tasks:Q", axis=alt.Axis(tickMinStep=1)),
                color=alt.Color("Priority:N", legend=None, scale=alt.Scale(
                    domain=PRIORITIES, range=[PRIORITY_COLOR[p] for p in PRIORITIES])))),
                use_container_width=True)
        with r:
            st.subheader("📚 Subjects")
            by = (tasks.assign(Status=tasks["completed"].map({True: "Completed", False: "Pending"}))
                  .groupby(["subject", "Status"]).size().reset_index(name="Tasks"))
            st.altair_chart(style_chart(alt.Chart(by).mark_bar(
                cornerRadiusTopLeft=8, cornerRadiusTopRight=8).encode(
                x=alt.X("subject:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("Tasks:Q", axis=alt.Axis(tickMinStep=1)),
                color=alt.Color("Status:N", scale=status_scale,
                                legend=alt.Legend(orient="bottom")))),
                use_container_width=True)

        st.subheader("🟩 Activity - Last 28 Days")
        counts = tasks["completed_on"].dropna().value_counts().to_dict()
        shades = ["#E5E7EB", "#BBF7D0", "#4ADE80", "#16A34A"]
        cells = ""
        for i in range(27, -1, -1):
            d = today - timedelta(days=i)
            n = counts.get(d, 0)
            shade = shades[min(n, 3)]
            txt = "#fff" if n >= 2 else "#475569"
            cells += f'<div style="background:{shade};color:{txt}" title="{d}: {n}">{d.day}</div>'
        st.markdown(f'<div class="heat">{cells}</div>', unsafe_allow_html=True)
        st.caption("Darker green = more tasks completed that day.")

        st.subheader("💡 Insights")
        rate = tasks.groupby("subject")["completed"].mean().sort_values()
        weak = rate.index[0]
        if rate.iloc[0] < 1:
            st.info(f"📌 **{weak}** has the lowest completion rate "
                    f"({int(rate.iloc[0] * 100)}%). Give it extra time this week.")
        if overdue_count:
            st.warning(f"⚠️ {overdue_count} overdue task(s). Clear them to protect your streak.")
        else:
            st.success("✅ No overdue tasks. Keep it up!")
        if hrs_left:
            st.info(f"⏳ About {hrs_left:g} study hours remain across {pending_count} task(s).")
