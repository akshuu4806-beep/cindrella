import asyncio

import os
import re
import time
import uuid
from collections import defaultdict, deque
from html import escape
from contextlib import suppress
from datetime import timedelta
from io import BytesIO
from pyrogram import idle
from pyrogram import filters

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from datetime import datetime, timedelta
from pyrogram.errors import FloodWait
import asyncio


from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.errors import BadRequest

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = os.getenv("MONGO_URL")

mongo = AsyncIOMotorClient(MONGO_URL)

db = mongo.rose

users_col = db.users
groups_col = db.groups
warns_col = db.warns
filters_col = db.filters

# Imports ke paas add karein
import time
from html import escape

# LINK aur BIO CHCECKER KE LIYE REGEX AUR CACHE
BIO_LINK_PATTERNS = [r'http[s]?://\S+', r'www\.\S+', r't\.me/\S+', r'\S+\.(com|org|net|in|co|io|xyz|me|info)\b']
def has_link(text: str) -> bool:
    if not text: return False
    for pattern in BIO_LINK_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE): return True
    return False

# FloodWait se bachne ke liye user bios ko 10 min tak cache karenge
bio_cache = {}

load_dotenv()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "rose_clone")
mongo_client: AsyncIOMotorClient | None = None
mongo_db = None
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client["RoseBot"] # Yahi 'db' missing tha
filters_db = db["chat_filters"] # Filters ke liye

LINK_RE = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)



# Inhe script ke upar, functions se bahar rakheinasync def add_filter(client: Client, message: Message):
active_tagging = {} 
purge_running = {}

FLOOD_WINDOW_SEC = 7
FLOOD_LIMIT = 6
flood_tracker: dict[tuple[int, int], deque] = defaultdict(deque)

LOCK_TYPES = {"media", "sticker", "gif", "voice", "poll", "link"}

LANG = {
    "en": {"admin_only": "Admin only command.", "reloaded": "Reloaded runtime caches.", "lang_set": "Language updated.", "tag_none": "No active users cached yet."},
    "hi": {"admin_only": "Ye command sirf admin ke liye hai.", "reloaded": "Runtime cache reload ho gaya.", "lang_set": "Bhasha update ho gayi.", "tag_none": "Abhi active users cache nahi hai."},
}
active_users: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=300))

HELP_SECTIONS: dict[str, tuple[str, str]] = {
    "home": ("📚 Help Menu", "Select any category button below."),
    "admin": ("👮 ADMIN", "/adminlist /promote /demote /setgtitle /setgpic /setgdesc /admincache"),
    "antiflood": ("🌊 ANTIFLOOD", "/flood on|off /setflood <number> /setfloodmode <mute|ban|kick|delete> /clearflood on|off /antiraid on|off"),
    "approval": ("✅ APPROVAL", "/approve /unapprove /approved"),
    "bans": ("🔨 BANS", "/ban /sban /dban /tban /unban /kickme /kick /dkick /skick"),
    "blocklist": ("🚫 BLOCKLIST", "/blocklist /addblock /unblock /blocklistmode <action>"),
    "captcha": ("🧩 CAPTCHA", "/captcha on|off"),
    "cleancmd": ("🧹 CLEAN COMMAND", "/cleancommands on|off /cleanfor <cmd> on|off"),
    "disabling": ("⛔ DISABLING", "/disable /enable /disabled"),
    "cleanservice": ("🧼 CLEAN SERVICE", "/cleanservice on|off"),
    "federation": ("🌐 FEDERATION", "/newfed /joinfed /fedinfo /fban /unfban"),
    "connection": ("🔌 CONNECTION", "/connection"),
    "filters": ("🔎 FILTERS", "/filter /stop /filters"),
    "formatting": ("✍️ FORMATTING", "/formatting"),
    "greetings": ("👋 GREETINGS", "/setwelcome /resetwelcome /welcome on|off /setgoodbye /resetgoodbye /goodbye on|off"),
    "language": ("🈯 LANGUAGE", "/language en|hi"),
    "locks": ("🔐 LOCKS", "/lock /unlock"),
    "notes": ("🗒 NOTES", "/setnote /get /delnote"),
    "pin": ("📌 PIN", "/pin /unpin"),
    "logchannels": ("📡 LOGCHANNELS", "/setlogchannel /logchannel"),
    "privacy": ("🔏 PRIVACY", "Use /owner in PM for privacy/config details."),
    "privacydata": ("📦 PRIVACY DATA", "/ddata /deldata (private-chat only)"),
    "purges": ("🗑 PURGES", "/purge"),
    "reports": ("🚨 REPORTS", "/report"),
    "rules": ("📜 RULES", "/setrules /rules"),
    "topic": ("🧵 TOPIC", "Topic tools placeholder (extend as needed)."),
    "warning": ("⚠️ WARNING", "/warn /dwarn /warnings /resetwarns"),
    "silent": ("🤫 SILENT POWER", "/smute /sban /skick /dmute /dwarn"),
    "importexport": ("📤 IMPORT/EXPORT", "/exportdata /importdata <json>"),
    "mics": ("🎙 MICS", "/mics on|off"),
    "extra": ("✨ EXTRA", "/id /info /reload /utag /atag"),
    "bio": ("🧬 BIO CHECK", "Bio check module placeholder (extend as needed)."),
    "antiabuse": ("🛡 ANTIABUSE", "Anti-abuse module placeholder (extend as needed)."),
    "all": ("📖 ALL", "/start /help /owner /adminlist /promote /demote /flood /approve /ban /kick /warn /cleancommands /cleanservice /filter /formatting /language /lock /setnote /pin /purge /report /rules /captcha /fban"),
}

def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ADMIN", callback_data="help:admin"), InlineKeyboardButton("ANTIFLOOD", callback_data="help:antiflood"), InlineKeyboardButton("APPROVAL", callback_data="help:approval")],
        [InlineKeyboardButton("BANS", callback_data="help:bans"), InlineKeyboardButton("BLOCKLIST", callback_data="help:blocklist"), InlineKeyboardButton("CAPTCHA", callback_data="help:captcha")],
        [InlineKeyboardButton("CLEAN COMMAND", callback_data="help:cleancmd"), InlineKeyboardButton("DISABLING", callback_data="help:disabling"), InlineKeyboardButton("CLEAN SERVICE", callback_data="help:cleanservice")],
        [InlineKeyboardButton("FEDERATION", callback_data="help:federation"), InlineKeyboardButton("CONNECTION", callback_data="help:connection"), InlineKeyboardButton("FILTERS", callback_data="help:filters")],
        [InlineKeyboardButton("FORMATTING", callback_data="help:formatting"), InlineKeyboardButton("GREETINGS", callback_data="help:greetings"), InlineKeyboardButton("IMPORT/EXPORT", callback_data="help:importexport")],
        [InlineKeyboardButton("LANGUAGE", callback_data="help:language"), InlineKeyboardButton("LOCKS", callback_data="help:locks"), InlineKeyboardButton("NOTES", callback_data="help:notes")],
        [InlineKeyboardButton("PIN", callback_data="help:pin"), InlineKeyboardButton("LOGCHANNELS", callback_data="help:logchannels"), InlineKeyboardButton("PRIVACY", callback_data="help:privacy")],
        [InlineKeyboardButton("PRIVACY DATA", callback_data="help:privacydata"), InlineKeyboardButton("PURGES", callback_data="help:purges"), InlineKeyboardButton("REPORTS", callback_data="help:reports")],
        [InlineKeyboardButton("RULES", callback_data="help:rules"), InlineKeyboardButton("TOPIC", callback_data="help:topic"), InlineKeyboardButton("MICS", callback_data="help:mics")],
        [InlineKeyboardButton("WARNING", callback_data="help:warning"), InlineKeyboardButton("SILENT POWER", callback_data="help:silent"), InlineKeyboardButton("EXTRA", callback_data="help:extra")],
        [InlineKeyboardButton("BIO CHECK", callback_data="help:bio"), InlineKeyboardButton("ANTIABUSE", callback_data="help:antiabuse"), InlineKeyboardButton("ALL", callback_data="help:all")],
    ])

def start_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add To Group", url=f"https://t.me/{bot_username}?startgroup=true"), InlineKeyboardButton("Get Your Own Bot", callback_data="start:ownbot")],
        [InlineKeyboardButton("Update", url="https://t.me/+rjE5xZlIK4U3ODA1")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="help:home")]])

def dm_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Go DM", url=f"https://t.me/{bot_username}?start=help")]])

def start_back_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"start:menu:{bot_username}")]])

def to_bool_str(v: bool) -> str:
    return "1" if v else "0"

def get_args(message: Message) -> list[str]:
    return message.command[1:] if len(message.command) > 1 else []

def get_filter_data(message: Message):
    """Extract text or media from message for saving in MongoDB"""
    if message.text:
        return {"type": "text", "data": message.text}
    elif message.sticker:
        return {"type": "sticker", "file_id": message.sticker.file_id}
    elif message.photo:
        return {"type": "photo", "file_id": message.photo.file_id, "caption": message.caption if message.caption else ""}
    elif message.video:
        return {"type": "video", "file_id": message.video.file_id, "caption": message.caption if message.caption else ""}
    elif message.animation:
        return {"type": "animation", "file_id": message.animation.file_id}
    elif message.voice:
        return {"type": "voice", "file_id": message.voice.file_id}
    return None

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS federations (
                fed_id TEXT PRIMARY KEY, 
                fed_name TEXT, 
                owner_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS fed_admins (
                fed_id TEXT, 
                user_id INTEGER, 
                PRIMARY KEY(fed_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS fban_list (
                fed_id TEXT, 
                user_id INTEGER, 
                reason TEXT,
                PRIMARY KEY(fed_id, user_id)
            );
        
            CREATE TABLE IF NOT EXISTS warns (chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, PRIMARY KEY(chat_id, user_id));
            CREATE TABLE IF NOT EXISTS notes (chat_id INTEGER, name TEXT, content TEXT, PRIMARY KEY(chat_id, name));
            CREATE TABLE IF NOT EXISTS approved (chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id, user_id));
            CREATE TABLE IF NOT EXISTS fed_membership (chat_id INTEGER PRIMARY KEY, fed_name TEXT);
            CREATE TABLE IF NOT EXISTS fban (fed_name TEXT, user_id INTEGER, reason TEXT, PRIMARY KEY(fed_name, user_id));
            CREATE TABLE IF NOT EXISTS clean_cmd_rules (chat_id INTEGER, command TEXT, enabled INTEGER DEFAULT 1, PRIMARY KEY(chat_id, command));
            CREATE TABLE IF NOT EXISTS disabled_commands (chat_id INTEGER, command TEXT, PRIMARY KEY(chat_id, command));
            CREATE TABLE IF NOT EXISTS user_activity (chat_id INTEGER, user_id INTEGER, msg_count INTEGER DEFAULT 0, last_seen INTEGER, PRIMARY KEY(chat_id, user_id));
            CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key));
        """)
        await db.commit()
# ... init_mongo function ke baad ye do functions add karo

# MongoDB Collection setup (Upar check karein 'db' variable define hona chahiye)
filters_db = db["filters"]

async def add_filter_mongo(chat_id, keyword, content):
    await filters_db.update_one(
        {"chat_id": chat_id, "keyword": keyword.lower()},
        {"$set": {"content": content}},
        upsert=True
    )

async def get_filters_mongo(chat_id):
    cursor = filters_db.find({"chat_id": chat_id})
    return await cursor.to_list(length=1000)

async def delete_filter_mongo(chat_id, keyword):
    await filters_db.delete_one({"chat_id": chat_id, "keyword": keyword.lower()})

async def get_rules(chat_id: int) -> str | None:
    """Retrieve rules for a chat from MongoDB."""
    if mongo_db is None:
        return None
    doc = await mongo_db.rules.find_one({"chat_id": chat_id})
    return doc["rules_text"] if doc else None

async def set_rules(chat_id: int, rules_text: str) -> None:
    """Store or update rules for a chat in MongoDB."""
    if mongo_db is None:
        return
    await mongo_db.rules.update_one(
        {"chat_id": chat_id},
        {"$set": {"rules_text": rules_text}},
        upsert=True
    )

async def init_mongo() -> None:
    global mongo_client, mongo_db
    if not MONGO_URI: return
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    mongo_db = mongo_client[MONGO_DB_NAME]
    await mongo_db["bot_meta"].update_one({"_id": "config"}, {"$set": {"owner_id": OWNER_ID, "db_name": MONGO_DB_NAME}}, upsert=True)

async def get_chat_settings(chat_id: int) -> dict:
    """MongoDB se chat ki settings lao, agar nahi to default do"""
    if mongo_db is None:
        return {}
    doc = await mongo_db.chat_settings.find_one({"chat_id": chat_id})
    if doc:
        return doc
    # Default settings
    return {
        "chat_id": chat_id,
        "welcome": {"type": "text", "text": "Welcome {fullname}!", "file_id": None, "caption": None},
        "welcome_enabled": False,
        "goodbye": {"type": "text", "text": "Goodbye {fullname}!", "file_id": None, "caption": None},
        "goodbye_enabled": False
    }

async def update_chat_settings(chat_id: int, **kwargs) -> None:
    """Chat settings update karo (upsert)"""
    if mongo_db is None:
        return
    await mongo_db.chat_settings.update_one(
        {"chat_id": chat_id},
        {"$set": kwargs},
        upsert=True
    )

# --- BIO SYSTEM MONGODB HELPERS (CORRECTED) ---
async def get_bio_config(chat_id: int):
    if mongo_db is not None:
        doc = await mongo_db.bio_config.find_one({"chat_id": chat_id})
        if doc:
            # warn_limit, action, enabled
            return doc.get("warn_limit", 5), doc.get("action", "mute"), doc.get("enabled", False)
    return 5, "mute", False

async def set_bio_config(chat_id: int, key: str, value):
    if mongo_db is not None:
        await mongo_db.bio_config.update_one({"chat_id": chat_id}, {"$set": {key: value}}, upsert=True)

async def increment_bio_stat(field: str):
    # Purana: if mongo_db:
    # Naya: if mongo_db is not None:
    if mongo_db is not None:
        await mongo_db.system_info.update_one(
            {"type": "stats"}, 
            {"$inc": {field: 1}}, 
            upsert=True
        )

from datetime import datetime

def format_welcome_text(template: str | None, user) -> str:
    """User ke variables ko replace karo – safe version"""
    if template is None:
        return ""
    replacements = {
        "{username}": f"@{user.username}" if user.username else "No username",
        "{fullname}": f"{user.first_name} {user.last_name or ''}".strip(),
        "{firstname}": user.first_name,
        "{lastname}": user.last_name or "",
        "{id}": str(user.id),
        "{mention}": f"<a href='tg://user?id={user.id}'>{user.first_name}</a>",
        "{date}": datetime.now().strftime("%Y-%m-%d"),
        "{time}": datetime.now().strftime("%H:%M:%S"),
        "{datetime}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template

async def get_chat_setting(chat_id: int, key: str, default: str = "0") -> str:
    """MongoDB se chat ki specific setting lao"""
    if mongo_db is None:
        return default
    doc = await mongo_db.chat_settings.find_one({"chat_id": chat_id})
    if doc and key in doc:
        return str(doc[key])
    return default

async def set_chat_setting(chat_id: int, key: str, value: str) -> None:
    """MongoDB mein setting store karo"""
    if mongo_db is None:
        return
    await mongo_db.chat_settings.update_one(
        {"chat_id": chat_id},
        {"$set": {key: value}},
        upsert=True
    )

async def get_setting(chat_id: int, key: str, default: str | None = None) -> str | None:
    """SQLite se setting value lo"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key)) as cur:
            row = await cur.fetchone()
    return row[0] if row else default

