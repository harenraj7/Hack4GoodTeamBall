import os
import sqlite3
import time
from typing import Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

DB_PATH = "bot.db"

# -----------------------
# Utilities
# -----------------------

def now_ts() -> int:
    return int(time.time())

def get_handle(update: Update) -> Optional[str]:
    """Telegram username (without @). Required as PK per your spec."""
    u = update.effective_user
    if not u or not u.username:
        return None
    return u.username.lower()

def parse_local_datetime_to_ts(s: str) -> Optional[int]:
    """
    Parse 'YYYY-MM-DD HH:MM' into local unix timestamp.
    Example: '2026-01-19 14:30'
    """
    try:
        tup = time.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return int(time.mktime(tup))  # local time
    except Exception:
        return None

def fmt_dt(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))

def is_admin(handle: str) -> bool:
    u = user_get(handle)
    if not u:
        return False
    return u[1] == "admin"  # role

def fmt_activity_row(row: Tuple) -> str:
    # (id, title, start_ts, end_ts, capacity, booked_count)
    act_id, title, start_ts, end_ts, capacity, booked = row
    return (
        f"#{act_id} — {title}\n"
        f"🕒 {fmt_dt(int(start_ts))}–{time.strftime('%H:%M', time.localtime(int(end_ts)))}\n"
        f"👥 {booked}/{capacity}"
    )

# -----------------------
# DB Layer
# -----------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            handle TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('individual','caregiver','admin')),
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
    with db() as conn:
        (cnt,) = conn.execute("SELECT COUNT(*) FROM activities;").fetchone()
        if cnt > 0:
            return
        base = now_ts() + 3600
        demo = [
            ("Music Therapy", base, base + 3600, 10),
            ("Physio Session", base + 1800, base + 5400, 5),   # overlaps with first
            ("Art Jam", base + 5400, base + 7200, 8),          # later
        ]
        conn.executemany(
            "INSERT INTO activities(title,start_ts,end_ts,capacity) VALUES (?,?,?,?);",
            demo
        )

def user_get(handle: str) -> Optional[Tuple]:
    with db() as conn:
        return conn.execute(
            "SELECT handle, role, full_name, phone FROM users WHERE handle=?;",
            (handle,),
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

def user_set_role(handle: str, role: str) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET role=? WHERE handle=?;", (role, handle))

def caregiver_add_individual(caregiver_handle: str, name: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,NULL);",
            (caregiver_handle, name.strip()),
        )
        return int(cur.lastrowid)

def caregiver_list_individuals(caregiver_handle: str) -> List[Tuple[int, str]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT id, name FROM individuals WHERE caregiver_handle=? ORDER BY id;",
            (caregiver_handle,),
        )
        return [(int(r[0]), r[1]) for r in cur.fetchall()]

def individual_get_for_handle(handle: str) -> Optional[int]:
    """
    If role=individual, ensure a corresponding individuals row exists:
    we store it as caregiver_handle = handle.
    """
    u = user_get(handle)
    if not u:
        return None
    _, role, full_name, _ = u
    if role != "individual":
        return None

    with db() as conn:
        row = conn.execute(
            "SELECT id FROM individuals WHERE caregiver_handle=? LIMIT 1;",
            (handle,),
        ).fetchone()
        if row:
            return int(row[0])

        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,NULL);",
            (handle, full_name or handle),
        )
        return int(cur.lastrowid)

def list_activities() -> List[Tuple]:
    # Enforced soonest-first ordering by start_ts ASC
    with db() as conn:
        cur = conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts, a.capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=a.id) AS booked_count
            FROM activities a
            ORDER BY a.start_ts ASC, a.id ASC;
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
    """Overlap: existing.start < new.end AND new.start < existing.end."""
    with db() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts FROM activities WHERE id=?;",
            (new_activity_id,),
        ).fetchone()
        if not row:
            return "Activity not found."
        new_start, new_end = int(row[0]), int(row[1])

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
        return f"Conflicts with: {title} ({fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))})"

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
            (activity_id, individual_id),
        )
        return cur.rowcount > 0

