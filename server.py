import os, json, urllib.request, urllib.error, secrets, hashlib, time, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode
from datetime import datetime, timezone, timedelta, date

# ── Config ─────────────────────────────────────────────────────────────────────
PORT                 = int(os.environ.get("PORT", 3810))
DIR                  = os.path.dirname(os.path.abspath(__file__))
ANTHROPIC_KEY        = os.environ.get("ANTHROPIC_KEY", "")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL             = os.environ.get("BASE_URL", "http://localhost:3810")
REDIRECT_URI         = BASE_URL + "/auth/callback"
GORGIAS_DOMAIN       = os.environ.get("GORGIAS_DOMAIN", "freedomgrooming.gorgias.com")
GORGIAS_USERNAME     = os.environ.get("GORGIAS_USERNAME", "")
GORGIAS_API_KEY      = os.environ.get("GORGIAS_API_KEY", "")
ADMIN_SEED_EMAIL     = os.environ.get("ADMIN_SEED_EMAIL", "drew@myfreebird.com")
EMERGENCY_PIN        = os.environ.get("EMERGENCY_PIN", "")
DATABASE_URL         = os.environ.get("DATABASE_URL", "")
ALLOWED_DOMAINS      = {"myfreebird.com", "freedom-grooming.com"}

# OAuth state store (in-memory, short-lived, CSRF protection)
OAUTH_STATES     = {}
OAUTH_STATE_TTL  = 300
SESSION_TTL      = 60 * 60 * 24 * 7  # 7 days

# ── Gorgias custom field IDs ───────────────────────────────────────────────────
CF_PRODUCT          = "5807"
CF_CONTACT_REASON   = "7630"
CF_RESOLUTION       = "11375"
CF_ADDL_RESOLUTION  = "11421"
CF_SECONDARY_REASON = "9969"

# ── Database (pg8000 — pure Python, no system libs) ────────────────────────────
# Compatibility shim so existing psycopg2-style code works unchanged.
import re as _re

class _DictRow(dict):
    pass

class _CursorWrap:
    """Wraps a pg8000 cursor to support psycopg2-style %s params and dict rows."""
    def __init__(self, cur, as_dict=False):
        self._cur = cur
        self._as_dict = as_dict

    def execute(self, query, params=None):
        # pg8000 uses %s paramstyle (format) — same as psycopg2, so pass through.
        # But pg8000 requires params as a tuple/list; convert None to no-arg call.
        if params is None:
            self._cur.execute(query)
        else:
            self._cur.execute(query, tuple(params))
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._as_dict:
            cols = [d[0] for d in self._cur.description]
            return _DictRow(zip(cols, row))
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        if self._as_dict:
            cols = [d[0] for d in self._cur.description]
            return [_DictRow(zip(cols, r)) for r in rows]
        return rows

    def __getattr__(self, name):
        return getattr(self._cur, name)

class _ConnWrap:
    """Wraps a pg8000 connection to support cursor_factory kwarg."""
    def __init__(self, conn):
        self._conn = conn
        self.autocommit = False

    def cursor(self, cursor_factory=None):
        as_dict = cursor_factory is not None
        return _CursorWrap(self._conn.cursor(), as_dict=as_dict)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

# Fake psycopg2.extras module so `import psycopg2.extras` and RealDictCursor work
class _FakeExtras:
    RealDictCursor = object
import sys as _sys
import types as _types
_fake_psycopg2 = _types.ModuleType("psycopg2")
_fake_psycopg2.extras = _FakeExtras()
_sys.modules.setdefault("psycopg2", _fake_psycopg2)
_sys.modules.setdefault("psycopg2.extras", _FakeExtras())

def _parse_db_url(url):
    # postgresql://user:pass@host:port/dbname  (also postgres://)
    m = _re.match(r'^postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(.+)$', url)
    if not m:
        raise ValueError("Could not parse DATABASE_URL")
    user, password, host, port, dbname = m.groups()
    # Strip query string from dbname if present
    dbname = dbname.split("?")[0]
    return {
        "user": user,
        "password": password,
        "host": host,
        "port": int(port) if port else 5432,
        "database": dbname,
    }

def get_db():
    try:
        import pg8000.dbapi
        cfg = _parse_db_url(DATABASE_URL)
        conn = pg8000.dbapi.connect(
            user=cfg["user"],
            password=cfg["password"],
            host=cfg["host"],
            port=cfg["port"],
            database=cfg["database"],
        )
        return _ConnWrap(conn)
    except Exception as e:
        print(f"[DB] Connection failed: {e}")
        return None

