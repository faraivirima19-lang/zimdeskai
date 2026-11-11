# ZimDesk AI — WhatsApp Bot (Prototype)

A WhatsApp chatbot for IT helpdesk tasks (password reset, account creation) using a **fake Active Directory** (JSON file) for demo purposes.

## Features
- `hie` → greeting + menu
- Identify user by employee number (EMP001..)
- Reset password → one-time burn link (expires in 5 min)
- Create new account → temp password via burn link
- My account lookup
- Service-down handling + auto-notify users when restored

## Run locally
```
pip install -r requirements.txt
python app.py
```
Then expose with ngrok: `ngrok http 5000` and set the ngrok URL as the Meta Callback URL.

## Deploy to Render
Push to GitHub, connect to Render (uses `render.yaml`). Set env vars in Render dashboard:
`WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `BASE_URL`, `VERIFY_TOKEN`, `ADMIN_SECRET`.

## Test endpoints
- `GET /` — Meta webhook verification
- `POST /` — Meta webhook
- `GET /burn/<token>` — one-time password view
- `GET /admin/status?secret=...&action=down|restore` — toggle service status
