# ==============================================================================
#  server.py  --  SCOUT  (CSAT & support-intelligence app)
#  This is NOT Wingman. Wingman (the QA audit tool) has its own server.py with
#  /proxy and /gorgias endpoints. They share the filename but are different apps.
#  Scout: Gorgias -> Postgres sync, dashboard, CSAT, weekly Claude insights,
#         Asia/Manila weekly auto-sync, CSV export, and a cross-app ticket query.
# ==============================================================================
import os, json, urllib.request, urllib.error, secrets, hashlib, time, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, urlencode
from datetime import datetime, timezone, timedelta, date

# ── Timezone: Asia/Manila is the canonical TZ for all bucketing/display (matches Gorgias). ──
# Philippines observes no DST, so a fixed UTC+8 offset is exact year-round.
MANILA = timezone(timedelta(hours=8))
def now_manila():
    return datetime.now(MANILA)
def manila_week_bounds_iso(week_start):
    """Given a Manila Monday (date), return (start_iso, end_iso) as Manila-aware ISO datetimes
    (Mon 00:00 +08 .. next Mon 00:00 +08) for exact comparison against UTC TIMESTAMPTZ columns."""
    start = datetime(week_start.year, week_start.month, week_start.day, 0, 0, 0, tzinfo=MANILA)
    return start.isoformat(), (start + timedelta(days=7)).isoformat()

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
SCOUT_API_KEY        = os.environ.get("SCOUT_API_KEY", "")  # shared key for cross-app reads (e.g. Wingman)
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_insight_runs (
                department   TEXT NOT NULL,
                week_start   DATE NOT NULL,
                status       TEXT NOT NULL DEFAULT 'running',
                error        TEXT,
                updated_at   TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (department, week_start)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scout_csat (
                id             BIGINT PRIMARY KEY,
                ticket_id      BIGINT,
                score          INTEGER,
                comment        TEXT,
                created_date   TIMESTAMPTZ,
                customer_email TEXT,
                raw_json       JSONB,
                synced_at      TIMESTAMPTZ DEFAULT now()
            )
        """)
        # Seed admin user
        if ADMIN_SEED_EMAIL:
            cur.execute("""
                INSERT INTO scout_users (name, email, role, active)
                VALUES (%s, %s, 'admin', true)
                ON CONFLICT (email) DO NOTHING
            """, (ADMIN_SEED_EMAIL.split("@")[0].replace(".", " ").title(), ADMIN_SEED_EMAIL.lower()))
        # One-time fix: earlier syncs stored the ticket_url as /app/tickets/ (plural);
        # Gorgias's ticket view is /app/ticket/ (singular). Idempotent (no-op once clean).
        cur.execute("UPDATE scout_tickets SET ticket_url = REPLACE(ticket_url, '/app/tickets/', '/app/ticket/') WHERE ticket_url LIKE '%/app/tickets/%'")
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
        f"https://{GORGIAS_DOMAIN}/app/ticket/{t.get('id')}",   # Gorgias ticket view is singular /ticket/
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

# ── Sync debug log (in-memory ring buffer, surfaced via /api/sync/debug) ───────
SYNC_DEBUG = []
def slog(msg):
    try:
        SYNC_DEBUG.append(datetime.now(timezone.utc).strftime("%H:%M:%S") + " " + str(msg))
        if len(SYNC_DEBUG) > 600:
            del SYNC_DEBUG[:len(SYNC_DEBUG) - 600]
    except Exception:
        pass
    print(msg)

# ── Cooperative sync cancellation ──────────────────────────────────────────────
# Set by /api/sync/cancel; checked by every sync loop between pages/tickets and by
# the orchestrators between phases, so a running background sync stops at the next
# safe boundary. Each new user/scheduler-initiated run clears it before starting.
_sync_stop = threading.Event()


def run_sync(since_dt, sync_type="manual", log_id=None):
    """Fetch all tickets updated since since_dt, upsert into DB."""
    if not GORGIAS_USERNAME or not GORGIAS_API_KEY:
        slog("[Sync] Gorgias credentials not configured")
        return 0

    conn = get_db()
    if not conn:
        slog("[Sync] ERROR: no DB connection")
        return 0

    total = 0
    page_num = 0
    cursor = None
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00") if isinstance(since_dt, datetime) else since_dt
    slog(f"[Sync] START type={sync_type} since={since_str} log_id={log_id}")

    def update_log_progress(status="running", error=None):
        """Persist current progress to the sync log immediately."""
        if not log_id:
            return
        try:
            lc = get_db()
            if not lc:
                return
            lcur = lc.cursor()
            if error:
                lcur.execute("""
                    UPDATE scout_sync_log
                    SET finished_at = now(), tickets_synced = %s, status = %s, error = %s
                    WHERE id = %s
                """, (total, status, str(error)[:2000], log_id))
            else:
                lcur.execute("""
                    UPDATE scout_sync_log
                    SET tickets_synced = %s, status = %s
                    WHERE id = %s
                """, (total, status, log_id))
            lc.commit()
            lc.close()
        except Exception as le:
            slog(f"[Sync] WARN: could not update log: {le}")

    try:
        cur = conn.cursor()
        stop = False
        cancelled = False
        while not stop:
            if _sync_stop.is_set():
                slog("[Sync] cancel requested — stopping at page boundary")
                cancelled = True
                break
            page_num += 1
            params = {"limit": 100, "order_by": "updated_datetime:desc"}
            if cursor:
                params["cursor"] = cursor

            # Fetch with retry on transient errors (429, 5xx, network)
            data = None
            for attempt in range(1, 4):
                try:
                    data = gorgias_request("/tickets", params)
                    break
                except urllib.error.HTTPError as he:
                    body = ""
                    try: body = he.read().decode()[:300]
                    except: pass
                    slog(f"[Sync] page={page_num} HTTP {he.code} attempt {attempt}/3: {body}")
                    if he.code == 429:
                        wait = 5 * attempt
                        slog(f"[Sync] rate limited, waiting {wait}s")
                        time.sleep(wait)
                    elif he.code >= 500:
                        time.sleep(3 * attempt)
                    else:
                        raise
                except Exception as ne:
                    slog(f"[Sync] page={page_num} network error attempt {attempt}/3: {ne}")
                    time.sleep(3 * attempt)
            if data is None:
                raise Exception(f"Failed to fetch page {page_num} after 3 attempts")

            tickets = data.get("data", [])
            slog(f"[Sync] page={page_num} fetched={len(tickets)} total_so_far={total}")
            if not tickets:
                slog(f"[Sync] page={page_num} empty, stopping")
                break

            page_upserted = 0
            for t in tickets:
                updated = t.get("updated_datetime", "") or ""
                if updated < since_str:
                    slog(f"[Sync] reached cutoff at ticket {t.get('id')} ({updated} < {since_str}), stopping")
                    stop = True
                    break
                try:
                    upsert_ticket(cur, t)
                    total += 1
                    page_upserted += 1
                except Exception as ue:
                    slog(f"[Sync] WARN: upsert failed for ticket {t.get('id')}: {ue}")
                    # roll back just this row's failed statement, keep going
                    conn.rollback()
                    cur = conn.cursor()

            conn.commit()
            update_log_progress("running")
            slog(f"[Sync] page={page_num} upserted={page_upserted} committed, total={total}")

            if stop:
                break

            meta = data.get("meta", {})
            next_cursor = meta.get("next_cursor")
            if not next_cursor:
                slog(f"[Sync] no next_cursor after page {page_num}, reached end")
                break

            # Guard against Gorgias deep-pagination limits
            if page_num >= 5000:
                slog(f"[Sync] WARN: hit page limit (5000 pages = 500k tickets), stopping defensively")
                break

            cursor = next_cursor
            time.sleep(0.15)  # gentle rate limiting

        conn.commit()
        if cancelled:
            update_log_progress("cancelled", error="Cancelled by user")
            slog(f"[Sync] CANCELLED type={sync_type} total={total} pages={page_num}")
        else:
            update_log_progress("success")
            slog(f"[Sync] DONE type={sync_type} total={total} pages={page_num}")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        slog(f"[Sync] FATAL error after {total} tickets, page {page_num}: {e}")
        print(tb)
        try:
            conn.rollback()
        except:
            pass
        update_log_progress("error", error=f"{e} (after {total} tickets, page {page_num})")
    finally:
        try:
            conn.close()
        except:
            pass
    return total

def run_backfill(force=False, start=None):
    """Historical backfill of tickets + CSAT. `start` is a date (default: 1st of the previous month).
    Tickets sync per page (each page committed before the next), then CSAT for the same window.
    Intentionally does NOT run the heavy transcript top-up — transcripts are pulled on demand by the
    chat agent or via a separate bounded pass, so a large historical pull can't stall on that phase.
    force=True bypasses the 'skip if DB already has tickets' guard."""
    if start is None:
        today = date.today()
        if today.month == 1:
            start = date(today.year - 1, 12, 1)
        else:
            start = date(today.year, today.month - 1, 1)
    since = datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=timezone.utc)
    _sync_stop.clear()

    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM scout_tickets")
        count = cur.fetchone()[0]
        conn.close()
        if count > 0 and not force:
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
    if _sync_stop.is_set():
        slog("[Backfill] cancelled — skipping CSAT phase")
        return
    # Sync CSAT for the same window as the ticket backfill.
    slog("[CSAT] Starting post-backfill CSAT sync...")
    run_csat_sync(since_dt=since)

def run_full_sync(since_dt, sync_type="manual", log_id=None):
    """Full sync: tickets updated since since_dt, then transcript top-up, then CSAT."""
    _sync_stop.clear()
    slog(f"[Sync] Full sync ({sync_type}) since {since_dt.isoformat()}")
    run_sync(since_dt, sync_type=sync_type, log_id=log_id)
    if _sync_stop.is_set():
        slog("[Sync] cancelled — skipping transcript and CSAT phases")
        return
    slog("[Transcripts] post-sync transcript top-up...")
    try:
        run_transcript_backfill(max_fetches=100000)   # cover the whole prior week, not just 2000
    except Exception as e:
        slog(f"[Transcripts] error: {e}")
    if _sync_stop.is_set():
        slog("[Sync] cancelled — skipping CSAT phase")
        return
    slog("[CSAT] post-sync CSAT top-up...")
    try:
        run_csat_sync(since_dt=since_dt)
    except Exception as e:
        slog(f"[CSAT] error: {e}")

# ── Weekly scheduler: Monday 00:00 Asia/Manila full sync (in-process, no external cron) ──
def _weekly_run_logged_since(dt_utc):
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM scout_sync_log WHERE sync_type='weekly' AND started_at >= %s", (dt_utc,))
        return (cur.fetchone()[0] or 0) > 0
    except Exception:
        return False
    finally:
        conn.close()

def _run_weekly_sync():
    since = datetime.now(timezone.utc) - timedelta(days=8)  # cover prior week + buffer; upsert is idempotent
    conn = get_db(); log_id = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO scout_sync_log (sync_type) VALUES ('weekly') RETURNING id")
            log_id = cur.fetchone()[0]; conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()
    run_full_sync(since, sync_type="weekly", log_id=log_id)

def _next_monday_manila(now):
    days_ahead = (7 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=7)
    return target

def weekly_scheduler_loop():
    # Startup catch-up: if past this week's Manila Monday and no weekly sync logged since then, run once.
    try:
        now = now_manila()
        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        if not _weekly_run_logged_since(this_monday.astimezone(timezone.utc)):
            slog("[Scheduler] Startup catch-up: no weekly sync logged since Monday, running now")
            _run_weekly_sync()
    except Exception as e:
        slog(f"[Scheduler] catch-up error: {e}")
    while True:
        try:
            now = now_manila()
            target = _next_monday_manila(now)
            secs = (target - now).total_seconds()
            slog(f"[Scheduler] Next weekly full sync at {target.isoformat()} (in {int(secs)}s)")
            time.sleep(max(secs, 60))
            slog("[Scheduler] Monday 00:00 Asia/Manila reached, running weekly full sync")
            _run_weekly_sync()
            time.sleep(120)  # guard against double-fire within the same minute
        except Exception as e:
            slog(f"[Scheduler] loop error: {e}")
            time.sleep(300)

def _daily_run_logged_since(dt_utc):
    """True if any ticket sync (daily/manual/weekly) has been logged since dt_utc —
    lets the in-process daily loop coexist with an external Railway cron without double-firing."""
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM scout_sync_log WHERE sync_type IN ('daily','manual','weekly') AND started_at >= %s", (dt_utc,))
        return (cur.fetchone()[0] or 0) > 0
    except Exception:
        return False
    finally:
        conn.close()

def daily_scheduler_loop():
    """Runs run_daily_sync at 00:00 Asia/Manila every day, in-process (no external cron required)."""
    # Startup catch-up: if nothing has synced since today's Manila midnight, run once now.
    try:
        now = now_manila()
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if not _daily_run_logged_since(today_midnight.astimezone(timezone.utc)):
            slog("[Scheduler] Startup catch-up: no sync logged since 00:00 Manila, running daily sync now")
            run_daily_sync()
    except Exception as e:
        slog(f"[Scheduler] daily catch-up error: {e}")
    while True:
        try:
            now = now_manila()
            target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            secs = (target - now).total_seconds()
            slog(f"[Scheduler] Next daily sync at {target.isoformat()} (in {int(secs)}s)")
            time.sleep(max(secs, 60))
            # Skip if an external cron / manual run already covered today.
            midnight = now_manila().replace(hour=0, minute=0, second=0, microsecond=0)
            if _daily_run_logged_since(midnight.astimezone(timezone.utc)):
                slog("[Scheduler] Daily sync already logged since midnight, skipping in-process run")
            else:
                slog("[Scheduler] 00:00 Asia/Manila reached, running daily sync")
                run_daily_sync()
            time.sleep(120)  # guard against double-fire within the same minute
        except Exception as e:
            slog(f"[Scheduler] daily loop error: {e}")
            time.sleep(300)

def run_daily_sync():
    """Daily sync — tickets updated in last 25 hours (buffer for timezone drift)."""
    _sync_stop.clear()
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
    slog("[Sync] Running daily sync...")
    run_sync(since, sync_type="daily", log_id=log_id)
    if _sync_stop.is_set():
        slog("[Sync] daily cancelled — skipping transcript and CSAT phases")
        return
    # After syncing tickets, top up transcripts for the recent window (background).
    slog("[Transcripts] Starting post-sync transcript top-up...")
    run_transcript_backfill(max_fetches=2000)
    if _sync_stop.is_set():
        slog("[Sync] daily cancelled — skipping CSAT phase")
        return
    # Sync recent CSAT responses (last ~30 days is plenty for daily top-up).
    slog("[CSAT] Starting post-sync CSAT top-up...")
    run_csat_sync(since_dt=datetime.now(timezone.utc) - timedelta(days=30))

def get_transcript_window_start():
    """Monday of 4 weeks ago (start of a 4-full-week window), Manila-aligned."""
    today = now_manila().date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(weeks=3)  # this week + 3 prior = 4 weeks

def run_transcript_backfill(max_fetches=2000, start=None):
    """
    Fetch transcripts for tickets from `start` (a date) to today; defaults to the last 4 weeks
    (Monday-aligned) when start is None.
    - Closed tickets: fetch once (transcript_fetched=true and not empty → skip on later runs).
    - Open tickets: re-fetch each run (still accumulating messages).
    Commits every 100 tickets and caps at max_fetches per run, so a large historical pull is
    bounded and RESUMABLE — re-run it and it continues with whatever still lacks a transcript.
    Designed to run slowly in the background.
    """
    window_start = start if start is not None else get_transcript_window_start()
    ws = window_start.isoformat()
    conn = get_db()
    if not conn:
        slog("[Transcripts] No DB connection")
        return 0

    log_id = None
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO scout_sync_log (sync_type, status) VALUES ('transcripts','running') RETURNING id")
        log_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        slog(f"[Transcripts] could not create log: {e}")
        try: conn.rollback()
        except: pass

    fetched = 0
    try:
        import psycopg2.extras
        # Candidates: tickets in window that are either open (always re-fetch)
        # or closed without a transcript yet.
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ticket_id, status FROM scout_tickets
            WHERE created_date >= %s
              AND (
                    status = 'open'
                 OR transcript_fetched = false
                 OR transcript IS NULL
                 OR transcript = ''
              )
            ORDER BY created_date DESC
        """, (ws,))
        candidates = cur.fetchall()
        slog(f"[Transcripts] window from {ws}: {len(candidates)} candidates (cap {max_fetches})")

        ucur = conn.cursor()
        cancelled = False
        for row in candidates:
            if _sync_stop.is_set():
                slog("[Transcripts] cancel requested — stopping")
                cancelled = True
                break
            if fetched >= max_fetches:
                slog(f"[Transcripts] hit per-run cap ({max_fetches}), will continue next run")
                break
            tid = row["ticket_id"]
            transcript = fetch_messages_for_ticket(tid)
            ucur.execute("""
                UPDATE scout_tickets SET transcript = %s, transcript_fetched = true
                WHERE ticket_id = %s
            """, (transcript or "", tid))
            fetched += 1
            if fetched % 100 == 0:
                conn.commit()
                slog(f"[Transcripts] {fetched} fetched...")
                time.sleep(1)  # breather every 100
            else:
                time.sleep(0.15)  # gentle pace
        conn.commit()

        if log_id:
            lc = get_db()
            if lc:
                lcur = lc.cursor()
                lcur.execute("""
                    UPDATE scout_sync_log SET finished_at = now(), tickets_synced = %s, status = %s, error = %s
                    WHERE id = %s
                """, (fetched, "cancelled" if cancelled else "success",
                      "Cancelled by user" if cancelled else None, log_id))
                lc.commit(); lc.close()
        slog(f"[Transcripts] {'CANCELLED' if cancelled else 'DONE'} — {fetched} transcripts fetched")
    except Exception as e:
        import traceback
        slog(f"[Transcripts] ERROR after {fetched}: {e}")
        print(traceback.format_exc())
        try: conn.rollback()
        except: pass
        if log_id:
            try:
                lc = get_db()
                if lc:
                    lcur = lc.cursor()
                    lcur.execute("""
                        UPDATE scout_sync_log SET finished_at = now(), tickets_synced = %s, status = 'error', error = %s
                        WHERE id = %s
                    """, (fetched, str(e)[:1000], log_id))
                    lc.commit(); lc.close()
            except: pass
    finally:
        try: conn.close()
        except: pass
    return fetched

