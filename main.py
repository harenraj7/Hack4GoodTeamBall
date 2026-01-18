import os
import sqlite3
import time
from typing import Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

DB_PATH = "bot.db"

# Registration conversation states (command-based fallback)
REG_ROLE, REG_NAME, REG_PHONE = range(3)


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


def is_organizer(handle: str) -> bool:
    raw = os.environ.get("ORGANIZER_HANDLES", "")
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return handle.lower() in allowed


def fmt_activity_row(row: Tuple) -> str:
    # (id, title, start_ts, end_ts, capacity, booked_count)
    act_id, title, start_ts, end_ts, capacity, booked = row
    start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(start_ts)))
    end_str = time.strftime("%H:%M", time.localtime(int(end_ts)))
    return f"#{act_id} — {title}\n🕒 {start_str}–{end_str}\n👥 {booked}/{capacity}"


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
    """Hackathon helper: creates demo activities if none exist."""
    with db() as conn:
        (cnt,) = conn.execute("SELECT COUNT(*) FROM activities;").fetchone()
        if cnt > 0:
            return
        base = now_ts() + 3600  # 1 hour from now
        demo = [
            ("Music Therapy", base, base + 3600, 10),
            ("Art Jam", base + 5400, base + 7200, 8),          # no overlap with first
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


def caregiver_add_individual(caregiver_handle: str, name: str, nric_last4: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,?);",
            (caregiver_handle, name, nric_last4 or None),
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
            (handle,),
        ).fetchone()
        if row:
            return int(row[0])

        cur = conn.execute(
            "INSERT INTO individuals(caregiver_handle,name,nric_last4) VALUES (?,?,NULL);",
            (handle, full_name or handle),
        )
        return int(cur.lastrowid)