async def set_setting(chat_id: int, key: str, value: str) -> None:
    """SQLite mein setting store karo"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings(chat_id, key, value) VALUES(?,?,?)", (chat_id, key, value))
        await db.commit()

async def get_lang(chat_id: int) -> str:
    lang = (await get_setting(chat_id, "language", "en") or "en").lower()
    return lang if lang in LANG else "en"

def tr(lang: str, key: str) -> str:
    return LANG.get(lang, LANG["en"]).get(key, LANG["en"].get(key, key))

async def owner_cmd(client: Client, message: Message) -> None:
    owner_text = str(OWNER_ID) if OWNER_ID else "Not configured"
    await message.reply_text(f"Owner ID: <code>{owner_text}</code>", parse_mode=enums.ParseMode.HTML)

async def is_admin(client: Client, message: Message, uid: int | None = None) -> bool:
    chat = message.chat
    user = message.from_user
    if not chat or not (uid or user): return False
    try:
        member = await client.get_chat_member(chat.id, uid or user.id)
        return member.status in {enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}
    except Exception:
        return False

async def require_admin(client: Client, message: Message) -> bool:
    if OWNER_ID and message.from_user and message.from_user.id == OWNER_ID: return True
    if not await is_admin(client, message):
        lang = await get_lang(message.chat.id)
        await message.reply_text(tr(lang, "admin_only"))
        return False
    return True

async def is_approved(chat_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM approved WHERE chat_id=? AND user_id=?", (chat_id, user_id)) as cur:
            return (await cur.fetchone()) is not None

def target_user(message: Message) -> int | None:
    if message.reply_to_message: return message.reply_to_message.from_user.id
    return None

def parse_duration(raw: str) -> int:
    m = re.fullmatch(r"(\d+)([mhd])", raw.lower().strip())
    if not m: return 0
    num, unit = int(m.group(1)), m.group(2)
    if unit == "m": return num * 60
    if unit == "h": return num * 3600
    return num * 86400

async def start(client: Client, message: Message) -> None:
    me = await client.get_me()
    args = get_args(message)
    if message.chat.type == enums.ChatType.PRIVATE and args:
        arg = args[0].lower()
        if arg == "help":
            # existing help menu
            title, body = HELP_SECTIONS["home"]
            dynamic_title = title.replace("Help Menu", f"{me.first_name} Help")
            await message.reply_text(
                f"<b>{dynamic_title}</b>\n{body}",
                parse_mode=enums.ParseMode.HTML,
                reply_markup=help_keyboard()
            )
            return
        elif arg.startswith("rules_"):
            try:
                chat_id = int(arg.split("_")[1])
                rules_text = await get_rules(chat_id)
                if rules_text:
                    await message.reply_text(rules_text, parse_mode=enums.ParseMode.HTML)
                else:
                    await message.reply_text("No rules set for that group.")
            except (IndexError, ValueError):
                await message.reply_text("Invalid rules link.")
            return
    # Normal start message
    await message.reply_text(
        f"Hey there! My name is {me.first_name}— I'm here to help you manage your groups!\n"
        "Use /help to find out how to use me to my full potential.\n\n"
        "Check /privacy to view the privacy policy, and interact with your data.\n",
        reply_markup=start_keyboard(me.username)
    )

async def start_buttons(client: Client, callback_query: CallbackQuery) -> None:
    q = callback_query
    parts = q.data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    me = await client.get_me()
    bot_username = parts[2] if len(parts) > 2 else me.username
    bot_name = me.first_name

    if action == "menu":
        await q.edit_message_text(f"Hey there! My name is {bot_name}— I'm here to help you manage your groups!\nUse /help to find out how to use me to my full potential.\n\nCheck /privacy to view the privacy policy, and interact with your data.\n", reply_markup=start_keyboard(bot_username))
    elif action == "ownbot":
        await q.edit_message_text(f"Get your own clone of {bot_name} with your own bot token!:\n\nHow to clone:\n1) Create a new bot by BotFather\n2) Copy the bot token\n3) Use: /clone YOUR_TOKEN\n\nYour bot will have all the same features!\n", reply_markup=start_back_keyboard(bot_username))

async def clone(client: Client, message: Message) -> None:
    await message.reply_text("Clone guide:\ngit clone <repo-url>\ncd <repo>\npython -m venv .venv && source .venv/bin/activate\npip install -r requirements.txt\ncp .env.example .env (BOT_TOKEN set karo)\npython bot.py")

async def help_cmd(client: Client, message: Message) -> None:
    me = await client.get_me()
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("See in DM.", reply_markup=dm_keyboard(me.username))
        return
    title, body = HELP_SECTIONS["home"]
    dynamic_title = title.replace("Help Menu", f"{me.first_name} Help")
    await message.reply_text(f"<b>{dynamic_title}</b>\n\n{body}", parse_mode=enums.ParseMode.HTML, reply_markup=help_keyboard())

async def help_buttons(client: Client, callback_query: CallbackQuery) -> None:
    q = callback_query
    section = q.data.split(":", maxsplit=1)[1]
    me = await client.get_me()

    if q.message.chat.type != enums.ChatType.PRIVATE:
        await q.edit_message_text("See in DM.", reply_markup=dm_keyboard(me.username))
        return

    title, body = HELP_SECTIONS.get(section, HELP_SECTIONS["home"])
    if section == "home":
        dynamic_title = title.replace("Help Menu", f"{me.first_name} Help")
        await q.edit_message_text(f"<b>{dynamic_title}</b>\n\n{body}", parse_mode=enums.ParseMode.HTML, reply_markup=help_keyboard())
    else:
        await q.edit_message_text(body, parse_mode=enums.ParseMode.HTML, reply_markup=back_keyboard())

async def generic_toggle(client: Client, message: Message, key: str, label: str) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args or args[0].lower() not in {"on", "off"}:
        await message.reply_text(f"Usage: /{label} on|off")
        return
    enabled = args[0].lower() == "on"
    await set_setting(message.chat.id, key, to_bool_str(enabled))
    await message.reply_text(f"{label} {'enabled' if enabled else 'disabled'}.")

async def can_manage_filters(client: Client, message: Message) -> bool:
    # Agar user bot owner hai
    if message.from_user.id == OWNER_ID:
        return True
    
    # Group mein check karein
    member = await client.get_chat_member(message.chat.id, message.from_user.id)
    if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        if member.privileges and member.privileges.can_change_info:
            return True
            
    await message.reply_text("Aapke paas 'Change Group Info' permission honi chahiye.")
    return False

async def filter_cmd_handler(client: Client, message: Message):
    # Admin Permission Check (Group Info permission)
    if not await can_manage_filters(client, message):
        return

    # Split: /filter keyword <optional_text>
    args = message.text.split(None, 2)
    
    if len(args) < 2:
        return await message.reply_text("💡 **Usage:**\n1. Reply to media: `/filter hello` \n2. Direct text: `/filter hi how are you?`")

    keyword = args[1].lower()
    reply = message.reply_to_message
    content = None

    # Case 1: Agar kisi message par reply kiya hai
    if reply:
        content = get_filter_data(reply)
    # Case 2: Agar reply nahi kiya, par keyword ke baad text likha hai
    elif len(args) > 2:
        content = {"type": "text", "data": args[2]}

    if not content:
        return await message.reply_text("❌ Kripya reply karein ya keyword ke saath text likhein.")

    # MongoDB me save (filters_db use karein)
    await filters_db.update_one(
        {"chat_id": message.chat.id, "keyword": keyword},
        {"$set": {"content": content}},
        upsert=True
    )
    await message.reply_text(f"✅ Filter saved for: **{keyword}**")

async def stop_filter_handler(client: Client, message: Message):
    if not await can_manage_filters(client, message):
        return
        
    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.reply_text("Usage: `/stop <keyword>`")
        return
        
    await delete_filter_mongo(message.chat.id, args[1].lower())
    await message.reply_text(f"Stopped filter for: `{args[1]}`")

async def list_filters_handler(client: Client, message: Message):
    # Filters ki list dikhane ke liye
    filters_list = await filters_db.find({"chat_id": message.chat.id}).to_list(length=100)
    if not filters_list:
        return await message.reply_text("इस ग्रुप में कोई फ़िल्टर नहीं है।")
    
    reply = f"📑 **Filters in {message.chat.title}:**\n"
    for f in filters_list:
        reply += f"- `{f['keyword']}`\n"
    await message.reply_text(reply)

async def auto_filter_handler(client: Client, message: Message):
    if not message.from_user or message.from_user.is_bot: return

    chat_id = message.chat.id
    

    msg_text = (message.text or message.caption or "").lower()
    msg_words = set(re.findall(r"[a-zA-Z0-9]+", msg_text))
    username_words = set(re.findall(r"[a-zA-Z0-9]+", (message.from_user.username or "").lower()))
    fullname = f"{message.from_user.first_name} {message.from_user.last_name or ''}".lower()
    name_words = set(re.findall(r"[a-zA-Z0-9]+", fullname))

    # MongoDB se filters search karein
    cursor = filters_db.find({"chat_id": chat_id})
    async for f in cursor:
        keyword = f['keyword'].strip().lower()
        # Exact word match rakho; substring match (raj -> traj/rajput) avoid karo
        if keyword in msg_words or keyword in username_words or keyword in name_words:
            c = f['content']
            m_type = c['type']
            f_id = c.get('file_id')
            cap = c.get('caption', "")

            if m_type == "text":
                await message.reply_text(c['data'])
            elif m_type == "photo":
                await message.reply_photo(f_id, caption=cap)
            elif m_type == "sticker":
                await message.reply_sticker(f_id)
            elif m_type == "video":
                await message.reply_video(f_id, caption=cap)
            elif m_type == "animation":
                await message.reply_animation(f_id)
            return
    
async def setwelcome(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return

    data = {"type": "text", "text": "", "file_id": None, "caption": None}
    reply = message.reply_to_message

    if reply:
        if reply.photo:
            data = {'type': 'photo', 'file_id': reply.photo.file_id, 'caption': reply.caption or ""}
        elif reply.video:
            data = {'type': 'video', 'file_id': reply.video.file_id, 'caption': reply.caption or ""}
        elif reply.sticker:
            data = {'type': 'sticker', 'file_id': reply.sticker.file_id, 'caption': ""}
        elif reply.animation:
            data = {'type': 'animation', 'file_id': reply.animation.file_id, 'caption': reply.caption or ""}
        elif reply.document:
            data = {'type': 'document', 'file_id': reply.document.file_id, 'caption': reply.caption or ""}
        elif reply.voice:
            data = {'type': 'voice', 'file_id': reply.voice.file_id, 'caption': ""}
        elif reply.audio:
            data = {'type': 'audio', 'file_id': reply.audio.file_id, 'caption': reply.caption or ""}
        elif reply.text:
            data = {'type': 'text', 'text': reply.text, 'file_id': None, 'caption': None}
        else:
            args = get_args(message)
            if args:
                data = {'type': 'text', 'text': " ".join(args)}
            else:
                await message.reply_text("Usage: /setwelcome with text or reply to a media/text message")
                return
    else:
        args = get_args(message)
        if not args:
            await message.reply_text("Usage: /setwelcome <text> or reply to a message with /setwelcome")
            return
        data = {'type': 'text', 'text': " ".join(args)}

    # Update MongoDB
    await update_chat_settings(message.chat.id, welcome=data, welcome_enabled=True)
    await message.reply_text("✅ Welcome message set and enabled.")

async def resetwelcome(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return
    default_welcome = {"type": "text", "text": "Welcome {fullname}!", "file_id": None, "caption": None}
    await update_chat_settings(message.chat.id, welcome=default_welcome)
    await message.reply_text("✅ Welcome reset to default.")

async def welcome_toggle(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return
    args = get_args(message)
    if not args or args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /welcome on|off")
        return
    enabled = args[0].lower() == "on"
    await update_chat_settings(message.chat.id, welcome_enabled=enabled)
    await message.reply_text(f"Welcome {'enabled' if enabled else 'disabled'}.")

async def setgoodbye(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return

    data = {"type": "text", "text": "", "file_id": None, "caption": None}
    reply = message.reply_to_message

    if reply:
        if reply.photo:
            data = {'type': 'photo', 'file_id': reply.photo.file_id, 'caption': reply.caption or ""}
        elif reply.video:
            data = {'type': 'video', 'file_id': reply.video.file_id, 'caption': reply.caption or ""}
        elif reply.sticker:
            data = {'type': 'sticker', 'file_id': reply.sticker.file_id, 'caption': ""}
        elif reply.animation:
            data = {'type': 'animation', 'file_id': reply.animation.file_id, 'caption': reply.caption or ""}
        elif reply.document:
            data = {'type': 'document', 'file_id': reply.document.file_id, 'caption': reply.caption or ""}
        elif reply.voice:
            data = {'type': 'voice', 'file_id': reply.voice.file_id, 'caption': ""}
        elif reply.audio:
            data = {'type': 'audio', 'file_id': reply.audio.file_id, 'caption': reply.caption or ""}
        elif reply.text:
            data = {'type': 'text', 'text': reply.text, 'file_id': None, 'caption': None}
        else:
            args = get_args(message)
            if args:
                data = {'type': 'text', 'text': " ".join(args)}
            else:
                await message.reply_text("Usage: /setgoodbye with text or reply to a media/text message")
                return
    else:
        args = get_args(message)
        if not args:
            await message.reply_text("Usage: /setgoodbye <text> or reply to a message with /setgoodbye")
            return
        data = {'type': 'text', 'text': " ".join(args)}

    await update_chat_settings(message.chat.id, goodbye=data, goodbye_enabled=True)
    await message.reply_text("✅ Goodbye message set and enabled.")

async def resetgoodbye(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return
    default_goodbye = {"type": "text", "text": "Goodbye {fullname}!", "file_id": None, "caption": None}
    await update_chat_settings(message.chat.id, goodbye=default_goodbye)
    await message.reply_text("✅ Goodbye reset to default.")

async def goodbye_toggle(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return
    args = get_args(message)
    if not args or args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /goodbye on|off")
        return
    enabled = args[0].lower() == "on"
    await update_chat_settings(message.chat.id, goodbye_enabled=enabled)
    await message.reply_text(f"Goodbye {'enabled' if enabled else 'disabled'}.")        

async def send_welcome_goodbye(client, chat_id, user, data, reply_to_message_id=None):
    """Data ke according message bhejo (with variable replacement)"""
    # Caption ya text ko safely get karo
    caption = data.get('caption') or data.get('text') or ''
    caption = format_welcome_text(caption, user)
    msg_type = data.get('type', 'text')
    file_id = data.get('file_id')

    try:
        if msg_type == 'photo' and file_id:
            await client.send_photo(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'video' and file_id:
            await client.send_video(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        elif msg_type == 'sticker' and file_id:
            await client.send_sticker(chat_id, file_id,reply_to_message_id=reply_to_message_id)
            if caption:
                await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        elif msg_type == 'animation' and file_id:
            await client.send_animation(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        elif msg_type == 'document' and file_id:
            await client.send_document(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        elif msg_type == 'voice' and file_id:
            await client.send_voice(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        elif msg_type == 'audio' and file_id:
            await client.send_audio(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
        else:
            await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)
    except Exception as e:
        print(f"Error sending: {e}")
        # Fallback to plain text
        await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML,reply_to_message_id=reply_to_message_id)

async def on_new_members(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    settings = await get_chat_settings(chat_id)
    if not settings.get("welcome_enabled", False):
        return
    welcome_data = settings.get("welcome", {})
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        await send_welcome_goodbye(client, chat_id, member, welcome_data,reply_to_message_id=message.id)

async def on_left_member(client: Client, event):
    # Agar ye message handler se aa raha hai (purana tarika)
    if isinstance(event, Message):
        chat_id = event.chat.id
        user = event.left_chat_member
        message_id = event.id
    # Agar ye ChatMemberUpdated handler se aa raha hai (naya tarika)
    else:
        chat_id = event.chat.id
        user = event.old_chat_member.user
        message_id = None # Member update mein message id nahi hoti

    if not user or user.is_bot:
        return

    settings = await get_chat_settings(chat_id)
    if not settings.get("goodbye_enabled", False):
        return

    goodbye_data = settings.get("goodbye", {})
    
    # Check karein ki user sach mein group chhod raha hai (Banned or Left)
    if not isinstance(event, Message):
        if event.new_chat_member and event.new_chat_member.status in {enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT}:
             await send_welcome_goodbye(client, chat_id, user, goodbye_data, reply_to_message_id=message_id)
    else:
        # Message filter ke liye purana logic
        await send_welcome_goodbye(client, chat_id, user, goodbye_data, reply_to_message_id=message_id)

async def setrules(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return

    rules_text = None
    if message.reply_to_message:
        # Use replied message's text or caption
        if message.reply_to_message.text:
            rules_text = message.reply_to_message.text
        elif message.reply_to_message.caption:
            rules_text = message.reply_to_message.caption

    if not rules_text:
        # Fallback to command arguments
        args = get_args(message)
        if not args:
            await message.reply_text("Usage: /setrules <text> or reply to a message with /setrules")
            return
        rules_text = " ".join(args)

    # Save to MongoDB
    await set_rules(message.chat.id, rules_text)
    await message.reply_text("Rules updated.")

async def rules(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    if message.chat.type != enums.ChatType.PRIVATE:
        # Group: show button that opens private chat with rules
        me = await client.get_me()
        deep_link = f"https://t.me/{me.username}?start=rules_{chat_id}"
        button = InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 Rules", url=deep_link)]
        ])
        await message.reply_text(
            "Click on the button below to see the chat rules.",
            reply_markup=button
        )
    else:
        # Private chat: just show rules if any (usually none)
        rules_text = await get_rules(chat_id)
        if rules_text:
            await message.reply_text(rules_text, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply_text("No rules set for this chat.")

async def setnote(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if len(args) < 2:
        await message.reply_text("Usage: /setnote <name> <content>")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO notes(chat_id,name,content) VALUES(?,?,?)", (message.chat.id, args[0].lower(), " ".join(args[1:])))
        await db.commit()
    await message.reply_text("Note saved.")

async def get_note(client: Client, message: Message) -> None:
    args = get_args(message)
    if not args: return
    name = args[0].lower()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT content FROM notes WHERE chat_id=? AND name=?", (message.chat.id, name)) as cur:
            row = await cur.fetchone()
    if row: await message.reply_text(row[0], parse_mode=enums.ParseMode.HTML)

async def delnote(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args: return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM notes WHERE chat_id=? AND name=?", (message.chat.id, args[0].lower()))
        await db.commit()
    await message.reply_text("Note deleted.")

async def warn_common(client: Client, message: Message, delete_cmd: bool = False) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO warns(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (chat_id, uid))
        async with db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid)) as cur:
            cnt = (await cur.fetchone())[0]
        await db.commit()
    limit = int(await get_setting(chat_id, "warn_limit", "3"))
    if cnt >= limit:
        await client.ban_chat_member(chat_id, uid)
        await client.unban_chat_member(chat_id, uid)
    await message.reply_text(f"Warn count: {cnt}/{limit}")
    if delete_cmd:
        with suppress(Exception): await message.delete()

async def warn(client: Client, message: Message) -> None: await warn_common(client, message)
async def dwarn(client: Client, message: Message) -> None: await warn_common(client, message, delete_cmd=True)

async def warnings(client: Client, message: Message) -> None:
    uid = target_user(message) or message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, uid)) as cur:
            row = await cur.fetchone()
    await message.reply_text(f"Warnings: {row[0] if row else 0}")

async def resetwarns(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, uid))
        await db.commit()
    await message.reply_text("Warns reset.")

async def do_restrict(client: Client, message: Message, mode: str) -> None:
    uid = target_user(message)
    if not uid: return
    args = get_args(message)
    if mode in {"mute", "dmute", "smute", "tmute"}:
        until_date = None
        if mode == "tmute" and args:
            secs = parse_duration(args[0])
            if secs > 0: until_date = int(time.time()) + secs
        await client.restrict_chat_member(message.chat.id, uid, ChatPermissions(can_send_messages=False), until_date=until_date)
        if mode == "dmute" and message.reply_to_message:
            with suppress(Exception): await message.reply_to_message.delete()
        if mode not in {"smute"}: await message.reply_text("Muted.")
    else:
        if mode == "dban" and message.reply_to_message:
            with suppress(Exception): await message.reply_to_message.delete()
        until_date = None
        if mode == "tban" and args:
            secs = parse_duration(args[0])
            if secs > 0: until_date = int(time.time()) + secs
        await client.ban_chat_member(message.chat.id, uid, until_date=until_date)
        if mode in {"kick", "dkick", "skick"}:
            await client.unban_chat_member(message.chat.id, uid)
        if mode not in {"sban", "skick"}: await message.reply_text("Action completed.")
    if mode in {"dwarn", "dmute", "dkick"}:
        with suppress(Exception): await message.delete()

async def mute(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "mute")
async def dmute(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "dmute")
async def smute(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "smute")
async def tmute(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "tmute")
async def unmute(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    await client.restrict_chat_member(message.chat.id, uid, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
    await message.reply_text("Unmuted.")

async def ban(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "ban")
async def sban(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "sban")
async def tban(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "tban")
async def dban(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "dban")
async def unban(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args: return
    await client.unban_chat_member(message.chat.id, int(args[0]))
    await message.reply_text("Unbanned.")

async def kick(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "kick")
async def dkick(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "dkick")
async def skick(client: Client, message: Message) -> None: 
    if await require_admin(client, message): await do_restrict(client, message, "skick")
async def kickme(client: Client, message: Message) -> None:
    uid = message.from_user.id
    await client.ban_chat_member(message.chat.id, uid)
    await client.unban_chat_member(message.chat.id, uid)

async def lock(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args or args[0].lower() not in LOCK_TYPES:
        await message.reply_text("Usage: /lock media|sticker|gif|voice|poll|link")
        return
    await set_setting(message.chat.id, f"lock_{args[0].lower()}", "1")
    await message.reply_text("Locked.")

async def unlock(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args or args[0].lower() not in LOCK_TYPES: return
    await set_setting(message.chat.id, f"lock_{args[0].lower()}", "0")
    await message.reply_text("Unlocked.")

async def cleanservice(client: Client, message: Message) -> None: await generic_toggle(client, message, "clean_service", "cleanservice")
async def flood_toggle(client: Client, message: Message) -> None: await generic_toggle(client, message, "flood_enabled", "flood")
async def captcha_toggle(client: Client, message: Message) -> None: await generic_toggle(client, message, "captcha_enabled", "captcha")

async def approve(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO approved(chat_id,user_id) VALUES(?,?)", (message.chat.id, uid))
        await db.commit()
    await message.reply_text("Approved user.")

async def unapprove(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM approved WHERE chat_id=? AND user_id=?", (message.chat.id, uid))
        await db.commit()
    await message.reply_text("Unapproved user.")

async def approved(client: Client, message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM approved WHERE chat_id=?", (message.chat.id,)) as cur:
            rows = await cur.fetchall()
    await message.reply_text("Approved: " + (", ".join([str(r[0]) for r in rows]) if rows else "none"))

async def adminlist(client: Client, message: Message) -> None:
    if not await require_admin(client, message):
        return

    chat_id = message.chat.id
    chat_title = message.chat.title

    owner = []
    co_founders = []
    admins = []
    
    ANONYMOUS_ID = 1087968824
    total_count = 0

    async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        user = member.user
        
        # Bots ko skip
        if user.is_bot:
            continue

        # Anonymous admin check
        is_anonymous = (user.id == ANONYMOUS_ID or user.first_name == "Group")
        
        if is_anonymous:
            mention = "Hidden"
        else:
            # Normal user ke liye full name banao
            full_name = f"{user.first_name} {user.last_name or ''}".strip()
            
            # Mention link banao
            mention = f"<a href='tg://user?id={user.id}'>{escape(full_name)}</a>"
        
        total_count += 1
        title_str = "" if is_anonymous else (f" {member.custom_title}" if member.custom_title else "")
        
        # Owner check
        if member.status == enums.ChatMemberStatus.OWNER:
            owner.append(f"• {mention}{title_str}")
            continue

        # Co-Founder check (permissions based)
        p = member.privileges
        if p and p.can_change_info and p.can_promote_members:
            co_founders.append(f"• {mention}{title_str}")
        else:
            admins.append(f"• {mention}{title_str}")

    # Message build
    reply = f"<b>Admins in {escape(chat_title)}:</b>\n\n"

    if owner:
        reply += "<b>👑 OWNER</b>\n" + "\n".join(owner) + "\n\n"
    if co_founders:
        reply += "<b>⚡ CO-FOUNDERS</b>\n" + "\n".join(co_founders) + "\n\n"
    if admins:
        reply += "<b>📋 ADMINS</b>\n" + "\n".join(admins) + "\n\n"
    reply += f"<b>Total Admins:</b> {total_count}"

    await message.reply_text(reply, parse_mode=enums.ParseMode.HTML)

async def promote(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    await client.promote_chat_member(message.chat.id, uid, privileges=enums.ChatPrivileges(can_manage_chat=True, can_delete_messages=True, can_restrict_members=True, can_invite_users=True, can_pin_messages=True))
    await message.reply_text("Promoted.")

async def demote(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    uid = target_user(message)
    if not uid: return
    await client.promote_chat_member(message.chat.id, uid, privileges=enums.ChatPrivileges(can_manage_chat=False, can_delete_messages=False, can_restrict_members=False, can_invite_users=False, can_pin_messages=False))
    await message.reply_text("Demoted.")

async def setgtitle(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /setgtitle <title>")
        return
    await client.set_chat_title(message.chat.id, " ".join(args))
    await message.reply_text("Group title updated.")

async def setgdesc(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /setgdesc <text>")
        return
    await client.set_chat_description(message.chat.id, " ".join(args))
    await message.reply_text("Group description updated.")

async def setgpic(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    reply = message.reply_to_message
    if not reply or not reply.photo:
        await message.reply_text("Reply to a photo with /setgpic")
        return
    bio = await client.download_media(reply, in_memory=True)
    await client.set_chat_photo(message.chat.id, photo=bio)
    await message.reply_text("Group photo updated.")

async def admincache(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    admins = []
    async for a in client.get_chat_members(message.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        admins.append(str(a.user.id))
    cache = ",".join(admins)
    await set_setting(message.chat.id, "admin_cache", cache)
    await message.reply_text(f"Admin cache refreshed: {len(admins)} admins")

async def setflood(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args or not args[0].isdigit():
        await message.reply_text("Usage: /setflood <number>")
        return
    limit = max(2, int(args[0]))
    await set_setting(message.chat.id, "flood_limit", str(limit))
    await message.reply_text(f"Flood limit set to {limit}")

async def setfloodmode(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    allowed = {"mute", "ban", "kick", "delete"}
    args = get_args(message)
    if not args or args[0].lower() not in allowed:
        await message.reply_text("Usage: /setfloodmode <mute|ban|kick|delete>")
        return
    mode = args[0].lower()
    await set_setting(message.chat.id, "flood_mode", mode)
    await message.reply_text(f"Flood mode set: {mode}")

async def clearflood(client: Client, message: Message) -> None: await generic_toggle(client, message, "clear_flood_enabled", "clearflood")

async def id_cmd(client: Client, message: Message) -> None:

    tid = message.chat.id

    # reply case
    if message.reply_to_message:
        user = message.reply_to_message.from_user

    # username / userid
    elif len(message.command) > 1:
        try:
            user = await client.get_users(message.command[1])
        except:
            return await message.reply_text("User not found.")

    # default sender
    else:
        user = message.from_user

    if message.chat.type == enums.ChatType.PRIVATE:
        txt = f"""
