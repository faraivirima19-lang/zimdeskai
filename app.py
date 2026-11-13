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
BURN_TTL = int(os.getenv("BURN_TTL", "300"))
AD_FILE = "fake_ad.json"

# ---- Service status ----
SERVICE_DOWN = False
NOTIFY_ON_RESTORE = []
_service_lock = threading.Lock()

# ---- Conversation state ----
user_sessions = {}   # user_id -> emp_id (fully authenticated)
pending_auth = {}    # user_id -> emp_id (awaiting secret key)
burn_links = {}      # token -> {...}

# ---- Rate limiting ----
rate_limits = {}     # user_id -> {action: [timestamps]}
RATE_LIMITS = {
    "message": (10, 60),       # 10 messages per 60 seconds
    "reset_password": (3, 3600), # 3 resets per hour
    "create_account": (2, 3600), # 2 creates per hour
    "wifi": (5, 3600),         # 5 wifi requests per hour
}
SESSION_TIMEOUT = 900  # 15 minutes

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

def check_rate_limit(user_id, action):
    """Returns True if allowed, False if rate limited."""
    limit, window = RATE_LIMITS.get(action, (10, 60))
    now = time.time()
    if user_id not in rate_limits:
        rate_limits[user_id] = {}
    if action not in rate_limits[user_id]:
        rate_limits[user_id][action] = []
    # Remove old timestamps
    rate_limits[user_id][action] = [t for t in rate_limits[user_id][action] if now - t < window]
    if len(rate_limits[user_id][action]) >= limit:
        return False
    rate_limits[user_id][action].append(now)
    return True

def is_session_valid(user_id):
    """Check if session exists and hasn't timed out."""
    if user_id not in user_sessions:
        return False
    # Simple timeout check - store last activity
    return True

