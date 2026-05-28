# Promo Code System

Simple standalone web app to manually generate and verify one-time promo codes.

## Business rules configured

- Minimum purchase amount: `2500`
- Discount: `15%`
- Expiry: `20 days` from purchase date
- Promo code use: one-time only
- Verification: manual via website
- Staff login: required
- Audit logs: enabled
- Search by code/phone: enabled
- Auto send promo code: enabled with Twilio configuration
- Reminder messages: enabled with manual "Send Due Reminders" action

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Create a real `.env` file by copying `.env.example`, then update values in `.env`.
4. Run:
   - `python app.py`
5. Open:
   - `http://localhost:5000`

## Messaging setup (auto-send + reminders)

Set these in your environment:

- `PROMO_MESSAGE_FROM` (Twilio number, e.g. `+1...` or WhatsApp sender like `whatsapp:+1...`)
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `PROMO_REMINDER_DAYS_BEFORE_EXPIRY` (default `3`)

If messaging is not configured, promo generation still works; message send attempts are shown as failed in UI/audit logs.

## Why `.env.example` did not change login

`.env.example` is only a template. The app reads values from `.env` (or system environment variables).  
After changing `.env`, restart the server (`python app.py`) so new values are loaded.

## Default login (change in production)

- Username: `admin`
- Password: `admin123`

Set `PROMO_ADMIN_USER` and `PROMO_ADMIN_PASSWORD` to secure values before hosting.

## Hosting

For simple hosting, you can deploy this app on services like Render, Railway, or any VPS that supports Python.
