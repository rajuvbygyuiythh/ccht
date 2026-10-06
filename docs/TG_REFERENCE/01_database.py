"""EVA BOT v7.0 — Database (SQLite)
Fast, simple, single-file. No PostgreSQL needed.
"""
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "eva_bot.db"
_conn = None

def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn

def init():
    c = get_conn().cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT DEFAULT 'User',
        username TEXT DEFAULT '',
        profile_name TEXT DEFAULT '',
        age INTEGER DEFAULT 0,
        country TEXT DEFAULT '',
        relationship_status TEXT DEFAULT '',
        lead_score INTEGER DEFAULT 0,
        message_count INTEGER DEFAULT 0,
        media_received INTEGER DEFAULT 0,
        media_sent INTEGER DEFAULT 0,
        bot_media_sent INTEGER DEFAULT 0,
        promo_sent INTEGER DEFAULT 0,
        link_clicks INTEGER DEFAULT 0,
        funnel_stage TEXT DEFAULT 'hook',
        horny_score REAL DEFAULT 0.0,
        last_horny_score REAL DEFAULT 0.0,
        age_exchanged INTEGER DEFAULT 0,
        age_asked INTEGER DEFAULT 0,
        adult_consent INTEGER DEFAULT 0,
        adult_consent_asked INTEGER DEFAULT 0,
        pic_exchanged INTEGER DEFAULT 0,
        first_seen TEXT DEFAULT '',
        last_seen TEXT DEFAULT '',
        last_bot_reply TEXT DEFAULT '',
        last_question TEXT DEFAULT '',
        intro_sent INTEGER DEFAULT 0,
        intro_followup_sent INTEGER DEFAULT 0,
        refusal_count INTEGER DEFAULT 0,
        last_vibe TEXT DEFAULT '',
        vibe_score REAL DEFAULT 0.0,
        last_emotion TEXT DEFAULT 'neutral',
        emotion_confidence REAL DEFAULT 0.0,
        prelink_tease_sent INTEGER DEFAULT 0,
        pending_horny_link INTEGER DEFAULT 0,
        cta_ready_score REAL DEFAULT 0.0,
        trust_score INTEGER DEFAULT 0,
        personality TEXT DEFAULT '',
        metadata TEXT DEFAULT '{}'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        role TEXT,
        content TEXT,
        content_type TEXT DEFAULT 'text',
        horny_score REAL DEFAULT 0.0,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS learned_pairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_text TEXT,
        reply_text TEXT,
        source TEXT DEFAULT 'auto',
        timestamp TEXT,
        UNIQUE(user_text, reply_text)
    )""")

    # Lightweight migrations for older eva_bot.db files already shipped with the bot.
    cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    if "age_asked" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN age_asked INTEGER DEFAULT 0")
    for col, ddl in {
        "interest_score": "ALTER TABLE users ADD COLUMN interest_score REAL DEFAULT 0.0",
        "interest_action": "ALTER TABLE users ADD COLUMN interest_action TEXT DEFAULT ''",
        "intro_sent": "ALTER TABLE users ADD COLUMN intro_sent INTEGER DEFAULT 0",
        "intro_followup_sent": "ALTER TABLE users ADD COLUMN intro_followup_sent INTEGER DEFAULT 0",
        "last_vibe": "ALTER TABLE users ADD COLUMN last_vibe TEXT DEFAULT ''",
        "vibe_score": "ALTER TABLE users ADD COLUMN vibe_score REAL DEFAULT 0.0",
        "prelink_tease_sent": "ALTER TABLE users ADD COLUMN prelink_tease_sent INTEGER DEFAULT 0",
        "pending_horny_link": "ALTER TABLE users ADD COLUMN pending_horny_link INTEGER DEFAULT 0",
        "cta_ready_score": "ALTER TABLE users ADD COLUMN cta_ready_score REAL DEFAULT 0.0",
        "last_emotion": "ALTER TABLE users ADD COLUMN last_emotion TEXT DEFAULT 'neutral'",
        "emotion_confidence": "ALTER TABLE users ADD COLUMN emotion_confidence REAL DEFAULT 0.0",
        "adult_consent": "ALTER TABLE users ADD COLUMN adult_consent INTEGER DEFAULT 0",
        "adult_consent_asked": "ALTER TABLE users ADD COLUMN adult_consent_asked INTEGER DEFAULT 0",
        "trust_score": "ALTER TABLE users ADD COLUMN trust_score INTEGER DEFAULT 0",
        "user_archetype": "ALTER TABLE users ADD COLUMN user_archetype TEXT DEFAULT 'unknown'",
        "interest_velocity": "ALTER TABLE users ADD COLUMN interest_velocity REAL DEFAULT 0.0",
        "interest_momentum": "ALTER TABLE users ADD COLUMN interest_momentum REAL DEFAULT 0.0",
        "personalized_threshold": "ALTER TABLE users ADD COLUMN personalized_threshold REAL DEFAULT 0.50",
        "interest_peak_count": "ALTER TABLE users ADD COLUMN interest_peak_count INTEGER DEFAULT 0",
        "council_decision": "ALTER TABLE users ADD COLUMN council_decision TEXT DEFAULT ''",
        "council_confidence": "ALTER TABLE users ADD COLUMN council_confidence REAL DEFAULT 0.0",
    }.items():
        if col not in cols:
            c.execute(ddl)
    get_conn().commit()


def reset_user(uid: int) -> bool:
    """Delete one user's profile, history, and learned pairs derived from that chat."""
    c = get_conn().cursor()
    c.execute("DELETE FROM messages WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE user_id=?", (uid,))
    get_conn().commit()
    return True


