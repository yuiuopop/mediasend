import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import psycopg2
from psycopg2 import pool
import threading
import math
import os
try:
    from flask import Flask, request, abort
except ImportError:
    Flask = None

# ================= Configuration =================
# Prioritizes system environment variables (hosting sites) over .env defaults
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip()]

# Basic validation
if not BOT_TOKEN or not DATABASE_URL:
    print("CRITICAL: BOT_TOKEN or DATABASE_URL not found in environment!")
    # In some hosting environments, we want to fail fast

# Economics
FREE_STARTING_POINTS = 10
MEDIA_COST = 1
REFERRAL_BONUS = 2

upload_batches = {}
upload_lock = threading.Lock()
admin_active_category = {}   # admin_id → cat_id
admin_content_type = {}      # admin_id → 'text' | 'gif_sticker' | 'media'
admin_session_msg = {}       # admin_id → (chat_id, message_id)
admin_session_stats = {}     # admin_id → {'sent': int, 'saved': int, 'dupes': int}

# ================= Database =================
# Connection pool for thread-safe PostgreSQL access
db_pool = None

def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, dsn=DATABASE_URL)
    return db_pool

def get_db():
    import time
    max_retries = 5
    for i in range(max_retries):
        try:
            return get_db_pool().getconn()
        except Exception as e:
            print(f"Database connection attempt {i+1} failed: {e}")
            if i < max_retries - 1:
                time.sleep(2) # wait for DB to wake up
            else:
                raise e

def release_db(conn):
    get_db_pool().putconn(conn)

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                points INTEGER DEFAULT 10,
                referred_by BIGINT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                media_received INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                req_referrals INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                content_type TEXT DEFAULT 'media',
                type_configured BOOLEAN DEFAULT FALSE,
                sort_order INTEGER DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS media (
                id SERIAL PRIMARY KEY,
                file_id TEXT,
                media_type TEXT,
                file_unique_id TEXT,
                category_id INTEGER REFERENCES categories(id),
                content TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_category_stats (
                user_id BIGINT,
                category_id INTEGER,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, category_id)
            )
        ''')
        cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS firewall_channels (
                id SERIAL PRIMARY KEY,
                button_name TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                invite_link TEXT NOT NULL
            )
        ''')
        # Safe migrations
        cursor.execute("ALTER TABLE categories ADD COLUMN IF NOT EXISTS content_type TEXT DEFAULT 'media'")
        cursor.execute("ALTER TABLE categories ADD COLUMN IF NOT EXISTS type_configured BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE categories ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE media ADD COLUMN IF NOT EXISTS content TEXT")
        # Initialize sort_order for existing categories if all are 0
        cursor.execute("SELECT COUNT(*) FROM categories WHERE sort_order != 0")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                UPDATE categories SET sort_order = sub.rn
                FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM categories) sub
                WHERE categories.id = sub.id
            """)

        cursor.execute("INSERT INTO settings (key, value) VALUES ('start_message', %s) ON CONFLICT (key) DO NOTHING", ("<b>Welcome to the Media Bot!</b> 📺\n<blockquote>Use the menu below to navigate.</blockquote>",))
        cursor.execute("INSERT INTO settings (key, value) VALUES ('media_caption', %s) ON CONFLICT (key) DO NOTHING", ("<blockquote>Enjoy this from <b>{cat_name}</b>! 🍿</blockquote>\n<code>Remaining points: {points}</code>",))
        cursor.execute("INSERT INTO settings (key, value) VALUES ('firewall_enabled', 'false') ON CONFLICT (key) DO NOTHING")
        cursor.execute("INSERT INTO settings (key, value) VALUES ('firewall_message', %s) ON CONFLICT (key) DO NOTHING",
                       ("<blockquote>🔒 <b>You must join our channel(s) to use this bot.</b></blockquote>\n\nPlease join the channel(s) below, then press ✅ <b>I Joined</b>.",))

        cursor.execute("SELECT id FROM categories LIMIT 1")
        if not cursor.fetchone():
            cursor.execute("INSERT INTO categories (name) VALUES (%s)", ('📺 Watch Media',))

        conn.commit()
    finally:
        release_db(conn)

def add_user(user_id, username, starting_points, referred_by=None):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id, username, points, referred_by) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (user_id, username, starting_points, referred_by))
        conn.commit()
        return cursor.rowcount > 0
    except: return False
    finally: release_db(conn)

def get_user(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, points, referred_by, join_date::DATE FROM users WHERE user_id = %s", (user_id,))
        return cursor.fetchone()
    finally: release_db(conn)

def update_points(user_id, delta):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET points = points + %s WHERE user_id = %s", (delta, user_id))
        conn.commit()
    finally: release_db(conn)

def get_points(user_id):
    user = get_user(user_id)
    return user[2] if user else 0
    
def update_media_received(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET media_received = media_received + 1 WHERE user_id = %s", (user_id,))
        conn.commit()
    finally: release_db(conn)

def get_setting(key, default=None):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
        res = cursor.fetchone()
        return res[0] if res else default
    finally: release_db(conn)

def set_setting(key, value):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
        conn.commit()
    finally: release_db(conn)

def increment_user_category_stat(user_id, category_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_category_stats (user_id, category_id, count) 
            VALUES (%s, %s, 1)
            ON CONFLICT(user_id, category_id) DO UPDATE SET count = user_category_stats.count + 1
        ''', (user_id, category_id))
        conn.commit()
    finally: release_db(conn)

def get_user_list_page(limit, offset):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.user_id, u.username, (SELECT COUNT(*) FROM users WHERE referred_by = u.user_id) as ref_count 
            FROM users u 
            ORDER BY u.join_date DESC 
            LIMIT %s OFFSET %s
        ''', (limit, offset))
        return cursor.fetchall()
    finally: release_db(conn)

def get_user_detail(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, points, referred_by, join_date, media_received FROM users WHERE user_id = %s", (user_id,))
        return cursor.fetchone()
    finally: release_db(conn)

def get_user_cat_breakdown(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT c.name, s.count 
            FROM user_category_stats s
            JOIN categories c ON s.category_id = c.id
            WHERE s.user_id = %s
        ''', (user_id,))
        return cursor.fetchall()
    finally: release_db(conn)

def get_total_referrals(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = %s", (user_id,))
        res = cursor.fetchone()
        return res[0] if res else 0
    finally: release_db(conn)

def search_users(query):
    """Search users by user_id, username. Returns list of (user_id, username, points)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        # If query is a number, search by user_id first
        results = []
        if query.lstrip('-').isdigit():
            cursor.execute(
                "SELECT user_id, username, points FROM users WHERE user_id = %s LIMIT 10",
                (int(query),)
            )
            results = cursor.fetchall()
        # Also search by username (partial match)
        if not results:
            cursor.execute(
                "SELECT user_id, username, points FROM users WHERE LOWER(username) LIKE LOWER(%s) LIMIT 10",
                (f"%{query}%",)
            )
            results = cursor.fetchall()
        return results
    finally: release_db(conn)

def get_categories():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, is_hidden FROM categories ORDER BY sort_order, id")
        return cursor.fetchall()
    finally: release_db(conn)

def get_visible_categories():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM categories WHERE is_hidden = 0 ORDER BY sort_order, id")
        return cursor.fetchall()
    finally: release_db(conn)

