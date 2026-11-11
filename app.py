import json, random, string, os, time, threading
from flask import Flask, request, redirect
import requests
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
BURN_TTL = int(os.getenv("BURN_TTL", "300"))  # 5 minutes default
AD_FILE = "fake_ad.json"

# ---- Service status (simulates AD / email server health) ----
# Set SERVICE_DOWN=True to simulate an outage; flip back to False to restore.
# When restored, the bot notifies any users who tried during the outage.
SERVICE_DOWN = False
NOTIFY_ON_RESTORE = []   # list of WhatsApp numbers to notify
_service_lock = threading.Lock()

def load_ad():
    with open(AD_FILE, 'r') as f:
        return json.load(f)

def save_ad(data):
    with open(AD_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def generate_password():
    return "Zim@" + ''.join(random.choices(string.ascii_letters + string.digits, k=6))

def send_whatsapp(to, text):
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print("send_whatsapp error:", e)

# Track conversation state
user_sessions = {}   # phone -> emp_id
burn_links = {}      # token -> {"password":..., "emp_id":..., "expires":..., "used":False}

def make_burn_link(emp_id, password):
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    burn_links[token] = {
        "emp_id": emp_id,
        "password": password,
        "expires": time.time() + BURN_TTL,
        "used": False,
    }
    return f"{BASE_URL}/burn/{token}"

def cleanup_burn_links():
    now = time.time()
    expired = [t for t, d in burn_links.items() if d["expires"] < now or d["used"]]
    for t in expired:
        del burn_links[t]

# ---- Webhook verification (Meta) ----
@app.route("/", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

# ---- Burn link endpoint: shows password once, then invalidates ----
@app.route("/burn/<token>", methods=["GET"])
def burn(token):
    cleanup_burn_links()
    data = burn_links.get(token)
    if not data:
        return ("<!DOCTYPE html><html><body style='font-family:Arial;text-align:center;padding:40px'>"
                "<h2>Link expired or already used 🔒</h2>"
                "<p>This secure link is no longer available for security reasons.</p>"
                "</body></html>"), 410
    if data["used"]:
        return ("<!DOCTYPE html><html><body style='font-family:Arial;text-align:center;padding:40px'>"
                "<h2>Link already used 🔒</h2>"
                "<p>This secure link has already been viewed. Request a new password reset.</p>"
                "</body></html>"), 410
    if time.time() > data["expires"]:
        del burn_links[token]
        return ("<!DOCTYPE html><html><body style='font-family:Arial;text-align:center;padding:40px'>"
                "<h2>Link expired 🔒</h2>"
                "<p>This secure link expired (5 minutes). Request a new password reset.</p>"
                "</body></html>"), 410
    # Mark as used immediately (one-time view)
    data["used"] = True
    emp_id = data["emp_id"]
    password = data["password"]
    # Try to fetch name
    try:
        ad = load_ad()
        name = ad.get(emp_id, {}).get("name", emp_id)
    except Exception:
        name = emp_id
    html = (f"<!DOCTYPE html><html><body style='font-family:Arial;text-align:center;padding:40px'>"
            f"<h2>Password Reset ✅</h2>"
            f"<p><b>Employee:</b> {emp_id} ({name})</p>"
            f"<p><b>New Password:</b> <code style='font-size:18px'>{password}</code></p>"
            f"<p style='color:#888'>This link has now expired. Please log in and change your password.</p>"
            f"</body></html>")
    return html, 200

# ---- Service restore notifier (background) ----
def notify_restore():
    global NOTIFY_ON_RESTORE
    with _service_lock:
        targets = list(NOTIFY_ON_RESTORE)
        NOTIFY_ON_RESTORE = []
    for num in targets:
        send_whatsapp(num, "Good news ✅ IT has restored AD/Email services. You can now retry your request. Type 'hie' to start.")
    print("Notified", len(targets), "users of service restore.")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "zimdesk_admin_123")

@app.route("/admin/status", methods=["GET"])
def admin_status():
    # ?secret=...&action=down|restore
    if request.args.get("secret") != ADMIN_SECRET:
        return "Unauthorized", 403
    action = request.args.get("action", "")
    global SERVICE_DOWN
    if action == "down":
        SERVICE_DOWN = True
        return "Service marked DOWN", 200
    if action == "restore":
        SERVICE_DOWN = False
        threading.Thread(target=notify_restore, daemon=True).start()
        return "Service restored, users notified", 200
    return f"SERVICE_DOWN={SERVICE_DOWN}", 200

# ---- Main webhook ----
@app.route("/", methods=["POST"])
def webhook():
    global SERVICE_DOWN, NOTIFY_ON_RESTORE
    data = request.get_json(silent=True) or {}
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            from_num = msg['from']
            text = msg['text']['body'].strip().lower()
            ad = load_ad()

            # Service down handling
            if SERVICE_DOWN:
                with _service_lock:
                    if from_num not in NOTIFY_ON_RESTORE:
                        NOTIFY_ON_RESTORE.append(from_num)
                send_whatsapp(from_num, "⚠️ Service Unavailable: AD/Email services are currently down. IT is working to restore them. You will be notified when services are back. Sorry for the inconvenience.")
                return "ok", 200

            # 1. GREETING
            if text in ["hie", "hi", "hello", "hey", "mhoro", "hie zimdeskai"]:
                send_whatsapp(from_num, "Hie, I am ZimDeskAI, how can I help you?\n\n1. Type your Employee Number e.g EMP001 to identify\n2. Type 'reset password'\n3. Type 'create account'\n4. Type 'my account'")
                return "ok", 200

            # 2. IDENTIFY BY EMP NUMBER
            if text.upper().startswith("EMP"):
                emp_id = text.upper().split()[0]
                if emp_id in ad:
                    user = ad[emp_id]
                    user_sessions[from_num] = emp_id
                    status = "Active" if user['active'] else "Disabled"
                    send_whatsapp(from_num, f"Account Found ✅\nName: {user['name']}\nID: {emp_id}\nDept: {user['department']}\nEmail: {user['email']}\nStatus: {status}\n\nWhat do you want to do? Type 'reset password'")
                else:
                    send_whatsapp(from_num, f"Employee {emp_id} not found. Type 'create account' to create it.")
                return "ok", 200

            # 3. MY ACCOUNT
            if "my account" in text or "who am i" in text:
                emp_id = user_sessions.get(from_num)
                if not emp_id:
                    send_whatsapp(from_num, "Please first send your Employee Number e.g EMP001")
                else:
                    u = ad[emp_id]
                    send_whatsapp(from_num, f"Your Record:\n{json.dumps(u, indent=2)}")
                return "ok", 200

            # 4. RESET PASSWORD (with burn link)
            if "reset" in text and "password" in text:
                emp_id = user_sessions.get(from_num)
                if not emp_id:
                    send_whatsapp(from_num, "First send your Employee Number e.g EMP001")
                    return "ok", 200
                new_pass = generate_password()
                ad[emp_id]['password'] = new_pass
                save_ad(ad)
                link = make_burn_link(emp_id, new_pass)
                send_whatsapp(from_num, f"Password Recreated Successfully 🔐\nFor {emp_id} ({ad[emp_id]['name']})\n\nView your new password (one-time, expires in 5 min):\n{link}\n\nPlease log in and change it.")
                return "ok", 200

            # 5. CREATE ACCOUNT
            if "create account" in text:
                user_sessions[from_num + "_creating"] = True
                send_whatsapp(from_num, "Let's create account. Send in this format:\nCREATE EMP006 | Full Name | email | Department\nExample: CREATE EMP006 | Blessing Banda | blessing@zimdesk.local | IT")
                return "ok", 200

            if text.upper().startswith("CREATE EMP"):
                try:
                    parts = text.split("|")
                    emp_id = parts[0].replace("create","").strip().upper()
                    name = parts[1].strip().title()
                    email = parts[2].strip()
                    dept = parts[3].strip()

                    if emp_id in ad:
                        send_whatsapp(from_num, f"{emp_id} already exists!")
                    else:
                        new_pass = generate_password()
                        ad[emp_id] = {"name": name, "email": email, "department": dept, "password": new_pass, "active": True}
                        save_ad(ad)
                        link = make_burn_link(emp_id, new_pass)
                        send_whatsapp(from_num, f"Account Created ✅\nID: {emp_id}\nName: {name}\nEmail: {email}\nDept: {dept}\n\nView your temp password (one-time, expires in 5 min):\n{link}")
                except Exception:
                    send_whatsapp(from_num, "Wrong format. Use: CREATE EMP006 | Full Name | email | Department")
                return "ok", 200

            # Default
            send_whatsapp(from_num, "I didn't understand. Type 'hie' to start.")

    except Exception as e:
        print("webhook error:", e)
    return "ok", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
