import os
import sqlite3
import time
from typing import Optional, List, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
)

DB_PATH = "bot.db"

# Registration conversation states
REG_ROLE, REG_NAME, REG_PHONE = range(3)

def now_ts() -> int:
    return int(time.time())

def get_handle(update: Update) -> Optional[str]:
    """Telegram username (without @). Required for your PK requirement."""
    u = update.effective_user
    if not u or not u.username:
        return None
    return u.username.lower()

def is_organizer(handle: str) -> bool:
    raw = os.environ.get("ORGANIZER_HANDLES", "")
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return handle.lower() in allowed

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            handle TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('individual','caregiver')),
            full_name TEXT,
            phone TEXT
        );

        CREATE TABLE IF NOT EXISTS individuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caregiver_handle TEXT NOT NULL,
            name TEXT NOT NULL,
            nric_last4 TEXT,
            FOREIGN KEY (caregiver_handle) REFERENCES users(handle) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            capacity INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL,
            individual_id INTEGER NOT NULL,
            booked_by_handle TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            UNIQUE(activity_id, individual_id),
            FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE,
            FOREIGN KEY(individual_id) REFERENCES individuals(id) ON DELETE CASCADE,
            FOREIGN KEY(booked_by_handle) REFERENCES users(handle) ON DELETE CASCADE
        );
        """)

def seed_demo_activities_if_empty() -> None:
    """Hackathon helper: creates some activities if none exist."""
    with db() as conn:
        (cnt,) = conn.execute("SELECT COUNT(*) FROM activities;").fetchone()
        if cnt > 0:
            return
        base = now_ts() + 3600  # 1 hour from now
        demo = [
            ("Music Therapy", base, base + 3600, 10),
            ("Art Jam", base + 5400, base + 7200, 8),          # no overlap w/ first
            ("Physio Session", base + 1800, base + 5400, 5),   # overlaps with first
        ]
        conn.executemany(
            "INSERT INTO activities(title,start_ts,end_ts,capacity) VALUES (?,?,?,?);",
            demo
        )

def user_get(handle: str) -> Optional[Tuple]:
    with db() as conn:
        return conn.execute(
            "SELECT handle, role, full_name, phone FROM users WHERE handle=?;",
            (handle,)
        ).fetchone()

def user_upsert(handle: str, role: str, full_name: str, phone: str) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO users(handle, role, full_name, phone)
            VALUES (?,?,?,?)
            ON CONFLICT(handle) DO UPDATE SET
                role=excluded.role,
                full_name=excluded.full_name,
                phone=excluded.phone;
        """, (handle, role, full_name, phone))

def caregiver_add_individual(caregiver_handle: str, name: str, nric_last4: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,?);",
            (caregiver_handle, name, nric_last4 or None)
        )
        return int(cur.lastrowid)

def individual_get_for_handle(handle: str) -> Optional[int]:
    """If role=individual, ensure a corresponding 'individuals' record exists."""
    u = user_get(handle)
    if not u:
        return None
    _, role, full_name, _ = u
    if role != "individual":
        return None

    with db() as conn:
        row = conn.execute(
            "SELECT id FROM individuals WHERE caregiver_handle=? LIMIT 1;",
            (handle,)
        ).fetchone()
        if row:
            return int(row[0])

        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,NULL);",
            (handle, full_name or handle)
        )
        return int(cur.lastrowid)

def caregiver_list_individuals(caregiver_handle: str) -> List[Tuple[int, str]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT id, name FROM individuals WHERE caregiver_handle=? ORDER BY id;",
            (caregiver_handle,)
        )
        return [(int(r[0]), r[1]) for r in cur.fetchall()]

def list_activities() -> List[Tuple]:
    with db() as conn:
        cur = conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts, a.capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=a.id) AS booked_count
            FROM activities a
            ORDER BY a.start_ts ASC;
        """)
        return cur.fetchall()

def activity_exists(activity_id: int) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM activities WHERE id=?;", (activity_id,)).fetchone() is not None

def capacity_available(activity_id: int) -> bool:
    with db() as conn:
        row = conn.execute("""
            SELECT a.capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=a.id) AS booked
            FROM activities a WHERE a.id=?;
        """, (activity_id,)).fetchone()
        if not row:
            return False
        cap, booked = int(row[0]), int(row[1])
        return booked < cap

def booking_conflicts(individual_id: int, new_activity_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts, title FROM activities WHERE id=?;",
            (new_activity_id,)
        ).fetchone()
        if not row:
            return "Activity not found."
        new_start, new_end, _ = int(row[0]), int(row[1]), row[2]

        hit = conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_id=?
              AND a.start_ts < ?
              AND ? < a.end_ts;
        """, (individual_id, new_end, new_start)).fetchone()

        if not hit:
            return None

        _, title, s, e = hit
        s_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(s)))
        e_str = time.strftime("%H:%M", time.localtime(int(e)))
        return f"Conflicts with: {title} ({s_str}-{e_str})"

