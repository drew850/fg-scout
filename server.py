import os, json, urllib.request, urllib.error, secrets, hashlib, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode

PORT                = int(os.environ.get("PORT", 3747))
DIR                 = os.path.dirname(os.path.abspath(__file__))
NOTION_TOKEN        = os.environ.get("NOTION_TOKEN", "")
ANTHROPIC_KEY       = os.environ.get("ANTHROPIC_KEY", "")
GOOGLE_CLIENT_ID    = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET= os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL            = os.environ.get("BASE_URL", "https://vigilant-youthfulness-production-896b.up.railway.app")
REDIRECT_URI        = BASE_URL + "/auth/callback"
GORGIAS_DOMAIN      = os.environ.get("GORGIAS_DOMAIN", "freedomgrooming.gorgias.com")
GORGIAS_USERNAME    = os.environ.get("GORGIAS_USERNAME", "")
GORGIAS_API_KEY     = os.environ.get("GORGIAS_API_KEY", "")
QA_USERS_DB_ID      = os.environ.get("QA_USERS_DB_ID", "3744e96c994180c9b8adcec4048bc6fb")
EMERGENCY_PIN       = os.environ.get("EMERGENCY_PIN", "")

ALLOWED_DOMAINS     = {"myfreebird.com", "freedom-grooming.com"}

# SECURITY: /proxy may only forward to these hosts (prevents SSRF / open-relay abuse)
PROXY_ALLOWED_HOSTS = {"api.notion.com", "api.anthropic.com"}

# In-memory session store: token -> {email, name, exp}
SESSIONS = {}
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days

# In-memory OAuth state store: state -> timestamp (prevents CSRF)
OAUTH_STATES = {}
OAUTH_STATE_TTL = 300  # 5 minutes

