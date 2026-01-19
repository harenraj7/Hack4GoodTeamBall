import os
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
ADMIN_PASSWORD = "admin_password"

# ------------------------
# Helpers
# ------------------------

def now_ts() -> int:
    return int(time.time())

def fmt_dt(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))

def parse_local_dt(s: str) -> Optional[int]:
    try:
        t = time.strptime(s.strip(), "%Y-%m-%d %H:%M")
        return int(time.mktime(t))  # local time
    except Exception:
        return None

def get_handle(update: Update) -> Optional[str]:
    u = update.effective_user
    if not u or not u.username:
        return None
    return u.username.lower()

def norm_handle(s: str) -> str:
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    return s.lower()

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

# ------------------------
# DB init
# ------------------------

def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            handle TEXT PRIMARY KEY,
            role TEXT NOT NULL CHECK(role IN ('individual','caregiver','admin')),
            full_name TEXT,
            phone TEXT,
            chat_id INTEGER
        );

        -- Individuals under care. These can be "profiles" even if they don't run the bot.
        CREATE TABLE IF NOT EXISTS individual_profiles (
            handle TEXT PRIMARY KEY,           -- telehandle of the individual
            name TEXT NOT NULL,
            created_ts INTEGER NOT NULL
        );

        -- Caregiver -> individuals mapping
        CREATE TABLE IF NOT EXISTS caregiver_links (
            caregiver_handle TEXT NOT NULL,
            individual_handle TEXT NOT NULL,
            PRIMARY KEY (caregiver_handle, individual_handle),
            FOREIGN KEY(caregiver_handle) REFERENCES users(handle) ON DELETE CASCADE,
            FOREIGN KEY(individual_handle) REFERENCES individual_profiles(handle) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            location TEXT,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            capacity INTEGER NOT NULL
        );

        /*
          Booking model:
          - individual_handle always set
          - booked_by_handle: who initiated the booking (individual or caregiver)
          - caregiver_handle: optional caregiver joining with individual
          - caregiver_status: 'pending'|'confirmed'|'declined'|NULL
        */
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL,
            individual_handle TEXT NOT NULL,
            booked_by_handle TEXT NOT NULL,
            caregiver_handle TEXT,
            caregiver_status TEXT CHECK(caregiver_status IN ('pending','confirmed','declined')),
            created_ts INTEGER NOT NULL,
            UNIQUE(activity_id, individual_handle),
            FOREIGN KEY(activity_id) REFERENCES activities(id) ON DELETE CASCADE,
            FOREIGN KEY(individual_handle) REFERENCES individual_profiles(handle) ON DELETE CASCADE,
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
            ("Music Therapy", "Group music activities", "Room A", base, base + 3600, 10),
            ("Physio Session", "Guided physio exercises", "Room B", base + 5400, base + 7200, 5),
        ]
        conn.executemany(
            "INSERT INTO activities(title,description,location,start_ts,end_ts,capacity) VALUES (?,?,?,?,?,?);",
            demo
        )

# ------------------------
# DB ops
# ------------------------

def user_get(handle: str) -> Optional[Tuple]:
    with db() as conn:
        return conn.execute(
            "SELECT handle, role, full_name, phone, chat_id FROM users WHERE handle=?;",
            (handle,),
        ).fetchone()

def user_upsert(handle: str, role: str, full_name: str, phone: str, chat_id: Optional[int]) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO users(handle, role, full_name, phone, chat_id)
            VALUES (?,?,?,?,?)
            ON CONFLICT(handle) DO UPDATE SET
                role=excluded.role,
                full_name=excluded.full_name,
                phone=excluded.phone,
                chat_id=excluded.chat_id;
        """, (handle, role, full_name, phone, chat_id))

def user_set_chat_id(handle: str, chat_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET chat_id=? WHERE handle=?;", (chat_id, handle))

def user_set_role(handle: str, role: str) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET role=? WHERE handle=?;", (role, handle))

def individual_profile_upsert(ind_handle: str, name: str) -> None:
    ind_handle = norm_handle(ind_handle)
    with db() as conn:
        conn.execute("""
            INSERT INTO individual_profiles(handle, name, created_ts)
            VALUES (?,?,?)
            ON CONFLICT(handle) DO UPDATE SET
                name=excluded.name;
        """, (ind_handle, name.strip(), now_ts()))

def caregiver_link_add(caregiver_handle: str, individual_handle: str) -> None:
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO caregiver_links(caregiver_handle, individual_handle)
            VALUES (?,?);
        """, (caregiver_handle, norm_handle(individual_handle)))

