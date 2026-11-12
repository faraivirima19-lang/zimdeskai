import json, random, string, os, time, threading
from flask import Flask, request, redirect, render_template_string
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
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"send_whatsapp to={to} status={r.status_code} body={r.text[:500]}")
        if r.status_code != 200:
            print("send_whatsapp FAILED:", r.text)
    except Exception as e:
        print("send_whatsapp exception:", e)

# Track conversation state
user_sessions = {}   # user_id -> emp_id
burn_links = {}      # token -> {"password":..., "emp_id":..., "expires":..., "used":False}

def handle_message(user_id, text):
    """Core bot logic. Returns a list of reply strings. Used by both WhatsApp webhook and web chat."""
    replies = []
    text = text.strip().lower()
    ad = load_ad()

    # Admin: toggle service status via chat
    global SERVICE_DOWN, NOTIFY_ON_RESTORE
    if text == "service down":
        SERVICE_DOWN = True
        replies.append("⚠️ Service marked as DOWN. Type 'service restore' to bring it back.")
        return replies
    if text == "service restore":
        SERVICE_DOWN = False
        replies.append("✅ Service restored. Users notified.")
        return replies

    if SERVICE_DOWN:
        with _service_lock:
            if user_id not in NOTIFY_ON_RESTORE:
                NOTIFY_ON_RESTORE.append(user_id)
        replies.append("⚠️ Service Unavailable: AD/Email services are currently down. IT is working to restore them. You will be notified when services are back. Sorry for the inconvenience.")
        return replies

    # 1. GREETING
    if text in ["hie", "hi", "hello", "hey", "mhoro", "hie zimdeskai"]:
        replies.append("Hie, I am ZimDeskAI, how can I help you?\n\n1. Type your Employee Number e.g EMP001 to identify\n2. Type 'reset password'\n3. Type 'create account'\n4. Type 'my account'\n5. Type 'wifi' for WiFi credentials")
        return replies

    # 2. IDENTIFY BY EMP NUMBER
    if text.upper().startswith("EMP"):
        emp_id = text.upper().split()[0]
        if emp_id in ad:
            user = ad[emp_id]
            user_sessions[user_id] = emp_id
            status = "Active" if user['active'] else "Disabled"
            replies.append(f"Account Found ✅\nName: {user['name']}\nID: {emp_id}\nDept: {user['department']}\nEmail: {user['email']}\nStatus: {status}\n\nWhat do you want to do? Type 'reset password'")
        else:
            replies.append(f"Employee {emp_id} not found. Type 'create account' to create it.")
        return replies

    # 3. MY ACCOUNT
    if "my account" in text or "who am i" in text:
        emp_id = user_sessions.get(user_id)
        if not emp_id:
            replies.append("Please first send your Employee Number e.g EMP001")
        else:
            u = ad[emp_id]
            replies.append(f"Your Record:\n{json.dumps(u, indent=2)}")
        return replies

    # 4. RESET PASSWORD (with burn link)
    if "reset" in text and "password" in text:
        emp_id = user_sessions.get(user_id)
        if not emp_id:
            replies.append("First send your Employee Number e.g EMP001")
            return replies
        new_pass = generate_password()
        ad[emp_id]['password'] = new_pass
        save_ad(ad)
        link = make_burn_link(emp_id, new_pass)
        replies.append(f"Password Recreated Successfully 🔐\nFor {emp_id} ({ad[emp_id]['name']})\n\nView your new password (one-time, expires in 5 min):\n{link}\n\nPlease log in and change it.")
        return replies

    # WIFI CREDENTIALS
    if "wifi" in text:
        link = make_burn_link("WIFI", "13245678", link_type="wifi", extra={"network": "IT"})
        replies.append(f"WiFi Credentials 🔐\nNetwork: IT\n\nView your WiFi password (one-time, expires in 5 min):\n{link}")
        return replies

    # 5. CREATE ACCOUNT
    if "create account" in text:
        user_sessions[user_id + "_creating"] = True
        replies.append("Let's create account. Send in this format:\nCREATE EMP006 | Full Name | email | Department\nExample: CREATE EMP006 | Blessing Banda | blessing@zimdesk.local | IT")
        return replies

    if text.upper().startswith("CREATE EMP"):
        try:
            parts = text.split("|")
            emp_id = parts[0].replace("create","").strip().upper()
            name = parts[1].strip().title()
            email = parts[2].strip()
            dept = parts[3].strip()

            if emp_id in ad:
                replies.append(f"{emp_id} already exists!")
            else:
                new_pass = generate_password()
                ad[emp_id] = {"name": name, "email": email, "department": dept, "password": new_pass, "active": True}
                save_ad(ad)
                link = make_burn_link(emp_id, new_pass)
                replies.append(f"Account Created ✅\nID: {emp_id}\nName: {name}\nEmail: {email}\nDept: {dept}\n\nView your temp password (one-time, expires in 5 min):\n{link}")
        except Exception:
            replies.append("Wrong format. Use: CREATE EMP006 | Full Name | email | Department")
        return replies

    # Default
    replies.append("I didn't understand. Type 'hie' to start.")
    return replies