# ── Emergency login page HTML ──────────────────────────────────────────────────
EMERGENCY_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FG QA — Emergency Access</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f0f;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }}
  .card {{
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 40px;
    width: 360px;
  }}
  .logo {{
    font-size: 12px;
    color: #444;
    text-transform: uppercase;
    letter-spacing: 2px;
    margin-bottom: 28px;
  }}
  h2 {{
    color: #e0e0e0;
    font-size: 20px;
    font-weight: 600;
    margin-bottom: 8px;
  }}
  p {{
    color: #555;
    font-size: 13px;
    margin-bottom: 28px;
    line-height: 1.5;
  }}
  label {{
    display: block;
    color: #666;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 6px;
  }}
  input {{
    width: 100%;
    background: #111;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    color: #e0e0e0;
    font-size: 14px;
    padding: 10px 14px;
    margin-bottom: 20px;
    outline: none;
    transition: border-color 0.2s;
  }}
  input:focus {{ border-color: #444; }}
  button {{
    width: 100%;
    background: #222;
    border: 1px solid #333;
    border-radius: 8px;
    color: #ccc;
    font-size: 14px;
    padding: 11px;
    cursor: pointer;
    transition: background 0.2s, color 0.2s;
  }}
  button:hover {{ background: #2a2a2a; color: #e0e0e0; }}
  .error {{
    background: #1e0f0f;
    border: 1px solid #4a1f1f;
    border-radius: 8px;
    color: #e06060;
    font-size: 13px;
    padding: 10px 14px;
    margin-bottom: 20px;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Freedom Grooming &nbsp;·&nbsp; QA Tool</div>
  <h2>Emergency Access</h2>
  <p>Use this only if Google SSO is unavailable. Enter your work email and the emergency PIN.</p>
  {error_block}
  <form method="POST" action="/emergency">
    <label>Work Email</label>
    <input type="email" name="email" placeholder="you@myfreebird.com" required autocomplete="off" autofocus>
    <label>Emergency PIN</label>
    <input type="password" name="pin" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" required autocomplete="off">
    <button type="submit">Access QA Tool</button>
  </form>
</div>
</body>
</html>"""

def render_emergency(error=None, status=200):
    error_block = f'<div class="error">{error}</div>' if error else ""
    html = EMERGENCY_HTML.replace("{error_block}", error_block).encode()
    return html, status

# ── Helpers ────────────────────────────────────────────────────────────────────

CR_L1_OPTIONS = [
    "Cancel",
    "Order Issue",
    "Order Status",
    "Other",
    "Subscription",
    "Troubleshooting",
    "Update Order"
]

CR_L2_OPTIONS = {
    "Cancel": [
        "Cancel 1st Product Order",
        "Subscription (Aware)",
        "Subscription (Unaware)",
        "Subscription Aware",
        "Subscription Order",
        "Subscription Unaware"
    ],
    "Order Issue": [
        "CX Wrong Item / Order",
        "FB Wrong Item / Order",
        "Missing Item From Kit",
        "Missing Item From Order",
        "Package Damaged / Damaged Upon Arrival",
        "Received Unsatisfactory Product",
        "Received Used Product",
        "Return Request"
    ],
    "Order Status": [
        "Delays, but Not Lost",
        "Delivered, Not Received",
        "International (Delays, but Not Lost)",
        "International (No Delays)",
        "Lost in Transit",
        "Never Shipped",
        "No Delays",
        "Returned to Sender",
        "Wrong Address"
    ],
    "Other": [
        "General Order Question",
        "General Product Question",
        "Influencer/Job Inquiry",
        "Negative Feedback",
        "Other",
        "Payment/Charge Issues",
        "Positive Feedback",
        "Promo Request/Issue",
        "Social General/Tagging",
        "System Notification",
        "Update Account",
        "Wholesale"
    ],
    "Subscription": [
        "Change Address",
        "Change Frequency",
        "Change Product",
        "Skip Order"
    ],
    "Troubleshooting": [
        "Broken Blade / Attachment",
        "Never worked (new device)",
        "Stopped Working",
        "Will Not Charge",
        "Won't turn OFF"
    ],
    "Update Order": [
        "Add / Remove / Change Item",
        "Change Address"
    ]
}

_users_cache      = None
_users_cache_time = 0
USERS_CACHE_TTL   = 300  # 5 minutes

def fetch_qa_users_from_notion():
    """Load active users from QA Users Notion DB. Cached for 5 minutes."""
    global _users_cache, _users_cache_time
    now = time.time()
    if _users_cache is not None and (now - _users_cache_time) < USERS_CACHE_TTL:
        return _users_cache
    if not NOTION_TOKEN or not QA_USERS_DB_ID:
        return []
    users = []
    cursor = None
    has_more = True
    while has_more:
        body = {"page_size": 100, "filter": {"property": "Active", "checkbox": {"equals": True}}}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{QA_USERS_DB_ID}/query",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            for page in result.get("results", []):
                props = page.get("properties", {})
                email_arr = props.get("Email", {}).get("title", [])
                email = email_arr[0]["text"]["content"].lower().strip() if email_arr else ""
                name_arr  = props.get("Name", {}).get("rich_text", [])
                name  = name_arr[0]["text"]["content"].strip() if name_arr else ""
                role_prop = props.get("Role", {})
                if "select" in role_prop and role_prop["select"]:
                    role = role_prop["select"]["name"].lower().strip()
                else:
                    role_arr = role_prop.get("rich_text", [])
                    role = role_arr[0]["text"]["content"].lower().strip() if role_arr else "view"
                if email:
                    users.append({"email": email, "name": name or email.split("@")[0], "role": role})
            has_more = result.get("has_more", False)
            cursor   = result.get("next_cursor")
        except Exception as e:
            print(f"[Users] Notion fetch failed: {e}")
            break
    _users_cache      = users
    _users_cache_time = now
    print(f"[Users] Loaded {len(users)} active users from QA Users DB")
    return users

def find_user_by_email(email):
    """Return user dict if email matches an active Notion user, else None (strict, no domain fallback)."""
    email_lower = email.lower().strip()
    for u in fetch_qa_users_from_notion():
        if u["email"] == email_lower:
            return u
    return None

def inject_env(html: bytes) -> bytes:
    import json as _json
    # SECURITY: NOTION_TOKEN and ANTHROPIC_KEY are intentionally NOT exposed to the
    # client. The /proxy handler injects them server-side based on the destination
    # host, so the keys never reach the browser. Only non-secret config is shipped.
    env = {
        "GOOGLE_CLIENT_ID":   GOOGLE_CLIENT_ID,
        "BASE_URL":           BASE_URL,
        "GORGIAS_DOMAIN":     GORGIAS_DOMAIN,
        "GORGIAS_CONFIGURED": bool(GORGIAS_USERNAME and GORGIAS_API_KEY),
        "QA_USERS_DB_ID":     QA_USERS_DB_ID,
    }
    snippet = (
        "<script>"
        f"window.__ENV__={_json.dumps(env)};"
        f"window.__CR_L1_OPTIONS={_json.dumps(CR_L1_OPTIONS)};"
        f"window.__CR_L2_OPTIONS={_json.dumps(CR_L2_OPTIONS)};"
        "</script>"
    )
    return html.replace(b"</head>", snippet.encode() + b"</head>", 1)

def get_version():
    try:
        fp = os.path.join(DIR, "QAToolNotion.html")
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                if "Version:" in line:
                    return line.strip()
        return "unknown"
    except:
        return "error reading file"

def google_get_token(code):
    data = urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def google_get_userinfo(access_token):
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())

def create_session(email, name, via="google"):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "email": email.lower().strip(),
        "name":  name,
        "via":   via,
        "exp":   time.time() + SESSION_TTL
    }
    # Clean expired sessions opportunistically
    expired = [k for k, v in SESSIONS.items() if v["exp"] < time.time()]
    for k in expired:
        del SESSIONS[k]
    return token

def verify_session(token):
    session = SESSIONS.get(token)
    if not session:
        return None
    if session["exp"] < time.time():
        del SESSIONS[token]
        return None
    return session

def parse_form(raw: bytes) -> dict:
    """Parse application/x-www-form-urlencoded body."""
    out = {}
    for pair in raw.decode(errors="replace").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[unquote(k.replace("+", " "))] = unquote(v.replace("+", " "))
    return out

# ── Request handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _session(self):
        """Return the verified session for this request, or None. Reads token from
        X-Session-Token header (preferred) or Authorization: Bearer fallback."""
        tok = self.headers.get("X-Session-Token", "")
        if not tok:
            tok = self.headers.get("Authorization", "").replace("Bearer ", "")
        return verify_session(tok.strip())

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")

    def _json(self, data, status=200):
        msg = json.dumps(data).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def _html(self, data: bytes, status=200):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        # ── Version ───────────────────────────────────────────────────────
        if path == "/version":
            self._json({
                "version": get_version(),
                "file":    os.path.join(DIR, "QAToolNotion.html"),
                "exists":  os.path.isfile(os.path.join(DIR, "QAToolNotion.html"))
            })
            return

        # ── Google OAuth: initiate ────────────────────────────────────────
        if path == "/auth/google":
            if not GOOGLE_CLIENT_ID:
                self._json({"error": "Google SSO not configured"}, 500)
                return
            state = secrets.token_urlsafe(16)
            OAUTH_STATES[state] = time.time()
            params = urlencode({
                "client_id":     GOOGLE_CLIENT_ID,
                "redirect_uri":  REDIRECT_URI,
                "response_type": "code",
                "scope":         "openid email profile",
                "state":         state,
                "access_type":   "online",
                "hd":            "myfreebird.com"
            })
            self._redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
            return

        # ── Google OAuth: callback ────────────────────────────────────────
        if path == "/auth/callback":
            code  = qs.get("code",  [""])[0]
            state = qs.get("state", [""])[0]
            error = qs.get("error", [""])[0]

            if error:
                self._redirect(f"{BASE_URL}/?auth_error={error}")
                return

            state_time = OAUTH_STATES.pop(state, None)
            if not state_time or (time.time() - state_time) > OAUTH_STATE_TTL:
                self._redirect(f"{BASE_URL}/?auth_error=invalid_state")
                return

            try:
                token_data   = google_get_token(code)
                access_token = token_data.get("access_token")
                if not access_token:
                    raise Exception("No access token")
                userinfo = google_get_userinfo(access_token)
                email    = userinfo.get("email", "").lower().strip()
                name     = userinfo.get("name", email.split("@")[0])
                if not email:
                    raise Exception("No email returned")
                session_token = create_session(email, name, via="google")
                self._redirect(f"{BASE_URL}/?session={session_token}")
            except Exception as e:
                print(f"[SSO] Auth error: {e}")
                self._redirect(f"{BASE_URL}/?auth_error=auth_failed")
            return

        # ── Session verify ────────────────────────────────────────────────
        if path == "/auth/verify":
            auth_header = self.headers.get("Authorization", "")
            token = auth_header.replace("Bearer ", "").strip()
            if not token:
                token = qs.get("token", [""])[0]
            session = verify_session(token)
            if session:
                self._json({"ok": True, "email": session["email"], "name": session["name"]})
            else:
                self._json({"ok": False, "error": "Invalid or expired session"}, 401)
            return

        # ── Emergency login page (GET) ────────────────────────────────────
        if path == "/emergency":
            html, status = render_emergency()
            self._html(html, status)
            return

        # ── Static file serving ───────────────────────────────────────────
        if path in ("/", "/index.html"):
            path = "/QAToolNotion.html"
        filepath = os.path.join(DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            with open(filepath, "rb") as f:
                data = f.read()
            if filepath.endswith(".html"):
                data = inject_env(data)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html" if filepath.endswith(".html") else "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        # ── Session logout ────────────────────────────────────────────────
        if path == "/auth/logout":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body  = json.loads(raw)
                token = body.get("token", "")
                if token in SESSIONS:
                    del SESSIONS[token]
            except:
                pass
            self._json({"ok": True})
            return

        # ── Emergency login (POST) ────────────────────────────────────────
        if path == "/emergency":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length) if length else b""
            form   = parse_form(raw)

            submitted_email = form.get("email", "").strip().lower()
            submitted_pin   = form.get("pin",   "").strip()

            # PIN must be configured
            if not EMERGENCY_PIN:
                html, status = render_emergency("Emergency login is not configured. Contact your admin.")
                self._html(html, 403)
                return

            # All three checks must pass — use constant-time compare for PIN
            pin_ok    = secrets.compare_digest(submitted_pin, EMERGENCY_PIN)
            domain    = submitted_email.split("@")[-1] if "@" in submitted_email else ""
            domain_ok = domain in ALLOWED_DOMAINS
            user      = find_user_by_email(submitted_email) if (pin_ok and domain_ok) else None

            if not (pin_ok and domain_ok and submitted_email and user):
                html, status = render_emergency("Invalid email or PIN.")
                self._html(html, 401)
                return

            name          = user.get("name", submitted_email.split("@")[0].replace(".", " ").title())
            session_token = create_session(submitted_email, name, via="emergency")
            self._redirect(f"{BASE_URL}/?session={session_token}")
            return

        # ── Gorgias proxy ─────────────────────────────────────────────────
        if path == "/gorgias":
            if not self._session():
                self._json({"error": "unauthorized"}, 401)
                return
            if not GORGIAS_USERNAME or not GORGIAS_API_KEY:
                self._json({"error": "Gorgias credentials not configured"}, 500)
                return
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except:
                body = {}
            endpoint = body.get("endpoint", "")
            params   = body.get("params", {})
            g_method = body.get("method", "GET")
            payload  = body.get("body")
            if not endpoint:
                self._json({"error": "endpoint required"}, 400)
                return
            import base64
            url = f"https://{GORGIAS_DOMAIN}/api{endpoint}"
            if params:
                url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
            creds = base64.b64encode(f"{GORGIAS_USERNAME}:{GORGIAS_API_KEY}".encode()).decode()
            fwd_headers = {
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/json",
                "User-Agent":    "FG-QA-Tool/1.0 (internal)",
                "Accept":        "application/json"
            }
            try:
                body_bytes = json.dumps(payload).encode() if payload else None
                req  = urllib.request.Request(url, data=body_bytes, headers=fwd_headers, method=g_method)
                resp = urllib.request.urlopen(req, timeout=30)
                data = resp.read()
                self.send_response(resp.status)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return

        # ── General proxy (Notion + Anthropic only, session-gated) ────────
        if not self.path.startswith("/proxy"):
            self.send_response(404)
            self.end_headers()
            return

        # Require a valid session — the proxy holds the real API keys
        if not self._session():
            self._json({"error": "unauthorized"}, 401)
            return

        qs     = parse_qs(urlparse(self.path).query)
        target = unquote(qs.get("url", [""])[0])
        if not target.startswith("https://"):
            self._json({"error": "bad url"}, 400)
            return

        # Host allowlist — prevents SSRF / open-relay abuse
        host = urlparse(target).hostname or ""
        if host not in PROXY_ALLOWED_HOSTS:
            self._json({"error": f"host not allowed: {host}"}, 403)
            return

        length     = int(self.headers.get("Content-Length", 0))
        raw        = self.rfile.read(length) if length else b"{}"
        try:    wrapper = json.loads(raw)
        except: wrapper = {}

        method     = wrapper.get("_method", "POST")
        body_obj   = wrapper.get("_body")
        body_bytes = json.dumps(body_obj).encode() if body_obj is not None else None

        # Credentials are injected SERVER-SIDE based on destination host.
        # Any client-supplied auth headers (_headers) are ignored — keys never
        # leave the server.
        fwd = {"Content-Type": "application/json"}
        if host == "api.notion.com":
            fwd["Authorization"]  = f"Bearer {NOTION_TOKEN}"
            fwd["Notion-Version"] = "2022-06-28"
        elif host == "api.anthropic.com":
            fwd["x-api-key"]         = ANTHROPIC_KEY
            fwd["anthropic-version"] = "2023-06-01"

        try:
            req  = urllib.request.Request(target, data=body_bytes, headers=fwd, method=method)
            resp = urllib.request.urlopen(req, timeout=120)
            data = resp.read()
            self.send_response(resp.status)
            self._cors()
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

if __name__ == "__main__":
    print(f"Starting on port {PORT}")
    print(f"Serving from: {DIR}")
    print(f"HTML version: {get_version()}")
    print(f"HTML exists: {os.path.isfile(os.path.join(DIR, 'QAToolNotion.html'))}")
    print(f"Google SSO: {'configured' if GOOGLE_CLIENT_ID else 'NOT configured'}")
    print(f"Gorgias: {'configured' if GORGIAS_USERNAME and GORGIAS_API_KEY else 'NOT configured'}")
    print(f"Emergency login: {'configured' if EMERGENCY_PIN else 'NOT configured — set EMERGENCY_PIN in Railway'}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
