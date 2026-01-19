import sqlite3
import time
from typing import Optional, List, Tuple

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
ADMIN_PASSWORD = "admin_password"  # per your requirement

# ---------- Time helpers ----------

def now_ts() -> int:
    return int(time.time())

def fmt_dt(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))

def parse_local_dt(s: str) -> Optional[int]:
    """Parse 'YYYY-MM-DD HH:MM' in local time."""
    try:
        t = time.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return int(time.mktime(t))
    except Exception:
        return None

# ---------- Telegram helpers ----------

def get_handle(update: Update) -> Optional[str]:
    u = update.effective_user
    if not u or not u.username:
        return None
    return u.username.lower()

# ---------- DB ----------

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
            ("Physio Session", base + 1800, base + 5400, 5),
            ("Art Jam", base + 5400, base + 7200, 8),
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

def individual_get_or_create_for_individual_user(handle: str) -> Optional[int]:
    """
    For role=individual, we create exactly one 'individuals' row where caregiver_handle == handle.
    """
    u = user_get(handle)
    if not u or u[1] != "individual":
        return None
    full_name = u[2] or handle

    with db() as conn:
        row = conn.execute(
            "SELECT id FROM individuals WHERE caregiver_handle=? LIMIT 1;",
            (handle,),
        ).fetchone()
        if row:
            return int(row[0])

        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name) VALUES (?,?);",
            (handle, full_name),
        )
        return int(cur.lastrowid)

def caregiver_add_person(caregiver_handle: str, name: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name) VALUES (?,?);",
            (caregiver_handle, name.strip()),
        )
        return int(cur.lastrowid)

def caregiver_list_people(caregiver_handle: str) -> List[Tuple[int, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name FROM individuals WHERE caregiver_handle=? ORDER BY id;",
            (caregiver_handle,),
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

def list_activities() -> List[Tuple]:
    # soonest first
    with db() as conn:
        return conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts, a.capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=a.id)
            FROM activities a
            ORDER BY a.start_ts ASC, a.id ASC;
        """).fetchall()

def capacity_available(activity_id: int) -> bool:
    with db() as conn:
        row = conn.execute("""
            SELECT a.capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=a.id) AS booked
            FROM activities a
            WHERE a.id=?;
        """, (activity_id,)).fetchone()
        if not row:
            return False
        cap, booked = int(row[0]), int(row[1])
        return booked < cap

def booking_conflict(individual_id: int, activity_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts, title FROM activities WHERE id=?;",
            (activity_id,),
        ).fetchone()
        if not row:
            return "Activity not found."
        new_start, new_end, _ = int(row[0]), int(row[1]), row[2]

        hit = conn.execute("""
            SELECT a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_id=?
              AND a.start_ts < ?
              AND ? < a.end_ts
            LIMIT 1;
        """, (individual_id, new_end, new_start)).fetchone()

        if not hit:
            return None
        title, s, e = hit
        return f"Conflicts with {title} ({fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))})"

def create_booking(activity_id: int, individual_id: int, booked_by_handle: str) -> Tuple[bool, str]:
    if not capacity_available(activity_id):
        return False, "Activity is full."
    conflict = booking_conflict(individual_id, activity_id)
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

def list_bookings(individual_id: int) -> List[Tuple]:
    with db() as conn:
        return conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_id=?
            ORDER BY a.start_ts ASC, a.id ASC;
        """, (individual_id,)).fetchall()

def cancel_booking(activity_id: int, individual_id: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM bookings WHERE activity_id=? AND individual_id=?;",
            (activity_id, individual_id),
        )
        return cur.rowcount > 0

def attendee_list(activity_id: int) -> List[Tuple]:
    with db() as conn:
        return conn.execute("""
            SELECT i.name, i.id, b.booked_by_handle, b.created_ts
            FROM bookings b
            JOIN individuals i ON i.id=b.individual_id
            WHERE b.activity_id=?
            ORDER BY i.name ASC;
        """, (activity_id,)).fetchall()

def admin_add_activity(title: str, start_ts: int, end_ts: int, capacity: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO activities(title,start_ts,end_ts,capacity) VALUES (?,?,?,?);",
            (title.strip(), int(start_ts), int(end_ts), int(capacity)),
        )
        return int(cur.lastrowid)