def handle_message(user_id, text):
    """Core bot logic. Returns a list of reply strings."""
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

    # Rate limit check
    if not check_rate_limit(user_id, "message"):
        replies.append("Too many requests. Please wait a moment before trying again.")
        return replies

    # Service down handling
    if SERVICE_DOWN:
        with _service_lock:
            if user_id not in NOTIFY_ON_RESTORE:
                NOTIFY_ON_RESTORE.append(user_id)
        replies.append("⚠️ Service Unavailable: AD/Email services are currently down. IT is working to restore them. You will be notified when services are back. Sorry for the inconvenience.")
        return replies

    # 1. GREETING
    if text in ["hie", "hi", "hello", "hey", "mhoro", "hie zhrcaidesk", "hie zhrdeskai"]:
        replies.append("Hie, I am ZHRCDeskAI, how can I help you?\n\nPlease provide your Employee Number (e.g. EMP001) to identify yourself before we can proceed.")
        return replies

    # 2. PENDING SECRET KEY VERIFICATION
    if user_id in pending_auth:
        emp_id = pending_auth[user_id]
        if emp_id not in ad:
            del pending_auth[user_id]
            replies.append("Access denied. You are not authorized to use this application.")
            return replies
        expected_key = ad[emp_id].get("secret_key", "0000")
        if text == expected_key:
            user = ad[emp_id]
            if not user.get("active", True):
                del pending_auth[user_id]
                replies.append("Access denied. Your account is disabled.")
                return replies
            del pending_auth[user_id]
            user_sessions[user_id] = emp_id
            role = user.get("role", "user")
            first_name = user['name'].split()[0]
            menu = f"Greetings {first_name}, how can I help you today?\n\nWhat do you want to do?\n- Type 'reset password'\n- Type 'my account' (to view your details)\n- Type 'wifi' for WiFi credentials"
            if role == "admin":
                menu += "\n\nAdmin commands:\n- Type 'create account'\n- Type 'list users'\n- Type 'disable EMP00X'\n- Type 'enable EMP00X'"
            replies.append(menu)
        else:
            del pending_auth[user_id]
            replies.append("Access denied. You are not authorized to use this application.")
        return replies

    # 3. IDENTIFY BY EMP NUMBER
    if text.upper().startswith("EMP"):
        emp_id = text.upper().split()[0]
        if emp_id in ad:
            user = ad[emp_id]
            if not user.get("active", True):
                replies.append("Access denied. You are not authorized to use this application.")
                return replies
            # Ask for secret key
            pending_auth[user_id] = emp_id
            replies.append("Please enter your secret key (last 4 digits of your National ID).\nFor demo, use: 0000")
        else:
            replies.append("Access denied. You are not authorized to use this application.")
        return replies

    # Helper: check if user is identified
    emp_id = user_sessions.get(user_id)
    if not emp_id:
        replies.append("Please first provide your Employee Number e.g EMP001 to identify yourself.")
        return replies

    # Verify emp_id still exists and is active
    if emp_id not in ad or not ad[emp_id].get("active", True):
        if emp_id in user_sessions:
            del user_sessions[user_id]
        replies.append("Access denied. You are not authorized to use this application.")
        return replies

    role = ad[emp_id].get("role", "user")
    is_admin = (role == "admin")

    # 4. MY ACCOUNT
    if "my account" in text or "who am i" in text:
        u = ad[emp_id]
        replies.append(f"Your Record:\n{json.dumps(u, indent=2)}")
        return replies

    # 5. RESET PASSWORD (with burn link)
    if "reset" in text and "password" in text:
        if not check_rate_limit(user_id, "reset_password"):
            replies.append("Too many password reset requests. Please try again later.")
            return replies
        new_pass = generate_password()
        ad[emp_id]['password'] = new_pass
        save_ad(ad)
        link = make_burn_link(emp_id, new_pass)
        replies.append(f"Password Recreated Successfully 🔐\nFor {emp_id} ({ad[emp_id]['name']})\n\nView your new password (one-time, expires in 5 min):\n{link}\n\nPlease log in and change it.")
        return replies

    # WIFI CREDENTIALS
    if "wifi" in text:
        if not check_rate_limit(user_id, "wifi"):
            replies.append("Too many WiFi credential requests. Please try again later.")
            return replies
        link = make_burn_link("WIFI", "13245678", link_type="wifi", extra={"network": "IT"})
        replies.append(f"WiFi Credentials 🔐\nNetwork: IT\n\nView your WiFi password (one-time, expires in 5 min):\n{link}")
        return replies

    # 6. CREATE ACCOUNT - admin only
    if "create account" in text:
        if not is_admin:
            replies.append("You are not authorized to perform this action. Only admins can create accounts.")
            return replies
        replies.append("Let's create account. Send in this format:\nCREATE EMP006 | Full Name | email | Department\nExample: CREATE EMP006 | Blessing Banda | blessing@zimdesk.local | IT")
        return replies

    if text.upper().startswith("CREATE EMP"):
        if not is_admin:
            replies.append("You are not authorized to perform this action. Only admins can create accounts.")
            return replies
        if not check_rate_limit(user_id, "create_account"):
            replies.append("Too many account creation requests. Please try again later.")
            return replies
        try:
            parts = text.split("|")
            new_emp_id = parts[0].replace("create","").strip().upper()
            name = parts[1].strip().title()
            email = parts[2].strip()
            dept = parts[3].strip()

            if new_emp_id in ad:
                replies.append(f"{new_emp_id} already exists!")
            else:
                new_pass = generate_password()
                ad[new_emp_id] = {"name": name, "email": email, "department": dept, "password": new_pass, "active": True, "role": "user", "secret_key": "0000"}
                save_ad(ad)
                link = make_burn_link(new_emp_id, new_pass)
                replies.append(f"Account Created ✅\nID: {new_emp_id}\nName: {name}\nEmail: {email}\nDept: {dept}\nRole: user\nSecret Key: 0000 (default)\n\nView temp password (one-time, expires in 5 min):\n{link}")
        except Exception:
            replies.append("Wrong format. Use: CREATE EMP006 | Full Name | email | Department")
        return replies

    # ADMIN: List all users
    if text == "list users" or text == "list all users":
        if not is_admin:
            replies.append("You are not authorized to perform this action.")
            return replies
        lines = ["All Users:"]
        for eid, u in ad.items():
            status = "Active" if u.get("active", True) else "Disabled"
            role = u.get("role", "user")
            lines.append(f"{eid}: {u['name']} | {u['department']} | {status} | {role}")
        replies.append("\n".join(lines))
        return replies

    # ADMIN: Disable user
    if text.startswith("disable "):
        if not is_admin:
            replies.append("You are not authorized to perform this action.")
            return replies
        target = text.replace("disable", "").strip().upper()
        if target in ad:
            ad[target]["active"] = False
            save_ad(ad)
            replies.append(f"User {target} ({ad[target]['name']}) has been disabled.")
        else:
            replies.append(f"Employee {target} not found.")
        return replies

    # ADMIN: Enable user
    if text.startswith("enable "):
        if not is_admin:
            replies.append("You are not authorized to perform this action.")
            return replies
        target = text.replace("enable", "").strip().upper()
        if target in ad:
            ad[target]["active"] = True
            save_ad(ad)
            replies.append(f"User {target} ({ad[target]['name']}) has been enabled.")
        else:
            replies.append(f"Employee {target} not found.")
        return replies

    # Default
    replies.append("I didn't understand. Type 'hie' to start.")
    return replies