def list_bookings_for_individual(individual_id: int) -> List[Tuple]:
    with db() as conn:
        cur = conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_id=?
            ORDER BY a.start_ts ASC, a.id ASC;
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

def admin_add_activity(title: str, start_ts: int, end_ts: int, capacity: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO activities(title,start_ts,end_ts,capacity) VALUES (?,?,?,?);",
            (title.strip(), int(start_ts), int(end_ts), int(capacity)),
        )
        return int(cur.lastrowid)

# -----------------------
# Buttons
# -----------------------

def main_menu_kb(handle: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📝 Register / Update Profile", callback_data="MENU_REGISTER")],
        [InlineKeyboardButton("📅 Activities (Book)", callback_data="MENU_ACTIVITIES")],
        [InlineKeyboardButton("✅ My Bookings", callback_data="MENU_MY")],
        [InlineKeyboardButton("❌ Cancel Booking", callback_data="MENU_CANCEL")],
    ]
    if handle and is_admin(handle):
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="MENU_ADMIN")])
    else:
        rows.append([InlineKeyboardButton("🔐 Admin Login", callback_data="MENU_ADMIN_LOGIN")])
    return InlineKeyboardMarkup(rows)

def register_role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 Individual", callback_data="REGROLE_individual")],
        [InlineKeyboardButton("🧑‍🦽 Caregiver", callback_data="REGROLE_caregiver")],
        [InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")],
    ])