Name: {user.first_name} {user.last_name or ""}
Username: @{user.username if user.username else "None"}
User ID: <code>{user.id}</code>
"""
    else:
        txt = f"""
Chat ID: <code>{tid}</code>
User ID: <code>{user.id}</code>
"""

    await message.reply_text(txt, parse_mode=enums.ParseMode.HTML)

async def info(client: Client, message: Message) -> None:

    # target user
    if message.reply_to_message:
        user = message.reply_to_message.from_user
    elif len(message.command) > 1:
        try:
            user = await client.get_users(message.command[1])
        except:
            return await message.reply_text("User not found.")
    else:
        user = message.from_user

    # status
    try:
        member = await client.get_chat_member(message.chat.id, user.id)
        status = member.status.name
    except:
        status = "User"

    # bio
    try:
        chat = await client.get_chat(user.id)
        bio = chat.bio if chat.bio else "No bio"
    except:
        bio = "No bio"

    # user link
    user_link = f"<a href='https://t.me/{user.username}'>Link</a>" if user.username else f"<a href='tg://user?id={user.id}'>Link</a>"

    if message.chat.type == enums.ChatType.PRIVATE:
        text = f"""
<b>User Information</b>

<b>Name:</b> {user.first_name} {user.last_name or ""}
<b>Username:</b> @{user.username if user.username else "None"}
<b>User ID:</b> <code>{user.id}</code>