# ---------- UI: Reply keyboard (main menu always shows Admin Login) ----------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("📝 Register / Update Profile")],
        [KeyboardButton("📅 Activities (Book)")],
        [KeyboardButton("✅ My Bookings"), KeyboardButton("❌ Cancel Booking")],
        [KeyboardButton("🔐 Admin Login"), KeyboardButton("🛠 Admin Panel")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def register_role_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("🙋 Individual"), KeyboardButton("🧑‍🦽 Caregiver")],
        [KeyboardButton("⬅️ Back")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def admin_panel_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("➕ Add Event"), KeyboardButton("📋 Print Namelist")],
        [KeyboardButton("⬅️ Back")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

# ---------- UI: Inline keyboards for dynamic lists ----------

def activities_inline_kb(acts: List[Tuple]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"Book #{a[0]} — {a[1]}", callback_data=f"BOOK_ACT|{a[0]}")] for a in acts]
    return InlineKeyboardMarkup(rows)

def caregiver_people_inline_kb(people: List[Tuple[int, str]], activity_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{name} (id={pid})", callback_data=f"BOOK_FOR|{activity_id}|{pid}")]
            for pid, name in people]
    return InlineKeyboardMarkup(rows)

def cancel_inline_kb(bookings: List[Tuple]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"Cancel #{b[0]} — {b[1]}", callback_data=f"CANCEL|{b[0]}")] for b in bookings]
    return InlineKeyboardMarkup(rows)

def admin_events_inline_kb(acts: List[Tuple], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"#{a[0]} — {a[1]}", callback_data=f"{prefix}|{a[0]}")] for a in acts]
    return InlineKeyboardMarkup(rows)

# ---------- Bot handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text(
            "Set a Telegram username first (Settings → Username), then /start again."
        )
        return
    await update.message.reply_text("Menu:", reply_markup=main_menu_keyboard())