def activities_kb(acts: List[Tuple]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for act_id, title, *_ in acts:
        rows.append([InlineKeyboardButton(f"Book: #{act_id} {title}", callback_data=f"ACT_{act_id}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")])
    return InlineKeyboardMarkup(rows)

def caregiver_people_kb(people: List[Tuple[int, str]], activity_id: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for pid, name in people:
        rows.append([InlineKeyboardButton(f"{name} (id={pid})", callback_data=f"PICK_{activity_id}_{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="MENU_ACTIVITIES")])
    return InlineKeyboardMarkup(rows)

def cancel_kb(rows: List[Tuple]) -> InlineKeyboardMarkup:
    btns: List[List[InlineKeyboardButton]] = []
    for act_id, title, *_ in rows:
        btns.append([InlineKeyboardButton(f"Cancel: #{act_id} {title}", callback_data=f"CAN_{act_id}")])
    btns.append([InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")])
    return InlineKeyboardMarkup(btns)

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Event", callback_data="ADMIN_ADD_EVENT")],
        [InlineKeyboardButton("📋 Print Namelist", callback_data="ADMIN_NAMELIST")],
        [InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")],
    ])

def admin_events_kb(acts: List[Tuple], prefix: str) -> InlineKeyboardMarkup:
    # prefix: "ATT" or something
    rows: List[List[InlineKeyboardButton]] = []
    for act_id, title, *_ in acts:
        rows.append([InlineKeyboardButton(f"#{act_id} {title}", callback_data=f"{prefix}_{act_id}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="MENU_ADMIN")])
    return InlineKeyboardMarkup(rows)

# -----------------------
# Handlers
# -----------------------

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
            "Welcome. Use the menu below to register and book activities.",
            reply_markup=main_menu_kb(handle),
        )
        return

    _, role, full_name, phone = u
    await update.message.reply_text(
        f"Logged in as @{handle}\nRole: {role}\nName: {full_name or '-'}\nPhone: {phone or '-'}\n\nMenu:",
        reply_markup=main_menu_kb(handle),
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    handle = get_handle(update)
    data = q.data or ""

    if not handle:
        await q.edit_message_text("Set a Telegram username first (Settings → Username).")
        return

    # Home
    if data in ("MENU_HOME",):
        await q.edit_message_text("Menu:", reply_markup=main_menu_kb(handle))
        return

    # Register
    if data == "MENU_REGISTER":
        await q.edit_message_text("Choose your role:", reply_markup=register_role_kb())
        return

    if data.startswith("REGROLE_"):
        role = data.split("_", 1)[1]
        context.user_data["reg_role"] = role
        context.user_data["awaiting"] = "REG_NAME"
        await q.edit_message_text("Type your full name (one time):")
        return

    # Activities
    if data == "MENU_ACTIVITIES":
        if not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb(handle))
            return
        acts = list_activities()
        if not acts:
            await q.edit_message_text("No activities available.", reply_markup=main_menu_kb(handle))
            return
        text = "Available activities (soonest first):\n\n" + "\n\n".join(fmt_activity_row(a) for a in acts)
        await q.edit_message_text(text, reply_markup=activities_kb(acts))
        return

    if data.startswith("ACT_"):
        u = user_get(handle)
        if not u:
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb(handle))
            return
        activity_id = int(data.split("_", 1)[1])
        _, role, *_ = u

        if role == "individual":
            individual_id = individual_get_for_handle(handle)
            ok, msg = create_booking(activity_id, individual_id, handle)
            await q.edit_message_text(msg, reply_markup=main_menu_kb(handle))
            return

        if role == "caregiver":
            people = caregiver_list_individuals(handle)
            if not people:
                await q.edit_message_text(
                    "No individuals added yet.\nType: /addperson <name> (once) then try again.",
                    reply_markup=main_menu_kb(handle),
                )
                return
            await q.edit_message_text(
                "Select who you want to book for:",
                reply_markup=caregiver_people_kb(people, activity_id),
            )
            return

        await q.edit_message_text("Admins should not book as users. Use a normal account.", reply_markup=main_menu_kb(handle))
        return

    if data.startswith("PICK_"):
        u = user_get(handle)
        if not u or u[1] != "caregiver":
            await q.edit_message_text("Only caregivers can do this.", reply_markup=main_menu_kb(handle))
            return
        _, act_id_str, pid_str = data.split("_")
        activity_id = int(act_id_str)
        person_id = int(pid_str)

        owned = {pid for pid, _ in caregiver_list_individuals(handle)}
        if person_id not in owned:
            await q.edit_message_text("That person does not belong to you.", reply_markup=main_menu_kb(handle))
            return

        ok, msg = create_booking(activity_id, person_id, handle)
        await q.edit_message_text(msg, reply_markup=main_menu_kb(handle))
        return

    # My bookings
    if data == "MENU_MY":
        u = user_get(handle)
        if not u:
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb(handle))
            return
        role = u[1]

        if role == "individual":
            individual_id = individual_get_for_handle(handle)
            rows = list_bookings_for_individual(individual_id)
            if not rows:
                await q.edit_message_text("No bookings yet.", reply_markup=main_menu_kb(handle))
                return
            text = "Your bookings:\n" + "\n".join([f"- #{act_id} {title}" for act_id, title, *_ in rows])
            await q.edit_message_text(text, reply_markup=main_menu_kb(handle))
            return

        if role == "caregiver":
            people = caregiver_list_individuals(handle)
            if not people:
                await q.edit_message_text("No individuals added yet. Use /addperson <name>.", reply_markup=main_menu_kb(handle))
                return
            lines = ["Caregiver view:\n"]
            for pid, name in people:
                rows = list_bookings_for_individual(pid)
                lines.append(f"{name} (id={pid}):")
                if not rows:
                    lines.append("  - (none)")
                else:
                    for act_id, title, *_ in rows:
                        lines.append(f"  - #{act_id} {title}")
                lines.append("")
            await q.edit_message_text("\n".join(lines), reply_markup=main_menu_kb(handle))
            return

        await q.edit_message_text("Admins do not have bookings view.", reply_markup=main_menu_kb(handle))
        return

    # Cancel
    if data == "MENU_CANCEL":
        u = user_get(handle)
        if not u:
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb(handle))
            return
        if u[1] != "individual":
            await q.edit_message_text(
                "Cancel buttons currently support individuals only.\nCaregivers: /cancelfor <activity_id> <person_id>",
                reply_markup=main_menu_kb(handle),
            )
            return
        individual_id = individual_get_for_handle(handle)
        rows = list_bookings_for_individual(individual_id)
        if not rows:
            await q.edit_message_text("No bookings to cancel.", reply_markup=main_menu_kb(handle))
            return
        await q.edit_message_text("Tap a booking to cancel:", reply_markup=cancel_kb(rows))
        return

    if data.startswith("CAN_"):
        u = user_get(handle)
        if not u or u[1] != "individual":
            await q.edit_message_text("Only individuals can cancel via buttons.", reply_markup=main_menu_kb(handle))
            return
        activity_id = int(data.split("_", 1)[1])
        individual_id = individual_get_for_handle(handle)
        ok = cancel_booking(activity_id, individual_id)
        await q.edit_message_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_kb(handle))
        return

    # Admin login / panel
    if data == "MENU_ADMIN_LOGIN":
        context.user_data["awaiting"] = "ADMIN_PASSWORD"
        await q.edit_message_text("Enter admin password:")
        return

    if data == "MENU_ADMIN":
        if not is_admin(handle):
            await q.edit_message_text("Not authorised. Use Admin Login.", reply_markup=main_menu_kb(handle))
            return
        await q.edit_message_text("Admin Panel:", reply_markup=admin_panel_kb())
        return

    if data == "ADMIN_ADD_EVENT":
        if not is_admin(handle):
            await q.edit_message_text("Not authorised.", reply_markup=main_menu_kb(handle))
            return
        # Start add-event wizard
        context.user_data["admin_event"] = {}
        context.user_data["awaiting"] = "ADMIN_EVENT_TITLE"
        await q.edit_message_text("Event title:")
        return

    if data == "ADMIN_NAMELIST":
        if not is_admin(handle):
            await q.edit_message_text("Not authorised.", reply_markup=main_menu_kb(handle))
            return
        acts = list_activities()
        if not acts:
            await q.edit_message_text("No events available.", reply_markup=admin_panel_kb())
            return
        await q.edit_message_text(
            "Select an event to print namelist:",
            reply_markup=admin_events_kb(acts, prefix="NL"),
        )
        return

    if data.startswith("NL_"):
        if not is_admin(handle):
            await q.edit_message_text("Not authorised.", reply_markup=main_menu_kb(handle))
            return
        activity_id = int(data.split("_", 1)[1])
        rows = attendee_list(activity_id)
        if not rows:
            await q.edit_message_text("No attendees yet.", reply_markup=admin_panel_kb())
            return

        lines = ["name,individual_id,booked_by,created_time"]
        for name, pid, booked_by, created_ts in rows:
            lines.append(f"{name},{pid},@{booked_by},{fmt_dt(int(created_ts))}")

        # Telegram message length limit exists; hackathon approach: send as text
        await q.edit_message_text("Namelist:\n\n" + "\n".join(lines), reply_markup=admin_panel_kb())
        return

    # Fallback
    await q.edit_message_text("Menu:", reply_markup=main_menu_kb(handle))

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single text handler for:
    - button-based registration input
    - admin password input
    - admin add-event wizard input
    """
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Please set a Telegram username first (Settings → Username).")
        return

    awaiting = context.user_data.get("awaiting")
    msg = (update.message.text or "").strip()

    # ----- Admin password -----
    if awaiting == "ADMIN_PASSWORD":
        admin_pw = os.environ.get("ADMIN_PASSWORD", "")
        if not admin_pw:
            await update.message.reply_text(
                "ADMIN_PASSWORD is not set on the machine running the bot.",
                reply_markup=main_menu_kb(handle),
            )
            context.user_data["awaiting"] = None
            return

        if msg == admin_pw:
            # If user doesn't exist yet, create as admin (minimal profile)
            if not user_get(handle):
                user_upsert(handle, "admin", full_name=handle, phone="")
            else:
                user_set_role(handle, "admin")
            await update.message.reply_text("Admin access granted.", reply_markup=main_menu_kb(handle))
        else:
            await update.message.reply_text("Wrong password.", reply_markup=main_menu_kb(handle))
        context.user_data["awaiting"] = None
        return

    # ----- Registration (name then phone) -----
    if awaiting == "REG_NAME":
        context.user_data["reg_full_name"] = msg
        context.user_data["awaiting"] = "REG_PHONE"
        await update.message.reply_text("Phone number? (or type '-' to skip)")
        return

    if awaiting == "REG_PHONE":
        phone = "" if msg == "-" else msg
        role = context.user_data.get("reg_role", "individual")
        full_name = context.user_data.get("reg_full_name", handle)

        user_upsert(handle, role, full_name, phone)
        if role == "individual":
            individual_get_for_handle(handle)

        context.user_data["awaiting"] = None
        await update.message.reply_text("Registration complete.", reply_markup=main_menu_kb(handle))
        return

    # ----- Admin add-event wizard -----
    if awaiting and awaiting.startswith("ADMIN_EVENT_"):
        ev = context.user_data.get("admin_event", {})

        if awaiting == "ADMIN_EVENT_TITLE":
            ev["title"] = msg
            context.user_data["admin_event"] = ev
            context.user_data["awaiting"] = "ADMIN_EVENT_START"
            await update.message.reply_text("Start datetime (YYYY-MM-DD HH:MM), e.g. 2026-01-19 14:30")
            return

        if awaiting == "ADMIN_EVENT_START":
            ts = parse_local_datetime_to_ts(msg)
            if ts is None:
                await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
                return
            ev["start_ts"] = ts
            context.user_data["admin_event"] = ev
            context.user_data["awaiting"] = "ADMIN_EVENT_END"
            await update.message.reply_text("End datetime (YYYY-MM-DD HH:MM)")
            return

        if awaiting == "ADMIN_EVENT_END":
            ts = parse_local_datetime_to_ts(msg)
            if ts is None:
                await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
                return
            ev["end_ts"] = ts
            if int(ev["end_ts"]) <= int(ev["start_ts"]):
                await update.message.reply_text("End must be after start. Enter end datetime again.")
                return
            context.user_data["admin_event"] = ev
            context.user_data["awaiting"] = "ADMIN_EVENT_CAP"
            await update.message.reply_text("Capacity (number), e.g. 20")
            return

        if awaiting == "ADMIN_EVENT_CAP":
            if not msg.isdigit() or int(msg) <= 0:
                await update.message.reply_text("Capacity must be a positive integer.")
                return
            ev["capacity"] = int(msg)

            # Create activity
            act_id = admin_add_activity(ev["title"], ev["start_ts"], ev["end_ts"], ev["capacity"])

            context.user_data["awaiting"] = None
            context.user_data["admin_event"] = {}

            await update.message.reply_text(
                f"Event created: #{act_id}\n{ev['title']}\n🕒 {fmt_dt(ev['start_ts'])}–{time.strftime('%H:%M', time.localtime(ev['end_ts']))}\n👥 cap={ev['capacity']}",
                reply_markup=main_menu_kb(handle),
            )
            return

    # If they typed random text, show menu hint
    await update.message.reply_text("Use /start and the menu buttons.", reply_markup=main_menu_kb(handle))

# -----------------------
# Command fallbacks (kept minimal)
# -----------------------

async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first (Settings → Username).")
        return
    # Send role buttons (button-first)
    await update.message.reply_text("Choose your role:", reply_markup=register_role_kb())

async def cmd_addperson(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u or u[1] != "caregiver":
        await update.message.reply_text("Only caregivers can add individuals. Register as caregiver first.", reply_markup=main_menu_kb(handle or ""))
        return
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /addperson <name>")
        return
    pid = caregiver_add_individual(handle, parts[1].strip())
    await update.message.reply_text(f"Added individual (id={pid}).", reply_markup=main_menu_kb(handle))

async def cmd_cancelfor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u or u[1] != "caregiver":
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
    await update.message.reply_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_kb(handle))

# -----------------------
# App wiring
# -----------------------

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("addperson", cmd_addperson))
    app.add_handler(CommandHandler("cancelfor", cmd_cancelfor))

    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

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
