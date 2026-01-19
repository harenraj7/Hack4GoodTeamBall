# MINDS Booking System (Telegram Bot) — Team Ball H4G
Problem statement 3 - MINDS Problem Statement
A Telegram bot prototype to reduce friction in activity sign-ups for individuals and caregivers, while reducing manual admin work (events + attendance lists).

---

## Features

### Individual
- Register profile (Telegram handle used as primary key)
- Browse activities **step-by-step**:
  1) View list of activity names (soonest first)  
  2) Tap an activity to view details  
  3) Tap **Book**
- Booking conflict detection (prevents overlapping bookings)
- Optional caregiver attendance request:
  - If the individual indicates caregiver is joining, bot asks for caregiver handle
  - Bot sends caregiver a confirm/decline prompt (caregiver must have `/start`ed the bot before to receive messages)
- View bookings
- Cancel booking (by activity ID)

### Caregiver
- Register caregiver profile
- Link individuals under care (during signup + `/add_individual`)
- Book activities for linked individuals
  - **Caregiver is automatically included as attending (confirmed)** when they book
- View:
  - Events caregiver is attending with individuals
  - Events linked individuals are attending without caregiver

### Admin
- Password-protected **Admin Login**
- Create events (title, description, location, start/end time, capacity)
- View upcoming events by month (button-based)
- Generate **attendance list** for any event (by activity ID)

---

## Tech Stack
- Python
- `python-telegram-bot`
- SQLite (`bot.db` generated locally)

---

## Requirements
- Python 3.10+ recommended (works on 3.12+; avoid very old versions)
- A Telegram account with a **username** (Telegram → Settings → Username)
- A bot token created via **@BotFather**

---

## Repo Structure
- `main.py` — bot code
- `requirements.txt` — Python dependencies
- `.env.example` — template for environment variables
- `.gitignore` — ignores `.env`, `.venv`, `bot.db`, etc.

---

## Setup (Local)

### 1) Run the following in terminal
```bash
git clone https://github.com/harenraj7/Hack4GoodTeamBall.git
cd Hack4GoodTeamBall

### 2) 
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

### 3) [Replace 'PASTE_TOKEN_HERE' with bot token created via **@BotFather**]
export BOT_TOKEN='PASTE_TOKEN_HERE' 
python main.py