def run_csat_sync(since_dt=None, log_id=None):
    """
    Sync Gorgias CSAT (satisfaction survey) responses into scout_csat.
    Aligned to the ticket data range. Upserts on survey id, links by ticket_id.
    """
    if not GORGIAS_USERNAME or not GORGIAS_API_KEY:
        slog("[CSAT] Gorgias credentials not configured")
        return 0
    conn = get_db()
    if not conn:
        return 0

    # Default: align with ticket backfill window (1st of previous month)
    if since_dt is None:
        today = date.today()
        if today.month == 1:
            start = date(today.year - 1, 12, 1)
        else:
            start = date(today.year, today.month - 1, 1)
        since_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    total = 0
    cursor = None
    page = 0

    # Log CSAT runs to scout_sync_log so they appear in the sync history.
    if log_id is None:
        try:
            lc = get_db()
            if lc:
                lcur = lc.cursor()
                lcur.execute("INSERT INTO scout_sync_log (sync_type) VALUES ('csat') RETURNING id")
                log_id = lcur.fetchone()[0]
                lc.commit(); lc.close()
        except Exception as le:
            slog(f"[CSAT] WARN: could not create sync log: {le}")

    def update_csat_log(status="running", error=None):
        if not log_id:
            return
        try:
            lc = get_db()
            if not lc:
                return
            lcur = lc.cursor()
            if error:
                lcur.execute("UPDATE scout_sync_log SET finished_at=now(), tickets_synced=%s, status=%s, error=%s WHERE id=%s",
                             (total, status, str(error)[:2000], log_id))
            elif status == "success":
                lcur.execute("UPDATE scout_sync_log SET finished_at=now(), tickets_synced=%s, status=%s WHERE id=%s",
                             (total, status, log_id))
            else:
                lcur.execute("UPDATE scout_sync_log SET tickets_synced=%s, status=%s WHERE id=%s",
                             (total, status, log_id))
            lc.commit(); lc.close()
        except Exception as le:
            slog(f"[CSAT] WARN: could not update sync log: {le}")

    slog(f"[CSAT] START since={since_str} log_id={log_id}")
    try:
        cur = conn.cursor()
        stop = False
        cancelled = False
        while not stop:
            if _sync_stop.is_set():
                slog("[CSAT] cancel requested — stopping at page boundary")
                cancelled = True
                break
            page += 1
            params = {"limit": 100, "order_by": "created_datetime:desc"}
            if cursor:
                params["cursor"] = cursor
            data = None
            for attempt in range(1, 4):
                try:
                    data = gorgias_request("/satisfaction-surveys", params)
                    break
                except urllib.error.HTTPError as he:
                    if he.code == 429:
                        time.sleep(5 * attempt)
                    elif he.code == 404:
                        slog("[CSAT] /satisfaction-surveys not available (404) — skipping CSAT sync")
                        conn.close()
                        return 0
                    elif he.code >= 500:
                        time.sleep(3 * attempt)
                    else:
                        raise
                except Exception as ne:
                    slog(f"[CSAT] page={page} error attempt {attempt}: {ne}")
                    time.sleep(3 * attempt)
            if data is None:
                raise Exception(f"Failed to fetch CSAT page {page}")

            surveys = data.get("data", [])
            if not surveys:
                break

            for s in surveys:
                created = s.get("created_datetime", "") or ""
                if created and created < since_str:
                    stop = True
                    break
                # Gorgias survey: score, body/comment, ticket_id, customer
                sid = s.get("id")
                tid = s.get("ticket_id") or (s.get("ticket") or {}).get("id")
                score = s.get("score")
                comment = s.get("body_text") or s.get("comment") or s.get("body") or ""
                cust = s.get("customer") or {}
                cur.execute("""
                    INSERT INTO scout_csat (id, ticket_id, score, comment, created_date, customer_email, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        score = EXCLUDED.score, comment = EXCLUDED.comment,
                        ticket_id = EXCLUDED.ticket_id, raw_json = EXCLUDED.raw_json
                """, (sid, tid, score, comment[:5000], created or None, cust.get("email",""), json.dumps(s)))
                total += 1

            conn.commit()
            meta = data.get("meta", {})
            cursor = meta.get("next_cursor")
            slog(f"[CSAT] page={page} fetched={len(surveys)} total={total} more={'yes' if cursor else 'no'}")
            update_csat_log("running")
            if not cursor or stop:
                break
            if page >= 2000:
                break
            time.sleep(0.15)
        conn.commit()
        if cancelled:
            slog(f"[CSAT] CANCELLED — {total} surveys synced")
            update_csat_log("cancelled", error="Cancelled by user")
        else:
            slog(f"[CSAT] DONE — {total} surveys synced")
            update_csat_log("success")
    except Exception as e:
        slog(f"[CSAT] ERROR after {total}: {e}")
        update_csat_log("error", error=e)
        try: conn.rollback()
        except: pass
    finally:
        try: conn.close()
        except: pass
    return total