def create_booking(activity_id: int, individual_id: int, booked_by_handle: str) -> Tuple[bool, str]:
    if not capacity_available(activity_id):
        return False, "Activity is full."
    conflict = booking_conflicts(individual_id, activity_id)
    if conflict:
        return False, conflict

    with db() as conn:
        try:
            conn.execute("""
                INSERT INTO bookings(activity_id, individual_id, booked_by_handle, created_ts)
                VALUES (?,?,?,?);
            """, (activity_id, individual_id, booked_by_handle, now_ts()))
            return True, "Booked successfully."
        except sqlite3.IntegrityError:
            return False, "Already booked."

def cancel_booking(activity_id: int, individual_id: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM bookings WHERE activity_id=? AND individual_id=?;",
            (activity_id, individual_id)
        )
        return cur.rowcount > 0

def list_bookings_for_individual(individual_id: int) -> List[Tuple]:
    with db() as conn:
        cur = conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_id=?
            ORDER BY a.start_ts ASC;
        """, (individual_id,))
        return cur.fetchall()

def attendee_list(activity_id: int) -> List[Tuple]:
    with db() as conn:
        cur = conn.execute("""
            SELECT i.name, i.id, b.booked_by_handle, b.created_ts
            FROM bookings b
            JOIN individuals i ON i.id=b.individual_id
            WHERE b.activity_id=?
            ORDER BY i.name ASC;
        """, (activity_id,))
        return cur.fetchall()

def fmt_activity_row(row: Tuple) -> str:
    act_id, title, start_ts, end_ts, cap, booked = row
    start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(start_ts)))
    end_str = time.strftime("%H:%M", time.localtime(int(end_ts)))
    return f"#{act_id} — {title}\n🕒 {start_str}–{end_str}\n👥 {booked}/{cap}"

# ---------------- Bot Handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text(
            "Please set a Telegram username first (Settings → Username). Then run /start again."
        )
        return

    u = user_get(handle)
    if not u:
        await update.message.reply_text(
            "Welcome. Please register:\n/register\n\nRoles:\n- individual\n- caregiver"
        )
        return

    _, role, full_name, phone = u
    await update.message.reply_text(
        f"Logged in as @{handle}\nRole: {role}\nName: {full_name or '-'}\nPhone: {phone or '-'}\n\n"
        "Commands:\n"
        "/activities — list activities\n"
        "/book <activity_id> — book\n"
        "/my — view bookings\n"
        "/cancel <activity_id> — cancel booking\n"
        "/addperson <name> — caregiver adds an individual\n"
        "/bookfor <activity_id> <person_id> — caregiver books for an individual\n"
        "/cancelfor <activity_id> <person_id> — caregiver cancels for an individual\n"
        "/attendees <activity_id> — organiser attendee list"
    )

# Registration conversation
async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text(
            "Please set a Telegram username first (Settings → Username). Then run /register again."
        )
        return ConversationHandler.END

    await update.message.reply_text("Register as: `individual` or `caregiver`?", parse_mode=ParseMode.MARKDOWN)
    return REG_ROLE

async def reg_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = (update.message.text or "").strip().lower()
    if role not in ("individual", "caregiver"):
        await update.message.reply_text("Reply with `individual` or `caregiver`.")
        return REG_ROLE
    context.user_data["role"] = role
    await update.message.reply_text("Full name?")
    return REG_NAME

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["full_name"] = (update.message.text or "").strip()
    await update.message.reply_text("Phone number? (or type `-` to skip)")
    return REG_PHONE

async def reg_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("No Telegram username found. Set one and try again.")
        return ConversationHandler.END

    phone = (update.message.text or "").strip()
    if phone == "-":
        phone = ""
    role = context.user_data.get("role", "")
    full_name = context.user_data.get("full_name", "")

    user_upsert(handle, role, full_name, phone)

    # First time: create individual profile mapped by handle
    if role == "individual":
        individual_get_for_handle(handle)

    await update.message.reply_text("Registration complete. Use /activities to view events.")
    return ConversationHandler.END

async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Registration cancelled.")
    return ConversationHandler.END

async def activities_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle or not user_get(handle):
        await update.message.reply_text("Please /register first.")
        return

    acts = list_activities()
    if not acts:
        await update.message.reply_text("No activities available.")
        return

    lines = ["Available activities:\n"]
    for row in acts:
        lines.append(fmt_activity_row(row))
        lines.append("")
    lines.append("Book with: /book <activity_id>")
    await update.message.reply_text("\n".join(lines))

async def addperson_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return
    _, role, _, _ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can add individuals.")
        return

    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /addperson <name>")
        return

    name = parts[1].strip()
    pid = caregiver_add_individual(handle, name)
    await update.message.reply_text(f"Added: {name} (person_id={pid}).")

async def book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /book <activity_id>")
        return

    activity_id = int(parts[1])
    if not activity_exists(activity_id):
        await update.message.reply_text("Activity not found. Use /activities.")
        return

    _, role, _, _ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        ok, msg = create_booking(activity_id, individual_id, handle)
        await update.message.reply_text(msg)
        return

    # caregiver flow: show people + instruction
    people = caregiver_list_individuals(handle)
    if not people:
        await update.message.reply_text("No individuals yet. Add one with /addperson <name>.")
        return

    await update.message.reply_text(
        "Caregiver booking:\n"
        "Use: /bookfor <activity_id> <person_id>\n\n"
        "Your individuals:\n" +
        "\n".join([f"- {pid}: {name}" for pid, name in people])
    )

async def bookfor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return

    _, role, _, _ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can use /bookfor.")
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await update.message.reply_text("Usage: /bookfor <activity_id> <person_id>")
        return

    activity_id = int(parts[1])
    person_id = int(parts[2])

    owned = {pid for pid, _ in caregiver_list_individuals(handle)}
    if person_id not in owned:
        await update.message.reply_text("That person_id does not belong to you.")
        return

    ok, msg = create_booking(activity_id, person_id, handle)
    await update.message.reply_text(msg)

async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return

    _, role, _, _ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        rows = list_bookings_for_individual(individual_id)
        if not rows:
            await update.message.reply_text("No bookings yet.")
            return
        lines = ["Your bookings:\n"]
        for act_id, title, s, e in rows:
            s_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(s)))
            e_str = time.strftime("%H:%M", time.localtime(int(e)))
            lines.append(f"- #{act_id} {title} ({s_str}-{e_str})")
        await update.message.reply_text("\n".join(lines))
        return

    people = caregiver_list_individuals(handle)
    if not people:
        await update.message.reply_text("No individuals added yet. Use /addperson <name>.")
        return

    lines = ["Caregiver view:\n"]
    for pid, name in people:
        rows = list_bookings_for_individual(pid)
        lines.append(f"{name} (person_id={pid}):")
        if not rows:
            lines.append("  - (none)")
        else:
            for act_id, title, s, e in rows:
                s_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(s)))
                e_str = time.strftime("%H:%M", time.localtime(int(e)))
                lines.append(f"  - #{act_id} {title} ({s_str}-{e_str})")
        lines.append("")
    await update.message.reply_text("\n".join(lines))

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /cancel <activity_id>")
        return

    activity_id = int(parts[1])
    _, role, _, _ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        ok = cancel_booking(activity_id, individual_id)
        await update.message.reply_text("Cancelled." if ok else "No such booking.")
        return

    await update.message.reply_text(
        "Caregiver cancel:\nUse /cancelfor <activity_id> <person_id>\nSee /my for person_id."
    )

async def cancelfor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please /register first.")
        return

    _, role, _, _ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can use /cancelfor.")
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await update.message.reply_text("Usage: /cancelfor <activity_id> <person_id>")
        return

    activity_id = int(parts[1])
    person_id = int(parts[2])

    owned = {pid for pid, _ in caregiver_list_individuals(handle)}
    if person_id not in owned:
        await update.message.reply_text("That person_id does not belong to you.")
        return

    ok = cancel_booking(activity_id, person_id)
    await update.message.reply_text("Cancelled." if ok else "No such booking.")

async def attendees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle or not is_organizer(handle):
        await update.message.reply_text("Not authorised.")
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /attendees <activity_id>")
        return

    activity_id = int(parts[1])
    rows = attendee_list(activity_id)
    if not rows:
        await update.message.reply_text("No attendees yet.")
        return

    lines = ["name,individual_id,booked_by,created_time"]
    for name, pid, booked_by, created_ts in rows:
        t_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created_ts)))
        lines.append(f"{name},{pid},@{booked_by},{t_str}")

    await update.message.reply_text("Attendee list:\n\n" + "\n".join(lines))

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_cmd)],
        states={
            REG_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_role)],
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_phone)],
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(reg_conv)

    app.add_handler(CommandHandler("activities", activities_cmd))
    app.add_handler(CommandHandler("addperson", addperson_cmd))
    app.add_handler(CommandHandler("book", book_cmd))
    app.add_handler(CommandHandler("bookfor", bookfor_cmd))
    app.add_handler(CommandHandler("my", my_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("cancelfor", cancelfor_cmd))
    app.add_handler(CommandHandler("attendees", attendees_cmd))

    return app

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var is missing.")

    init_db()
    seed_demo_activities_if_empty()

    app = build_app(token)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