def caregiver_list_individuals(caregiver_handle: str) -> List[Tuple[int, str]]:
    with db() as conn:
        cur = conn.execute(
            "SELECT id, name FROM individuals WHERE caregiver_handle=? ORDER BY id;",
            (caregiver_handle,),
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
        return conn.execute(
            "SELECT 1 FROM activities WHERE id=?;",
            (activity_id,),
        ).fetchone() is not None


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
    """Overlap test: existing.start < new.end AND new.start < existing.end."""
    with db() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts, title FROM activities WHERE id=?;",
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


# -----------------------
# Button UI (Inline Keyboards)
# -----------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Register / Update Profile", callback_data="MENU_REGISTER")],
        [InlineKeyboardButton("📅 Activities (Book)", callback_data="MENU_ACTIVITIES")],
        [InlineKeyboardButton("✅ My Bookings", callback_data="MENU_MY")],
        [InlineKeyboardButton("❌ Cancel Booking", callback_data="MENU_CANCEL")],
        [InlineKeyboardButton("📋 Attendees (Organiser)", callback_data="MENU_ATTENDEES")],
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
    # rows: (act_id, title, start_ts, end_ts)
    btns: List[List[InlineKeyboardButton]] = []
    for act_id, title, *_ in rows:
        btns.append([InlineKeyboardButton(f"Cancel: #{act_id} {title}", callback_data=f"CAN_{act_id}")])
    btns.append([InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")])
    return InlineKeyboardMarkup(btns)


def register_role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 Individual", callback_data="REGROLE_individual")],
        [InlineKeyboardButton("🧑‍🦽 Caregiver", callback_data="REGROLE_caregiver")],
        [InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")],
    ])


# -----------------------
# Core Handlers
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
            "Welcome. Please register using the menu below.",
            reply_markup=main_menu_kb(),
        )
        return

    _, role, full_name, phone = u
    await update.message.reply_text(
        f"Logged in as @{handle}\nRole: {role}\nName: {full_name or '-'}\nPhone: {phone or '-'}\n\nMenu:",
        reply_markup=main_menu_kb(),
    )


# -----------------------
# Callback (buttons)
# -----------------------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    handle = get_handle(update)
    data = q.data or ""

    if data in ("MENU_HOME", "MENU_HOME_REFRESH"):
        await q.edit_message_text("Menu:", reply_markup=main_menu_kb())
        return

    if data == "MENU_REGISTER":
        if not handle:
            await q.edit_message_text("Please set a Telegram username first.", reply_markup=main_menu_kb())
            return
        await q.edit_message_text("Choose your role:", reply_markup=register_role_kb())
        return

    if data.startswith("REGROLE_"):
        if not handle:
            await q.edit_message_text("Please set a Telegram username first.", reply_markup=main_menu_kb())
            return
        role = data.split("_", 1)[1]
        context.user_data["reg_role"] = role
        context.user_data["reg_waiting_name"] = True
        context.user_data["reg_waiting_phone"] = False
        await q.edit_message_text("Type your full name (one time):")
        return

    if data == "MENU_ACTIVITIES":
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return
        acts = list_activities()
        if not acts:
            await q.edit_message_text("No activities available.", reply_markup=main_menu_kb())
            return
        text = "Available activities:\n\n" + "\n\n".join(fmt_activity_row(a) for a in acts)
        await q.edit_message_text(text, reply_markup=activities_kb(acts))
        return

    if data.startswith("ACT_"):
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return

        activity_id = int(data.split("_", 1)[1])
        u = user_get(handle)
        _, role, *_ = u

        if role == "individual":
            individual_id = individual_get_for_handle(handle)
            ok, msg = create_booking(activity_id, individual_id, handle)
            await q.edit_message_text(msg, reply_markup=main_menu_kb())
            return

        # caregiver: pick who to book for
        people = caregiver_list_individuals(handle)
        if not people:
            await q.edit_message_text(
                "No individuals added yet.\nUse /addperson <name> to add one (command fallback).",
                reply_markup=main_menu_kb(),
            )
            return

        await q.edit_message_text(
            "Select who you want to book for:",
            reply_markup=caregiver_people_kb(people, activity_id),
        )
        return

    if data.startswith("PICK_"):
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return

        u = user_get(handle)
        _, role, *_ = u
        if role != "caregiver":
            await q.edit_message_text("Only caregivers can do this.", reply_markup=main_menu_kb())
            return

        _, act_id_str, pid_str = data.split("_")
        activity_id = int(act_id_str)
        person_id = int(pid_str)

        owned = {pid for pid, _ in caregiver_list_individuals(handle)}
        if person_id not in owned:
            await q.edit_message_text("That person does not belong to you.", reply_markup=main_menu_kb())
            return

        ok, msg = create_booking(activity_id, person_id, handle)
        await q.edit_message_text(msg, reply_markup=main_menu_kb())
        return

    if data == "MENU_MY":
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return

        _, role, *_ = user_get(handle)

        if role == "individual":
            individual_id = individual_get_for_handle(handle)
            rows = list_bookings_for_individual(individual_id)
            if not rows:
                await q.edit_message_text("No bookings yet.", reply_markup=main_menu_kb())
                return
            text = "Your bookings:\n" + "\n".join([f"- #{act_id} {title}" for act_id, title, *_ in rows])
            await q.edit_message_text(text, reply_markup=main_menu_kb())
            return

        # caregiver: show summary (buttons for caregiver view can be added later)
        people = caregiver_list_individuals(handle)
        if not people:
            await q.edit_message_text("No individuals added yet. Use /addperson <name>.", reply_markup=main_menu_kb())
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
        await q.edit_message_text("\n".join(lines), reply_markup=main_menu_kb())
        return

    if data == "MENU_CANCEL":
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return

        _, role, *_ = user_get(handle)
        if role != "individual":
            await q.edit_message_text(
                "Cancel buttons currently support individuals only.\n"
                "Caregivers can cancel via /cancelfor <activity_id> <person_id> (command fallback).",
                reply_markup=main_menu_kb(),
            )
            return

        individual_id = individual_get_for_handle(handle)
        rows = list_bookings_for_individual(individual_id)
        if not rows:
            await q.edit_message_text("No bookings to cancel.", reply_markup=main_menu_kb())
            return

        await q.edit_message_text("Tap a booking to cancel:", reply_markup=cancel_kb(rows))
        return

    if data.startswith("CAN_"):
        if not handle or not user_get(handle):
            await q.edit_message_text("Please register first.", reply_markup=main_menu_kb())
            return

        activity_id = int(data.split("_", 1)[1])
        individual_id = individual_get_for_handle(handle)
        ok = cancel_booking(activity_id, individual_id)
        await q.edit_message_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_kb())
        return

    if data == "MENU_ATTENDEES":
        if not handle or not is_organizer(handle):
            await q.edit_message_text("Not authorised.", reply_markup=main_menu_kb())
            return

        acts = list_activities()
        if not acts:
            await q.edit_message_text("No activities available.", reply_markup=main_menu_kb())
            return

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"Attendees: #{a[0]} {a[1]}", callback_data=f"ATT_{a[0]}")] for a in acts] +
            [[InlineKeyboardButton("⬅️ Back", callback_data="MENU_HOME")]]
        )
        await q.edit_message_text("Choose an activity:", reply_markup=kb)
        return

    if data.startswith("ATT_"):
        if not handle or not is_organizer(handle):
            await q.edit_message_text("Not authorised.", reply_markup=main_menu_kb())
            return

        activity_id = int(data.split("_", 1)[1])
        rows = attendee_list(activity_id)
        if not rows:
            await q.edit_message_text("No attendees yet.", reply_markup=main_menu_kb())
            return

        lines = ["name,individual_id,booked_by,created_time"]
        for name, pid, booked_by, created_ts in rows:
            t_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created_ts)))
            lines.append(f"{name},{pid},@{booked_by},{t_str}")

        await q.edit_message_text("Attendee list:\n\n" + "\n".join(lines), reply_markup=main_menu_kb())
        return

    # Fallback
    await q.edit_message_text("Menu:", reply_markup=main_menu_kb())