# ── Insight generation ─────────────────────────────────────────────────────────
# Product return/replacement rate threshold for flagging (changeable). 0.05 = 5%.
PRODUCT_RETURN_RATE_THRESHOLD = float(os.environ.get("PRODUCT_RETURN_RATE_THRESHOLD", "0.05"))

DEPT_CONFIGS = {
    "cx": {
        "name": "Customer Experience",
        "filters": {},
        "system": """You are an analytics expert reviewing CX support data for Freedom Grooming (Freebird), a grooming and electric shaver subscription company.
Analyze this week's ticket data and provide insights for the CX leadership team.
Focus on: agent performance patterns, ticket volume trends, repeat contact signals, resolution quality, coaching opportunities, and operational bottlenecks.
Structure your response with clear sections: Key Metrics, CSAT Highlights, Notable Patterns, Risks & Flags, Recommended Actions.
The CSAT Highlights section must include:
- Headline metrics: Avg CSAT score (out of 5), Surveys Sent, Response Rate %, and the score distribution (how many 1/2/3/4/5-star).
- Exactly 3 five-star verbatims worth sharing with the team — the most specific and meaningful ones, not generic one-liners — each attributed with the customer name and ticket ID exactly as provided in the data (format: -- Customer Name, Ticket #12345). If fewer than 3 exist, include all available ones.
- Insights worth discussing: 3 to 5 short, specific observations the CX team should actually talk about. Go beyond restating the numbers. Identify recurring themes in BOTH the praise and the low-score (1-2 star) feedback, what appears to be driving satisfaction up or down, any concerning or repeated complaints, and where coaching or a process change could move the score. Reference the relevant ticket numbers. Then finish with 2 to 3 concrete discussion questions or talking points for the next CSAT review. If there is little low-score feedback, say so plainly rather than inventing problems.
Be specific and data-driven. Reference actual numbers from the data provided."""
    },
    "growth": {
        "name": "Growth",
        "filters": {},
        "system": """You are an analytics expert reviewing customer support data, CSAT survey responses, and customer verbatims for Freedom Grooming (Freebird), a grooming and electric shaver subscription company.
Analyze this week's data and surface insights for the Growth team (marketing & acquisition).

Answer these specific questions, drawing primarily from ticket transcripts and CSAT comments (the customers' own words), not just structured tags:
1. Which product features did people purchase our product for? (What features/benefits do customers mention as their reason for buying?)
2. What customer use cases emerge that we could lean into for marketing? (How/where/why are customers actually using the product?)
3. Was there anything customers saw or heard that especially influenced their purchase? (Ads, referrals, reviews, social, word-of-mouth, specific claims.)
4. What themes emerge across CSAT comments and customer sentiment? (Positive drivers worth amplifying, and any recurring praise.)

Structure your response with clear sections: Purchase Motivations, Marketable Use Cases, Purchase Influences, Review & Sentiment Themes, Recommended Actions.
Quote representative customer phrasing where it illustrates a theme (but never expose names or emails). Focus on patterns across many customers, not one-offs.
Note: Judge.me review integration is planned for a future version — for now, base review/sentiment themes on CSAT comments and support transcripts."""
    },
    "product": {
        "name": "Product",
        "filters": {"contact_reason_l1": ["Troubleshooting", "Order Issue", "Order Status", "Update Order"]},
        "system": """You are an analytics expert reviewing product quality signals for Freedom Grooming (Freebird), a grooming and electric shaver subscription company.
Analyze this week's data for the Product team. IMPORTANT: identify meaningful TRENDS and patterns across many tickets — do NOT dwell on individual customer cases.

Address these specifically:
1. Quality-related return and replacement RATES BY PRODUCT, including the trend versus prior months, and flag any product whose return/replacement rate exceeds the established threshold (provided in the data).
2. Primary return and warranty REASON CODES — the leading drivers of dissatisfaction and product failures.
3. Key Voice-of-Customer (VOC) themes from complaints, reviews, and support contacts — emerging issues, recurring pain points, areas where performance may be deteriorating.
4. Any significant MONTH-OVER-MONTH increases in return rates, replacement rates, complaint volume, or specific failure modes.

Structure your response with clear sections: Return & Replacement Rates by Product, Reason Code Analysis, VOC Themes, Month-over-Month Movement, Flags & Recommended Actions.

NOTE ON DATA QUALITY: The structured resolution/reason fields are human-entered by agents and may be incomplete or inaccurate. Where transcripts are available, cross-check the structured data against what customers actually describe, and note any discrepancies you find. Be data-driven and reference actual numbers."""
    }
}

