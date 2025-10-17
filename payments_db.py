# payments_db.py
import sqlite3, os, time

DB_PATH = os.environ.get("DB_PATH", "payments.sqlite3")

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS subs(
        tenant TEXT PRIMARY KEY,
        provider TEXT,
        status TEXT,
        email TEXT,
        plan_id TEXT,
        created_at INTEGER
    )""")
    return c

def upsert_sub(tenant, provider, status, email, plan_id):
    c = _conn()
    c.execute("INSERT INTO subs(tenant,provider,status,email,plan_id,created_at) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(tenant) DO UPDATE SET provider=excluded.provider,status=excluded.status,email=excluded.email,plan_id=excluded.plan_id",
              (tenant, provider, status, email, plan_id, int(time.time())))
    c.commit(); c.close()

def get_sub(tenant):
    c = _conn()
    cur = c.execute("SELECT tenant,provider,status,email,plan_id,created_at FROM subs WHERE tenant=?", (tenant,))
    row = cur.fetchone(); c.close()
    return row
