# MINDS Booking System (Telegram Bot) — Hack4Good

Telegram bot to reduce friction in activity sign-ups for individuals and caregivers, while reducing admin effort to manage registrations.

## Features
### User (Individual / Caregiver)
- Register as **Individual** or **Caregiver** (Telegram handle used as primary key)
- View activities (step-by-step): pick activity name → view details → book
- Booking conflict detection (prevents overlapping schedules)
- Individual booking can request caregiver attendance confirmation
- Caregiver can link multiple individuals under their care and book for them
- View bookings / attendance

### Admin
- Admin login (password-protected) [Password is admin_password]
- Create events (title/description/location/time/capacity)
- View upcoming events by month

---

## Requirements
- Python 3.10+ recommended
- Telegram account with a **username** (Settings → Username)
- A Telegram bot token from **@BotFather** [Token is 8474153804:AAG3Tq3ViHmNr-ftoI3jra_hljsUxHVq3hk]

---

## Setup (Local)

### 1) Clone the repo
```bash
git clone https://github.com/<YOUR_USERNAME>/<YOUR_REPO>.git
cd <YOUR_REPO>