def init_db():
    conn = get_db()
    if not conn:
        print("[DB] Cannot init — no connection")
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_users (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                email       TEXT UNIQUE NOT NULL,
                role        TEXT NOT NULL DEFAULT 'viewer',
                department  TEXT,
                active      BOOLEAN NOT NULL DEFAULT true,
                created_at  TIMESTAMPTZ DEFAULT now(),
                updated_at  TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_sessions (
                token      TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                name       TEXT NOT NULL,
                via        TEXT NOT NULL DEFAULT 'google',
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_tickets (
                ticket_id               BIGINT PRIMARY KEY,
                ticket_url              TEXT,
                subject                 TEXT,
                status                  TEXT,
                initial_channel         TEXT,
                via                     TEXT,
                created_date            TIMESTAMPTZ,
                closed_date             TIMESTAMPTZ,
                updated_date            TIMESTAMPTZ,
                agent                   TEXT,
                agent_email             TEXT,
                assignee_team           TEXT,
                customer_email          TEXT,
                customer_name           TEXT,
                contact_reason_l1       TEXT,
                contact_reason_l2       TEXT,
                contact_reason_l3       TEXT,
                product_l1              TEXT,
                product_l2              TEXT,
                ticket_resolution_l1    TEXT,
                ticket_resolution_l2    TEXT,
                additional_resolution_l1 TEXT,
                additional_resolution_l2 TEXT,
                additional_reason_l1    TEXT,
                additional_reason_l2    TEXT,
                tags                    TEXT,
                message_count           INTEGER,
                transcript              TEXT,
                transcript_fetched      BOOLEAN DEFAULT false,
                raw_json                JSONB,
                first_seen_at           TIMESTAMPTZ DEFAULT now(),
                last_updated_at         TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_insights (
                id           SERIAL PRIMARY KEY,
                department   TEXT NOT NULL,
                week_start   DATE NOT NULL,
                week_end     DATE NOT NULL,
                content      TEXT NOT NULL,
                generated_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(department, week_start)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_sync_log (
                id           SERIAL PRIMARY KEY,
                sync_type    TEXT NOT NULL,
                started_at   TIMESTAMPTZ DEFAULT now(),
                finished_at  TIMESTAMPTZ,
                tickets_synced INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'running',
                error        TEXT
            )
        """)
        # Seed admin user
        if ADMIN_SEED_EMAIL:
            cur.execute("""
                INSERT INTO scout_users (name, email, role, active)
                VALUES (%s, %s, 'admin', true)
                ON CONFLICT (email) DO NOTHING
            """, (ADMIN_SEED_EMAIL.split("@")[0].replace(".", " ").title(), ADMIN_SEED_EMAIL.lower()))
        conn.commit()
        print("[DB] Schema initialized")
    except Exception as e:
        conn.rollback()
        print(f"[DB] Init error: {e}")
    finally:
        conn.close()

# ── Session helpers ────────────────────────────────────────────────────────────
def create_session(email, name, via="google"):
    token   = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL)
    conn = get_db()
    if not conn:
        return token
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scout_sessions (token, email, name, via, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (token) DO NOTHING
        """, (token, email.lower().strip(), name, via, expires))
        cur.execute("DELETE FROM scout_sessions WHERE expires_at < now()")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[Session] Create error: {e}")
    finally:
        conn.close()
    return token

def verify_session(token):
    if not token:
        return None
    conn = get_db()
    if not conn:
        return None
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT email, name, via FROM scout_sessions
            WHERE token = %s AND expires_at > now()
        """, (token,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[Session] Verify error: {e}")
        return None
    finally:
        conn.close()

def delete_session(token):
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM scout_sessions WHERE token = %s", (token,))
        conn.commit()
    except:
        conn.rollback()
    finally:
        conn.close()

def get_user(email):
    conn = get_db()
    if not conn:
        return None
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, name, email, role, department, active
            FROM scout_users WHERE email = %s AND active = true
        """, (email.lower().strip(),))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[User] Get error: {e}")
        return None
    finally:
        conn.close()

def auth_required(handler_fn):
    """Decorator: verify session, attach user to request, call handler."""
    def wrapper(self, body=None):
        auth_header = self.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()
        session = verify_session(token)
        if not session:
            self._json({"error": "Unauthorized"}, 401)
            return
        user = get_user(session["email"])
        if not user:
            domain = session["email"].split("@")[-1] if "@" in session["email"] else ""
            if domain in ALLOWED_DOMAINS:
                user = {"email": session["email"], "name": session["name"], "role": "viewer", "department": None, "active": True}
            else:
                self._json({"error": "Access denied"}, 403)
                return
        if body is not None:
            handler_fn(self, user, body)
        else:
            handler_fn(self, user)
    return wrapper

# ── Gorgias helpers ────────────────────────────────────────────────────────────
def gorgias_request(endpoint, params=None):
    url = f"https://{GORGIAS_DOMAIN}/api{endpoint}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    creds = base64.b64encode(f"{GORGIAS_USERNAME}:{GORGIAS_API_KEY}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Scout/1.0 (internal)"
    })
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())

def parse_custom_fields(cf):
    def split_cf(key):
        val = cf.get(key, {})
        if isinstance(val, dict):
            raw = val.get("value", "") or ""
        else:
            raw = str(val) if val else ""
        parts = [p.strip() for p in raw.split("::")]
        return parts

    product   = split_cf(CF_PRODUCT)
    contact   = split_cf(CF_CONTACT_REASON)
    res       = split_cf(CF_RESOLUTION)
    addl_res  = split_cf(CF_ADDL_RESOLUTION)
    sec       = split_cf(CF_SECONDARY_REASON)

    return {
        "product_l1":               product[0] if len(product) > 0 else "",
        "product_l2":               product[1] if len(product) > 1 else "",
        "contact_reason_l1":        contact[0] if len(contact) > 0 else "",
        "contact_reason_l2":        contact[1] if len(contact) > 1 else "",
        "contact_reason_l3":        contact[2] if len(contact) > 2 else "",
        "ticket_resolution_l1":     res[0]     if len(res)     > 0 else "",
        "ticket_resolution_l2":     res[1]     if len(res)     > 1 else "",
        "additional_resolution_l1": addl_res[0] if len(addl_res) > 0 else "",
        "additional_resolution_l2": addl_res[1] if len(addl_res) > 1 else "",
        "additional_reason_l1":     sec[0]     if len(sec)     > 0 else "",
        "additional_reason_l2":     sec[1]     if len(sec)     > 1 else "",
    }

def parse_tags(tags_list):
    non_bot = [t["name"] for t in (tags_list or []) if not t.get("name","").startswith("Yuma:")]
    return ", ".join(non_bot)

def fetch_messages_for_ticket(ticket_id):
    try:
        data = gorgias_request(f"/tickets/{ticket_id}/messages", {"limit": 50, "order_by": "created_datetime:asc"})
        messages = data.get("data", [])
        parts = []
        for m in messages:
            sender = "Agent" if m.get("from_agent") else "Customer"
            body   = m.get("body_text") or m.get("body") or ""
            body   = body[:500].strip()
            if body:
                parts.append(f"{sender}: {body}")
        return "\n".join(parts)
    except Exception as e:
        print(f"[Gorgias] Messages fetch error for {ticket_id}: {e}")
        return ""