def get_week_bounds(week_offset=0):
    """Return (week_start, week_end) as date objects, Manila-aligned. week_offset=0 is previous Mon-Sun."""
    today = now_manila().date()
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7 * (1 + week_offset))
    last_sunday  = last_monday + timedelta(days=6)
    return last_monday, last_sunday

def _set_insight_status(dept, week_start, status, error=None):
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scout_insight_runs (department, week_start, status, error, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (department, week_start) DO UPDATE SET
                status = EXCLUDED.status, error = EXCLUDED.error, updated_at = now()
        """, (dept, week_start, status, str(error)[:1000] if error else None))
        conn.commit()
    except Exception as e:
        print(f"[Insights] status update failed: {e}")
        conn.rollback()
    finally:
        conn.close()

def generate_insights(dept, week_start, week_end):
    """Generate Claude insights for a department and week. Returns insight text."""
    config = DEPT_CONFIGS.get(dept)
    if not config:
        return None

    _set_insight_status(dept, week_start, "running")
    conn = get_db()
    if not conn:
        _set_insight_status(dept, week_start, "error", "No DB connection")
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

        # ── Department-specific data ──────────────────────────────────────
        dept_data = ""

        if dept == "cx":
            # CSAT stats for the week: sent, responded, response rate, avg score, 5-star verbatims
            try:
                cur.execute("""
                    SELECT
                        COUNT(*)                                                      AS surveys_sent,
                        COUNT(*) FILTER (WHERE score IS NOT NULL)                    AS surveys_responded,
                        ROUND(AVG(score) FILTER (WHERE score IS NOT NULL)::numeric, 2) AS avg_score
                    FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s
                """, (ws, we))
                csat_stats  = dict(cur.fetchone() or {})
                sent        = int(csat_stats.get("surveys_sent") or 0)
                responded   = int(csat_stats.get("surveys_responded") or 0)
                avg_score   = csat_stats.get("avg_score")
                rate        = round((responded / sent * 100), 1) if sent else 0
                dept_data  += f"\n\nCSAT SUMMARY ({week_start} to {week_end}):"
                dept_data  += f"\n- Surveys Sent: {sent}"
                dept_data  += f"\n- Surveys Responded: {responded}"
                dept_data  += f"\n- Response Rate: {rate}%"
                dept_data  += f"\n- Avg CSAT Score: {float(avg_score):.2f} / 5" if avg_score is not None else "\n- Avg CSAT Score: N/A"

                # Five-star verbatims — pick most substantive ones (longest first, capped at 5 candidates)
                cur.execute("""
                    SELECT c.ticket_id,
                           c.comment,
                           COALESCE(NULLIF(t.customer_name, ''), NULLIF(c.customer_email, ''), 'Unknown') AS customer_name
                    FROM scout_csat c
                    LEFT JOIN scout_tickets t ON t.ticket_id = c.ticket_id
                    WHERE c.created_date >= %s AND c.created_date < %s
                      AND c.score = 5
                      AND c.comment IS NOT NULL AND c.comment != ''
                    ORDER BY LENGTH(c.comment) DESC
                    LIMIT 5
                """, (ws, we))
                five_star = [dict(r) for r in cur.fetchall()]
                if five_star:
                    dept_data += "\n\n5-STAR VERBATIMS (pick the 3 most meaningful for CSAT Highlights; keep the customer name and ticket ID with each one you choose):\n"
                    for v in five_star:
                        dept_data += f"- \"{(v.get('comment') or '')[:400]}\" -- {v.get('customer_name') or 'Unknown'} (Ticket #{v.get('ticket_id')})\n"
                else:
                    dept_data += "\n\n5-STAR VERBATIMS: None this week."

                # Score distribution — helps the model see what is dragging the average
                cur.execute("""
                    SELECT score, COUNT(*) AS cnt FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s AND score IS NOT NULL
                    GROUP BY score ORDER BY score
                """, (ws, we))
                dist = {int(r["score"]): int(r["cnt"]) for r in cur.fetchall()}
                if dist:
                    tot = sum(dist.values())
                    low = dist.get(1, 0) + dist.get(2, 0)
                    dept_data += "\n\nCSAT SCORE DISTRIBUTION (this week): " + ", ".join(f"{s}-star: {dist.get(s, 0)}" for s in range(5, 0, -1))
                    dept_data += f"\n- Low scores (1-2 star): {low} of {tot} responses ({round(low / tot * 100, 1) if tot else 0}%)"

                # Low-score (detractor) verbatims — the most discussion-worthy signal
                cur.execute("""
                    SELECT c.ticket_id, c.score, c.comment,
                           COALESCE(NULLIF(t.customer_name, ''), NULLIF(c.customer_email, ''), 'Unknown') AS customer_name
                    FROM scout_csat c
                    LEFT JOIN scout_tickets t ON t.ticket_id = c.ticket_id
                    WHERE c.created_date >= %s AND c.created_date < %s
                      AND c.score <= 2
                      AND c.comment IS NOT NULL AND c.comment != ''
                    ORDER BY c.score ASC, LENGTH(c.comment) DESC
                    LIMIT 8
                """, (ws, we))
                detractors = [dict(r) for r in cur.fetchall()]
                if detractors:
                    dept_data += "\n\nLOW-SCORE VERBATIMS (1-2 star -- analyze for recurring themes, root causes, and coaching/process gaps):\n"
                    for v in detractors:
                        dept_data += f"- [{v.get('score')}-star] \"{(v.get('comment') or '')[:400]}\" -- {v.get('customer_name') or 'Unknown'} (Ticket #{v.get('ticket_id')})\n"
                else:
                    dept_data += "\n\nLOW-SCORE VERBATIMS: None with comments this week."
            except Exception as ce:
                print(f"[Insights] CX CSAT pull failed: {ce}")

        if dept == "growth":
            # CSAT responses for the week (score + comment), and overall distribution
            try:
                cur.execute("""
                    SELECT score, comment FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s
                      AND comment IS NOT NULL AND comment != ''
                    ORDER BY created_date DESC LIMIT 60
                """, (ws, we))
                csat_rows = [dict(r) for r in cur.fetchall()]
                cur.execute("""
                    SELECT score, COUNT(*) as cnt FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s AND score IS NOT NULL
                    GROUP BY score ORDER BY score
                """, (ws, we))
                csat_dist = [dict(r) for r in cur.fetchall()]
                dept_data += f"\n\nCSAT SCORE DISTRIBUTION:\n{json.dumps(csat_dist, indent=2)}"
                dept_data += "\n\nCSAT COMMENTS (customer verbatims):\n"
                for c in csat_rows:
                    if c.get("comment"):
                        dept_data += f"- [score {c.get('score')}] {c['comment'][:300]}\n"
            except Exception as ce:
                print(f"[Insights] CSAT pull failed: {ce}")

        if dept == "product":
            # Return/replacement rate by product for this period + prior month
            try:
                month_start = (week_start.replace(day=1))
                prev_month_end = month_start
                prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

                def product_rates(d0, d1):
                    cur.execute("""
                        SELECT product_l1,
                               COUNT(*) AS total,
                               COUNT(*) FILTER (
                                 WHERE ticket_resolution_l1 ILIKE '%return%'
                                    OR ticket_resolution_l1 ILIKE '%replace%'
                                    OR additional_resolution_l1 ILIKE '%return%'
                                    OR additional_resolution_l1 ILIKE '%replace%'
                                    OR contact_reason_l1 ILIKE '%return%'
                               ) AS returns_replacements
                        FROM scout_tickets
                        WHERE created_date >= %s AND created_date < %s
                          AND product_l1 IS NOT NULL AND product_l1 != ''
                        GROUP BY product_l1
                        HAVING COUNT(*) >= 5
                        ORDER BY total DESC LIMIT 25
                    """, (d0.isoformat(), d1.isoformat()))
                    out = []
                    for r in cur.fetchall():
                        rr = dict(r)
                        rr["rate"] = round((rr["returns_replacements"]/rr["total"]) if rr["total"] else 0, 4)
                        rr["exceeds_threshold"] = rr["rate"] > PRODUCT_RETURN_RATE_THRESHOLD
                        out.append(rr)
                    return out

                this_month = product_rates(month_start, date.fromisoformat(we))
                prev_month  = product_rates(prev_month_start, prev_month_end)
                dept_data += f"\n\nRETURN/REPLACEMENT RATE THRESHOLD: {PRODUCT_RETURN_RATE_THRESHOLD:.0%} (products above this should be flagged)"
                dept_data += f"\n\nPRODUCT RETURN/REPLACEMENT RATES — CURRENT MONTH (from {month_start}):\n{json.dumps(this_month, indent=2, default=str)}"
                dept_data += f"\n\nPRODUCT RETURN/REPLACEMENT RATES — PRIOR MONTH ({prev_month_start} to {prev_month_end}) for MoM comparison:\n{json.dumps(prev_month, indent=2, default=str)}"

                # Reason code breakdown
                cur.execute("""
                    SELECT ticket_resolution_l1, ticket_resolution_l2, COUNT(*) as cnt
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                      AND (ticket_resolution_l1 ILIKE '%return%' OR ticket_resolution_l1 ILIKE '%replace%'
                           OR ticket_resolution_l1 ILIKE '%warrant%')
                    GROUP BY ticket_resolution_l1, ticket_resolution_l2 ORDER BY cnt DESC LIMIT 20
                """, (ws, we))
                reason_codes = [dict(r) for r in cur.fetchall()]
                dept_data += f"\n\nRETURN/WARRANTY REASON CODES (this week):\n{json.dumps(reason_codes, indent=2)}"
            except Exception as pe:
                print(f"[Insights] Product rate calc failed: {pe}")

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
{dept_data}

SAMPLE TICKETS WITH TRANSCRIPTS:
"""
        for st in sample_tickets:
            prompt += f"\n---\nTicket #{st['ticket_id']} | {st.get('contact_reason_l1','')} > {st.get('contact_reason_l2','')} | Agent: {st.get('agent','')} | Status: {st.get('status','')}\n"
            if st.get("transcript"):
                prompt += f"Transcript (excerpt):\n{st['transcript'][:800]}\n"

        # Call Claude
        bot_context = ("\n\nIMPORTANT CONTEXT: 'Jamie' is our automated AI chatbot (Yuma), NOT a human agent. "
                       "When analyzing agent performance, treat Jamie separately as the bot — do not rank it "
                       "against human agents or praise it as a top performer. Human agents are everyone else.")
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 4000,
            "system": config["system"] + bot_context,
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
        try:
            resp = urllib.request.urlopen(req, timeout=180)
            result = json.loads(resp.read())
        except urllib.error.HTTPError as he:
            body = ""
            try: body = he.read().decode()[:500]
            except: pass
            raise Exception(f"Claude API HTTP {he.code}: {body}")
        content = result.get("content", [])
        text = " ".join(c.get("text","") for c in content if c.get("type") == "text")
        if not text.strip():
            raise Exception(f"Claude returned empty content (stop_reason: {result.get('stop_reason')})")

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
        conn.close()
        _set_insight_status(dept, week_start, "success")
        return text

    except Exception as e:
        print(f"[Insights] Error for {dept}: {e}")
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        _set_insight_status(dept, week_start, "error", str(e))
        return None

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
  tags (text), message_count (integer), transcript (text), transcript_fetched (boolean),
  first_seen_at (timestamptz), last_updated_at (timestamptz)

Common contact_reason_l1 values: Cancel, Order Issue, Order Status, Subscription, Troubleshooting, Other, Update Order
Common status values: open, closed

IMPORTANT CONTEXT:
- 'Jamie' in the agent column is our automated AI chatbot (Yuma), NOT a human agent. When asked about
  agent performance or human agents, exclude Jamie (agent != 'Jamie') unless the user specifically asks about the bot.
- Structured fields (contact_reason, product, resolution) are tagged by agents and may be incomplete or generic.
  For nuanced questions — like the real reasons behind cancellations, customer sentiment, or why people are
  unhappy — the structured fields often aren't enough. The actual answer lives in the 'transcript' column
  (the customer's own words). Prefer reading transcripts for "why" questions.
- transcript may be empty if transcript_fetched = false. You can request transcripts to be fetched (see below).

Query rules:
- Use ILIKE for case-insensitive text matching.
- Always include LIMIT 100 unless the user asks for counts/aggregates.
- Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or ALTER.
"""

def fetch_transcripts_for_ids(ticket_ids):
    """On-demand: fetch + cache transcripts for specific ticket IDs that don't have them yet."""
    if not ticket_ids:
        return 0
    conn = get_db()
    if not conn:
        return 0
    fetched = 0
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Only fetch ones we don't already have
        placeholders = ",".join(["%s"] * len(ticket_ids))
        cur.execute(f"""
            SELECT ticket_id FROM scout_tickets
            WHERE ticket_id IN ({placeholders})
              AND (transcript_fetched = false OR transcript IS NULL OR transcript = '')
        """, tuple(ticket_ids))
        missing = [r["ticket_id"] for r in cur.fetchall()]
        ucur = conn.cursor()
        for tid in missing[:80]:  # cap per request to control latency/cost
            transcript = fetch_messages_for_ticket(tid)
            ucur.execute("""
                UPDATE scout_tickets SET transcript = %s, transcript_fetched = true
                WHERE ticket_id = %s
            """, (transcript or "", tid))
            fetched += 1
        conn.commit()
    except Exception as e:
        print(f"[Chat] Transcript fetch error: {e}")
        conn.rollback()
    finally:
        conn.close()
    return fetched

def chat_query(messages):
    """Multi-turn chat agent. messages = [{role, content}]. Returns answer text."""
    today = date.today()
    # Compute current week (Mon-Sun) and last week for relative date references
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    last_sunday = this_monday - timedelta(days=1)
    system = f"""You are Scout, an expert customer-support and business analyst for Freedom Grooming (Freebird), a grooming and electric shaver subscription company. You have access to the company's support ticket database, CSAT survey responses, and customer transcripts.

You are a knowledgeable analyst FIRST and a SQL engine second. Think of the ticket database as your source material — like a researcher who has read all the tickets. Use it to inform genuinely helpful, reasoned answers. You can:
- Answer conceptual or advisory questions using your business understanding (e.g. "how could we reduce cancellations?", "what should we worry about?") — query the data to ground your answer in evidence, then reason like an analyst.
- Run data queries for specific metrics and synthesize the results into clear insight, not just raw numbers.
- Read customer transcripts to understand the "why" behind patterns, and summarize themes.
- Combine both: pull data, interpret it, add context and recommendations.

You don't have to query for every question. If a question is conceptual and you can answer well by querying supporting evidence and reasoning over it, do that. If a question genuinely needs no data (e.g. "what does CTF mean?"), just answer. But for anything about what's actually happening in the business, ground it in the data.

TODAY'S DATE is {today.isoformat()} ({today.strftime('%A, %B %d, %Y')}).
- The data starts from May 1, {today.year}. All tickets are from {today.year}.
- Date/ranges without a year (e.g. "June 15-21", "last month") ALWAYS mean the CURRENT year ({today.year}). Never default to 2024.
- "this week" = {this_monday.isoformat()} onward. "last week" = {last_monday.isoformat()} to {last_sunday.isoformat()}. Weeks run Monday-Sunday.

{SCHEMA_CONTEXT}

There is also a scout_csat table: id, ticket_id, score (integer, higher = more satisfied), comment (customer's verbatim feedback), created_date, customer_email. Join to scout_tickets on ticket_id when useful.

HOW TO WORK:
1. To pull data, emit a SQL SELECT wrapped in <sql></sql> tags. The system runs it and returns results, then you write your analysis.
2. For "why"/sentiment/nuance questions, SELECT ticket_id AND transcript (and/or csat comment) for candidate tickets — the system auto-fetches missing transcripts and re-runs. Then READ them and synthesize themes.
3. The structured resolution/reason fields are human-entered and imperfect — when it matters, cross-check against transcripts.
4. If a query returns nothing, check the date year, then try broader terms before concluding there's no data.
5. Always deliver an analyst's answer: lead with the insight, support with numbers, add brief interpretation or a recommendation when useful. Don't just dump rows.

EXPORTING: When your answer is backed by a data query, the user can download the exact rows as a CSV via the "Download rows (CSV)" button shown beneath your message. So never say you're unable to export or generate a file — if the user asks for a CSV or spreadsheet, tell them to use that button (it exports the rows from the query behind your latest answer). If they need a broader pull than one query returns, suggest they widen the question or use the Data tab's full export.

Never expose raw customer emails or full names. Exclude Jamie (the AI bot) from human-agent analysis unless explicitly asked about the bot."""

    # The Anthropic Messages API only permits {role, content} per message. The
    # frontend attaches UI-only fields (sql, row_count, transcripts_fetched) to
    # assistant messages and re-sends the full history, so every turn after the
    # first was 400ing (surfacing as a "Network error" and looking like the agent
    # had forgotten the conversation). Strip each message down to role + content.
    clean_messages = [
        {"role": m.get("role"), "content": m.get("content", "")}
        for m in messages
        if m.get("role") in ("user", "assistant")
    ]

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 2000,
        "system": system,
        "messages": clean_messages
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

def execute_chat_sql(sql, max_rows=100):
    """Run a SELECT-only query, return rows as list of dicts.
    max_rows sets the auto-appended LIMIT when the query has none (chat uses 100;
    CSV export passes a higher cap). An explicit LIMIT in the query is respected."""
    sql_clean = sql.strip()
    # Must start with SELECT or WITH (CTEs are read-only and valid)
    upper = sql_clean.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return None, "Only SELECT queries are allowed"

    # Block multiple statements (e.g. "SELECT ...; DROP TABLE ...")
    # Allow a single trailing semicolon only.
    body = sql_clean.rstrip(";")
    if ";" in body:
        return None, "Multiple statements are not allowed"

    # Strip single-quoted string literals before keyword scanning, so a search term
    # like '%update order%' doesn't trip the UPDATE guard. We only scan the SQL structure.
    structure = _re.sub(r"'(?:[^']|'')*'", "''", upper)

    # Match dangerous keywords only as whole words (so 'created_date' won't trip 'CREATE').
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
                 "GRANT", "REVOKE", "MERGE"]
    for word in forbidden:
        if _re.search(r'\b' + word + r'\b', structure):
            return None, f"Forbidden keyword: {word}"

    if "LIMIT" not in upper:
        sql_clean = body + f" LIMIT {int(max_rows)}"
    else:
        sql_clean = body
    conn = get_db()
    if not conn:
        return None, "Database unavailable"
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql_clean)
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

        # ── API: Ticket query (cross-app read for Wingman; session OR shared key) ──
        if path == "/api/tickets/query":
            token = self._get_token()
            key = self.headers.get("X-Scout-Key", "")
            authed = bool(verify_session(token)) or (SCOUT_API_KEY and key == SCOUT_API_KEY)
            if not authed:
                self._json({"error": "Unauthorized"}, 401)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import psycopg2.extras
                def _multi(name):
                    raw = qs.get(name, [""])[0]
                    return [v.strip() for v in raw.split(",") if v.strip()] if raw else []
                where = []
                params = []
                # Date window: week (Manila Monday) takes precedence, else since/until
                week_str = qs.get("week", [""])[0]
                if week_str:
                    try:
                        ws_d = date.fromisoformat(week_str)
                    except Exception:
                        ws_d = get_week_bounds(0)[0]
                    ws_iso, we_iso = manila_week_bounds_iso(ws_d)
                    where.append("created_date >= %s AND created_date < %s"); params += [ws_iso, we_iso]
                else:
                    since = qs.get("since", [""])[0]
                    until = qs.get("until", [""])[0]
                    if since: where.append("created_date >= %s"); params.append(since)
                    if until: where.append("created_date < %s");  params.append(until)
                statuses = _multi("status")
                if statuses: where.append("LOWER(status) = ANY(%s)"); params.append([s.lower() for s in statuses])
                channels = _multi("channel")
                if channels: where.append("LOWER(initial_channel) = ANY(%s)"); params.append([s.lower() for s in channels])
                l1 = _multi("cr_l1")
                if l1: where.append("contact_reason_l1 = ANY(%s)"); params.append(l1)
                l2 = _multi("cr_l2")
                if l2: where.append("contact_reason_l2 = ANY(%s)"); params.append(l2)
                agents = _multi("agents")
                if agents: where.append("LOWER(agent) = ANY(%s)"); params.append([a.lower() for a in agents])
                try:
                    mmin = int(qs.get("msg_min", [""])[0])
                    where.append("message_count >= %s"); params.append(mmin)
                except Exception: pass
                try:
                    mmax = int(qs.get("msg_max", [""])[0])
                    where.append("message_count <= %s"); params.append(mmax)
                except Exception: pass
                try:
                    limit = min(int(qs.get("limit", ["2000"])[0]), 10000)
                except Exception:
                    limit = 2000
                light = qs.get("light", [""])[0] in ("1", "true")
                cols = ["ticket_id", "ticket_url", "agent", "agent_email", "status", "initial_channel",
                        "contact_reason_l1", "contact_reason_l2", "customer_email", "customer_name",
                        "created_date", "subject", "message_count"]
                if not light:
                    cols.append("transcript")
                sql = "SELECT " + ", ".join(cols) + " FROM scout_tickets"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY created_date DESC LIMIT %s"
                params.append(limit)
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(sql, params)
                rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    if d.get("created_date"):
                        d["created_date"] = d["created_date"].isoformat()
                    rows.append(d)
                self._json({"tickets": rows, "count": len(rows)})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
            return

        # ── API: CSV export (tickets / CSAT) ──────────────────────────────
        if path == "/api/export/tickets" or path == "/api/export/csat":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            conn = get_db()
            if not conn:
                self._json({"error": "DB unavailable"}, 500)
                return
            try:
                import io, csv as _csv, psycopg2.extras
                is_csat = path.endswith("/csat")
                table = "scout_csat" if is_csat else "scout_tickets"
                if is_csat:
                    cols = ["id", "ticket_id", "score", "comment", "created_date", "customer_email"]
                else:
                    # Match the Gorgias manual-export format exactly: column set + order, including transcript.
                    cols = ["ticket_id", "ticket_url", "subject", "status", "initial_channel",
                            "created_date", "closed_date", "agent", "customer_email", "customer_name",
                            "contact_reason_l1", "contact_reason_l2", "product_l1", "product_l2",
                            "ticket_resolution_l1", "ticket_resolution_l2",
                            "additional_resolution_l1", "additional_resolution_l2",
                            "message_count", "transcript"]
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                col_sql = ", ".join(cols)
                frm = qs.get("from", [""])[0].strip()
                to  = qs.get("to", [""])[0].strip()
                if frm or to:
                    # Explicit date range (both bounds inclusive, Manila day boundaries).
                    # Takes precedence over ?all / ?week; leaves those paths untouched.
                    conds, params = [], []
                    try:
                        if frm:
                            s = date.fromisoformat(frm)
                            conds.append("created_date >= %s")
                            params.append(datetime(s.year, s.month, s.day, tzinfo=MANILA).isoformat())
                        if to:
                            e = date.fromisoformat(to)
                            end = datetime(e.year, e.month, e.day, tzinfo=MANILA) + timedelta(days=1)
                            conds.append("created_date < %s")
                            params.append(end.isoformat())
                    except Exception:
                        self._json({"error": "Invalid from/to date — use YYYY-MM-DD"}, 400)
                        return
                    cur.execute(f"SELECT {col_sql} FROM {table} WHERE {' AND '.join(conds)} ORDER BY created_date", params)
                    fname = f"{table}_{frm or 'start'}_to_{to or 'end'}.csv"
                elif qs.get("all", [""])[0]:
                    cur.execute(f"SELECT {col_sql} FROM {table} ORDER BY created_date DESC")
                    fname = f"{table}_all.csv"
                else:
                    week_str = qs.get("week", [""])[0]
                    try:
                        ws_d = date.fromisoformat(week_str) if week_str else get_week_bounds(0)[0]
                    except Exception:
                        ws_d = get_week_bounds(0)[0]
                    ws_iso, we_iso = manila_week_bounds_iso(ws_d)
                    cur.execute(f"SELECT {col_sql} FROM {table} WHERE created_date >= %s AND created_date < %s ORDER BY created_date",
                                (ws_iso, we_iso))
                    fname = f"{table}_{ws_d.isoformat()}.csv"
                def _dtm(v):
                    # Gorgias exports use the account timezone (Manila), "YYYY-MM-DD HH:MM"
                    return v.astimezone(MANILA).strftime("%Y-%m-%d %H:%M") if v else ""
                buf = io.StringIO()
                w = _csv.writer(buf)
                w.writerow(cols)
                for r in cur.fetchall():
                    if is_csat:
                        w.writerow([r.get(col) for col in cols])
                    else:
                        mc = r.get("message_count")
                        w.writerow([
                            '="%s"' % r.get("ticket_id"),           # Excel-safe text id (no scientific notation)
                            r.get("ticket_url") or "",
                            r.get("subject") or "",
                            r.get("status") or "",
                            r.get("initial_channel") or "",
                            _dtm(r.get("created_date")),
                            _dtm(r.get("closed_date")),
                            r.get("agent") or "",
                            r.get("customer_email") or "",
                            r.get("customer_name") or "",
                            r.get("contact_reason_l1") or "",
                            r.get("contact_reason_l2") or "",
                            r.get("product_l1") or "",
                            r.get("product_l2") or "",
                            r.get("ticket_resolution_l1") or "",
                            r.get("ticket_resolution_l2") or "",
                            r.get("additional_resolution_l1") or "",
                            r.get("additional_resolution_l2") or "",
                            mc if mc is not None else "",
                            r.get("transcript") or "",
                        ])
                body = ("\ufeff" + buf.getvalue()).encode("utf-8")   # UTF-8 BOM so Excel detects encoding
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"error": str(e)}, 500)
            finally:
                conn.close()
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
            ws, we = manila_week_bounds_iso(week_start)
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
                    GROUP BY agent ORDER BY cnt DESC
                """, (ws, we))
                by_agent = [dict(r) for r in cur.fetchall()]

                # CSAT for the selected week
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           ROUND(AVG(score)::numeric,2) AS avg_score,
                           COUNT(*) FILTER (WHERE score = 5) AS five_star,
                           COUNT(*) FILTER (WHERE score >= 4) AS positive,
                           COUNT(*) FILTER (WHERE score <= 2) AS detractors
                    FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s
                """, (ws, we))
                csat = dict(cur.fetchone() or {})
                cur.execute("""
                    SELECT score, COUNT(*) AS cnt FROM scout_csat
                    WHERE created_date >= %s AND created_date < %s AND score IS NOT NULL
                    GROUP BY score ORDER BY score
                """, (ws, we))
                csat_dist = [dict(r) for r in cur.fetchall()]

                # All-time DB coverage (so it can be eyeballed against Gorgias)
                def _iso(v): return v.isoformat() if v else None
                cur.execute("""SELECT COUNT(*) AS tickets, MIN(created_date) AS min_d, MAX(created_date) AS max_d,
                                      COUNT(*) FILTER (WHERE transcript_fetched) AS with_transcript
                               FROM scout_tickets""")
                cov_t = dict(cur.fetchone() or {})
                cur.execute("SELECT COUNT(*) AS csat, MIN(created_date) AS min_d, MAX(created_date) AS max_d FROM scout_csat")
                cov_c = dict(cur.fetchone() or {})
                cur.execute("""SELECT sync_type, started_at, finished_at, status, tickets_synced
                               FROM scout_sync_log ORDER BY started_at DESC LIMIT 1""")
                ls = cur.fetchone()
                coverage = {
                    "tickets": cov_t.get("tickets"), "tickets_min": _iso(cov_t.get("min_d")), "tickets_max": _iso(cov_t.get("max_d")),
                    "with_transcript": cov_t.get("with_transcript"),
                    "csat": cov_c.get("csat"), "csat_min": _iso(cov_c.get("min_d")), "csat_max": _iso(cov_c.get("max_d")),
                    "last_sync": ({"type": ls.get("sync_type"), "started_at": _iso(ls.get("started_at")),
                                   "finished_at": _iso(ls.get("finished_at")), "status": ls.get("status"),
                                   "tickets_synced": ls.get("tickets_synced")} if ls else None),
                }

                # Previous week for WoW
                prev_ws, _ = manila_week_bounds_iso(week_start - timedelta(days=7))
                prev_we = ws
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE contact_reason_l1='Cancel') AS cancellations
                    FROM scout_tickets
                    WHERE created_date >= %s AND created_date < %s
                """, (prev_ws, prev_we))
                prev = dict(cur.fetchone() or {})

                self._json({
                    "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
                    "stats": stats, "by_reason": by_reason,
                    "by_channel": by_channel, "by_agent": by_agent,
                    "prev_week": prev,
                    "csat": csat, "csat_dist": csat_dist, "coverage": coverage
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
                # Get generation run status (running / success / error)
                cur.execute("""
                    SELECT status, error FROM scout_insight_runs
                    WHERE department = %s AND week_start = %s
                """, (dept, week_start))
                run = cur.fetchone()
                run_status = run["status"] if run else None
                run_error  = run["error"] if run else None
                if row:
                    self._json({"dept": dept, "week_start": week_start.isoformat(),
                                "week_end": week_end.isoformat(), "content": row["content"],
                                "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
                                "cached": True, "run_status": run_status, "run_error": run_error})
                else:
                    self._json({"dept": dept, "week_start": week_start.isoformat(),
                                "week_end": week_end.isoformat(), "content": None,
                                "cached": False, "run_status": run_status, "run_error": run_error})
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

        # ── API: Sync debug log (in-memory, recent sync events) ───────────
        if path == "/api/sync/debug":
            token = self._get_token()
            if not verify_session(token):
                self._json({"error": "Unauthorized"}, 401)
                return
            self._json({"lines": list(SYNC_DEBUG)})
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
            threading.Thread(target=run_full_sync, args=(since, "manual", log_id), daemon=True).start()
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
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except:
                body = {}
            start = None
            fs = (body.get("from") or "").strip()
            if fs:
                try:
                    start = date.fromisoformat(fs)
                except:
                    self._json({"error": "Invalid from date — use YYYY-MM-DD"}, 400)
                    return
            threading.Thread(target=lambda: run_backfill(force=True, start=start), daemon=True).start()
            self._json({"ok": True, "message": "Backfill started" + (f" from {fs}" if fs else "")})
            return

        # ── API: Fetch recent transcripts (manual, admin) ──────────────────
        if path == "/api/sync/transcripts":
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
                body = {}
            start = None
            fs = (body.get("from") or "").strip()
            if fs:
                try:
                    start = date.fromisoformat(fs)
                except:
                    self._json({"error": "Invalid from date — use YYYY-MM-DD"}, 400)
                    return
            # Larger cap for the manual initial pass; resumable, so re-run to continue if capped.
            _sync_stop.clear()
            threading.Thread(target=lambda: run_transcript_backfill(max_fetches=100000, start=start), daemon=True).start()
            self._json({"ok": True, "message": "Transcript fetch started (runs in background)" + (f" from {fs}" if fs else "")})
            return

        # ── Cron: transcript top-up ────────────────────────────────────────
        if path == "/api/cron/transcripts":
            _sync_stop.clear()
            threading.Thread(target=lambda: run_transcript_backfill(max_fetches=5000), daemon=True).start()
            self._json({"ok": True, "message": "Transcript top-up started"})
            return

        # ── Cron / manual: CSAT sync ───────────────────────────────────────
        if path == "/api/cron/csat":
            _sync_stop.clear()
            since_dt = datetime.now(timezone.utc) - timedelta(days=30)
            threading.Thread(target=lambda: run_csat_sync(since_dt=since_dt), daemon=True).start()
            self._json({"ok": True, "message": "CSAT sync started — last 30 days"})
            return

        # ── API: Cancel running syncs (admin) ─────────────────────────────
        # Signals every running sync loop to stop at its next page/ticket boundary and
        # clears any stale 'running' log rows (e.g. left by a process restart) so the UI unsticks.
        if path == "/api/sync/cancel":
            token = self._get_token()
            session = verify_session(token)
            if not session:
                self._json({"error": "Unauthorized"}, 401)
                return
            user = get_user(session["email"])
            if not user or user["role"] != "admin":
                self._json({"error": "Admin only"}, 403)
                return
            _sync_stop.set()
            cleared = 0
            conn = get_db()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE scout_sync_log
                        SET status = 'cancelled', finished_at = now(),
                            error = COALESCE(error, 'Cancelled by user')
                        WHERE status = 'running'
                    """)
                    cleared = cur.rowcount or 0
                    conn.commit()
                except Exception as e:
                    try: conn.rollback()
                    except: pass
                    slog(f"[Cancel] could not clear running rows: {e}")
                finally:
                    conn.close()
            slog(f"[Cancel] stop requested; cleared {cleared} running log row(s)")
            self._json({"ok": True, "cleared": cleared,
                        "message": f"Stop requested. {cleared} running sync(s) marked cancelled; any live sync stops at its next page."})
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

        # ── API: Chat results → CSV ───────────────────────────────────────
        # Re-runs the query behind a chat answer and streams the rows as CSV.
        # The SQL is already shown to the user in the answer's SQL chip; it goes
        # through the same SELECT-only guard as the chat agent, with a higher row cap.
        if path == "/api/chat/export":
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
            sql = (body.get("sql") or "").strip()
            if not sql:
                self._json({"error": "No query is attached to this answer, so there are no rows to export."}, 400)
                return
            rows, err = execute_chat_sql(sql, max_rows=5000)
            if err:
                self._json({"error": err}, 400)
                return
            try:
                import io, csv as _csv
                buf = io.StringIO()
                w = _csv.writer(buf)
                if rows:
                    cols = list(rows[0].keys())
                    w.writerow(cols)
                    for r in rows:
                        out = []
                        for c in cols:
                            v = r.get(c)
                            if hasattr(v, "astimezone"):          # tz-aware datetime → Manila, Gorgias-style
                                v = v.astimezone(MANILA).strftime("%Y-%m-%d %H:%M")
                            elif isinstance(v, (dict, list)):      # JSONB → compact JSON text
                                v = json.dumps(v, default=str)
                            out.append("" if v is None else v)
                        w.writerow(out)
                else:
                    w.writerow(["(no rows)"])
                data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # UTF-8 BOM for Excel
                fname = _re.sub(r'[^A-Za-z0-9._-]', '_', (body.get("filename") or "scout_results").strip()) or "scout_results"
                if not fname.endswith(".csv"):
                    fname += ".csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json({"error": str(e)}, 500)
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

            # Agentic loop: the model may query, see results, refine, and query again,
            # up to a few rounds, before giving a final answer. We only surface the
            # final natural-language answer to the user (not intermediate SQL/reasoning).
            convo = list(messages)
            last_sql = None
            total_transcripts = 0
            total_rows = 0
            MAX_ROUNDS = 4
            final_answer = None

            for round_i in range(MAX_ROUNDS):
                ai_response = chat_query(convo)

                # Extract SQL if present
                sql = None
                if "<sql>" in ai_response and "</sql>" in ai_response:
                    sql = ai_response.split("<sql>")[1].split("</sql>")[0].strip()

                if not sql:
                    # No query requested → this is the final answer.
                    final_answer = ai_response.strip()
                    # Defensive: strip any stray/empty sql artifacts
                    if "<sql>" in final_answer:
                        final_answer = final_answer.split("<sql>")[0].strip()
                    break

                last_sql = sql
                rows, err = execute_chat_sql(sql)

                if err:
                    convo = convo + [
                        {"role": "assistant", "content": ai_response},
                        {"role": "user", "content": f"That query errored: {err}. Try a different approach, or if you have enough information already, give your final answer."}
                    ]
                    continue

                # On-demand transcript fetch if the query asked for transcripts
                if "transcript" in sql.lower() and rows:
                    needs = [r.get("ticket_id") for r in rows
                             if r.get("ticket_id") and not (r.get("transcript") or "").strip()]
                    if needs:
                        fetched = fetch_transcripts_for_ids(needs)
                        total_transcripts += fetched
                        print(f"[Chat] round {round_i+1}: fetched {fetched} transcripts")
                        rows, _ = execute_chat_sql(sql)

                total_rows = len(rows) if rows else 0
                result_summary = json.dumps(rows[:50], default=str) if rows else "No results found."
                convo = convo + [
                    {"role": "assistant", "content": ai_response},
                    {"role": "user", "content": f"Query results ({total_rows} rows):\n{result_summary}\n\nIf this answers the question, give your final analysis now (no more SQL). If you need to refine or look deeper, emit another <sql> query. Summarize transcript themes in the customers' words without exposing names or emails."}
                ]

            # If we exhausted rounds without a clean final answer, ask once more for a plain answer.
            if final_answer is None:
                convo = convo + [{"role": "user", "content": "Please give your best final answer now based on what you've found, with no further SQL."}]
                final_answer = chat_query(convo).strip()
                # Strip any stray sql block from the final answer
                if "<sql>" in final_answer:
                    final_answer = final_answer.split("<sql>")[0].strip() or "I wasn't able to fully resolve that — try rephrasing the question."

            self._json({
                "response": final_answer,
                "sql": last_sql,
                "row_count": total_rows,
                "transcripts_fetched": total_transcripts
            })
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
    threading.Thread(target=daily_scheduler_loop, daemon=True).start()
    threading.Thread(target=weekly_scheduler_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