def caregiver_linked_individuals(caregiver_handle: str) -> List[Tuple[str, str]]:
    with db() as conn:
        rows = conn.execute("""
            SELECT p.handle, p.name
            FROM caregiver_links l
            JOIN individual_profiles p ON p.handle=l.individual_handle
            WHERE l.caregiver_handle=?
            ORDER BY p.name ASC;
        """, (caregiver_handle,)).fetchall()
        return [(r[0], r[1]) for r in rows]

def ensure_self_individual_profile(handle: str, name_fallback: str) -> str:
    """
    For role=individual (bot user), their 'individual_profile' is their own telehandle.
    """
    h = norm_handle(handle)
    with db() as conn:
        row = conn.execute("SELECT handle FROM individual_profiles WHERE handle=?;", (h,)).fetchone()
        if row:
            return h
    individual_profile_upsert(h, name_fallback)
    return h

def list_activities() -> List[Tuple]:
    with db() as conn:
        return conn.execute("""
            SELECT id, title, description, location, start_ts, end_ts, capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=activities.id) AS booked
            FROM activities
            ORDER BY start_ts ASC, id ASC;
        """).fetchall()

def activity_get(act_id: int) -> Optional[Tuple]:
    with db() as conn:
        return conn.execute("""
            SELECT id, title, description, location, start_ts, end_ts, capacity,
                   (SELECT COUNT(*) FROM bookings b WHERE b.activity_id=activities.id) AS booked
            FROM activities WHERE id=?;
        """, (act_id,)).fetchone()

def capacity_available(act_id: int) -> bool:
    row = activity_get(act_id)
    if not row:
        return False
    cap, booked = int(row[6]), int(row[7])
    return booked < cap

def booking_conflict(individual_handle: str, act_id: int) -> Optional[str]:
    """
    Overlap check: existing.start < new.end AND new.start < existing.end
    """
    row = activity_get(act_id)
    if not row:
        return "Activity not found."
    new_start, new_end = int(row[4]), int(row[5])

    with db() as conn:
        hit = conn.execute("""
            SELECT a.title, a.start_ts, a.end_ts
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_handle=?
              AND a.start_ts < ?
              AND ? < a.end_ts
            LIMIT 1;
        """, (norm_handle(individual_handle), new_end, new_start)).fetchone()

    if not hit:
        return None
    title, s, e = hit
    return f"Conflicts with {title} ({fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))})"

def create_booking(activity_id: int, individual_handle: str, booked_by: str,
                   caregiver_handle: Optional[str], caregiver_status: Optional[str]) -> Tuple[bool, str]:
    if not capacity_available(activity_id):
        return False, "Activity is full."
    conflict = booking_conflict(individual_handle, activity_id)
    if conflict:
        return False, conflict

    with db() as conn:
        try:
            conn.execute("""
                INSERT INTO bookings(activity_id, individual_handle, booked_by_handle, caregiver_handle, caregiver_status, created_ts)
                VALUES (?,?,?,?,?,?);
            """, (
                int(activity_id),
                norm_handle(individual_handle),
                norm_handle(booked_by),
                norm_handle(caregiver_handle) if caregiver_handle else None,
                caregiver_status,
                now_ts()
            ))
            return True, "Booked successfully."
        except sqlite3.IntegrityError:
            return False, "Already booked."

def update_caregiver_status(activity_id: int, individual_handle: str, caregiver_handle: str, status: str) -> None:
    with db() as conn:
        conn.execute("""
            UPDATE bookings
            SET caregiver_status=?
            WHERE activity_id=? AND individual_handle=? AND caregiver_handle=?;
        """, (status, int(activity_id), norm_handle(individual_handle), norm_handle(caregiver_handle)))