async def handle_reply_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first (Settings → Username).")
        return

    text = (update.message.text or "").strip()

    # Back buttons
    if text == "⬅️ Back":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("tmp", None)
        await update.message.reply_text("Menu:", reply_markup=main_menu_keyboard())
        return

    # Main menu: Register
    if text == "📝 Register / Update Profile":
        context.user_data["awaiting"] = "REG_ROLE"
        await update.message.reply_text("Choose role:", reply_markup=register_role_keyboard())
        return

    # Main menu: Activities
    if text == "📅 Activities (Book)":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Please register first (tap Register).", reply_markup=main_menu_keyboard())
            return
        acts = list_activities()
        if not acts:
            await update.message.reply_text("No activities available.", reply_markup=main_menu_keyboard())
            return
        msg = "Available activities (soonest first):\n\n" + "\n\n".join(
            f"#{a[0]} — {a[1]}\n🕒 {fmt_dt(int(a[2]))}–{time.strftime('%H:%M', time.localtime(int(a[3])))}\n👥 {a[5]}/{a[4]}"
            for a in acts
        )
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard())
        await update.message.reply_text("Tap to book:", reply_markup=activities_inline_kb(acts))
        return

    # Main menu: My bookings
    if text == "✅ My Bookings":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Please register first.", reply_markup=main_menu_keyboard())
            return
        if u[1] != "individual":
            await update.message.reply_text("This view is for individuals only (for now).", reply_markup=main_menu_keyboard())
            return
        ind_id = individual_get_or_create_for_individual_user(handle)
        rows = list_bookings(ind_id)
        if not rows:
            await update.message.reply_text("No bookings yet.", reply_markup=main_menu_keyboard())
            return
        msg = "Your bookings:\n" + "\n".join([f"- #{r[0]} {r[1]}" for r in rows])
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard())
        return

    # Main menu: Cancel booking
    if text == "❌ Cancel Booking":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Please register first.", reply_markup=main_menu_keyboard())
            return
        if u[1] != "individual":
            await update.message.reply_text("Cancel buttons are for individuals only (for now).", reply_markup=main_menu_keyboard())
            return
        ind_id = individual_get_or_create_for_individual_user(handle)
        rows = list_bookings(ind_id)
        if not rows:
            await update.message.reply_text("No bookings to cancel.", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Tap a booking to cancel:", reply_markup=cancel_inline_kb(rows))
        return

    # Main menu: Admin login (ALWAYS present)
    if text == "🔐 Admin Login":
        context.user_data["awaiting"] = "ADMIN_PASSWORD"
        await update.message.reply_text("Enter admin password:")
        return

    # Main menu: Admin panel
    if text == "🛠 Admin Panel":
        u = user_get(handle)
        if not u or u[1] != "admin":
            await update.message.reply_text("Not authorised. Tap Admin Login first.", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Admin Panel:", reply_markup=admin_panel_keyboard())
        return

    # Admin Panel options (reply keyboard)
    if text == "➕ Add Event":
        u = user_get(handle)
        if not u or u[1] != "admin":
            await update.message.reply_text("Not authorised.", reply_markup=main_menu_keyboard())
            return
        context.user_data["awaiting"] = "ADMIN_ADD_TITLE"
        context.user_data["tmp"] = {}
        await update.message.reply_text("Event title:", reply_markup=admin_panel_keyboard())
        return

    if text == "📋 Print Namelist":
        u = user_get(handle)
        if not u or u[1] != "admin":
            await update.message.reply_text("Not authorised.", reply_markup=main_menu_keyboard())
            return
        acts = list_activities()
        if not acts:
            await update.message.reply_text("No events available.", reply_markup=admin_panel_keyboard())
            return
        await update.message.reply_text("Select event:", reply_markup=admin_events_inline_kb(acts, "NAMELIST"))
        return

    # Registration role selection (reply)
    if context.user_data.get("awaiting") == "REG_ROLE":
        if text not in ("🙋 Individual", "🧑‍🦽 Caregiver"):
            await update.message.reply_text("Pick a role using the buttons.", reply_markup=register_role_keyboard())
            return
        role = "individual" if text.startswith("🙋") else "caregiver"
        context.user_data["tmp"] = {"role": role}
        context.user_data["awaiting"] = "REG_NAME"
        await update.message.reply_text("Type your full name:", reply_markup=main_menu_keyboard())
        return

    # If user typed something else, route to text wizard handler
    await handle_wizards(update, context)

async def handle_wizards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        return
    msg = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")
    tmp = context.user_data.get("tmp", {})

    # Admin password
    if awaiting == "ADMIN_PASSWORD":
        if msg == ADMIN_PASSWORD:
            if not user_get(handle):
                user_upsert(handle, "admin", handle, "")
            else:
                user_set_role(handle, "admin")
            context.user_data["awaiting"] = None
            await update.message.reply_text("Admin access granted. Tap Admin Panel.", reply_markup=main_menu_keyboard())
        else:
            context.user_data["awaiting"] = None
            await update.message.reply_text("Wrong password.", reply_markup=main_menu_keyboard())
        return

    # Register name
    if awaiting == "REG_NAME":
        tmp["full_name"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "REG_PHONE"
        await update.message.reply_text("Type phone number (or '-' to skip):", reply_markup=main_menu_keyboard())
        return

    # Register phone
    if awaiting == "REG_PHONE":
        phone = "" if msg == "-" else msg
        role = tmp.get("role", "individual")
        full_name = tmp.get("full_name", handle)
        user_upsert(handle, role, full_name, phone)
        if role == "individual":
            individual_get_or_create_for_individual_user(handle)
        context.user_data["awaiting"] = None
        context.user_data["tmp"] = {}
        await update.message.reply_text("Registration complete.", reply_markup=main_menu_keyboard())
        return

    # Admin add event wizard
    if awaiting == "ADMIN_ADD_TITLE":
        tmp["title"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADMIN_ADD_START"
        await update.message.reply_text("Start datetime (YYYY-MM-DD HH:MM):", reply_markup=admin_panel_keyboard())
        return

    if awaiting == "ADMIN_ADD_START":
        ts = parse_local_dt(msg)
        if ts is None:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
            return
        tmp["start_ts"] = ts
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADMIN_ADD_END"
        await update.message.reply_text("End datetime (YYYY-MM-DD HH:MM):", reply_markup=admin_panel_keyboard())
        return

    if awaiting == "ADMIN_ADD_END":
        ts = parse_local_dt(msg)
        if ts is None:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
            return
        if ts <= int(tmp["start_ts"]):
            await update.message.reply_text("End must be after start. Enter end datetime again.")
            return
        tmp["end_ts"] = ts
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADMIN_ADD_CAP"
        await update.message.reply_text("Capacity (positive integer):", reply_markup=admin_panel_keyboard())
        return

    if awaiting == "ADMIN_ADD_CAP":
        if not msg.isdigit() or int(msg) <= 0:
            await update.message.reply_text("Capacity must be a positive integer.")
            return
        tmp["capacity"] = int(msg)
        act_id = admin_add_activity(tmp["title"], tmp["start_ts"], tmp["end_ts"], tmp["capacity"])
        context.user_data["awaiting"] = None
        context.user_data["tmp"] = {}
        await update.message.reply_text(
            f"Event created: #{act_id}\n{tmp['title']}\n🕒 {fmt_dt(tmp['start_ts'])}–{time.strftime('%H:%M', time.localtime(tmp['end_ts']))}\n👥 cap={tmp['capacity']}",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Not in a flow
    await update.message.reply_text("Use /start and the menu buttons.", reply_markup=main_menu_keyboard())

async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    handle = get_handle(update)
    if not handle:
        await q.edit_message_text("Set a Telegram username first.")
        return

    data = q.data or ""
    parts = data.split("|")

    # Booking flow
    if parts[0] == "BOOK_ACT":
        activity_id = int(parts[1])
        u = user_get(handle)
        if not u:
            await q.edit_message_text("Please register first.")
            return

        role = u[1]
        if role == "individual":
            ind_id = individual_get_or_create_for_individual_user(handle)
            ok, msg = create_booking(activity_id, ind_id, handle)
            await q.edit_message_text(msg)
            return

        if role == "caregiver":
            people = caregiver_list_people(handle)
            if not people:
                await q.edit_message_text("No individuals added. Use /addperson <name> first.")
                return
            await q.edit_message_text(
                "Select who to book for:",
                reply_markup=caregiver_people_inline_kb(people, activity_id),
            )
            return

        await q.edit_message_text("Admins cannot book as users. Use a normal account.")
        return

    if parts[0] == "BOOK_FOR":
        activity_id = int(parts[1])
        person_id = int(parts[2])
        u = user_get(handle)
        if not u or u[1] != "caregiver":
            await q.edit_message_text("Only caregivers can do this.")
            return

        owned = {pid for pid, _ in caregiver_list_people(handle)}
        if person_id not in owned:
            await q.edit_message_text("That person does not belong to you.")
            return

        ok, msg = create_booking(activity_id, person_id, handle)
        await q.edit_message_text(msg)
        return

    # Cancel flow
    if parts[0] == "CANCEL":
        activity_id = int(parts[1])
        u = user_get(handle)
        if not u or u[1] != "individual":
            await q.edit_message_text("Only individuals can cancel via buttons.")
            return
        ind_id = individual_get_or_create_for_individual_user(handle)
        ok = cancel_booking(activity_id, ind_id)
        await q.edit_message_text("Cancelled." if ok else "No such booking.")
        return

    # Admin namelist
    if parts[0] == "NAMELIST":
        activity_id = int(parts[1])
        u = user_get(handle)
        if not u or u[1] != "admin":
            await q.edit_message_text("Not authorised.")
            return

        rows = attendee_list(activity_id)
        if not rows:
            await q.edit_message_text("No attendees yet.")
            return

        lines = ["name,individual_id,booked_by,created_time"]
        for name, pid, booked_by, created_ts in rows:
            lines.append(f"{name},{pid},@{booked_by},{fmt_dt(int(created_ts))}")
        await q.edit_message_text("Namelist:\n\n" + "\n".join(lines))
        return

# ---------- Minimal command fallbacks (caregiver add person) ----------

async def addperson_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first.")
        return
    u = user_get(handle)
    if not u or u[1] != "caregiver":
        await update.message.reply_text("Only caregivers can add individuals. Register as caregiver first.", reply_markup=main_menu_keyboard())
        return
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /addperson <name>")
        return
    pid = caregiver_add_person(handle, parts[1].strip())
    await update.message.reply_text(f"Added individual: id={pid}", reply_markup=main_menu_keyboard())

# ---------- App wiring ----------

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addperson", addperson_cmd))

    # Reply keyboard presses come as text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_buttons))

    # Inline keyboards
    app.add_handler(CallbackQueryHandler(inline_callback))

    return app

def main() -> None:
    token = None
    # read BOT_TOKEN from environment
    import os
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var is missing.")

    init_db()
    seed_demo_activities_if_empty()

    app = build_app(token)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