def upsert_ticket(cur, t):
    cf = t.get("custom_fields", {})
    fields = parse_custom_fields(cf)
    assignee = t.get("assignee_user") or {}
    team     = t.get("assignee_team") or {}
    customer = t.get("customer") or {}

    cur.execute("""
        INSERT INTO scout_tickets (
            ticket_id, ticket_url, subject, status, initial_channel, via,
            created_date, closed_date, updated_date,
            agent, agent_email, assignee_team,
            customer_email, customer_name,
            contact_reason_l1, contact_reason_l2, contact_reason_l3,
            product_l1, product_l2,
            ticket_resolution_l1, ticket_resolution_l2,
            additional_resolution_l1, additional_resolution_l2,
            additional_reason_l1, additional_reason_l2,
            tags, message_count, raw_json, last_updated_at
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()
        )
        ON CONFLICT (ticket_id) DO UPDATE SET
            status                   = EXCLUDED.status,
            closed_date              = EXCLUDED.closed_date,
            updated_date             = EXCLUDED.updated_date,
            agent                    = EXCLUDED.agent,
            agent_email              = EXCLUDED.agent_email,
            assignee_team            = EXCLUDED.assignee_team,
            contact_reason_l1        = EXCLUDED.contact_reason_l1,
            contact_reason_l2        = EXCLUDED.contact_reason_l2,
            contact_reason_l3        = EXCLUDED.contact_reason_l3,
            product_l1               = EXCLUDED.product_l1,
            product_l2               = EXCLUDED.product_l2,
            ticket_resolution_l1     = EXCLUDED.ticket_resolution_l1,
            ticket_resolution_l2     = EXCLUDED.ticket_resolution_l2,
            additional_resolution_l1 = EXCLUDED.additional_resolution_l1,
            additional_resolution_l2 = EXCLUDED.additional_resolution_l2,
            additional_reason_l1     = EXCLUDED.additional_reason_l1,
            additional_reason_l2     = EXCLUDED.additional_reason_l2,
            tags                     = EXCLUDED.tags,
            message_count            = EXCLUDED.message_count,
            raw_json                 = EXCLUDED.raw_json,
            last_updated_at          = now()
    """, (
        t.get("id"),
        f"https://{GORGIAS_DOMAIN}/app/tickets/{t.get('id')}",
        t.get("subject",""),
        t.get("status",""),
        t.get("channel",""),
        t.get("via",""),
        t.get("created_datetime"),
        t.get("closed_datetime"),
        t.get("updated_datetime"),
        assignee.get("name","") or assignee.get("email",""),
        assignee.get("email",""),
        team.get("name",""),
        customer.get("email",""),
        customer.get("name",""),
        fields["contact_reason_l1"],
        fields["contact_reason_l2"],
        fields["contact_reason_l3"],
        fields["product_l1"],
        fields["product_l2"],
        fields["ticket_resolution_l1"],
        fields["ticket_resolution_l2"],
        fields["additional_resolution_l1"],
        fields["additional_resolution_l2"],
        fields["additional_reason_l1"],
        fields["additional_reason_l2"],
        parse_tags(t.get("tags", [])),
        t.get("messages_count", 0),
        json.dumps(t)
    ))

def run_sync(since_dt, sync_type="manual", log_id=None):
    """Fetch all tickets updated since since_dt, upsert into DB."""
    if not GORGIAS_USERNAME or not GORGIAS_API_KEY:
        print("[Sync] Gorgias credentials not configured")
        return 0

    conn = get_db()
    if not conn:
        return 0

    total = 0
    cursor = None
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00") if isinstance(since_dt, datetime) else since_dt

    try:
        import psycopg2.extras
        cur = conn.cursor()
        while True:
            params = {
                "limit": 100,
                "order_by": "updated_datetime:asc",
            }
            if cursor:
                params["cursor"] = cursor
            data = gorgias_request("/tickets", params)
            tickets = data.get("data", [])
            if not tickets:
                break

            # Filter client-side since Gorgias doesn't support date range filter
            filtered = [t for t in tickets if t.get("updated_datetime","") >= since_str]

            for t in filtered:
                upsert_ticket(cur, t)
                total += 1

            if total % 100 == 0 and total > 0:
                conn.commit()
                print(f"[Sync] {total} tickets synced...")

            meta = data.get("meta", {})
            next_cursor = meta.get("next_cursor")
            if not next_cursor:
                break

            # Stop paginating if all remaining tickets are older than since_dt
            last_updated = tickets[-1].get("updated_datetime", "")
            if last_updated < since_str:
                break

            cursor = next_cursor
            time.sleep(0.1)  # gentle rate limiting

        conn.commit()

        if log_id:
            cur.execute("""
                UPDATE scout_sync_log
                SET finished_at = now(), tickets_synced = %s, status = 'success'
                WHERE id = %s
            """, (total, log_id))
            conn.commit()

        print(f"[Sync] Done — {total} tickets synced")
    except Exception as e:
        conn.rollback()
        print(f"[Sync] Error: {e}")
        if log_id:
            try:
                cur2 = conn.cursor()
                cur2.execute("""
                    UPDATE scout_sync_log
                    SET finished_at = now(), status = 'error', error = %s
                    WHERE id = %s
                """, (str(e), log_id))
                conn.commit()
            except:
                pass
    finally:
        conn.close()
    return total

def run_backfill():
    """One-time backfill from 1st of previous month."""
    today = date.today()
    if today.month == 1:
        start = date(today.year - 1, 12, 1)
    else:
        start = date(today.year, today.month - 1, 1)
    since = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=timezone.utc)

    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM scout_tickets")
        count = cur.fetchone()[0]
        conn.close()
        if count > 0:
            print(f"[Backfill] Skipping — {count} tickets already in DB")
            return
    except:
        conn.close()
        return

    print(f"[Backfill] Starting from {since.date()}")
    conn2 = get_db()
    if not conn2:
        return
    try:
        cur2 = conn2.cursor()
        cur2.execute("""
            INSERT INTO scout_sync_log (sync_type) VALUES ('backfill') RETURNING id
        """)
        log_id = cur2.fetchone()[0]
        conn2.commit()
        conn2.close()
    except:
        conn2.close()
        log_id = None

    run_sync(since, sync_type="backfill", log_id=log_id)