def move_category(cat_id, direction):
    """Move a category up (-1) or down (+1) by swapping sort_order with its neighbour."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        # Get ordered list of all categories
        cursor.execute("SELECT id, sort_order FROM categories ORDER BY sort_order, id")
        rows = cursor.fetchall()
        ids = [r[0] for r in rows]
        if cat_id not in ids: return
        idx = ids.index(cat_id)
        swap_idx = idx + direction  # -1 = up, +1 = down
        if swap_idx < 0 or swap_idx >= len(ids): return  # already at edge
        other_id = ids[swap_idx]
        # Swap sort_order values
        cur_order = rows[idx][1]
        other_order = rows[swap_idx][1]
        cursor.execute("UPDATE categories SET sort_order = %s WHERE id = %s", (other_order, cat_id))
        cursor.execute("UPDATE categories SET sort_order = %s WHERE id = %s", (cur_order, other_id))
        conn.commit()
    finally: release_db(conn)

def toggle_category_visibility(cat_id, hide):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE categories SET is_hidden = %s WHERE id = %s", (1 if hide else 0, cat_id))
        conn.commit()
    finally: release_db(conn)

def delete_category_db(cat_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM media WHERE category_id = %s", (cat_id,))
        cursor.execute("DELETE FROM categories WHERE id = %s", (cat_id,))
        conn.commit()
    finally: release_db(conn)

def get_category_req(cat_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT req_referrals FROM categories WHERE id = %s", (cat_id,))
        res = cursor.fetchone()
        return res[0] if res and res[0] else 0
    except: return 0
    finally: release_db(conn)

def update_category_req(cat_id, limit):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE categories SET req_referrals = %s WHERE id = %s", (limit, cat_id))
        conn.commit()
    finally: release_db(conn)

def add_category(name):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO categories (name) VALUES (%s) RETURNING id", (name,))
        cat_id = cursor.fetchone()[0]
        conn.commit()
        return cat_id
    except: return None
    finally: release_db(conn)

def add_media(file_id, media_type, file_unique_id=None, category_id=1):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO media (file_id, media_type, file_unique_id, category_id) VALUES (%s, %s, %s, %s) RETURNING id", (file_id, media_type, file_unique_id, category_id))
        media_id = cursor.fetchone()[0]
        conn.commit()
        return media_id
    finally: release_db(conn)

def check_duplicate_media(file_unique_id):
    if not file_unique_id: return False
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM media WHERE file_unique_id = %s", (file_unique_id,))
        return cursor.fetchone() is not None
    finally: release_db(conn)

def get_random_media(category_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, file_id, media_type FROM media WHERE category_id = %s ORDER BY RANDOM() LIMIT 1", (category_id,))
        return cursor.fetchone()
    finally: release_db(conn)

def delete_media(media_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM media WHERE id = %s", (media_id,))
        success = cursor.rowcount > 0
        conn.commit()
        return success
    finally: release_db(conn)

def get_media_page(category_id, limit, offset):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, media_type FROM media WHERE category_id = %s ORDER BY id DESC LIMIT %s OFFSET %s", (category_id, limit, offset))
        return cursor.fetchall()
    finally: release_db(conn)

def get_media_by_id(media_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT file_id, media_type FROM media WHERE id = %s", (media_id,))
        return cursor.fetchone()
    finally: release_db(conn)

def wipe_category(category_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM media WHERE category_id = %s", (category_id,))
        conn.commit()
    finally: release_db(conn)

def get_cat_stats(category_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM media WHERE category_id = %s", (category_id,))
        res = cursor.fetchone()
        return res[0] if res else 0
    finally: release_db(conn)

def get_stats():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM media")
        media_count = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(media_received) FROM users")
        ts = cursor.fetchone()[0]
        return users_count, media_count, int(ts) if ts else 0
    finally: release_db(conn)

def set_category_content_type(cat_id, content_type):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE categories SET content_type = %s, type_configured = TRUE WHERE id = %s",
            (content_type, cat_id)
        )
        conn.commit()
    finally: release_db(conn)

def is_category_type_configured(cat_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT type_configured FROM categories WHERE id = %s", (cat_id,))
        res = cursor.fetchone()
        return bool(res[0]) if res else False
    finally: release_db(conn)

def get_category_extractions(cat_id):
    """Total number of times users have received content from this category."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE(SUM(count), 0) FROM user_category_stats WHERE category_id = %s",
            (cat_id,)
        )
        res = cursor.fetchone()
        return int(res[0]) if res else 0
    finally: release_db(conn)