def list_bookings_for_individual(ind_handle: str) -> List[Tuple]:
    with db() as conn:
        return conn.execute("""
            SELECT a.id, a.title, a.start_ts, a.end_ts, b.caregiver_handle, b.caregiver_status
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            WHERE b.individual_handle=?
            ORDER BY a.start_ts ASC, a.id ASC;
        """, (norm_handle(ind_handle),)).fetchall()

def cancel_booking(activity_id: int, individual_handle: str) -> bool:
    with db() as conn:
        cur = conn.execute("""
            DELETE FROM bookings WHERE activity_id=? AND individual_handle=?;
        """, (int(activity_id), norm_handle(individual_handle)))
        return cur.rowcount > 0

def caregiver_view_attendance(caregiver_handle: str) -> Tuple[List[Tuple], List[Tuple]]:
    """
    Returns:
      - with_me: bookings where caregiver_handle matches and caregiver_status confirmed/pending
      - without_me: bookings for linked individuals where caregiver_handle is null OR declined
    """
    caregiver_handle = norm_handle(caregiver_handle)
    with db() as conn:
        with_me = conn.execute("""
            SELECT p.name, p.handle, a.title, a.start_ts, a.end_ts, b.caregiver_status, a.id
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            JOIN individual_profiles p ON p.handle=b.individual_handle
            WHERE b.caregiver_handle=? AND b.caregiver_status IN ('pending','confirmed')
            ORDER BY a.start_ts ASC;
        """, (caregiver_handle,)).fetchall()

        without_me = conn.execute("""
            SELECT p.name, p.handle, a.title, a.start_ts, a.end_ts, a.id
            FROM bookings b
            JOIN activities a ON a.id=b.activity_id
            JOIN individual_profiles p ON p.handle=b.individual_handle
            JOIN caregiver_links l ON l.individual_handle=p.handle
            WHERE l.caregiver_handle=?
              AND (b.caregiver_handle IS NULL OR b.caregiver_status='declined')
            ORDER BY a.start_ts ASC;
        """, (caregiver_handle,)).fetchall()

    return with_me, without_me