def reset_all() -> bool:
    """Delete all conversation state while preserving static local text databases."""
    c = get_conn().cursor()
    c.execute("DELETE FROM messages")
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM learned_pairs")
    get_conn().commit()
    return True

def upsert_user(uid, first_name="User", username=""):
    now = datetime.now().isoformat(timespec="seconds")
    c = get_conn().cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if c.fetchone():
        c.execute("UPDATE users SET first_name=?,username=?,last_seen=?,message_count=message_count+1 WHERE user_id=?", (first_name, username, now, uid))
        get_conn().commit()
        return False
    c.execute("INSERT INTO users(user_id,first_name,username,first_seen,last_seen,message_count) VALUES(?,?,?,?,?,1)", (uid, first_name, username, now, now))
    get_conn().commit()
    return True

def get_user(uid):
    c = get_conn().cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r:
        return {}
    cols = [d[0] for d in c.description]
    return dict(zip(cols, r))

def update_field(uid, field, value):
    c = get_conn().cursor()
    now = datetime.now().isoformat(timespec="seconds")
    c.execute("INSERT OR IGNORE INTO users(user_id,first_seen,last_seen) VALUES(?,?,?)", (uid, now, now))
    c.execute(f"UPDATE users SET {field}=?,last_seen=? WHERE user_id=?", (value, now, uid))
    get_conn().commit()

def get_field(uid, field, default=None):
    u = get_user(uid)
    return u.get(field, default)

def save_message(uid, role, content, ctype="text", horny_score=0.0):
    c = get_conn().cursor()
    now = datetime.now().isoformat(timespec="seconds")
    c.execute("INSERT INTO messages(user_id,role,content,content_type,horny_score,timestamp) VALUES(?,?,?,?,?,?)", (uid, role, content[:1000], ctype, horny_score, now))
    get_conn().commit()

def get_recent(uid, limit=20):
    c = get_conn().cursor()
    c.execute("SELECT role,content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))
    return [(r[0], r[1]) for r in reversed(c.fetchall())]

def get_recent_user_texts(uid, limit=10):
    c = get_conn().cursor()
    c.execute("SELECT content FROM messages WHERE user_id=? AND role='user' ORDER BY id DESC LIMIT ?", (uid, limit))
    return [r[0] for r in c.fetchall()]

def get_recent_assistant_replies(uid, limit=8):
    c = get_conn().cursor()
    c.execute("SELECT content FROM messages WHERE user_id=? AND role='assistant' ORDER BY id DESC LIMIT ?", (uid, limit))
    return [r[0] for r in reversed(c.fetchall())]

def get_msg_count(uid):
    c = get_conn().cursor()
    c.execute("SELECT message_count FROM users WHERE user_id=?", (uid,))
    r = c.fetchone()
    return r[0] if r else 0

def mark_promo_sent(uid):
    now = datetime.now().isoformat(timespec="seconds")
    update_field(uid, "promo_sent", 1)
    c = get_conn().cursor()
    c.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now, uid))
    get_conn().commit()