def get_category_content_type(cat_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content_type FROM categories WHERE id = %s", (cat_id,))
        res = cursor.fetchone()
        return res[0] if res else 'media'
    finally: release_db(conn)

def add_text_content(text_content, category_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO media (media_type, category_id, content) VALUES ('text', %s, %s) RETURNING id",
            (category_id, text_content)
        )
        media_id = cursor.fetchone()[0]
        conn.commit()
        return media_id
    finally: release_db(conn)


def check_duplicate_text(content, category_id):
    """Check if identical text already exists in this category."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM media WHERE media_type='text' AND content=%s AND category_id=%s",
            (content, category_id)
        )
        return cursor.fetchone() is not None
    finally: release_db(conn)

# ================= Firewall DB Helpers =================

def get_firewall_channels():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, button_name, channel_id, invite_link FROM firewall_channels ORDER BY id")
        return cursor.fetchall()
    finally: release_db(conn)

def add_firewall_channel(button_name, channel_id, invite_link):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO firewall_channels (button_name, channel_id, invite_link) VALUES (%s, %s, %s)",
            (button_name, channel_id, invite_link)
        )
        conn.commit()
    finally: release_db(conn)

def remove_firewall_channel(fw_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM firewall_channels WHERE id = %s", (fw_id,))
        conn.commit()
    finally: release_db(conn)

def is_firewall_enabled():
    return get_setting('firewall_enabled', 'false') == 'true'

def set_firewall_enabled(enabled: bool):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES ('firewall_enabled', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ('true' if enabled else 'false',)
        )
        conn.commit()
    finally: release_db(conn)

def check_user_firewall(user_id):
    """
    Returns a list of (id, button_name, invite_link) for channels
    the user has NOT joined. Empty list = firewall passed.
    Admins bypass this check externally.
    """
    if not is_firewall_enabled(): return []
    channels = get_firewall_channels()
    if not channels: return []
    missing = []
    for fw_id, btn_name, ch_id, invite_link in channels:
        # Failsafe: Ignore corrupted entries with invalid URLs to prevent Telegram API crashes
        if not str(invite_link).startswith('http'):
            continue
            
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status in ('left', 'kicked'):
                missing.append((fw_id, btn_name, invite_link))
        except:
            missing.append((fw_id, btn_name, invite_link))
    return missing

def send_firewall_prompt(chat_id, missing_channels):
    """Send the force-join message with join buttons + a check button."""
    fw_msg = get_setting(
        'firewall_message',
        "🔒 **You must join our channel(s) to use this bot.**\n\nJoin below then press ✅ I Joined."
    )
    markup = InlineKeyboardMarkup(row_width=1)
    for _fw_id, btn_name, invite_link in missing_channels:
        markup.add(InlineKeyboardButton(f"🔗 {btn_name}", url=invite_link))
    markup.add(InlineKeyboardButton("✅ I Joined — Check Again", callback_data="fw_check_join"))
    bot.send_message(chat_id, fw_msg, reply_markup=markup, parse_mode="HTML")

# ================= UI & Keyboards =================
def get_main_keyboard(admin=False):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    categories = get_visible_categories()
    
    # Generate buttons for all categories dynamically
    cat_buttons = [KeyboardButton(c[1]) for c in categories]
    markup.add(*cat_buttons)
    
    markup.add(KeyboardButton("🔗 Referral"), KeyboardButton("💰 Balance"))
    if admin:
        markup.add(KeyboardButton("👑 Admin Panel"))
    return markup

def get_admin_panel_markup(user_id=None):
    markup = InlineKeyboardMarkup()
    
    # Aesthetic styling: Emoji Original Name
    markup.add(InlineKeyboardButton("⚡️ Analytics", callback_data="admin_stats"))
    
    markup.row(
        InlineKeyboardButton("🗂️ Categories", callback_data="admin_manage_categories"),
        InlineKeyboardButton("🔮 Media Library", callback_data="manage_cats")
    )
    markup.row(
        InlineKeyboardButton("📤 Upload Media", callback_data="admin_setcat"),
        InlineKeyboardButton("🔐 Access Rules", callback_data="admin_limits")
    )
    markup.row(
        InlineKeyboardButton("🛡️ Security Gate", callback_data="admin_firewall"),
        InlineKeyboardButton("⚙️ Settings", callback_data="admin_tools")
    )
    
    if user_id and is_super_admin(user_id):
        markup.add(InlineKeyboardButton("👑 Admin Team", callback_data="admin_manage_admins"))
        
    return markup

def generate_divisions_markup(cat_id):
    markup = InlineKeyboardMarkup()
    total_media = get_cat_stats(cat_id)
    
    if total_media == 0:
        markup.add(InlineKeyboardButton("No media in this category.", callback_data="ignore"))
        markup.add(InlineKeyboardButton("‹ Back to Categories", callback_data="manage_cats"))
        return markup
        
    chunk_size = 100
    total_chunks = math.ceil(total_media / chunk_size)
    
    for i in range(total_chunks):
        start_count = (i * chunk_size) + 1
        end_count = min((i + 1) * chunk_size, total_media)
        target_page = (i * chunk_size) // 5 
        btn_text = f"📂 Media {start_count} - {end_count}"
        markup.add(InlineKeyboardButton(btn_text, callback_data=f"manage_page_{cat_id}_{target_page}"))
        
    markup.add(InlineKeyboardButton("⚠️ Purge All", callback_data=f"wipe_media_init_{cat_id}"))
    markup.add(InlineKeyboardButton("‹ Back to Categories", callback_data="manage_cats"))
    return markup

def generate_manage_markup(cat_id, page):
    markup = InlineKeyboardMarkup()
    total_media = get_cat_stats(cat_id)
    
    limit = 5
    offset = page * limit
    total_pages = math.ceil(total_media / limit) if total_media > 0 else 1
    
    media_items = get_media_page(cat_id, limit, offset)
    
    if media_items:
        for m_id, m_type in media_items:
            preview_btn = InlineKeyboardButton(f"👁️ Preview [{m_type.upper()} {m_id}]", callback_data=f"preview_{m_id}_{cat_id}_{page}")
            delete_btn = InlineKeyboardButton(f"🗑️ Remove", callback_data=f"delmedia_{m_id}_{cat_id}_{page}")
            markup.add(preview_btn, delete_btn)
            
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage_page_{cat_id}_{page-1}"))
        else:
            nav_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
            
        nav_buttons.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="ignore"))
        
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage_page_{cat_id}_{page+1}"))
        else:
            nav_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
            
        markup.row(*nav_buttons)
        markup.add(InlineKeyboardButton("‹ Back to Folders", callback_data=f"manage_divs_{cat_id}"))
        markup.add(InlineKeyboardButton("⚠️ Purge All", callback_data=f"wipe_media_init_{cat_id}"))
    else:
        markup.add(InlineKeyboardButton("No media found.", callback_data="ignore"))
        markup.add(InlineKeyboardButton("‹ Back to Folders", callback_data=f"manage_divs_{cat_id}"))
        
    return markup

# ================= Bot Initialization =================
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("[ERROR] Please set your BOT_TOKEN in the configuration section at the top of the file.")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

def is_super_admin(user_id):
    """Super admins: only those set via the ADMIN_IDS environment variable."""
    return user_id in ADMIN_IDS

def is_admin(user_id):
    """Full admins: super admins + sub-admins added via /addadmin."""
    if user_id in ADMIN_IDS:
        return True
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM bot_admins WHERE user_id = %s", (user_id,))
        return cursor.fetchone() is not None
    except: return False
    finally: release_db(conn)

def add_admin_db(user_id, added_by):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO bot_admins (user_id, added_by) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, added_by)
        )
        conn.commit()
        return cursor.rowcount > 0
    except: return False
    finally: release_db(conn)

def remove_admin_db(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_admins WHERE user_id = %s", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally: release_db(conn)

def get_all_admins_db():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, added_at FROM bot_admins ORDER BY added_at DESC")
        return cursor.fetchall()
    finally: release_db(conn)

# ================= Primary Handlers =================
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    admin_mode = is_admin(user_id)
    
    args = message.text.split()
    referred_by = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id: referred_by = referrer_id

    is_new = add_user(user_id, username, FREE_STARTING_POINTS, referred_by)
    if is_new and referred_by:
        update_points(referred_by, REFERRAL_BONUS)
        try: bot.send_message(referred_by, f"🎉 Someone joined using your referral link! You earned {REFERRAL_BONUS} points.")
        except: pass
            
    if admin_mode:
        bot.reply_to(message, "<b>❖ SYSTEM ACCESS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Welcome to your control center.</i>", reply_markup=get_main_keyboard(admin=True), parse_mode="HTML")
    else:
        # 🛡️ Security Gate check for new/existing users on /start
        missing = check_user_firewall(user_id)
        if missing:
            send_firewall_prompt(message.chat.id, missing)
            return

        start_msg = get_setting('start_message', "<b>Welcome to the Media Bot!</b> 📺\n<blockquote>Use the menu below to navigate.</blockquote>")
        bot.reply_to(message, start_msg, reply_markup=get_main_keyboard(), parse_mode="HTML")

@bot.message_handler(commands=['newcategory'])
def handle_newcategory(message):
    if not is_admin(message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "Usage: `/newcategory <Name>`\nEx: `/newcategory 🏎️ Cars`", parse_mode="HTML")
        
    cat_name = args[1].strip()
    res = add_category(cat_name)
    if res:
        bot.reply_to(message, f"✅ Category `{cat_name}` created!\nYour keyboard has been updated.", reply_markup=get_main_keyboard(admin=True), parse_mode="HTML")
    else:
        bot.reply_to(message, "⚠️ A category with that name already exists.")

def flush_upload_batch(chat_id):
    with upload_lock:
        if chat_id in upload_batches:
            saved = upload_batches[chat_id]['saved']
            dupes = upload_batches[chat_id]['dupes']
            cat_name = upload_batches[chat_id]['cat_name']
            del upload_batches[chat_id]
            try:
                if dupes > 0: bot.send_message(chat_id, f"✅ Added {saved} media item(s) to *{cat_name}*.\n⚠️ Skipped {dupes} duplicate(s)!", parse_mode="HTML")
                else: bot.send_message(chat_id, f"✅ Added {saved} media item(s) to *{cat_name}*!", parse_mode="HTML")
            except: pass

def _build_session_text(cat_id, cat_name, ctype, admin_id=None):
    """Build the live upload session message text."""
    count = get_cat_stats(cat_id)
    type_labels = {'text': '📝 Text', 'gif_sticker': '🎬 GIF & Stickers', 'media': '🖼️ Photo / Video / Document'}
    type_label = type_labels.get(ctype, ctype)
    type_instructions = {
        'text': 'Send any text messages to add them to this category.',
        'gif_sticker': 'Send GIFs or Stickers to add them to this category.',
        'media': 'Send photos, videos, or documents to add them to this category.',
    }
    instruction = type_instructions.get(ctype, '')

    stats_line = ""
    if admin_id and admin_id in admin_session_stats:
        s = admin_session_stats[admin_id]
        stats_line = (
            f"\n<b>Session Summary:</b>\n"
            f"<i>Sent: {s['sent']} | Saved: {s['saved']} | Dupes: {s['dupes']}</i>\n"
        )

    return (
        f"<b>❖ UPLOAD SESSION ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        f"<i>Target: <b>{cat_name}</b></i>\n"
        f"<i>Expected: {type_label}</i>\n"
        f"<i>Current Items: {count}</i>\n"
        f"{stats_line}\n"
        f"<b>Instructions:</b>\n<i>{instruction}</i>\n\n"
        f"<i>Press <b>Done</b> when finished.</i>"
    )

def _build_session_markup(cat_id):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Done", callback_data=f"upload_done_{cat_id}"),
        InlineKeyboardButton("‹ Back", callback_data="admin_setcat")
    )
    return markup

def _update_session_message(admin_id):
    """Edit the live session message to reflect the current item count and stats."""
    info = admin_session_msg.get(admin_id)
    if not info: return
    chat_id, msg_id = info
    cat_id = admin_active_category.get(admin_id)
    ctype = admin_content_type.get(admin_id, 'media')
    if not cat_id: return
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    try:
        bot.edit_message_text(
            _build_session_text(cat_id, cat_name, ctype, admin_id),
            chat_id, msg_id,
            reply_markup=_build_session_markup(cat_id),
            parse_mode="Markdown"
        )
    except: pass

@bot.message_handler(content_types=['photo', 'video', 'document'])
def handle_media_upload(message):
    if not is_admin(message.from_user.id): return
    admin_id = message.from_user.id
    active_cat_id = admin_active_category.get(admin_id)
    if not active_cat_id: return  # no active session, silently ignore

    # Enforce content type
    ctype = admin_content_type.get(admin_id, 'media')
    if ctype != 'media':
        bot.reply_to(message, f"⚠️ This category only accepts <b>{'text' if ctype == 'text' else 'GIF & Stickers'}</b> content.", parse_mode="HTML")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        file_unique_id = message.photo[-1].file_unique_id
        media_type = 'photo'
    elif message.video:
        file_id = message.video.file_id
        file_unique_id = message.video.file_unique_id
        media_type = 'video'
    elif message.document:
        file_id = message.document.file_id
        file_unique_id = message.document.file_unique_id
        media_type = 'document'
    else: return

    if check_duplicate_media(file_unique_id):
        try: bot.delete_message(message.chat.id, message.message_id)
        except: pass
        s = admin_session_stats.setdefault(admin_id, {'sent': 0, 'saved': 0, 'dupes': 0})
        s['sent'] += 1; s['dupes'] += 1
        _update_session_message(admin_id)
        return
    add_media(file_id, media_type, file_unique_id, category_id=active_cat_id)
    try: bot.delete_message(message.chat.id, message.message_id)
    except: pass
    s = admin_session_stats.setdefault(admin_id, {'sent': 0, 'saved': 0, 'dupes': 0})
    s['sent'] += 1; s['saved'] += 1
    _update_session_message(admin_id)

@bot.message_handler(content_types=['animation', 'sticker'])
def handle_gif_sticker_upload(message):
    if not is_admin(message.from_user.id): return
    admin_id = message.from_user.id
    active_cat_id = admin_active_category.get(admin_id)
    if not active_cat_id: return

    ctype = admin_content_type.get(admin_id, 'media')
    if ctype != 'gif_sticker':
        bot.reply_to(message, "⚠️ This category does not accept GIFs or Stickers.", parse_mode="HTML")
        return

    if message.animation:
        file_id = message.animation.file_id
        file_unique_id = message.animation.file_unique_id
        media_type = 'animation'
    elif message.sticker:
        file_id = message.sticker.file_id
        file_unique_id = message.sticker.file_unique_id
        media_type = 'sticker'
    else: return

    if check_duplicate_media(file_unique_id):
        try: bot.delete_message(message.chat.id, message.message_id)
        except: pass
        s = admin_session_stats.setdefault(admin_id, {'sent': 0, 'saved': 0, 'dupes': 0})
        s['sent'] += 1; s['dupes'] += 1
        _update_session_message(admin_id)
        return
    add_media(file_id, media_type, file_unique_id, category_id=active_cat_id)
    try: bot.delete_message(message.chat.id, message.message_id)
    except: pass
    s = admin_session_stats.setdefault(admin_id, {'sent': 0, 'saved': 0, 'dupes': 0})
    s['sent'] += 1; s['saved'] += 1
    _update_session_message(admin_id)

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    admin_mode = is_admin(user_id)
    text = message.text

    # 🛡️ Security Gate check — blocks non-admins who haven't joined required channels
    if not admin_mode:
        missing = check_user_firewall(user_id)
        if missing:
            send_firewall_prompt(message.chat.id, missing)
            return
    
    if text == "💰 Balance":
        user = get_user(user_id)
        if not user: return bot.reply_to(message, "Please type /start first.")
        bot.reply_to(message, f"<b>❖ ACCOUNT INFO ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>💳 Balance: {user[2]} Points\n📅 Joined: {user[4]}</i>", parse_mode="HTML")
        return
        
    if text == "🔗 Referral":
        points = get_points(user_id)
        referral_link = f"https://t.me/{bot.get_me().username}?start={user_id}"
        bot.reply_to(message, f"<b>❖ REFERRAL SYSTEM ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>🏆 Your Points: {points}</i>\n\n<b>🔗 Invite Link:</b>\n<code>{referral_link}</code>\n\n<i>Invite friends to earn +{REFERRAL_BONUS} points each!</i>", parse_mode="HTML")
        return
        
    if text == "👑 Admin Panel" and admin_mode:
        bot.reply_to(message, "<b>❖ ADMIN CONTROL PANEL ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select an operation below to manage the bot.</i>", reply_markup=get_admin_panel_markup(user_id), parse_mode="HTML")
        return

    # 📝 Text upload during active session
    if admin_mode and text and not text.startswith('/'):
        active_cat_id = admin_active_category.get(user_id)
        ctype = admin_content_type.get(user_id)
        if active_cat_id and ctype == 'text':
            s = admin_session_stats.setdefault(user_id, {'sent': 0, 'saved': 0, 'dupes': 0})
            s['sent'] += 1
            if check_duplicate_text(text, active_cat_id):
                s['dupes'] += 1
            else:
                add_text_content(text, active_cat_id)
                s['saved'] += 1
            try: bot.delete_message(message.chat.id, message.message_id)
            except: pass
            _update_session_message(user_id)
            return

    # Check if text targets a Media Category dynamically
    categories = get_categories()
    for cat_id, cat_name, cat_hidden in categories:
        if text == cat_name:
            process_media_request(message, cat_id, cat_name, admin_mode)
            return

def process_media_request(message, cat_id, cat_name, admin_mode):
    user_id = message.from_user.id
    points = get_points(user_id)
    ctype = get_category_content_type(cat_id)  # 'media', 'gif_sticker', or 'text'
    is_free = ctype in ('text', 'gif_sticker')  # non-media types are always free

    if not admin_mode:
        # Referral gate (applies to all types)
        req_refs = get_category_req(cat_id)
        if req_refs > 0:
            actual_refs = get_total_referrals(user_id)
            if actual_refs < req_refs:
                return bot.reply_to(message, f"<b>❖ ACCESS DENIED ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>The <b>{cat_name}</b> category requires at least <b>{req_refs} referrals</b> to unlock.\n\nYou currently have {actual_refs} referrals.</i>\n\n<b>Tip:</b> <i>Use your Referral Link to invite more friends!</i>", parse_mode="HTML")

        # Points gate — only for paid (media) categories
        if not is_free and points < MEDIA_COST:
            return bot.reply_to(message, f"⚠️ You don't have enough points left!\nClick '🔗 Referral Link' to get your invite link.")

    media = get_random_media(cat_id)
    if not media:
        return bot.reply_to(message, f"Currently there is no content available in {cat_name}. Check back later!")

    _id, file_id, media_type = media

    if not admin_mode and not is_free:
        update_points(user_id, -MEDIA_COST)
        update_media_received(user_id)
        increment_user_category_stat(user_id, cat_id)
        new_points = points - MEDIA_COST
        caption_tmpl = get_setting('media_caption', "<blockquote>Enjoy this from <b>{cat_name}</b>! 🍿</blockquote>\n<code>Remaining points: {points}</code>")
        caption_text = caption_tmpl.replace("{cat_name}", cat_name).replace("{points}", str(new_points))
    elif not admin_mode and is_free:
        update_media_received(user_id)
        increment_user_category_stat(user_id, cat_id)
        caption_text = f"🍿 {cat_name}"
    else:
        caption_text = f"🍿 {cat_name}\n[👑 Admin View: Unlimited]\n[ID: {_id}]"

    # Fetch the content field for text items
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM media WHERE id = %s", (_id,))
        row = cursor.fetchone()
        content_text = row[0] if row else None
    finally:
        release_db(conn)

    try:
        if media_type == 'text':
            bot.send_message(user_id, content_text or "📝 (empty)", parse_mode="HTML")
        elif media_type == 'animation':
            bot.send_animation(user_id, file_id, caption=caption_text if not is_free else None, parse_mode="HTML")
        elif media_type == 'sticker':
            bot.send_sticker(user_id, file_id)
        elif media_type == 'photo':
            bot.send_photo(user_id, file_id, caption=caption_text, parse_mode="HTML")
        elif media_type == 'video':
            bot.send_video(user_id, file_id, caption=caption_text, parse_mode="HTML")
        else:
            bot.send_document(user_id, file_id, caption=caption_text, parse_mode="HTML")
    except:
        # Refund points on failure if it was a paid request
        if not admin_mode and not is_free:
            update_points(user_id, MEDIA_COST)

# ================= Admin Callbacks =================

@bot.callback_query_handler(func=lambda call: call.data == "admin_firewall")
def cb_admin_firewall(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    _show_firewall_menu(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)

def _show_firewall_menu(chat_id, message_id=None):
    enabled = is_firewall_enabled()
    channels = get_firewall_channels()
    
    markup = InlineKeyboardMarkup(row_width=1)
    
    if enabled:
        markup.add(InlineKeyboardButton("🟢 Firewall: ENABLED (Click to Disable)", callback_data="fw_toggle_0"))
    else:
        markup.add(InlineKeyboardButton("🔴 Firewall: DISABLED (Click to Enable)", callback_data="fw_toggle_1"))
        
    markup.add(InlineKeyboardButton("✍️ Edit Firewall Alert", callback_data="fw_edit_msg"))
    markup.add(InlineKeyboardButton("➕ Add Channel", callback_data="fw_add_channel"))
    
    header = "<b>❖ SECURITY GATE ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    body = f"<i>Status: {'ENABLED' if enabled else 'DISABLED'}\n\nRequired Channels:</i>\n"
    
    if channels:
        for fw_id, btn_name, ch_id, inv_link in channels:
            body += f"• {btn_name} (<code>{ch_id}</code>)\n"
            markup.add(InlineKeyboardButton(f"🗑️ Remove {btn_name}", callback_data=f"fw_remove_{fw_id}"))
    else:
        body += "<i>• No channels configured.</i>\n"
        
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    
    full_text = header + body
    
    if message_id:
        bot.edit_message_text(full_text, chat_id, message_id, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, full_text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("fw_toggle_"))
def cb_fw_toggle(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    enable = call.data.split('_')[2] == '1'
    set_firewall_enabled(enable)
    _show_firewall_menu(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, f"Firewall {'Enabled' if enable else 'Disabled'}")

@bot.callback_query_handler(func=lambda call: call.data == "fw_edit_msg")
def cb_fw_edit_msg(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    msg = bot.send_message(call.message.chat.id, "✍️ Send the new Firewall prompt message.\n\n_Note: Buttons attached automatically.\nTip: Use HTML tags like <b>bold</b> or <blockquote>quote</blockquote>_")
    bot.register_next_step_handler(msg, process_fw_edit_msg)
    bot.answer_callback_query(call.id)

def process_fw_edit_msg(message):
    if not is_admin(message.from_user.id): return
    set_setting('firewall_message', message.text)
    bot.reply_to(message, "✅ Firewall message updated.")
    _show_firewall_menu(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "fw_add_channel")
def cb_fw_add_channel(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    msg = bot.send_message(
        call.message.chat.id, 
        "➕ **Add Firewall Channel/Group**\n\n"
        "Send the details in this exact format:\n"
        "`Button Name - ChannelID - InviteLink`\n\n"
        "**Example:**\n"
        "`Join Our VIP - -10012345678 - https://t.me/+AbCdEfGh`\n\n"
        "_(Make sure the bot is an admin in that channel/group to verify members!)_",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_fw_add_channel)
    bot.answer_callback_query(call.id)

def process_fw_add_channel(message):
    if not is_admin(message.from_user.id): return
    try:
        # Split by " - " instead of just "-" to prevent breaking on negative IDs like -100xxx
        parts = message.text.split(' - ')
        if len(parts) < 3:
            bot.reply_to(message, "⚠️ Invalid format. Make sure to use spaces around the dashes: `Name - ID - Link`.", parse_mode="HTML")
            return _show_firewall_menu(message.chat.id)
            
        btn_name = parts[0].strip()
        ch_id = parts[1].strip()
        # Join the rest back in case the URL contains " - "
        link = ' - '.join(parts[2:]).strip()
        
        if not link.startswith('http'):
            bot.reply_to(message, "⚠️ Invalid link! Must start with http:// or https://")
            return _show_firewall_menu(message.chat.id)
            
        add_firewall_channel(btn_name, ch_id, link)
        bot.reply_to(message, f"✅ Added '{btn_name}' to firewall requirements!")
        _show_firewall_menu(message.chat.id)
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("fw_remove_"))
def cb_fw_remove(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    fw_id = int(call.data.split('_')[2])
    remove_firewall_channel(fw_id)
    _show_firewall_menu(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id, "✅ Channel removed")

@bot.callback_query_handler(func=lambda call: call.data == "fw_check_join")
def cb_fw_check_join(call):
    user_id = call.from_user.id
    missing = check_user_firewall(user_id)
    if missing:
        bot.answer_callback_query(call.id, "⚠️ You still haven't joined all required channels!", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "✅ Thank you for joining!", show_alert=True)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        bot.send_message(call.message.chat.id, "<b>❖ ACCESS GRANTED ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>You can now use the bot.</i>", reply_markup=get_main_keyboard(), parse_mode="HTML")

# ================= Chat Join Requests =================
@bot.chat_join_request_handler()
def handle_join_request(message):
    """Automatically approve users who request to join firewall channels."""
    try:
        bot.approve_chat_join_request(message.chat.id, message.from_user.id)
    except Exception as e:
        print(f"Failed to auto-approve join request: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_tools")
def cb_admin_tools(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("✍️ Edit Greeting", callback_data="tool_edit_start"))
    markup.add(InlineKeyboardButton("✍️ Edit Captions", callback_data="tool_edit_caption"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    bot.edit_message_text("<b>❖ SETTINGS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Customize your bot's automated messages.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "tool_edit_start")
def cb_edit_start_init(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, "<b>❖ EDIT GREETING ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Please send the new Start Message text.</i>\n\n<b>Tip:</b> <i>Use HTML tags like &lt;b&gt;, &lt;i&gt;, &lt;code&gt;.</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_start_msg_edit)
    bot.answer_callback_query(call.id)

def process_start_msg_edit(message):
    if not is_admin(message.from_user.id): return
    new_text = message.text
    set_setting('start_message', new_text)
    bot.reply_to(message, "✅ <b>Start Message Updated!</b>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "tool_edit_caption")
def cb_edit_caption_init(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, "<b>❖ EDIT CAPTIONS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Please send the new Media Caption Template.</i>\n\n<b>Available Placeholders:</b>\n<code>{cat_name}</code> - Category Name\n<code>{points}</code> - User Points Remaining\n\n<b>Example:</b> <i>Here is your {cat_name}! You have {points} left.</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_caption_edit)
    bot.answer_callback_query(call.id)

def process_caption_edit(message):
    if not is_admin(message.from_user.id): return
    new_text = message.text
    set_setting('media_caption', new_text)
    bot.reply_to(message, "✅ <b>Media Caption Updated!</b>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def cb_admin_stats(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    users_count, media_count, total_received = get_stats()
    text = (f"📊 **Bot Stats Dashboard**\n\n👥 **Total Registered Users:** {users_count}\n📦 **Total Media Uploaded:** {media_count}\n📤 **Total Media Distributed:** {total_received}")
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("👥 User List", callback_data="admin_user_list_0"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    
    try: bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except: pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_user_list_"))
def cb_admin_user_list(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    page = int(call.data.split('_')[3])
    limit = 10
    offset = page * limit
    
    users = get_user_list_page(limit, offset)
    users_count, _, _ = get_stats()
    total_pages = math.ceil(users_count / limit) if users_count > 0 else 1
    
    markup = InlineKeyboardMarkup()
    if users:
        for u_id, u_name, ref_count in users:
            display_name = u_name if u_name else f"ID:{u_id}"
            markup.add(InlineKeyboardButton(f"{display_name} ({ref_count} refs)", callback_data=f"user_detail_{u_id}_{page}"))
            
        nav_btns = []
        if page > 0: nav_btns.append(InlineKeyboardButton("⬅️", callback_data=f"admin_user_list_{page-1}"))
        nav_btns.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1: nav_btns.append(InlineKeyboardButton("➡️", callback_data=f"admin_user_list_{page+1}"))
        markup.row(*nav_btns)
        
    markup.add(InlineKeyboardButton("‹ Back to Stats", callback_data="admin_stats"))
    
    bot.edit_message_text("<b>❖ USER DIRECTORY ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select a user to view their full profile.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_detail_"))
def cb_admin_user_detail(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    pts = call.data.split('_')
    user_id = int(pts[2])
    back_page = int(pts[3])
    
    user = get_user_detail(user_id)
    if not user: return bot.answer_callback_query(call.id, "User not found")
    
    u_id, u_name, points, ref_by, join_date, total_media = user
    ref_count = get_total_referrals(user_id)
    breakdown = get_user_cat_breakdown(user_id)
    
    text = (f"👤 **User Profile: {u_name if u_name else 'N/A'}**\n\n"
            f"🆔 **ID:** `{u_id}`\n"
            f"📅 **Joined:** {join_date}\n"
            f"💰 **Points Balance:** {points}\n"
            f"👥 **Total Referrals:** {ref_count}\n"
            f"📦 **Total Media Extracted:** {total_media}\n\n"
            f"📑 **Category Breakdown:**\n")
    
    if breakdown:
        for cat_name, count in breakdown:
            text += f"- {cat_name}: {count}\n"
    else:
        text += "_No specific category data yet._"
        
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("💰 Give Points", callback_data=f"givepoints_init_{user_id}_{back_page}"),
        InlineKeyboardButton("‹ Back to List", callback_data=f"admin_user_list_{back_page}")
    )
    
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("givepoints_init_"))
def cb_givepoints_init(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    pts = call.data.split('_')
    target_uid = int(pts[2])
    back_page = pts[3]
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        f"💰 **Give Points to User `{target_uid}`**\n\n"
        f"Send the number of points to add (use negative to deduct, e.g. `-5`).\n"
        f"Type /cancel to abort.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_givepoints, target_uid, back_page)

@bot.callback_query_handler(func=lambda call: call.data == "admin_manage_categories")
def cb_manage_cats_main(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("➕ Create New Category", callback_data="admin_newcat"))
    markup.add(InlineKeyboardButton("✏️ Edit/Hide/Delete Categories", callback_data="admin_edit_cats_list"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    bot.edit_message_text("<b>❖ CATEGORIES ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Create, hide, or delete categories.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_edit_cats_list")
def cb_edit_cats_list(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cats = get_categories()
    markup = InlineKeyboardMarkup()
    for c_id, c_name, c_hidden in cats:
        status = "👻 " if c_hidden else ""
        markup.add(InlineKeyboardButton(f"{status}{c_name}", callback_data=f"edit_cat_opts_{c_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_manage_categories"))
    bot.edit_message_text("<b>❖ EDIT CATEGORY ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select a category to modify its properties.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_cat_opts_"))
def cb_edit_cat_opts(call):
    cat_id = int(call.data.split('_')[3])
    cats = get_categories()
    cat = next((c for c in cats if c[0] == cat_id), None)
    if not cat: return bot.answer_callback_query(call.id, "Category not found.")

    c_id, c_name, c_hidden = cat
    cat_ids = [c[0] for c in cats]
    idx = cat_ids.index(c_id)
    is_first = (idx == 0)
    is_last  = (idx == len(cat_ids) - 1)

    markup = InlineKeyboardMarkup(row_width=2)
    # Position controls
    up_btn   = InlineKeyboardButton("⬆️ Move Up",   callback_data=f"cat_move_{c_id}_up")
    down_btn = InlineKeyboardButton("⬇️ Move Down", callback_data=f"cat_move_{c_id}_down")
    none_btn = InlineKeyboardButton(" ",            callback_data="ignore")
    if is_first:
        markup.row(none_btn, down_btn)
    elif is_last:
        markup.row(up_btn, none_btn)
    else:
        markup.row(up_btn, down_btn)
    markup.add(InlineKeyboardButton(f"📄 Position: {idx + 1} of {len(cats)}", callback_data="ignore"))

    # Visibility
    if c_hidden:
        markup.add(InlineKeyboardButton("👁️ Unhide", callback_data=f"toggle_hide_{c_id}_0"))
    else:
        markup.add(InlineKeyboardButton("👻 Hide",   callback_data=f"toggle_hide_{c_id}_1"))

    markup.add(InlineKeyboardButton("❌ Delete Folder", callback_data=f"del_cat_init_{c_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_edit_cats_list"))

    status_text = "HIDDEN" if c_hidden else "VISIBLE"
    bot.edit_message_text(
        f"✏️ **Editing: {c_name}**\n"
        f"Status: `{status_text}` Position: `{idx + 1} of {len(cats)}`",
        call.message.chat.id, call.message.message_id,
        reply_markup=markup, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_move_"))
def cb_cat_move(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    parts = call.data.split('_')  # cat_move_{cat_id}_{up|down}
    cat_id = int(parts[2])
    direction = -1 if parts[3] == 'up' else 1
    move_category(cat_id, direction)
    bot.answer_callback_query(call.id, "✅ Position updated!")
    # Refresh the edit panel with the new order
    call.data = f"edit_cat_opts_{cat_id}"
    cb_edit_cat_opts(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle_hide_"))
def cb_toggle_hide(call):
    pts = call.data.split('_')
    cat_id = int(pts[2])
    hide = int(pts[3]) == 1
    toggle_category_visibility(cat_id, hide)
    bot.answer_callback_query(call.id, "Visibility updated!")
    cb_edit_cats_list(call)

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_cat_init_"))
def cb_del_cat_init(call):
    cat_id = int(call.data.split('_')[3])
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⚠️ YES, DELETE AND WIPE MEDIA ⚠️", callback_data=f"del_cat_confirm_{cat_id}"))
    markup.add(InlineKeyboardButton("⚠️ Cancel", callback_data=f"edit_cat_opts_{cat_id}"))
    bot.edit_message_text("<b>❖ PURGE WARNING ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>⚠️ Deleting this category will permanently erase it AND ALL MEDIA INSIDE IT. This cannot be undone.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("del_cat_confirm_"))
def cb_del_cat_confirm(call):
    cat_id = int(call.data.split('_')[3])
    delete_category_db(cat_id)
    bot.answer_callback_query(call.id, "Category and media deleted!")
    cb_edit_cats_list(call)

@bot.callback_query_handler(func=lambda call: call.data == "admin_newcat")
def cb_admin_newcat(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "<b>❖ NEW CATEGORY ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>To create a new category, type:</i>\n<code>/newcategory &lt;Name&gt;</code>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_setcat")
def cb_admin_setcat(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cats = get_categories()
    if not cats: return bot.answer_callback_query(call.id, "No categories exist!")
    
    markup = InlineKeyboardMarkup()
    for c_id, c_name, c_hidden in cats: markup.add(InlineKeyboardButton(c_name, callback_data=f"setactive_{c_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    
    bot.edit_message_text("<b>❖ UPLOAD MEDIA ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select a category to set as your upload destination.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("setactive_"))
def cb_setactive(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[1])
    bot.answer_callback_query(call.id)

    if is_category_type_configured(cat_id):
        # Type already configured — show the category dashboard
        _show_category_dashboard(call, cat_id)
    else:
        # First time — ask for content type
        cats = get_categories()
        cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton("🖼️ Photo / Video / Document", callback_data=f"set_ctype_{cat_id}_media"),
            InlineKeyboardButton("🎬 GIF & Stickers",             callback_data=f"set_ctype_{cat_id}_gif_sticker"),
            InlineKeyboardButton("📝 Text Messages",                callback_data=f"set_ctype_{cat_id}_text"),
            InlineKeyboardButton("‹ Back",                         callback_data="admin_setcat")
        )
        bot.edit_message_text(
            f"<b>❖ CATEGORY SETUP ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Setup for: <b>{cat_name}</b></i>\n\n<i>This is your first time setting up this category.\nWhat type of content will it hold?</i>",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="HTML"
        )

def _show_category_dashboard(call, cat_id):
    """Edit (or send) the category dashboard message."""
    cats = get_categories()
    cat_row = next((c for c in cats if c[0] == cat_id), None)
    cat_name = cat_row[1] if cat_row else "Unknown"
    ctype = get_category_content_type(cat_id)
    count = get_cat_stats(cat_id)
    extractions = get_category_extractions(cat_id)

    type_labels = {
        'text': '📝 Text Messages',
        'gif_sticker': '🎬 GIF & Stickers',
        'media': '🖼️ Photo / Video / Document'
    }
    type_label = type_labels.get(ctype, ctype)

    text = (
        f"<b>❖ CATEGORY DASHBOARD ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        f"<i>Configuration for: <b>{cat_name}</b></i>\n\n"
        f"• <b>Type:</b> <i>{type_label}</i>\n"
        f"• <b>Items:</b> <i>{count}</i>\n"
        f"• <b>Stats:</b> <i>{extractions} extractions</i>"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Media",    callback_data=f"cat_add_content_{cat_id}"),
        InlineKeyboardButton("🗑️ Remove Content", callback_data=f"cat_delete_menu_{cat_id}")
    )
    markup.add(InlineKeyboardButton("🔄 Switch Type", callback_data=f"cat_change_type_{cat_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_setcat"))

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode="HTML")
    except:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_add_content_"))
def cb_cat_add_content(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[3])
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    ctype = get_category_content_type(cat_id)

    admin_active_category[call.from_user.id] = cat_id
    admin_content_type[call.from_user.id] = ctype
    admin_session_stats[call.from_user.id] = {'sent': 0, 'saved': 0, 'dupes': 0}

    bot.answer_callback_query(call.id, "✅ Upload session started!")
    sent = bot.send_message(
        call.message.chat.id,
        _build_session_text(cat_id, cat_name, ctype, call.from_user.id),
        reply_markup=_build_session_markup(cat_id),
        parse_mode="Markdown"
    )
    admin_session_msg[call.from_user.id] = (call.message.chat.id, sent.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_change_type_"))
def cb_cat_change_type(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[3])
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("🖼️ Photo / Video / Document", callback_data=f"set_ctype_{cat_id}_media"),
        InlineKeyboardButton("🎬 GIF & Stickers",             callback_data=f"set_ctype_{cat_id}_gif_sticker"),
        InlineKeyboardButton("📝 Text Messages",                callback_data=f"set_ctype_{cat_id}_text"),
        InlineKeyboardButton("‹ Back",                         callback_data=f"setactive_{cat_id}")
    )
    bot.edit_message_text(
        f"🏷️ **Change Type: {cat_name}**\n\n⚠️ Changing the content type will affect future uploads only.\nExisting content stays as-is.\n\nSelect new type:",
        call.message.chat.id, call.message.message_id,
        reply_markup=markup, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_delete_menu_"))
def cb_cat_delete_menu(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[3])
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    count = get_cat_stats(cat_id)

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📝 Delete Specific Content", callback_data=f"manage_divs_{cat_id}"),
        InlineKeyboardButton("🚨 Delete ALL Content 🚨",   callback_data=f"wipe_media_init_{cat_id}"),
        InlineKeyboardButton("‹ Back",                        callback_data=f"setactive_{cat_id}")
    )
    bot.edit_message_text(
        f"🗑️ **Delete Content: {cat_name}**\n\n"
        f"📄 Current items: **{count}**\n\n"
        f"Choose what to delete:",
        call.message.chat.id, call.message.message_id,
        reply_markup=markup, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_ctype_"))
def cb_set_content_type(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    parts = call.data.split('_', 3)  # set_ctype_{cat_id}_{type}
    cat_id = int(parts[2])
    ctype = parts[3]  # 'media', 'gif_sticker', or 'text'

    # Save the category content type to DB (also marks type_configured=TRUE)
    set_category_content_type(cat_id, ctype)
    bot.answer_callback_query(call.id, "✅ Content type saved!")

    # Show the category dashboard
    _show_category_dashboard(call, cat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("upload_done_"))
def cb_upload_done(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    admin_id = call.from_user.id
    cat_id = int(call.data.split('_')[2])
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    count = get_cat_stats(cat_id)

    # Pull session stats before clearing
    s = admin_session_stats.pop(admin_id, {'sent': 0, 'saved': 0, 'dupes': 0})
    admin_active_category.pop(admin_id, None)
    admin_content_type.pop(admin_id, None)
    admin_session_msg.pop(admin_id, None)

    # Build summary
    summary = (
        f"<b>❖ UPLOAD COMPLETE ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        f"<i>Category: <b>{cat_name}</b></i>\n\n"
        f"<b>Final Stats:</b>\n"
        f"<i>Sent: {s['sent']} | Saved: {s['saved']} | Dupes: {s['dupes']}</i>\n\n"
        f"<i>Total Items: {count}</i>"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add More",        callback_data=f"cat_add_content_{cat_id}"),
        InlineKeyboardButton("📂 Dashboard",       callback_data=f"setactive_{cat_id}")
    )
    markup.add(InlineKeyboardButton("‹ Back to Admin Panel", callback_data="admin_panel_back"))

    bot.edit_message_text(
        summary,
        call.message.chat.id, call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, f"✅ Done! {s['saved']} saved, {s['dupes']} dupes.")

@bot.callback_query_handler(func=lambda call: call.data == "admin_limits")
def cb_admin_limits(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cats = get_categories()
    markup = InlineKeyboardMarkup()
    for c_id, c_name, c_hidden in cats: 
        reqs = get_category_req(c_id)
        markup.add(InlineKeyboardButton(f"{c_name} (Req: {reqs} refs)", callback_data=f"manage_req_{c_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    bot.edit_message_text("<b>❖ CATEGORY LIMITS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Configure referral requirements.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_req_"))
def cb_manage_req(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[2])
    cats = get_categories()
    cat_name = next((c[1] for c in cats if c[0] == cat_id), "Unknown")
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"<b>❖ UPDATE LIMITS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>To change the requirement for <b>{cat_name}</b>, type:</i>\n<code>/setreq {cat_id} &lt;limit&gt;</code>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data == "admin_panel_back")
def cb_panel_back(call):
    if not is_admin(call.from_user.id): return
    bot.edit_message_text("<b>❖ ADMIN CONTROL PANEL ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select an operation below to manage the bot.</i>", call.message.chat.id, call.message.message_id, reply_markup=get_admin_panel_markup(call.from_user.id), parse_mode="HTML")
    bot.answer_callback_query(call.id)

# --- Media Management via Categories ---
@bot.callback_query_handler(func=lambda call: call.data == "manage_cats")
def cb_manage_cats(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cats = get_categories()
    markup = InlineKeyboardMarkup()
    for c_id, c_name, c_hidden in cats: 
        m_count = get_cat_stats(c_id)
        markup.add(InlineKeyboardButton(f"{c_name} ({m_count} items)", callback_data=f"manage_divs_{c_id}"))
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    
    bot.edit_message_text("<b>❖ MEDIA LIBRARY ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select a category to view its content.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_divs_"))
def cb_manage_divs(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[2])
    
    markup = generate_divisions_markup(cat_id)
    bot.edit_message_text("<b>❖ CATEGORY FOLDERS ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Select a chunk to manage:</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_page_"))
def cb_manage_page(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    pts = call.data.split('_')
    cat_id = int(pts[2])
    page = int(pts[3])
    
    markup = generate_manage_markup(cat_id, page)
    try: bot.edit_message_text("<b>❖ MEDIA EXPLORER ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Browse media items.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except: pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("preview_"))
def cb_preview(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    media_id = call.data.split('_')[1]
    media = get_media_by_id(media_id)
    if not media: return bot.answer_callback_query(call.id, "Media no longer exists.")
    file_id, media_type = media
    bot.answer_callback_query(call.id, "Sending preview...")
    
    try:
        msg = f"👀 **Preview ID: {media_id}**"
        if media_type == 'photo': bot.send_photo(call.message.chat.id, file_id, caption=msg, parse_mode="HTML")
        elif media_type == 'video': bot.send_video(call.message.chat.id, file_id, caption=msg, parse_mode="HTML")
        else: bot.send_document(call.message.chat.id, file_id, caption=msg, parse_mode="HTML")
    except: bot.send_message(call.message.chat.id, f"⚠️ Failed to preview Media ID {media_id}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delmedia_"))
def cb_delmedia(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    pts = call.data.split('_')
    media_id = pts[1]
    cat_id = int(pts[2])
    page = int(pts[3])
    
    if delete_media(media_id): bot.answer_callback_query(call.id, f"✅ Deleted!")
    else: bot.answer_callback_query(call.id, "Not found.")
    
    markup = generate_manage_markup(cat_id, page)
    try: bot.edit_message_text("<b>❖ MEDIA EXPLORER ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>Browse media items.</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    except: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_media_init_"))
def cb_wipe_init(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[3])
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⚠️ YES, DELETE EVERYTHING ⚠️", callback_data=f"wipe_media_confirm_{cat_id}"))
    markup.add(InlineKeyboardButton("⚠️ Cancel", callback_data=f"manage_divs_{cat_id}"))
    bot.edit_message_text("<b>❖ EMPTY CATEGORY ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>⚠️ Are you sure you want to permanently empty this entire category?</i>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("wipe_media_confirm_"))
def cb_wipe_confirm(call):
    if not is_admin(call.from_user.id): return bot.answer_callback_query(call.id, "Unauthorized")
    cat_id = int(call.data.split('_')[3])
    
    wipe_category(cat_id)
    bot.answer_callback_query(call.id, "Wiped!")
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("‹ Back to Categories", callback_data="manage_cats"))
    bot.edit_message_text("✅ Category emptied successfully.", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "ignore")
def cb_ignore(call): bot.answer_callback_query(call.id)

@bot.message_handler(commands=['setreq'])
def handle_setreq(message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 3:
        return bot.reply_to(message, "Usage: `/setreq <CategoryID> <Limit>`\nYou can get the Category ID from the Category Limits menu.", parse_mode="HTML")
    try:
        cat_id = int(args[1])
        limit = int(args[2])
        update_category_req(cat_id, limit)
        bot.reply_to(message, f"✅ Done! The category now requires <b>{limit}</b> referrals to access.", parse_mode="HTML")
    except:
        bot.reply_to(message, "Invalid number.")

def process_givepoints(message, target_uid, back_page):
    """Next-step handler: receives the points amount from admin."""
    if not is_admin(message.from_user.id): return
    if message.text and message.text.strip().lower() == '/cancel':
        return bot.reply_to(message, "⚠️ Cancelled.")
    try:
        amount = int(message.text.strip())
    except (ValueError, AttributeError):
        return bot.reply_to(message, "⚠️ Invalid number. Please send a whole number like `50` or `-10`.", parse_mode="HTML")

    update_points(target_uid, amount)
    new_balance = get_points(target_uid)
    action = "added" if amount >= 0 else "deducted"
    bot.reply_to(
        message,
        f"✅ **Done!** `{abs(amount)}` points {action} for user `{target_uid}`.\n"
        f"💰 Their new balance: **{new_balance} points**.",
        parse_mode="Markdown"
    )
    # Notify the user silently
    try:
        direction = f"+{amount}" if amount >= 0 else str(amount)
        bot.send_message(
            target_uid,
            f"🎁 An admin has adjusted your balance: **{direction} points**!\n"
            f"💰 Your new balance: **{new_balance} points**.",
            parse_mode="Markdown"
        )
    except: pass

@bot.message_handler(commands=['givepoints'])
def handle_givepoints(message):
    """Usage: /givepoints <user_id> <amount>"""
    if not is_admin(message.from_user.id): return
    args = message.text.split()
    if len(args) != 3:
        return bot.reply_to(
            message,
            "📋 **Usage:** `/givepoints <user_id> <amount>`\n"
            "_Examples:_\n"
            "`/givepoints 123456789 50` → add 50 points\n"
            "`/givepoints 123456789 -10` → deduct 10 points",
            parse_mode="Markdown"
        )
    try:
        target_uid = int(args[1])
        amount = int(args[2])
    except ValueError:
        return bot.reply_to(message, "⚠️ Both `user_id` and `amount` must be whole numbers.", parse_mode="HTML")

    user = get_user(target_uid)
    if not user:
        return bot.reply_to(message, f"⚠️ User `{target_uid}` not found in database.", parse_mode="HTML")

    update_points(target_uid, amount)
    new_balance = get_points(target_uid)
    action = "added" if amount >= 0 else "deducted"
    bot.reply_to(
        message,
        f"✅ `{abs(amount)}` points {action} for user `{target_uid}` (@{user[1] or 'no username'}).\n"
        f"💰 New balance: **{new_balance} points**.",
        parse_mode="Markdown"
    )
    try:
        direction = f"+{amount}" if amount >= 0 else str(amount)
        bot.send_message(
            target_uid,
            f"🎁 An admin has adjusted your balance: **{direction} points**!\n"
            f"💰 Your new balance: **{new_balance} points**.",
            parse_mode="Markdown"
        )
    except: pass

@bot.message_handler(commands=['search'])
def handle_search(message):
    """Usage: /search <user_id | @username | name>"""
    if not is_admin(message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        return bot.reply_to(
            message,
            "🔍 **User Search**\n\n"
            "**Usage:** `/search <query>`\n"
            "_Search by:_\n"
            "User ID (e.g. `/search 123456789`)\n"
            "Username (e.g. `/search john` or `/search @john`)",
            parse_mode="Markdown"
        )

    query = args[1].strip().lstrip('@')
    results = search_users(query)

    if not results:
        return bot.reply_to(message, f"🔍 No users found matching `{query}`.", parse_mode="HTML")

    markup = InlineKeyboardMarkup()
    for u_id, u_name, u_points in results:
        display = f"@{u_name}" if u_name else f"ID:{u_id}"
        markup.add(InlineKeyboardButton(
            f"{display} — {u_points} pts",
            callback_data=f"user_detail_{u_id}_0"
        ))

    bot.reply_to(
        message,
        f"🔍 **Search results for** `{query}` — {len(results)} found:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

# ================= Admin Management =================

@bot.callback_query_handler(func=lambda call: call.data == "admin_manage_admins")
def cb_manage_admins(call):
    if not is_super_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "⛔ Only super admins can manage admins.", show_alert=True)
    
    sub_admins = get_all_admins_db()
    markup = InlineKeyboardMarkup(row_width=1)
    
    header = "<b>❖ ADMIN TEAM ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
    body = "<b>Super Admins:</b>\n"
    for sa_id in ADMIN_IDS:
        body += f"• <code>{sa_id}</code>\n"
    
    body += f"\n<b>Sub-Admins ({len(sub_admins)}):</b>\n"
    if sub_admins:
        for sa_id, added_at in sub_admins:
            body += f"• <code>{sa_id}</code> (since {str(added_at)[:10]})\n"
            markup.add(InlineKeyboardButton(f"🚫 Remove {sa_id}", callback_data=f"removeadmin_confirm_{sa_id}"))
    else:
        body += "<i>• No sub-admins configured.</i>\n"
    
    body += "\n<b>Tip:</b> <i>Use /addadmin to add members.</i>"
    markup.add(InlineKeyboardButton("‹ Back", callback_data="admin_panel_back"))
    
    bot.edit_message_text(header + body, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("removeadmin_confirm_"))
def cb_removeadmin_confirm(call):
    if not is_super_admin(call.from_user.id):
        return bot.answer_callback_query(call.id, "⛔ Unauthorized", show_alert=True)
    target_uid = int(call.data.split('_')[2])
    if is_super_admin(target_uid):
        return bot.answer_callback_query(call.id, "⛔ Cannot remove a super admin.", show_alert=True)
    remove_admin_db(target_uid)
    bot.answer_callback_query(call.id, f"✅ Admin {target_uid} removed.")
    cb_manage_admins(call)

@bot.message_handler(commands=['addadmin'])
def handle_addadmin(message):
    """Super-admin only. Usage: /addadmin <user_id>"""
    if not is_super_admin(message.from_user.id):
        return bot.reply_to(message, "⛔ Only super admins can use this command.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return bot.reply_to(
            message,
            "<b>❖ COMMAND USAGE ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            "<i>To add a new admin, type:</i>\n"
            "<code>/addadmin &lt;user_id&gt;</code>\n\n"
            "<i>• The user must have started the bot.\n• Use /search to find user IDs.</i>",
            parse_mode="HTML"
        )
    target_uid = int(args[1])
    if is_super_admin(target_uid):
        return bot.reply_to(message, "ℹ️ That user is already a super admin.")
    success = add_admin_db(target_uid, message.from_user.id)
    if success:
        bot.reply_to(message, f"✅ User `{target_uid}` is now an admin!\nThey will see the Admin Panel next time they send /start.", parse_mode="HTML")
        try:
            bot.send_message(target_uid, "<b>❖ SYSTEM NOTICE ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<i>You have been granted <b>Admin access</b> to this bot!\nSend /start to see your Admin Panel.</i>", parse_mode="HTML")
        except: pass
    else:
        bot.reply_to(message, f"ℹ️ User `{target_uid}` is already an admin.", parse_mode="HTML")

@bot.message_handler(commands=['removeadmin'])
def handle_removeadmin(message):
    """Super-admin only. Usage: /removeadmin <user_id>"""
    if not is_super_admin(message.from_user.id):
        return bot.reply_to(message, "⛔ Only super admins can use this command.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return bot.reply_to(message, "<b>❖ COMMAND USAGE ❖</b>\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n<code>/removeadmin &lt;user_id&gt;</code>", parse_mode="HTML")
    target_uid = int(args[1])
    if is_super_admin(target_uid):
        return bot.reply_to(message, "⛔ Cannot remove a super admin. Remove them from the `ADMIN_IDS` environment variable instead.")
    success = remove_admin_db(target_uid)
    if success:
        bot.reply_to(message, f"✅ Admin `{target_uid}` has been removed.", parse_mode="HTML")
        try:
            bot.send_message(target_uid, "ℹ️ Your admin access to this bot has been revoked.")
        except: pass
    else:
        bot.reply_to(message, f"⚠️ User `{target_uid}` is not a sub-admin.", parse_mode="HTML")

@bot.message_handler(commands=['listadmins'])
def handle_listadmins(message):
    """List all admins. Super-admin only."""
    if not is_super_admin(message.from_user.id):
        return bot.reply_to(message, "⛔ Only super admins can use this command.")
    sub_admins = get_all_admins_db()
    text = "👥 **Admin List**\n\n"
    text += "🔒 **Super Admins** (from environment):\n"
    for sa_id in ADMIN_IDS:
        text += f"`{sa_id}`\n"
    text += f"\n👤 **Sub-Admins** ({len(sub_admins)}):\n"
    if sub_admins:
        for sa_id, added_at in sub_admins:
            text += f"`{sa_id}` — since {str(added_at)[:10]}\n"
    else:
        text += "_None yet. Use /addadmin to add one._"
    bot.reply_to(message, text, parse_mode="HTML")

# ================= Execution =================
# Supports two modes:
#   POLLING  (local dev)  — no extra env vars needed, just run the script.
#   WEBHOOK  (web hosting) — set WEBHOOK_URL=https://your-app.onrender.com
#                            and optionally WEBHOOK_PORT (default 8080).
#
# On Koyeb / Render / Railway: set WEBHOOK_URL, expose the HTTP port,
#   the bot will register the webhook and serve Telegram updates via Flask.

WEBHOOK_URL  = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH = f"/{BOT_TOKEN}"  # secret path Telegram will POST to

if __name__ == "__main__":
    init_db()
    print("Database initialized.")

    if WEBHOOK_URL and Flask is not None:
        # ── WEBHOOK MODE (web hosting) ──────────────────────────────────
        print(f"Starting in WEBHOOK mode on port {WEBHOOK_PORT}")
        print(f"Webhook URL: {WEBHOOK_URL}{WEBHOOK_PATH}")

        app = Flask(__name__)

        @app.route("/", methods=["GET"])
        def health():
            """Health-check endpoint required by most hosting platforms."""
            return "Bot is running in Webhook mode!", 200

        @app.route(WEBHOOK_PATH, methods=["POST"])
        def webhook():
            if request.headers.get("content-type") == "application/json":
                json_str = request.get_data(as_text=True)
                update = telebot.types.Update.de_json(json_str)
                bot.process_new_updates([update])
                return "", 200
            else:
                abort(403)

        # Remove any old webhook and set the new one
        bot.remove_webhook()
        bot.set_webhook(url=f"{WEBHOOK_URL}{WEBHOOK_PATH}")
        print("Webhook registered. Flask server starting...")

        app.run(host="0.0.0.0", port=WEBHOOK_PORT)

    else:
        # ── POLLING MODE (local / Render background worker) ─────────────
        if WEBHOOK_URL and Flask is None:
            print("WARNING: WEBHOOK_URL is set but Flask is not installed.")
            print("Run: pip install flask. Falling back to POLLING mode.")
            
        print("Starting in POLLING mode...")
        bot.remove_webhook()  # clear any stale webhook
        
        # Start a dummy Flask server in a separate thread to satisfy Render/Koyeb port scans
        if Flask is not None and os.environ.get("PORT"):
            port = int(os.environ.get("PORT"))
            app = Flask(__name__)
            @app.route("/")
            def health():
                return "Bot is polling happily in the background!", 200
            
            import threading
            import time
            import requests

            def self_ping():
                """Periodically pings the health server to prevent Render from sleeping."""
                time.sleep(30) # wait for server to start
                render_url = os.environ.get("RENDER_EXTERNAL_URL")
                
                # If no RENDER_EXTERNAL_URL, try to construct one or fallback to localhost
                ping_url = render_url if render_url else f"http://localhost:{port}"
                
                print(f"Anti-Sleep pinger targeting: {ping_url}")
                while True:
                    try:
                        # Ping the URL
                        res = requests.get(ping_url, timeout=10)
                        print(f"Self-ping to {ping_url} status: {res.status_code}. Staying awake!")
                    except Exception as e:
                        print(f"Self-ping failed: {e}")
                    time.sleep(600) # Ping every 10 minutes (Render sleeps after 15)

            threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True).start()
            threading.Thread(target=self_ping, daemon=True).start()
            print(f"Dummy health server and Anti-Sleep pinger started on port {port}.")

        try: 
            bot.infinity_polling()
        except Exception as e: 
            print(f"Polling error: {e}")