def admin_add_activity(title: str, description: str, location: str, start_ts: int, end_ts: int, capacity: int) -> int:
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO activities(title,description,location,start_ts,end_ts,capacity)
            VALUES (?,?,?,?,?,?);
        """, (title.strip(), description.strip(), location.strip(), int(start_ts), int(end_ts), int(capacity)))
        return int(cur.lastrowid)

# ------------------------
# UI
# ------------------------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("📝 Register / Update Profile")],
        [KeyboardButton("📅 Activities")],
        [KeyboardButton("✅ My Bookings"), KeyboardButton("❌ Cancel Booking")],
        [KeyboardButton("👥 Caregiver: My Attendance")],
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
        [KeyboardButton("➕ Add Event"), KeyboardButton("⬅️ Back")],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def activities_inline_kb(acts: List[Tuple]) -> InlineKeyboardMarkup:
    rows = []
    for a in acts:
        act_id, title = int(a[0]), a[1]
        rows.append([
            InlineKeyboardButton(f"ℹ️ Details #{act_id}", callback_data=f"DETAILS|{act_id}"),
            InlineKeyboardButton(f"✅ Book #{act_id}", callback_data=f"BOOK|{act_id}"),
        ])
    return InlineKeyboardMarkup(rows)

def yesno_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data=f"{prefix}|YES"),
         InlineKeyboardButton("No", callback_data=f"{prefix}|NO")]
    ])

def caregiver_pick_individual_kb(caregiver_handle: str, activity_id: int) -> InlineKeyboardMarkup:
    people = caregiver_linked_individuals(caregiver_handle)
    rows = []
    for h, name in people:
        rows.append([InlineKeyboardButton(f"{name} (@{h})", callback_data=f"CGBOOK|{activity_id}|{h}")])
    return InlineKeyboardMarkup(rows)

def caregiver_confirm_kb(activity_id: int, individual_handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"CGCONF|{activity_id}|{individual_handle}|YES"),
            InlineKeyboardButton("❌ Decline", callback_data=f"CGCONF|{activity_id}|{individual_handle}|NO"),
        ]
    ])

# ------------------------
# Bot handlers
# ------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first (Settings → Username), then /start again.")
        return

    # store chat_id if user exists (or later upon register/login)
    chat_id = update.effective_chat.id
    if user_get(handle):
        user_set_chat_id(handle, chat_id)

    await update.message.reply_text("Menu:", reply_markup=main_menu_keyboard())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first (Settings → Username).")
        return

    text = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")
    tmp = context.user_data.get("tmp", {})

    # Back
    if text == "⬅️ Back":
        context.user_data.clear()
        await update.message.reply_text("Menu:", reply_markup=main_menu_keyboard())
        return

    # Wizard routing
    if awaiting:
        await handle_wizard_text(update, context)
        return

    # Main menu
    if text == "📝 Register / Update Profile":
        context.user_data["awaiting"] = "REG_ROLE"
        await update.message.reply_text("Choose role:", reply_markup=register_role_keyboard())
        return

    if text == "📅 Activities":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Register first (tap Register).", reply_markup=main_menu_keyboard())
            return
        acts = list_activities()
        if not acts:
            await update.message.reply_text("No activities available.", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Activities (tap Details or Book):", reply_markup=main_menu_keyboard())
        await update.message.reply_text("Select:", reply_markup=activities_inline_kb(acts))
        return

    if text == "✅ My Bookings":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Register first.", reply_markup=main_menu_keyboard())
            return
        if u[1] != "individual":
            await update.message.reply_text("This view is for individuals. Caregivers use 'Caregiver: My Attendance'.", reply_markup=main_menu_keyboard())
            return
        ind_handle = ensure_self_individual_profile(handle, u[2] or handle)
        rows = list_bookings_for_individual(ind_handle)
        if not rows:
            await update.message.reply_text("No bookings yet.", reply_markup=main_menu_keyboard())
            return
        lines = ["Your bookings:"]
        for act_id, title, s, e, cg, cg_status in rows:
            cg_part = ""
            if cg:
                cg_part = f" | caregiver @{cg} ({cg_status})"
            lines.append(f"- #{act_id} {title} ({fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))}){cg_part}")
        await update.message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard())
        return

    if text == "❌ Cancel Booking":
        u = user_get(handle)
        if not u:
            await update.message.reply_text("Register first.", reply_markup=main_menu_keyboard())
            return
        if u[1] != "individual":
            await update.message.reply_text("Cancel is implemented for individuals only in this version.", reply_markup=main_menu_keyboard())
            return
        context.user_data["awaiting"] = "CANCEL_ACT_ID"
        await update.message.reply_text("Enter activity id to cancel (e.g., 1):", reply_markup=main_menu_keyboard())
        return

    if text == "👥 Caregiver: My Attendance":
        u = user_get(handle)
        if not u or u[1] != "caregiver":
            await update.message.reply_text("This is for caregiver accounts only.", reply_markup=main_menu_keyboard())
            return
        with_me, without_me = caregiver_view_attendance(handle)

        out = []
        out.append("Events you are attending with your individual (pending/confirmed):")
        if not with_me:
            out.append("- (none)")
        else:
            for name, ih, title, s, e, status, act_id in with_me:
                out.append(f"- #{act_id} {title} | {name} (@{ih}) | {fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))} | {status}")

        out.append("")
        out.append("Events your linked individuals are attending without you:")
        if not without_me:
            out.append("- (none)")
        else:
            for name, ih, title, s, e, act_id in without_me:
                out.append(f"- #{act_id} {title} | {name} (@{ih}) | {fmt_dt(int(s))}-{time.strftime('%H:%M', time.localtime(int(e)))}")

        await update.message.reply_text("\n".join(out), reply_markup=main_menu_keyboard())
        return

    if text == "🔐 Admin Login":
        context.user_data["awaiting"] = "ADMIN_PASSWORD"
        await update.message.reply_text("Enter admin password:")
        return

    if text == "🛠 Admin Panel":
        u = user_get(handle)
        if not u or u[1] != "admin":
            await update.message.reply_text("Not authorised. Tap Admin Login first.", reply_markup=main_menu_keyboard())
            return
        await update.message.reply_text("Admin Panel:", reply_markup=admin_panel_keyboard())
        return

    if text == "➕ Add Event":
        u = user_get(handle)
        if not u or u[1] != "admin":
            await update.message.reply_text("Not authorised.", reply_markup=main_menu_keyboard())
            return
        context.user_data["awaiting"] = "ADM_TITLE"
        context.user_data["tmp"] = {}
        await update.message.reply_text("Event title:", reply_markup=admin_panel_keyboard())
        return

    # registration role choice (reply buttons)
    if text in ("🙋 Individual", "🧑‍🦽 Caregiver"):
        if context.user_data.get("awaiting") != "REG_ROLE":
            await update.message.reply_text("Use Register to begin.", reply_markup=main_menu_keyboard())
            return

        role = "individual" if text.startswith("🙋") else "caregiver"
        context.user_data["tmp"] = {"role": role}
        context.user_data["awaiting"] = "REG_NAME"
        await update.message.reply_text("Type your full name (one time):", reply_markup=main_menu_keyboard())
        return

    await update.message.reply_text("Use /start and the menu buttons.", reply_markup=main_menu_keyboard())

async def handle_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    chat_id = update.effective_chat.id
    msg = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")
    tmp = context.user_data.get("tmp", {})

    # Admin password
    if awaiting == "ADMIN_PASSWORD":
        if msg == ADMIN_PASSWORD:
            existing = user_get(handle)
            if not existing:
                user_upsert(handle, "admin", handle, "", chat_id)
            else:
                user_set_role(handle, "admin")
                user_set_chat_id(handle, chat_id)
            context.user_data.clear()
            await update.message.reply_text("Admin access granted. Tap Admin Panel.", reply_markup=main_menu_keyboard())
        else:
            context.user_data.clear()
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

        user_upsert(handle, role, full_name, phone, chat_id)

        # Ensure individual profile for individuals (self)
        if role == "individual":
            ensure_self_individual_profile(handle, full_name)

        # NEW: caregiver registration asks for first individual + telehandle
        if role == "caregiver":
            context.user_data["awaiting"] = "CG_FIRST_NAME"
            context.user_data["tmp"] = {"full_name": full_name, "phone": phone, "role": role}
            await update.message.reply_text("Caregiver setup: What is the individual's name under your care?", reply_markup=main_menu_keyboard())
            return

        # done for individual
        context.user_data.clear()
        await update.message.reply_text("Registration complete.", reply_markup=main_menu_keyboard())
        return

    # Caregiver first individual name
    if awaiting == "CG_FIRST_NAME":
        tmp["ind_name"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "CG_FIRST_HANDLE"
        await update.message.reply_text("What is the individual's Telegram handle? (e.g., @john123 or john123)")
        return

    # Caregiver first individual handle
    if awaiting == "CG_FIRST_HANDLE":
        ind_handle = norm_handle(msg)
        ind_name = tmp.get("ind_name", "Individual")

        individual_profile_upsert(ind_handle, ind_name)
        caregiver_link_add(handle, ind_handle)

        context.user_data.clear()
        await update.message.reply_text(
            f"Caregiver registration complete.\nLinked individual: {ind_name} (@{ind_handle}).\n\nTo add more later: /add_individual",
            reply_markup=main_menu_keyboard(),
        )
        return

    # Cancel booking
    if awaiting == "CANCEL_ACT_ID":
        u = user_get(handle)
        if not u or u[1] != "individual":
            context.user_data.clear()
            await update.message.reply_text("Cancel is for individuals only.", reply_markup=main_menu_keyboard())
            return
        if not msg.isdigit():
            await update.message.reply_text("Enter a numeric activity id (e.g., 1).")
            return
        act_id = int(msg)
        ind_handle = ensure_self_individual_profile(handle, u[2] or handle)
        ok = cancel_booking(act_id, ind_handle)
        context.user_data.clear()
        await update.message.reply_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_keyboard())
        return

    # Admin add event wizard (details + timing + capacity)
    if awaiting == "ADM_TITLE":
        tmp["title"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADM_DESC"
        await update.message.reply_text("Description:")
        return

    if awaiting == "ADM_DESC":
        tmp["description"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADM_LOC"
        await update.message.reply_text("Location:")
        return

    if awaiting == "ADM_LOC":
        tmp["location"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADM_START"
        await update.message.reply_text("Start datetime (YYYY-MM-DD HH:MM):")
        return

    if awaiting == "ADM_START":
        ts = parse_local_dt(msg)
        if ts is None:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
            return
        tmp["start_ts"] = ts
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADM_END"
        await update.message.reply_text("End datetime (YYYY-MM-DD HH:MM):")
        return

    if awaiting == "ADM_END":
        ts = parse_local_dt(msg)
        if ts is None:
            await update.message.reply_text("Invalid format. Use YYYY-MM-DD HH:MM")
            return
        if ts <= int(tmp["start_ts"]):
            await update.message.reply_text("End must be after start. Enter end datetime again.")
            return
        tmp["end_ts"] = ts
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADM_CAP"
        await update.message.reply_text("Capacity (positive integer):")
        return

    if awaiting == "ADM_CAP":
        if not msg.isdigit() or int(msg) <= 0:
            await update.message.reply_text("Capacity must be a positive integer.")
            return
        cap = int(msg)
        act_id = admin_add_activity(
            tmp["title"],
            tmp.get("description", ""),
            tmp.get("location", ""),
            tmp["start_ts"],
            tmp["end_ts"],
            cap,
        )
        context.user_data.clear()
        await update.message.reply_text(f"Event created: #{act_id}", reply_markup=main_menu_keyboard())
        return

    # Individual booking flow: waiting caregiver handle
    if awaiting == "IND_CG_HANDLE":
        # tmp: {activity_id, individual_handle}
        cg_handle = norm_handle(msg)
        activity_id = int(tmp["activity_id"])
        individual_handle = tmp["individual_handle"]

        # Update booking with caregiver pending (booking already created without caregiver)
        with db() as conn:
            conn.execute("""
                UPDATE bookings
                SET caregiver_handle=?, caregiver_status='pending'
                WHERE activity_id=? AND individual_handle=?;
            """, (cg_handle, activity_id, norm_handle(individual_handle)))

        # Try to notify caregiver
        cg_user = user_get(cg_handle)
        if not cg_user or not cg_user[4]:
            context.user_data.clear()
            await update.message.reply_text(
                f"Saved caregiver @{cg_handle} as pending.\n"
                f"Note: I can only message the caregiver if they have started the bot at least once (/start).",
                reply_markup=main_menu_keyboard(),
            )
            return

        act = activity_get(activity_id)
        title = act[1] if act else f"Activity #{activity_id}"
        s = fmt_dt(act[4]) if act else ""
        e = time.strftime('%H:%M', time.localtime(int(act[5]))) if act else ""

        await context.bot.send_message(
            chat_id=cg_user[4],
            text=(
                f"Attendance confirmation request:\n"
                f"Individual @{individual_handle} booked: {title}\n"
                f"🕒 {s}-{e}\n\n"
                f"Will you attend with them?"
            ),
            reply_markup=caregiver_confirm_kb(activity_id, individual_handle),
        )

        context.user_data.clear()
        await update.message.reply_text("Caregiver notified (pending confirmation).", reply_markup=main_menu_keyboard())
        return

    # Caregiver add individual wizard
    if awaiting == "ADDIND_NAME":
        tmp["ind_name"] = msg
        context.user_data["tmp"] = tmp
        context.user_data["awaiting"] = "ADDIND_HANDLE"
        await update.message.reply_text("Individual's Telegram handle (e.g., @john123):")
        return

    if awaiting == "ADDIND_HANDLE":
        ind_handle = norm_handle(msg)
        ind_name = tmp.get("ind_name", "Individual")
        individual_profile_upsert(ind_handle, ind_name)
        caregiver_link_add(handle, ind_handle)
        context.user_data.clear()
        await update.message.reply_text(f"Added & linked: {ind_name} (@{ind_handle})", reply_markup=main_menu_keyboard())
        return

    context.user_data.clear()
    await update.message.reply_text("Flow reset. Use /start.", reply_markup=main_menu_keyboard())

async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    handle = get_handle(update)
    if not handle:
        await q.edit_message_text("Set a Telegram username first.")
        return

    data = q.data or ""
    parts = data.split("|")
    action = parts[0]

    # Details
    if action == "DETAILS":
        act_id = int(parts[1])
        act = activity_get(act_id)
        if not act:
            await q.edit_message_text("Activity not found.")
            return
        _, title, desc, loc, s, e, cap, booked = act
        text = (
            f"#{act_id} — {title}\n"
            f"🕒 {fmt_dt(int(s))}–{time.strftime('%H:%M', time.localtime(int(e)))}\n"
            f"📍 {loc or '-'}\n"
            f"📝 {desc or '-'}\n"
            f"👥 {booked}/{cap}"
        )
        await q.edit_message_text(text)
        return

    # Book (routes based on role)
    if action == "BOOK":
        act_id = int(parts[1])
        u = user_get(handle)
        if not u:
            await q.edit_message_text("Register first (tap Register).")
            return

        role = u[1]
        if role == "individual":
            ind_handle = ensure_self_individual_profile(handle, u[2] or handle)
            ok, msg = create_booking(act_id, ind_handle, handle, None, None)
            if not ok:
                await q.edit_message_text(msg)
                return

            # Ask if caregiver joining
            context.user_data["tmp"] = {"activity_id": act_id, "individual_handle": ind_handle}
            await q.edit_message_text("Will your caregiver be joining?", reply_markup=yesno_kb("INDCG"))
            return

        if role == "caregiver":
            people = caregiver_linked_individuals(handle)
            if not people:
                await q.edit_message_text("No linked individuals. Use /add_individual first.")
                return
            await q.edit_message_text(
                "Select individual to book for:",
                reply_markup=caregiver_pick_individual_kb(handle, act_id),
            )
            return

        await q.edit_message_text("Admins cannot book as users.")
        return

    # Individual caregiver-join answer
    if action == "INDCG":
        yn = parts[1]
        tmp = context.user_data.get("tmp", {})
        act_id = int(tmp.get("activity_id", -1))
        ind_handle = tmp.get("individual_handle")

        if yn == "NO":
            context.user_data.clear()
            await q.edit_message_text("Booked (no caregiver).")
            return

        # YES -> ask for caregiver handle in chat
        context.user_data["awaiting"] = "IND_CG_HANDLE"
        await q.edit_message_text("Type your caregiver’s Telegram handle (e.g., @caregiver123):")
        return

    # Caregiver book for chosen linked individual
    if action == "CGBOOK":
        act_id = int(parts[1])
        ind_handle = norm_handle(parts[2])

        # enforce linked
        linked = {h for h, _ in caregiver_linked_individuals(handle)}
        if ind_handle not in linked:
            await q.edit_message_text("You can only book for individuals linked to your caregiver account.")
            return

        ok, msg = create_booking(act_id, ind_handle, handle, None, None)
        await q.edit_message_text(msg)
        return

    # Caregiver confirms attendance request
    if action == "CGCONF":
        act_id = int(parts[1])
        ind_handle = norm_handle(parts[2])
        yn = parts[3]
        status = "confirmed" if yn == "YES" else "declined"

        # Update only if caregiver_handle matches current user
        update_caregiver_status(act_id, ind_handle, handle, status)

        await q.edit_message_text("Recorded: " + ("Confirmed ✅" if status == "confirmed" else "Declined ❌"))
        return

# ------------------------
# Commands
# ------------------------

async def add_individual_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Set a Telegram username first.")
        return
    u = user_get(handle)
    if not u or u[1] != "caregiver":
        await update.message.reply_text("This command is for caregivers only.", reply_markup=main_menu_keyboard())
        return
    context.user_data["awaiting"] = "ADDIND_NAME"
    context.user_data["tmp"] = {}
    await update.message.reply_text("New individual: what is their name?", reply_markup=main_menu_keyboard())

# ------------------------
# App
# ------------------------

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_individual", add_individual_cmd))
    app.add_handler(CallbackQueryHandler(inline_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
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