<b>User:</b> {user_link}

<b>Bio:</b>
{bio} 
"""
    else:
        text = f"""
<b>User Information</b>

<b>Name:</b> {user.first_name} {user.last_name or ""}
<b>Username:</b> @{user.username if user.username else "None"}
<b>User ID:</b> <code>{user.id}</code>
<b>Status:</b> {status}

<b>User:</b> {user_link}

<b>Bio:</b>
{bio}
"""

    # profile photo
    photo = None
    async for p in client.get_chat_photos(user.id, limit=1):
        photo = p.file_id

    if photo:
        await message.reply_photo(photo, caption=text, parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)    

async def report(client: Client, message: Message) -> None:
    if not message.reply_to_message: return
    offender = message.reply_to_message.from_user
    await message.reply_text(f"🚨 Reported: @{offender.username or offender.id}")

async def formatting(client: Client, message: Message) -> None:
    await message.reply_text("Use HTML tags: <b>bold</b>, <i>italic</i>, <code>code</code>, <a href='https://t.me'>link</a>", parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)

async def purge(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    chat_id = message.chat.id

    if not message.reply_to_message:
        return await message.reply_text("Reply to a message to start purge.")

    start_id = message.reply_to_message.id
    end_id = message.id - 1   # command message ko include nahi karna

    deleted = 0

    for mid in range(start_id, end_id + 1):
        try:
            await client.delete_messages(chat_id, mid)
            deleted += 1   # sirf successful delete count
        except:
            continue

    await message.reply_text(f"Purged {deleted} messages.")

async def spurge(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return

    start = message.reply_to_message.id
    end = message.id

    for mid in range(start, end):
        try:
            await client.delete_messages(message.chat.id, mid)
        except:
            pass

async def purge_amount(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if len(message.command) < 2:
        return

    amount = int(message.command[1])
    chat_id = message.chat.id
    current = message.id

    deleted = 0

    for mid in range(current - amount, current):
        try:
            await client.delete_messages(chat_id, mid)
            deleted += 1
        except:
            pass

    await message.reply_text(f"Purged {deleted} messages.")

async def purgeuser(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to a user's message.")

    target = message.reply_to_message.from_user.id
    start = message.reply_to_message.id
    end = message.id

    deleted = 0

    for mid in range(start, end):
        try:
            msg = await client.get_messages(message.chat.id, mid)
            if msg.from_user and msg.from_user.id == target:
                await msg.delete()
                deleted += 1
        except:
            pass

    await message.reply_text(f"Deleted {deleted} messages from that user.")

async def purgebots(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to start purge.")

    start = message.reply_to_message.id
    end = message.id

    deleted = 0

    for mid in range(start, end):
        try:
            msg = await client.get_messages(message.chat.id, mid)
            if msg.from_user and msg.from_user.is_bot:
                await msg.delete()
                deleted += 1
        except:
            pass

    await message.reply_text(f"Deleted {deleted} bot messages.")

async def purgemedia(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to start purge.")

    start = message.reply_to_message.id
    end = message.id

    deleted = 0

    for mid in range(start, end):
        try:
            msg = await client.get_messages(message.chat.id, mid)
            if msg.photo or msg.video or msg.animation:
                await msg.delete()
                deleted += 1
        except:
            pass

    await message.reply_text(f"Deleted {deleted} media messages.")

async def purgelinks(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to start purge.")

    start = message.reply_to_message.id
    end = message.id

    deleted = 0

    for mid in range(start, end):
        try:
            msg = await client.get_messages(message.chat.id, mid)

            if msg.text and ("http" in msg.text or "t.me" in msg.text):
                await msg.delete()
                deleted += 1

        except:
            pass

    await message.reply_text(f"Deleted {deleted} link messages.")

async def fastpurge(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to start purge.")

    start = message.reply_to_message.id
    end = message.id

    ids = list(range(start, end))

    deleted = 0

    for i in range(0, len(ids), 100):

        chunk = ids[i:i+100]

        try:
            await client.delete_messages(message.chat.id, chunk)
            deleted += len(chunk)
        except:
            pass

    await message.reply_text(f"Purged {deleted} messages.")
                                
async def cancelpurge(client: Client, message: Message):

    if not await require_admin(client, message):
        return

    chat_id = message.chat.id

    if not purge_running.get(chat_id):
        return await message.reply_text("No purge running.")

    purge_running[chat_id] = False

    await message.reply_text("Purge cancelling...")

# --- BIO SYSTEM CORE COMMANDS & SCANNER ---
async def allow_bio_user(client: Client, message: Message):
    if not await require_admin(client, message): return
    target = await get_target_user(client, message)
    if not target: return await message.reply_text("User nahi mila.")
    
    await mongo_db.bio_allowlist.update_one({"user_id": target.id}, {"$set": {"user_id": target.id}}, upsert=True)
    await mongo_db.bio_warnings.delete_one({"chat_id": message.chat.id, "user_id": target.id})
    await message.reply_text(f"✅ User {target.mention} ko bio/link checks ke liye whitelist kar diya gaya hai.")

async def unallow_bio_user(client: Client, message: Message):
    if not await require_admin(client, message): return
    target = await get_target_user(client, message)
    if not target: return await message.reply_text("User nahi mila.")
    
    await mongo_db.bio_allowlist.delete_one({"user_id": target.id})
    await message.reply_text(f"❌ User {target.mention} ko whitelist se hata diya gaya hai.")

async def aplist_bio(client: Client, message: Message):
    if not await require_admin(client, message): return
    cursor = mongo_db.bio_allowlist.find({})
    users = [str(doc["user_id"]) async for doc in cursor]
    text = "✅ **Bio Allowed Users:**\n" + "\n".join([f"<code>{u}</code>" for u in users]) if users else "Koi allowed user nahi hai."
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def bioconfig_cmd(client: Client, message: Message):
    if not await require_admin(client, message): return
    chat_id = message.chat.id
    warn_limit, action, is_enabled = await get_bio_config(chat_id)
    
    status_text = "✅ ENABLED" if is_enabled else "❌ DISABLED"
    text = (
        f"⚙️ **Anti-Bio Configuration**\n\n"
        f"🛡 **Status:** {status_text}\n"
        f"⚠️ **Warn Limit:** {warn_limit}\n"
        f"🔨 **Action:** {action.upper()}"
    )
    
    toggle_btn = "🔴 Disable Anti-Bio" if is_enabled else "🟢 Enable Anti-Bio"
    mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
    ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_btn, callback_data="cfg_toggle")],
        [InlineKeyboardButton(f"⚠️ Change Warns ({warn_limit})", callback_data="cfg_warn")],
        [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
        [InlineKeyboardButton("🗑 Close Menu", callback_data="del_msg")]
    ])
    await message.reply_text(text, reply_markup=keyboard)
# MAIN SCANNER
# --- IS JAGAH PAR DHOONDEIN (bio_and_link_scanner ke andar) ---

async def bio_and_link_scanner(client: Client, message: Message):
    if not message.from_user or message.chat.type == enums.ChatType.PRIVATE: return
    
    chat_id = message.chat.id
    # 1. Check if Anti-Bio is ON or OFF
    warn_limit, action, is_enabled = await get_bio_config(chat_id)
    if not is_enabled:
        return # Agar OFF hai toh bot kuch nahi karega

    user_id = message.from_user.id
    # 2. Admin Check
    if await is_admin(client, message, user_id): return
    
    # 3. YAHAN PAR BADALNA HAI:
    is_allowed = False
    if mongo_db is not None:
        is_allowed = await mongo_db.bio_allowlist.find_one({"user_id": user_id})

    if is_allowed: 
        return
    
    # 4. Baaki code niche waise hi rahega...
    await increment_bio_stat("scanned")
    msg_text = message.text or message.caption or ""
    violation, reason = False, ""

    # Link in Message check
    if has_link(msg_text):
        violation, reason = True, "Link in Message"
    else:
        # Link in Bio check (with caching to avoid flood limits)
        now = time.time()
        cached_bio = bio_cache.get(user_id)
        
        # Cache expire in 10 minutes (600 secs)
        if not cached_bio or (now - cached_bio['time']) > 600:
            try:
                chat = await client.get_chat(user_id)
                bio_cache[user_id] = {'bio': chat.bio, 'time': now}
                user_bio = chat.bio
            except:
                user_bio = None
        else:
            user_bio = cached_bio['bio']

        if user_bio and has_link(user_bio):
            violation, reason = True, "Link in Bio"
            await increment_bio_stat("caught")

    if violation:
        warn_limit, action, is_enabled = await get_bio_config(chat_id)
        
        # Message delete karein
        with suppress(Exception): await message.delete()

        # Update warning count in DB
        warn_doc = await mongo_db.bio_warnings.find_one_and_update(
            {"chat_id": chat_id, "user_id": user_id},
            {"$inc": {"count": 1}},
            upsert=True, return_document=True
        )
        # --- YE WALA HISSA UPDATE KAREIN ---
        count = warn_doc["count"]
        
        # User details nikalne ke liye
        safe_name = escape(message.from_user.first_name)
        user_id = message.from_user.id
        mention = message.from_user.mention  # Isse naam par click karke profile khul jayegi

        if count >= warn_limit:
            if action == "mute":
                with suppress(Exception): 
                    await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
                text = (
                    f"🔇 <b>User Muted Indefinitely</b>\n\n"
                    f"👤 <b>User:</b> {mention}\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📝 <b>Reason:</b> {reason}"
                )
                btn = InlineKeyboardButton("🔊 Unmute", callback_data=f"bio_unmute_{user_id}")
            else:
                with suppress(Exception): 
                    await client.ban_chat_member(chat_id, user_id)
                text = (
                    f"🚫 <b>User Banned</b>\n\n"
                    f"👤 <b>User:</b> {mention}\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📝 <b>Reason:</b> {reason}"
                )
                btn = InlineKeyboardButton("🔓 Unban", callback_data=f"bio_unban_{user_id}")

            keyboard = InlineKeyboardMarkup([[btn], [InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]])
            await client.send_message(chat_id, text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
        
        else:
            # Warning Message
            text = (
                f"⚠️ <b>MESSAGE REMOVED (Link Detected)</b>\n\n"
                f"👤 <b>User:</b> {mention}\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"📝 <b>Reason:</b> {reason}\n"
                f"📊 <b>Warnings:</b> {count}/{warn_limit}\n\n"
                f"🛑 <i>Notice: Please remove any links from your Bio or messages.</i>\n\n"
                f"📌 REPEATED VIOLATIONS WILL LEAD TO MUTE/BAN."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Allow", callback_data=f"bio_allow_{user_id}"), InlineKeyboardButton("🛡 Unwarn", callback_data=f"bio_unwarn_{user_id}")],
                [InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]
            ])
            await client.send_message(chat_id, text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

async def set_vc_msg(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    await set_setting(message.chat.id, "vc_msg_text", " ".join(args) if args else "Voice chat started!")
    await message.reply_text("VC message set.")

async def set_vc_invite(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    await set_setting(message.chat.id, "vc_invite_text", " ".join(args) if args else "Join VC now!")
    await message.reply_text("VC invite message set.")

async def vcmsg_toggle(client: Client, message: Message) -> None: await generic_toggle(client, message, "vc_msg_enabled", "vcmsg")
async def vcinvite_toggle(client: Client, message: Message) -> None: await generic_toggle(client, message, "vc_invite_enabled", "vcinvite")


async def vc_events(client: Client, message: Message):

    chat_id = message.chat.id

    vc_msg_enabled = await get_setting(chat_id, "vc_msg_enabled", False)
    vc_invite_enabled = await get_setting(chat_id, "vc_invite_enabled", False)

    # VC START
    if message.video_chat_started and vc_msg_enabled:

        text = await get_setting(chat_id, "vc_msg_text", "🎙 Voice chat started!")

        await message.reply_text(text)


    # VC END
    elif message.video_chat_ended and vc_msg_enabled:

        await message.reply_text("🔴 Voice chat ended.")


    # VC INVITE
    elif message.video_chat_members_invited and vc_invite_enabled:

        text = await get_setting(chat_id, "vc_invite_text", "📢 You are invited to VC!")

        users = message.video_chat_members_invited.users

        user_ids = "_".join([str(u.id) for u in users])

        button = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎙 Join VC", callback_data=f"joinvc_{user_ids}")]]
        )

        await message.reply_text(text, reply_markup=button)

# ==========================================
# VOICE CHAT (VC) HANDLERS
# ==========================================

async def vc_started(client: Client, message: Message) -> None:
    """Jab group mein VC start ho"""
    await message.reply_text("🎙 **Voice Chat shuru ho chuki hai!** Aa jao sab.")

async def vc_ended(client: Client, message: Message) -> None:
    """Jab group mein VC end ho"""
    duration = message.video_chat_ended.duration
    # Duration seconds mein milti hai, aap ise format kar sakte hain
    await message.reply_text(f"🔇 **Voice Chat khatam ho gayi.**\n⏳ Duration: {duration} seconds.")

async def vc_invited(client: Client, message: Message):
    # Invited users ki list nikalna
    invited_users = message.video_chat_members_invited.users
    invited_names = ", ".join([u.first_name for u in invited_users])

    # Sender ka naam (None check ke sath)
    if message.from_user:
        sender_name = message.from_user.first_name
    elif message.sender_chat:
        sender_name = message.sender_chat.title
    else:
        sender_name = "Ek Admin"

    # Inline Button setup
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Join Voice Chat 📞", 
                    url=f"https://t.me/{message.chat.username}" if message.chat.username else f"https://t.me/c/{str(message.chat.id)[4:]}/1"
                )
            ]
        ]
    )

    # Final message with buttons
    await message.reply_text(
        f"📞 **VC Invite:** {sender_name} ne {invited_names} ko Voice Chat mein bulaya hai!",
        reply_markup=keyboard
    )

async def get_target_user(client: Client, message: Message):
    """
    Utility to get target user from reply, username, or ID.
    """
    if message.reply_to_message:
        return message.reply_to_message.from_user
    
    args = message.command[1:] if len(message.command) > 1 else None
    if not args:
        return None
    
    user_input = args[0]
    
    # Agar input ID hai (integer)
    if user_input.isdigit():
        user_input = int(user_input)
        
    try:
        user = await client.get_users(user_input)
        return user
    except Exception:
        return None

# --- Iske niche aapke Federation commands shuru honge ---

async def get_fed(chat_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT fed_name FROM fed_membership WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def fed_info(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Check group kis fed se juda hai
    membership = await mongo_db.fed_membership.find_one({"chat_id": chat_id})
    if not membership:
        return await message.reply_text("Ye group kisi federation se nahi juda hai.")
    
    fed_id = membership["fed_id"]
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    if not fed:
        return await message.reply_text("Federation data nahi mila.")

    # Data Calculations
    owner = await client.get_users(fed["owner_id"])
    admin_count = len(fed.get("admins", []))
    groups_count = await mongo_db.fed_membership.count_documents({"fed_id": fed_id})
    bans_count = await mongo_db.fban_list.count_documents({"fed_id": fed_id})
    subs_count = len(fed.get("subscribed_feds", []))
    created_at = fed.get("created_at", "N/A")

    text = (
        f"<b>📊 Federation Information</b>\n\n"
        f"<b>🏷 Name:</b> {fed['fed_name']}\n"
        f"<b>🆔 ID:</b> <code>{fed_id}</code>\n"
        f"<b>👑 Owner:</b> {owner.mention if owner else 'Unknown'}\n"
        f"<b>👥 Admins:</b> {admin_count}\n"
        f"<b>🏢 Groups Joined:</b> {groups_count}\n"
        f"<b>🚫 Total Bans:</b> {bans_count}\n"
        f"<b>🔗 Subscribed Feds:</b> {subs_count}\n"
        f"<b>📅 Created On:</b> {created_at}"
    )

    # Inline Button for Admins/Owner only
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("View Admins List 👥", callback_data=f"fed_admins:{fed_id}")]
    ])

    await message.reply_text(text, reply_markup=keyboard)

async def fed_admin_list_callback(client: Client, callback_query: CallbackQuery):
    fed_id = callback_query.data.split(":")[1]
    user_id = callback_query.from_user.id
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    # Permission Check
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await callback_query.answer("Ye button sirf Fed Admins ke liye hai!", show_alert=True)
    
    # List taiyar karein
    owner_info = await client.get_users(fed["owner_id"])
    res = f"<b>Fed Admins for {fed['fed_name']}</b>\n\n"
    res += f"👑 Owner: {owner_info.first_name}\n"
    
    for admin_id in fed.get("admins", []):
        try:
            u = await client.get_users(admin_id)
            res += f"• {u.first_name} (<code>{u.id}</code>)\n"
        except: continue
        
    await callback_query.edit_message_text(res, reply_markup=back_keyboard()) 

async def new_fed(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /newfed <federation_name>")
    
    fed_name = " ".join(args)
    fed_id = str(uuid.uuid4())[:12] # Unique Link

    # Mongo mein save karein
    await mongo_db.federations.insert_one({
        "fed_id": fed_id,
        "fed_name": fed_name,
        "owner_id": user_id,
        "admins": [] # Khali list admins ke liye
    })
    
    await message.reply_text(
        f"<b>Federation Created!</b>\n\n"
        f"<b>Name:</b> {fed_name}\n"
        f"<b>Fed ID:</b> <code>{fed_id}</code>\n\n"
        f"Group owner <code>/joinfed {fed_id}</code> use karke join kar sakta hai."
    )

async def join_fed(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /joinfed <target_fed_id>")
    
    target_fed_id = args[0]
    
    # Check kya user khud kisi fed ka owner hai
    my_fed = await mongo_db.federations.find_one({"owner_id": user_id})
    if not my_fed:
        return await message.reply_text("Pehle apni khud ki federation banayein (/newfed).")
    
    # Check target fed exist karti hai
    target_fed = await mongo_db.federations.find_one({"fed_id": target_fed_id})
    if not target_fed:
        return await message.reply_text("Galt Federation ID.")

    if my_fed["fed_id"] == target_fed_id:
        return await message.reply_text("Aap apni hi fed ko subscribe nahi kar sakte.")

    # Subscribe logic (Target fed ko apni list mein add karein)
    await mongo_db.federations.update_one(
        {"fed_id": my_fed["fed_id"]},
        {"$addToSet": {"subscribed_feds": target_fed_id}}
    )
    
    await message.reply_text(f"Safalta-पूर्वक! Aapki federation <b>{my_fed['fed_name']}</b> ab <b>{target_fed['fed_name']}</b> ko subscribe kar chuki hai.")

async def fban_user(client: Client, message: Message):
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    
    if not target:
        return await message.reply_text("<b>Error:</b> User nahi mila. UserID/Username dein ya reply karein.")

    # Group ki Fed ID check
    membership = await mongo_db.fed_membership.find_one({"chat_id": message.chat.id})
    if not membership:
        return await message.reply_text("Ye group kisi federation se nahi juda hai.")
    
    fed_id = membership["fed_id"]
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    # Permission Check
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await message.reply_text("Sirf Fed Admin/Owner hi fban kar sakte hain.")

    reason = " ".join(get_args(message)[1:]) if len(get_args(message)) > 1 else "No reason"
    
    # Mongo update
    await mongo_db.fban_list.update_one(
        {"fed_id": fed_id, "user_id": target.id},
        {"$set": {"reason": reason, "user_name": target.first_name}},
        upsert=True
    )

    # Subscriber Feds mein bhi sync karein
    subscribers = mongo_db.federations.find({"subscribed_feds": fed_id})
    async for sub_fed in subscribers:
        await mongo_db.fban_list.update_one(
            {"fed_id": sub_fed["fed_id"], "user_id": target.id},
            {"$set": {"reason": f"Synced: {reason}", "user_name": target.first_name}},
            upsert=True
        )

    # Action
    count = 0
    groups = mongo_db.fed_membership.find({"fed_id": fed_id})
    async for group in groups:
        try:
            await client.ban_chat_member(group["chat_id"], target.id)
            count += 1
        except: continue

    fed_name = fed['fed_name']
    admin_mention = message.from_user.mention

    await message.reply_text(
        f"✅ <b>Federation Ban</b>\n"
        f"👮 <b>Admin:</b> {admin_mention}\n"
        f"🏷 <b>Federation:</b> {fed_name}\n"
        f"👤 <b>User:</b> {target.first_name} ({target.id})\n" 
        f"📝 <b>Reason:</b> {reason}\n"
        f"🌐 <b>Groups:</b> {count}",
        parse_mode=enums.ParseMode.HTML
    )

async def unfban_user(client: Client, message: Message):
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    
    if not target:
        return await message.reply_text("<b>Error:</b> User nahi mila. UserID/Username dein ya reply karein.")

    # Group ki Fed ID check
    membership = await mongo_db.fed_membership.find_one({"chat_id": message.chat.id})
    if not membership:
        return await message.reply_text("Ye group kisi federation se nahi juda hai.")
    
    fed_id = membership["fed_id"]
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    # Permission Check
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await message.reply_text("Sirf Fed Admin/Owner hi unfban kar sakte hain.")

    # Current Fed se FBAN entry delete karein
    await mongo_db.fban_list.delete_one({"fed_id": fed_id, "user_id": target.id})

    # Subscriber Feds mein bhi Sync karein
    subscribers = mongo_db.federations.find({"subscribed_feds": fed_id})
    async for sub_fed in subscribers:
        await mongo_db.fban_list.delete_one({"fed_id": sub_fed["fed_id"], "user_id": target.id})

    # Action: Groups mein Unban karna
    count = 0
    groups = mongo_db.fed_membership.find({"fed_id": fed_id})
    async for group in groups:
        try:
            await client.unban_chat_member(group["chat_id"], target.id)
            count += 1
        except Exception:
            continue

    fed_name = fed['fed_name']
    admin_mention = message.from_user.mention

    await message.reply_text(
        f"✅ <b>Federation Un-ban</b>\n"
        f"👮 <b>FedAdmin:</b> {admin_mention}\n"
        f"🏷 <b>Federation:</b> {fed_name}\n"
        f"👤 <b>User:</b> {target.first_name} ({target.id})\n"
        f"🌐 <b>Groups Unbanned:</b> {count}\n"
        f"🔄 <b>Status:</b> Removed from this fed and all subscribers.",
        parse_mode=enums.ParseMode.HTML
    )    

async def federation_admin_manager(client: Client, message: Message):
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    
    if not target:
        return await message.reply_text("User nahi mila.")

    membership = await mongo_db.fed_membership.find_one({"chat_id": message.chat.id})
    if not membership:
        return await message.reply_text("Ye group fed mein nahi hai.")
    
    fed_id = membership["fed_id"]
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    if user_id != fed["owner_id"]:
        return await message.reply_text("Sirf Federation Owner hi ye kar sakta hai.")

    cmd = message.command[0].lower()
    if cmd == "fpromote":
        await mongo_db.federations.update_one({"fed_id": fed_id}, {"$addToSet": {"admins": target.id}})
        text = f"⭐️ {target.first_name} ab is Federation ka Admin hai!"
    else:
        await mongo_db.federations.update_one({"fed_id": fed_id}, {"$pull": {"admins": target.id}})
        text = f"🗑 {target.first_name} ko Fed Admin se hata diya gaya."

    await message.reply_text(text)    

async def del_fed(client: Client, message: Message):
    """Delete a federation (owner only)."""
    user_id = message.from_user.id
    args = get_args(message)
    
    if not args:
        return await message.reply_text("Usage: /delfed <fed_id>")
    
    fed_id = args[0]
    
    # Check if federation exists and user is owner
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")
    
    if fed["owner_id"] != user_id:
        return await message.reply_text("Only the federation owner can delete it.")
    
    # Confirm deletion (optional, but good to have)
    # We'll ask for confirmation via inline keyboard
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delfed:{fed_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_delfed")]
    ])
    await message.reply_text(
        f"Are you sure you want to delete federation **{fed['fed_name']}**?\n"
        "This will remove all associated groups, bans, and subscriptions.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def rename_fed(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if len(args) < 2:
        return await message.reply_text("Usage: /renamefed <fed_id> <new_name>")
    fed_id = args[0]
    new_name = " ".join(args[1:])
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")
    if fed["owner_id"] != user_id:
        return await message.reply_text("Only the federation owner can rename it.")
    
    await mongo_db.federations.update_one(
        {"fed_id": fed_id},
        {"$set": {"fed_name": new_name}}
    )
    await message.reply_text(f"Federation renamed to **{new_name}**.", parse_mode=enums.ParseMode.MARKDOWN)

async def transfer_fed(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if len(args) < 2:
        return await message.reply_text("Usage: /transferfed <fed_id> <user> (reply, username or ID)")
    fed_id = args[0]
    user_input = args[1]
    
    # Target user प्राप्त करें
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user
    else:
        try:
            target = await client.get_users(user_input)
        except:
            return await message.reply_text("Target user not found.")
    if not target:
        return await message.reply_text("Target user not found.")
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")
    if fed["owner_id"] != user_id:
        return await message.reply_text("Only the owner can transfer ownership.")
    
    await mongo_db.federations.update_one(
        {"fed_id": fed_id},
        {"$set": {"owner_id": target.id}}
    )
    await message.reply_text(f"Federation ownership transferred to {target.mention}.")

async def my_feds(client: Client, message: Message):
    user_id = message.from_user.id

    # Federations where user is Owner
    owner_feds = mongo_db.federations.find({"owner_id": user_id})
    owner_count = await mongo_db.federations.count_documents({"owner_id": user_id})

    # Federations where user is Admin
    admin_feds = mongo_db.federations.find({"admins": user_id})
    admin_count = await mongo_db.federations.count_documents({"admins": user_id})

    if owner_count == 0 and admin_count == 0:
        return await message.reply_text("❌ You are not the owner or admin of any federation.")

    text = f"<b>📋 Your Federations</b>\n\n"

    if owner_count > 0:
        text += f"<b>👑 Owner ({owner_count}):</b>\n"
        async for fed in owner_feds:
            groups = await mongo_db.fed_membership.count_documents({"fed_id": fed["fed_id"]})
            bans = await mongo_db.fban_list.count_documents({"fed_id": fed["fed_id"]})
            text += f"• <b>{fed['fed_name']}</b>\n"
            text += f"  🆔 <code>{fed['fed_id']}</code>\n"
            text += f"  🏢 Groups: {groups} | 🚫 Bans: {bans}\n\n"

    if admin_count > 0:
        text += f"<b>🛡️ Admin ({admin_count}):</b>\n"
        async for fed in admin_feds:
            groups = await mongo_db.fed_membership.count_documents({"fed_id": fed["fed_id"]})
            bans = await mongo_db.fban_list.count_documents({"fed_id": fed["fed_id"]})
            text += f"• <b>{fed['fed_name']}</b>\n"
            text += f"  🆔 <code>{fed['fed_id']}</code>\n"
            text += f"  🏢 Groups: {groups} | 🚫 Bans: {bans}\n\n"

    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def fban_list(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /fbanlist <fed_id>")
    fed_id = args[0]
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")
    if fed["owner_id"] != user_id:
        return await message.reply_text("Only the owner can view ban list.")
    
    bans = mongo_db.fban_list.find({"fed_id": fed_id})
    count = await mongo_db.fban_list.count_documents({"fed_id": fed_id})
    if count == 0:
        return await message.reply_text("No banned users in this federation.")
    
    text = f"<b>Banned users in {fed['fed_name']}:</b> ({count})\n\n"
    async for ban in bans:
        user_link = f"<a href='tg://user?id={ban['user_id']}'>{ban.get('user_name', 'Unknown')}</a>"
        text += f"• {user_link} (<code>{ban['user_id']}</code>)\n"
        text += f"  Reason: {ban.get('reason', 'No reason')}\n\n"
        if len(text) > 3500:  # सीमा से अधिक होने पर रोकें
            text += "... and more."
            break
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def leave_fed(client: Client, message: Message):
    user_id = message.from_user.id
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /leavefed <fed_id>")
    fed_id = args[0]
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")
    if fed["owner_id"] == user_id:
        return await message.reply_text("Owner cannot leave. Transfer or delete federation instead.")
    if user_id not in fed.get("admins", []):
        return await message.reply_text("You are not an admin of this federation.")
    
    await mongo_db.federations.update_one(
        {"fed_id": fed_id},
        {"$pull": {"admins": user_id}}
    )
    await message.reply_text("You have left the federation (removed from admins).")



async def captcha_verify(client: Client, callback_query: CallbackQuery) -> None:
    q = callback_query
    _, chat_id, uid = q.data.split(":")
    if q.from_user.id != int(uid):
        await q.answer("Ye button aapke liye nahi hai", show_alert=True)
        return
    await client.restrict_chat_member(int(chat_id), int(uid), ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
    await q.edit_message_text("Verified ✅")

async def on_vc_service(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    if await get_setting(chat_id, "vc_msg_enabled", "0") == "1":
        txt = await get_setting(chat_id, "vc_msg_text", "Voice chat started!")
        await message.reply_text(txt)
    if await get_setting(chat_id, "vc_invite_enabled", "0") == "1":
        txt = await get_setting(chat_id, "vc_invite_text", "Join VC now!")
        await message.reply_text(txt)

async def combined_filter_and_safety_handler(client: Client, message: Message):
    if not message or not message.from_user or message.from_user.is_bot: 
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (message.text or message.caption or "").lower()

    # 1. ADMIN & APPROVED CHECK (Admins par locks/filters kaam nahi karenge)
    if await is_admin(client, message, user_id) or await is_approved(chat_id, user_id): 
        return

    # 2. FLOOD CONTROL (Puraana logic)
    if await get_setting(chat_id, "flood_enabled", "1") == "1":
        key = (chat_id, user_id)
        now = time.time()
        dq = flood_tracker[key]
        dq.append(now)
        while dq and now - dq[0] > 10: # 10 second window
            dq.popleft()
        if len(dq) >= int(await get_setting(chat_id, "flood_limit", "5")):
            mode = (await get_setting(chat_id, "flood_mode", "mute")).lower()
            with suppress(Exception): await message.delete()
            if mode == "ban": await client.ban_chat_member(chat_id, user_id)
            elif mode == "mute": await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
            return

    # 3. CHAT LOCKS (Media, Link, Sticker, etc.)
    for lk in ["link", "media", "sticker", "gif", "voice", "poll"]:
        if await get_setting(chat_id, f"lock_{lk}", "0") == "1":
            if (lk == "link" and re.search(r'http[s]?://\S+', text)) or \
               (lk == "media" and any([message.photo, message.video, message.document])) or \
               (lk == "sticker" and message.sticker) or \
               (lk == "gif" and message.animation) or \
               (lk == "voice" and message.voice):
                with suppress(Exception): await message.delete()
                return

    # 4. MONGODB FILTERS TRIGGER (New Logic - Text, Media, Username, Fullname)
    check_list = [text]
    if message.from_user.username:
        check_list.append(f"@{message.from_user.username.lower()}")
    fullname = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip().lower()
    check_list.append(fullname)

    # MongoDB se filters check karein
    cursor = db["chat_filters"].find({"chat_id": chat_id})

    msg_words = set(re.findall(r"[a-zA-Z0-9]+", text.lower()))

    username_words = set(re.findall(r"[a-zA-Z0-9]+", (message.from_user.username or "").lower()))

    fullname = f"{message.from_user.first_name} {message.from_user.last_name or ''}".lower()
    name_words = set(re.findall(r"[a-zA-Z0-9]+", fullname))

    async for f in cursor:
        keyword = f['keyword'].strip().lower()

        if keyword in msg_words or keyword in username_words or keyword in name_words:

            c = f['content']
            m_type = c['type']
            f_id = c.get('file_id')
            cap = c.get('caption', "")

            if m_type == "text":
                await message.reply_text(c['data'])

            elif m_type == "photo":
                await message.reply_photo(f_id, caption=cap)

            elif m_type == "sticker":
                await message.reply_sticker(f_id)

            elif m_type == "video":
                await message.reply_video(f_id, caption=cap)

            elif m_type == "animation":
                await message.reply_animation(f_id)

            return
        
async def service_cleaner(client: Client, message: Message) -> None:
    if message.service and await get_setting(message.chat.id, "clean_service", "0") == "1":
        with suppress(Exception): await message.delete()

async def reload_bot(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    flood_tracker.clear()
    active_users.clear()
    lang = await get_lang(message.chat.id)
    await message.reply_text(tr(lang, "reloaded"))

async def language(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args or args[0].lower() not in {"en", "hi"}:
        await message.reply_text("Usage: /language en|hi")
        return
    await set_setting(message.chat.id, "language", args[0].lower())
    lang = await get_lang(message.chat.id)
    await message.reply_text(tr(lang, "lang_set"))

async def pin(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    if not message.reply_to_message:
        await message.reply_text("Reply to message for pin.")
        return
    await client.pin_chat_message(message.chat.id, message.reply_to_message.id)

async def unpin(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    await client.unpin_all_chat_messages(message.chat.id)

async def clean_cmd_toggle(client: Client, message: Message) -> None: await generic_toggle(client, message, "clean_cmd_enabled", "cleancommands")

async def clean_cmd_for(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if len(args) < 2 or args[1].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /cleanfor <command> on|off")
        return
    cmd = args[0].lower().lstrip("/")
    enabled = 1 if args[1].lower()=="on" else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO clean_cmd_rules(chat_id,command,enabled) VALUES(?,?,?)", (message.chat.id, cmd, enabled))
        await db.commit()
    await message.reply_text(f"Clean for /{cmd}: {'on' if enabled else 'off'}")

async def clean_cmd_handler(client: Client, message: Message) -> None:
    if not message or not message.text:
        return
    chat_id = message.chat.id
    if await get_chat_setting(chat_id, "clean_cmd_enabled", "0") != "1":
        return
    cmd = message.command[0].lower()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT enabled FROM clean_cmd_rules WHERE chat_id=? AND command=?", (chat_id, cmd)) as cur:
            row = await cur.fetchone()
    if row is not None and row[0] == 0:
        return
    with suppress(Exception):
        await message.delete()

async def disable_cmd(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /disable <command>")
        return
    cmd = args[0].lower().lstrip("/")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO disabled_commands(chat_id,command) VALUES(?,?)", (message.chat.id, cmd))
        await db.commit()
    await message.reply_text(f"Disabled /{cmd}")

async def enable_cmd(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /enable <command>")
        return
    cmd = args[0].lower().lstrip("/")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM disabled_commands WHERE chat_id=? AND command=?", (message.chat.id, cmd))
        await db.commit()
    await message.reply_text(f"Enabled /{cmd}")

async def disabled_cmds(client: Client, message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT command FROM disabled_commands WHERE chat_id=? ORDER BY command", (message.chat.id,)) as cur:
            rows = await cur.fetchall()
    await message.reply_text("Disabled commands: " + (", ".join(r[0] for r in rows) if rows else "none"))

async def disabled_cmd_guard(client: Client, message: Message) -> None:
    if not message or not message.text: return
    if await is_admin(client, message): return
    cmd = message.command[0].lower()
    if cmd in {"enable", "disable", "disabled"}: return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM disabled_commands WHERE chat_id=? AND command=?", (message.chat.id, cmd)) as cur:
            row = await cur.fetchone()
    if row:
        with suppress(Exception): await message.reply_text("This command is disabled in this chat.")
        message.stop_propagation()

async def antiraid(client: Client, message: Message) -> None: await generic_toggle(client, message, "antiraid_enabled", "antiraid")

async def connection(client: Client, message: Message) -> None:
    fed = await get_fed(message.chat.id)
    log_chan = await get_setting(message.chat.id, "log_channel", "not_set")
    txt = f"Chat: <code>{message.chat.id}</code>\nFed: <code>{fed or 'none'}</code>\nLog channel: <code>{log_chan}</code>"
    await message.reply_text(txt, parse_mode=enums.ParseMode.HTML)

async def export_data(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    chat_id = message.chat.id
    payload: dict[str, object] = {"chat_id": chat_id}
    async with aiosqlite.connect(DB_PATH) as db:
        tables = {"settings": "SELECT key,value FROM settings WHERE chat_id=?", "notes": "SELECT name,content FROM notes WHERE chat_id=?", "filters": "SELECT keyword,content FROM filters WHERE chat_id=?", "disabled_commands": "SELECT command FROM disabled_commands WHERE chat_id=?"}
        for key, query in tables.items():
            async with db.execute(query, (chat_id,)) as cur:
                payload[key] = await cur.fetchall()
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    bio = BytesIO(data)
    bio.name = f"export_{chat_id}.json"
    await message.reply_document(document=bio, file_name=bio.name)

async def import_data(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /importdata <json>")
        return
    try:
        data = json.loads(" ".join(args))
    except json.JSONDecodeError:
        await message.reply_text("Invalid JSON payload.")
        return
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        for k, v in data.get("settings", []): await db.execute("INSERT OR REPLACE INTO settings(chat_id,key,value) VALUES(?,?,?)", (chat_id, k, str(v)))
        for n, c in data.get("notes", []): await db.execute("INSERT OR REPLACE INTO notes(chat_id,name,content) VALUES(?,?,?)", (chat_id, n, c))
        for k, c in data.get("filters", []): await db.execute("INSERT OR REPLACE INTO filters(chat_id,keyword,content) VALUES(?,?,?)", (chat_id, k, c))
        for row in data.get("disabled_commands", []):
            cmd = row[0] if isinstance(row, (list, tuple)) else row
            await db.execute("INSERT OR IGNORE INTO disabled_commands(chat_id,command) VALUES(?,?)", (chat_id, str(cmd)))
        await db.commit()
    await message.reply_text("Import complete.")

async def set_log_channel(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /setlogchannel <chat_id>")
        return
    await set_setting(message.chat.id, "log_channel", args[0])
    await message.reply_text("Log channel set.")

async def log_channel(client: Client, message: Message) -> None:
    cid = await get_setting(message.chat.id, "log_channel", "not_set")
    await message.reply_text(f"Log channel: {cid}")

async def mics(client: Client, message: Message) -> None: await generic_toggle(client, message, "mics_enabled", "mics")

async def ddata(client: Client, message: Message) -> None:
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("Use /ddata in private chat only.")
        return
    uid = message.from_user.id
    payload: dict[str, object] = {"user_id": uid}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id,count FROM warns WHERE user_id=?", (uid,)) as cur: payload["warns"] = await cur.fetchall()
        async with db.execute("SELECT chat_id FROM approved WHERE user_id=?", (uid,)) as cur: payload["approved_in_chats"] = await cur.fetchall()
        async with db.execute("SELECT chat_id,msg_count,last_seen FROM user_activity WHERE user_id=?", (uid,)) as cur: payload["activity"] = await cur.fetchall()
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    bio = BytesIO(data)
    bio.name = f"my_data_{uid}.json"
    await message.reply_document(document=bio, file_name=bio.name)

async def deldata(client: Client, message: Message) -> None:
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("Use /deldata in private chat only.")
        return
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM warns WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM approved WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM user_activity WHERE user_id=?", (uid,))
        await db.commit()
    await message.reply_text("Your stored data was deleted from bot DB.")

# --- TAGGING LOGIC (utag & atag) ---

# 1. Update Canceltag: Isme hum active_tagging ko False set karenge
async def canceltag(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    chat_id = message.chat.id
    
    if chat_id in active_tagging and active_tagging[chat_id]:
        active_tagging[chat_id] = False # Yahan status change hoga
        await message.reply_text("<b>Tagging process ko rok diya gaya hai.</b>", parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("Abhi koi tagging nahi chal rahi hai.")

# 2. Update utag: Loop ke andar har baar status check hoga
async def utag(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    chat_id = message.chat.id
    
    if active_tagging.get(chat_id):
        await message.reply_text("Ek tagging pehle se chal rahi hai.")
        return

    tagged_list = []
    async for member in client.get_chat_members(chat_id, limit=100):
        if not member.user.is_bot and not member.user.is_deleted:
            tagged_list.append((member.user.id, member.user.first_name))

    if not tagged_list:
        await message.reply_text("Tag karne ke liye koi users nahi mile.")
        return

    active_tagging[chat_id] = True # Tagging shuru
    await message.reply_text("<b>Tagging Shuru!</b>", parse_mode=enums.ParseMode.HTML)
    
    args = get_args(message)
    prefix = escape(" ".join(args)) if args else "Attention"

    for i in range(0, len(tagged_list), 5):
        # --- ZAROORI: Har baar check karo ki admin ne cancel toh nahi kiya ---
        if active_tagging.get(chat_id) is False:
            break
            
        chunk = tagged_list[i:i + 5]
        mentions = [f"<a href='tg://user?id={u[0]}'>{escape(u[1])}</a>" for u in chunk]
        
        try:
            await client.send_message(
                chat_id,
                f"{prefix}\n" + " ".join(mentions),
                parse_mode=enums.ParseMode.HTML,
                reply_to_message_id=message.reply_to_message.id if message.reply_to_message else None
           )
            await asyncio.sleep(3.0) # Flood wait se bachne ke liye thoda gap
        except Exception:
            break

    # Final check
    if active_tagging.get(chat_id) is not False:
        await message.reply_text("<b>Tagging Completed!</b>", parse_mode=enums.ParseMode.HTML)
    
    active_tagging[chat_id] = False # Reset state

async def atag(client: Client, message: Message) -> None:
    if not await require_admin(client, message): return
    chat_id = message.chat.id
    
    admins = []
    async for a in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        # Exclude bots from admin tags
        if not a.user.is_bot:
            admins.append(a.user.mention())

    if not admins:
        await message.reply_text("No human admins found.")
        return

    active_tagging[chat_id] = True
    await message.reply_text("<b>Admin Tagging Started...</b>", parse_mode=enums.ParseMode.HTML)

    # Batch of 5 admins per message
    for i in range(0, len(admins), 5):
        if not active_tagging.get(chat_id):
            break
        chunk = admins[i:i + 5]
        await client.send_message(
            chat_id,
            "<b>Admin Alert:</b>\n" + " ".join(chunk),
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=message.reply_to_message.id if message.reply_to_message else None
        )
        await asyncio.sleep(2.0)

    active_tagging[chat_id] = False
    await message.reply_text("<b>Admin tagging completed!</b>")

async def track_activity(client: Client, message: Message) -> None:
    # sirf real users ko track karega, bots ko nahi
    if message.from_user and not message.from_user.is_bot and message.chat:
        dq = active_users[message.chat.id]
        uid = message.from_user.id
        if uid in dq:
            with suppress(ValueError): dq.remove(uid)
        dq.append(uid)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO user_activity(chat_id,user_id,msg_count,last_seen)
                VALUES(?,?,1,?)
                ON CONFLICT(chat_id,user_id)
                DO UPDATE SET msg_count=msg_count+1,last_seen=excluded.last_seen
            """, (message.chat.id, uid, int(time.time())))
            await db.commit()
    message.continue_propagation()