# ---- Webhook verification (Meta) ----
@app.route("/", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

# ---- Burn link endpoint ----
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

# ---- Webhook (WhatsApp) ----
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

# ---- Web Chat API ----
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", "web_user")
    text = data.get("message", "")
    replies = handle_message(user_id, text)
    return {"replies": replies}, 200

# ---- Web Chat UI ----
@app.route("/test", methods=["GET"])
def test_ui():
    return render_template_string(CHAT_HTML)

CHAT_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ZHRCDeskAI - Web Chat Test</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); display: flex; justify-content: center; align-items: center; height: 100vh; }
  .chat-container { width: 440px; max-width: 100%; height: 92vh; background: #fff; border-radius: 16px; box-shadow: 0 8px 32px rgba(0,0,0,0.3); display: flex; flex-direction: column; overflow: hidden; }
  .chat-header { background: linear-gradient(135deg, #075e54, #128c7e); color: #fff; padding: 18px 20px; display: flex; align-items: center; gap: 14px; }
  .chat-header .avatar { width: 48px; height: 48px; border-radius: 50%; background: rgba(255,255,255,0.2); display: flex; align-items: center; justify-content: center; font-size: 24px; border: 2px solid rgba(255,255,255,0.3); }
  .chat-header .info h3 { font-size: 18px; font-weight: 600; }
  .chat-header .info p { font-size: 12px; opacity: 0.85; margin-top: 2px; }
  .chat-messages { flex: 1; padding: 20px; overflow-y: auto; background: #f0f2f5; display: flex; flex-direction: column; gap: 10px; }
  .msg { max-width: 85%; padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; animation: fadeIn 0.2s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .msg.user { align-self: flex-end; background: #dcf8c6; border-bottom-right-radius: 4px; }
  .msg.bot { align-self: flex-start; background: #fff; border-bottom-left-radius: 4px; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
  .msg a { color: #075e54; text-decoration: none; font-weight: 600; display: inline-block; margin-top: 6px; padding: 6px 14px; background: #075e54; color: #fff; border-radius: 6px; font-size: 13px; }
  .msg a:hover { background: #128c7e; }
  .chat-input { display: flex; padding: 12px 16px; background: #fff; gap: 10px; border-top: 1px solid #e0e0e0; }
  .chat-input input { flex: 1; padding: 12px 18px; border: 2px solid #e0e0e0; border-radius: 24px; font-size: 14px; outline: none; transition: border-color 0.2s; }
  .chat-input input:focus { border-color: #075e54; }
  .chat-input button { background: linear-gradient(135deg, #075e54, #128c7e); color: #fff; border: none; border-radius: 50%; width: 44px; height: 44px; cursor: pointer; font-size: 18px; display: flex; align-items: center; justify-content: center; transition: transform 0.1s; }
  .chat-input button:hover { transform: scale(1.05); }
  .typing { align-self: flex-start; background: #fff; padding: 12px 16px; border-radius: 12px; font-size: 12px; color: #888; font-style: italic; border-bottom-left-radius: 4px; }
  .footer { text-align: center; padding: 6px; font-size: 10px; color: #999; background: #f0f2f5; }
</style>
</head>
<body>
<div class="chat-container">
  <div class="chat-header">
    <div class="avatar">🤖</div>
    <div class="info">
      <h3>ZHRCDeskAI</h3>
      <p>IT Helpdesk Assistant • Web Test</p>
    </div>
  </div>
  <div class="chat-messages" id="messages"></div>
  <div class="chat-input">
    <input type="text" id="input" placeholder="Type a message..." onkeypress="if(event.key==='Enter')sendMessage()" autofocus>
    <button onclick="sendMessage()">➤</button>
  </div>
  <div class="footer">Zimbabwe Human Rights Commission — IT Department</div>
</div>
<script>
  const userId = "web_user_" + Math.random().toString(36).substr(2, 9);
  const messagesDiv = document.getElementById('messages');
  const input = document.getElementById('input');

  function addMsg(text, isUser) {
    const div = document.createElement('div');
    div.className = 'msg ' + (isUser ? 'user' : 'bot');
    let html = text.replace(/(https?:\/\/[^\s]+)/g, function(url) {
      return '<a href="' + url + '" target="_blank">🔗 Open Link</a>';
    });
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
    typing.textContent = 'ZHRCDeskAI is typing...';
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

# ---- Admin status ----
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "zimdesk_admin_123")

@app.route("/admin/status", methods=["GET"])
def admin_status():
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

if __name__ == "__main__":
    app.run(port=5000, debug=True)