# -----------------------
# Button-based registration text capture
# -----------------------

async def reg_text_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Captures name and phone after user selects role via buttons.
    This handler runs on any non-command text.
    """
    handle = get_handle(update)
    if not handle:
        await update.message.reply_text("Please set a Telegram username first (Settings → Username).")
        return

    if context.user_data.get("reg_waiting_name"):
        context.user_data["full_name"] = (update.message.text or "").strip()
        context.user_data["reg_waiting_name"] = False
        context.user_data["reg_waiting_phone"] = True
        await update.message.reply_text("Type your phone number (or '-' to skip):")
        return

    if context.user_data.get("reg_waiting_phone"):
        phone = (update.message.text or "").strip()
        if phone == "-":
            phone = ""
        role = context.user_data.get("reg_role", "individual")
        full_name = context.user_data.get("full_name", "")

        user_upsert(handle, role, full_name, phone)
        if role == "individual":
            individual_get_for_handle(handle)

        context.user_data["reg_waiting_phone"] = False
        await update.message.reply_text("Registration complete.", reply_markup=main_menu_kb())
        return


# -----------------------
# Command handlers (fallback)
# -----------------------

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
    if role == "individual":
        individual_get_for_handle(handle)

    await update.message.reply_text("Registration complete.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Registration cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def activities_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle or not user_get(handle):
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    acts = list_activities()
    if not acts:
        await update.message.reply_text("No activities available.", reply_markup=main_menu_kb())
        return

    lines = ["Available activities:\n"]
    for row in acts:
        lines.append(fmt_activity_row(row))
        lines.append("")
    lines.append("Use buttons (recommended) or: /book <activity_id>")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())


async def addperson_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return
    _, role, *_ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can add individuals.", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Usage: /addperson <name>", reply_markup=main_menu_kb())
        return

    name = parts[1].strip()
    pid = caregiver_add_individual(handle, name)
    await update.message.reply_text(f"Added: {name} (person_id={pid}).", reply_markup=main_menu_kb())


async def book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /book <activity_id>", reply_markup=main_menu_kb())
        return

    activity_id = int(parts[1])
    if not activity_exists(activity_id):
        await update.message.reply_text("Activity not found. Use /activities.", reply_markup=main_menu_kb())
        return

    _, role, *_ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        ok, msg = create_booking(activity_id, individual_id, handle)
        await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return

    # caregiver flow (command fallback): prompt to use buttons or /bookfor
    people = caregiver_list_individuals(handle)
    if not people:
        await update.message.reply_text("No individuals yet. Add one with /addperson <name>.", reply_markup=main_menu_kb())
        return

    await update.message.reply_text(
        "Caregiver booking:\nUse buttons (recommended) or: /bookfor <activity_id> <person_id>\n\n"
        "Your individuals:\n" + "\n".join([f"- {pid}: {name}" for pid, name in people]),
        reply_markup=main_menu_kb(),
    )


async def bookfor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    _, role, *_ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can use /bookfor.", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await update.message.reply_text("Usage: /bookfor <activity_id> <person_id>", reply_markup=main_menu_kb())
        return

    activity_id = int(parts[1])
    person_id = int(parts[2])

    owned = {pid for pid, _ in caregiver_list_individuals(handle)}
    if person_id not in owned:
        await update.message.reply_text("That person_id does not belong to you.", reply_markup=main_menu_kb())
        return

    ok, msg = create_booking(activity_id, person_id, handle)
    await update.message.reply_text(msg, reply_markup=main_menu_kb())


async def my_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    _, role, *_ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        rows = list_bookings_for_individual(individual_id)
        if not rows:
            await update.message.reply_text("No bookings yet.", reply_markup=main_menu_kb())
            return
        lines = ["Your bookings:\n"]
        for act_id, title, s, e in rows:
            s_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(s)))
            e_str = time.strftime("%H:%M", time.localtime(int(e)))
            lines.append(f"- #{act_id} {title} ({s_str}-{e_str})")
        await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())
        return

    # caregiver view
    people = caregiver_list_individuals(handle)
    if not people:
        await update.message.reply_text("No individuals added yet. Use /addperson <name>.", reply_markup=main_menu_kb())
        return

    lines = ["Caregiver view:\n"]
    for pid, name in people:
        rows = list_bookings_for_individual(pid)
        lines.append(f"{name} (person_id={pid}):")
        if not rows:
            lines.append("  - (none)")
        else:
            for act_id, title, *_ in rows:
                lines.append(f"  - #{act_id} {title}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /cancel <activity_id>", reply_markup=main_menu_kb())
        return

    activity_id = int(parts[1])
    _, role, *_ = u

    if role == "individual":
        individual_id = individual_get_for_handle(handle)
        ok = cancel_booking(activity_id, individual_id)
        await update.message.reply_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_kb())
        return

    await update.message.reply_text(
        "Caregiver cancel:\nUse /cancelfor <activity_id> <person_id> (or buttons for individual cancel).",
        reply_markup=main_menu_kb(),
    )


async def cancelfor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    u = user_get(handle) if handle else None
    if not u:
        await update.message.reply_text("Please register first: /register", reply_markup=main_menu_kb())
        return

    _, role, *_ = u
    if role != "caregiver":
        await update.message.reply_text("Only caregivers can use /cancelfor.", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await update.message.reply_text("Usage: /cancelfor <activity_id> <person_id>", reply_markup=main_menu_kb())
        return

    activity_id = int(parts[1])
    person_id = int(parts[2])

    owned = {pid for pid, _ in caregiver_list_individuals(handle)}
    if person_id not in owned:
        await update.message.reply_text("That person_id does not belong to you.", reply_markup=main_menu_kb())
        return

    ok = cancel_booking(activity_id, person_id)
    await update.message.reply_text("Cancelled." if ok else "No such booking.", reply_markup=main_menu_kb())


async def attendees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    handle = get_handle(update)
    if not handle or not is_organizer(handle):
        await update.message.reply_text("Not authorised.", reply_markup=main_menu_kb())
        return

    parts = (update.message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /attendees <activity_id>", reply_markup=main_menu_kb())
        return

    activity_id = int(parts[1])
    rows = attendee_list(activity_id)
    if not rows:
        await update.message.reply_text("No attendees yet.", reply_markup=main_menu_kb())
        return

    lines = ["name,individual_id,booked_by,created_time"]
    for name, pid, booked_by, created_ts in rows:
        t_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(created_ts)))
        lines.append(f"{name},{pid},@{booked_by},{t_str}")

    await update.message.reply_text("Attendee list:\n\n" + "\n".join(lines), reply_markup=main_menu_kb())


# -----------------------
# App wiring
# -----------------------

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    # Command-based registration conversation (fallback)
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

    # Button callback handler (must be registered)
    app.add_handler(CallbackQueryHandler(menu_callback))

    # Text capture for button-based registration (non-command text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reg_text_capture))

    # Command fallbacks
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