async def join_vc_callback(client: Client, query: CallbackQuery) -> None:
    # Allowed users check (wahi purana logic)
    data = query.data.replace("joinvc_", "")
    allowed_users = data.split("_")

    if str(query.from_user.id) not in allowed_users:
        return await query.answer("❌ Ye invite aapke liye nahi hai!", show_alert=True)

    chat = query.message.chat
    
    # 1. Agar group public hai (Username hai)
    if chat.username:
        # Ye URL direct VC panel open kar deta hai
        vc_link = f"https://t.me/{chat.username}?videochat"
        await query.answer("🎙 VC Panel khul raha hai...")
        # User ko link par bhej dega jo auto-join window open karega
        await client.send_message(query.from_user.id, f"Click here to join the VC: {vc_link}") # Optional backup
        return await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ JOIN NOW", url=vc_link)]])
        )

    # 2. Agar group private hai
    else:
        # Private groups mein direct link kaam nahi karta, isliye pop-up alert best hai
        await query.answer("🎙 Invited! Ab bas screen ke top par 'Join' button dabayein.", show_alert=True)


async def fed_callback_handler(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    if data.startswith("fed:"):
        # Yahan aap apna logic likhenge
        await callback_query.answer("Federation details loading...", show_alert=True)
        # For example: 
        # await callback_query.edit_message_text("Ye rahi aapki Fed info...")

async def confirm_delfed_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    user_id = q.from_user.id
    data = q.data
    
    if data.startswith("confirm_delfed:"):
        fed_id = data.split(":", 1)[1]
        
        # Verify again that user is owner
        fed = await mongo_db.federations.find_one({"fed_id": fed_id})
        if not fed:
            await q.edit_message_text("Federation already deleted or not found.")
            return
        
        if fed["owner_id"] != user_id:
            await q.answer("You are not the owner!", show_alert=True)
            return
        
        # Perform deletion
        # 1. Delete federation document
        await mongo_db.federations.delete_one({"fed_id": fed_id})
        
        # 2. Delete all group memberships
        await mongo_db.fed_membership.delete_many({"fed_id": fed_id})
        
        # 3. Delete all fbans
        await mongo_db.fban_list.delete_many({"fed_id": fed_id})
        
        # 4. Remove this fed_id from other federations' subscribed_feds arrays
        await mongo_db.federations.update_many(
            {"subscribed_feds": fed_id},
            {"$pull": {"subscribed_feds": fed_id}}
        )
        
        await q.edit_message_text(
            f"Federation **{fed['fed_name']}** has been deleted successfully.",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    
    elif data == "cancel_delfed":
        await q.edit_message_text("Deletion cancelled.")

async def fed_admin_list_callback(client: Client, callback_query: CallbackQuery):
    fed_id = callback_query.data.split(":")[1]
    user_id = callback_query.from_user.id
    
    fed = await mongo_db.federations.find_one({"fed_id": fed_id})
    
    # Permission Check
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await callback_query.answer("Ye button sirf Fed Admins ke liye hai!", show_alert=True)
    
    # List taiyar karein
    owner_info = await client.get_users(fed["owner_id"])
    res = f"<b>Fed Admins for {fed['fed_name']}</b>\n\n"
    res += f"👑 Owner: {owner_info.first_name}\n"
    
    for admin_id in fed.get("admins", []):
        try:
            u = await client.get_users(admin_id)
            res += f"• {u.first_name} (<code>{u.id}</code>)\n"
        except: continue
        
    # यहाँ से back_keyboard() हटा दिया
    await callback_query.edit_message_text(res, parse_mode=enums.ParseMode.HTML)        

# --- BIO SYSTEM CALLBACKS ---
async def bio_callbacks_handler(client: Client, query: CallbackQuery):
    data = query.data
    chat_id = query.message.chat.id
    
    if data == "del_msg":
        with suppress(Exception): 
            await query.message.delete()
        return

    # Check admin permission
    if not await is_admin(client, query.message, query.from_user.id):
        return await query.answer("❌ You are not an admin!", show_alert=True)

    if data.startswith("cfg_") or data.startswith("setwarn_"):
        # Pehle config fetch karein
        warn_limit, action, is_enabled = await get_bio_config(chat_id)

        # 1. Toggle ON/OFF Logic
        if data == "cfg_toggle":
            is_enabled = not is_enabled
            await set_bio_config(chat_id, "enabled", is_enabled)
            
            alert_text = "✅ Anti-Bio Enabled" if is_enabled else "❌ Anti-Bio Disabled"
            await query.answer(alert_text, show_alert=False)

            status_text = "✅ ENABLED" if is_enabled else "❌ DISABLED"
            toggle_btn_text = "🔴 Disable Anti-Bio" if is_enabled else "🟢 Enable Anti-Bio"
            mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
            ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"

            text = (
                f"⚙️ **Anti-Bio Configuration**\n\n"
                f"🛡 **Status:** {status_text}\n"
                f"⚠️ **Warn Limit:** {warn_limit}\n"
                f"🔨 **Action:** {action.upper()}"
            )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_btn_text, callback_data="cfg_toggle")],
                [InlineKeyboardButton(f"⚠️ Change Warns ({warn_limit})", callback_data="cfg_warn")],
                [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
                [InlineKeyboardButton("🗑 Close Menu", callback_data="del_msg")]
            ])

            try:
                await query.edit_message_text(text, reply_markup=keyboard)
            except Exception:
                pass
            return 

        # 2. Mute/Ban Logic
        elif data in ["cfg_mute", "cfg_ban"]:
            action = data.split("_")[1]
            await set_bio_config(chat_id, "action", action)
            await query.answer(f"✅ Action set to {action.upper()}", show_alert=False)

            status_text = "✅ ENABLED" if is_enabled else "❌ DISABLED"
            toggle_btn_text = "🔴 Disable Anti-Bio" if is_enabled else "🟢 Enable Anti-Bio"
            mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
            ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"

            text = (
                f"⚙️ **Anti-Bio Configuration**\n\n"
                f"🛡 **Status:** {status_text}\n"
                f"⚠️ **Warn Limit:** {warn_limit}\n"
                f"🔨 **Action:** {action.upper()}"
            )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle_btn_text, callback_data="cfg_toggle")],
                [InlineKeyboardButton(f"⚠️ Change Warns ({warn_limit})", callback_data="cfg_warn")],
                [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
                [InlineKeyboardButton("🗑 Close Menu", callback_data="del_msg")]
            ])

            try:
                await query.edit_message_text(text, reply_markup=keyboard)
            except Exception:
                pass
            return

        # 3. Warn Numbers Menu
        elif data == "cfg_warn":
            def get_btn(num):
                return InlineKeyboardButton(f"✅ {num}" if num == warn_limit else str(num), callback_data=f"setwarn_{num}")
            
            kb = [
                [get_btn(3), get_btn(4), get_btn(5), get_btn(6)],
                [get_btn(7), get_btn(8), get_btn(9), get_btn(10)],
                [InlineKeyboardButton("⬅️ Back", callback_data="cfg_main")]
            ]
            await query.edit_message_text("⚠️ **Select Warning Limit:**", reply_markup=InlineKeyboardMarkup(kb))

        # 4. Set Specific Warn Limit
        elif data.startswith("setwarn_"):
            limit = int(data.split("_")[1])
            await set_bio_config(chat_id, "warn_limit", limit)
            await query.answer(f"✅ Warning limit set to {limit}", show_alert=False)
            
            def get_btn(num):
                return InlineKeyboardButton(f"✅ {num}" if num == limit else str(num), callback_data=f"setwarn_{num}")
            
            kb = [
                [get_btn(3), get_btn(4), get_btn(5), get_btn(6)],
                [get_btn(7), get_btn(8), get_btn(9), get_btn(10)],
                [InlineKeyboardButton("⬅️ Back to Settings", callback_data="cfg_main")]
            ]
            
            try:
                await query.edit_message_text("⚠️ **Select Warning Limit:**", reply_markup=InlineKeyboardMarkup(kb))
            except Exception:
                pass
            return

        # 5. Back to Main Menu
        elif data == "cfg_main":
            status_text = "✅ ENABLED" if is_enabled else "❌ DISABLED"
            toggle_btn_text = "🔴 Disable Anti-Bio" if is_enabled else "🟢 Enable Anti-Bio"
            mute_btn = "✅ 🔇 Mute" if action == "mute" else "🔇 Mute"
            ban_btn = "✅ 🚫 Ban" if action == "ban" else "🚫 Ban"
            
            text = (
                f"⚙️ **Anti-Bio Configuration**\n\n"
                f"🛡 **Status:** {status_text}\n"
                f"⚠️ **Warn Limit:** {warn_limit}\n"
                f"🔨 **Action:** {action.upper()}"
            )
            
            kb = [
                [InlineKeyboardButton(toggle_btn_text, callback_data="cfg_toggle")],
                [InlineKeyboardButton(f"⚠️ Change Warns ({warn_limit})", callback_data="cfg_warn")],
                [InlineKeyboardButton(mute_btn, callback_data="cfg_mute"), InlineKeyboardButton(ban_btn, callback_data="cfg_ban")],
                [InlineKeyboardButton("🗑 Close Menu", callback_data="del_msg")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

    # User Actions (Allow, Unwarn, etc.)
    elif data.startswith("bio_"):
        parts = data.split("_")
        b_action, target_id = parts[1], int(parts[2])
        
        if b_action == "allow":
            if mongo_db is not None:
                await mongo_db.bio_allowlist.update_one({"user_id": target_id}, {"$set": {"user_id": target_id}}, upsert=True)
                await mongo_db.bio_warnings.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"✅ User `{target_id}` allowed.")
        
        elif b_action == "unwarn":
            if mongo_db is not None:
                await mongo_db.bio_warnings.update_one({"chat_id": chat_id, "user_id": target_id}, {"$inc": {"count": -1}})
            await query.answer("🛡 Warning cleared!")
            with suppress(Exception): await query.message.delete()
            
        elif b_action == "unmute":
            with suppress(Exception): 
                await client.restrict_chat_member(chat_id, target_id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True))
            if mongo_db is not None:
                await mongo_db.bio_warnings.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"🔊 User `{target_id}` Unmuted.")
            
        elif b_action == "unban":
            with suppress(Exception): 
                await client.unban_chat_member(chat_id, target_id)
            if mongo_db is not None:
                await mongo_db.bio_warnings.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"🔓 User `{target_id}` Unbanned.")