def make_burn_link(emp_id, password, link_type="password", extra=None):
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    burn_links[token] = {
        "emp_id": emp_id,
        "password": password,
        "expires": time.time() + BURN_TTL,
        "used": False,
        "type": link_type,
        "extra": extra or {},
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
    link_type = data.get("type", "password")
    emp_id = data["emp_id"]
    password = data["password"]
    extra = data.get("extra", {})

    if link_type == "wifi":
        network = extra.get("network", "IT")
        html = (f"<!DOCTYPE html><html><body style='font-family:Arial;text-align:center;padding:40px'>"
                f"<h2>WiFi Credentials ✅</h2>"
                f"<p><b>Network Name:</b> <code style='font-size:18px'>{network}</code></p>"
                f"<p><b>Password:</b> <code style='font-size:18px'>{password}</code></p>"
                f"<p style='color:#888'>This link has now expired. Keep your WiFi password safe.</p>"
                f"</body></html>")
        return html, 200

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

# ---- Main webhook (WhatsApp) ----
@app.route("/", methods=["POST"])
def webhook():
    global SERVICE_DOWN, NOTIFY_ON_RESTORE
    data = request.get_json(silent=True) or {}
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            from_num = msg['from']
            text = msg['text']['body']
            replies = handle_message(from_num, text)
            for r in replies:
                send_whatsapp(from_num, r)
    except Exception as e:
        print("webhook error:", e)
    return "ok", 200

# ---- Web Chat API (for browser testing) ----
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", "web_user")
    text = data.get("message", "")
    replies = handle_message(user_id, text)
    # Convert burn links to full URLs
    full_replies = []
    for r in replies:
        full_replies.append(r)
    return {"replies": full_replies}, 200

# ---- Web Chat UI ----
@app.route("/test", methods=["GET"])
def test_ui():
    return render_template_string(CHAT_HTML)

CHAT_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZimDesk AI - Web Chat Test</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #e5ddd5; display: flex; justify-content: center; align-items: center; height: 100vh; }
  .chat-container { width: 420px; max-width: 100%; height: 90vh; background: #fff; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); display: flex; flex-direction: column; overflow: hidden; }
  .chat-header { background: #075e54; color: #fff; padding: 16px; display: flex; align-items: center; gap: 12px; }
  .chat-header .avatar { width: 40px; height: 40px; border-radius: 50%; background: #25d366; display: flex; align-items: center; justify-content: center; font-size: 20px; }
  .chat-header .info h3 { font-size: 16px; }
  .chat-header .info p { font-size: 12px; opacity: 0.8; }
  .chat-messages { flex: 1; padding: 16px; overflow-y: auto; background: #e5ddd5; display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 8px; font-size: 14px; line-height: 1.4; white-space: pre-wrap; word-wrap: break-word; }
  .msg.user { align-self: flex-end; background: #dcf8c6; border-bottom-right-radius: 2px; }
  .msg.bot { align-self: flex-start; background: #fff; border-bottom-left-radius: 2px; box-shadow: 0 1px 1px rgba(0,0,0,0.1); }
  .msg a { color: #075e54; }
  .chat-input { display: flex; padding: 10px; background: #f0f0f0; gap: 8px; }
  .chat-input input { flex: 1; padding: 10px 14px; border: none; border-radius: 20px; font-size: 14px; outline: none; }
  .chat-input button { background: #25d366; color: #fff; border: none; border-radius: 50%; width: 40px; height: 40px; cursor: pointer; font-size: 18px; display: flex; align-items: center; justify-content: center; }
  .chat-input button:hover { background: #128c7e; }
  .typing { align-self: flex-start; background: #fff; padding: 10px 14px; border-radius: 8px; font-size: 12px; color: #888; font-style: italic; }
  .burn-link { display: inline-block; margin-top: 6px; padding: 6px 12px; background: #075e54; color: #fff; border-radius: 6px; text-decoration: none; font-size: 13px; }
  .burn-link:hover { background: #128c7e; }
  .quick-btns { display: none; }
</style>
</head>
<body>
<div class="chat-container">
  <div class="chat-header">
    <div class="avatar">🤖</div>
    <div class="info">
      <h3>ZimDesk AI</h3>
      <p>Web Test Interface (same logic as WhatsApp bot)</p>
    </div>
  </div>
  <div class="chat-messages" id="messages"></div>

  <div class="chat-input">
    <input type="text" id="input" placeholder="Type a message..." onkeypress="if(event.key==='Enter')sendMessage()">
    <button onclick="sendMessage()">➤</button>
  </div>
</div>
<script>
  const userId = "web_user_" + Math.random().toString(36).substr(2, 9);
  const messagesDiv = document.getElementById('messages');
  const input = document.getElementById('input');

  function addMsg(text, isUser) {
    const div = document.createElement('div');
    div.className = 'msg ' + (isUser ? 'user' : 'bot');
    // Convert URLs to clickable links
    let html = text.replace(/(https?:\/\/[^\s]+)/g, '<a href="$1" target="_blank" class="burn-link">Open Link</a>');
    div.innerHTML = html;
    messagesDiv.appendChild(div);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
  }

  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    addMsg(text, true);
    input.value = '';
    const typing = document.createElement('div');
    typing.className = 'typing';
    typing.textContent = 'ZimDeskAI is typing...';
    messagesDiv.appendChild(typing);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;

    try {
      const res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, message: text })
      });
      const data = await res.json();
      typing.remove();
      for (const reply of data.replies) {
        addMsg(reply, false);
      }
    } catch (e) {
      typing.remove();
      addMsg('Error: ' + e.message, false);
    }
  }

  // Clean start - no auto-message, user types to begin
</script>
</body>
</html>'''

# ---- Service restore notifier (background) ----
def notify_restore():
    global NOTIFY_ON_RESTORE
    with _service_lock:
        targets = list(NOTIFY_ON_RESTORE)
        NOTIFY_ON_RESTORE = []
    for num in targets:
        # For web chat, we can't push messages; just log
        print(f"Would notify {num} of service restore")
    print("Notified", len(targets), "users of service restore.")

@app.route("/service/restore", methods=["POST"])
def restore_service():
    global SERVICE_DOWN
    SERVICE_DOWN = False
    threading.Thread(target=notify_restore, daemon=True).start()
    return "Service restored", 200

@app.route("/service/down", methods=["POST"])
def set_down():
    global SERVICE_DOWN
    SERVICE_DOWN = True
    return "Service marked down", 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
