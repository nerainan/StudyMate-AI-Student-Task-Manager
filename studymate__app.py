"""
StudyMate v2 - colorful, gamified student planner

Run with:
    py -m pip install streamlit pandas
    py -m streamlit run studymate_v2.py
(uses the same tasks.db as v1; old data keeps working)
"""

import html
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
                     ("completed_on", "TEXT")]:
        if col not in have:
            run(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")


def add_task(title, subject, deadline, priority, hours, notes):
    run("INSERT INTO tasks (title, subject, deadline, priority, est_hours, notes) "
        "VALUES (?,?,?,?,?,?)",
        (title, subject, deadline.isoformat(), priority, hours, notes))


def update_task(tid, title, subject, deadline, priority, hours, notes):
    run("UPDATE tasks SET title=?, subject=?, deadline=?, priority=?, "
        "est_hours=?, notes=? WHERE id=?",
        (title, subject, deadline.isoformat(), priority, hours, notes, tid))


def update_status(tid, done):
    run("UPDATE tasks SET completed=?, completed_on=? WHERE id=?",
        (int(done), date.today().isoformat() if done else None, tid))


def delete_task(tid):
    run("DELETE FROM tasks WHERE id=?", (tid,))


def get_tasks():
    rows = run("SELECT id, title, subject, deadline, priority, completed, "
               "COALESCE(est_hours,1) AS est_hours, COALESCE(notes,'') AS notes, "
               "completed_on FROM tasks ORDER BY deadline", fetch=True)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["deadline"] = pd.to_datetime(df["deadline"]).dt.date
    df["completed_on"] = pd.to_datetime(df["completed_on"], errors="coerce").dt.date
    df["completed"] = df["completed"].astype(bool)
    df["est_hours"] = df["est_hours"].astype(float)
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
tasks = get_tasks()
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
name = st.sidebar.text_input("Your name", value="Student", key="name")
theme = st.sidebar.selectbox("🎨 Theme", list(THEMES))
C1, C2, BG = THEMES[theme]
page = st.sidebar.radio("Navigation", [
    "🏠 Dashboard", "📝 My Tasks", "🧠 Study Planner", "⏱️ Focus Timer", "📊 Analytics"])
st.sidebar.divider()
st.sidebar.write(f"🏅 **Level {level}**  ·  {xp} XP")
st.sidebar.progress(xp_in_level / 100)
st.sidebar.caption(f"🔥 {streak}-day streak")

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