def run_daily_sync():
    """Daily sync — tickets updated in last 25 hours (buffer for timezone drift)."""
    since = datetime.now(timezone.utc) - timedelta(hours=25)
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scout_sync_log (sync_type) VALUES ('daily') RETURNING id
        """)
        log_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
    except:
        conn.close()
        log_id = None
    print("[Sync] Running daily sync...")
    run_sync(since, sync_type="daily", log_id=log_id)

# ── Insight generation ─────────────────────────────────────────────────────────
DEPT_CONFIGS = {
    "cx": {
        "name": "Customer Experience",
        "filters": {},
        "system": """You are an analytics expert reviewing CX support data for Freedom Grooming (Freebird), a grooming and electric shaver subscription company.
Analyze this week's ticket data and provide insights for the CX leadership team.
Focus on: agent performance patterns, ticket volume trends, repeat contact signals, resolution quality, coaching opportunities, and operational bottlenecks.
Structure your response with clear sections: Key Metrics, Notable Patterns, Risks & Flags, Recommended Actions.
Be specific and data-driven. Reference actual numbers from the data provided."""
    },
    "marketing": {
        "name": "Marketing",
        "filters": {"contact_reason_l1": ["Other", "Cancel"]},
        "system": """You are an analytics expert reviewing customer support data for Freedom Grooming (Freebird).
Analyze this week's ticket data and surface insights for the Marketing team.
Focus on: subscription unawareness rate, product confusion, customer sentiment themes, voice-of-customer signals, messaging gaps, and campaign-related feedback.
Structure your response with clear sections: Key Metrics, Customer Sentiment, Messaging Gaps, VOC Highlights, Recommended Actions.
Be specific. Quote from transcripts where relevant to illustrate points."""
    },
    "sales": {
        "name": "Sales",
        "filters": {"contact_reason_l1": ["Cancel", "Subscription"]},
        "system": """You are an analytics expert reviewing customer support data for Freedom Grooming (Freebird).
Analyze this week's ticket data and surface insights for the Sales team.
Focus on: cancellation reasons, save rate signals, subscription awareness issues, upgrade/downgrade patterns, and retention opportunities.
Structure your response with clear sections: Key Metrics, Cancel Reason Breakdown, Save Opportunities, Retention Signals, Recommended Actions."""
    },
    "ops": {
        "name": "Operations / Fulfillment",
        "filters": {"contact_reason_l1": ["Order Issue", "Order Status", "Update Order"]},
        "system": """You are an analytics expert reviewing customer support data for Freedom Grooming (Freebird).
Analyze this week's ticket data and surface insights for the Operations and Fulfillment team.
Focus on: shipping failures, wrong item rates, return rates, damaged/lost packages, address issues, and fulfillment SLA signals.
Structure your response with clear sections: Key Metrics, Fulfillment Issues, Shipping Patterns, Return & Replacement Rate, Recommended Actions."""
    },
    "product": {
        "name": "Product",
        "filters": {"contact_reason_l1": ["Troubleshooting", "Order Issue"]},
        "system": """You are an analytics expert reviewing customer support data for Freedom Grooming (Freebird).
Analyze this week's ticket data and surface insights for the Product team.
Focus on: defect rates, broken blades, charging issues, product-specific complaint patterns, recurring failures, and feature feedback.
Structure your response with clear sections: Key Metrics, Defect & Quality Signals, Product-Specific Breakdown, Recurring Issues, Recommended Actions."""
    }
}

def get_week_bounds(week_offset=0):
    """Return (week_start, week_end) as date objects. week_offset=0 is previous Mon-Sun."""
    today = date.today()
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7 * (1 + week_offset))
    last_sunday  = last_monday + timedelta(days=6)
    return last_monday, last_sunday

def generate_insights(dept, week_start, week_end):
    """Generate Claude insights for a department and week. Returns insight text."""
    config = DEPT_CONFIGS.get(dept)
    if not config:
        return None

    conn = get_db()
    if not conn:
        return None

    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        ws = week_start.isoformat()
        we = (week_end + timedelta(days=1)).isoformat()

        # Aggregate stats
        cur.execute("""
            SELECT
                COUNT(*)                                                AS total,
                COUNT(*) FILTER (WHERE status='closed')                AS closed,
                COUNT(*) FILTER (WHERE status='open')                  AS open,
                COUNT(DISTINCT customer_email)                         AS unique_customers,
                AVG(message_count)                                     AS avg_messages,
                COUNT(*) FILTER (WHERE contact_reason_l1='Cancel')     AS cancellations,
                COUNT(*) FILTER (WHERE contact_reason_l1='Order Issue') AS order_issues,
                COUNT(*) FILTER (WHERE contact_reason_l1='Troubleshooting') AS troubleshooting
            FROM scout_tickets
            WHERE created_date >= %s AND created_date < %s
        """, (ws, we))
        stats = dict(cur.fetchone() or {})

        # Top contact reasons
        cur.execute("""
            SELECT contact_reason_l1, contact_reason_l2, COUNT(*) as cnt
            FROM scout_tickets
            WHERE created_date >= %s AND created_date < %s
              AND contact_reason_l1 IS NOT NULL AND contact_reason_l1 != ''
            GROUP BY contact_reason_l1, contact_reason_l2
            ORDER BY cnt DESC LIMIT 15
        """, (ws, we))
        top_reasons = [dict(r) for r in cur.fetchall()]

        # Agent breakdown
        cur.execute("""
            SELECT agent, COUNT(*) as cnt,
                   COUNT(*) FILTER (WHERE status='closed') as closed
            FROM scout_tickets
            WHERE created_date >= %s AND created_date < %s
              AND agent IS NOT NULL AND agent != ''
            GROUP BY agent ORDER BY cnt DESC LIMIT 10
        """, (ws, we))
        agents = [dict(r) for r in cur.fetchall()]

        # Resolution breakdown
        cur.execute("""
            SELECT ticket_resolution_l1, COUNT(*) as cnt
            FROM scout_tickets
            WHERE created_date >= %s AND created_date < %s
              AND ticket_resolution_l1 IS NOT NULL AND ticket_resolution_l1 != ''
            GROUP BY ticket_resolution_l1 ORDER BY cnt DESC LIMIT 10
        """, (ws, we))
        resolutions = [dict(r) for r in cur.fetchall()]

        # Sample tickets with transcripts (lazy fetch up to 10)
        filters = config.get("filters", {})
        filter_clause = ""
        filter_vals   = [ws, we]
        if filters.get("contact_reason_l1"):
            placeholders = ",".join(["%s"] * len(filters["contact_reason_l1"]))
            filter_clause = f"AND contact_reason_l1 IN ({placeholders})"
            filter_vals += filters["contact_reason_l1"]

        cur.execute(f"""
            SELECT ticket_id, subject, contact_reason_l1, contact_reason_l2,
                   ticket_resolution_l1, agent, status, message_count, transcript, transcript_fetched
            FROM scout_tickets
            WHERE created_date >= %s AND created_date < %s
            {filter_clause}
            ORDER BY message_count DESC LIMIT 10
        """, filter_vals)
        sample_tickets = [dict(r) for r in cur.fetchall()]

        # Fetch missing transcripts lazily
        cur2 = conn.cursor()
        for st in sample_tickets:
            if not st.get("transcript_fetched") and st.get("ticket_id"):
                transcript = fetch_messages_for_ticket(st["ticket_id"])
                if transcript:
                    cur2.execute("""
                        UPDATE scout_tickets SET transcript = %s, transcript_fetched = true
                        WHERE ticket_id = %s
                    """, (transcript, st["ticket_id"]))
                    st["transcript"] = transcript
                    st["transcript_fetched"] = True
        conn.commit()

        # Build prompt
        prompt = f"""Week: {week_start} to {week_end}