def get_promo_sent(uid):
    return get_field(uid, "promo_sent", 0)

def mark_media_received(uid):
    c = get_conn().cursor()
    c.execute("UPDATE users SET media_received=media_received+1 WHERE user_id=?", (uid,))
    get_conn().commit()

def mark_bot_media_sent(uid):
    c = get_conn().cursor()
    c.execute("UPDATE users SET bot_media_sent=bot_media_sent+1 WHERE user_id=?", (uid,))
    get_conn().commit()

def user_sent_media(uid):
    return bool(get_field(uid, "media_received", 0))

def bot_sent_media(uid):
    return bool(get_field(uid, "bot_media_sent", 0))

def mark_age_exchanged(uid):
    update_field(uid, "age_exchanged", 1)

def age_exchanged(uid):
    return bool(get_field(uid, "age_exchanged", 0))

def mark_age_asked(uid):
    update_field(uid, "age_asked", 1)

def age_asked(uid):
    return bool(get_field(uid, "age_asked", 0))

def mark_pic_exchanged(uid):
    update_field(uid, "pic_exchanged", 1)

def pic_exchanged(uid):
    return bool(get_field(uid, "pic_exchanged", 0))

def update_horny_score(uid, score):
    prev = get_field(uid, "horny_score", 0.0) or 0.0
    # Smooth average with recency weighting
    new_score = round(prev * 0.3 + score * 0.7, 2)
    update_field(uid, "last_horny_score", prev)
    update_field(uid, "horny_score", new_score)

def get_horny_score(uid):
    return float(get_field(uid, "horny_score", 0.0) or 0.0)

def add_lead_score(uid, points):
    current = int(get_field(uid, "lead_score", 0) or 0)
    update_field(uid, "lead_score", current + points)

def save_learned_pair(ut, rt, src="auto"):
    try:
        c = get_conn().cursor()
        now = datetime.now().isoformat(timespec="seconds")
        c.execute("INSERT OR IGNORE INTO learned_pairs(user_text,reply_text,source,timestamp) VALUES(?,?,?,?)", (ut.lower().strip()[:200], rt.strip()[:200], src, now))
        get_conn().commit()
        return True
    except:
        return False

def get_stats():
    c = get_conn().cursor()
    stats = {}
    for name, q in [
        ("total_users", "SELECT COUNT(*) FROM users"),
        ("promos_sent", "SELECT COUNT(*) FROM users WHERE promo_sent=1"),
        ("hot_leads", "SELECT COUNT(*) FROM users WHERE horny_score>=0.5"),
        ("age_exchanged", "SELECT COUNT(*) FROM users WHERE age_exchanged=1"),
        ("age_asked", "SELECT COUNT(*) FROM users WHERE age_asked=1"),
        ("pic_exchanged", "SELECT COUNT(*) FROM users WHERE pic_exchanged=1"),
        ("total_messages", "SELECT COUNT(*) FROM messages"),
        ("learned_pairs", "SELECT COUNT(*) FROM learned_pairs"),
    ]:
        c.execute(q)
        stats[name] = c.fetchone()[0]
    return stats

PERSONALITIES = ["funny", "bold", "shy", "mysterious", "romantic", "wild", "sweet"]

def get_user_personality(uid):
    """Get user personality. personality_mode=false = always 'real'."""
    import config
    if not config.PERSONALITY_MODE:
        return "real"
    p = get_field(uid, "personality", "")
    if not p:
        import random
        p = random.choice(PERSONALITIES)
        update_field(uid, "personality", p)
    return p