def main():
    token = os.getenv("BOT_TOKEN")
    app = Client("rose_clone", bot_token=token, api_id=int(os.getenv("API_ID")), api_hash=os.getenv("API_HASH"))

    # Command Handlers
    commands = {
        "start": start, "clone": clone, "help": help_cmd, "reload": reload_bot, "language": language,
        "owner": owner_cmd, "antiraid": antiraid, "connection": connection, "disable": disable_cmd,
        "enable": enable_cmd, "disabled": disabled_cmds, "exportdata": export_data, "importdata": import_data,
        "setlogchannel": set_log_channel, "logchannel": log_channel, "mics": mics, "ddata": ddata,
        "deldata": deldata, "setwelcome": setwelcome, "resetwelcome": resetwelcome, "welcome": welcome_toggle,
        "setgoodbye": setgoodbye, "resetgoodbye": resetgoodbye, "goodbye": goodbye_toggle, "setrules": setrules,
        "rules": rules, "setnote": setnote, "get": get_note, "delnote": delnote,"warn": warn, "dwarn": dwarn, "warnings": warnings,
        "resetwarns": resetwarns, "mute": mute, "dmute": dmute, "smute": smute, "tmute": tmute, "unmute": unmute,
        "ban": ban, "sban": sban, "tban": tban, "dban": dban, "unban": unban, "kick": kick, "dkick": dkick,
        "skick": skick, "kickme": kickme, "lock": lock, "unlock": unlock, "pin": pin, "unpin": unpin,
        "cleanservice": cleanservice, "cleancommands": clean_cmd_toggle, "cleanfor": clean_cmd_for,
        "captcha": captcha_toggle, "flood": flood_toggle, "setflood": setflood, "setfloodmode": setfloodmode,
        "clearflood": clearflood, "approve": approve, "unapprove": unapprove, "approved": approved,
        "adminlist": adminlist, "promote": promote, "demote": demote, "setgtitle": setgtitle, "setgpic": setgpic,
        "setgdesc": setgdesc, "admincache": admincache, "id": id_cmd, "admin": adminlist, "info": info,
        "purge": purge, "report": report, "formatting": formatting,"fpromote": federation_admin_manager,"fdemote": federation_admin_manager,
        "setvcmsg": set_vc_msg, "vcmsg": vcmsg_toggle, "setvcinvite": set_vc_invite, "vcinvite": vcinvite_toggle,
        "newfed": new_fed, "joinfed": join_fed, "fedinfo": fed_info, "delfed": del_fed, "fban": fban_user, "unfban": unfban_user,"utag": utag,
        "atag": atag, "canceltag": canceltag, "purge": purge, "spurge": spurge, "cancelpurge": cancelpurge,
        "purgeuser": purgeuser, "purgebots": purgebots, "purgemedia": purgemedia, "purgelinks": purgelinks,
        "renamefed": rename_fed,"transferfed": transfer_fed,"myfeds": my_feds,"fbanlist": fban_list,"leavefed": leave_fed,
        # existing list me ye commands bhi add kijiye
        "bioconfig": bioconfig_cmd, "allow": allow_bio_user, "unallow": unallow_bio_user, "aplist": aplist_bio,
    }

    # Register handlers
    for name, fn in commands.items():
        app.add_handler(MessageHandler(fn, filters.command(name)))

    app.add_handler(CallbackQueryHandler(start_buttons, filters.regex(r"^start:")))
    app.add_handler(CallbackQueryHandler(help_buttons, filters.regex(r"^help:")))
    app.add_handler(CallbackQueryHandler(captcha_verify, filters.regex(r"^captcha:")))
    app.add_handler(CallbackQueryHandler(join_vc_callback, filters.regex("^joinvc_")))
    # In handlers ke saath ise bhi add kar dein:
    app.add_handler(CallbackQueryHandler(fed_admin_list_callback, filters.regex(r"^fed_admins:")))
    app.add_handler(CallbackQueryHandler(fed_callback_handler, filters.regex("^fed:")))
    app.add_handler(CallbackQueryHandler(confirm_delfed_callback, filters.regex(r"^confirm_delfed:")))
    app.add_handler(CallbackQueryHandler(confirm_delfed_callback, filters.regex(r"^cancel_delfed")))
    # is jagah (line ~1255)
    app.add_handler(CallbackQueryHandler(bio_callbacks_handler, filters.regex(r"^(cfg_|setwarn_|del_msg|bio_)")))

    # --- GROUP 1: Welcome/Goodbye & VC Logic ---
    app.add_handler(MessageHandler(on_new_members, filters.new_chat_members), group=1)
    app.add_handler(MessageHandler(on_left_member, filters.left_chat_member))
    
    # VC Events ko sahi group mein rakha gaya hai
    app.add_handler(MessageHandler(vc_started, filters.video_chat_started), group=1)
    app.add_handler(MessageHandler(vc_ended, filters.video_chat_ended), group=1)
    app.add_handler(MessageHandler(vc_invited, filters.video_chat_members_invited), group=1)
    # auto_filter_and_locks ke theek neeche add kar dein
    app.add_handler(MessageHandler(bio_and_link_scanner, filters.group & ~filters.service & ~filters.command(list(commands.keys()))), group=4)

    # --- GROUP 2: Service Cleaner ---
    # Cleaner hamesha VC handlers ke baad chalna chahiye
    app.add_handler(MessageHandler(service_cleaner, filters.service), group=2)
    # fpromote aur fdemote dono isi function ko call karen ge
    app.add_handler(MessageHandler(federation_admin_manager, filters.command(["fpromote", "fdemote"]) & filters.group))

    # Commands
    app.add_handler(MessageHandler(filter_cmd_handler, filters.command("filter") & filters.group))
    app.add_handler(MessageHandler(stop_filter_handler, filters.command("stop") & filters.group))
    app.add_handler(MessageHandler(list_filters_handler, filters.command("filters") & filters.group))

    # Trigger (Isko auto_filter_and_locks ki jagah replace karein)
    # Group=4 rakhein taaki baaki admin filters isko disturb na karein
    app.add_handler(MessageHandler(auto_filter_handler, filters.group & ~filters.service & ~filters.command(["filter", "stop", "filters"])), group=5)
    # --- BAAKI GROUPS ---
    app.add_handler(MessageHandler(track_activity, filters.group & ~filters.service), group=3)
    app.add_handler(MessageHandler(combined_filter_and_safety_handler, filters.group & ~filters.service), group=6)
    app.add_handler(MessageHandler(clean_cmd_handler, filters.command(list(commands.keys()))), group=7)
    app.add_handler(MessageHandler(disabled_cmd_guard, filters.command(list(commands.keys()))), group=-1)


    async def start_bot():
        await app.start()
        await init_db()
        await init_mongo()
        print("Bot is now online and running!")
        await idle()  # This keeps the bot alive
        await app.stop()

    app.run(start_bot())

if __name__ == "__main__":
    main()