TICKET STATISTICS:
{json.dumps(stats, indent=2, default=str)}

TOP CONTACT REASONS:
{json.dumps(top_reasons, indent=2)}

AGENT BREAKDOWN:
{json.dumps(agents, indent=2)}

RESOLUTION BREAKDOWN:
{json.dumps(resolutions, indent=2)}

SAMPLE TICKETS WITH TRANSCRIPTS:
"""
        for st in sample_tickets:
            prompt += f"\n---\nTicket #{st['ticket_id']} | {st.get('contact_reason_l1','')} > {st.get('contact_reason_l2','')} | Agent: {st.get('agent','')} | Status: {st.get('status','')}\n"
            if st.get("transcript"):
                prompt += f"Transcript (excerpt):\n{st['transcript'][:800]}\n"

        # Call Claude
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "system": config["system"],
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())
        content = result.get("content", [])
        text = " ".join(c.get("text","") for c in content if c.get("type") == "text")

        # Cache insight
        cur3 = conn.cursor()
        cur3.execute("""
            INSERT INTO scout_insights (department, week_start, week_end, content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (department, week_start) DO UPDATE SET
                content = EXCLUDED.content,
                generated_at = now()
        """, (dept, week_start, week_end, text))
        conn.commit()
        return text

    except Exception as e:
        print(f"[Insights] Error for {dept}: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def run_weekly_insights():
    """Generate insights for all departments for the previous week."""
    week_start, week_end = get_week_bounds(0)
    print(f"[Insights] Generating for week {week_start} to {week_end}")
    for dept in DEPT_CONFIGS:
        print(f"[Insights] Generating {dept}...")
        generate_insights(dept, week_start, week_end)
        time.sleep(2)
    print("[Insights] Done")

# ── Scheduler (Railway Cron hits /api/cron) ────────────────────────────────────
# See deploy instructions — set up two Railway Cron services:
# 1. Daily sync:    0 16 * * *    (12:00 AM MNL = 16:00 UTC)  POST /api/cron/sync
# 2. Weekly insights: 0 10 * * 2 (Tuesday 6PM MNL = 10:00 UTC) POST /api/cron/insights

# ── Google OAuth helpers ───────────────────────────────────────────────────────
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

# ── Chat agent ─────────────────────────────────────────────────────────────────
SCHEMA_CONTEXT = """
Table: scout_tickets
Columns: ticket_id (bigint), subject (text), status (text), initial_channel (text),
  created_date (timestamptz), closed_date (timestamptz), agent (text), agent_email (text),
  assignee_team (text), customer_email (text), customer_name (text),
  contact_reason_l1 (text), contact_reason_l2 (text), contact_reason_l3 (text),
  product_l1 (text), product_l2 (text),
  ticket_resolution_l1 (text), ticket_resolution_l2 (text),
  additional_resolution_l1 (text), additional_resolution_l2 (text),
  additional_reason_l1 (text), additional_reason_l2 (text),
  tags (text), message_count (integer), transcript (text),
  first_seen_at (timestamptz), last_updated_at (timestamptz)

Common contact_reason_l1 values: Cancel, Order Issue, Order Status, Subscription, Troubleshooting, Other, Update Order
Common status values: open, closed
Use ILIKE for case-insensitive text matching.
Always include LIMIT 100 unless user asks for counts/aggregates.
Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or ALTER.
Return only the SQL query, nothing else.
"""

def chat_query(messages):
    """Multi-turn chat agent. messages = [{role, content}]. Returns answer text."""
    system = f"""You are a data analyst assistant for Freedom Grooming (Freebird) support operations.
You answer questions about customer support tickets stored in a PostgreSQL database.

{SCHEMA_CONTEXT}

When asked a question:
1. Generate a SQL SELECT query to answer it
2. Return ONLY the SQL query wrapped in <sql></sql> tags
3. After seeing results, provide a clear natural language answer

If the question is a follow-up referencing previous context, use that context to refine the query.
Never expose raw customer emails or personal data in your answer summaries."""

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "system": system,
        "messages": messages
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    content = result.get("content", [])
    return " ".join(c.get("text","") for c in content if c.get("type") == "text")

def execute_chat_sql(sql):
    """Run a SELECT-only query, return rows as list of dicts."""
    sql_clean = sql.strip().upper()
    if not sql_clean.startswith("SELECT"):
        return None, "Only SELECT queries are allowed"
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE"]
    for word in forbidden:
        if word in sql_clean:
            return None, f"Forbidden keyword: {word}"
    if "LIMIT" not in sql_clean:
        sql = sql.rstrip(";") + " LIMIT 100"
    conn = get_db()
    if not conn:
        return None, "Database unavailable"
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        return rows, None
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()

# ── Emergency login ────────────────────────────────────────────────────────────
EMERGENCY_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scout — Emergency Access</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7f9;display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .card{{background:#fff;border:1px solid #e6e8eb;border-radius:14px;padding:40px;width:360px;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  .logo{{font-size:11px;color:#98a2b3;text-transform:uppercase;letter-spacing:2px;margin-bottom:28px}}
  h2{{color:#1a1d21;font-size:20px;font-weight:600;margin-bottom:8px}}
  p{{color:#667085;font-size:13px;margin-bottom:28px;line-height:1.5}}
  label{{display:block;color:#667085;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}}
  input{{width:100%;background:#fff;border:1px solid #d0d5dd;border-radius:8px;color:#1a1d21;font-size:14px;padding:10px 14px;margin-bottom:20px;outline:none}}
  input:focus{{border-color:#2970ff}}
  button{{width:100%;background:#2970ff;border:none;border-radius:8px;color:#fff;font-size:14px;padding:11px;cursor:pointer;font-weight:500}}
  button:hover{{background:#1c5fe0}}
  .error{{background:#fef3f2;border:1px solid #fecdca;border-radius:8px;color:#b42318;font-size:13px;padding:10px 14px;margin-bottom:20px}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Scout &nbsp;·&nbsp; Freedom Grooming</div>
  <h2>Emergency access</h2>
  <p>Use only if Google sign-in is unavailable.</p>
  {error_block}
  <form method="POST" action="/emergency">
    <label>Work email</label>
    <input type="email" name="email" placeholder="you@myfreebird.com" required autocomplete="off" autofocus>
    <label>Emergency PIN</label>
    <input type="password" name="pin" placeholder="••••••••" required autocomplete="off">
    <button type="submit">Access Scout</button>
  </form>
</div>
</body>
</html>"""

def render_emergency(error=None):
    block = f'<div class="error">{error}</div>' if error else ""
    return EMERGENCY_HTML.replace("{error_block}", block).encode()

def parse_form(raw):
    out = {}
    for pair in raw.decode(errors="replace").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[unquote(k.replace("+", " "))] = unquote(v.replace("+", " "))
    return out

def inject_env(html):
    snippet = (
        f'<script>window.__ENV__={{'
        f'GOOGLE_CLIENT_ID:"{GOOGLE_CLIENT_ID}",'
        f'BASE_URL:"{BASE_URL}",'
        f'ANTHROPIC_KEY:"{ANTHROPIC_KEY}"'
        f'}};</script>'
    )
    return html.replace(b"</head>", snippet.encode() + b"</head>", 1)

# ── HTTP Handler ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.send_header("Cache-Control", "no-cache,no-store,must-revalidate")

    def _json(self, data, status=200):
        msg = json.dumps(data, default=str).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def _html(self, data, status=200):
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

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b"{}"

    def _get_token(self):
        auth = self.headers.get("Authorization","")
        return auth.replace("Bearer ","").strip()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        # ── Health ────────────────────────────────────────────────────────
        if path == "/health":
            self._json({"ok": True, "version": "1.0.0"})
            return

        # ── Google OAuth initiate ─────────────────────────────────────────
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

        # ── Google OAuth callback ─────────────────────────────────────────
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
                email    = userinfo.get("email","").lower().strip()
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
            token   = self._get_token() or qs.get("token",[""])[0]
            session = verify_session(token)
            if session:
                user = get_user(session["email"])
                if not user:
                    domain = session["email"].split("@")[-1] if "@" in session["email"] else ""
                    if domain in ALLOWED_DOMAINS:
                        user = {"email": session["email"], "name": session["name"], "role": "viewer", "department": None}
                    else:
                        self._json({"ok": False, "error": "Access denied"}, 403)
                        return
                self._json({"ok": True, "email": user["email"], "name": user["name"], "role": user["role"], "department": user.get("department")})
            else:
                self._json({"ok": False, "error": "Invalid or expired session"}, 401)
            return

        # ── Emergency login page ──────────────────────────────────────────
        if path == "/emergency":
            self._html(render_emergency())
            return

        # ── API: Dashboard stats ──────────────────────────────────────────
        if path == "/api/dashboard":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            week_str  = qs.get("week", [""])[0]
            try:
                if week_str:
                    week_start = date.fromisoformat(week_str)
                else:
                    week_start, _ = get_week_bounds(0)
                week_end = week_start + timedelta(days=6)
            except:
                week_start, week_end = get_week_bounds(0)
            ws = week_start.isoformat()
            we = (week_end + timedelta(days=1)).isoformat()
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT
                        COUNT(*)                                               AS total_tickets,
                        COUNT(*) FILTER (WHERE status='closed')               AS closed,
                        COUNT(*) FILTER (WHERE status='open')                 AS open,
                        COUNT(DISTINCT customer_email)                        AS unique_customers,
                        ROUND(AVG(message_count)::numeric,1)                  AS avg_messages,
                        COUNT(*) FILTER (WHERE contact_reason_l1='Cancel')    AS cancellations,
                        COUNT(*) FILTER (WHERE contact_reason_l1='Order Issue') AS order_issues,
                        COUNT(*) FILTER (WHERE contact_reason_l1='Troubleshooting') AS troubleshooting
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                """, (ws, we))
                stats = dict(cur.fetchone() or {})

                cur.execute("""
                    SELECT contact_reason_l1, COUNT(*) as cnt
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                      AND contact_reason_l1 IS NOT NULL AND contact_reason_l1 != ''
                    GROUP BY contact_reason_l1 ORDER BY cnt DESC
                """, (ws, we))
                by_reason = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT initial_channel, COUNT(*) as cnt
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                    GROUP BY initial_channel ORDER BY cnt DESC
                """, (ws, we))
                by_channel = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT agent, COUNT(*) as cnt
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                      AND agent IS NOT NULL AND agent != ''
                    GROUP BY agent ORDER BY cnt DESC LIMIT 10
                """, (ws, we))
                by_agent = [dict(r) for r in cur.fetchall()]

                # Previous week for WoW
                prev_ws = (week_start - timedelta(days=7)).isoformat()
                prev_we = ws
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE contact_reason_l1='Cancel') AS cancellations
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                """, (prev_ws, prev_we))
                prev = dict(cur.fetchone() or {})

                self._json({
                    "week_start": ws, "week_end": week_end.isoformat(),
                    "stats": stats, "by_reason": by_reason,
                    "by_channel": by_channel, "by_agent": by_agent,
                    "prev_week": prev
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── API: Department insights ──────────────────────────────────────
        if path.startswith("/api/insights/"):
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            dept = path.split("/")[-1]
            if dept not in DEPT_CONFIGS:
                self._json({"error": "Unknown department"}, 404)
                return
            week_str = qs.get("week", [""])[0]
            try:
                week_start = date.fromisoformat(week_str) if week_str else get_week_bounds(0)[0]
            except:
                week_start = get_week_bounds(0)[0]
            week_end = week_start + timedelta(days=6)
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT content, generated_at FROM scout_insights
                    WHERE department = %s AND week_start = %s
                """, (dept, week_start))
                row = cur.fetchone()
                if row:
                    self._json({"dept": dept, "week_start": week_start.isoformat(),
                                "week_end": week_end.isoformat(), "content": row["content"],
                                "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
                                "cached": True})
                else:
                    self._json({"dept": dept, "week_start": week_start.isoformat(),
                                "week_end": week_end.isoformat(), "content": None,
                                "cached": False})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── API: Sync log ─────────────────────────────────────────────────
        if path == "/api/sync/log":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT id, sync_type, started_at, finished_at,
                           tickets_synced, status, error
                    FROM scout_sync_log ORDER BY started_at DESC LIMIT 20
                """)
                rows = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*) as total FROM scout_tickets")
                total = cur.fetchone()["total"]
                self._json({"logs": rows, "total_tickets": total})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── API: Users list (admin only) ──────────────────────────────────
        if path == "/api/users":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT id, name, email, role, department, active, created_at, updated_at
                    FROM scout_users ORDER BY created_at ASC
                """)
                users = [dict(r) for r in cur.fetchall()]
                self._json({"users": users})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── API: Available weeks ──────────────────────────────────────────
        if path == "/api/weeks":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("""
                    SELECT DISTINCT
                        date_trunc('week', created_date)::date AS week_start,
                        (date_trunc('week', created_date) + interval '6 days')::date AS week_end,
                        COUNT(*) as ticket_count
                    FROM scout_tickets
                    WHERE created_date IS NOT NULL
                    GROUP BY 1,2 ORDER BY 1 DESC LIMIT 26
                """)
                weeks = [dict(r) for r in cur.fetchall()]
                self._json({"weeks": weeks})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── Static / index ────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            path = "/scout.html"
        filepath = os.path.join(DIR, path.lstrip("/"))
        if os.path.isfile(filepath):
            with open(filepath, "rb") as f:
                data = f.read()
            if filepath.endswith(".html"):
                data = inject_env(data)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        # ── Logout ────────────────────────────────────────────────────────
        if path == "/auth/logout":
            raw = self._read_body()
            try:
                token = json.loads(raw).get("token","")
                delete_session(token)
            except:
                pass
            self._json({"ok": True})
            return

        # ── Emergency login ───────────────────────────────────────────────
        if path == "/emergency":
            raw  = self._read_body()
            form = parse_form(raw)
            submitted_email = form.get("email","").strip().lower()
            submitted_pin   = form.get("pin","").strip()
            if not EMERGENCY_PIN:
                self._html(render_emergency("Emergency login not configured."), 403)
                return
            pin_ok    = secrets.compare_digest(submitted_pin, EMERGENCY_PIN)
            domain    = submitted_email.split("@")[-1] if "@" in submitted_email else ""
            domain_ok = domain in ALLOWED_DOMAINS
            user      = get_user(submitted_email) if (pin_ok and domain_ok) else None
            if not (pin_ok and domain_ok and submitted_email and user):
                self._html(render_emergency("Invalid email or PIN."), 401)
                return
            token = create_session(submitted_email, user["name"], via="emergency")
            self._redirect(f"{BASE_URL}/?session={token}")
            return

        # ── Cron: daily sync ──────────────────────────────────────────────
        if path == "/api/cron/sync":
            threading.Thread(target=run_daily_sync, daemon=True).start()
            self._json({"ok": True, "message": "Daily sync started"})
            return

        # ── Cron: weekly insights ─────────────────────────────────────────
        if path == "/api/cron/insights":
            threading.Thread(target=run_weekly_insights, daemon=True).start()
            self._json({"ok": True, "message": "Weekly insights generation started"})
            return

        # ── API: Manual sync ──────────────────────────────────────────────
        if path == "/api/sync":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                body = {}
            since_str = body.get("since")
            try:
                if since_str:
                    since = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
                else:
                    since = datetime.now(timezone.utc) - timedelta(hours=25)
            except:
                since = datetime.now(timezone.utc) - timedelta(hours=25)
            conn = get_db()
            log_id = None
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO scout_sync_log (sync_type) VALUES ('manual') RETURNING id")
                    log_id = cur.fetchone()[0]
                    conn.commit()
                except:
                    conn.rollback()
                finally:
                    conn.close()
            threading.Thread(target=run_sync, args=(since, "manual", log_id), daemon=True).start()
            self._json({"ok": True, "message": "Sync started", "since": since.isoformat()})
            return

        # ── API: Backfill ─────────────────────────────────────────────────
        if path == "/api/sync/backfill":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            threading.Thread(target=run_backfill, daemon=True).start()
            self._json({"ok": True, "message": "Backfill started"})
            return

        # ── API: Refresh insights ─────────────────────────────────────────
        if path.startswith("/api/insights/") and path.endswith("/refresh"):
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            parts = path.split("/")
            dept  = parts[-2] if len(parts) >= 3 else ""
            if dept not in DEPT_CONFIGS:
                self._json({"error": "Unknown department"}, 404)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
                week_str = body.get("week")
            except:
                week_str = None
            try:
                week_start = date.fromisoformat(week_str) if week_str else get_week_bounds(0)[0]
            except:
                week_start = get_week_bounds(0)[0]
            week_end = week_start + timedelta(days=6)

            def do_refresh():
                result = generate_insights(dept, week_start, week_end)
                print(f"[Insights] Refresh done for {dept}: {'ok' if result else 'failed'}")

            threading.Thread(target=do_refresh, daemon=True).start()
            self._json({"ok": True, "message": f"Refreshing insights for {dept}"})
            return

        # ── API: Chat ─────────────────────────────────────────────────────
        if path == "/api/chat":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                self._json({"error": "Invalid JSON"}, 400)
                return
            messages = body.get("messages", [])
            if not messages:
                self._json({"error": "messages required"}, 400)
                return

            # Step 1: Ask Claude for SQL
            ai_response = chat_query(messages)

            # Step 2: Extract and run SQL if present
            sql = None
            if "<sql>" in ai_response and "</sql>" in ai_response:
                sql = ai_response.split("<sql>")[1].split("</sql>")[0].strip()

            if sql:
                rows, err = execute_chat_sql(sql)
                if err:
                    final_messages = messages + [
                        {"role": "assistant", "content": ai_response},
                        {"role": "user", "content": f"SQL error: {err}. Please try a different approach."}
                    ]
                    final_response = chat_query(final_messages)
                    self._json({"response": final_response, "sql": sql, "error": err})
                else:
                    result_summary = json.dumps(rows[:50], default=str) if rows else "No results found."
                    final_messages = messages + [
                        {"role": "assistant", "content": ai_response},
                        {"role": "user", "content": f"SQL results: {result_summary}\n\nPlease provide a clear, concise answer based on these results."}
                    ]
                    final_response = chat_query(final_messages)
                    self._json({"response": final_response, "sql": sql, "row_count": len(rows) if rows else 0})
            else:
                self._json({"response": ai_response, "sql": None})
            return

        # ── API: Add user ─────────────────────────────────────────────────
        if path == "/api/users":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                self._json({"error": "Invalid JSON"}, 400)
                return
            name  = body.get("name","").strip()
            email = body.get("email","").strip().lower()
            role  = body.get("role","viewer")
            dept  = body.get("department")
            if not name or not email:
                self._json({"error": "name and email required"}, 400)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO scout_users (name, email, role, department)
                    VALUES (%s, %s, %s, %s)
                """, (name, email, role, dept))
                conn.commit()
                self._json({"ok": True, "message": "User added"})
            except Exception as e:
                conn.rollback()
                self._json({"error": str(e)}, 400)
            finally:
                conn.close()
            return

        # ── API: Update user ──────────────────────────────────────────────
        if path == "/api/users/update":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                self._json({"error": "Invalid JSON"}, 400)
                return
            uid    = body.get("id")
            name   = body.get("name")
            role   = body.get("role")
            dept   = body.get("department")
            active = body.get("active")
            if not uid:
                self._json({"error": "id required"}, 400)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                # Admin guard: prevent removing last admin
                if role != "admin" or active == False:
                    cur.execute("SELECT email FROM scout_users WHERE id = %s", (uid,))
                    target = cur.fetchone()
                    if target:
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM scout_users
                            WHERE role = 'admin' AND active = true AND email != %s
                        """, (target["email"],))
                        other_admins = cur.fetchone()["cnt"]
                        cur.execute("SELECT role FROM scout_users WHERE id = %s", (uid,))
                        cur_role = cur.fetchone()
                        if cur_role and cur_role["role"] == "admin" and other_admins == 0:
                            self._json({"error": "Cannot remove the last admin. Assign another admin first."}, 400)
                            conn.close()
                            return
                updates = []
                vals    = []
                if name   is not None: updates.append("name = %s");       vals.append(name)
                if role   is not None: updates.append("role = %s");       vals.append(role)
                if dept   is not None: updates.append("department = %s"); vals.append(dept)
                if active is not None: updates.append("active = %s");     vals.append(active)
                if not updates:
                    self._json({"error": "Nothing to update"}, 400)
                    conn.close()
                    return
                updates.append("updated_at = now()")
                vals.append(uid)
                cur2 = conn.cursor()
                cur2.execute(f"UPDATE scout_users SET {', '.join(updates)} WHERE id = %s", vals)
                conn.commit()
                self._json({"ok": True})
            except Exception as e:
                conn.rollback()
                self._json({"error": str(e)}, 400)
            finally:
                conn.close()
            return

        # ── API: Delete user ──────────────────────────────────────────────
        if path == "/api/users/delete":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                self._json({"error": "Invalid JSON"}, 400)
                return
            uid = body.get("id")
            if not uid:
                self._json({"error": "id required"}, 400)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT email, role FROM scout_users WHERE id = %s", (uid,))
                target = cur.fetchone()
                if not target:
                    self._json({"error": "User not found"}, 404)
                    conn.close()
                    return
                if target["role"] == "admin":
                    cur.execute("""
                        SELECT COUNT(*) as cnt FROM scout_users
                        WHERE role = 'admin' AND active = true AND email != %s
                    """, (target["email"],))
                    if cur.fetchone()["cnt"] == 0:
                        self._json({"error": "Cannot delete the last admin."}, 400)
                        conn.close()
                        return
                cur2 = conn.cursor()
                cur2.execute("DELETE FROM scout_users WHERE id = %s", (uid,))
                conn.commit()
                self._json({"ok": True})
            except Exception as e:
                conn.rollback()
                self._json({"error": str(e)}, 400)
            finally:
                conn.close()
            return

        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    print("=" * 50)
    print("Scout v1.0.0")
    print(f"Port:      {PORT}")
    print(f"Base URL:  {BASE_URL}")
    print(f"Google SSO: {'configured' if GOOGLE_CLIENT_ID else 'NOT configured'}")
    print(f"Gorgias:   {'configured' if GORGIAS_USERNAME and GORGIAS_API_KEY else 'NOT configured'}")
    print(f"Anthropic: {'configured' if ANTHROPIC_KEY else 'NOT configured'}")
    print(f"Database:  {'configured' if DATABASE_URL else 'NOT configured'}")
    print(f"Admin seed: {ADMIN_SEED_EMAIL}")
    print("=" * 50)
    init_db()
    threading.Thread(target=run_backfill, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
