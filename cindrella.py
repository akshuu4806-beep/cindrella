import asyncio
# Fix for Pyrogram on Python 3.14+ (no event loop in main thread)
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# -------------------------
import os
from pyrogram import Client, filters
from pyrogram.types import Message
import json
import re
import time
import uuid
import aiohttp
from pyrogram.types import ChatMemberUpdated
from pyrogram.handlers import ChatMemberUpdatedHandler
from pyrogram.enums import ChatMemberStatus
from aiohttp import web
from collections import defaultdict, deque
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from html import escape
from pyrogram.types import ChatPrivileges
from io import BytesIO
from pyrogram.types import ChatJoinRequest
from pyrogram.handlers import ChatJoinRequestHandler
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters, idle
from pyrogram.errors import BadRequest, FloodWait, MessageNotModified
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()

# --- Environment variables ---
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI", "")
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")

# --- MongoDB setup ---
mongo_client: AsyncIOMotorClient | None = None
mongo_db = None

if MONGO_URI:
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    mongo_db = mongo_client["RoseBot"]
else:
    raise ValueError("MONGO_URI not set!")

# Collections
chat_settings_col = mongo_db["chat_settings"]
notes_col = mongo_db["notes"]
warns_col = mongo_db["warns"]
approved_col = mongo_db["approved"]
clean_cmd_rules_col = mongo_db["clean_cmd_rules"]
disabled_commands_col = mongo_db["disabled_commands"]
user_activity_col = mongo_db["user_activity"]
filters_col = mongo_db["filters"]
fed_membership_col = mongo_db["fed_membership"]
federations_col = mongo_db["federations"]
fban_list_col = mongo_db["fban_list"]
bio_config_col = mongo_db["bio_config"]
bio_allowlist_col = mongo_db["bio_allowlist"]
bio_warns_col = mongo_db["bio_warns"]
system_info_col = mongo_db["system_info"]
permapin_messages_col = mongo_db["permapin_messages"]
autoantiraid_joins: dict[int, deque] = defaultdict(deque)
user_profile_col = mongo_db["user_profile"] 
# --- Owner & Sudo collections ---
sudo_users_col = mongo_db["sudo_users"]
active_chats_col = mongo_db["active_chats"]
# --- Link detection regex ---
import re

# Expanded list of TLDs – add any that are common in your group
TLDS = (
    'com|org|net|in|co|io|xyz|me|info|biz|edu|gov|mil|name|pro|club|online|site|space|tech|app'
)

BIO_LINK_PATTERNS = [
    r'https?://\S+',                     # http:// or https://
    r'www\.\S+',                         # www.
    r't\.me/\S+',                        # t.me/ links
    r'\S+\.(' + TLDS + r')(?:\b|/|\?)', # any word followed by a TLD and then word boundary, slash, or ?
    r'\S+\.\w{2,}/?\S*',                # generic domain with at least two letters in TLD (fallback)
]

def has_link(text: str) -> bool:
    """Return True if the text contains any link."""
    if not text:
        return False
    for pattern in BIO_LINK_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

EMOJI_PATTERN = re.compile(
    r'[\U0001F600-\U0001F64F]|'   # Emoticons
    r'[\U0001F300-\U0001F5FF]|'   # Symbols & pictographs
    r'[\U0001F680-\U0001F6FF]|'   # Transport & map symbols
    r'[\U0001F700-\U0001F77F]|'   # Alchemical symbols
    r'[\U0001F780-\U0001F7FF]|'   # Geometric shapes
    r'[\U0001F800-\U0001F8FF]|'   # Supplemental arrows
    r'[\U0001F900-\U0001F9FF]|'   # Supplemental symbols and pictographs
    r'[\U0001FA00-\U0001FA6F]|'   # Chess symbols
    r'[\U0001FA70-\U0001FAFF]|'   # Symbols and pictographs extended
    r'[\U00002702-\U000027B0]|'   # Dingbats
    r'[\U000024C2-\U0001F251]|'   # Enclosed characters
    r'[\u203C-\u3299]'             # Various other symbols
)

def has_emoji(text: str) -> bool:
    return bool(EMOJI_PATTERN.search(text))

# Bio cache to avoid hitting flood limits
bio_cache = {}
# --- Anonymous admin pending actions ---
pending_admin_actions = {}
PENDING_ACTION_TIMEOUT = 300  # 5 minutes
ANONYMOUS_ADMIN_ID = 1087968824
# --- Global tracking structures ---
pending_nightmode = {}
pending_antiraid_actions = {}
pending_floodmode = {}  # key: unique_id, value: {chat_id, user_id, action, duration_str, message_id}
active_tagging = {}
purge_running = {}
# --- Broadcast tracking ---
pending_broadcast = {}  # key: user_id, value: {text, media, pin, mode}
FLOOD_WINDOW_SEC = 7
FLOOD_LIMIT = 6
FLOOD_WINDOW = 10          # seconds
flood_tracker: dict[tuple[int, int], deque] = defaultdict(deque)
LOCK_TYPES = {"media", "sticker", "gif", "voice", "poll", "link", "emoji", "text"}
active_users: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=300))
flood_timer_tracker: dict[tuple[int, int], list] = defaultdict(list)
pending_warnmode = {}  # key: session_id, value: {chat_id, user_id, action, duration_min_str, message_id}

# ========== LOG CHANNEL CATEGORIES ==========
DEFAULT_LOG_CATEGORIES = {
    "settings": True,   # bot settings toggles, welcome, rules, etc.
    "admin": True,      # bans, mutes, warns, promotes, demotes
    "user": True,       # join, leave, kickme
    "automated": True,  # flood, antiraid, bio, captcha
    "reports": True,    # /report and @admins
    "other": True       # notes, filters, pins, etc.
}

async def send_log(client: Client, chat_id: int, category: str, action: str, user_mention: str, user_id: int, admin_mention: str = None, reason: str = None, extra: str = None):
    log_chan = await get_chat_setting(chat_id, "log_channel", "not_set")
    if log_chan == "not_set":
        return

    categories_str = await get_chat_setting(chat_id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
    categories = json.loads(categories_str) if isinstance(categories_str, str) else categories_str
    if not categories.get(category, True):
        return

    # Fetch group title (with fallback)
    try:
        chat = await client.get_chat(chat_id)
        group_name = chat.title or "Unknown Group"
    except Exception:
        group_name = "Unknown Group"

    # Convert mentions to plain names
    plain_user = extract_plain_name(user_mention, user_id)
    plain_admin = extract_plain_name(admin_mention, user_id) if admin_mention else None

    text = f"📋 **{category.upper()} LOG** | Group: **{group_name}** (ID: `{chat_id}`)\n\n"
    text += f"**Action:** {action}\n"
    text += f"**User:** {plain_user} (ID: `{user_id}`)\n"
    if plain_admin:
        text += f"**Admin:** {plain_admin}\n"
    if reason:
        text += f"**Reason:** {reason}\n"
    if extra:
        text += f"**Extra:** {extra}\n"

    try:
        await client.send_message(int(log_chan), text, parse_mode=enums.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Failed to send log to {log_chan}: {e}")

# --- Language support (English only now, but structure kept for future) ---
LANG = {
    "en": {
        "admin_only": "You need to be an admin to do this..",
        "reloaded": "Reloaded runtime caches.",
        "lang_set": "Language updated.",
        "tag_none": "No active users cached yet.",
    }
}

HELP_SECTIONS: dict[str, tuple[str, str]] = {
    "home": (
        "📚 Help Menu",
        (
            "Welcome to the help menu!\n\n"
            "Select a category below to learn about the available commands.\n\n"
            "If you're new, start with the **ALL** section to see all commands at once.\n"
        
        )
    ),
    "admin": (
        "👮 ADMIN",
        (
            "Make it easy to promote and demote users with the admin module!\n\n"
            "Admin commands:\n"
            "- `/promote <reply/username/mention/userid>`: Promote a user.\n"
            "- `/demote <reply/username/mention/userid>`: Demote a user.\n"
            "- `/adminlist`: List the admins in the current chat.\n"
            "- `/admincache`: Update the admin cache, to take into account new admins/admin permissions.\n"
            "- `/anonadmin`: Open a menu to allow or disallow anonymous admins to use all commands without checking their permissions. (Not recommended.)\n"
            "- `/setgtitle`, `/setgpic`, `/setgdesc`: Change group title, photo, or description.\n\n"
            "Sometimes, you promote or demote an admin manually, and the bot doesn't realise it immediately. "
            "This is because, to avoid spamming Telegram servers, admin status is cached locally. "
            "That means you may have to wait a few minutes for admin rights to update. "
            "If you want to update them immediately, you can use the `/admincache` command; that'll force the bot to check who the admins are again."
        )
    ),
    "antiflood": (
        "🌊 ANTIFLOOD",
        (
            "Keep your chat clean from spammers with antiflood!\n\n"
            "Commands:\n"
            "- `/floodcontrol`: Show current flood control settings and toggle on/off.\n"
            "- `/setfloodmode <mute|ban|kick|tban|tmute> [duration]`: Set the action for flood offenders.\n"
            "- `/setfloodtimer <count> <seconds>`: Enable timer‑based flood (e.g., 5 messages in 10 seconds). Use `off` to disable.\n"
            "- `/clearflood`: Toggle automatic deletion of flood messages.\n"
            "- `/antiraid`: Open a menu to enable/configure raid protection.\n"
            "- `/autoantiraid`: Set up automatic raid detection based on join speed.\n\n"
            "Flood control helps prevent message spam. You can choose between consecutive messages or a timed window."
        )
    ),
    "approval": (
        "✅ APPROVAL",
        (
            "Manage users who are exempt from certain restrictions.\n\n"
            "Commands:\n"
            "- `/approve <reply/username/mention/userid>`: Add a user to the approved list.\n"
            "- `/unapprove <reply/username/mention/userid>`: Remove a user from the approved list.\n"
            "- `/unapproveall`: Remove all approved users (owner only).\n"
            "- `/approved`: List all approved users.\n"
            "- `/approval`: Check approval status of a user.\n\n"
            "Approved users bypass locks, flood control, and night mode restrictions."
        )
    ),
    "bans": (
        "🔨 BANS",
        (
            "Powerful tools to remove unwanted members.\n\n"
            "Commands:\n"
            "- `/ban <reply/username/mention/userid>`: Ban a user.\n"
            "- `/dban`: Delete the replied message and ban the user.\n"
            "- `/sban`: Silently ban a user (no notification).\n"
            "- `/tban <duration>`: Temporarily ban a user (e.g., `/tban 2h`).\n"
            "- `/unban <reply/username/mention/userid>`: Unban a user.\n"
            "- `/kick`: Kick a user (they can rejoin).\n"
            "- `/dkick`: Delete the replied message and kick the user.\n"
            "- `/skick`: Silently kick a user.\n"
            "- `/kickme`: Leave the group yourself (the command sender)."
        )
    ),
    "blocklist": (
        "🚫 BLOCKLIST",
        (
            "Automatically delete messages containing blacklisted words.\n\n"
            "Commands:\n"
            "- `/blocklist`: List all blocked words.\n"
            "- `/addblock <word>`: Add a word to the blocklist.\n"
            "- `/unblock <word>`: Remove a word from the blocklist.\n"
            "- `/blocklistmode <action>`: Set action (delete, warn, mute, etc.) when a blocked word is used.\n\n"
            "Note: This module is currently under development; some commands may not be fully implemented."
        )
    ),
    "captcha": (
        "🧩 CAPTCHA",
        (
            "Verify new users with a simple CAPTCHA.\n\n"
            "Commands:\n"
            "- `/captcha on|off`: Enable or disable the CAPTCHA system.\n\n"
            "When enabled, new members will receive a CAPTCHA button and must solve it to gain speaking rights."
        )
    ),
    "cleancmd": (
        "🧹 CLEAN COMMAND",
        (
            "Automatically delete command messages to keep chats tidy.\n\n"
            "Commands:\n"
            "- `/cleancommands on|off`: Toggle automatic deletion of all command messages.\n"
            "- `/cleanfor <command> on|off`: Enable/disable deletion for a specific command (e.g., `/cleanfor ban on`)."
        )
    ),
    "disabling": (
        "⛔ DISABLING",
        (
            "Disable commands that you don't want regular users to use.\n\n"
            "Commands:\n"
            "- `/disable <command>`: Disable a command in this group.\n"
            "- `/enable <command>`: Enable a previously disabled command.\n"
            "- `/disabled`: List all disabled commands.\n\n"
            "Disabled commands will still work for admins."
        )
    ),
    "cleanservice": (
        "🧼 CLEAN SERVICE",
        (
            "Remove service messages (e.g., \"User joined\") automatically.\n\n"
            "Commands:\n"
            "- `/cleanservice on|off`: Toggle cleaning of service messages."
        )
    ),
    "federation": (
        "🌐 FEDERATION",
        (
            "Connect multiple groups to share bans and settings.\n\n"
            "Commands:\n"
            "- `/newfed <name>`: Create a new federation (PM only).\n"
            "- `/joinfed <fed_id>`: Connect this group to a federation (group owner).\n"
            "- `/leavefed`: Disconnect this group from its federation.\n"
            "- `/fedinfo [fed_id]`: Show federation details.\n"
            "- `/fban <user>`: Ban a user from the whole federation.\n"
            "- `/unfban <user>`: Remove a federation ban.\n"
            "- `/fedstat [user] [fed_id]`: Show federation ban info.\n"
            "- `/subfed <fed_id>`: Subscribe your federation to another (fed owner).\n"
            "- `/myfeds`: List federations you own or admin.\n"
            "- `/delfed`: Delete your federation (PM only).\n"
            "- `/renamefed <new_name>`: Rename your federation.\n"
            "- `/transferfed <user>`: Transfer federation ownership.\n"
            "- `/fpromote <user>`: Add a federation admin.\n"
            "- `/fdemote <user>`: Remove a federation admin.\n"
            "- `/quietfed`: Toggle quiet mode – silently remove fed-banned users.\n\n"
            "Federations allow you to enforce bans across multiple groups at once."
        )
    ),
    "connection": (
        "🔌 CONNECTION",
        (
            "See which federation and log channel this chat is connected to.\n\n"
            "Commands:\n"
            "- `/connection`: Display the current chat's federation and log channel (if any)."
        )
    ),
    "filters": (
        "🔎 FILTERS",
        (
            "Create automatic replies to keywords.\n\n"
            "Commands:\n"
            "- `/filter <keyword> <reply text>` or reply to a message: Save a filter.\n"
            "- `/stop <keyword>`: Delete a filter.\n"
            "- `/filters`: List all filters in this chat.\n\n"
            "Filters can be text, stickers, photos, or other media. They work by whole‑word matching in messages, usernames, and full names."
        )
    ),
    "formatting": (
        "✍️ FORMATTING",
        (
            "Learn how to use HTML formatting in your messages.\n\n"
            "Supported tags:\n"
            "- `<b>bold</b>`\n"
            "- `<i>italic</i>`\n"
            "- `<code>code</code>`\n"
            "- `<a href='url'>link</a>`\n\n"
            "Use these in filter replies, welcome messages, notes, and rules."
        )
    ),
    "greetings": (
        "👋 GREETINGS",
        (
            "Set custom welcome and goodbye messages.\n\n"
            "Commands:\n"
            "- `/setwelcome <text>` or reply to a message: Set welcome message.\n"
            "- `/resetwelcome`: Reset welcome to default.\n"
            "- `/welcome on|off`: Toggle welcome messages.\n"
            "- `/setgoodbye <text>` or reply: Set goodbye message.\n"
            "- `/resetgoodbye`: Reset goodbye to default.\n"
            "- `/goodbye on|off`: Toggle goodbye messages.\n\n"
            "Use placeholders: `{firstname}`, `{fullname}`, `{username}`, `{mention}`, `{id}`, `{date}`, `{time}`."
        )
    ),
    "language": (
        "🈯 LANGUAGE",
        (
            "Change the bot's language (currently only English).\n\n"
            "Commands:\n"
            "- `/language en` – English only.\n\n"
            "More languages may be added in the future."
        )
    ),
    "locks": (
        "🔐 LOCKS",
        (
            "Prevent specific types of messages from being sent.\n\n"
            "Commands:\n"
            "- `/lock <type>`: Lock a message type (media, sticker, gif, voice, poll, link, emoji, text, all).\n"
            "- `/unlock <type>`: Unlock a message type.\n"
            "- `lock all` and `unlock all` to lock/unlock everything.\n\n"
            "Approved users and admins bypass locks."
        )
    ),
    "notes": (
        "🗒 NOTES",
        (
            "Store and retrieve notes for later use.\n\n"
            "Commands:\n"
            "- `/setnote <name> <content>`: Create or edit a note.\n"
            "- `/get <name>`: Retrieve a note.\n"
            "- `/delnote <name>`: Delete a note.\n\n"
            "Notes can contain HTML formatting."
        )
    ),
    "pin": (
        "📌 PIN",
        (
            "Pin important messages with ease.\n\n"
            "Commands:\n"
            "- `/pin <reply>`: Pin the replied message (notify members).\n"
            "- `/spin <reply>`: Pin silently (no notification).\n"
            "- `/unpin <reply>`: Unpin the replied message.\n"
            "- `/unpinall`: Unpin all pinned messages (confirmation required).\n"
            "- `/pinned`: Show the currently pinned message.\n"
            "- `/permapin <text>`: Send and pin a permanent message (replaces channel pins automatically)."
        )
    ),
    "logchannels": (
        "📡 LOGCHANNELS",
        (
            "Log important actions to a separate channel.\n\n"
            "Commands:\n"
            "- `/setlog`: Set the log channel for this group (forward from your channel).\n"
            "- `/unsetlog`: Unset the log channel.\n"
            "- `/logchannel`: Show the current log channel.\n"
            "- `/logcategories`: List all log categories.\n"
            "- `/log <category>`: Enable logging for a category.\n"
            "- `/nolog <category>`: Disable logging for a category.\n\n"
            "Categories: settings, admin, user, automated, reports, other."
        )
    ),
    "privacy": (
        "🔏 PRIVACY",
        (
            "Your privacy is important.\n\n"
            "The bot stores minimal data necessary for its functions (user IDs, chat settings, warns, etc.). "
            "No private messages are stored unless explicitly sent as commands. Data is not shared with third parties. "
            "For full details, contact the bot owner in PM using `/owner`."
        )
    ),
    "privacydata": (
        "📦 PRIVACY DATA",
        (
            "Manage your personal data.\n\n"
            "Commands:\n"
            "- `/ddata`: Download your data stored by the bot (private chat only).\n"
            "- `/deldata`: Delete your data from the bot's database (private chat only)."
        )
    ),
    "purges": (
        "🗑 PURGES",
        (
            "Quickly delete multiple messages.\n\n"
            "Commands:\n"
            "- `/purge <reply>`: Delete all messages from the replied message to this command.\n"
            "- `/spurge <reply>`: Delete all messages and delete the command silently.\n"
            "- `/purge <amount>`: Delete the last `<amount>` messages.\n"
            "- `/purgeuser <reply>`: Delete all messages from that user between the replied message and this command.\n"
            "- `/purgebots <reply>`: Delete all bot messages between two points.\n"
            "- `/purgemedia <reply>`: Delete all media messages.\n"
            "- `/purgelinks <reply>`: Delete all messages containing links.\n"
            "- `/fastpurge <reply>`: Use batch deletion for faster purging.\n"
            "- `/cancelpurge`: Stop an ongoing purge (if any)."
        )
    ),
    "reports": (
        "🚨 REPORTS",
        (
            "Report messages to admins easily.\n\n"
            "Commands:\n"
            "- `/report` or `@admins` in reply to a message: Notify admins.\n\n"
            "The report is sent to the group with a mention of all admins."
        )
    ),
    "rules": (
        "📜 RULES",
        (
            "Set and display group rules.\n\n"
            "Commands:\n"
            "- `/setrules <text>` or reply to a message: Set the rules.\n"
            "- `/rules`: Get a link to the rules (in group) or view them (in PM)."
        )
    ),
    "topic": (
        "🧵 TOPIC",
        (
            "Topic management (placeholder – not yet implemented).\n\n"
            "Future commands for managing topics in supergroups will appear here."
        )
    ),
    "warning": (
        "⚠️ WARNING",
        (
            "Warn users who break the rules.\n\n"
            "Commands:\n"
            "- `/warn <user> [reason]`: Warn a user.\n"
            "- `/dwarn`: Warn and delete the replied message.\n"
            "- `/swarn`: Silently warn (no public message).\n"
            "- `/warns <user>`: Show warnings for a user.\n"
            "- `/resetwarns <user>`: Reset all warnings for a user.\n"
            "- `/unwarn <user>`: Remove the latest warning.\n"
            "- `/resetallwarns`: Reset all warns in the group (owner only).\n"
            "- `/warnmode`: Configure the action when warn limit is reached (ban, mute, kick, tban, tmute).\n"
            "- `/warntime <duration>`: Set warn expiry time.\n"
            "- `/warninfo <user>`: Show detailed warn info.\n\n"
            "You can also use `/warnings` to view current warn settings."
        )
    ),
    "silent": (
        "🤫 SILENT POWER",
        (
            "Silent moderation actions (no public messages).\n\n"
            "Commands:\n"
            "- `/smute`: Silently mute a user.\n"
            "- `/sban`: Silently ban a user.\n"
            "- `/skick`: Silently kick a user.\n"
            "- `/dmute`: Delete the replied message and mute the user.\n"
            "- `/dwarn`: Delete the replied message and warn the user.\n\n"
            "These commands are useful when you don't want to draw attention to your actions."
        )
    ),
    "importexport": (
        "📤 IMPORT/EXPORT",
        (
            "Back up and restore your data.\n\n"
            "Commands:\n"
            "- `/exportdata`: Download all data for this chat (filters, notes, etc.) as JSON.\n"
            "- `/importdata <json>`: Import data from a JSON backup.\n\n"
            "Note: This feature may be limited or require owner permissions."
        )
    ),
    "mics": (
        "🎙 MICS",
        (
            "Voice chat management (placeholder).\n\n"
            "Commands:\n"
            "- `/mics on|off`: Enable/disable voice chat notifications.\n\n"
            "More voice chat commands may be added in the future."
        )
    ),
    "extra": (
        "✨ EXTRA",
        (
            "Useful utilities.\n\n"
            "Commands:\n"
            "- `/id [user]`: Get chat or user ID.\n"
            "- `/info [user]`: Get detailed information about a user.\n"
            "- `/reload`: Reload runtime caches (admin only).\n"
            "- `/utag <message>`: Mention all members (slow).\n"
            "- `/atag <message>`: Mention all admins.\n"
            "- `/cancel`: Stop an ongoing tag.\n"
            "- `/admins`: List all admins (non‑admins can use this to report).\n"
            "- `/del <reply>`: Delete the replied message (admin only)."
        )
    ),
    "bio": (
        "🧬 BIO CHECK",
        (
            "Automatically detect and warn users with links in their bio.\n\n"
            "Commands:\n"
            "- `/bioconfig`: Configure the bio filter (warn limit, action).\n"
            "- `/allow <user>`: Whitelist a user from bio checks.\n"
            "- `/unallow <user>`: Remove from whitelist.\n"
            "- `/aplist`: List whitelisted users.\n\n"
            "When enabled, the bot will delete messages from users who have links in their bio or send links, and give warnings until they are muted/banned."
        )
    ),
    "antiabuse": (
        "🛡 ANTIABUSE",
        (
            "Protect your group from abuse.\n\n"
            "Features:\n"
            "- Antiflood – stops message spammers.\n"
            "- Antiraid – blocks mass joining.\n"
            "- Antichannel Pin – restores manually pinned messages when a channel pins.\n"
            "- Clean Linked Channel – deletes messages from linked channels.\n\n"
            "All these features have their own commands (see the respective sections)."
        )
    ),
    "all": (
        "📖 ALL",
        (
            "Complete list of commands\n\n"
            "Use the categories above to see detailed information.\n\n"
            "Quick reference:\n"
            "`/start` – Start the bot\n"
            "`/help` – This menu\n"
            "`/owner` – Contact the bot owner\n"
            "`/adminlist`, `/promote`, `/demote` – Admin management\n"
            "`/floodcontrol`, `/antiraid` – Flood/raid prevention\n"
            "`/approve`, `/unapprove` – Approval system\n"
            "`/ban`, `/unban`, `/kick`, `/mute`, `/unmute` – Ban/kick/mute\n"
            "`/warn`, `/resetwarns`, `/warnmode` – Warning system\n"
            "`/filter`, `/stop`, `/filters` – Filters\n"
            "`/setnote`, `/get`, `/delnote` – Notes\n"
            "`/setwelcome`, `/setgoodbye` – Greetings\n"
            "`/lock`, `/unlock` – Locks\n"
            "`/pin`, `/unpin`, `/permapin` – Pinning\n"
            "`/purge`, `/purgeuser` – Purges\n"
            "`/report`, `/admins` – Reporting\n"
            "`/setrules`, `/rules` – Rules\n"
            "`/captcha` – CAPTCHA verification\n"
            "`/fban`, `/joinfed`, `/newfed` – Federation bans\n"
            "For a complete list, explore the categories above."
        )
    ),
}

# --- Helper functions for keyboards ---
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

# --- MongoDB e functions ---
def extract_plain_name(mention: str, user_id: int) -> str:
    """Extract plain name from HTML mention like '<a href="tg://user?id=123">John</a>'."""
    if not mention:
        return f"User {user_id}"
    # Try to find content between > and <
    match = re.search(r'>([^<]+)<', mention)
    if match:
        return match.group(1)
    # If no HTML tag, return as is
    return mention
    
# --- Helper for owner/sudo permissions ---
async def is_owner_or_sudo(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    doc = await sudo_users_col.find_one({"user_id": user_id})
    return doc is not None

async def get_chat_link(client: Client, chat) -> str:
    """Generate a clickable link for a chat. Returns None if no link is available."""
    if isinstance(chat, int):
        chat = await client.get_chat(chat)
    if chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        # Public group with username
        if chat.username:
            return f"https://t.me/{chat.username}"
        # Private group – try to get an invite link
        try:
            # Check if bot has invite permission
            bot_member = await client.get_chat_member(chat.id, (await client.get_me()).id)
            if bot_member.privileges and bot_member.privileges.can_invite_users:
                invite = await client.export_chat_invite_link(chat.id)
                return invite
            else:
                # Fallback: deep link (only works for members)
                return f"tg://openchat?chat_id={chat.id}"
        except Exception:
            return f"tg://openchat?chat_id={chat.id}"
    elif chat.type == enums.ChatType.PRIVATE:
        return f"tg://user?id={chat.id}"
    return None


# --- Autoantiraid & antiraid punish settings ---
async def get_autoantiraid_threshold(chat_id: int) -> int:
    return int(await get_chat_setting(chat_id, "autoantiraid_threshold", "0"))

async def set_autoantiraid_threshold(chat_id: int, threshold: int):
    await set_chat_setting(chat_id, "autoantiraid_threshold", str(threshold))

async def get_antiraid_punish_action(chat_id: int) -> str:
    return await get_chat_setting(chat_id, "antiraid_punish_action", "mute")

async def set_antiraid_punish_action(chat_id: int, action: str):
    await set_chat_setting(chat_id, "antiraid_punish_action", action)

async def get_antiraid_punish_duration(chat_id: int) -> int:
    return int(await get_chat_setting(chat_id, "antiraid_punish_duration", "0"))

async def set_antiraid_punish_duration(chat_id: int, duration: int):
    await set_chat_setting(chat_id, "antiraid_punish_duration", str(duration))


async def get_antiraid_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "antiraid_enabled", "0") == "1"

async def set_antiraid_enabled(chat_id: int, enabled: bool):
    await set_chat_setting(chat_id, "antiraid_enabled", to_bool_str(enabled))

async def get_antiraid_duration(chat_id: int) -> int:
    return int(await get_chat_setting(chat_id, "antiraid_duration", "0"))

async def set_antiraid_duration(chat_id: int, seconds: int):
    await set_chat_setting(chat_id, "antiraid_duration", str(seconds))

async def get_antiraid_expiry(chat_id: int) -> int:
    return int(await get_chat_setting(chat_id, "antiraid_expiry", "0"))

async def set_antiraid_expiry(chat_id: int, timestamp: int):
    await set_chat_setting(chat_id, "antiraid_expiry", str(timestamp))


async def expire_antiraid_loop():
    """Periodically disable antiraid when the duration expires."""
    while True:
        try:
            now = int(time.time())
            async for doc in chat_settings_col.find({"antiraid_enabled": "1", "antiraid_expiry": {"$ne": "0"}}):
                chat_id = doc["chat_id"]
                expiry = int(doc.get("antiraid_expiry", "0"))
                if expiry > 0 and now >= expiry:
                    await set_antiraid_enabled(chat_id, False)
                    await set_antiraid_expiry(chat_id, 0)
                    try:
                        await client.send_message(chat_id, "🛡 Antiraid mode has been automatically disabled after the set duration.")
                    except Exception:
                        pass
        except Exception as e:
            print(f"Antiraid expiry loop error: {e}")
        await asyncio.sleep(60)


def is_night_time(start_str: str, end_str: str) -> bool:
    """Return True if current time (Kolkata) is within the given window."""
    def parse_time(t: str):
        t = t.strip()
        if not t:
            return None
        parts = t.split()
        if len(parts) != 2:
            return None
        time_part, ampm = parts[0], parts[1].upper()
        hour_min = time_part.split(":")
        if len(hour_min) != 2:
            return None
        try:
            hour = int(hour_min[0])
            minute = int(hour_min[1])
        except ValueError:
            return None
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour, minute

    parsed_start = parse_time(start_str)
    parsed_end = parse_time(end_str)
    if parsed_start is None or parsed_end is None:
        return False

    start_h, start_m = parsed_start
    end_h, end_m = parsed_end

    # get current time in Kolkata (UTC+5:30)
    now = datetime.now(timezone.utc)
    kolkata = now + timedelta(hours=5, minutes=30)
    current_h = kolkata.hour
    current_m = kolkata.minute
    current_minutes = current_h * 60 + current_m
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    else:
        # crosses midnight
        return current_minutes >= start_minutes or current_minutes < end_minutes
    

def format_duration(seconds: int) -> str:
    """Convert seconds into a human-readable string (e.g., '1h 30m')."""
    if seconds <= 0:
        return "0s"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 and not parts:  # only show seconds if nothing else
        parts.append(f"{secs}s")
    return " ".join(parts)

def to_datetime(ts):
    return datetime.fromtimestamp(ts) if ts is not None else None

def get_clearflood_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Build the clearflood inline keyboard with green tick when enabled."""
    enable_text = "✅ Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "❌ Disable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(enable_text, callback_data="clearflood:enable"),
         InlineKeyboardButton(disable_text, callback_data="clearflood:disable")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="clearflood:refresh")],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

async def is_flood_enabled(chat_id: int) -> bool:
    """Return True if any flood control mode is active."""
    doc = await chat_settings_col.find_one({"chat_id": chat_id})
    if not doc:
        return False
    return doc.get("flood_enabled", "0") == "1" or doc.get("flood_timer_enabled", "0") == "1"

async def get_flood_mode(chat_id: int) -> tuple[str, dict]:
    """Return (mode, params) where mode is 'timer' or 'consecutive' and params are the settings."""
    doc = await chat_settings_col.find_one({"chat_id": chat_id})
    if not doc:
        return "consecutive", {"limit": 5, "window": 10}
    if doc.get("flood_timer_enabled", "0") == "1":
        count = int(doc.get("flood_timer_count", "5"))
        seconds = int(doc.get("flood_timer_seconds", "10"))
        return "timer", {"count": count, "seconds": seconds}
    else:
        limit = int(doc.get("flood_limit", "5"))
        window = FLOOD_WINDOW   # global constant
        return "consecutive", {"limit": limit, "window": window}

def get_flood_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Build the flood control inline keyboard based on current state."""
    enable_text = "✅ Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "❌ Disable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(enable_text, callback_data="flood_enable"),
         InlineKeyboardButton(disable_text, callback_data="flood_disable")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="flood_refresh")],
    ])

def build_number_pad(current_duration: str) -> InlineKeyboardMarkup:
    """Create a number pad keyboard with digits, clear, back, and OK buttons."""
    rows = []
    for i in range(1, 10, 3):
        row = []
        for j in range(3):
            digit = str(i + j)
            row.append(InlineKeyboardButton(digit, callback_data=f"floodmode_digit:{digit}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("0", callback_data="floodmode_digit:0"),
        InlineKeyboardButton("⌫ Clear", callback_data="floodmode_clear")
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="floodmode_back"),
        InlineKeyboardButton("✅ OK", callback_data="floodmode_ok")
    ])
    return InlineKeyboardMarkup(rows)

def build_number_pad_warnmode(current_duration: str) -> InlineKeyboardMarkup:
    """Number pad for warnmode duration input."""
    rows = []
    for i in range(1, 10, 3):
        row = []
        for j in range(3):
            digit = str(i + j)
            row.append(InlineKeyboardButton(digit, callback_data=f"warnmode_digit:{digit}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("0", callback_data="warnmode_digit:0"),
        InlineKeyboardButton("⌫ Clear", callback_data="warnmode_clear")
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="warnmode_back"),
        InlineKeyboardButton("✅ OK", callback_data="warnmode_ok")
    ])
    return InlineKeyboardMarkup(rows)

def get_flood_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Build the flood control inline keyboard based on current state."""
    enable_text = "✅ Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "❌ Disable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(enable_text, callback_data="flood_enable"),
         InlineKeyboardButton(disable_text, callback_data="flood_disable")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="flood_refresh")],
    ])

async def get_quietfed_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "quietfed_enabled", "0") == "1"

async def set_quietfed_enabled(chat_id: int, enabled: bool) -> None:
    await set_chat_setting(chat_id, "quietfed_enabled", to_bool_str(enabled))

async def get_anonadmin_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "anonadmin_enabled", "0") == "1"

async def set_anonadmin_enabled(chat_id: int, enabled: bool):
    await set_chat_setting(chat_id, "anonadmin_enabled", to_bool_str(enabled))

async def get_nightmode_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "nightmode_enabled", "0") == "1"

async def set_nightmode_enabled(chat_id: int, enabled: bool):
    await set_chat_setting(chat_id, "nightmode_enabled", to_bool_str(enabled))

async def get_nightmode_start(chat_id: int) -> str:
    return await get_chat_setting(chat_id, "nightmode_start", "22:00")

async def set_nightmode_start(chat_id: int, time_str: str):
    await set_chat_setting(chat_id, "nightmode_start", time_str)

async def get_nightmode_end(chat_id: int) -> str:
    return await get_chat_setting(chat_id, "nightmode_end", "06:00")

async def set_nightmode_end(chat_id: int, time_str: str):
    await set_chat_setting(chat_id, "nightmode_end", time_str)

async def get_last_admincache(chat_id: int) -> int:
    val = await get_chat_setting(chat_id, "last_admincache_ts", "0")
    return int(val)

async def set_last_admincache(chat_id: int, timestamp: int):
    await set_chat_setting(chat_id, "last_admincache_ts", str(timestamp))

async def get_cleanlinked_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "cleanlinked_enabled", "0") == "1"

async def set_cleanlinked_enabled(chat_id: int, enabled: bool):
    await set_chat_setting(chat_id, "cleanlinked_enabled", to_bool_str(enabled))

async def get_antichannel_enabled(chat_id: int) -> bool:
    return await get_chat_setting(chat_id, "antichannel_enabled", "0") == "1"

async def set_antichannel_enabled(chat_id: int, enabled: bool):
    await set_chat_setting(chat_id, "antichannel_enabled", to_bool_str(enabled))

async def get_last_manual_pin(chat_id: int) -> int | None:
    val = await get_chat_setting(chat_id, "last_manual_pin", "0")
    return int(val) if val != "0" else None

async def set_last_manual_pin(chat_id: int, message_id: int):
    await set_chat_setting(chat_id, "last_manual_pin", str(message_id))

async def is_permapin(chat_id: int, message_id: int) -> bool:
    doc = await permapin_messages_col.find_one({"chat_id": chat_id, "message_id": message_id})
    return doc is not None

async def user_has_permission(client: Client, chat_id: int, user_id: int, perm: str) -> bool:
    """Check if a user has a specific admin permission in a chat."""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status == enums.ChatMemberStatus.OWNER:
            return True  # Owner has all permissions
        if member.status == enums.ChatMemberStatus.ADMINISTRATOR and member.privileges:
            return getattr(member.privileges, perm, False)
        return False
    except Exception:
        return False

async def require_bot_admin(client: Client, message: Message) -> bool:
    """
    Checks if bot is admin in the group. If not, sends a reply and returns False.
    Only applies to groups/supergroups; PMs always return True.
    """
    chat = message.chat
    if chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return True  # No need admin in private chats

    try:
        bot_member = await client.get_chat_member(chat.id, (await client.get_me()).id)
        if bot_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True
        else:
            await message.reply_text("❌ Please make me an admin first!")
            return False
    except Exception as e:
        await message.reply_text(f"❌ Could not verify admin status: {e}")
        return False

async def check_ban_permissions(client, message):
    chat_id = message.chat.id

    # BOT CHECK
    bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)

    if not bot_member.privileges:
        await message.reply_text("❌ I must be admin to do this.")
        return False

    if not bot_member.privileges.can_restrict_members:
        await message.reply_text("❌ I do not have the right to restrict members.")
        return False

    # USER CHECK (skip if anonymous admin)
    if message.from_user:
        admin_member = await client.get_chat_member(chat_id, message.from_user.id)

        if not admin_member.privileges:
            await message.reply_text("❌ You must be admin.")
            return False

        if not admin_member.privileges.can_restrict_members:
            await message.reply_text("❌ You do not have the right to restrict members.")
            return False

    return True

async def check_target_status(client, chat_id, user_id, action):
    try:
        member = await client.get_chat_member(chat_id, user_id)

        # admin protection
        if member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            return "admin"

        # already banned
        if action in ["ban", "dban", "sban", "tban"] and member.status == enums.ChatMemberStatus.BANNED:
            return "already_banned"

        # already muted
        if action == "mute" and member.status == enums.ChatMemberStatus.RESTRICTED:
            if not member.permissions.can_send_messages:
                return "already_muted"

        return "ok"

    except Exception:
        return "ok"
    
async def require_admin_proof(client: Client, message: Message, action_name: str) -> bool:
    # If anonymous admin detection
    if message.from_user is None and message.sender_chat:
        # Check if anonymous admin mode is enabled in this chat
        chat_id = message.chat.id
        if await get_anonadmin_enabled(chat_id):
            # No need for proof – proceed as verified
            return True

        # Otherwise, send the proof button (existing code)
        action_id = str(uuid.uuid4())
        pending_admin_actions[action_id] = {
            "chat_id": message.chat.id,
            "message": message,
            "action": action_name,
            "time": time.time(),
            "used": False
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔐 Click To Prove Admin",
                callback_data=f"prove_admin:{action_id}"
            )
        ]])
        await message.reply_text(
            "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
            reply_markup=keyboard
        )
        return False

    # Not anonymous – no proof needed
    return True

async def delete_verify_button(msg):
    await asyncio.sleep(30)
    try:
        await msg.delete()
    except:
        pass

async def expire_warns_loop():
    """Periodically remove warns older than the chat's warn_expiry setting."""
    while True:
        try:
            print("[Expiry] Checking for expired warns...")
            # Get all unique chat IDs that have any warns
            pipeline = [{"$group": {"_id": "$chat_id"}}]
            chat_ids_cursor = warns_col.aggregate(pipeline)
            chat_ids = [doc["_id"] async for doc in chat_ids_cursor]

            for chat_id in chat_ids:
                expiry_str = await get_chat_setting(chat_id, "warn_expiry", "0")
                expiry = int(expiry_str)
                if expiry <= 0:
                    continue
                cutoff = int(time.time()) - expiry

                # Remove expired warns from all users in this chat
                result = await warns_col.update_many(
                    {"chat_id": chat_id},
                    {"$pull": {"warns": {"time": {"$lt": cutoff}}}}
                )
                if result.modified_count:
                    print(f"[Expiry] Removed expired warns for {result.modified_count} users in chat {chat_id}")

                # Delete user documents that now have an empty warns array
                deleted = await warns_col.delete_many({"chat_id": chat_id, "warns": {"$size": 0}})
                if deleted.deleted_count:
                    print(f"[Expiry] Deleted {deleted.deleted_count} empty warning records in chat {chat_id}")

        except Exception as e:
            print(f"[Expiry Error] {e}")

        await asyncio.sleep(60)  # check every minute

async def is_chat_owner(client: Client, message: Message) -> bool:
    """Check if the user (or anonymous admin) is the chat owner."""
    if message.from_user:
        member = await client.get_chat_member(message.chat.id, message.from_user.id)
        if member.status == enums.ChatMemberStatus.OWNER:
            return True
    if message.sender_chat:
        member = await client.get_chat_member(message.chat.id, message.sender_chat.id)
        if member.status == enums.ChatMemberStatus.OWNER:
            return True
    return False
    
async def bot_is_admin(client: Client, chat_id: int) -> bool:
    """Check if bot is an admin in the chat."""
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        return member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
    except Exception:
        return False

async def get_warn_action(chat_id: int) -> str:
    """Get the action to take when warn limit is reached."""
    return await get_chat_setting(chat_id, "warn_action", "mute")

async def set_warn_action(chat_id: int, action: str) -> None:
    await set_chat_setting(chat_id, "warn_action", action)


async def get_chat_setting(chat_id: int, key: str, default: str = "0") -> str:
    """Retrieve a specific setting for a chat from MongoDB."""
    doc = await chat_settings_col.find_one({"chat_id": chat_id})
    if doc and key in doc:
        return str(doc[key])
    # Defaults for voice chat features
    vc_defaults = {"vc_msg_enabled": "1", "vc_invite_enabled": "1"}
    if key in vc_defaults:
        return vc_defaults[key]
    return default

async def set_chat_setting(chat_id: int, key: str, value: str) -> None:
    """Store a chat setting in MongoDB (upsert)."""
    await chat_settings_col.update_one(
        {"chat_id": chat_id},
        {"$set": {key: value}},
        upsert=True
    )

async def get_chat_settings(chat_id: int) -> dict:
    """Retrieve all settings for a chat (used for welcome/goodbye)."""
    doc = await chat_settings_col.find_one({"chat_id": chat_id})
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
    """Update multiple chat settings at once."""
    await chat_settings_col.update_one(
        {"chat_id": chat_id},
        {"$set": kwargs},
        upsert=True
    )

async def get_rules(chat_id: int) -> str | None:
    doc = await chat_settings_col.find_one({"chat_id": chat_id})
    return doc.get("rules") if doc else None

async def set_rules(chat_id: int, rules_text: str) -> None:
    await chat_settings_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"rules": rules_text}},
        upsert=True
    )

# --- Bio config helpers ---
async def get_bio_config(chat_id: int):
    doc = await bio_config_col.find_one({"chat_id": chat_id})
    if doc:
        return doc.get("warn_limit", 3), doc.get("action", "mute"), doc.get("enabled", True)
    return 3, "mute", True

async def set_bio_config(chat_id: int, key: str, value):
    await bio_config_col.update_one({"chat_id": chat_id}, {"$set": {key: value}}, upsert=True)

async def increment_bio_stat(field: str):
    await system_info_col.update_one(
        {"type": "stats"},
        {"$inc": {field: 1}},
        upsert=True
    )

async def get_adminlist_text(client: Client, chat_id: int, chat_title: str) -> str:
    """Generate the admin list text for a given chat."""
    owner = []
    co_founders = []
    admins = []
    total_count = 0

    async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        user = member.user
        
        # Skip bots
        if user.is_bot:
            continue

        # Extract privileges early
        p = member.privileges

        # Correct Pyrogram check for any anonymous admin
        is_anonymous = p and p.is_anonymous

        # Handle Hidden vs Visible profiles
        if is_anonymous or user.is_deleted:
            mention = "Hidden"
            title_str = ""  # Keeps the title completely hidden
        else:
            full_name = f"{user.first_name} {user.last_name or ''}".strip()
            mention = f"<a href='tg://user?id={user.id}'>{escape(full_name)}</a>"
            title_str = f" {escape(member.custom_title)}" if member.custom_title else ""

        total_count += 1

        # Categorize Owner
        if member.status == enums.ChatMemberStatus.OWNER:
            owner.append(f"• {mention}{title_str}")
            continue

        # Categorize Co-Founders vs Standard Admins
        if p and p.can_change_info and p.can_promote_members:
            co_founders.append(f"• {mention}{title_str}")
        else:
            admins.append(f"• {mention}{title_str}")

    # Build the final string
    reply = f"<b>Admins in {escape(chat_title)}:</b>\n\n"
    
    if owner:
        reply += "<b>👑 OWNER</b>\n" + "\n".join(owner) + "\n\n"
    if co_founders:
        reply += "<b>⚡ CO-FOUNDERS</b>\n" + "\n".join(co_founders) + "\n\n"
    if admins:
        reply += "<b>📋 ADMINS</b>\n" + "\n".join(admins) + "\n\n"
        
    reply += f"<b>Total Admins:</b> {total_count}"
    
    return reply

# --- Welcome text formatting ---
def format_welcome_text(template: str | None, user) -> str:
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

# --- Permission and helper checks ---
async def bot_has_permission(client: Client, chat_id: int, perm: str) -> bool:
    try:
        me = await client.get_me()
        bot_member = await client.get_chat_member(chat_id, me.id)
        if bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR:
            priv = bot_member.privileges
            if priv:
                return getattr(priv, perm, False)
        return False
    except Exception:
        return False
    
async def get_warn_limit(chat_id: int) -> int:
    return int(await get_chat_setting(chat_id, "warn_limit", "3"))

async def set_warn_limit(chat_id: int, limit: int) -> None:
    await set_chat_setting(chat_id, "warn_limit", str(limit))

async def is_admin(client: Client, message: Message, uid: int | None = None) -> bool:
    chat = message.chat
    user = message.from_user
    if uid is not None:
        user_id = uid
    elif user:
        user_id = user.id
    else:
        if message.sender_chat and message.sender_chat.id == chat.id:
            return True
        return False
    try:
        member = await client.get_chat_member(chat.id, user_id)
        return member.status in {enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER}
    except Exception:
        return False

async def require_admin(client: Client, message: Message, user_id: int = None) -> bool:
    if user_id is None:
        if message.from_user:
            user_id = message.from_user.id
        else:
            await message.reply_text("Could not identify user.")
            return False
    if OWNER_ID and user_id == OWNER_ID:
        return True
    if not await is_admin(client, message, user_id):
        await message.reply_text("You need to be an admin to do this..")
        return False
    return True

async def is_approved(chat_id: int, user_id: int) -> bool:
    doc = await approved_col.find_one({"chat_id": chat_id, "user_id": user_id})
    return doc is not None

def target_user(message: Message) -> int | None:
    if message.reply_to_message:
        return message.reply_to_message.from_user.id
    return None

# ----- Update get_target_user -----
async def get_target_user(client: Client, message: Message):
    """Extract user from reply, user ID, username, or full name mention."""
    # If reply, get that user
    if message.reply_to_message:
        return message.reply_to_message.from_user

    args = get_args(message)
    if not args:
        return None

    # Join all arguments to support multi‑word names
    user_input = " ".join(args).strip()

    # Private chat: just try to get the user globally
    if message.chat.type in (enums.ChatType.PRIVATE, enums.ChatType.BOT):
        try:
            return await client.get_users(user_input)
        except Exception:
            return None

    # Try to parse as user ID (must be numeric only)
    if user_input.isdigit():
        try:
            return await client.get_users(int(user_input))
        except:
            pass

    # Try as username (strip leading @)
    if user_input.startswith('@'):
        user_input = user_input[1:]
    try:
        return await client.get_users(user_input)
    except:
        pass

    # --- Full name matching in groups ---
    chat_id = message.chat.id
    full_name = user_input.lower().strip()
    # Search among recently active members (from active_users cache)
    recent_uids = list(active_users.get(chat_id, deque()))
    for uid in recent_uids:
        try:
            user = await client.get_users(uid)
            name = f"{user.first_name} {user.last_name or ''}".strip().lower()
            if name == full_name:
                return user
        except:
            continue

    # If still not found, try to fetch all members (slow, only as last resort)
    try:
        async for member in client.get_chat_members(chat_id):
            user = member.user
            if user.is_bot or user.is_deleted:
                continue
            name = f"{user.first_name} {user.last_name or ''}".strip().lower()
            if name == full_name:
                return user
    except:
        pass

    return None

def parse_duration(raw: str) -> int:
    """Convert duration string like 1m, 2h, 3d, 1w, 2M, 1y to seconds."""
    m = re.fullmatch(r"(\d+)([mhdwMy])", raw.lower().strip())
    if not m:
        return 0
    num, unit = int(m.group(1)), m.group(2)
    if unit == "m":      # minutes
        return num * 60
    if unit == "h":      # hours
        return num * 3600
    if unit == "d":      # days
        return num * 86400
    if unit == "w":      # weeks
        return num * 604800
    if unit == "M":      # months (30 days)
        return num * 2592000
    if unit == "y":      # years (365 days)
        return num * 31536000
    return 0

# --- Core command handlers ---
async def start(client: Client, message: Message) -> None:
    me = await client.get_me()
    args = get_args(message)
    if message.chat.type == enums.ChatType.PRIVATE and args:
        arg = args[0].lower()
        if arg == "help":
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
        await q.edit_message_text(
            f"Hey there! My name is {bot_name}— I'm here to help you manage your groups!\n"
            "Use /help to find out how to use me to my full potential.\n\n"
            "Check /privacy to view the privacy policy, and interact with your data.\n",
            reply_markup=start_keyboard(bot_username)
        )
    elif action == "ownbot":
        await q.edit_message_text(
            f"Get your own clone of {bot_name} with your own bot token!:\n\n"
            "How to clone:\n"
            "1) Create a new bot by BotFather\n"
            "2) Copy the bot token\n"
            "3) Use: /clone YOUR_TOKEN\n\n"
            "Your bot will have all the same features!\n",
            reply_markup=start_back_keyboard(bot_username)
        )

async def clone(client: Client, message: Message) -> None:
    await message.reply_text(
        "Clone guide:\n"
        "git clone <repo-url>\n"
        "cd <repo>\n"
        "python -m venv .venv && source .venv/bin/activate\n"
        "pip install -r requirements.txt\n"
        "cp .env.example .env (set BOT_TOKEN)\n"
        "python bot.py"
    )

async def help_cmd(client: Client, message: Message) -> None:
    me = await client.get_me()
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("See in DM.", reply_markup=dm_keyboard(me.username))
        return
    title, body = HELP_SECTIONS["home"]
    dynamic_title = title.replace("Help Menu", f"{me.first_name} Help")
    await message.reply_text(
        f"<b>{dynamic_title}</b>\n\n{body}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=help_keyboard()
    )

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
        await q.edit_message_text(
            f"<b>{dynamic_title}</b>\n\n{body}",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=help_keyboard()
        )
    else:
        await q.edit_message_text(body, parse_mode=enums.ParseMode.HTML, reply_markup=back_keyboard())

async def setlog_command(client: Client, message: Message):
    """
    Usage:
    1. Add bot to your channel as admin.
    2. Send /setlog in that channel.
    3. Forward that message to the group you want logged.
    """
    # If command is used in a channel (sender_chat exists and it's a channel)
    if message.sender_chat and message.sender_chat.type == enums.ChatType.CHANNEL:
        await message.reply_text(
            "✅ Now forward this message to the group you want to log.\n\n"
            "After forwarding, the log channel will be set automatically."
        )
        return

    # If command is used in a group (and not replying to a channel message), check owner
    if message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        if not await is_chat_owner(client, message):
            await message.reply_text("❌ Only the group owner can use this command.")
            return

    # If command is forwarded from a channel (check reply_to_message)
    if message.reply_to_message and message.reply_to_message.sender_chat:
        channel_chat = message.reply_to_message.sender_chat
        if channel_chat.type == enums.ChatType.CHANNEL:
            # Owner check (already done above, but kept for safety)
            if not await is_chat_owner(client, message):
                return await message.reply_text("❌ Only the group owner can set the log channel.")
            
            log_channel_id = channel_chat.id
            await set_chat_setting(message.chat.id, "log_channel", str(log_channel_id))
            await set_chat_setting(message.chat.id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
            await message.reply_text(f"✅ Log channel set to {channel_chat.title} (ID: `{log_channel_id}`)")
            return

    # Otherwise, show help (only reachable by owners now)
    await message.reply_text(
        "**How to set a log channel:**\n\n"
        "1. Add me to your channel as an admin.\n"
        "2. Send `/setlog` in that channel.\n"
        "3. Forward that message to this group.\n\n"
        "That's it! Logs will start appearing in your channel."
    )

async def unsetlog_command(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.reply_text("This command only works in groups.")
    if not await is_chat_owner(client, message):
        return await message.reply_text("❌ Only the group owner can unset the log channel.")
    await set_chat_setting(message.chat.id, "log_channel", "not_set")
    await set_chat_setting(message.chat.id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
    await message.reply_text("✅ Log channel removed for this group.")

async def logchannel_cmd(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.reply_text("This command only works in groups.")
    if not await is_admin(client, message):
        return await message.reply_text("❌ You need to be an admin to see the log channel.")
    log_chan = await get_chat_setting(message.chat.id, "log_channel", "not_set")
    await message.reply_text(f"📋 Log channel: `{log_chan}`")

async def logcategories_cmd(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.reply_text("This command only works in groups.")
    if not await is_chat_owner(client, message):
        return await message.reply_text("❌ Only the group owner can manage log categories.")
    
    categories_str = await get_chat_setting(message.chat.id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
    categories = json.loads(categories_str) if isinstance(categories_str, str) else categories_str
    
    text = "**📋 Log Categories**\n\n"
    for cat, enabled in categories.items():
        status = "✅" if enabled else "❌"
        text += f"{status} `{cat}`\n"
    text += "\nUse `/log <category>` to enable, `/nolog <category>` to disable."
    await message.reply_text(text)
    
async def log_enable(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.reply_text("This command only works in groups.")
    if not await is_chat_owner(client, message):
        return await message.reply_text("❌ Only the group owner can change log settings.")
    
    args = get_args(message)
    if not args or args[0].lower() not in DEFAULT_LOG_CATEGORIES:
        return await message.reply_text(f"Usage: `/log <category>`\nAvailable: {', '.join(DEFAULT_LOG_CATEGORIES.keys())}")
    
    cat = args[0].lower()
    categories_str = await get_chat_setting(message.chat.id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
    categories = json.loads(categories_str) if isinstance(categories_str, str) else categories_str
    categories[cat] = True
    await set_chat_setting(message.chat.id, "log_categories", json.dumps(categories))
    await message.reply_text(f"✅ Logging for `{cat}` enabled.")
    
async def log_disable(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.reply_text("This command only works in groups.")
    if not await is_chat_owner(client, message):
        return await message.reply_text("❌ Only the group owner can change log settings.")
    
    args = get_args(message)
    if not args or args[0].lower() not in DEFAULT_LOG_CATEGORIES:
        return await message.reply_text(f"Usage: `/nolog <category>`\nAvailable: {', '.join(DEFAULT_LOG_CATEGORIES.keys())}")
    
    cat = args[0].lower()
    categories_str = await get_chat_setting(message.chat.id, "log_categories", json.dumps(DEFAULT_LOG_CATEGORIES))
    categories = json.loads(categories_str) if isinstance(categories_str, str) else categories_str
    categories[cat] = False
    await set_chat_setting(message.chat.id, "log_categories", json.dumps(categories))
    await message.reply_text(f"❌ Logging for `{cat}` disabled.")
    
async def owner_group(client: Client, message: Message):
    chat = message.chat

    if chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        await message.reply_text("This command only works in groups.")
        return

    try:
        owner = None
        owner_member = None

        async for member in client.get_chat_members(
            chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS
        ):
            if member.status == enums.ChatMemberStatus.OWNER:
                owner_member = member
                owner = member.user
                break

        if not owner_member or not owner:
            return await message.reply_text("Could not find group owner.")

        # Correct check: Look inside member.privileges
        is_anonymous = owner_member.privileges and owner_member.privileges.is_anonymous

        if is_anonymous or owner.is_deleted:
            owner_display = "Hidden"
        else:
            full_name = f"{owner.first_name} {owner.last_name or ''}".strip()
            if owner.username:
                owner_display = f"{full_name} (@{owner.username})"
            else:
                owner_display = full_name

        text = f"👑 **Group Owner:** {owner_display}"
        await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)

    except Exception as e:
        await message.reply_text(f"Error: {e}")

# -------------------- Owner & Sudo Commands --------------------
async def chats_cmd(client: Client, message: Message):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    chats = await active_chats_col.find().sort("last_activity", -1).to_list(length=None)
    if not chats:
        return await message.reply_text("No active chats found.")
    reply = "**Active Chats (Group + DM)**\n\n"
    for idx, chat in enumerate(chats, start=1):
        chat_id = chat["chat_id"]
        title = chat.get("title", "Unknown")
        chat_type = chat["type"]
        emoji = "👥" if chat_type in ["group", "supergroup"] else "👤"
        reply += f"{idx}. {emoji} {title} (`{chat_id}`)\n"
    await message.reply_text(reply, parse_mode=enums.ParseMode.MARKDOWN)

async def pchats_cmd(client: Client, message: Message):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    chats = await active_chats_col.find({"type": "private"}).sort("last_activity", -1).to_list(length=None)
    if not chats:
        return await message.reply_text("No active private chats found.")
    reply = "**Active Private Chats**\n\n"
    for idx, chat in enumerate(chats, start=1):
        chat_id = chat["chat_id"]
        title = chat.get("title", "Unknown")
        reply += f"{idx}. {title} (`{chat_id}`)\n"
    await message.reply_text(reply, parse_mode=enums.ParseMode.MARKDOWN)

async def gchats_cmd(client: Client, message: Message):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    chats = await active_chats_col.find({"type": {"$in": ["group", "supergroup"]}}).sort("last_activity", -1).to_list(length=None)
    if not chats:
        return await message.reply_text("No active groups found.")
    reply = "**Active Groups**\n\n"
    for idx, chat in enumerate(chats, start=1):
        chat_id = chat["chat_id"]
        title = chat.get("title", "Unknown")
        reply += f"{idx}. 👥 {title} (`{chat_id}`)\n"
    await message.reply_text(reply, parse_mode=enums.ParseMode.MARKDOWN)

async def getlink_cmd(client: Client, message: Message):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /getlink <serial_number>")
    try:
        sn = int(args[0])
    except:
        return await message.reply_text("Invalid serial number.")
    chats = await active_chats_col.find().sort("last_activity", -1).to_list(length=None)
    if sn < 1 or sn > len(chats):
        return await message.reply_text("Serial number out of range.")
    chat_data = chats[sn-1]
    chat_id = chat_data["chat_id"]
    try:
        chat = await client.get_chat(chat_id)
    except Exception as e:
        return await message.reply_text(f"Failed to fetch chat: {e}")

    link = await get_chat_link(client, chat)
    title = chat_data.get("title", "Unknown")
    chat_type = chat_data.get("type", "unknown")

    if link:
        # Create a clickable button
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Click to open", url=link)]])
        text = (
            f"**{title}** (`{chat_id}`)\n"
            f"Type: {chat_type}\n"
            f"Link ready:"
        )
        await message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    else:
        await message.reply_text(f"Could not generate a link for **{title}** (`{chat_id}`).", parse_mode=enums.ParseMode.MARKDOWN)
        
async def syncchats_cmd(client: Client, message: Message):
    """Remove inactive chats from the active_chats collection."""
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")

    await message.reply_text("🔄 Syncing active chats... This may take a while.")

    bot_me = await client.get_me()
    bot_id = bot_me.id
    removed = 0
    total = 0

    # Iterate over all active chats
    async for chat_data in active_chats_col.find({}):
        total += 1
        chat_id = chat_data["chat_id"]
        chat_type = chat_data.get("type", "unknown")

        # For groups and supergroups, check if bot is still a member
        if chat_type in ("group", "supergroup"):
            try:
                member = await client.get_chat_member(chat_id, bot_id)
                # If bot is not a member (i.e., status not in MEMBER, ADMIN, OWNER), delete
                if member.status not in (enums.ChatMemberStatus.MEMBER,
                                         enums.ChatMemberStatus.ADMINISTRATOR,
                                         enums.ChatMemberStatus.OWNER):
                    await active_chats_col.delete_one({"chat_id": chat_id})
                    removed += 1
            except Exception as e:
                # If any error occurs (chat deleted, bot not found, etc.), treat as inactive
                print(f"Error checking chat {chat_id}: {e}")
                await active_chats_col.delete_one({"chat_id": chat_id})
                removed += 1
        else:
            # For private chats, we keep them (no reliable way to detect block)
            pass

    await message.reply_text(
        f"✅ Sync complete.\n"
        f"Total active chats processed: {total}\n"
        f"Removed inactive groups: {removed}"
    )

async def broadcast_cmd(client: Client, message: Message, pin=False, mode="all"):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    reply = message.reply_to_message
    args = get_args(message)
    if not reply and not args:
        return await message.reply_text("Reply to a message or provide text to broadcast.")
    text = ""
    media = None
    if reply:
        text = reply.text or reply.caption or ""
        if reply.photo:
            media = ("photo", reply.photo.file_id)
        elif reply.video:
            media = ("video", reply.video.file_id)
        elif reply.document:
            media = ("document", reply.document.file_id)
        elif reply.animation:
            media = ("animation", reply.animation.file_id)
        elif reply.sticker:
            media = ("sticker", reply.sticker.file_id)
    else:
        text = " ".join(args)
    # Store in pending
    pending_broadcast[message.from_user.id] = {
        "text": text,
        "media": media,
        "pin": pin,
        "mode": mode,
        "message": message
    }
    # Send confirmation keyboard
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Broadcast", callback_data=f"broadcast_confirm:{message.from_user.id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"broadcast_cancel:{message.from_user.id}")]
    ])
    await message.reply_text("⚠️ You are about to broadcast to all active chats. Confirm?", reply_markup=keyboard)

async def gcast_cmd(client: Client, message: Message):
    await broadcast_cmd(client, message, pin=False, mode="group")

async def pcast_cmd(client: Client, message: Message):
    await broadcast_cmd(client, message, pin=False, mode="private")

async def broadcastpin_cmd(client: Client, message: Message):
    await broadcast_cmd(client, message, pin=True, mode="all")

async def gcastpin_cmd(client: Client, message: Message):
    await broadcast_cmd(client, message, pin=True, mode="group")

async def pcastpin_cmd(client: Client, message: Message):
    await broadcast_cmd(client, message, pin=True, mode="private")

async def broadcastunpin_cmd(client: Client, message: Message, mode="all"):
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    # Get all chats of the specified mode
    query = {}
    if mode == "group":
        query["type"] = {"$in": ["group", "supergroup"]}
    elif mode == "private":
        query["type"] = "private"
    chats = await active_chats_col.find(query).to_list(length=None)
    if not chats:
        return await message.reply_text("No active chats found for the selected mode.")
    count = 0
    for chat_data in chats:
        chat_id = chat_data["chat_id"]
        # Check if bot has pin permission in this chat
        try:
            chat = await client.get_chat(chat_id)
            if chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
                # Try to unpin the latest pinned message
                pinned = chat.pinned_message
                if pinned:
                    await client.unpin_chat_message(chat_id, pinned.id)
                    count += 1
        except Exception:
            continue
    await message.reply_text(f"Unpinned the latest message in {count} chats (only where possible).")

async def gcastunpin_cmd(client: Client, message: Message):
    await broadcastunpin_cmd(client, message, mode="group")

async def pcastunpin_cmd(client: Client, message: Message):
    await broadcastunpin_cmd(client, message, mode="private")

async def add_sudo(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("❌ Only the bot owner can manage sudo users.")
    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")
    user_id = target.id
    if user_id == OWNER_ID:
        return await message.reply_text("The owner is already sudo.")
    existing = await sudo_users_col.find_one({"user_id": user_id})
    if existing:
        return await message.reply_text("User is already a sudo user.")
    await sudo_users_col.insert_one({"user_id": user_id})
    await message.reply_text(f"✅ {target.mention} added as sudo user.")

async def rm_sudo(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("❌ Only the bot owner can manage sudo users.")
    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")
    user_id = target.id
    if user_id == OWNER_ID:
        return await message.reply_text("The owner cannot be removed from sudo.")
    result = await sudo_users_col.delete_one({"user_id": user_id})
    if result.deleted_count:
        await message.reply_text(f"❌ {target.mention} removed from sudo users.")
    else:
        await message.reply_text("User was not a sudo user.")

async def sudo_list(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("❌ Only the bot owner can view the sudo list.")
    users = await sudo_users_col.find().to_list(length=100)
    if not users:
        return await message.reply_text("No sudo users.")
    text = "**Sudo Users**\n"
    for u in users:
        user_id = u["user_id"]
        try:
            user = await client.get_users(user_id)
            text += f"• {user.mention} (<code>{user_id}</code>)\n"
        except:
            text += f"• <code>{user_id}</code>\n"
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def generic_toggle(client: Client, message: Message, key: str, label: str, verified=False, admin_id: int = None) -> None:
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await generic_toggle(client, message, key, label, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": label,
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if not args or args[0].lower() not in {"on", "off"}:
        await message.reply_text(f"Usage: /{label} on|off")
        return
    enabled = args[0].lower() == "on"
    await set_chat_setting(message.chat.id, key, to_bool_str(enabled))
    await message.reply_text(f"{label} {'enabled' if enabled else 'disabled'}.")
    
async def can_manage_filters(client: Client, message: Message, admin_id: int = None) -> bool:
    user_id = admin_id if admin_id is not None else (message.from_user.id if message.from_user else None)
    if not user_id:
        await message.reply_text("Could not identify admin.")
        return False

    if user_id == OWNER_ID:
        return True

    if not await is_admin(client, message, user_id):
        await message.reply_text("You need to be an admin to do this..")
        return False

    member = await client.get_chat_member(message.chat.id, user_id)
    if member.privileges and member.privileges.can_change_info:
        return True
    await message.reply_text("You need 'Change Group Info' permission.")
    return False

async def filter_cmd_handler(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        # Check if anonymous admin mode is enabled for this chat
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass the button – treat as verified with placeholder admin ID 0
            return await filter_cmd_handler(client, message, verified=True, admin_id=0)
        else:
            # Existing button code (unchanged)
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "filter",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # For anonymous verified (admin_id=0), skip the permission check entirely
    if not (message.from_user is None and verified):
        if not await can_manage_filters(client, message, admin_id=admin_id):
            return

    args = message.text.split(None, 2)
    if len(args) < 2:
        return await message.reply_text("You need to give the filter a name!")

    keyword = args[1].lower()
    reply = message.reply_to_message
    content = None

    if reply:
        content = get_filter_data(reply)
    elif len(args) > 2:
        content = {"type": "text", "data": args[2]}

    if not content:
        return await message.reply_text("❌ Please reply to a message or provide text after the keyword.")

    await filters_col.update_one(
        {"chat_id": message.chat.id, "keyword": keyword},
        {"$set": {"content": content}},
        upsert=True
    )
    await message.reply_text(f"✅ Filter saved for: **{keyword}**")

async def stop_filter_handler(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await stop_filter_handler(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "stopfilter",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # --- NEW: Verified anonymous admin → skip permission check ---
    if verified and message.from_user is None:
        pass
    else:
        if not await can_manage_filters(client, message, admin_id=admin_id):
            return

    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.reply_text("Usage: `/stop <keyword>`")
        return
    keyword = args[1].lower()
    result = await filters_col.delete_one({"chat_id": message.chat.id, "keyword": keyword})
    if result.deleted_count > 0:
        await message.reply_text(f"Stopped filter for: `{keyword}`")
    else:
        await message.reply_text(f"No filter found with keyword: `{keyword}`")

async def list_filters_handler(client: Client, message: Message):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    filters_list = await filters_col.find({"chat_id": message.chat.id}).to_list(length=100)
    if not filters_list:
        return await message.reply_text("No filters in this group.")
    reply = f"📑 **Filters in {message.chat.title}:**\n"
    for f in filters_list:
        reply += f"- `{f['keyword']}`\n"
    await message.reply_text(reply)

async def setwelcome(client: Client, message: Message, verified=False) -> None:
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setwelcome(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setwelcome",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
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

    await update_chat_settings(message.chat.id, welcome=data, welcome_enabled=True)
    await message.reply_text("✅ Welcome message set and enabled.")

async def resetwelcome(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await resetwelcome(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "resetwelcome",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    default_welcome = {"type": "text", "text": "Welcome {fullname}!", "file_id": None, "caption": None}
    await update_chat_settings(message.chat.id, welcome=default_welcome)
    await message.reply_text("✅ Welcome reset to default.")

async def welcome_toggle(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await welcome_toggle(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "welcome_toggle",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if not args or args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /welcome on|off")
        return
    enabled = args[0].lower() == "on"
    await update_chat_settings(message.chat.id, welcome_enabled=enabled)
    await message.reply_text(f"Welcome {'enabled' if enabled else 'disabled'}.")

async def setgoodbye(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setgoodbye(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setgoodbye",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
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

async def resetgoodbye(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await resetgoodbye(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "resetgoodbye",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    default_goodbye = {"type": "text", "text": "Goodbye {fullname}!", "file_id": None, "caption": None}
    await update_chat_settings(message.chat.id, goodbye=default_goodbye)
    await message.reply_text("✅ Goodbye reset to default.")

async def goodbye_toggle(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await goodbye_toggle(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "goodbye_toggle",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
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
    caption = data.get('caption') or data.get('text') or ''
    caption = format_welcome_text(caption, user)
    msg_type = data.get('type', 'text')
    file_id = data.get('file_id')

    try:
        if msg_type == 'photo' and file_id:
            await client.send_photo(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'video' and file_id:
            await client.send_video(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'sticker' and file_id:
            await client.send_sticker(chat_id, file_id, reply_to_message_id=reply_to_message_id)
            if caption:
                await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'animation' and file_id:
            await client.send_animation(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'document' and file_id:
            await client.send_document(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'voice' and file_id:
            await client.send_voice(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        elif msg_type == 'audio' and file_id:
            await client.send_audio(chat_id, file_id, caption=caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
        else:
            await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)
    except Exception as e:
        print(f"Error sending: {e}")
        await client.send_message(chat_id, caption, parse_mode=enums.ParseMode.HTML, reply_to_message_id=reply_to_message_id)

async def on_new_members(client: Client, message: Message) -> None:
    chat_id = message.chat.id
    for member in message.new_chat_members:
        if member.is_bot:
            continue

        # 1. Fedban check
        banned = await check_and_punish_fedbanned_user(client, chat_id, member.id, message)
        if banned:
            continue

        # --- AUTOANTIRAID TRACKING ---
        threshold = await get_autoantiraid_threshold(chat_id)
        if threshold > 0:
            now = time.time()
            join_queue = autoantiraid_joins[chat_id]
            join_queue.append((now, member.id))

            # Remove entries older than 60 seconds
            while join_queue and now - join_queue[0][0] > 60:
                join_queue.popleft()

            # Check if threshold reached
            if len(join_queue) >= threshold:
                # Get the punishment action
                action = await get_antiraid_punish_action(chat_id)
                # We'll use duration 0 (permanent) for now
                punished_users = []
                for ts, uid in list(join_queue):
                    if await is_admin(client, message, uid) or await is_approved(chat_id, uid):
                        continue
                    try:
                        if action == "mute":
                            await client.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False))
                        elif action == "kick":
                            await client.ban_chat_member(chat_id, uid)
                            await client.unban_chat_member(chat_id, uid)
                        elif action == "ban":
                            await client.ban_chat_member(chat_id, uid)
                        punished_users.append(uid)
                    except Exception as e:
                        print(f"Autoantiraid punish failed for {uid}: {e}")

                # Clear the queue to avoid re-punishing
                join_queue.clear()

                # Send notification
                if punished_users:
                    await client.send_message(
                        chat_id,
                        f"🚨 **Autoantiraid triggered!**\n"
                        f"{len(punished_users)} users were {action}ed for joining too fast.\n"
                        f"Threshold: {threshold} joins/minute.",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                else:
                    await client.send_message(
                        chat_id,
                        f"⚠️ Autoantiraid threshold reached but no users could be punished (all were admins or approved).",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

        # 2. Welcome message
        settings = await get_chat_settings(chat_id)
        if settings.get("welcome_enabled", False):
            welcome_data = settings.get("welcome", {})
            await send_welcome_goodbye(client, chat_id, member, welcome_data, reply_to_message_id=message.id)

# Cache to prevent duplicates
welcome_cache = {}
CACHE_TTL = 5

async def on_chat_member_update(client: Client, update: ChatMemberUpdated):
    # Welcome when a user becomes a member (approved joins)
    if (update.new_chat_member and 
        update.new_chat_member.status == enums.ChatMemberStatus.MEMBER and
        (update.old_chat_member is None or update.old_chat_member.status != enums.ChatMemberStatus.MEMBER)):
        user = update.new_chat_member.user
        if user.is_bot:
            return
        chat_id = update.chat.id
        key = (chat_id, user.id)
        now = time.time()
        if key in welcome_cache and now - welcome_cache[key] < CACHE_TTL:
            return
        welcome_cache[key] = now

        settings = await get_chat_settings(chat_id)
        if settings.get("welcome_enabled", False):
            welcome_data = settings.get("welcome", {})
            await send_welcome_goodbye(client, chat_id, user, welcome_data)    
# Simple cache to prevent duplicate goodbye messages
goodbye_cache = {}
CACHE_TTL = 5  # seconds

async def on_left_member(client: Client, message: Message) -> None:
    """
    Sends goodbye message when a user leaves the group, is kicked, or banned.
    """
    # Only process if it's a left member event
    if not message.left_chat_member:
        return

    user = message.left_chat_member
    if user.is_bot:
        return

    chat_id = message.chat.id

    # Deduplication: avoid double messages if both service message and ChatMemberUpdated fire
    key = (chat_id, user.id)
    now = time.time()
    if key in goodbye_cache and now - goodbye_cache[key] < CACHE_TTL:
        return
    goodbye_cache[key] = now

    # Send goodbye message if enabled
    settings = await get_chat_settings(chat_id)
    if settings.get("goodbye_enabled", False):
        goodbye_data = settings.get("goodbye", {})
        await send_welcome_goodbye(client, chat_id, user, goodbye_data)

async def setrules(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setrules(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setrules",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users (only when not verified)
    if not verified:
        if not await require_admin(client, message):
            return

    rules_text = None

    # reply rules support
    if message.reply_to_message:
        reply = message.reply_to_message
        rules_text = reply.text or reply.caption

    # command text rules
    if not rules_text:
        args = get_args(message)
        if not args:
            await message.reply_text(
                "Usage: /setrules <text> or reply to a message with /setrules"
            )
            return
        rules_text = " ".join(args)

    # save rules
    await set_rules(message.chat.id, rules_text)

    await message.reply_text("✅ Rules have been set for this chat.")
    

async def rules(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
                                        
    chat_id = message.chat.id
    if message.chat.type != enums.ChatType.PRIVATE:
        me = await client.get_me()
        deep_link = f"https://t.me/{me.username}?start=rules_{chat_id}"
        button = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Rules", url=deep_link)]])
        await message.reply_text("Click on the button below to see the chat rules.", reply_markup=button)
    else:
        rules_text = await get_rules(chat_id)
        if rules_text:
            await message.reply_text(rules_text, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply_text("No rules set for this chat.")

async def setnote(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setnote(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setnote",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if len(args) < 2:
        await message.reply_text("Usage: /setnote <name> <content>")
        return
    name = args[0].lower()
    content = " ".join(args[1:])
    await notes_col.update_one(
        {"chat_id": message.chat.id, "name": name},
        {"$set": {"content": content}},
        upsert=True
    )
    await message.reply_text("Note saved.")

async def delnote(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await delnote(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "delnote",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if not args:
        return
    name = args[0].lower()
    await notes_col.delete_one({"chat_id": message.chat.id, "name": name})
    await message.reply_text("Note deleted.")    

async def get_note(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             
    args = get_args(message)
    if not args:
        return
    name = args[0].lower()
    doc = await notes_col.find_one({"chat_id": message.chat.id, "name": name})
    if doc:
        await message.reply_text(doc["content"], parse_mode=enums.ParseMode.HTML)

async def warn_common(client: Client, message: Message, delete_cmd: bool = False, silent: bool = False, delete_replied: bool = False, admin_id: int = None, cmd_type: str = None) -> None:
    
    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return
    uid = target.id
    chat_id = message.chat.id

    # Reason extraction
    args = get_args(message)
    reason = None
    if message.reply_to_message:
        reason = " ".join(args) if args else None
    else:
        reason = " ".join(args[1:]) if len(args) > 1 else None

    # Check if target is admin
    if await is_admin(client, message, uid):
        await message.reply_text("I cannot warn an admin.")
        return

    # Bot permission check
    if not await bot_has_permission(client, chat_id, "can_restrict_members"):
        await message.reply_text("I need ban permission to warn users.")
        return

    # Determine admin who issued the warning
    admin = admin_id if admin_id else (message.from_user.id if message.from_user else None)

    # If anonymous admin and mode is enabled, bypass permission check and use placeholder ID 0
    anonadmin_enabled = await get_anonadmin_enabled(chat_id)
    if admin is None and message.from_user is None and anonadmin_enabled:
        # Treat as anonymous admin with full permissions
        admin = 0  # placeholder ID for anonymous admin
        # Skip permission check
    else:
        # Permission check for normal admins
        if not await user_has_permission(client, chat_id, admin, "can_restrict_members"):
            await message.reply_text("❌ You do not have ban rights (can_restrict_members permission required).")
            return

    # Create warning object
    warning = {
        "reason": reason,
        "time": int(time.time()),
        "admin_id": admin
    }

    # Update database: push warning
    await warns_col.update_one(
        {"chat_id": chat_id, "user_id": uid},
        {"$push": {"warns": warning}},
        upsert=True
    )

    # Fetch updated document to get count
    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": uid})
    cnt = len(doc.get("warns", []))
    limit = int(await get_chat_setting(chat_id, "warn_limit", "3"))

    # --- LOG: warning issued ---
    admin_mention_str = message.from_user.mention if message.from_user else "Anonymous"
    await send_log(client, chat_id, "admin", f"Warning ({cnt}/{limit})", target.mention, uid, admin_mention=admin_mention_str, reason=reason)
    
    cmd = cmd_type or (message.command[0] if getattr(message, "command", None) else "")

    # 👇 ADD THIS BLOCK
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    try:
        if cmd in ("warn", "dwarn"):
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❌ Remove Warn", callback_data=f"rmwarn:{chat_id}:{uid}"),
                    InlineKeyboardButton("🔄 Reset Warns", callback_data=f"resetwarn:{chat_id}:{uid}")
                ]
            ])

            await message.reply_text(
                f"⚠️ {target.mention} has been warned! ({cnt}/{limit})\n"
                f"Reason: {reason or 'No reason'}",
                reply_markup=keyboard
            )

    except Exception as e:
        print(f"Warn notify error: {e}")

    
    if cnt >= limit:
        action = await get_warn_action(chat_id)
        now = int(time.time())
        duration = int(await get_chat_setting(chat_id, "warn_duration", "86400"))
        until = now + duration if action in ("tban", "tmute") else None

        try:
            if action == "ban":
                await client.ban_chat_member(chat_id, uid)
                action_text = "banned"
            elif action == "mute":
                await client.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False))
                action_text = "muted"
            elif action == "kick":
                await client.ban_chat_member(chat_id, uid)
                await client.unban_chat_member(chat_id, uid)
                action_text = "kicked"
            elif action == "tban":
                    await client.ban_chat_member(chat_id, uid, until_date=to_datetime(until))
                    action_text = f"temp banned for {format_duration(duration)}"
            elif action == "tmute":
                    await client.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False), until_date=to_datetime(until))
                    action_text = f"temp muted for {format_duration(duration)}"
            else:
                action_text = "action applied"
        except Exception as e:
            await message.reply_text(f"Failed to apply {action}: {e}")
            return

        # Delete all warns after punishment
        await warns_col.delete_one({"chat_id": chat_id, "user_id": uid})

        await send_log(client, chat_id, "admin", f"Warn limit reached → {action}", target.mention, uid, admin_mention=admin_mention_str, reason=reason, extra=f"Duration: {format_duration(duration) if action in ('tban','tmute') else 'permanent'}")
        
        if cmd in ["warn", "dwarn"]:
            reply = f"⚠️ {target.mention} exceeded warn limit ({cnt}/{limit}) and was {action_text}."
            if reason:
                reply += f"\n**Reason:** {reason}"
            try:
                # Optional: check if bot can send messages
                if await bot_has_permission(client, chat_id, "can_send_messages"):
                    await message.reply_text(reply)
                else:
                    # fallback: send as a new message (not a reply)
                    await client.send_message(chat_id, reply)
            except Exception as e:
                print(f"Error sending warn notification: {e}")
            
async def warnings(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await warnings(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "warnings",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users (if not verified)
    if not verified:
        # Determine user_id: either passed admin_id or message.from_user.id
        user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
        if not user_id:
            await message.reply_text("Could not identify admin.")
            return
        if not await require_admin(client, message, user_id=user_id):
            return

    chat_id = message.chat.id
    chat_title = message.chat.title or "this group"

    limit = await get_warn_limit(chat_id)
    mode = await get_warn_action(chat_id)
    duration = await get_chat_setting(chat_id, "warn_duration", "0")
    if mode in ("tban", "tmute"):
        duration_text = f" (Duration: {int(duration)//60} minutes)" if duration != "0" else ""
    else:
        duration_text = ""
    text = (
        f"⚙️ **Warning Settings for {chat_title}**\n\n"
        f"⚠️ Warn Limit: {limit}\n"
        f"🔨 Action Mode: {mode}{duration_text}"
    )

    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)

async def warntime_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    if not verified:
        if not await require_admin_proof(client, message, "warntime"):
            return
        if not await require_admin(client, message, user_id=admin_id):
            return

    # Change info permission check
    user_id = admin_id if admin_id else message.from_user.id
    if not await user_has_permission(client, message.chat.id, user_id, "can_change_info"):
        return await message.reply_text("❌ You do not have 'Change Group Info' permission.")

    args = get_args(message)

    if not args:
        current = await get_chat_setting(message.chat.id, "warn_expiry", "0")
        if current == "0":
            text = "No expiry (warns last forever)"
        else:
            text = f"{current} seconds"
        return await message.reply_text(f"Current warning expiry: {text}")

    duration_str = args[0]
    if duration_str.lower() in ("0", "off"):
        seconds = 0
    else:
        seconds = parse_duration(duration_str)

    await set_chat_setting(message.chat.id, "warn_expiry", str(seconds))
    await message.reply_text("✅ Warning expiry updated.")

async def warnmode_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await warnmode_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "warnmode",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # Permission check – skip for verified anonymous admins (user_id == 0)
    if user_id != 0:
        if not await user_has_permission(client, message.chat.id, user_id, "can_change_info"):
            return await message.reply_text("❌ You do not have 'Change Group Info' permission.")
    # else: verified anonymous, skip permission check

    chat_id = message.chat.id
    current_action = await get_warn_action(chat_id)
    current_limit = await get_warn_limit(chat_id)

    text = f"**⚙️ Warn Action Configuration**\n\nCurrent action: **{current_action.upper()}**\nCurrent limit: **{current_limit}**\nSelect new action:"

    actions = ["ban", "mute", "kick", "tban", "tmute"]
    buttons = []
    row = []
    for i, a in enumerate(actions):
        display = f"✅ {a.upper()}" if a == current_action else a.upper()
        row.append(InlineKeyboardButton(display, callback_data=f"warnmode:{a}"))
        if len(row) == 2 or i == len(actions)-1:
            buttons.append(row)
            row = []

    buttons.append([InlineKeyboardButton(f"🔢 Change Warn Limit ({current_limit})", callback_data="warnlimit:menu")])
    buttons.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])

    keyboard = InlineKeyboardMarkup(buttons)
    await message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    
async def resetallwarns(client: Client, message: Message, verified=False, admin_id=None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await resetallwarns(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "resetallwarns",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # For verified anonymous admin, skip proof and require_admin
    if verified and message.from_user is None:
        # Verified anonymous, no need to require_admin_proof
        pass
    else:
        if not await require_admin_proof(client, message, "resetallwarns"):
            return
        if not await require_admin(client, message):
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # --- OWNER CHECK BEFORE SHOWING BUTTONS ---
    try:
        member = await client.get_chat_member(message.chat.id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await message.reply_text("❌ Only the group owner can reset all warns.")
            return
    except Exception:
        await message.reply_text("❌ Could not verify owner status.")
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "this group"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Reset All warns", callback_data=f"resetall:confirm:{chat_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"resetall:cancel:{chat_id}")]
    ])

    await message.reply_text(
        f"⚠️ **Are you sure?**\n\nThis will reset **all warns** for every member in {chat_title}.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def resetwarns(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await resetwarns(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "resetwarns",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # For verified anonymous admin, skip proof and require_admin
    if verified and message.from_user is None:
        # Verified anonymous, no need for additional checks
        pass
    else:
        if not await require_admin_proof(client, message, "resetwarns"):
            return
        if not await require_admin(client, message):
            return

    # Determine admin user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # Ban permission check – for verified anonymous (admin_id=0) we assume they have permission
    if user_id != 0:
        if not await user_has_permission(client, message.chat.id, user_id, "can_restrict_members"):
            return await message.reply_text("❌ You do not have ban rights (can_restrict_members permission required).")
    # else: verified anonymous, skip permission check (they are admin)

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    chat_id = message.chat.id
    uid = target.id

    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": uid})
    if not doc or not doc.get("warns"):
        await message.reply_text("⚠️ This user has no warns.")
        return

    await warns_col.delete_one({"chat_id": chat_id, "user_id": uid})
    await message.reply_text(f"✅ All warns reset for {target.mention}.")    

async def warninfo_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await warninfo_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message":  message,
                "action": "warninfo",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users (only when not verified)
    if not verified:
        user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
        if not user_id:
            await message.reply_text("Could not identify admin.")
            return
        if not await require_admin(client, message, user_id=user_id):
            return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("Please specify a user (reply, ID, username, or full name).")
        return

    chat_id = message.chat.id
    uid = target.id
    issuer_id = admin_id if admin_id else message.from_user.id

    # Check if target is an admin
    if await is_admin(client, message, uid) and issuer_id != OWNER_ID:
        await message.reply_text("He is an admin.")
        return

    # Get warn count
    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": uid})
    warn_count = len(doc.get("warns", [])) if doc else 0
    limit = int(await get_chat_setting(chat_id, "warn_limit", "3"))

    # Get member status
    try:
        member = await client.get_chat_member(chat_id, uid)
        status = member.status
        if status == enums.ChatMemberStatus.BANNED:
            if member.until_date:
                remaining = member.until_date - int(time.time())
                if remaining > 0:
                    status_text = f"Banned (unban in {remaining//3600}h {remaining%3600//60}m)"
                else:
                    status_text = "Banned"
            else:
                status_text = "Banned"
        elif status == enums.ChatMemberStatus.RESTRICTED:
            perms = member.permissions
            if not perms.can_send_messages:
                if member.until_date:
                    remaining = member.until_date - int(time.time())
                    if remaining > 0:
                        status_text = f"Muted (unmute in {remaining//3600}h {remaining%3600//60}m)"
                    else:
                        status_text = "Muted"
                else:
                    status_text = "Muted"
            else:
                status_text = "Restricted (other permissions)"
        else:
            status_text = "Normal"
    except Exception:
        status_text = "Unknown"

    # Build clean user identifier
    name = f"{target.first_name} {target.last_name or ''}".strip()
    if target.username:
        user_info = f"{name} (@{target.username})"
    else:
        user_info = f"{name} (ID: {uid})"

    text = (
        f"**Warn Info for {user_info}**\n"
        f"• warns: {warn_count}/{limit}\n"
        f"• Status: {status_text}"
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)        

pending_warn_actions = {}

async def warn(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # anonymous admin detection
    if message.from_user is None and message.sender_chat:
        target = await get_target_user(client, message)
        if not target:
            return await message.reply_text("User not found.")

        # Check if already muted
        try:
            member = await client.get_chat_member(message.chat.id, target.id)
            if member.status == enums.ChatMemberStatus.RESTRICTED:
                perms = member.permissions
                if not perms.can_send_messages:
                    return await message.reply_text("🔇 User is already muted.")
        except:
            pass

        # Check if anonymous admin mode is enabled
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified with no admin ID
            await warn_common(client, message, admin_id=None)
            return

        # Otherwise, send proof button (existing code)
        action_id = str(uuid.uuid4())
        pending_warn_actions[action_id] = {
            "chat_id": message.chat.id,
            "message": message,
            "target_id": target.id,
            "time": time.time(),
            "used": False
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🔐 Click To Prove Admin",
                callback_data=f"provewarn:{action_id}"
            )]
        ])
        msg = await message.reply_text(
            "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
            reply_markup=keyboard
        )
        asyncio.create_task(delete_verify_button(msg))
        return

    # normal admin flow
    await warn_common(client, message, silent=False, cmd_type="warn")

async def dwarn(client: Client, message: Message) -> None:
    # 1. Target check
    target = await get_target_user(client, message)
    if not target:
        return

    uid = target.id

    # 2. Admin check
    if await is_admin(client, message, uid):
        return

    # 3. Mute check
    try:
        member = await client.get_chat_member(message.chat.id, uid)
        if member.permissions and not member.permissions.can_send_messages:
            await message.reply_text("⚠️ User is already muted, so no warning issued.")
            if message.reply_to_message:
                await message.reply_to_message.delete()
            await message.delete()
            return
    except Exception:
        pass

    # 4. Anonymous admin detection
    is_anon = message.from_user is None

    if is_anon:
        # Check if anonymous admin mode is enabled
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified
            await warn_common(client, message, delete_cmd=True, silent=False, delete_replied=True, admin_id=None)
            return

        # Otherwise, send proof button (existing code)
        action_id = str(uuid.uuid4())
        pending_warn_actions[action_id] = {
            "chat_id": message.chat.id,
            "target_id": uid,
            "message": message,
            "time": time.time(),
            "used": False,
            "type": "dwarn"
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔐 Click To Prove Admin",
                callback_data=f"provewarn:{action_id}"
            )
        ]])
        return await message.reply_text(
            "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
            reply_markup=keyboard
        )

    # 5. Regular admin logic
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except:
            pass
    try:
        await message.delete()
    except:
        pass
    
    await warn_common(client, message, delete_cmd=True, silent=False, delete_replied=True, cmd_type="dwarn")

async def swarn(client: Client, message: Message) -> None:
    if message.from_user is None and message.sender_chat:
        target = await get_target_user(client, message)
        if not target:
            return await message.reply_text("User not found.")

        # Check if anonymous admin mode is enabled
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified
            await warn_common(client, message, delete_cmd=True, silent=True, delete_replied=False, admin_id=None)
            return

        # Otherwise, send proof button (existing code)
        action_id = str(uuid.uuid4())
        pending_warn_actions[action_id] = {
            "chat_id": message.chat.id,
            "target_id": target.id,
            "message": message,
            "time": time.time(),
            "used": False,
            "type": "swarn"
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔐 Click To Prove Admin",
                callback_data=f"provewarn:{action_id}"
            )
        ]])
        return await message.reply_text(
            "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
            reply_markup=keyboard
        )

    await warn_common(client, message, delete_cmd=True, silent=True, delete_replied=False, cmd_type="swarn")

async def warns(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await warns(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "warns",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
        if not user_id:
            await message.reply_text("Could not identify admin.")
            return
        if not await require_admin(client, message, user_id=user_id):
            return

    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")

    uid = target.id
    chat_id = message.chat.id

    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": uid})
    if not doc or not doc.get("warns"):
        cnt = 0
        warns_list = []
    else:
        warns_list = doc["warns"]
        cnt = len(warns_list)

    limit = await get_warn_limit(chat_id)
    action = await get_warn_action(chat_id)

    name_parts = [target.first_name]
    if target.last_name:
        name_parts.append(target.last_name)
    full_name = " ".join(name_parts)
    if target.username:
        user_string = f"{full_name} (@{target.username})"
    else:
        user_string = f"{full_name} (ID: {uid})"

    text = f"⚠️ **warns for {user_string}**\n\n"
    text += f"**Current warns:** {cnt}/{limit}\n"
    text += f"**Action on reaching limit:** `{action.upper()}`\n\n"

    if warns_list:
        text += "**List of warns:**\n"
        for i, w in enumerate(warns_list, 1):
            reason = w.get("reason", "No reason")
            text += f"{i}. {reason}\n"
    else:
        text += "No warns recorded."

    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)

async def unwarn(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    if not verified:
        if not await require_admin_proof(client, message, "unwarns"):
            return
        if not await require_admin(client, message):
            return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    uid = target.id
    chat_id = message.chat.id

    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": uid})
    if not doc or not doc.get("warns"):
        await message.reply_text("This user has no warns.")
        return

    await warns_col.update_one(
        {"chat_id": chat_id, "user_id": uid},
        {"$pop": {"warns": 1}}
    )

    await message.reply_text(f"✅ Latest warning removed from {target.mention}.")

async def do_restrict(client: Client, message: Message, mode: str) -> None:
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        await message.reply_text("I don't have the right to restrict members. Please give me admin with restrict permission.")
        return
    
    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return
    uid = target.id
    args = get_args(message)
    chat_id = message.chat.id

    if mode in {"mute", "dmute", "smute", "tmute"}:
        until_date = None
        if mode == "tmute" and args:
            secs = parse_duration(args[0])
            if secs > 0:
                until_date = int(time.time()) + secs
        # Convert to datetime if needed
        await client.restrict_chat_member(chat_id, uid, ChatPermissions(can_send_messages=False), until_date=to_datetime(until_date))
    
        if mode == "dmute" and message.reply_to_message:
            with suppress(Exception):
                await message.reply_to_message.delete()
        if mode not in {"smute"}:
            await message.reply_text(f"🔇 {target.mention} muted.")
    else:
        if mode == "dban" and message.reply_to_message:
            with suppress(Exception):
                await message.reply_to_message.delete()
        until_date = None
        if mode == "tban" and args:
            secs = parse_duration(args[0])
            if secs > 0:
                until_date = int(time.time()) + secs
        # Convert to datetime if needed
        await client.ban_chat_member(chat_id, uid, until_date=to_datetime(until_date))
        if mode in {"kick", "dkick", "skick"}:
            await client.unban_chat_member(chat_id, uid)
        if mode not in {"sban", "skick"}:
            action = "kicked" if mode in {"kick", "dkick", "skick"} else "banned"
            await message.reply_text(f"🚫 {target.mention} {action}.")
    if mode in {"dwarn", "dmute", "dkick"}:
        with suppress(Exception):
            await message.delete()

async def mute(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await mute(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "mute",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Bot restrict permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have restrict members permission.")

    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")

    uid = target.id

    # Prevent muting admins
    if await is_admin(client, message, uid):
        return await message.reply_text("❌ It is not possible to mute an admin.")

    member = await client.get_chat_member(message.chat.id, uid)

    # Already muted check
    if member.permissions and not member.permissions.can_send_messages:
        return await message.reply_text("⚠️ User is already muted.")

    await client.restrict_chat_member(
        message.chat.id,
        uid,
        ChatPermissions(can_send_messages=False)
    )

    await send_log(client, message.chat.id, "admin", "Mute", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    
    await message.reply_text(f"🔇 {target.mention} muted.")

async def dmute(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified with placeholder admin ID 0
            return await dmute(client, message, verified=True, admin_id=0)
        else:
            # Send proof button
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "dmute",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Bot restrict permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have restrict members permission.")

    target = await get_target_user(client, message)
    if not target:
        return

    uid = target.id

    # Prevent muting admins
    if await is_admin(client, message, uid):
        return await message.reply_text("❌ It is not possible to mute an admin.")

    member = await client.get_chat_member(message.chat.id, uid)

    # Already muted check
    if member.permissions and not member.permissions.can_send_messages:
        await message.reply_text("⚠️ User is already muted.")
    else:
        await client.restrict_chat_member(
            message.chat.id,
            uid,
            ChatPermissions(can_send_messages=False)
        )
        # Send a temporary confirmation message
        conf_msg = await message.reply_text(f"🔇 {target.mention} muted.")
        # Delete confirmation after 3 seconds
        asyncio.create_task(delete_after_delay(conf_msg, 3))

    # Delete the replied message (spam)
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except Exception:
            pass

    # Delete the command message
    try:
        await message.delete()
    except Exception:
        pass

async def smute(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified with placeholder admin ID 0
            return await smute(client, message, verified=True, admin_id=0)
        else:
            # Send proof button
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "smute",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Bot restrict permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have restrict members permission.")

    target = await get_target_user(client, message)
    if not target:
        return

    uid = target.id

    # Prevent muting admins
    if await is_admin(client, message, uid):
        return await message.reply_text("❌ It is not possible to mute an admin.")

    member = await client.get_chat_member(message.chat.id, uid)

    # Already muted check
    if member.permissions and not member.permissions.can_send_messages:
        return await message.reply_text("⚠️ User is already muted.")

    # Mute silently (no reply)
    await client.restrict_chat_member(
        message.chat.id,
        uid,
        ChatPermissions(can_send_messages=False)
    )    
    await send_log(client, message.chat.id, "admin", "Mute (silent)", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")

async def tmute(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await tmute(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "tmute",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Bot restrict permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have restrict members permission.")

    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /tmute 10m")

    duration = parse_duration(args[0])
    if duration == 0:
        return await message.reply_text("Invalid duration format. Use: 1h, 30m, 2d, etc.")

    target = await get_target_user(client, message)
    if not target:
        return

    uid = target.id

    # Prevent muting admins
    if await is_admin(client, message, uid):
        return await message.reply_text("❌ It is not possible to mute an admin.")

    # Get member to check if already muted
    member = await client.get_chat_member(message.chat.id, uid)

    # Already muted check
    if member.permissions and not member.permissions.can_send_messages:
        return await message.reply_text("⚠️ User is already muted.")

    until = int(time.time()) + duration
    await client.restrict_chat_member(
        message.chat.id,
        uid,
        ChatPermissions(can_send_messages=False),
        until_date=to_datetime(until)
    )

    duration_str = args[0] if args else "0"
    await send_log(client, message.chat.id, "admin", f"Temporary Mute ({duration_str})", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    
    await message.reply_text(f"🔇 {target.mention} muted for {args[0]}.")

async def unmute(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unmute(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unmute",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        await message.reply_text(
            "I don't have the right to unrestrict members. Please give me admin with restrict permission."
        )
        return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    chat_id = message.chat.id
    uid = target.id

    # Admin protection
    if await is_admin(client, message, uid):
        await message.reply_text("❌ It is not possible to unmute an admin.")
        return

    try:
        member = await client.get_chat_member(chat_id, uid)

        if member.status != enums.ChatMemberStatus.RESTRICTED:
            await message.reply_text("❌ User is not muted.")
            return

        perms = member.permissions
        if perms and perms.can_send_messages:
            await message.reply_text("❌ User is not muted.")
            return

        await client.restrict_chat_member(
            chat_id,
            uid,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )

        await send_log(client, message.chat.id, "admin", "Unmute", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
        
        await message.reply_text(f"🔊 {target.mention} unmuted.")

    except Exception as e:
        await message.reply_text(f"Error: {e}")

# --- Global Ban/Unban commands (owner/sudo only) ---
async def gban_cmd(client: Client, message: Message):
    """Ban a user from any group (owner/sudo only)."""
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")

    args = get_args(message)
    if len(args) < 2:
        return await message.reply_text("Usage: `/gban <group_id> <user_id> [reason]`")

    try:
        group_id = int(args[0])
        user_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Group ID and User ID must be integers.")

    reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"

    # Check if bot is admin in the target group
    try:
        bot_member = await client.get_chat_member(group_id, (await client.get_me()).id)
        if bot_member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            return await message.reply_text(f"❌ I am not an admin in group `{group_id}`.")
        if not bot_member.privileges or not bot_member.privileges.can_restrict_members:
            return await message.reply_text(f"❌ I do not have 'ban members' permission in group `{group_id}`.")
    except Exception as e:
        return await message.reply_text(f"❌ Failed to verify bot status in group `{group_id}`: {e}")

    # Perform ban
    try:
        await client.ban_chat_member(group_id, user_id)
        # Optionally, get user info
        user = await client.get_users(user_id)
        group = await client.get_chat(group_id)
        await message.reply_text(
            f"✅ **Global Ban**\n"
            f"👤 User: {user.mention} (`{user.id}`)\n"
            f"🏠 Group: {group.title} (`{group.id}`)\n"
            f"📝 Reason: {reason}\n"
            f"🔨 Action: Banned"
        )
    except Exception as e:
        await message.reply_text(f"❌ Failed to ban user: {e}")

async def gunban_cmd(client: Client, message: Message):
    """Unban a user from any group (owner/sudo only)."""
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")

    args = get_args(message)
    if len(args) < 2:
        return await message.reply_text("Usage: `/gunban <group_id> <user_id>`")

    try:
        group_id = int(args[0])
        user_id = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Group ID and User ID must be integers.")

    # Check if bot is admin in the target group
    try:
        bot_member = await client.get_chat_member(group_id, (await client.get_me()).id)
        if bot_member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            return await message.reply_text(f"❌ I am not an admin in group `{group_id}`.")
        if not bot_member.privileges or not bot_member.privileges.can_restrict_members:
            return await message.reply_text(f"❌ I do not have 'ban members' permission in group `{group_id}`.")
    except Exception as e:
        return await message.reply_text(f"❌ Failed to verify bot status in group `{group_id}`: {e}")

    # Perform unban
    try:
        await client.unban_chat_member(group_id, user_id)
        user = await client.get_users(user_id)
        group = await client.get_chat(group_id)
        await message.reply_text(
            f"✅ **Global Unban**\n"
            f"👤 User: {user.mention} (`{user.id}`)\n"
            f"🏠 Group: {group.title} (`{group.id}`)\n"
            f"🔓 Action: Unbanned"
        )
    except Exception as e:
        await message.reply_text(f"❌ Failed to unban user: {e}")

async def ban(client: Client, message: Message, verified=False, admin_id: int = None):
    
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await ban(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "ban",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        return await message.reply_text("Reply to a user.")

    status = await check_target_status(client, message.chat.id, user.id, "ban")
    if status == "admin":
        return await message.reply_text("I can't ban admins.")
    if status == "banned":
        return await message.reply_text("User already banned.")

    await client.ban_chat_member(message.chat.id, user.id)
    # बैन करने के बाद
    await send_log(client, message.chat.id, "admin", "Ban", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous", reason=" ".join(args))
    await message.reply_text(f"{user.mention} banned.")

async def dban(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await dban(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "dban",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Check bot has restrict permission
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I need ban permission.")
    # Check bot has delete permission
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        return await message.reply_text("❌ I need delete permission to remove the message.")

    user = await get_target_user(client, message)
    if not user:
        await message.reply_text("User not found.")
        return

    chat_id = message.chat.id
    user_id = user.id

    # Delete the replied message
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except Exception as e:
            await message.reply_text(f"Could not delete replied message: {e}")

    status = await check_target_status(client, chat_id, user_id, "dban")
    if status == "admin":
        await message.reply_text("❌ I can't ban an admin.")
        return
    if status == "already_banned":
        await message.reply_text("⚠️ User is already banned.")
        return

    try:
        await client.ban_chat_member(chat_id, user_id)
        await send_log(client, chat_id, "admin", "Ban", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
        await message.reply_text(f"✅ {user.mention} banned.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to ban: {e}")

async def sban(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await sban(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "sban",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        await message.reply_text("User not found.")
        return

    status = await check_target_status(client, message.chat.id, user.id, "sban")
    if status != "ok":
        return  # silently ignore

    await client.ban_chat_member(message.chat.id, user.id)
    await send_log(client, message.chat.id, "admin", "Ban (silent)", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    
async def tban(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await tban(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "tban",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        return

    # --- ADD ADMIN CHECK ---
    status = await check_target_status(client, message.chat.id, user.id, "tban")
    if status == "admin":
        return await message.reply_text("I can't ban an admin.")
    if status == "already_banned":
        return await message.reply_text("User already banned.")
    # --- END ADDITION ---

    # Parse duration from args (e.g., /tban 2h)
    args = get_args(message)
    duration = 3600  # default 1 hour
    if args:
        secs = parse_duration(args[0])
        if secs > 0:
            duration = secs

    until = int(time.time()) + duration
    await client.ban_chat_member(message.chat.id, user.id, until_date=to_datetime(until))
    duration_str = args[0] if args else "1h"
    await send_log(client, message.chat.id, "admin", f"Temporary Ban ({duration_str})", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    await message.reply_text(f"{user.mention} banned for {duration//3600} hour(s).")
    

async def unban(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unban(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unban",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        return await message.reply_text("Reply to a user.")

    try:
        member = await client.get_chat_member(message.chat.id, user.id)
        if member.status != enums.ChatMemberStatus.BANNED:
            return await message.reply_text("User is already unbanned.")
    except Exception:
        return await message.reply_text("User is already unbanned.")

    await client.unban_chat_member(message.chat.id, user.id)
    await send_log(client, message.chat.id, "admin", "Unban", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    await message.reply_text(f"{user.mention} unbanned.")

async def kick(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await kick(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "kick",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        await message.reply_text("User not found.")
        return

    status = await check_target_status(client, message.chat.id, user.id, "kick")
    if status == "admin":
        return await message.reply_text("Can't kick admins.")

    await client.ban_chat_member(message.chat.id, user.id)
    await client.unban_chat_member(message.chat.id, user.id)
    await send_log(client, message.chat.id, "admin", "Kick", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    await message.reply_text(f"{user.mention} kicked.")

async def skick(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await skick(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "skick",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        await message.reply_text("User not found.")
        return

    await client.ban_chat_member(message.chat.id, user.id)
    await send_log(client, message.chat.id, "admin", "Kick (silent)", user.mention, user.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    await client.unban_chat_member(message.chat.id, user.id)

async def dkick(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await dkick(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "dkick",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await check_ban_permissions(client, message):
        return

    user = await get_target_user(client, message)
    if not user:
        await message.reply_text("User not found.")
        return

    chat_id = message.chat.id
    user_id = user.id

    # Delete the replied message
    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except Exception as e:
            await message.reply_text(f"Could not delete replied message: {e}")

    try:
        await client.ban_chat_member(chat_id, user_id)
        await client.unban_chat_member(chat_id, user_id)
        await send_log(client, chat_id, "admin", "Kick (delete)", user.mention, user_id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
    except Exception as e:
        await message.reply_text(f"❌ Failed to kick: {e}")
        return

    # Delete the command message
    try:
        await message.delete()
    except Exception:
        pass    


async def kickme(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")
    
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        await message.reply_text("I don't have the right to kick members. Please give me admin with restrict permission.")
        return
    uid = message.from_user.id
    await client.ban_chat_member(message.chat.id, uid)
    await client.unban_chat_member(message.chat.id, uid)
    await message.reply_text("You have left the group.")

async def lock(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await lock(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "lock",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check – only for non‑verified
    if not verified:
        if not await require_admin(client, message):
            return

    # Permission check for normal users (if from_user exists)
    if message.from_user:
        if not await user_has_permission(
            client,
            message.chat.id,
            message.from_user.id,
            "can_change_info"
        ):
            await message.reply_text(
                "❌ You need 'Change Group Info' permission to manage locks."
            )
            return

    # Bot permission check
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text(
            "❌ I don't have the right to delete messages. "
            "Please give me admin with delete permission to use locks."
        )
        return

    args = get_args(message)
    if not args:
        await message.reply_text(
            "Usage: /lock media|sticker|gif|voice|poll|link|emoji|text|all"
        )
        return

    lock_type = args[0].lower()

    # Handle "all" by delegating to lockall
    if lock_type == "all":
        # Determine admin ID: use passed admin_id, or fallback to message.from_user.id
        admin_id_to_use = admin_id if admin_id is not None else (message.from_user.id if message.from_user else None)
        await lockall(client, message, verified=verified, admin_id=admin_id_to_use)
        return

    if lock_type not in LOCK_TYPES:
        await message.reply_text(
            "Usage: /lock media|sticker|gif|voice|poll|link|emoji|text|all"
        )
        return

    current = await get_chat_setting(message.chat.id, f"lock_{lock_type}", "0")
    if current == "1":
        await message.reply_text(f"ℹ️ `{lock_type}` is already locked.")
        return

    await set_chat_setting(message.chat.id, f"lock_{lock_type}", "1")
    await message.reply_text(f"🔒 `{lock_type}` locked.")

async def unlock(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unlock(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unlock",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check
    if not verified:
        if not await require_admin(client, message):
            return

    # Permission check for normal users
    if message.from_user:
        if not await user_has_permission(
            client,
            message.chat.id,
            message.from_user.id,
            "can_change_info"
        ):
            await message.reply_text(
                "❌ You need 'Change Group Info' permission to manage locks."
            )
            return

    # Bot permission check
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text(
            "❌ I don't have the right to delete messages. "
            "Please give me admin with delete permission to use locks."
        )
        return

    args = get_args(message)
    if not args:
        await message.reply_text(
            "Usage: /unlock media|sticker|gif|voice|poll|link|emoji|text|all"
        )
        return

    lock_type = args[0].lower()

    # Handle "all" by delegating to unlockall
    if lock_type == "all":
        admin_id_to_use = admin_id if admin_id is not None else (message.from_user.id if message.from_user else None)
        await unlockall(client, message, verified=verified, admin_id=admin_id_to_use)
        return

    if lock_type not in LOCK_TYPES:
        await message.reply_text(
            "Usage: /unlock media|sticker|gif|voice|poll|link|emoji|text|all"
        )
        return

    current = await get_chat_setting(message.chat.id, f"lock_{lock_type}", "0")
    if current == "0":
        await message.reply_text(f"ℹ️ `{lock_type}` is already unlocked.")
        return

    await set_chat_setting(message.chat.id, f"lock_{lock_type}", "0")
    await message.reply_text(f"🔓 `{lock_type}` unlocked.")    

async def lockall(client: Client, message: Message, verified=False, admin_id=None):

    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin proof
    if not verified:
        if not await require_admin_proof(client, message, "lockall"):
            return

    # Admin check
    if not verified:
        if not await require_admin(client, message):
            return

    # Bot permission check
    if not await check_ban_permissions(client, message):
        return

    try:
        await client.set_chat_permissions(
            message.chat.id,
            ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_send_polls=False
            )
        )
        await message.reply_text("🔒 Group Locked Successfully!")

    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def unlockall(client: Client, message: Message, verified=False, admin_id=None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin proof
    if not verified:
        if not await require_admin_proof(client, message, "unlockall"):
            return

    # Admin check
    if not verified:
        if not await require_admin(client, message):
            return

    # Bot permission check
    if not await check_ban_permissions(client, message):
        return

    try:
        await client.set_chat_permissions(
            message.chat.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True
            )
        )
        await message.reply_text("🔓 Group Unlocked Successfully!")

    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def track_toggle(client: Client, message: Message):

    # 🔒 only owner
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("❌ Only bot owner can use this.")

    if len(message.command) < 2:
        return await message.reply_text("Usage: /track on or /track off")

    state = message.command[1].lower()

    if state == "on":
        await set_chat_setting(message.chat.id, "tracker_enabled", "1")
        await message.reply_text("✅ Tracker Enabled")
    elif state == "off":
        await set_chat_setting(message.chat.id, "tracker_enabled", "0")
        await message.reply_text("❌ Tracker Disabled")
    else:
        await message.reply_text("Use: on / off")


async def message_tracker(client: Client, message: Message):

    if not message.from_user:
        return

    chat_id = message.chat.id
    user = message.from_user

    enabled = await get_chat_setting(chat_id, "tracker_enabled", "0")
    if enabled != "1":
        return

    new_name = f"{user.first_name} {user.last_name or ''}".strip()
    new_username = user.username or "None"

    old = await user_profile_col.find_one({
        "user_id": user.id,
        "chat_id": chat_id
    })

    changes = []

    if old:
        if old.get("name") != new_name:
            changes.append(f"👤 Name changed\n• Old: {old.get('name')}\n• New: {new_name}")

        if old.get("username") != new_username:
            changes.append(f"🔗 Username changed\n• Old: @{old.get('username')}\n• New: @{new_username}")

    # update db
    await user_profile_col.update_one(
        {"user_id": user.id, "chat_id": chat_id},
        {"$set": {"name": new_name, "username": new_username}},
        upsert=True
    )

    if changes:
        await message.reply_text(
            f"🕵️ Profile Updated\n\n{user.mention}\n\n" + "\n\n".join(changes)
        )

async def leave_tracker(client: Client, message: Message):

    user = message.left_chat_member
    chat_id = message.chat.id

    enabled = await get_chat_setting(chat_id, "tracker_enabled", "0")
    if enabled != "1":
        return

    new_name = f"{user.first_name} {user.last_name or ''}".strip()
    new_username = user.username or "None"

    old = await user_profile_col.find_one({
        "user_id": user.id,
        "chat_id": chat_id
    })

    changes = []

    if old:
        if old.get("name") != new_name:
            changes.append(f"👤 Name changed before leaving\n• Old: {old.get('name')}\n• New: {new_name}")

        if old.get("username") != new_username:
            changes.append(f"🔗 Username changed before leaving\n• Old: @{old.get('username')}\n• New: @{new_username}")

    # update db
    await user_profile_col.update_one(
        {"user_id": user.id, "chat_id": chat_id},
        {"$set": {"name": new_name, "username": new_username}},
        upsert=True
    )

    if changes:
        await message.reply_text(
            f"🚪 User Left & Profile Changed\n\n{user.mention}\n\n" + "\n\n".join(changes)
        )

async def join_tracker(client: Client, message: Message):

    chat_id = message.chat.id

    enabled = await get_chat_setting(chat_id, "tracker_enabled", "0")
    if enabled != "1":
        return

    for user in message.new_chat_members:

        new_name = f"{user.first_name} {user.last_name or ''}".strip()
        new_username = user.username or "None"

        old = await user_profile_col.find_one({
            "user_id": user.id,
            "chat_id": chat_id
        })

        changes = []

        if old:
            if old.get("name") != new_name:
                changes.append(
                    f"👤 Name changed\n• Old: {old.get('name')}\n• New: {new_name}"
                )

            if old.get("username") != new_username:
                changes.append(
                    f"🔗 Username changed\n• Old: @{old.get('username')}\n• New: @{new_username}"
                )

        # update DB
        await user_profile_col.update_one(
            {"user_id": user.id, "chat_id": chat_id},
            {"$set": {
                "name": new_name,
                "username": new_username
            }},
            upsert=True
        )

        if changes:
            await message.reply_text(
                f"🟢 User Joined & Profile Updated\n\n{user.mention}\n\n"
                + "\n\n".join(changes)
            )

async def cleanservice(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "clean_service", "cleanservice", verified, admin_id)

async def captcha_toggle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "captcha_enabled", "captcha", verified, admin_id)

async def approve(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin here.")

    # Bot ban permission check (optional)
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have ban permission.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await approve(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "approve",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified (normal) admins
    if not verified:
        if not await require_admin(client, message):
            return

    # Additional permission check for normal users
    if message.from_user:
        if not await user_has_permission(
            client,
            message.chat.id,
            message.from_user.id,
            "can_change_info"
        ):
            await message.reply_text(
                "❌ You need 'Change Group Info' permission to approve users."
            )
            return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    # PREVENT APPROVING AN ADMIN
    if await is_admin(client, message, target.id):
        await message.reply_text(
            "❌ Cannot approve an admin. Admins are already exempt from locks and antiflood."
        )
        return

    # CHECK IF ALREADY APPROVED
    if await is_approved(message.chat.id, target.id):
        await message.reply_text(
            f"ℹ️ {target.mention} is already approved.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    await approved_col.update_one(
        {"chat_id": message.chat.id, "user_id": target.id},
        {"$set": {"user_id": target.id}},
        upsert=True
    )
    await message.reply_text(f"✅ {target.mention} approved.")


async def unapprove(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin here.")

    # Bot ban permission check
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text("❌ I don't have ban permission.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unapprove(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unapprove",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified (normal) admins
    if not verified:
        if not await require_admin(client, message):
            return

    # Permission check for normal users
    if message.from_user:
        if not await user_has_permission(
            client,
            message.chat.id,
            message.from_user.id,
            "can_change_info"
        ):
            await message.reply_text(
                "❌ You need 'Change Group Info' permission to unapprove users."
            )
            return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    # PREVENT UNAPPROVING AN ADMIN
    if await is_admin(client, message, target.id):
        await message.reply_text(
            "❌ Cannot unapprove an admin. Admins are always exempt from locks and antiflood."
        )
        return

    # CHECK IF NOT APPROVED
    if not await is_approved(message.chat.id, target.id):
        await message.reply_text(
            f"ℹ️ {target.mention} is not approved.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    await approved_col.delete_one({"chat_id": message.chat.id, "user_id": target.id})
    await message.reply_text(f"❌ {target.mention} unapproved.")


async def unapproveall(client: Client, message: Message, verified=False, admin_id=None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unapproveall(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unapproveall",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # For verified anonymous admin, skip proof and require_admin
    if verified and message.from_user is None:
        pass
    else:
        if not await require_admin_proof(client, message, "unapproveall"):
            return
        if not await require_admin(client, message):
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # --- OWNER CHECK BEFORE SHOWING BUTTONS ---
    try:
        member = await client.get_chat_member(message.chat.id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await message.reply_text("❌ Only the group owner can remove all approved users.")
            return
    except Exception:
        await message.reply_text("❌ Could not verify owner status.")
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "this group"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ Unapprove All", callback_data=f"unapproveall:confirm:{chat_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"unapproveall:cancel:{chat_id}")]
    ])

    await message.reply_text(
        f"⚠️ **Are you sure?**\n\nThis will remove all approved users from {chat_title}.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def approved(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await approved(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "approved",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    
    # Admin check - normal admin ke liye
    if not verified:
        if not await require_admin(client, message):
            return

    # Approved users ki list dikhao
    cursor = approved_col.find({"chat_id": message.chat.id})
    users = await cursor.to_list(length=1000)
    if users:
        text = "Approved users:\n"
        for u in users:
            user_id = u["user_id"]
            try:
                user = await client.get_users(user_id)
                text += f"• {user.mention} (<code>{user_id}</code>)\n"
            except:
                text += f"• <code>{user_id}</code>\n"
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("No approved users.")

async def approval(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    """Check if a user is approved in this chat."""
    chat_id = message.chat.id
    target = await get_target_user(client, message)

    # If no target specified, use the command sender
    if not target:
        # Check if sender is anonymous admin (channel)
        if message.sender_chat:
            # For anonymous admin, we can't get a user object, so show channel info
            await message.reply_text(
                f"ℹ️ This is a channel/admin account.\nChat ID: <code>{message.sender_chat.id}</code>\nStatus: Not applicable.",
                parse_mode=enums.ParseMode.HTML
            )
            return
        else:
            target = message.from_user

    if not target:
        await message.reply_text("Could not identify user.")
        return

    # Check if target is an admin in this chat
    if await is_admin(client, message, target.id):
        await message.reply_text(
            f"👑 {target.mention} is an admin, hence approved by default.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    # Check database for approval
    is_approved_db = await is_approved(chat_id, target.id)
    status = "✅ Approved" if is_approved_db else "❌ Not approved"
    await message.reply_text(
        f"👤 {target.mention}\nStatus: {status}",
        parse_mode=enums.ParseMode.HTML
    )          

async def adminlist(client: Client, message: Message) -> None:

    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    chat_id = message.chat.id
    chat_title = message.chat.title

    text = await get_adminlist_text(client, chat_id, chat_title)

    await message.reply_text(
        text,
        parse_mode=enums.ParseMode.HTML
    )

async def promote(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await promote(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "promote",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Determine the user ID for permission checks
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        # Check if user has permission to promote
        if not await user_has_permission(client, message.chat.id, user_id, "can_promote_members"):
            await message.reply_text("❌ You don't have permission to add admins.")
            return

    # Check if bot has permission to promote
    if not await bot_has_permission(client, message.chat.id, "can_promote_members"):
        await message.reply_text("I don't have the right to promote members. Please give me admin with 'Add Admins' permission.")
        return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    args = get_args(message)
    title = " ".join(args[1:]) if len(args) > 1 else None

    # --- NEW: Get bot's own privileges to copy ---
    bot_me = await client.get_me()
    bot_member = await client.get_chat_member(message.chat.id, bot_me.id)
    if bot_member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        await message.reply_text("❌ I am not admin here.")
        return
    if not bot_member.privileges:
        await message.reply_text("❌ I don't have any admin privileges to copy.")
        return

    # Copy all privileges except can_promote_members
    import inspect
    from pyrogram.types import ChatPrivileges
    privs = bot_member.privileges
    sig = inspect.signature(ChatPrivileges)
    valid_params = set(sig.parameters.keys())
    valid_params.discard('self')
    
    # Build dictionary with known fields
    priv_dict = {
        'can_manage_chat': privs.can_manage_chat,
        'can_delete_messages': privs.can_delete_messages,
        'can_restrict_members': privs.can_restrict_members,
        'can_invite_users': privs.can_invite_users,
        'can_pin_messages': privs.can_pin_messages,
        'can_promote_members': False,
    }
    # Add optional fields if they exist in both bot's privileges and are accepted by ChatPrivileges
    for attr in ['can_manage_video_chats', 'can_manage_stories']:
        if attr in valid_params and hasattr(privs, attr):
            priv_dict[attr] = getattr(privs, attr, False)
    
    new_privs = ChatPrivileges(**priv_dict)

    try:
        await client.promote_chat_member(
            message.chat.id, target.id,
            privileges=new_privs
        )
        if title:
            try:
                await client.set_administrator_title(message.chat.id, target.id, title)
            except:
                pass
        await message.reply_text(f"Promoted! {title}")
    except Exception as e:
        await message.reply_text(f"Failed to promote: {e}")
        extra = f"Title: {title}" if title else None
        await send_log(client, message.chat.id, "admin", "Promote", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous", extra=extra)
        
async def demote(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await demote(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "demote",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Determine the user ID for permission checks
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        # Check if user has permission to demote (needs can_promote_members)
        if not await user_has_permission(client, message.chat.id, user_id, "can_promote_members"):
            await message.reply_text("❌ You don't have permission to remove admins.")
            return

    # Check if bot has permission to demote
    if not await bot_has_permission(client, message.chat.id, "can_promote_members"):
        await message.reply_text("I don't have the right to demote members. Please give me admin with 'Add Admins' permission.")
        return

    target = await get_target_user(client, message)
    if not target:
        await message.reply_text("User not found.")
        return

    try:
        await client.promote_chat_member(
            message.chat.id, target.id,
            privileges=ChatPrivileges(
                can_manage_chat=False,
                can_delete_messages=False,
                can_restrict_members=False,
                can_invite_users=False,
                can_pin_messages=False
            )
        )
        await message.reply_text(f"Demoted.")
    except Exception as e:
        await message.reply_text(f"Failed to demote: {e}")
        await send_log(client, message.chat.id, "admin", "Demote", target.mention, target.id, admin_mention=message.from_user.mention if message.from_user else "Anonymous")
        
async def setgpic(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setgpic(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setgpic",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    reply = message.reply_to_message
    if not reply or not reply.photo:
        await message.reply_text("Reply to a photo with /setgpic")
        return
    bio = await client.download_media(reply, in_memory=True)
    await client.set_chat_photo(message.chat.id, photo=bio)
    await message.reply_text("Group photo updated.")


async def delgpic(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await delgpic(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "delgpic",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    try:
        await client.delete_chat_photo(message.chat.id)
        await message.reply_text("Group photo deleted.")
    except Exception as e:
        await message.reply_text(f"Failed to delete photo: {e}")

async def setgdesc(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setgdesc(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setgdesc",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # --- Determine description text ---
    desc_text = None
    if message.reply_to_message:
        # If replying, take text from the replied message (or caption)
        desc_text = message.reply_to_message.text or message.reply_to_message.caption
        if not desc_text:
            await message.reply_text("❌ Replied message has no text or caption.")
            return
    else:
        args = get_args(message)
        if not args:
            await message.reply_text("Usage: /setgdesc <text> or reply to a message.")
            return
        desc_text = " ".join(args)

    # Set the description
    try:
        await client.set_chat_description(message.chat.id, desc_text)
        await message.reply_text("✅ Group description updated.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to update description: {e}")
        
async def delgdesc(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await delgdesc(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "delgdesc",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    try:
        await client.set_chat_description(message.chat.id, "")
        await message.reply_text("Group description cleared.")
    except Exception as e:
        await message.reply_text(f"Failed to clear description: {e}")

async def setgtitle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setgtitle(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setgtitle",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /setgtitle <title>")
        return
    new_title = " ".join(args)

    # Get current title before changing
    current_title = message.chat.title

    try:
        await client.set_chat_title(message.chat.id, new_title)
        # Store the previous title
        await set_chat_setting(message.chat.id, "previous_title", current_title)
        await message.reply_text(f"Group title updated to: {new_title}")
    except Exception as e:
        await message.reply_text(f"Failed to update title: {e}")

async def resetgtitle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await resetgtitle(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "resetgtitle",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Get stored previous title
    previous = await get_chat_setting(message.chat.id, "previous_title", "")
    if not previous:
        await message.reply_text("No previous title found.")
        return

    try:
        await client.set_chat_title(message.chat.id, previous)
        await message.reply_text(f"Group title reset to: {previous}")
    except Exception as e:
        await message.reply_text(f"Failed to reset title: {e}") 

async def admincache(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await admincache(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "admincache",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        if not await require_admin(client, message):
            return

    chat_id = message.chat.id
    chat_title = message.chat.title or "this chat"
    now = int(time.time())
    last = await get_last_admincache(chat_id)

    # Cooldown check: 10 minutes = 600 seconds
    if now - last < 600:
        remaining = 600 - (now - last)
        minutes = remaining // 60
        seconds = remaining % 60
        await message.reply_text(
            f"You can only refresh the admin cache in {chat_title} once every 10 minutes. "
            f"Please wait {minutes} minute(s) {seconds} second(s)."
        )
        return

    admins = []
    async for a in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        admins.append(str(a.user.id))
    cache = ",".join(admins)
    await set_chat_setting(chat_id, "admin_cache", cache)
    await set_last_admincache(chat_id, now)
    await message.reply_text(f"Admin cache refreshed: {len(admins)} admins")

async def flood_cmd(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection (unchanged)
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await flood_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "flood",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return
    
    chat_id = message.chat.id
    enabled = await is_flood_enabled(chat_id)
    mode, params = await get_flood_mode(chat_id)

    if enabled:
        if mode == "timer":
            status_text = (
                f"🟢 Flood control is **enabled** in {message.chat.title} (Timer Mode)\n"
                f"Users sending **{params['count']}** or more messages within **{params['seconds']}** seconds will be punished.\n"
                f"Current action: {await get_chat_setting(chat_id, 'flood_mode', 'mute')}"
            )
        else:
            status_text = (
                f"🟢 Flood control is **enabled** in {message.chat.title} (Consecutive Mode)\n"
                f"Users sending **{params['limit']}** or more consecutive messages within **{params['window']}** seconds will be punished.\n"
                f"Current action: {await get_chat_setting(chat_id, 'flood_mode', 'mute')}"
            )
    else:
        status_text = (
            f"🔴 Flood control is **disabled** in {message.chat.title}.\n"
            "This chat is not currently enforcing flood control."
        )

    keyboard = get_flood_keyboard(enabled)
    await message.reply_text(status_text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def setfloodtimer_cmd(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setfloodtimer_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setfloodtimer",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # --- NEW: Bot must be admin and have delete messages permission ---
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        return await message.reply_text(f"❌ I don't have message deleting rights in {message.chat.title}.")

    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /setfloodtimer <count> <seconds>   or   /setfloodtimer off")
        return

    if args[0].lower() == "off":
        await set_chat_setting(message.chat.id, "flood_timer_enabled", "0")
        await message.reply_text("⏱️ Timed antiflood has been disabled. The chat will now use the default consecutive flood limit.")
        return

    if len(args) < 2:
        await message.reply_text("Usage: /setfloodtimer <count> <seconds>")
        return

    try:
        count = int(args[0])
        seconds = int(args[1])
        if count < 1 or seconds < 1:
            raise ValueError
    except ValueError:
        await message.reply_text("Both count and seconds must be positive integers.")
        return

    # Save timer settings
    await set_chat_setting(message.chat.id, "flood_timer_enabled", "1")
    await set_chat_setting(message.chat.id, "flood_timer_count", str(count))
    await set_chat_setting(message.chat.id, "flood_timer_seconds", str(seconds))

    # Clear the consecutive flood settings to avoid confusion (optional)
    await set_chat_setting(message.chat.id, "flood_enabled", "0")  # disable old mode

    await message.reply_text(
        f"✅ Timed antiflood setting for {message.chat.title} has been updated to **{count}** messages every **{seconds}** seconds.\n"
        f"Users exceeding this will be punished according to the configured flood mode."
    )

async def setfloodmode_cmd(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await setfloodmode_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setfloodmode",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # For normal users or verified anonymous, require admin
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_has_permission(client, message.chat.id, "can_restrict_members"):
        return await message.reply_text(f"❌ I don't have member restricting rights in {message.chat.title}.")

    # If arguments are provided, use the old text mode
    args = get_args(message)
    if args:
        allowed = {"mute", "ban", "kick", "tban", "tmute"}
        action = args[0].lower()
        if action not in allowed:
            await message.reply_text("Usage: /setfloodmode <mute|ban|kick|tban|tmute> [duration]")
            return
        if action in ("tban", "tmute"):
            if len(args) < 2:
                await message.reply_text("Please provide a duration for temporary action (e.g., 1h, 30m, 2d)")
                return
            duration = parse_duration(args[1])
            if duration == 0:
                await message.reply_text("Invalid duration format. Use: 1h, 30m, 2d, etc.")
                return
            await set_chat_setting(message.chat.id, "flood_mode", action)
            await set_chat_setting(message.chat.id, "flood_duration", str(duration))
            await message.reply_text(f"Flood mode set to {action} with duration {args[1]}")
        else:
            await set_chat_setting(message.chat.id, "flood_mode", action)
            await message.reply_text(f"Flood mode set to {action}")
        return

    chat_id = message.chat.id
    current_action = await get_chat_setting(chat_id, "flood_mode", "mute")
    current_duration_sec = int(await get_chat_setting(chat_id, "flood_duration", "60"))
    current_duration_min = current_duration_sec // 60   # show minutes

    actions = [
        ("mute", "🔇 Mute"),
        ("ban", "🚫 Ban"),
        ("kick", "👢 Kick"),
        ("tban", "⏱️ Tban"),
        ("tmute", "⏱️ Tmute")
    ]
    keyboard = []
    row = []
    for a, label in actions:
        display = f"✅ {label}" if a == current_action else label
        row.append(InlineKeyboardButton(display, callback_data=f"setfloodmode_action:{a}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
    await message.reply_text(
        f"**Current flood mode:** {current_action.upper()}\n"
        f"**Duration (for tban/tmute):** {current_duration_min} minute(s)\n\n"
        f"Select new flood action:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def clearflood(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await clearflood(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "clearflood",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Bot delete permission check
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        return await message.reply_text(f"❌ I don't have message deleting rights in {message.chat.title}.")

    chat_id = message.chat.id
    enabled = await get_chat_setting(chat_id, "clear_flood_enabled", "1") == "1"   # default ON

    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        f"Antiflood clearing is currently **{status}** in {message.chat.title}. "
        "When someone floods, all their flood messages will be automatically deleted."
        if enabled else
        f"Antiflood clearing is currently **{status}** in {message.chat.title}. "
        "When someone floods, their flood messages will NOT be automatically deleted."
    )

    keyboard = get_clearflood_keyboard(enabled)
    await message.reply_text(description, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)


# ----- Update id_cmd -----
async def id_cmd(client: Client, message: Message) -> None:
    tid = message.chat.id
    user = None

    # 1. First, check if the command is a reply to another message
    if message.reply_to_message:
        if message.reply_to_message.sender_chat:
            txt = f"Chat ID: <code>{message.reply_to_message.sender_chat.id}</code>"
            return await message.reply_text(txt, parse_mode=enums.ParseMode.HTML)
        else:
            user = message.reply_to_message.from_user

    # 2. Second, check if a username, ID, or full name was passed as an argument
    elif len(message.command) > 1:
        # Use get_target_user to resolve user from arguments (supports full names)
        user = await get_target_user(client, message)
        if not user:
            return await message.reply_text("User not found.")

    # 3. Third, if there is no reply and no argument, fetch the sender's own ID
    else:
        if message.sender_chat:
            txt = f"Chat ID: <code>{message.sender_chat.id}</code>"
            return await message.reply_text(txt, parse_mode=enums.ParseMode.HTML)
        else:
            user = message.from_user

    # 4. Finally, format and send the output for the found user
    if user:
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
        
# ----- Update info -----
async def info(client: Client, message: Message) -> None:
    user = None

    # 1. First, check if the command is a reply to another message
    if message.reply_to_message:
        if message.reply_to_message.sender_chat:
            chat = message.reply_to_message.sender_chat
            text = f"""
<b>Chat Information</b>

<b>Chat Name:</b> {chat.title}
<b>Chat ID:</b> <code>{chat.id}</code>
"""
            return await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
        else:
            user = message.reply_to_message.from_user

    # 2. Second, check if a username, ID, or full name was passed as an argument
    elif len(message.command) > 1:
        user = await get_target_user(client, message)
        if not user:
            return await message.reply_text("User not found.")

    # 3. Third, if there is no reply and no argument, fetch the sender's own info
    else:
        if message.sender_chat:
            chat = message.sender_chat
            text = f"""
<b>Chat Information</b>

<b>Chat Name:</b> {chat.title}
<b>Chat ID:</b> <code>{chat.id}</code>
"""
            return await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
        else:
            user = message.from_user

    if user:
        try:
            member = await client.get_chat_member(message.chat.id, user.id)
            st = member.status
            if st == enums.ChatMemberStatus.OWNER:
                status = "Owner 👑"
            elif st == enums.ChatMemberStatus.ADMINISTRATOR:
                status = "Admin 👮‍♂️"
            elif st == enums.ChatMemberStatus.RESTRICTED:
                if not member.permissions.can_send_messages:
                    status = "Muted 🔇"
                else:
                    status = "Restricted ⚠️"
            elif st == enums.ChatMemberStatus.BANNED:
                status = "Banned 🚫"
            else:
                status = "Member"
        except Exception:
            status = "Not a member"

        try:
            chat = await client.get_chat(user.id)
            bio = chat.bio if chat.bio else "No bio"
        except Exception:
            bio = "No bio"

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

        photo = None
        async for p in client.get_chat_photos(user.id, limit=1):
            photo = p.file_id

        if photo:
            await message.reply_photo(photo, caption=text, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
            
async def report(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not message.reply_to_message:
        await message.reply_text("Reply to a message to report it.")
        return
    offender = message.reply_to_message.from_user
    await message.reply_text(f"🚨 Reported {offender.mention}")

ADMIN_TAG_COOLDOWN = {}
ADMIN_COOLDOWN_TIME = 30

async def admins_cmd(client: Client, message: Message):

    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None

    # admins cannot use command
    if user_id:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return

    # cooldown system
    import time
    now = time.time()

    if chat_id in ADMIN_TAG_COOLDOWN:
        if now - ADMIN_TAG_COOLDOWN[chat_id] < ADMIN_COOLDOWN_TIME:
            return

    ADMIN_TAG_COOLDOWN[chat_id] = now

    admins = []

    try:
        async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            if not member.user.is_bot:
                admins.append(f"<a href='tg://user?id={member.user.id}'>⁠</a>")
    except:
        pass

    if not admins:
        return await message.reply_text("Reported to admins")

    text = "Reported to admins" + "".join(admins)

    await message.reply_text(
        text,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True
    )
    
async def formatting(client: Client, message: Message) -> None:
    await message.reply_text(
        "Use HTML tags: <b>bold</b>, <i>italic</i>, <code>code</code>, <a href='https://t.me'>link</a>",
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True
    )

async def purge(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purge(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purge",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Only non‑verified admins require admin check
    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
        return

    chat_id = message.chat.id
    if not message.reply_to_message:
        return await message.reply_text("Reply to a message to start purge.")

    start_id = message.reply_to_message.id
    end_id = message.id - 1
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await client.delete_messages(chat_id, mid)
            deleted += 1
        except:
            continue

    result = await message.reply_text(f"Purged {deleted} messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))
    
async def spurge(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await spurge(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "spurge",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
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

    await message.delete()

async def purge_amount(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purge_amount(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purge_amount",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
        return

    if len(message.command) < 2:
        return
    try:
        amount = int(message.command[1])
    except:
        return await message.reply_text("Invalid number.")

    chat_id = message.chat.id
    current = message.id
    deleted = 0
    for mid in range(current - amount, current):
        try:
            await client.delete_messages(chat_id, mid)
            deleted += 1
        except:
            pass

    result = await message.reply_text(f"Purged {deleted} messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def purgeuser(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purgeuser(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purgeuser",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to a user's message.")

    target_msg = message.reply_to_message
    if target_msg.from_user:
        target_id = target_msg.from_user.id
    elif target_msg.sender_chat:
        target_id = target_msg.sender_chat.id
    else:
        return await message.reply_text("Could not identify the sender of the replied message.")

    start = target_msg.id
    end = message.id
    deleted = 0

    for mid in range(start, end):
        try:
            msg = await client.get_messages(message.chat.id, mid)
            if (msg.from_user and msg.from_user.id == target_id) or (msg.sender_chat and msg.sender_chat.id == target_id):
                await msg.delete()
                deleted += 1
        except Exception:
            pass

    result = await message.reply_text(f"Deleted {deleted} messages from that user/channel.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def purgebots(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purgebots(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purgebots",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
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

    result = await message.reply_text(f"Deleted {deleted} bot messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def purgemedia(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purgemedia(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purgemedia",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
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

    result = await message.reply_text(f"Deleted {deleted} media messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def purgelinks(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await purgelinks(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "purgelinks",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
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

    result = await message.reply_text(f"Deleted {deleted} link messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def fastpurge(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await fastpurge(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "fastpurge",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
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

    result = await message.reply_text(f"Purged {deleted} messages.")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))

async def cancelpurge(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await cancelpurge(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "cancelpurge",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await require_admin(client, message):
            return

    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text("I don't have the right to delete messages. Please give me admin with delete permission.")
        return

    chat_id = message.chat.id
    if not purge_running.get(chat_id):
        result = await message.reply_text("No purge running.")
        await message.delete()
        asyncio.create_task(delete_after_delay(result, 5))
        return

    purge_running[chat_id] = False
    result = await message.reply_text("Purge cancelling...")
    await message.delete()
    asyncio.create_task(delete_after_delay(result, 5))                        

async def del_message(client: Client, message: Message, verified=False) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    """Delete the replied message."""
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await del_message(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "del",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Click To Prove Admin",
                    callback_data=f"prove_admin:{action_id}"
                )
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check - only for non-verified (normal) admins
    if not verified:
        if not await require_admin(client, message):
            return

    # For normal admins, check delete permission
    if message.from_user:
        if not await user_has_permission(
            client,
            message.chat.id,
            message.from_user.id,
            "can_delete_messages"
        ):
            await message.reply_text(
                "❌ You need 'Delete Messages' permission to use this command."
            )
            return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin.")

    # Bot delete permission check
    if not await bot_has_permission(client, message.chat.id, "can_delete_messages"):
        await message.reply_text(
            "I don't have the right to delete messages. "
            "Please give me admin with delete permission."
        )
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to a message to delete it.")
        return

    try:
        await message.reply_to_message.delete()
        await message.delete()  # delete the command message as well
    except Exception as e:
        await message.reply_text(f"Failed to delete: {e}")

async def pin_message(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await pin_message(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "pin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        # User pin permission check
        if not await user_has_permission(client, message.chat.id, user_id, "can_pin_messages"):
            await message.reply_text("❌ You don't have pin permission.")
            return

    if not await bot_has_permission(client, message.chat.id, "can_pin_messages"):
        await message.reply_text("I don't have the right to pin messages.")
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to a message to pin it.")
        return

    try:
        await message.reply_to_message.pin(disable_notification=False)
        await message.reply_text("📌 Message pinned.")
    except Exception as e:
        await message.reply_text(f"Failed to pin: {e}")

async def unpin_message(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unpin_message(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unpin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        # User pin permission check
        if not await user_has_permission(client, message.chat.id, user_id, "can_pin_messages"):
            await message.reply_text("❌ You don't have pin permission.")
            return

    if not await bot_has_permission(client, message.chat.id, "can_pin_messages"):
        await message.reply_text("I don't have the right to pin messages.")
        return

    # Check if replying to a message (to unpin specific message)
    if message.reply_to_message:
        try:
            await client.unpin_chat_message(message.chat.id, message.reply_to_message.id)
            await message.reply_text("📌 Selected message unpinned.")
        except Exception as e:
            await message.reply_text(f"Failed to unpin: {e}")
    else:
        # If no reply, unpin the latest pinned message
        try:
            chat = await client.get_chat(message.chat.id)
            if chat.pinned_message:
                await client.unpin_chat_message(message.chat.id, chat.pinned_message.id)
                await message.reply_text("📌 Latest pinned message unpinned.")
            else:
                await message.reply_text("❌ No pinned messages in this chat.")
        except Exception as e:
            await message.reply_text(f"Failed to unpin: {e}")

async def spin_message(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await spin_message(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "spin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        if not await user_has_permission(client, message.chat.id, user_id, "can_pin_messages"):
            await message.reply_text("❌ You don't have pin permission.")
            return

    if not await bot_has_permission(client, message.chat.id, "can_pin_messages"):
        await message.reply_text("I don't have the right to pin messages.")
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to a message to pin it silently.")
        return

    try:
        # Silent pin: disable_notification=True
        await message.reply_to_message.pin(disable_notification=True)
        # Send confirmation and delete it after 3 seconds to avoid clutter
        conf_msg = await message.reply_text("📌 Message pinned silently (no notification).")
        asyncio.create_task(delete_after_delay(conf_msg, 3))
    except Exception as e:
        await message.reply_text(f"Failed to pin: {e}")

async def pinned_cmd(client: Client, message: Message):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    """Get the current pinned message in the group. Works for all users."""
    chat = message.chat
    if chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        await message.reply_text("This command only works in groups.")
        return

    try:
        full_chat = await client.get_chat(chat.id)
    except Exception as e:
        await message.reply_text(f"Error fetching chat info: {e}")
        return

    pinned = full_chat.pinned_message
    if not pinned:
        await message.reply_text("No pinned message in this chat.")
        return

    # Fetch the full message to get all details
    try:
        pinned_msg = await client.get_messages(chat.id, pinned.id)
    except Exception as e:
        await message.reply_text(f"Error fetching pinned message: {e}")
        return

    if not pinned_msg:
        await message.reply_text("Pinned message not found (maybe deleted).")
        return

    # Determine sender – handles both normal users and anonymous admins (channels)
    sender = pinned_msg.from_user
    sender_chat = pinned_msg.sender_chat
    if sender:
        sender_name = f"{sender.first_name} {sender.last_name or ''}".strip()
        sender_id = sender.id
        sender_username = f"@{sender.username}" if sender.username else "No username"
        sender_info = f"👤 **Sender:** {sender_name} (`{sender_id}`)\n📱 **Username:** {sender_username}"
    elif sender_chat:
        sender_info = f"📢 **Channel:** {sender_chat.title}\n🆔 `{sender_chat.id}`"
    else:
        sender_info = "👤 **Sender:** Unknown"

    # Message content
    content = ""
    if pinned_msg.text:
        content = pinned_msg.text
    elif pinned_msg.caption:
        content = pinned_msg.caption
    else:
        # Check for media
        media_types = {
            "photo": "📷 Photo",
            "video": "🎥 Video",
            "animation": "🖼 GIF",
            "sticker": "🏷 Sticker",
            "document": "📄 Document",
            "voice": "🎤 Voice",
            "audio": "🎵 Audio",
            "poll": "📊 Poll",
            "location": "📍 Location",
            "venue": "🏟 Venue",
            "contact": "📞 Contact",
            "game": "🎮 Game",
        }
        for mtype, display in media_types.items():
            if getattr(pinned_msg, mtype, None):
                content = f"[{display}]"
                break
        if not content:
            content = "[Unsupported message type]"

    # Build message link
    if chat.username:
        link = f"https://t.me/{chat.username}/{pinned_msg.id}"
    else:
        # Private supergroup: link format t.me/c/chat_id/message_id
        link = f"https://t.me/c/{str(chat.id)[4:]}/{pinned_msg.id}"

    # Create inline button
    button = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Jump to Message", url=link)]])

    # Safe date handling
    if pinned_msg.date:
        date_str = pinned_msg.date.strftime('%Y-%m-%d %H:%M:%S')
    else:
        date_str = "Unknown date"

    # Final text
    text = f"📌 **Pinned Message**\n\n{content}\n\n{sender_info}\n\n📅 {date_str}"

    await message.reply_text(text, reply_markup=button, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)

# Updated unpinall_message function
async def unpinall_message(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    """Unpin all pinned messages in the group (admin only) with confirmation."""
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unpinall_message(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unpinall",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users (skip for verified anonymous)
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Determine user ID
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        await message.reply_text("Could not identify admin.")
        return

    # For verified anonymous admin (user_id == 0), skip permission checks
    if user_id != 0:
        # User must have pin permission
        if not await user_has_permission(client, message.chat.id, user_id, "can_pin_messages"):
            await message.reply_text("❌ You don't have pin permission.")
            return

    # Bot must have pin permission
    if not await bot_has_permission(client, message.chat.id, "can_pin_messages"):
        await message.reply_text("❌ I don't have the right to pin/unpin messages.")
        return

    # Check if there are any pinned messages
    try:
        chat = await client.get_chat(message.chat.id)
        if not chat.pinned_message:
            await message.reply_text("📭 No pinned messages in this chat.")
            return
    except Exception as e:
        await message.reply_text(f"❌ Failed to check pinned messages: {e}")
        return

    # Send confirmation with Yes/No buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"unpinall:yes:{user_id}:{message.chat.id}"),
            InlineKeyboardButton("❌ No", callback_data=f"unpinall:no:{user_id}:{message.chat.id}")
        ]
    ])
    await message.reply_text("Are you sure you want to unpin all messages?", reply_markup=keyboard)

import re
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def parse_button(text: str):
    """Parse button syntax: [Text](buttonurl:link)"""
    pattern = r'\[(.*?)\]\(buttonurl:(.*?)\)'
    buttons = []
    lines = text.split('\n')
    new_text = []
    for line in lines:
        matches = re.findall(pattern, line)
        if matches:
            row = []
            for name, url in matches:
                row.append(InlineKeyboardButton(name, url=url))
                # Remove the button markup from line
                line = line.replace(f'[{name}](buttonurl:{url})', '').strip()
            buttons.append(row)
        new_text.append(line)
    clean_text = '\n'.join(new_text).strip()
    return clean_text, buttons

async def permapin_command(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    """Send a custom pinned message (admin only)."""
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await permapin_command(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "permapin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        if not await require_admin(client, message):
            return

    # Determine user ID for permission check
    user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
    if not user_id:
        await message.reply_text("Could not identify admin.")
        return

    # User must have pin permission
    if not await user_has_permission(client, message.chat.id, user_id, "can_pin_messages"):
        await message.reply_text("❌ You don't have pin permission.")
        return

    # Bot must have pin permission (no need to check can_send_messages – admins can always send)
    if not await bot_has_permission(client, message.chat.id, "can_pin_messages"):
        await message.reply_text("❌ I don't have the right to pin messages.")
        return

    # Get the content: from reply or from command args
    content = None
    reply = message.reply_to_message
    args = get_args(message)

    if reply:
        if reply.text:
            content = reply.text
        elif reply.caption:
            content = reply.caption
        else:
            await message.reply_text("❌ Replied message has no text or caption. Please provide text.")
            return
        if args:
            content += "\n" + " ".join(args)
    else:
        if not args:
            await message.reply_text("Usage: /permapin <text> or reply to a message with text.")
            return
        content = " ".join(args)

    # Parse buttons and clean text
    clean_text, button_rows = parse_button(content)
    if not clean_text:
        clean_text = "📌 Pinned Message"  # fallback

    # Send the message
    try:
        if button_rows:
            sent = await client.send_message(
                message.chat.id,
                clean_text,
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(button_rows)
            )
        else:
            sent = await client.send_message(
                message.chat.id,
                clean_text,
                parse_mode=enums.ParseMode.MARKDOWN
            )
    except Exception as e:
        await message.reply_text(f"❌ Failed to send message: {e}")
        return

    # Pin it and store in database
    try:
        await sent.pin(disable_notification=True)
        await permapin_messages_col.update_one(
            {"chat_id": message.chat.id, "message_id": sent.id},
            {"$set": {"chat_id": message.chat.id, "message_id": sent.id}},
            upsert=True
        )
        await set_chat_setting(message.chat.id, "permapin_id", str(sent.id))   # <-- NEW
        await message.reply_text("✅ Message pinned permanently.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to pin: {e}")

async def antichannelpin_command(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await antichannelpin_command(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "antichannelpin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        if not await require_admin(client, message):
            return

    chat_id = message.chat.id
    enabled = await get_antichannel_enabled(chat_id)

    # Build keyboard with green dot on Enable if enabled, red dot on Disable if disabled
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"antichannelpin:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"antichannelpin:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, any message pinned from a channel will be automatically replaced with the previously manually pinned message."
        if enabled else
        "When disabled, channel pins will remain as pinned and will not be automatically replaced."
    )
    await message.reply_text(
        f"**Anti-Channel Pin**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def cleanlinked_command(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await cleanlinked_command(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "cleanlinked",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        if not await require_admin(client, message):
            return

    chat_id = message.chat.id
    enabled = await get_cleanlinked_enabled(chat_id)

    # Build keyboard with green dot on Enable if enabled, red dot on Disable if disabled
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"cleanlinked:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"cleanlinked:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, any message sent from the linked channel will be automatically deleted."
        if enabled else
        "When disabled, messages from the linked channel will not be deleted."
    )
    await message.reply_text(
        f"**Clean Linked Channel Messages**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def anonadmin_command(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection for owner verification
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await anonadmin_command(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "anonadmin",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Owner", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm you are the group owner.",
                reply_markup=keyboard
            )
            return

    # For normal users, check if they are owner
    user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
    if not user_id:
        await message.reply_text("Could not identify user.")
        return
    try:
        member = await client.get_chat_member(message.chat.id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await message.reply_text("❌ Only the group owner can use this command.")
            return
    except Exception:
        await message.reply_text("❌ Could not verify your status.")
        return

    chat_id = message.chat.id
    enabled = await get_anonadmin_enabled(chat_id)

    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"anonadmin:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"anonadmin:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    status = "✅ Enabled" if enabled else "❌ Disabled"
    chat_name = message.chat.title or "This chat"
    description = (
        f"{chat_name} currently allows all anonymous admins to use any admin command without restriction."
        if enabled else
        f"{chat_name} currently requires anonymous admins to press a button to confirm their permission."
    )
    await message.reply_text(
        f"**Anonymous Admin Mode**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def nightmode_cmd(client: Client, message: Message, verified=False, admin_id: int = None):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    # Bot admin check
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await nightmode_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "nightmode",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # Permission check (needs change_info? we'll use change_info as it's a chat setting)
    user_id = admin_id if admin_id else (message.from_user.id if message.from_user else None)
    if user_id != 0:
        if not await user_has_permission(client, message.chat.id, user_id, "can_change_info"):
            await message.reply_text("❌ You need 'Change Group Info' permission to manage night mode.")
            return

    chat_id = message.chat.id
    enabled = await get_nightmode_enabled(chat_id)
    start = await get_nightmode_start(chat_id)
    end = await get_nightmode_end(chat_id)

    status = "✅ Enabled" if enabled else "❌ Disabled"
    text = (
        f"🌙 **Night Mode Settings**\n\n"
        f"Status: {status}\n"
        f"Window: {start} → {end} (Kolkata time)\n\n"
        f"During this window, all media messages (photos, videos, stickers, etc.) from non‑approved users will be automatically deleted.\n"
        f"Approved users and admins are exempt."
    )

        # Determine button texts with dots
    enable_text = " Enable" if enabled else "🟢 Enable"
    disable_text = "🔴 Disable" if enabled else " Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"nightmode:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"nightmode:disable:{chat_id}")
        ],
        [
            InlineKeyboardButton(f"⏰ Set Start ({start})", callback_data=f"nightmode:set_start:{chat_id}"),
            InlineKeyboardButton(f"⏰ Set End ({end})", callback_data=f"nightmode:set_end:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    await message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)


# --- Bio system commands ---
async def allow_bio_user(client: Client, message: Message, verified=False):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await allow_bio_user(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "allow_bio",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check (only for non‑verified)
    if not verified:
        if not await bot_is_admin(client, message.chat.id):
            return await message.reply_text("❌ I am not admin.")
        if not await require_admin(client, message):
            return

    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")
    await bio_allowlist_col.update_one(
        {"user_id": target.id},
        {"$set": {"user_id": target.id}},
        upsert=True
    )
    await bio_warns_col.delete_one({"chat_id": message.chat.id, "user_id": target.id})
    await message.reply_text(f"✅ User {target.mention} whitelisted from bio/link checks.")

async def unallow_bio_user(client: Client, message: Message, verified=False):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await unallow_bio_user(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "unallow_bio",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await bot_is_admin(client, message.chat.id):
            return await message.reply_text("❌ I am not admin.")
        if not await require_admin(client, message):
            return

    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")
    await bio_allowlist_col.delete_one({"user_id": target.id})
    await message.reply_text(f"❌ User {target.mention} removed from whitelist.")

async def aplist_bio(client: Client, message: Message, verified=False):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await aplist_bio(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "aplist_bio",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await bot_is_admin(client, message.chat.id):
            return await message.reply_text("❌ I am not admin.")
        if not await require_admin(client, message):
            return

    cursor = bio_allowlist_col.find({})
    users = [str(doc["user_id"]) async for doc in cursor]
    text = "✅ **Bio Allowed Users:**\n" + "\n".join([f"<code>{u}</code>" for u in users]) if users else "No allowed users."
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def bioconfig_cmd(client: Client, message: Message, verified=False):
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await bioconfig_cmd(client, message, verified=True)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "bioconfig",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not verified:
        if not await bot_is_admin(client, message.chat.id):
            return await message.reply_text("❌ I am not admin.")
        if not await require_admin(client, message):
            return

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

async def bio_and_link_scanner(client: Client, message: Message):
    # Ignore private chats, bots, and service messages
    if not message.from_user or message.chat.type == enums.ChatType.PRIVATE or message.from_user.is_bot:
        return

    # Skip anonymous admin messages if the setting is enabled
    if message.sender_chat and message.from_user is None:
        if await get_anonadmin_enabled(message.chat.id):
            return

    chat_id = message.chat.id
    warn_limit, action, is_enabled = await get_bio_config(chat_id)
    if not is_enabled:
        return

    user_id = message.from_user.id

    # Skip admins and approved users
    if await is_admin(client, message, user_id) or await is_approved(chat_id, user_id):
        return

    # Skip allowlisted users
    if await bio_allowlist_col.find_one({"user_id": user_id}):
        return

    # Check message for link
    msg_text = message.text or message.caption or ""
    if has_link(msg_text):
        violation = True
        reason = "Link in Message"
    else:
        # Fetch user's bio (no cache – get fresh each time to catch new links)
        try:
            user = await client.get_chat(user_id)
            user_bio = user.bio
        except Exception as e:
            # If we can't fetch the bio, assume no violation
            user_bio = None
        violation = user_bio and has_link(user_bio)
        reason = "Link in Bio" if violation else ""

    if not violation:
        return

    # Try to delete the offending message
    try:
        await message.delete()
    except Exception as e:
        # If deletion fails, notify the chat (only once per minute to avoid spam)
        # (We'll send a warning later anyway)
        pass

    # Update warnings count
    warn_doc = await bio_warns_col.find_one_and_update(
        {"chat_id": chat_id, "user_id": user_id},
        {"$inc": {"count": 1}},
        upsert=True,
        return_document=True
    )
    count = warn_doc["count"]
    safe_name = escape(message.from_user.first_name)
    user_id = message.from_user.id
    mention = message.from_user.mention

    # If warnings reach limit, apply punishment
    if count >= warn_limit:
        # Check if bot has the necessary permissions before punishing
        bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)
        if bot_member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await client.send_message(
                chat_id,
                f"⚠️ **Bio warning failed**: I am not an admin in this chat.\n"
                f"User {mention} has {count}/{warn_limit} warnings (link in {reason}).",
                parse_mode=enums.ParseMode.MARKDOWN
            )
            return

        if action == "mute":
            if not bot_member.privileges.can_restrict_members:
                await client.send_message(
                    chat_id,
                    f"⚠️ **Bio warning failed**: I don't have permission to restrict members.\n"
                    f"User {mention} has {count}/{warn_limit} warnings (link in {reason}).",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                return
            try:
                await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
                # Clear warnings after successful punishment
                await bio_warns_col.delete_one({"chat_id": chat_id, "user_id": user_id})
                text = (
                    f"🔇 <b>User Muted Indefinitely</b>\n\n"
                    f"👤 <b>User:</b> {mention}\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📝 <b>Reason:</b> {reason}"
                )
                btn = InlineKeyboardButton("🔊 Unmute", callback_data=f"bio_unmute_{user_id}")
            except Exception as e:
                await client.send_message(
                    chat_id,
                    f"⚠️ **Bio warning failed**: Could not mute user {mention}.\nError: {e}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                return
        else:  # ban
            if not bot_member.privileges.can_restrict_members:
                await client.send_message(
                    chat_id,
                    f"⚠️ **Bio warning failed**: I don't have permission to restrict members.\n"
                    f"User {mention} has {count}/{warn_limit} warnings (link in {reason}).",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                return
            try:
                await client.ban_chat_member(chat_id, user_id)
                await bio_warns_col.delete_one({"chat_id": chat_id, "user_id": user_id})
                text = (
                    f"🚫 <b>User Banned</b>\n\n"
                    f"👤 <b>User:</b> {mention}\n"
                    f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                    f"📝 <b>Reason:</b> {reason}"
                )
                btn = InlineKeyboardButton("🔓 Unban", callback_data=f"bio_unban_{user_id}")
            except Exception as e:
                await client.send_message(
                    chat_id,
                    f"⚠️ **Bio warning failed**: Could not ban user {mention}.\nError: {e}",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                return

        keyboard = InlineKeyboardMarkup([[btn], [InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]])
        await client.send_message(chat_id, text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

    else:
        # Not yet at limit – send warning message
        text = (
            f"⚠️ <b>MESSAGE REMOVED (Link Detected)</b>\n\n"
            f"👤 <b>User:</b> {mention}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"📝 <b>Reason:</b> {reason}\n"
            f"📊 <b>warns:</b> {count}/{warn_limit}\n\n"
            f"🛑 <i>Please remove any links from your Bio or messages.</i>\n\n"
            f"📌 REPEATED VIOLATIONS WILL LEAD TO MUTE/BAN."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Allow", callback_data=f"bio_allow_{user_id}"),
             InlineKeyboardButton("🛡 Unwarn", callback_data=f"bio_unwarn_{user_id}")],
            [InlineKeyboardButton("🗑 Delete", callback_data="del_msg")]
        ])
        await client.send_message(chat_id, text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

    # Update stats
    await increment_bio_stat("scanned")
    if violation:
        await increment_bio_stat("caught")

async def set_vc_msg(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

# Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await set_vc_msg(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setvcmsg",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    await set_chat_setting(message.chat.id, "vc_msg_text", " ".join(args) if args else "Voice chat started!")
    await message.reply_text("VC message set.")

async def set_vc_invite(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await set_vc_invite(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "setvcinvite",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    await set_chat_setting(message.chat.id, "vc_invite_text", " ".join(args) if args else "Join VC now!")
    await message.reply_text("VC invite message set.")

async def vcmsg_toggle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "vc_msg_enabled", "vcmsg", verified, admin_id)

async def vcinvite_toggle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "vc_invite_enabled", "vcinvite", verified, admin_id)

async def vc_events(client: Client, message: Message):
    chat_id = message.chat.id
    vc_msg_enabled = await get_chat_setting(chat_id, "vc_msg_enabled", "0") == "1"
    vc_invite_enabled = await get_chat_setting(chat_id, "vc_invite_enabled", "0") == "1"

    if message.video_chat_started and vc_msg_enabled:
        text = await get_chat_setting(chat_id, "vc_msg_text", "🎙 Voice chat started!")
        await message.reply_text(text)
    elif message.video_chat_ended and vc_msg_enabled:
        await message.reply_text("🔴 Voice chat ended.")
    elif message.video_chat_members_invited and vc_invite_enabled:
        text = await get_chat_setting(chat_id, "vc_invite_text", "📢 You are invited to VC!")
        users = message.video_chat_members_invited.users
        user_ids = "_".join([str(u.id) for u in users])
        button = InlineKeyboardMarkup([[InlineKeyboardButton("🎙 Join VC", callback_data=f"joinvc_{user_ids}")]])
        await message.reply_text(text, reply_markup=button)

async def vc_started(client: Client, message: Message) -> None:
    await message.reply_text("🎙 **Voice Chat started!** Come join everyone.")

async def vc_ended(client: Client, message: Message) -> None:
    duration = message.video_chat_ended.duration
    formatted = format_duration(duration)   # uses the existing function
    await message.reply_text(f"🔇 **Voice Chat ended.**\n⏳ Duration: {formatted}.")

async def vc_invited(client: Client, message: Message):
    invited_users = message.video_chat_members_invited.users
    invited_names = ", ".join([u.first_name for u in invited_users])
    if message.from_user:
        sender_name = message.from_user.first_name
    elif message.sender_chat:
        sender_name = message.sender_chat.title
    else:
        sender_name = "An Admin"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Join Voice Chat 📞",
            url=f"https://t.me/{message.chat.username}" if message.chat.username else f"https://t.me/c/{str(message.chat.id)[4:]}/1"
        )]
    ])
    await message.reply_text(
        f"📞 **VC Invite:** {sender_name} invited {invited_names} to the Voice Chat!",
        reply_markup=keyboard
    )

# --- Federation commands ---
async def get_fed(chat_id: int) -> str | None:
    doc = await fed_membership_col.find_one({"chat_id": chat_id})
    return doc["fed_id"] if doc else None

async def fed_info(client: Client, message: Message, verified=False, admin_id=None):
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await fed_info(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "fedinfo",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Determine the user ID (if any)
    user_id = admin_id if admin_id is not None else (message.from_user.id if message.from_user else None)
    args = get_args(message)

    # Decide fed_id
    if args:
        fed_id = args[0]
    else:
        # For non-anonymous, try to get their own federation
        if user_id is None:
            return await message.reply_text("Usage: /fedinfo <fed_id>")
        my_fed = await federations_col.find_one({"owner_id": user_id})
        if not my_fed:
            return await message.reply_text(
                "❌ You need to provide a fedID or be a federation creator to use this command."
            )
        fed_id = my_fed["fed_id"]

    # Fetch federation
    fed = await federations_col.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("❌ Federation not found.")

    # Gather statistics
    owner = await client.get_users(fed["owner_id"])
    admin_count = len(fed.get("admins", []))
    groups_count = await fed_membership_col.count_documents({"fed_id": fed_id})
    bans_count = await fban_list_col.count_documents({"fed_id": fed_id})
    subs = fed.get("subscribed_feds", [])
    subs_count = len(subs)
    created_at = fed.get("created_at", "N/A")

    # Subscribed feds text
    if subs:
        subs_text = "\n".join([f"• <code>{fid}</code>" for fid in subs])
    else:
        subs_text = "This federation is not subscribed to any other feds."

    text = (
        f"<b>📊 Federation Information</b>\n\n"
        f"<b>🏷 Name:</b> {fed['fed_name']}\n"
        f"<b>🆔 ID:</b> <code>{fed_id}</code>\n"
        f"<b>👑 Owner:</b> {owner.mention if owner else 'Unknown'}\n"
        f"<b>👥 Admins:</b> {admin_count}\n"
        f"<b>🏢 Groups Joined:</b> {groups_count}\n"
        f"<b>🚫 Total Bans:</b> {bans_count}\n"
        f"<b>🔗 Subscribed Feds:</b> {subs_count}\n"
        f"<b>📅 Created On:</b> {created_at}\n\n"
        f"<b>📡 Subscribed To:</b>\n{subs_text}"
    )

    # Only show the "View Admins List" button if the user is an owner/admin of this federation
    # (or if anonymous admin with verified, they still don't have a user_id so we treat as non-admin)
    if user_id and (user_id == fed["owner_id"] or user_id in fed.get("admins", [])):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("View Admins List 👥", callback_data=f"fed_admins:{fed_id}")]
        ])
    else:
        keyboard = None

    await message.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)

async def fed_admin_list_callback(client: Client, callback_query: CallbackQuery):
    fed_id = callback_query.data.split(":")[1]
    fed = await federations_col.find_one({"fed_id": fed_id})
    # Remove the permission check – now anyone can see the admin list
    owner_info = await client.get_users(fed["owner_id"])
    res = f"<b>Fed Admins for {fed['fed_name']}</b>\n\n"
    res += f"👑 Owner: {owner_info.first_name}\n"
    for admin_id in fed.get("admins", []):
        try:
            u = await client.get_users(admin_id)
            res += f"• {u.first_name} (<code>{u.id}</code>)\n"
        except:
            continue
    await callback_query.edit_message_text(res, parse_mode=enums.ParseMode.HTML)

async def new_fed(client: Client, message: Message):
    # PM-only restriction
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command can only be used in private chat with the bot.")
        return

    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")

    user_id = message.from_user.id
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /newfed <federation_name>")

    # Check if user already owns a federation
    existing_fed = await federations_col.find_one({"owner_id": user_id})
    if existing_fed:
        return await message.reply_text(
            f"You already own a federation  **{existing_fed['fed_name']}**.\n\n"
            f"Please delete it first using /delfed before creating a new one.",
            parse_mode=enums.ParseMode.MARKDOWN
        )

    fed_name = " ".join(args)
    fed_id = str(uuid.uuid4())[:12]
    await federations_col.insert_one({
        "fed_id": fed_id,
        "fed_name": fed_name,
        "owner_id": user_id,
        "admins": []
    })
    await message.reply_text(
        f"<b>Federation Created!</b>\n\n"
        f"<b>Name:</b> {fed_name}\n"
        f"<b>Fed ID:</b> <code>{fed_id}</code>\n\n"
        f"Group owners can use this ID to join the federation! eg:\n"
        f"<code>/joinfed {fed_id}</code>."
    )

async def fedadmins_cmd(client: Client, message: Message):
    """Show federation admins and subscribed feds (PM only)."""
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command only works in private chat with the bot.")
        return

    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /fedadmins <fed_id>")
        return

    fed_id = args[0]
    fed = await federations_col.find_one({"fed_id": fed_id})
    if not fed:
        await message.reply_text("❌ Federation not found.")
        return

    # Owner
    try:
        owner_user = await client.get_users(fed["owner_id"])
        owner_mention = f"<a href='tg://user?id={owner_user.id}'>{escape(owner_user.first_name)}</a>"
        owner_line = f"👑 **Owner:** {owner_mention} (<code>{owner_user.id}</code>)"
    except:
        owner_line = f"👑 **Owner:** Unknown (<code>{fed['owner_id']}</code>)"

    # Admins
    admin_lines = []
    for admin_id in fed.get("admins", []):
        try:
            admin_user = await client.get_users(admin_id)
            mention = f"<a href='tg://user?id={admin_user.id}'>{escape(admin_user.first_name)}</a>"
            admin_lines.append(f"• {mention} (<code>{admin_user.id}</code>)")
        except:
            admin_lines.append(f"• Unknown (<code>{admin_id}</code>)")

    admins_text = "\n".join(admin_lines) if admin_lines else "None"

    # Subscribed feds
    subfed_lines = []
    for sub_id in fed.get("subscribed_feds", []):
        sub_fed = await federations_col.find_one({"fed_id": sub_id})
        if sub_fed:
            subfed_lines.append(f"• **{escape(sub_fed['fed_name'])}** (<code>{sub_fed['fed_id']}</code>)")
        else:
            subfed_lines.append(f"• Unknown federation (<code>{sub_id}</code>)")
    subfed_text = "\n".join(subfed_lines) if subfed_lines else "None"

    text = (
        f"<b>🏛 Federation: {escape(fed['fed_name'])}</b>\n\n"
        f"{owner_line}\n\n"
        f"<b>👥 Admins ({len(fed.get('admins', []))})</b>\n{admins_text}\n\n"
        f"<b>🔗 Subscribed Federations ({len(fed.get('subscribed_feds', []))})</b>\n{subfed_text}"
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


import time
from datetime import datetime

fedstat_cooldown = {}

async def fedstat_cmd(client: Client, message: Message, verified=False, admin_id=None):
    """
    Universal fedstat command – blocked for anonymous admins.
    """
    # --- Block anonymous admins immediately ---
    if message.from_user is None and message.sender_chat:
        await message.reply_text("❌ This command cannot be used by anonymous admins.")
        return

    # --- Cooldown check (1 minute) ---
    user_id = message.from_user.id  # safe because we blocked anonymous
    now = time.time()
    last = fedstat_cooldown.get(user_id, 0)
    if now - last < 60:
        remaining = int(60 - (now - last))
        await message.reply_text(f"⏳ Please wait {remaining} seconds before using this command again.")
        return
    fedstat_cooldown[user_id] = now

    # --- Determine user context ---
    args = get_args(message)
    effective_user_id = admin_id if admin_id is not None else user_id

    # --- Case 1: No arguments → show bans of the caller ---
    if not args:
        await show_user_bans(client, message, effective_user_id, fed_id=None)
        return

    # --- Case 2: One argument → could be a user or a fed_id ---
    if len(args) == 1:
        first = args[0]
        # Check if it's a valid fed_id (exists in DB)
        fed = await federations_col.find_one({"fed_id": first})
        if fed:
            # It's a fed_id → show caller's ban info for this fed
            await show_user_bans(client, message, effective_user_id, fed_id=first)
        else:
            # Assume it's a user identifier
            target_user = await resolve_user(client, first)
            if not target_user:
                await message.reply_text("User not found.")
                return
            await show_user_bans(client, message, target_user.id, fed_id=None)
        return

    # --- Case 3: Two arguments → user and fed_id ---
    if len(args) == 2:
        user_input, fed_id = args[0], args[1]
        target_user = await resolve_user(client, user_input)
        if not target_user:
            await message.reply_text("User not found.")
            return
        # Verify fed_id exists
        fed = await federations_col.find_one({"fed_id": fed_id})
        if not fed:
            await message.reply_text("Federation not found.")
            return
        await show_user_bans(client, message, target_user.id, fed_id=fed_id)
        return

    # Too many arguments
    await message.reply_text(
        "Usage:\n"
        "`/fedstat` – list your own bans\n"
        "`/fedstat <user>` – list bans of that user\n"
        "`/fedstat <fed_id>` – details of your ban in that fed\n"
        "`/fedstat <user> <fed_id>` – details of a user's ban in a fed",
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def chatfed_cmd(client: Client, message: Message):
    """Show federation info for the current chat."""
    chat = message.chat
    if chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        await message.reply_text("This command only works in groups.")
        return

    chat_id = chat.id
    membership = await fed_membership_col.find_one({"chat_id": chat_id})
    if not membership:
        await message.reply_text("This group is not connected to any federation.")
        return

    fed_id = membership["fed_id"]
    fed = await federations_col.find_one({"fed_id": fed_id})
    if not fed:
        await message.reply_text("Federation data not found (maybe deleted).")
        return

    # Owner info
    try:
        owner = await client.get_users(fed["owner_id"])
        owner_text = f"{owner.first_name} (<code>{owner.id}</code>)"
    except:
        owner_text = f"Unknown (<code>{fed['owner_id']}</code>)"

    # Stats
    groups_count = await fed_membership_col.count_documents({"fed_id": fed_id})
    bans_count = await fban_list_col.count_documents({"fed_id": fed_id})
    admin_count = len(fed.get("admins", []))

    text = (
        f"<b>🏛 Federation of this chat</b>\n\n"
        f"<b>Name:</b> {fed['fed_name']}\n"
        f"<b>ID:</b> <code>{fed_id}</code>\n"
        f"<b>Owner:</b> {owner_text}\n"
        f"<b>Groups in federation:</b> {groups_count}\n"
        f"<b>Total bans:</b> {bans_count}\n"
        f"<b>Admins:</b> {admin_count}\n"
    )

    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def resolve_user(client: Client, identifier: str):
    """Helper to get a user from ID, username, or mention."""
    # Try as integer ID
    if identifier.isdigit():
        try:
            return await client.get_users(int(identifier))
        except:
            pass
    # Try as username (strip @)
    if identifier.startswith('@'):
        identifier = identifier[1:]
    try:
        return await client.get_users(identifier)
    except:
        pass
    # Could also try by full name, but keep it simple for now
    return None

async def quietfed_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await quietfed_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "quietfed",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for normal users
    if not verified:
        if not await require_admin(client, message):
            return

    # Bot must be admin in group (if in group)
    if message.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
        if not await bot_is_admin(client, message.chat.id):
            await message.reply_text("❌ I am not admin here.")
            return

    chat_id = message.chat.id
    enabled = await get_quietfed_enabled(chat_id)

    # Build keyboard with green dot on Enable if enabled, red dot on Disable if disabled
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"quietfed:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"quietfed:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, users banned in the federation will be automatically removed "
        "(banned/kicked) whenever they join or send a message, with an explanation."
        if enabled else
        "When disabled, federation-banned users will not be automatically removed."
    )
    await message.reply_text(
        f"**Quiet Federation Ban Mode**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def check_and_punish_fedbanned_user(
    client: Client,
    chat_id: int,
    user_id: int,
    message: Message = None
) -> bool:
    """
    Checks if the user is banned in the chat's federation.
    If quietfed is enabled and the user is banned, they are banned from the chat,
    their message is deleted (if any), and a notification is sent.
    Returns True if the user was banned, False otherwise.
    """
    # 1. Get chat's federation
    fed_membership = await fed_membership_col.find_one({"chat_id": chat_id})
    if not fed_membership:
        return False
    fed_id = fed_membership["fed_id"]

    # 2. Check if user is fedbanned
    fed_ban = await fban_list_col.find_one({"fed_id": fed_id, "user_id": user_id})
    if not fed_ban:
        return False

    # 3. Check if quietfed is enabled
    if not await get_quietfed_enabled(chat_id):
        return False

    # 4. Get federation name
    fed = await federations_col.find_one({"fed_id": fed_id})
    fed_name = fed["fed_name"] if fed else "Unknown Federation"

    # 5. Ban the user from the chat
    try:
        await client.ban_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"Error banning fedbanned user {user_id}: {e}")
        return False

    # 6. Delete the user's message if provided
    if message:
        try:
            await message.delete()
        except Exception:
            pass

    # 7. Send notification
    reason = fed_ban.get("reason", "No reason provided")
    text = (
        f"🚫 **User removed**\n"
        f"👤 User: {message.from_user.mention if message else f'<code>{user_id}</code>'}\n"
        f"🏛 Federation: {fed_name}\n"
        f"📝 Reason: {reason}\n\n"
        f"This user is banned in the federation and cannot join or speak here."
    )
    await client.send_message(chat_id, text, parse_mode=enums.ParseMode.MARKDOWN)

    return True    

async def show_user_bans(client: Client, message: Message, user_id: int, fed_id: str = None):
    """
    Display bans for a given user, optionally filtered by federation.
    """
    # Fetch all bans for this user
    query = {"user_id": user_id}
    if fed_id:
        query["fed_id"] = fed_id
    bans_cursor = fban_list_col.find(query)
    bans = await bans_cursor.to_list(length=100)

    if not bans:
        if fed_id:
            await message.reply_text(f"User <code>{user_id}</code> is not banned in that federation.")
        else:
            await message.reply_text(f"User <code>{user_id}</code> is not banned in any federation.")
        return

    # Get user's name (if available)
    try:
        user = await client.get_users(user_id)
        user_display = f"{user.first_name} {user.last_name or ''}".strip()
        if user.username:
            user_display += f" (@{user.username})"
    except:
        user_display = f"User {user_id}"

    if fed_id:
        # Single federation: show details
        ban = bans[0]
        fed = await federations_col.find_one({"fed_id": ban["fed_id"]})
        fed_name = fed["fed_name"] if fed else "Unknown federation"
        reason = ban.get("reason", "No reason")
        timestamp = ban.get("timestamp")
        if timestamp:
            date_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = "Unknown date"

        text = (
            f"<b>🔒 Ban Information</b>\n\n"
            f"👤 <b>User:</b> {user_display} (<code>{user_id}</code>)\n"
            f"🏛 <b>Federation:</b> {fed_name} (<code>{ban['fed_id']}</code>)\n"
            f"📝 <b>Reason:</b> {reason}\n"
            f"📅 <b>Banned on:</b> {date_str}"
        )
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    else:
        # List all federations
        lines = []
        for ban in bans:
            fed = await federations_col.find_one({"fed_id": ban["fed_id"]})
            fed_name = fed["fed_name"] if fed else "Unknown federation"
            reason = ban.get("reason", "No reason")
            lines.append(f"• <b>{fed_name}</b> (<code>{ban['fed_id']}</code>)\n  Reason: {reason}")
        text = f"<b>🚫 Federations where {user_display} is banned:</b>\n\n" + "\n".join(lines)
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


async def prove_admin_fedstat_callback(client: Client, callback_query: CallbackQuery):
    # This will be called if the anonymous admin needs to prove identity.
    # We'll reuse the existing prove_admin_callback logic, but we must add a case for "fedstat".
    pass  # Actually we'll add it to the main prove_admin_callback

async def join_fed_group(client: Client, message: Message, verified=False, admin_id: int = None):
    """Connect the current group to a federation (only group owner can do this)."""
    # Anonymous admin mode detection (bypass button)
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            # Bypass button – treat as verified with placeholder admin ID 0
            return await join_fed_group(client, message, verified=True, admin_id=0)
        else:
            # Send proof button
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "joinfed",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Determine user ID (real ID from callback or normal user)
    if admin_id is not None:
        user_id = admin_id
    elif message.from_user:
        user_id = message.from_user.id
    else:
        # If verified anonymous admin (admin_id=0), we treat them as owner
        if verified and message.from_user is None and message.sender_chat:
            user_id = None  # Will handle via sender_chat
        else:
            await message.reply_text("Could not identify user.")
            return

    chat_id = message.chat.id

    # Owner verification
    if user_id is not None:
        # Normal user (with from_user) or admin_id passed (real ID)
        try:
            member = await client.get_chat_member(chat_id, user_id)
            if member.status != enums.ChatMemberStatus.OWNER:
                return await message.reply_text("Only the group owner can attach the group to a federation.")
        except Exception:
            return await message.reply_text("Only the group owner can attach the group to a federation.")
    else:
        # Verified anonymous admin (sender_chat is the admin channel)
        try:
            # Check if the sender_chat is the owner
            member = await client.get_chat_member(chat_id, message.sender_chat.id)
            if member.status != enums.ChatMemberStatus.OWNER:
                return await message.reply_text("Only the group owner can attach the group to a federation.")
        except Exception:
            return await message.reply_text("Could not verify your status. Are you a member?")

    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /joinfed <fed_id>")

    fed_id = args[0]
    fed = await federations_col.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("Federation not found.")

    # Check if the group is already connected
    existing = await fed_membership_col.find_one({"chat_id": chat_id})
    if existing:
        return await message.reply_text(
            "This group is already connected to a federation. Leave it first using /leavefed."
        )

    # Send confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Join Federation", callback_data=f"joinfed:confirm:{fed_id}:{chat_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"joinfed:cancel:{chat_id}")]
    ])
    await message.reply_text(
        f"⚠️ **Are you sure you want to connect this group to federation {fed['fed_name']}?**",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def leavefed_cmd(client: Client, message: Message, verified=False, admin_id=None):
    """Remove current group from its federation (group owner only)."""
    chat_id = message.chat.id

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(chat_id):
            return await leavefed_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": chat_id,
                "message": message,
                "action": "leavefed",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Determine user ID for permission check (if any)
    user_id = admin_id if admin_id is not None else (message.from_user.id if message.from_user else None)

    # Check if the user is the group owner
    if user_id is not None:
        # Normal user (from_user)
        try:
            member = await client.get_chat_member(chat_id, user_id)
            if member.status != enums.ChatMemberStatus.OWNER:
                return await message.reply_text("❌ Only the group owner can leave a federation.")
        except Exception:
            return await message.reply_text("❌ Could not verify owner status.")
    else:
        # Verified anonymous admin (sender_chat)
        try:
            member = await client.get_chat_member(chat_id, message.sender_chat.id)
            if member.status != enums.ChatMemberStatus.OWNER:
                return await message.reply_text("❌ Only the group owner can leave a federation.")
        except Exception:
            return await message.reply_text("❌ Could not verify owner status.")

    # Check if the group is actually in a federation
    membership = await fed_membership_col.find_one({"chat_id": chat_id})
    if not membership:
        return await message.reply_text("This group is not connected to any federation.")

    # Send confirmation buttons
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, leave federation", callback_data=f"leavefed:confirm:{chat_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"leavefed:cancel:{chat_id}")]
    ])
    await message.reply_text(
        "⚠️ **Are you sure you want to leave the federation?**\n\n"
        "This group will no longer be protected by federation bans.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def subscribe_fed(client: Client, message: Message):
    """Subscribe your federation to another federation (fed-to-fed subscription)."""

    # 1. Ensure normal user
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id

    # 2. FIRST CHECK → Does user own federation?
    my_fed = await federations_col.find_one({"owner_id": user_id})
    if not my_fed:
        return await message.reply_text(
            "❌ Only federation creators can subscribe to a fed.\n"
            "But you don't have a federation.\n\n"
            "👉 Use /newfed to create one first."
        )

    # 3. THEN check args
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /subfed <target_fed_id>")

    # 4. Continue logic
    target_fed_id = args[0]
    target_fed = await federations_col.find_one({"fed_id": target_fed_id})

    if not target_fed:
        return await message.reply_text("❌ Invalid Federation ID.")

    if my_fed["fed_id"] == target_fed_id:
        return await message.reply_text("❌ You cannot subscribe to your own federation.")

    await federations_col.update_one(
        {"fed_id": my_fed["fed_id"]},
        {"$addToSet": {"subscribed_feds": target_fed_id}}
    )

    await message.reply_text(
        f"✅ Success! Your federation <b>{my_fed['fed_name']}</b> now subscribes to <b>{target_fed['fed_name']}</b>.",
        parse_mode=enums.ParseMode.HTML
    )

async def unsubscribe_fed(client: Client, message: Message):
    """Unsubscribe your federation from another federation."""

    # 1. Anonymous admin check
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id

    # 2. Check if user owns a federation
    my_fed = await federations_col.find_one({"owner_id": user_id})
    if not my_fed:
        return await message.reply_text(
            "❌ Only federation creators can unsubscribe from a fed.\n"
            "But you don't have a federation."
        )

    # 3. Args check
    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /unsubfed <target_fed_id>")

    target_fed_id = args[0]

    # 4. Check target fed exists
    target_fed = await federations_col.find_one({"fed_id": target_fed_id})
    if not target_fed:
        return await message.reply_text("❌ Invalid Federation ID.")

    # 5. Check if already subscribed
    if target_fed_id not in my_fed.get("subscribed_feds", []):
        return await message.reply_text(
            "❌ Your federation is not subscribed to this federation."
        )

    # 6. Remove subscription
    await federations_col.update_one(
        {"fed_id": my_fed["fed_id"]},
        {"$pull": {"subscribed_feds": target_fed_id}}
    )

    await message.reply_text(
        f"✅ Success! Your federation <b>{my_fed['fed_name']}</b> has unsubscribed from <b>{target_fed['fed_name']}</b>.",
        parse_mode=enums.ParseMode.HTML
    ) 

async def fban_user(client: Client, message: Message):
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("<b>Error:</b> User not found. Provide UserID/Username or reply.")

    membership = await fed_membership_col.find_one({"chat_id": message.chat.id})
    if not membership:
        return await message.reply_text("This group is not connected to any federation.")
    fed_id = membership["fed_id"]
    fed = await federations_col.find_one({"fed_id": fed_id})
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await message.reply_text("Only Fed Admin/Owner can fban.")

    reason = " ".join(get_args(message)[1:]) if len(get_args(message)) > 1 else "No reason"
    await fban_list_col.update_one(
        {"fed_id": fed_id, "user_id": target.id},
        {"$set": {"reason": reason, "user_name": target.first_name}},
        upsert=True
    )

    subscribers = federations_col.find({"subscribed_feds": fed_id})
    async for sub_fed in subscribers:
        await fban_list_col.update_one(
            {"fed_id": sub_fed["fed_id"], "user_id": target.id},
            {"$set": {"reason": f"Synced: {reason}", "user_name": target.first_name}},
            upsert=True
        )

    count = 0
    groups = fed_membership_col.find({"fed_id": fed_id})
    async for group in groups:
        try:
            await client.ban_chat_member(group["chat_id"], target.id)
            count += 1
        except:
            continue

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
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("<b>Error:</b> User not found. Provide UserID/Username or reply.")

    membership = await fed_membership_col.find_one({"chat_id": message.chat.id})
    if not membership:
        return await message.reply_text("This group is not connected to any federation.")
    fed_id = membership["fed_id"]
    fed = await federations_col.find_one({"fed_id": fed_id})
    if user_id != fed["owner_id"] and user_id not in fed.get("admins", []):
        return await message.reply_text("Only Fed Admin/Owner can unfban.")

    await fban_list_col.delete_one({"fed_id": fed_id, "user_id": target.id})
    subscribers = federations_col.find({"subscribed_feds": fed_id})
    async for sub_fed in subscribers:
        await fban_list_col.delete_one({"fed_id": sub_fed["fed_id"], "user_id": target.id})

    count = 0
    groups = fed_membership_col.find({"fed_id": fed_id})
    async for group in groups:
        try:
            await client.unban_chat_member(group["chat_id"], target.id)
            count += 1
        except:
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
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id
    target = await get_target_user(client, message)
    if not target:
        return await message.reply_text("User not found.")
    
    # Get the user's owned federation
    fed = await federations_col.find_one({"owner_id": user_id})
    if not fed:
        return await message.reply_text("You don't own any federation. Create one with /newfed first.")
    
    cmd = message.command[0].lower()
    fed_id = fed["fed_id"]
    
    if cmd == "fpromote":
        await federations_col.update_one(
            {"fed_id": fed_id},
            {"$addToSet": {"admins": target.id}}
        )
        text = f"⭐️ {target.first_name} is now an Admin of your federation **{fed['fed_name']}**!"
    else:  # fdemote
        await federations_col.update_one(
            {"fed_id": fed_id},
            {"$pull": {"admins": target.id}}
        )
        text = f"🗑 {target.first_name} removed from Fed Admins of your federation **{fed['fed_name']}**."
    
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)

async def del_fed(client: Client, message: Message):
    # PM-only restriction
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command can only be used in private chat with the bot.")
        return

    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")

    user_id = message.from_user.id
    fed = await federations_col.find_one({"owner_id": user_id})
    if not fed:
        return await message.reply_text("You don't own any federation. Create one with /newfed first.")

    fed_id = fed["fed_id"]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delfed:{fed_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_delfed")]
    ])
    await message.reply_text(
        f"Are you sure you want to delete your federation **{fed['fed_name']}**?\n"
        "This will remove all associated groups, bans, and subscriptions.",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def rename_fed(client: Client, message: Message):
    # PM-only restriction
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command can only be used in private chat with the bot.")
        return

    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")

    user_id = message.from_user.id
    fed = await federations_col.find_one({"owner_id": user_id})
    if not fed:
        return await message.reply_text("You don't own any federation. Create one with /newfed first.")

    args = get_args(message)
    if not args:
        return await message.reply_text("Usage: /renamefed <new_name>")

    new_name = " ".join(args)
    await federations_col.update_one(
        {"fed_id": fed["fed_id"]},
        {"$set": {"fed_name": new_name}}
    )
    await message.reply_text(f"Your federation has been renamed to **{new_name}**.", parse_mode=enums.ParseMode.MARKDOWN)


async def transfer_fed(client: Client, message: Message):
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id
    fed = await federations_col.find_one({"owner_id": user_id})
    if not fed:
        return await message.reply_text("You don't own any federation. Create one with /newfed first.")
    
    target = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user
    else:
        args = get_args(message)
        if not args:
            return await message.reply_text("Usage: /transferfed <user> (reply, username or ID)")
        user_input = args[0]
        try:
            target = await client.get_users(user_input)
        except:
            return await message.reply_text("Target user not found.")
    
    if not target:
        return await message.reply_text("Target user not found.")
    
    await federations_col.update_one(
        {"fed_id": fed["fed_id"]},
        {"$set": {"owner_id": target.id}}
    )
    await message.reply_text(f"Federation ownership transferred to {target.mention}.")        

async def my_feds(client: Client, message: Message):
    # PM-only restriction
    if message.chat.type != enums.ChatType.PRIVATE:
        await message.reply_text("❌ This command can only be used in private chat with the bot.")
        return

    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")

    user_id = message.from_user.id
    owner_feds = federations_col.find({"owner_id": user_id})
    owner_count = await federations_col.count_documents({"owner_id": user_id})
    admin_feds = federations_col.find({"admins": user_id})
    admin_count = await federations_col.count_documents({"admins": user_id})

    if owner_count == 0 and admin_count == 0:
        return await message.reply_text("❌ You are not the owner or admin of any federation.")

    text = f"<b>📋 Your Federations</b>\n\n"
    if owner_count > 0:
        text += f"<b>👑 Owner ({owner_count}):</b>\n"
        async for fed in owner_feds:
            groups = await fed_membership_col.count_documents({"fed_id": fed["fed_id"]})
            bans = await fban_list_col.count_documents({"fed_id": fed["fed_id"]})
            text += f"• <b>{fed['fed_name']}</b>\n"
            text += f"  🆔 <code>{fed['fed_id']}</code>\n"
            text += f"  🏢 Groups: {groups} | 🚫 Bans: {bans}\n\n"
    if admin_count > 0:
        text += f"<b>🛡️ Admin ({admin_count}):</b>\n"
        async for fed in admin_feds:
            groups = await fed_membership_col.count_documents({"fed_id": fed["fed_id"]})
            bans = await fban_list_col.count_documents({"fed_id": fed["fed_id"]})
            text += f"• <b>{fed['fed_name']}</b>\n"
            text += f"  🆔 <code>{fed['fed_id']}</code>\n"
            text += f"  🏢 Groups: {groups} | 🚫 Bans: {bans}\n\n"
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def fban_list(client: Client, message: Message):
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id
    fed = await federations_col.find_one({"owner_id": user_id})
    if not fed:
        return await message.reply_text("You don't own any federation. Create one with /newfed first.")
    
    fed_id = fed["fed_id"]
    bans = fban_list_col.find({"fed_id": fed_id})
    count = await fban_list_col.count_documents({"fed_id": fed_id})
    if count == 0:
        return await message.reply_text("No banned users in your federation.")
    
    text = f"<b>Banned users in {fed['fed_name']}:</b> ({count})\n\n"
    async for ban in bans:
        user_link = f"<a href='tg://user?id={ban['user_id']}'>{ban.get('user_name', 'Unknown')}</a>"
        text += f"• {user_link} (<code>{ban['user_id']}</code>)\n"
        text += f"  Reason: {ban.get('reason', 'No reason')}\n\n"
        if len(text) > 3500:
            text += "... and more."
            break
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

async def feddemoteme(client: Client, message: Message):

    # 1. Only PM check
    if message.chat.type != enums.ChatType.PRIVATE:
        return await message.reply_text("❌ This command only works in PM.")

    # 2. Anonymous admin check
    if not message.from_user:
        return await message.reply_text("This command cannot be used by anonymous admins.")
    
    user_id = message.from_user.id

    # 3. Args check (custom message)
    args = get_args(message)
    if not args:
        return await message.reply_text(
            "❌ You need to specify a federation ID to demote yourself from."
        )

    fed_id = args[0]

    # 4. Federation check
    fed = await federations_col.find_one({"fed_id": fed_id})
    if not fed:
        return await message.reply_text("❌ Federation not found.")

    # 5. Owner check
    if fed["owner_id"] == user_id:
        return await message.reply_text(
            "❌ Owner cannot leave. Transfer or delete federation instead."
        )

    # 6. Admin check
    if user_id not in fed.get("admins", []):
        return await message.reply_text("❌ You are not an admin of this federation.")

    # 7. Demote
    await federations_col.update_one(
        {"fed_id": fed_id},
        {"$pull": {"admins": user_id}}
    )

    await message.reply_text("✅ Federation demoted successfully.")

async def captcha_verify(client: Client, callback_query: CallbackQuery) -> None:
    q = callback_query
    _, chat_id, uid = q.data.split(":")
    if q.from_user.id != int(uid):
        await q.answer("This button is not for you.", show_alert=True)
        return
    await client.restrict_chat_member(
        int(chat_id), int(uid),
        ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
    )
    await q.edit_message_text("Verified ✅")

async def filter_handler(client: Client, message: Message):
    if not message:
        return
    # Skip bots
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    text = (message.text or message.caption or "").lower()
    msg_text = text

    # Anonymous admin has no username/fullname
    username = ""
    fullname = ""
    if message.from_user:
        username = (message.from_user.username or "").lower()
        fullname = f"{message.from_user.first_name} {message.from_user.last_name or ''}".lower()

    cursor = filters_col.find({"chat_id": chat_id})
    async for f in cursor:
        keyword = f['keyword'].strip().lower()
        # Lookarounds ensure whole‑word matching (works with spaces)
        pattern = r'(?<!\w)' + re.escape(keyword) + r'(?!\w)'
        if (re.search(pattern, msg_text, re.IGNORECASE) or
            re.search(pattern, username, re.IGNORECASE) or
            re.search(pattern, fullname, re.IGNORECASE)):
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
            await message.stop_propagation()
            return

async def is_anonymous_admin(client: Client, message: Message) -> bool:
    """Return True if the message is from an anonymous admin (sender_chat) and that channel is an admin."""
    if not message.sender_chat:
        return False
    try:
        member = await client.get_chat_member(message.chat.id, message.sender_chat.id)
        return member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
    except Exception:
        return False

async def safety_handler(client: Client, message: Message):
    if not message or not message.from_user or message.from_user.is_bot:
        return

    # ---- NEW: Anonymous admin messages bypass all locks ----
    if message.sender_chat and message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (message.text or message.caption or "").lower()

    # Admins and approved users bypass flood and locks
    if await is_admin(client, message, user_id) or await is_approved(chat_id, user_id):
        return

    # --- NEW: Check fedbanned ---
    if await check_and_punish_fedbanned_user(client, chat_id, user_id, message):
        return

        # Flood control
    flood_timer_enabled = await get_chat_setting(chat_id, "flood_timer_enabled", "0") == "1"
    if flood_timer_enabled:
        # Timer‑based flood detection
        count = int(await get_chat_setting(chat_id, "flood_timer_count", "5"))
        seconds = int(await get_chat_setting(chat_id, "flood_timer_seconds", "10"))
        key = (chat_id, user_id)
        now = time.time()
        # Retrieve the user's timestamp list from a separate timer tracker
        # We'll use a new dict: flood_timer_tracker
        timestamps = flood_timer_tracker.get(key, [])
        # Remove timestamps older than the allowed window
        timestamps = [ts for ts in timestamps if now - ts < seconds]
        timestamps.append(now)
        flood_timer_tracker[key] = timestamps
        if len(timestamps) > count:
            mode = (await get_chat_setting(chat_id, "flood_mode", "mute")).lower()
            duration = int(await get_chat_setting(chat_id, "flood_duration", "60"))
            until = int(time.time()) + duration if mode in ("tban", "tmute") else None
            with suppress(Exception):
                await message.delete()
            if mode == "ban":
                await client.ban_chat_member(chat_id, user_id)
            elif mode == "mute":
                await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
            
            until = int(time.time()) + duration if mode in ("tban", "tmute") else None
            if mode == "tban":
                await client.ban_chat_member(chat_id, user_id, until_date=to_datetime(until))
            elif mode == "tmute":
                await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False), until_date=to_datetime(until))
            await message.stop_propagation()
            return
    else:
        # Original consecutive flood detection
        if await get_chat_setting(chat_id, "flood_enabled", "1") == "1":
            key = (chat_id, user_id)
            now = time.time()
            dq = flood_tracker[key]
            dq.append(now)
            while dq and now - dq[0] > FLOOD_WINDOW:
                dq.popleft()
            if len(dq) >= int(await get_chat_setting(chat_id, "flood_limit", "5")):
                mode = (await get_chat_setting(chat_id, "flood_mode", "mute")).lower()
                with suppress(Exception):
                    await message.delete()
                if mode == "ban":
                    await client.ban_chat_member(chat_id, user_id)
                elif mode == "mute":
                    await client.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
                await message.stop_propagation()
                return

async def nightmode_handler(client: Client, message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not message.from_user or message.from_user.is_bot:
        return
    # Skip if message is from anonymous admin (sender_chat)
    if message.sender_chat and message.from_user is None:
        return

    chat_id = message.chat.id
    if not await get_nightmode_enabled(chat_id):
        return
    start = await get_nightmode_start(chat_id)
    end = await get_nightmode_end(chat_id)
    if not is_night_time(start, end):
        return

    user_id = message.from_user.id
    # Skip admins and approved users
    if await is_admin(client, message, user_id) or await is_approved(chat_id, user_id):
        return

    # Check if message contains media
    media_types = [message.photo, message.video, message.animation, message.sticker, message.voice, message.audio, message.document]
    if not any(media_types):
        return

    # Delete the message
    try:
        await message.delete()
        warn_msg = await message.reply_text(
            f"⚠️ {message.from_user.mention}, media messages are not allowed during night time (Kolkata {start} – {end}).",
            parse_mode=enums.ParseMode.HTML
        )
        asyncio.create_task(delete_after_delay(warn_msg, 5))
    except Exception as e:
        print(f"Night mode deletion failed: {e}")

async def pin_event_handler(client: Client, message: Message):
    pinned = message.pinned_message
    if not pinned:
        return
    chat_id = message.chat.id

    # If the pinned message is from a channel (anonymous admin or linked channel)
    if pinned.sender_chat:
        if await get_antichannel_enabled(chat_id):
            # First try to restore the permanent pin
            permapin_id = await get_chat_setting(chat_id, "permapin_id", "0")
            if permapin_id != "0":
                permapin_id = int(permapin_id)
                try:
                    # Check if the permapin message still exists
                    await client.get_messages(chat_id, permapin_id)
                    # Unpin the channel message and restore the permapin
                    await client.unpin_chat_message(chat_id, pinned.id)
                    await client.pin_chat_message(chat_id, permapin_id, disable_notification=True)
                    return
                except Exception:
                    # permapin message no longer exists – clear the setting
                    await set_chat_setting(chat_id, "permapin_id", "0")
            # Fallback to the last manually pinned message
            last_pin_id = await get_last_manual_pin(chat_id)
            if last_pin_id:
                try:
                    await client.get_messages(chat_id, last_pin_id)
                    await client.unpin_chat_message(chat_id, pinned.id)
                    await client.pin_chat_message(chat_id, last_pin_id, disable_notification=True)
                except Exception:
                    await client.unpin_chat_message(chat_id, pinned.id)
                    await set_last_manual_pin(chat_id, 0)
            else:
                await client.unpin_chat_message(chat_id, pinned.id)
    else:
        # Manual pin from a user – update the last manual pin (does not affect permapin)
        await set_last_manual_pin(chat_id, pinned.id)

async def cleanlinked_handler(client: Client, message: Message):
    # Only in groups, ignore if not from a channel
    if message.chat.type not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return
    if not message.sender_chat:
        return

    # ---- NEW: Anonymous admin messages should not be deleted ----
    if message.from_user is None and message.sender_chat:
        return

    if await get_cleanlinked_enabled(message.chat.id):
        try:
            await message.delete()
        except Exception:
            pass
        
async def service_cleaner(client: Client, message: Message) -> None:
    if message.service and await get_chat_setting(message.chat.id, "clean_service", "0") == "1":
        with suppress(Exception):
            await message.delete()

async def clean_cmd_toggle(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "clean_cmd_enabled", "cleancommands", verified, admin_id)

async def clean_cmd_for(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await clean_cmd_for(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "cleanfor",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if len(args) < 2 or args[1].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /cleanfor <command> on|off")
        return
    cmd = args[0].lower().lstrip("/")
    enabled = 1 if args[1].lower() == "on" else 0
    await clean_cmd_rules_col.update_one(
        {"chat_id": message.chat.id, "command": cmd},
        {"$set": {"enabled": enabled}},
        upsert=True
    )
    await message.reply_text(f"Clean for /{cmd}: {'on' if enabled else 'off'}")

async def clean_cmd_handler(client: Client, message: Message) -> None:
    if not message or not message.text:
        return
    chat_id = message.chat.id
    if await get_chat_setting(chat_id, "clean_cmd_enabled", "0") != "1":
        return
    cmd = message.command[0].lower()
    doc = await clean_cmd_rules_col.find_one({"chat_id": chat_id, "command": cmd})
    if doc is not None and doc.get("enabled") == 0:
        return
    with suppress(Exception):
        await message.delete()

async def reload_bot(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await reload_bot(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "reload",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    flood_tracker.clear()
    active_users.clear()
    flood_timer_tracker.clear()
    await message.reply_text("Reloaded runtime caches.")

async def language(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await language(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "language",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    args = get_args(message)
    if not args or args[0].lower() not in {"en", "hi"}:
        await message.reply_text("Usage: /language en|hi")
        return
    await set_chat_setting(message.chat.id, "language", args[0].lower())
    await message.reply_text("Language updated.")

async def disable_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await disable_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "disable",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /disable <command>")
        return
    cmd = args[0].lower().lstrip("/")
    await disabled_commands_col.update_one(
        {"chat_id": message.chat.id, "command": cmd},
        {"$set": {"command": cmd}},
        upsert=True
    )
    await message.reply_text(f"Disabled /{cmd}")

async def enable_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await enable_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "enable",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Admin check for non‑verified or regular admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return

    args = get_args(message)
    if not args:
        await message.reply_text("Usage: /enable <command>")
        return
    cmd = args[0].lower().lstrip("/")
    await disabled_commands_col.delete_one({"chat_id": message.chat.id, "command": cmd})
    await message.reply_text(f"Enabled /{cmd}")

async def disabled_cmds(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")
    
    cursor = disabled_commands_col.find({"chat_id": message.chat.id})
    docs = await cursor.to_list(length=1000)
    cmds = [doc["command"] for doc in docs]
    await message.reply_text("Disabled commands: " + (", ".join(cmds) if cmds else "none"))                        

async def disabled_cmd_guard(client: Client, message: Message) -> None:
    if not message or not message.text:
        return
    if await is_admin(client, message):
        return
    cmd = message.command[0].lower()
    if cmd in {"enable", "disable", "disabled"}:
        return
    doc = await disabled_commands_col.find_one({"chat_id": message.chat.id, "command": cmd})
    if doc:
        with suppress(Exception):
            await message.reply_text("This command is disabled in this chat.")
        message.stop_propagation()

async def autoantiraid_cmd(client: Client, message: Message, verified=False, admin_id: int = None):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await autoantiraid_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "autoantiraid",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return
        # Permission check: need change info (like antiraid)
        user_id = admin_id if admin_id else message.from_user.id
        if not await user_has_permission(client, message.chat.id, user_id, "can_change_info"):
            await message.reply_text("❌ You need 'Change Group Info' permission.")
            return

    await show_autoantiraid_menu(client, message, message.chat.id, edit=False)

async def show_autoantiraid_menu(client, source, chat_id, edit=True):
    threshold = await get_autoantiraid_threshold(chat_id)

    status = "✅ Enabled" if threshold > 0 else "❌ Disabled"
    text = (
        f"**⚙️ Autoantiraid Settings**\n\n"
        f"Status: {status}\n"
        f"Threshold: {threshold} joins per minute\n\n"
        f"When enabled, if {threshold} or more users join within 60 seconds, "
        f"all those users will be punished using the action selected in antiraid's punish menu."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Enable", callback_data=f"autoantiraid:enable:{chat_id}"),
         InlineKeyboardButton("Disable", callback_data=f"autoantiraid:disable:{chat_id}")],
        [InlineKeyboardButton(f"🔢 Set Threshold ({threshold})", callback_data=f"autoantiraid:set_threshold:{chat_id}")],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    if edit and hasattr(source, 'edit_message_text'):
        try:
            await source.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass
    else:
        await source.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)

async def antiraid_cmd(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    """Manage antiraid settings via inline menu."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # Anonymous admin detection
    if not verified and message.from_user is None and message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await antiraid_cmd(client, message, verified=True, admin_id=0)
        else:
            action_id = str(uuid.uuid4())
            pending_admin_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "action": "antiraid",
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔐 Click To Prove Admin", callback_data=f"prove_admin:{action_id}")
            ]])
            await message.reply_text(
                "⚠️ Anonymous admin detected.\nPress button to confirm admin identity.",
                reply_markup=keyboard
            )
            return

    # Permission checks for normal (non‑anonymous) admins
    if not (verified and message.from_user is None):
        if not await require_admin(client, message):
            return
        user_id = message.from_user.id
        if not await user_has_permission(client, message.chat.id, user_id, "can_change_info"):
            await message.reply_text("❌ You need 'Change Group Info' permission to manage antiraid.")
            return
        if not await user_has_permission(client, message.chat.id, user_id, "can_restrict_members"):
            await message.reply_text("❌ You need 'Ban Members' permission to manage antiraid.")
            return
    elif verified and admin_id is not None and admin_id != 0:
        # Proven admin with a real user ID
        if not await user_has_permission(client, message.chat.id, admin_id, "can_change_info"):
            await message.reply_text("❌ You need 'Change Group Info' permission to manage antiraid.")
            return
        if not await user_has_permission(client, message.chat.id, admin_id, "can_restrict_members"):
            await message.reply_text("❌ You need 'Ban Members' permission to manage antiraid.")
            return
    # Verified anonymous admin (admin_id == 0) → skip permission checks

    await show_antiraid_menu(client, message, message.chat.id, edit=False)

async def show_antiraid_menu(client, source, chat_id, edit=True):
    """Display the antiraid settings inline keyboard."""
    enabled = await get_antiraid_enabled(chat_id)
    duration = await get_antiraid_duration(chat_id)
    expiry = await get_antiraid_expiry(chat_id)
    now = int(time.time())
    if enabled and expiry > now:
        status_text = f"✅ **Enabled** (expires in {format_duration(expiry - now)})"
    elif enabled:
        status_text = "✅ **Enabled** (no expiry)"
    else:
        status_text = "❌ **Disabled**"

    enable_text = "Enable" if not enabled else "🟢 Enable"
    disable_text = "🔴 Disable" if not enabled else "Disable"
    duration_text = format_duration(duration) if duration else "Not set"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(enable_text, callback_data=f"antiraid:enable:{chat_id}"),
         InlineKeyboardButton(disable_text, callback_data=f"antiraid:disable:{chat_id}")],
        [InlineKeyboardButton(f"⏱ Set Duration ({duration_text})", callback_data=f"antiraid:set_duration:{chat_id}")],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])

    text = (
        f"**🛡 Antiraid Settings**\n\n"
        f"Status: {status_text}\n"
        f"Duration: {duration_text}\n\n"
        f"When enabled, suspected raids will be handled automatically.\n"
        f"Use the Set Duration button to specify how long antiraid should stay active."
    )

    if edit and hasattr(source, 'edit_message_text'):
        try:
            await source.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        except MessageNotModified:
            pass  # Ignore if the message content is identical
    else:
        await source.reply_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        

async def connection(client: Client, message: Message) -> None:
    fed = await get_fed(message.chat.id)
    log_chan = await get_chat_setting(message.chat.id, "log_channel", "not_set")
    txt = f"Chat: <code>{message.chat.id}</code>\nFed: <code>{fed or 'none'}</code>\nLog channel: <code>{log_chan}</code>"
    await message.reply_text(txt, parse_mode=enums.ParseMode.HTML)

# export_data, import_data, ddata, deldata removed as per json removal requirement.

async def set_global_log(client: Client, message: Message):
    """Only bot owner or sudo can set log channel for any chat (by providing chat ID)."""
    if not await is_owner_or_sudo(message.from_user.id):
        return await message.reply_text("❌ Only bot owner or sudo users can use this command.")
    
    args = get_args(message)
    if len(args) < 2:
        return await message.reply_text("Usage: `/setglog <target_chat_id> <log_channel_id>`")
    try:
        target_chat_id = int(args[0])
        log_channel_id = args[1]
    except ValueError:
        return await message.reply_text("❌ Chat ID must be an integer.")
    
    await set_chat_setting(target_chat_id, "log_channel", log_channel_id)
    await message.reply_text(f"✅ Global log channel for chat `{target_chat_id}` set to `{log_channel_id}`.")
    
async def log_channel(client: Client, message: Message) -> None:
    cid = await get_chat_setting(message.chat.id, "log_channel", "not_set")
    await message.reply_text(f"Log channel: {cid}")

async def mics(client: Client, message: Message, verified=False, admin_id: int = None) -> None:
    await generic_toggle(client, message, "mics_enabled", "mics", verified, admin_id)
    
# --- Tagging commands ---

async def canceltag(client: Client, message: Message) -> None:
    # Only groups allowed
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return
    
    # --- NEW: Bot must be admin and have restrict members permission ---
    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")


    if not await require_admin(client, message):
        return
    chat_id = message.chat.id
    if chat_id in active_tagging and active_tagging[chat_id]:
        active_tagging[chat_id] = False
        await message.reply_text("<b>Tagging process stopped.</b>", parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text("No tagging in progress.")

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

async def utag(client: Client, message: Message) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # --- Anonymous admin handling ---
    if message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await _utag_main(client, message)
        else:
            action_id = str(uuid.uuid4())
            pending_warn_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "target_id": None,
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔐 Click to prove admin", callback_data=f"proveatag:{action_id}")]]
            )
            msg = await message.reply_text(
                "⚠️ Anonymous / Enormous admin detected.\nPress button to confirm identity before tagging.",
                reply_markup=keyboard
            )
            asyncio.create_task(delete_verify_button(msg))
            return

    # --- Normal user admin check ---
    if not await require_admin(client, message):
        return

    await _utag_main(client, message)

async def _utag_main(client: Client, message: Message):
    chat_id = message.chat.id
    if active_tagging.get(chat_id):
        await message.reply_text("A tagging is already in progress.")
        return

    tagged_list = []
    async for member in client.get_chat_members(chat_id, limit=100):
        if not member.user.is_bot and not member.user.is_deleted:
            tagged_list.append((member.user.id, member.user.first_name))

    if not tagged_list:
        await message.reply_text("No users to tag.")
        return

    active_tagging[chat_id] = True
    await message.reply_text("<b>Tagging Started!</b>", parse_mode=enums.ParseMode.HTML)

    args = get_args(message)
    prefix = escape(" ".join(args)) if args else "Attention Members"

    for i in range(0, len(tagged_list), 5):
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
            await asyncio.sleep(3.0)
        except Exception:
            break

    if active_tagging.get(chat_id) is not False:
        await message.reply_text("<b>Tagging Completed!</b>", parse_mode=enums.ParseMode.HTML)
    active_tagging[chat_id] = False


async def atag(client: Client, message: Message) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("This command only works in groups.")
        return

    if not await bot_is_admin(client, message.chat.id):
        return await message.reply_text("❌ I am not admin in this chat.")

    # --- Anonymous admin handling ---
    if message.sender_chat:
        if await get_anonadmin_enabled(message.chat.id):
            return await _atag_main(client, message)
        else:
            action_id = str(uuid.uuid4())
            pending_warn_actions[action_id] = {
                "chat_id": message.chat.id,
                "message": message,
                "target_id": None,
                "time": time.time(),
                "used": False
            }
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔐 Click to prove admin", callback_data=f"proveatag:{action_id}")]]
            )
            msg = await message.reply_text(
                "⚠️ Anonymous / Enormous admin detected.\nPress button to confirm identity before admin tagging.",
                reply_markup=keyboard
            )
            asyncio.create_task(delete_verify_button(msg))
            return

    # --- Normal user admin check ---
    if not await require_admin(client, message):
        return

    await _atag_main(client, message)

async def _atag_main(client: Client, message: Message):
    chat_id = message.chat.id
    admins = []
    async for a in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if not a.user.is_bot:
            admins.append(a.user.mention())

    if not admins:
        await message.reply_text("No human admins found.")
        return

    active_tagging[chat_id] = True
    await message.reply_text("<b>Admin Tagging Started...</b>", parse_mode=enums.ParseMode.HTML)

    for i in range(0, len(admins), 5):
        if not active_tagging.get(chat_id):
            break
        chunk = admins[i:i + 5]
        await client.send_message(
            chat_id,
            "<b>Hello admins :</b>\n" + " ".join(chunk),
            parse_mode=enums.ParseMode.HTML,
            reply_to_message_id=message.reply_to_message.id if message.reply_to_message else None
        )
        await asyncio.sleep(2.0)

    active_tagging[chat_id] = False
    await message.reply_text("<b>Admin tagging completed!</b>")

async def track_activity(client: Client, message: Message) -> None:
    if message.from_user and not message.from_user.is_bot and message.chat:
        # Update user activity in user_activity_col (already there)
        dq = active_users[message.chat.id]
        uid = message.from_user.id
        if uid in dq:
            with suppress(ValueError):
                dq.remove(uid)
        dq.append(uid)
        await user_activity_col.update_one(
            {"chat_id": message.chat.id, "user_id": uid},
            {"$inc": {"msg_count": 1}, "$set": {"last_seen": int(time.time())}},
            upsert=True
        )

        # --- NEW: Update active chats collection ---
        await active_chats_col.update_one(
            {"chat_id": message.chat.id},
            {
                "$set": {
                    "title": message.chat.title or "",
                    "type": message.chat.type.value,
                    "last_activity": int(time.time())
                }
            },
            upsert=True
        )
    message.continue_propagation()

# --- Join Request Handling ---
async def join_request_handler(client: Client, update: ChatJoinRequest):
    """Triggered when a user requests to join a group. Sends a notification with approve/reject buttons."""
    chat = update.chat
    user = update.from_user

    # Format user info
    name = user.first_name + (f" {user.last_name}" if user.last_name else "")
    username = f"@{user.username}" if user.username else "No username"
    user_id = user.id

    text = (
        f"📥 **New Join Request**\n"
        f"👤 **Name:** {name}\n"
        f"🆔 **Username:** {username}\n"
        f"🔢 **ID:** `{user_id}`\n\n"
        f"Note - This message was automatically deleted after 10 minutes."   
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"jr:approve:{chat.id}:{user_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"jr:reject:{chat.id}:{user_id}")
        ]
    ])

    msg = await client.send_message(
        chat.id,
        text,
        reply_markup=buttons,
        parse_mode=enums.ParseMode.MARKDOWN
    )

    # Auto-delete after 10 minutes (600 seconds)
    asyncio.create_task(delete_after_delay(msg, 600))

async def delete_after_delay(message, delay: int):
    """Delete a message after a given delay (seconds)."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass  # Ignore if already deleted

async def join_request_callback(client: Client, callback_query: CallbackQuery):
    """Handle approve/reject button clicks."""
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "jr":
        await callback_query.answer("Invalid data.", show_alert=True)
        return

    action = parts[1]          # "approve" or "reject"
    chat_id = int(parts[2])
    user_id = int(parts[3])
    admin_id = callback_query.from_user.id

    # 1. Verify the clicking user is an admin in that chat
    try:
        member = await client.get_chat_member(chat_id, admin_id)
        if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await callback_query.answer("❌ Only admins can do this.", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    # 2. Perform the action
    try:
        if action == "approve":
            await client.approve_chat_join_request(chat_id, user_id)
            await callback_query.answer("✅ User approved!", show_alert=False)
            await callback_query.edit_message_text(
                callback_query.message.text + "\n\n✅ Approved by admin.",
                reply_markup=None
            )
        elif action == "reject":
            await client.decline_chat_join_request(chat_id, user_id)
            await callback_query.answer("❌ User rejected!", show_alert=False)
            await callback_query.edit_message_text(
                callback_query.message.text + "\n\n❌ Rejected by admin.",
                reply_markup=None
            )
    except Exception as e:
        await callback_query.answer(f"❌ Error: {e}", show_alert=True)

async def join_vc_callback(client: Client, query: CallbackQuery) -> None:
    data = query.data.replace("joinvc_", "")
    allowed_users = data.split("_")
    if str(query.from_user.id) not in allowed_users:
        return await query.answer("❌ This invite is not for you!", show_alert=True)

    chat = query.message.chat
    if chat.username:
        vc_link = f"https://t.me/{chat.username}?videochat"
        await query.answer("🎙 Opening VC panel...")
        await client.send_message(query.from_user.id, f"Click here to join the VC: {vc_link}")
        return await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ JOIN NOW", url=vc_link)]])
        )
    else:
        await query.answer("🎙 Invited! Click the 'Join' button at the top of the screen.", show_alert=True)

async def fed_callback_handler(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    if data.startswith("fed:"):
        await callback_query.answer("Federation details loading...", show_alert=True)

async def confirm_delfed_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    user_id = q.from_user.id
    data = q.data
    if data.startswith("confirm_delfed:"):
        fed_id = data.split(":", 1)[1]
        fed = await federations_col.find_one({"fed_id": fed_id})
        if not fed:
            await q.edit_message_text("Federation already deleted or not found.")
            return
        if fed["owner_id"] != user_id:
            await q.answer("You are not the owner!", show_alert=True)
            return
        await federations_col.delete_one({"fed_id": fed_id})
        await fed_membership_col.delete_many({"fed_id": fed_id})
        await fban_list_col.delete_many({"fed_id": fed_id})
        await federations_col.update_many(
            {"subscribed_feds": fed_id},
            {"$pull": {"subscribed_feds": fed_id}}
        )
        await q.edit_message_text(
            f"Federation **{fed['fed_name']}** has been deleted successfully.",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif data == "cancel_delfed":
        await q.edit_message_text("Deletion cancelled.")

async def bio_callbacks_handler(client: Client, query: CallbackQuery):
    data = query.data
    chat_id = query.message.chat.id

    if data == "del_msg":
        with suppress(Exception):
            await query.message.delete()
        return

    if not await is_admin(client, query.message, query.from_user.id):
        return await query.answer("❌ You are not an admin!", show_alert=True)

    if data.startswith("cfg_") or data.startswith("setwarn_"):
        warn_limit, action, is_enabled = await get_bio_config(chat_id)

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

        elif data == "cfg_warn":
            def get_btn(num):
                return InlineKeyboardButton(f"✅ {num}" if num == warn_limit else str(num), callback_data=f"setwarn_{num}")

            kb = [
                [get_btn(3), get_btn(4), get_btn(5), get_btn(6)],
                [get_btn(7), get_btn(8), get_btn(9), get_btn(10)],
                [InlineKeyboardButton("⬅️ Back", callback_data="cfg_main")]
            ]
            await query.edit_message_text("⚠️ **Select Warning Limit:**", reply_markup=InlineKeyboardMarkup(kb))

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

    elif data.startswith("bio_"):
        parts = data.split("_")
        b_action, target_id = parts[1], int(parts[2])

        if b_action == "allow":
            await bio_allowlist_col.update_one({"user_id": target_id}, {"$set": {"user_id": target_id}}, upsert=True)
            await bio_warns_col.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"✅ User `{target_id}` allowed.")

        elif b_action == "unwarn":
            await bio_warns_col.update_one({"chat_id": chat_id, "user_id": target_id}, {"$inc": {"count": -1}})
            await query.answer("🛡 Warning cleared!")
            with suppress(Exception):
                await query.message.delete()

        elif b_action == "unmute":
            with suppress(Exception):
                await client.restrict_chat_member(
                    chat_id, target_id,
                    ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True
                    )
                )
            await bio_warns_col.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"🔊 User `{target_id}` Unmuted.")

        elif b_action == "unban":
            with suppress(Exception):
                await client.unban_chat_member(chat_id, target_id)
            await bio_warns_col.delete_one({"chat_id": chat_id, "user_id": target_id})
            await query.edit_message_text(f"🔓 User `{target_id}` Unbanned.")

async def warnmode_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    if data == "warnmode:back":
        # Go back to main warnmode menu
        await warnmode_cmd(client, q.message)
        # Re-fetch current values and rebuild
        chat_id = q.message.chat.id
        current_action = await get_warn_action(chat_id)
        current_limit = await get_warn_limit(chat_id)
        text = f"**⚙️ Warn Action Configuration**\n\nCurrent action: **{current_action.upper()}**\nCurrent limit: **{current_limit}**\nSelect new action:"
        actions = ["ban", "mute", "kick", "tban", "tmute"]
        buttons = []
        row = []
        for i, a in enumerate(actions):
            display = f"✅ {a.upper()}" if a == current_action else a.upper()
            row.append(InlineKeyboardButton(display, callback_data=f"warnmode:{a}"))
            if len(row) == 2 or i == len(actions)-1:
                buttons.append(row)
                row = []
        buttons.append([InlineKeyboardButton(f"🔢 Change Warn Limit ({current_limit})", callback_data="warnlimit:menu")])
        buttons.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.MARKDOWN)
        return

    if not data.startswith("warnmode:"):
        return
    action = data.split(":", 1)[1]
    chat_id = q.message.chat.id
    user_id = q.from_user.id

    if not await is_admin(client, q.message, user_id):
        await q.answer("❌ You are not an admin!", show_alert=True)
        return

    # If the action is temporary, show number pad
    if action in ("tban", "tmute"):
        session_id = str(uuid.uuid4())
        pending_warnmode[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "action": action,
            "duration_min_str": "",
            "message_id": q.message.id,
        }
        text = f"**Selected action:** {action.upper()}\n\nEnter duration in **minutes**:\n\nCurrent: 0"
        keyboard = build_number_pad_warnmode("")
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()
        return

    # For non‑temporary actions, set directly
    await set_warn_action(chat_id, action)
    # Also clear any previous duration if exists
    await set_chat_setting(chat_id, "warn_duration", "0")
    await q.answer(f"✅ Warn action set to {action.upper()}", show_alert=False)

    # Refresh message
    current_action = action
    current_limit = await get_warn_limit(chat_id)
    text = f"**⚙️ Warn Action Configuration**\n\nCurrent action: **{current_action.upper()}**\nCurrent limit: **{current_limit}**\nSelect new action:"
    actions = ["ban", "mute", "kick", "tban", "tmute"]
    buttons = []
    row = []
    for i, a in enumerate(actions):
        display = f"✅ {a.upper()}" if a == current_action else a.upper()
        row.append(InlineKeyboardButton(display, callback_data=f"warnmode:{a}"))
        if len(row) == 2 or i == len(actions)-1:
            buttons.append(row)
            row = []
    buttons.append([InlineKeyboardButton(f"🔢 Change Warn Limit ({current_limit})", callback_data="warnlimit:menu")])
    buttons.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
    keyboard = InlineKeyboardMarkup(buttons)
    try:
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    except:
        pass

async def warnlimit_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    user_id = q.from_user.id
    chat_id = q.message.chat.id

    # Verify admin
    if not await is_admin(client, q.message, user_id):
        await q.answer("❌ You are not an admin!", show_alert=True)
        return

    if data == "warnlimit:menu":
        # Show number picker (3-10)
        current = await get_warn_limit(chat_id)
        buttons = []
        row = []
        for i in range(3, 11):
            btn_text = f"✅ {i}" if i == current else str(i)
            row.append(InlineKeyboardButton(btn_text, callback_data=f"warnlimit:set:{i}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="warnmode:back")])
        await q.edit_message_text("🔢 **Select new warn limit:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("warnlimit:set:"):
        new_limit = int(data.split(":")[2])
        await set_warn_limit(chat_id, new_limit)
        await q.answer(f"✅ Warn limit set to {new_limit}", show_alert=False)
        # Go back to warnmode menu
        # Re-fetch current action
        current_action = await get_warn_action(chat_id)
        current_limit = new_limit
        text = f"**⚙️ Warn Action Configuration**\n\nCurrent action: **{current_action.upper()}**\nCurrent limit: **{current_limit}**\nSelect new action:"
        actions = ["ban", "mute", "kick", "tban", "tmute"]
        buttons = []
        row = []
        for i, a in enumerate(actions):
            display = f"✅ {a.upper()}" if a == current_action else a.upper()
            row.append(InlineKeyboardButton(display, callback_data=f"warnmode:{a}"))
            if len(row) == 2 or i == len(actions)-1:
                buttons.append(row)
                row = []
        buttons.append([InlineKeyboardButton(f"🔢 Change Warn Limit ({current_limit})", callback_data="warnlimit:menu")])
        buttons.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.MARKDOWN)

async def remove_warn_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    parts = data.split(":")
    if len(parts) != 3:
        await q.answer("Invalid data.", show_alert=True)
        return
    _, chat_id_str, user_id_str = parts
    chat_id = int(chat_id_str)
    user_id = int(user_id_str)

    # --- Check if user is admin in that chat ---
    try:
        member = await client.get_chat_member(chat_id, q.from_user.id)
        if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await q.answer("Only admins can remove warns!", show_alert=True)
            return
    except Exception:
        await q.answer("Failed to verify admin status.", show_alert=True)
        return

    # --- Permission check: require ban rights (can_restrict_members) ---
    if member.status != enums.ChatMemberStatus.OWNER:
        if not (member.privileges and member.privileges.can_restrict_members):
            await q.answer("❌ You need ban rights to remove warns.", show_alert=True)
            return

    # --- Get target user info (for message) ---
    try:
        target_user = await client.get_users(user_id)
        target_mention = target_user.mention
    except:
        target_mention = f"<code>{user_id}</code>"

    admin_mention = q.from_user.mention

    # --- Get current warns array ---
    doc = await warns_col.find_one({"chat_id": chat_id, "user_id": user_id})
    if not doc or not doc.get("warns"):
        await q.answer("This user has no warns.", show_alert=True)
        return

    # Pop the last warning
    await warns_col.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$pop": {"warns": 1}}
    )

    # If array becomes empty, delete the document
    remaining = await warns_col.find_one({"chat_id": chat_id, "user_id": user_id})
    if remaining and not remaining.get("warns"):
        await warns_col.delete_one({"chat_id": chat_id, "user_id": user_id})

    await q.answer("✅ Warning removed!", show_alert=False)

    # --- Remove inline button from original message ---
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except:
        pass

    # --- Announce in the chat ---
    await client.send_message(
        chat_id,
        f"🛡 Admin {admin_mention} has removed {target_mention}'s warning.",
        parse_mode=enums.ParseMode.HTML
    )

async def resetall_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    parts = data.split(":")
    if len(parts) != 3:
        await q.answer("Invalid data.", show_alert=True)
        return

    action, chat_id_str = parts[1], parts[2]
    chat_id = int(chat_id_str)

    # Owner check in callback
    try:
        member = await client.get_chat_member(chat_id, q.from_user.id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await q.answer("❌ Only the group owner can reset all warns.", show_alert=True)
            return
    except Exception:
        await q.answer("❌ Could not verify owner status.", show_alert=True)
        return

    if action == "cancel":
        await q.edit_message_text("❌ Operation cancelled.")
        await q.answer()
        return

    if action == "confirm":
        result = await warns_col.delete_many({"chat_id": chat_id})
        deleted_count = result.deleted_count
        await q.edit_message_text(f"✅ All warns reset for this group.\nDeleted {deleted_count} warning records.")
        await q.answer("✅ All warns cleared!", show_alert=False)

async def prove_warn_callback(client: Client, callback_query: CallbackQuery):

    data = callback_query.data.split(":")
    action_id = data[1]

    action = pending_warn_actions.get(action_id)

    if not action:
        return await callback_query.answer("Action expired.", show_alert=True)

    # expiry protection
    if time.time() - action["time"] > 300:
        del pending_warn_actions[action_id]
        return await callback_query.answer("Request expired.", show_alert=True)

    # double click protection
    if action["used"]:
        return await callback_query.answer(
            "⚠️ Action already verified.",
            show_alert=True
        )

    chat_id = action["chat_id"]

    member = await client.get_chat_member(chat_id, callback_query.from_user.id)

    # fake click protection
    if member.status not in [
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER
    ]:
        return await callback_query.answer(
            "❌ Only admins can confirm this action.",
            show_alert=True
        )

    # Permission check for warning actions
    if not await user_has_permission(client, chat_id, callback_query.from_user.id, "can_restrict_members"):
        await callback_query.answer("❌ You do not have ban rights (can_restrict_members permission required).", show_alert=True)
        return

    action["used"] = True

    msg = action["message"]

    t = action.get("type")

    if t == "dwarn":
        await warn_common(client, msg, delete_cmd=True, silent=False, delete_replied=True, admin_id=callback_query.from_user.id)

    elif t == "swarn":
        await warn_common(client, msg, delete_cmd=True, silent=True, delete_replied=False, admin_id=callback_query.from_user.id)

    else:
        await warn_common(client, msg, admin_id=callback_query.from_user.id)

    try:
        await callback_query.message.delete()
    except:
        pass

    del pending_warn_actions[action_id]

async def unapproveall_callback(client, callback_query):
    data = callback_query.data
    _, action, chat_id = data.split(":")
    chat_id = int(chat_id)
    user_id = callback_query.from_user.id

    # Verify the user is the group owner
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await callback_query.answer("❌ Only the group owner can confirm this.", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Could not verify your status.", show_alert=True)
        return

    if action == "confirm":
        result = await approved_col.delete_many({"chat_id": chat_id})
        count = result.deleted_count
        if count == 0:
            await callback_query.edit_message_text("ℹ️ No approved users to remove.")
        else:
            await callback_query.edit_message_text(f"✅ Removed {count} approved user(s).")
    elif action == "cancel":
        await callback_query.edit_message_text("❌ Action cancelled.")
    await callback_query.answer()

async def join_fed_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 4:
        return
    action = parts[1]
    fed_id = parts[2]
    chat_id = int(parts[3])
    user_id = callback_query.from_user.id

    # Verify the user is still the group owner
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await callback_query.answer("❌ Only the group owner can do this.", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Could not verify owner status.", show_alert=True)
        return

    if action == "confirm":
        # Check if the group is already connected (in case of double click)
        existing = await fed_membership_col.find_one({"chat_id": chat_id})
        if existing:
            await callback_query.answer("❌ This group is already connected to a federation.", show_alert=True)
            await callback_query.edit_message_text("❌ Group already connected to a federation.")
            return

        # Verify federation still exists
        fed = await federations_col.find_one({"fed_id": fed_id})
        if not fed:
            await callback_query.answer("❌ Federation no longer exists.", show_alert=True)
            await callback_query.edit_message_text("❌ Federation not found.")
            return

        # Perform the join
        await fed_membership_col.update_one(
            {"chat_id": chat_id},
            {"$set": {"fed_id": fed_id}},
            upsert=True
        )
        await callback_query.answer("✅ Group connected to federation!", show_alert=False)
        await callback_query.edit_message_text(
            f"✅ Group successfully connected to federation **{fed['fed_name']}**.",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    elif action == "cancel":
        await callback_query.answer("❌ Action cancelled.", show_alert=False)
        await callback_query.edit_message_text("❌ Federation connection cancelled.")

async def leavefed_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "leavefed":
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify the user is still the group owner
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await callback_query.answer("❌ Only the group owner can do this.", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Could not verify owner status.", show_alert=True)
        return

    if action == "confirm":
        result = await fed_membership_col.delete_one({"chat_id": chat_id})
        if result.deleted_count:
            await callback_query.edit_message_text("✅ Group has left the federation.")
        else:
            await callback_query.edit_message_text("❌ Group was not in any federation.")
    else:  # cancel
        await callback_query.edit_message_text("❌ Federation leave cancelled.")
    await callback_query.answer()

async def prove_admin_callback(client: Client, callback_query: CallbackQuery):

    data = callback_query.data.split(":")
    action_id = data[1]

    action = pending_admin_actions.get(action_id)

    if not action:
        return await callback_query.answer("Action expired.", show_alert=True)

    chat_id = action["chat_id"]

    member = await client.get_chat_member(chat_id, callback_query.from_user.id)

    if member.status not in [
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER
    ]:
        return await callback_query.answer(
            "❌ Only admins can confirm this action.",
            show_alert=True
        )

    if action["used"]:
        return await callback_query.answer(
            "⚠️ Action already verified.",
            show_alert=True
        )

    # --- Permission checks based on command ---
    command = action["action"]
    admin_id = callback_query.from_user.id

    if command == "resetwarns":
        if not await user_has_permission(client, chat_id, admin_id, "can_restrict_members"):
            return await callback_query.answer("❌ You do not have ban rights (can_restrict_members required).", show_alert=True)

    elif command == "resetallwarns":
        if member.status != enums.ChatMemberStatus.OWNER:
            return await callback_query.answer("❌ Only the group owner can reset all warns.", show_alert=True)

    elif command in ["warnmode", "warntime"]:
        if not await user_has_permission(client, chat_id, admin_id, "can_change_info"):
            return await callback_query.answer("❌ You do not have 'Change Group Info' permission.", show_alert=True)

    elif command == "anonadmin":
        if member.status != enums.ChatMemberStatus.OWNER:
            return await callback_query.answer("❌ Only the group owner can use this command.", show_alert=True)

    action["used"] = True

    msg = action["message"]
    command = action["action"]

    try:
        await callback_query.message.delete()
    except:
        pass

    if command == "resetwarns":
        await resetwarns(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "warninfo":
        await warninfo_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "unmute":
        await unmute(client, msg, verified=True)

    if command == "resetallwarns":
        await resetallwarns(
        client,
        msg,
        verified=True,
        admin_id=callback_query.from_user.id
    )

    elif command == "warnings":
        await warnings(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "warnmode":
        await warnmode_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "warntime":
        await warntime_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "warns":
        await warns(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "unwarns":
        await unwarn(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "filter":
        await filter_cmd_handler(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "stopfilter":
        await stop_filter_handler(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "setwelcome":
        await setwelcome(client, msg, verified=True)
   
    elif command == "resetwelcome":
        await resetwelcome(client, msg, verified=True)
   
    elif command == "welcome_toggle":
        await welcome_toggle(client, msg, verified=True)
   
    elif command == "setgoodbye":
        await setgoodbye(client, msg, verified=True)
   
    elif command == "resetgoodbye":
        await resetgoodbye(client, msg, verified=True)
   
    elif command == "goodbye_toggle":
        await goodbye_toggle(client, msg, verified=True)

    elif command == "mute":
        await mute(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "dmute":
        await dmute(client, msg, verified=True)

    elif command == "smute":
        await smute(client, msg, verified=True)

    elif command == "tmute":
        await tmute(client, msg, verified=True)

    elif command == "promote":
        await promote(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "demote":
        await demote(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "ban":
        await ban(client, msg, verified=True)

    elif command == "unban":
        await unban(client, msg, verified=True)

    elif command == "dban":
        await dban(client, msg, verified=True)   # msg is the original command message

    elif command == "sban":
        await sban(client, msg, verified=True)

    elif command == "kick":
        await kick(client, msg, verified=True)

    elif command == "dkick":
        await dkick(client, msg, verified=True)

    elif command == "skick":
        await skick(client, msg, verified=True)            
    
    elif command == "approve":
        await approve(client, msg, verified=True)

    elif command == "unapprove":
        await unapprove(client, msg, verified=True)

    elif command == "approved":
        await approved(client, msg, verified=True)

    elif command == "unapproveall":
        await unapproveall(
        client,
        msg,
        verified=True,
        admin_id=callback_query.from_user.id
    )    

    elif command == "lock":
        await lock(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "unlock":
        await unlock(client, msg, verified=True, admin_id=callback_query.from_user.id)
        
    elif command == "unlockall":
        await unlockall(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "lockall":
        await lockall(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "del":
        await del_message(client, msg, verified=True) 

    elif command == "pin":
        await pin_message(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "spin":
        await spin_message(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "unpin":
        await unpin_message(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "nightmode":
        await nightmode_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "allow_bio":
        await allow_bio_user(client, msg, verified=True)

    elif command == "unallow_bio":
        await unallow_bio_user(client, msg, verified=True)
    
    elif command == "aplist_bio":
        await aplist_bio(client, msg, verified=True)
    
    elif command == "bioconfig":
        await bioconfig_cmd(client, msg, verified=True)

    elif command == "setrules":
        await setrules(client, msg, verified=True)

    elif command == "purge":
        await purge(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "spurge":
        await spurge(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "purge_amount":
        await purge_amount(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "purgeuser":
        await purgeuser(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "purgebots":
        await purgebots(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "purgemedia":
        await purgemedia(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "purgelinks":
        await purgelinks(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "fastpurge":
        await fastpurge(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "cancelpurge":
        await cancelpurge(client, msg, verified=True, admin_id=callback_query.from_user.id)    

    elif command == "unpinall":
        await unpinall_message(client, msg, verified=True, admin_id=callback_query.from_user.id)    

    elif command == "permapin":
        await permapin_command(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "antichannelpin":
        await antichannelpin_command(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "cleanlinked":
        await cleanlinked_command(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "admincache":
        await admincache(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "anonadmin":
        await anonadmin_command(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "setgtitle":
        await setgtitle(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "resetgtitle":
        await resetgtitle(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "setgpic":
        await setgpic(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "delgpic":
        await delgpic(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "delgdesc":
        await delgdesc(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "reload":
        await reload_bot(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "language":
        await language(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "disable":
        await disable_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "enable":
        await enable_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "antiraid":
        await antiraid_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "autoantiraid":
        await autoantiraid_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "setnote":
        await setnote(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "delnote":
        await delnote(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "setvcmsg":
        await set_vc_msg(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "vcmsg":
        await vcmsg_toggle(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "setvcinvite":
        await set_vc_invite(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "vcinvite":
        await vcinvite_toggle(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "cleancommands":
        await clean_cmd_toggle(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "cleanfor":
        await clean_cmd_for(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "cleanservice":
        await cleanservice(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "clearflood":
        await clearflood(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "captcha":
        await captcha_toggle(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "mics":
        await mics(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    elif command == "setlogchannel":
        await set_log_channel(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "joinfed":
        await join_fed_group(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "fedinfo":
        await fed_info(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "leavefed":
        await leavefed_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "fedstat":
        await fedstat_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "quietfed":
        await quietfed_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "flood":
        await flood_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "setfloodmode":
        await setfloodmode_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "setfloodtimer":
        await setfloodtimer_cmd(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "utag":
        await utag(client, msg, verified=True, admin_id=callback_query.from_user.id)

    elif command == "atag":
        await atag(client, msg, verified=True, admin_id=callback_query.from_user.id)
    
    del pending_admin_actions[action_id]

async def proveatag_callback(client: Client, callback_query: CallbackQuery):
    if not callback_query.data.startswith("proveatag:"):
        return

    action_id = callback_query.data.split(":")[1]
    action = pending_warn_actions.get(action_id)

    if not action or action["used"]:
        await callback_query.answer("This action has expired or already used.", show_alert=True)
        return

    # Mark as used
    action["used"] = True

    # Delete the inline message completely
    try:
        await callback_query.message.delete()
    except Exception:
        pass  # message might have been deleted already

    # Optional: notify user privately that identity is confirmed
    await callback_query.answer("Admin identity confirmed!", show_alert=True)

    # Continue with normal tagging
    await _utag_main(client, action["message"])

async def custom_command_dispatcher(client: Client, message: Message):
    # Full command text (without /)
    full_cmd = message.text[1:].lower().strip()
    
    # 1. First check for exact match (e.g., "lock all")
    if full_cmd in COMMAND_HANDLERS:
        func = COMMAND_HANDLERS[full_cmd]
        await func(client, message, verified=False, admin_id=None)
        return
    
    # 2. If no exact match, check first word (e.g., "lock")
    first_word = full_cmd.split()[0]
    if first_word in COMMAND_HANDLERS:
        func = COMMAND_HANDLERS[first_word]
        await func(client, message, verified=False, admin_id=None)
        return
    
    # 3. If nothing matches, ignore (or send error message)
    await message.reply_text("Command not found.")

# New callback handler for unpinall confirmation
async def unpinall_confirm_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "unpinall":
        return
    action, user_id_str, chat_id_str = parts[1], parts[2], parts[3]
    stored_user_id = int(user_id_str)
    chat_id = int(chat_id_str)
    clicker_id = callback_query.from_user.id

    # If stored_user_id is 0 (anonymous admin), check if clicker has pin permission.
    # Otherwise, require the same user.
    if stored_user_id == 0:
        # Check if clicker is admin with pin permission
        if not await user_has_permission(client, chat_id, clicker_id, "can_pin_messages"):
            await callback_query.answer("❌ You don't have pin permission.", show_alert=True)
            return
    else:
        if clicker_id != stored_user_id:
            await callback_query.answer("This button is not for you.", show_alert=True)
            return

    # Delete the button message immediately
    await callback_query.message.delete()

    if action == "yes":
        try:
            await client.unpin_all_chat_messages(chat_id)
            await callback_query.message.reply_text("📌 All pinned messages have been unpinned.")
        except Exception as e:
            await callback_query.message.reply_text(f"Failed to unpin all: {e}")
    else:  # action == "no"
        await callback_query.message.reply_text("❌ Unpin all cancelled.")

async def antichannelpin_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "antichannelpin":
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action == "enable":
        await set_antichannel_enabled(chat_id, True)
        await callback_query.answer("✅ Anti-Channel Pin enabled.", show_alert=False)
    elif action == "disable":
        await set_antichannel_enabled(chat_id, False)
        await callback_query.answer("❌ Anti-Channel Pin disabled.", show_alert=False)

    # Refresh message
    enabled = await get_antichannel_enabled(chat_id)
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"antichannelpin:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"antichannelpin:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])
    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, any message pinned from a channel will be automatically replaced with the previously manually pinned message."
        if enabled else
        "When disabled, channel pins will remain as pinned and will not be automatically replaced."
    )
    await callback_query.edit_message_text(
        f"**Anti-Channel Pin**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def cleanlinked_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "cleanlinked":
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action == "enable":
        await set_cleanlinked_enabled(chat_id, True)
        await callback_query.answer("✅ Clean Linked Channel enabled.", show_alert=False)
    elif action == "disable":
        await set_cleanlinked_enabled(chat_id, False)
        await callback_query.answer("❌ Clean Linked Channel disabled.", show_alert=False)

    # Refresh message
    enabled = await get_cleanlinked_enabled(chat_id)
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"cleanlinked:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"cleanlinked:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])
    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, any message sent from the linked channel will be automatically deleted."
        if enabled else
        "When disabled, messages from the linked channel will not be deleted."
    )
    await callback_query.edit_message_text(
        f"**Clean Linked Channel Messages**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def anonadmin_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "anonadmin":
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify the user is the group owner
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status != enums.ChatMemberStatus.OWNER:
            await callback_query.answer("❌ Only the group owner can change this setting.", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Could not verify your status.", show_alert=True)
        return

    if action == "enable":
        await set_anonadmin_enabled(chat_id, True)
        await callback_query.answer("✅ Anonymous admins now have unrestricted access.", show_alert=False)
    elif action == "disable":
        await set_anonadmin_enabled(chat_id, False)
        await callback_query.answer("❌ Anonymous admins now must confirm permissions.", show_alert=False)

    # Refresh message
    enabled = await get_anonadmin_enabled(chat_id)
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"anonadmin:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"anonadmin:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])
    status = "✅ Enabled" if enabled else "❌ Disabled"
    chat = await client.get_chat(chat_id)
    chat_name = chat.title or "This chat"
    description = (
        f"{chat_name} currently allows all anonymous admins to use any admin command without restriction."
        if enabled else
        f"{chat_name} currently requires anonymous admins to press a button to confirm their permission."
    )
    await callback_query.edit_message_text(
        f"**Anonymous Admin Mode**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def quietfed_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "quietfed":
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action == "enable":
        await set_quietfed_enabled(chat_id, True)
        await callback_query.answer("✅ Quiet federation ban mode enabled.", show_alert=False)
    elif action == "disable":
        await set_quietfed_enabled(chat_id, False)
        await callback_query.answer("❌ Quiet federation ban mode disabled.", show_alert=False)

    # Refresh message
    enabled = await get_quietfed_enabled(chat_id)
    enable_text = "🟢 Enable" if enabled else "Enable"
    disable_text = "Disable" if enabled else "🔴 Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"quietfed:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"quietfed:disable:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])
    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        "When enabled, users banned in the federation will be automatically removed "
        "(banned/kicked) whenever they join or send a message, with an explanation."
        if enabled else
        "When disabled, federation-banned users will not be automatically removed."
    )
    await callback_query.edit_message_text(
        f"**Quiet Federation Ban Mode**\n\nCurrent status: {status}\n\n{description}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def flood_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    await q.answer()
    if not await is_admin(client, q.message, q.from_user.id):
        await q.edit_message_text("❌ Admin only.")
        return

    data = q.data
    chat_id = q.message.chat.id

    if data == "flood_enable":
        # Enable flood control: decide which mode to use
        timer_configured = await get_chat_setting(chat_id, "flood_timer_count", "0") != "0"
        if timer_configured:
            await set_chat_setting(chat_id, "flood_timer_enabled", "1")
            await set_chat_setting(chat_id, "flood_enabled", "0")
            msg = "✅ Flood control enabled (Timer Mode)."
        else:
            await set_chat_setting(chat_id, "flood_enabled", "1")
            await set_chat_setting(chat_id, "flood_timer_enabled", "0")
            msg = "✅ Flood control enabled (Consecutive Mode)."
    elif data == "flood_disable":
        await set_chat_setting(chat_id, "flood_enabled", "0")
        await set_chat_setting(chat_id, "flood_timer_enabled", "0")
        msg = "❌ Flood control disabled."
    elif data == "flood_refresh":
        msg = "🔄 Refreshed."
    else:
        return

    enabled = await is_flood_enabled(chat_id)
    mode, params = await get_flood_mode(chat_id)

    if enabled:
        if mode == "timer":
            status_text = (
                f"🟢 Flood control is **enabled** in {q.message.chat.title} (Timer Mode)\n"
                f"Users sending **{params['count']}** or more messages within **{params['seconds']}** seconds will be punished.\n"
                f"Current action: {await get_chat_setting(chat_id, 'flood_mode', 'mute')}"
            )
        else:
            status_text = (
                f"🟢 Flood control is **enabled** in {q.message.chat.title} (Consecutive Mode)\n"
                f"Users sending **{params['limit']}** or more consecutive messages within **{params['window']}** seconds will be punished.\n"
                f"Current action: {await get_chat_setting(chat_id, 'flood_mode', 'mute')}"
            )
    else:
        status_text = (
            f"🔴 Flood control is **disabled** in {q.message.chat.title}.\n"
            "This chat is not currently enforcing flood control."
        )

    keyboard = get_flood_keyboard(enabled)
    try:
        await q.edit_message_text(f"{msg}\n\n{status_text}", reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass

async def setfloodmode_action_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    action = data.split(":")[1]
    chat_id = q.message.chat.id
    user_id = q.from_user.id

    if not await is_admin(client, q.message, user_id):
        await q.answer("❌ You are not an admin!", show_alert=True)
        return

    if action in ("tban", "tmute"):
        session_id = str(uuid.uuid4())
        pending_floodmode[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "action": action,
            "duration_min_str": "",     # store the minutes string
            "message_id": q.message.id,
            "chat_id_for_edit": chat_id
        }
        text = f"**Selected action:** {action.upper()}\n\nEnter duration in **minutes**:\n\nCurrent: 0"
        keyboard = build_number_pad("")
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()
    else:
        await set_chat_setting(chat_id, "flood_mode", action)
        await set_chat_setting(chat_id, "flood_duration", "0")
        await q.edit_message_text(f"✅ Flood mode set to **{action.upper()}**.")
        await q.answer(f"Mode set to {action}", show_alert=False)

async def setfloodmode_duration_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    data = q.data
    user_id = q.from_user.id

    session = None
    session_id = None
    for sid, s in pending_floodmode.items():
        if s["user_id"] == user_id and s["chat_id"] == q.message.chat.id:
            session = s
            session_id = sid
            break

    if not session:
        await q.answer("Session expired. Please run /setfloodmode again.", show_alert=True)
        return

    if not await is_admin(client, q.message, user_id):
        await q.answer("❌ You are not an admin!", show_alert=True)
        return

    if data.startswith("floodmode_digit:"):
        digit = data.split(":")[1]
        session["duration_min_str"] += digit
        text = f"**Selected action:** {session['action'].upper()}\n\nEnter duration in **minutes**:\n\nCurrent: {session['duration_min_str']}"
        keyboard = build_number_pad(session["duration_min_str"])
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "floodmode_clear":
        session["duration_min_str"] = ""
        text = f"**Selected action:** {session['action'].upper()}\n\nEnter duration in **minutes**:\n\nCurrent: 0"
        keyboard = build_number_pad("")
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "floodmode_back":
        del pending_floodmode[session_id]
        # Re‑render the action selection menu (convert stored seconds to minutes)
        current_action = await get_chat_setting(session["chat_id"], "flood_mode", "mute")
        current_duration_sec = int(await get_chat_setting(session["chat_id"], "flood_duration", "60"))
        current_duration_min = current_duration_sec // 60
        actions = [
            ("mute", "🔇 Mute"),
            ("ban", "🚫 Ban"),
            ("kick", "👢 Kick"),
            ("tban", "⏱️ Tban"),
            ("tmute", "⏱️ Tmute")
        ]
        keyboard = []
        row = []
        for a, label in actions:
            display = f"✅ {label}" if a == current_action else label
            row.append(InlineKeyboardButton(display, callback_data=f"setfloodmode_action:{a}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
        text = (f"**Current flood mode:** {current_action.upper()}\n"
                f"**Duration (for tban/tmute):** {current_duration_min} minute(s)\n\n"
                f"Select new flood action:")
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "floodmode_ok":
        minutes_str = session["duration_min_str"]
        if not minutes_str or int(minutes_str) == 0:
            await q.answer("Please enter a valid duration (positive number of minutes).", show_alert=True)
            return
        minutes = int(minutes_str)
        seconds = minutes * 60
        await set_chat_setting(session["chat_id"], "flood_mode", session["action"])
        await set_chat_setting(session["chat_id"], "flood_duration", str(seconds))
        await q.edit_message_text(f"✅ Flood mode set to **{session['action'].upper()}** with duration **{minutes} minute(s)**.")
        await q.answer(f"Mode set to {session['action']} with {minutes} minute(s)", show_alert=False)
        del pending_floodmode[session_id]

    else:
        await q.answer("Invalid action.", show_alert=True)

async def clearflood_callback(client: Client, callback_query: CallbackQuery):
    q = callback_query
    await q.answer()
    if not await is_admin(client, q.message, q.from_user.id):
        await q.edit_message_text("❌ Admin only.")
        return

    data = q.data
    chat_id = q.message.chat.id
    action = data.split(":")[1]

    if action == "enable":
        await set_chat_setting(chat_id, "clear_flood_enabled", "1")
        msg = "✅ Clearflood enabled."
    elif action == "disable":
        await set_chat_setting(chat_id, "clear_flood_enabled", "0")
        msg = "❌ Clearflood disabled."
    elif action == "refresh":
        msg = "🔄 Refreshed."
    else:
        return

    enabled = await get_chat_setting(chat_id, "clear_flood_enabled", "1") == "1"
    status = "✅ Enabled" if enabled else "❌ Disabled"
    description = (
        f"Antiflood clearing is currently **{status}** in {q.message.chat.title}. "
        "When someone floods, all their flood messages will be automatically deleted."
        if enabled else
        f"Antiflood clearing is currently **{status}** in {q.message.chat.title}. "
        "When someone floods, their flood messages will NOT be automatically deleted."
    )

    keyboard = get_clearflood_keyboard(enabled)
    try:
        await q.edit_message_text(f"{msg}\n\n{description}", reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass

async def warnmode_digit_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    if not data.startswith("warnmode_"):
        return
    # now proceed...
    """Handle digit input for warnmode duration."""
    q = callback_query
    data = q.data
    user_id = q.from_user.id

    # Find the session for this user in this chat
    session = None
    session_id = None
    for sid, s in pending_warnmode.items():
        if s["user_id"] == user_id and s["chat_id"] == q.message.chat.id:
            session = s
            session_id = sid
            break

    if not session:
        await q.answer("Session expired. Please run /warnmode again.", show_alert=True)
        return

    if not await is_admin(client, q.message, user_id):
        await q.answer("❌ You are not an admin!", show_alert=True)
        return

    if data.startswith("warnmode_digit:"):
        digit = data.split(":")[1]
        session["duration_min_str"] += digit
        text = f"**Selected action:** {session['action'].upper()}\n\nEnter duration in **minutes**:\n\nCurrent: {session['duration_min_str']}"
        keyboard = build_number_pad_warnmode(session["duration_min_str"])
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "warnmode_clear":
        session["duration_min_str"] = ""
        text = f"**Selected action:** {session['action'].upper()}\n\nEnter duration in **minutes**:\n\nCurrent: 0"
        keyboard = build_number_pad_warnmode("")
        await q.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "warnmode_back":
        # Go back to main menu
        del pending_warnmode[session_id]
        # Re‑render the action selection menu
        chat_id = session["chat_id"]
        current_action = await get_warn_action(chat_id)
        current_limit = await get_warn_limit(chat_id)
        text = f"**⚙️ Warn Action Configuration**\n\nCurrent action: **{current_action.upper()}**\nCurrent limit: **{current_limit}**\nSelect new action:"
        actions = ["ban", "mute", "kick", "tban", "tmute"]
        buttons = []
        row = []
        for i, a in enumerate(actions):
            display = f"✅ {a.upper()}" if a == current_action else a.upper()
            row.append(InlineKeyboardButton(display, callback_data=f"warnmode:{a}"))
            if len(row) == 2 or i == len(actions)-1:
                buttons.append(row)
                row = []
        buttons.append([InlineKeyboardButton(f"🔢 Change Warn Limit ({current_limit})", callback_data="warnlimit:menu")])
        buttons.append([InlineKeyboardButton("🗑 Close", callback_data="del_msg")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.MARKDOWN)
        await q.answer()

    elif data == "warnmode_ok":
        minutes_str = session["duration_min_str"]
        if not minutes_str or int(minutes_str) == 0:
            await q.answer("Please enter a valid duration (positive number of minutes).", show_alert=True)
            return
        minutes = int(minutes_str)
        seconds = minutes * 60
        # Save action and duration
        await set_warn_action(session["chat_id"], session["action"])
        await set_chat_setting(session["chat_id"], "warn_duration", str(seconds))
        await q.edit_message_text(f"✅ Warn action set to **{session['action'].upper()}** with duration **{minutes} minute(s)**.")
        await q.answer(f"Action set to {session['action']} with {minutes} minute(s)", show_alert=False)
        del pending_warnmode[session_id]

async def warn_buttons(client, query: CallbackQuery):
    data = query.data.split(":")
    action, chat_id, user_id = data[0], int(data[1]), int(data[2])

    # 🔒 ADMIN CHECK
    try:
        member = await client.get_chat_member(chat_id, query.from_user.id)

        # 👑 creator always allowed
        if member.status == "creator":
            pass

        # 👮 admin but must have restrict permission
        elif member.status == "administrator":
            if not (member.privileges and member.privileges.can_restrict_members):
                return await query.answer("❌ You need ban permission!", show_alert=True)

        # ❌ normal users blocked
        
    except Exception as e:
        return await query.answer("Error checking admin!", show_alert=True)

    # ✅ REMOVE LAST WARN
    if action == "rmwarn":
        doc = await warns_col.find_one({"chat_id": chat_id, "user_id": user_id})

        if doc and doc.get("warns"):
            doc["warns"].pop()

            await warns_col.update_one(
                {"chat_id": chat_id, "user_id": user_id},
                {"$set": {"warns": doc["warns"]}}
            )

            await query.answer("Last warn removed ✅")

            try:
                user = await client.get_users(user_id)

                await query.message.reply_text(
                    f"❌ Last warning removed for {user.mention} by {query.from_user.mention}."
                )
            except Exception as e:
                print(f"Remove notify error: {e}")

        else:
            await query.answer("❌ No warns to remove!", show_alert=True)

    # ✅ RESET ALL WARNS
    elif action == "resetwarn":
        doc = await warns_col.find_one({"chat_id": chat_id, "user_id": user_id})

        # ❌ agar warn hi nahi hai
        if not doc or not doc.get("warns"):
            return await query.answer("❌ There are no warnings to reset!", show_alert=True)

        # ✅ reset
        await warns_col.delete_one({"chat_id": chat_id, "user_id": user_id})

        await query.answer("All warns reset ✅")

        try:
            user = await client.get_users(user_id)

            await query.message.reply_text(
                f"🔄 All warnings for {user.mention} have been reset by {query.from_user.mention}."
            )
        except Exception as e:
            print(f"Reset notify error: {e}")

async def health_check(request):
    return web.Response(text="OK")

async def run_web():
    """Start a minimal HTTP server for Render health checks."""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    # Keep the server alive forever
    await asyncio.Event().wait()

async def nightmode_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin
    if not await is_admin(client, callback_query.message, user_id):
        await callback_query.answer("❌ You are not an admin!", show_alert=True)
        return

    if action == "enable":
        await set_nightmode_enabled(chat_id, True)
        await callback_query.answer("✅ Night mode enabled.", show_alert=False)
    elif action == "disable":
        await set_nightmode_enabled(chat_id, False)
        await callback_query.answer("❌ Night mode disabled.", show_alert=False)
    elif action == "set_start":
        # Start time picker
        session_id = str(uuid.uuid4())
        pending_nightmode[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "mode": "start",
            "hour": None,
            "ampm": None,
            "message_id": callback_query.message.id
        }
        # Show hour selection
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f"nightmode_hour:{session_id}:{i}") for i in range(1, 7)],
            [InlineKeyboardButton(str(i), callback_data=f"nightmode_hour:{session_id}:{i}") for i in range(7, 13)],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"nightmode_cancel:{session_id}")]
        ])
        await callback_query.edit_message_text("Select hour (1‑12):", reply_markup=keyboard)
        await callback_query.answer()
        return
    elif action == "set_end":
        session_id = str(uuid.uuid4())
        pending_nightmode[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "mode": "end",
            "hour": None,
            "ampm": None,
            "message_id": callback_query.message.id
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f"nightmode_hour:{session_id}:{i}") for i in range(1, 7)],
            [InlineKeyboardButton(str(i), callback_data=f"nightmode_hour:{session_id}:{i}") for i in range(7, 13)],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"nightmode_cancel:{session_id}")]
        ])
        await callback_query.edit_message_text("Select hour (1‑12):", reply_markup=keyboard)
        await callback_query.answer()
        return
    else:
        return

    # Refresh main menu
    await refresh_nightmode_menu(client, callback_query, chat_id)

async def nightmode_hour_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    session_id = parts[1]
    hour = int(parts[2])
    session = pending_nightmode.get(session_id)
    if not session:
        await callback_query.answer("Session expired. Use /setnight again.", show_alert=True)
        return
    if session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("This button is not for you.", show_alert=True)
        return

    session["hour"] = hour
    # Ask for AM/PM
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("AM", callback_data=f"nightmode_ampm:{session_id}:AM"),
         InlineKeyboardButton("PM", callback_data=f"nightmode_ampm:{session_id}:PM")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"nightmode_cancel:{session_id}")]
    ])
    await callback_query.edit_message_text(f"Hour {hour} selected. Choose AM/PM:", reply_markup=keyboard)
    await callback_query.answer()

async def nightmode_ampm_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    session_id = parts[1]
    ampm = parts[2]
    session = pending_nightmode.get(session_id)
    if not session:
        await callback_query.answer("Session expired. Use /setnight again.", show_alert=True)
        return
    if session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("This button is not for you.", show_alert=True)
        return

    session["ampm"] = ampm
    # Build the time string: hour:00 AM/PM (we can set minutes to 00 for simplicity)
    time_str = f"{session['hour']}:00 {ampm}"
    chat_id = session["chat_id"]
    if session["mode"] == "start":
        await set_nightmode_start(chat_id, time_str)
        await callback_query.answer(f"Start time set to {time_str}", show_alert=False)
    else:
        await set_nightmode_end(chat_id, time_str)
        await callback_query.answer(f"End time set to {time_str}", show_alert=False)

    # Refresh main menu
    await refresh_nightmode_menu(client, callback_query, chat_id)
    del pending_nightmode[session_id]

async def nightmode_cancel_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_nightmode.get(session_id)
    if session and session["user_id"] == callback_query.from_user.id:
        chat_id = session["chat_id"]
        del pending_nightmode[session_id]
        await refresh_nightmode_menu(client, callback_query, chat_id)
    else:
        await callback_query.answer("Cancelled.", show_alert=False)

async def refresh_nightmode_menu(client, callback_query, chat_id):
    enabled = await get_nightmode_enabled(chat_id)
    start = await get_nightmode_start(chat_id)
    end = await get_nightmode_end(chat_id)
    status = "✅ Enabled" if enabled else "❌ Disabled"
    text = (
        f"🌙 **Night Mode Settings**\n\n"
        f"Status: {status}\n"
        f"Window: {start} → {end} (Kolkata time)\n\n"
        f"During this window, all media messages (photos, videos, stickers, etc.) from non‑approved users will be automatically deleted.\n"
        f"Approved users and admins are exempt."
    )
    enable_text = " Enable" if not enabled else "🟢 Enable"
    disable_text = "🔴 Disable" if not enabled else " Disable"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(enable_text, callback_data=f"nightmode:enable:{chat_id}"),
            InlineKeyboardButton(disable_text, callback_data=f"nightmode:disable:{chat_id}")
        ],
        [
            InlineKeyboardButton(f"⏰ Set Start ({start})", callback_data=f"nightmode:set_start:{chat_id}"),
            InlineKeyboardButton(f"⏰ Set End ({end})", callback_data=f"nightmode:set_end:{chat_id}")
        ],
        [InlineKeyboardButton("❌ Close", callback_data="del_msg")]
    ])
    
    try:
        await callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=enums.ParseMode.MARKDOWN)
    except MessageNotModified:
        pass

async def antiraid_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin and required permissions
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
        if member.status != enums.ChatMemberStatus.OWNER:
            if not (member.privileges and member.privileges.can_change_info):
                await callback_query.answer("❌ You need 'Change Group Info' permission.", show_alert=True)
                return
            if not (member.privileges and member.privileges.can_restrict_members):
                await callback_query.answer("❌ You need 'Ban Members' permission.", show_alert=True)
                return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action == "enable":
        # Check if duration is zero
        duration = await get_antiraid_duration(chat_id)
        if duration == 0:
            await callback_query.answer(
                "❌ Cannot enable antiraid because duration is set to 0.\n"
                "Please set a positive duration first using 'Set Duration'.",
                show_alert=True
            )
            return
        await set_antiraid_enabled(chat_id, True)
        duration = await get_antiraid_duration(chat_id)
        if duration > 0:
            await set_antiraid_expiry(chat_id, int(time.time()) + duration)
        else:
            await set_antiraid_expiry(chat_id, 0)
        await callback_query.answer("✅ Antiraid enabled.", show_alert=False)
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)

    elif action == "disable":
        await set_antiraid_enabled(chat_id, False)
        await set_antiraid_expiry(chat_id, 0)
        await callback_query.answer("❌ Antiraid disabled.", show_alert=False)
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)

    elif action == "set_duration":
        session_id = str(uuid.uuid4())
        pending_antiraid_actions[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": callback_query.message.id,
            "duration_str": "",
            "unit": None
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(1, 4)],
            [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(4, 7)],
            [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(7, 10)],
            [InlineKeyboardButton("0", callback_data=f"antiraid_digit:{session_id}:0")],
            [InlineKeyboardButton("⌫ Clear", callback_data=f"antiraid_clear:{session_id}")],
            [InlineKeyboardButton("Sec", callback_data=f"antiraid_unit:{session_id}:sec"),
             InlineKeyboardButton("Min", callback_data=f"antiraid_unit:{session_id}:min"),
             InlineKeyboardButton("Hour", callback_data=f"antiraid_unit:{session_id}:hour")],
            [InlineKeyboardButton("Day", callback_data=f"antiraid_unit:{session_id}:day"),
             InlineKeyboardButton("Week", callback_data=f"antiraid_unit:{session_id}:week")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"antiraid_back:{session_id}"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"antiraid_cancel:{session_id}")]
        ])
        await callback_query.edit_message_text(
            "⏱ **Set Antiraid Duration**\n\nEnter the number, then select a unit.\nCurrent: 0",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await callback_query.answer()

async def antiraid_digit_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    session_id = parts[1]
    digit = parts[2]
    session = pending_antiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    # If the current duration string is empty and the digit is "0", immediately disable antiraid
    if not session["duration_str"] and digit == "0":
        chat_id = session["chat_id"]
        # Set duration to 0
        await set_antiraid_duration(chat_id, 0)
        # Disable antiraid
        await set_antiraid_enabled(chat_id, False)
        await set_antiraid_expiry(chat_id, 0)
        # Remove the session
        del pending_antiraid_actions[session_id]
        # Show the main menu
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)
        await callback_query.answer("⚠️ Antiraid disabled (duration set to 0).", show_alert=False)
        return

    # Otherwise, add the digit to the string
    session["duration_str"] += digit
    # Update the message with the new current value
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(1, 4)],
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(4, 7)],
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(7, 10)],
        [InlineKeyboardButton("0", callback_data=f"antiraid_digit:{session_id}:0")],
        [InlineKeyboardButton("⌫ Clear", callback_data=f"antiraid_clear:{session_id}")],
        [InlineKeyboardButton("Sec", callback_data=f"antiraid_unit:{session_id}:sec"),
         InlineKeyboardButton("Min", callback_data=f"antiraid_unit:{session_id}:min"),
         InlineKeyboardButton("Hour", callback_data=f"antiraid_unit:{session_id}:hour")],
        [InlineKeyboardButton("Day", callback_data=f"antiraid_unit:{session_id}:day"),
         InlineKeyboardButton("Week", callback_data=f"antiraid_unit:{session_id}:week")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"antiraid_back:{session_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"antiraid_cancel:{session_id}")]
    ])
    await callback_query.edit_message_text(
        f"⏱ **Set Antiraid Duration**\n\nEnter the number, then select a unit.\nCurrent: {session['duration_str']}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await callback_query.answer()
    
async def antiraid_clear_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_antiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return
    session["duration_str"] = ""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(1, 4)],
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(4, 7)],
        [InlineKeyboardButton(str(i), callback_data=f"antiraid_digit:{session_id}:{i}") for i in range(7, 10)],
        [InlineKeyboardButton("0", callback_data=f"antiraid_digit:{session_id}:0")],
        [InlineKeyboardButton("⌫ Clear", callback_data=f"antiraid_clear:{session_id}")],
        [InlineKeyboardButton("Sec", callback_data=f"antiraid_unit:{session_id}:sec"),
         InlineKeyboardButton("Min", callback_data=f"antiraid_unit:{session_id}:min"),
         InlineKeyboardButton("Hour", callback_data=f"antiraid_unit:{session_id}:hour")],
        [InlineKeyboardButton("Day", callback_data=f"antiraid_unit:{session_id}:day"),
         InlineKeyboardButton("Week", callback_data=f"antiraid_unit:{session_id}:week")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"antiraid_back:{session_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"antiraid_cancel:{session_id}")]
    ])
    await callback_query.edit_message_text(
        f"⏱ **Set Antiraid Duration**\n\nEnter the number, then select a unit.\nCurrent: 0",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await callback_query.answer()

async def antiraid_unit_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    session_id = parts[1]
    unit = parts[2]
    session = pending_antiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return
    number_str = session["duration_str"]
    if not number_str:
        await callback_query.answer("Please enter a number first.", show_alert=True)
        return
    number = int(number_str)
    if unit == "sec":
        seconds = number
    elif unit == "min":
        seconds = number * 60
    elif unit == "hour":
        seconds = number * 3600
    elif unit == "day":
        seconds = number * 86400
    elif unit == "week":
        seconds = number * 604800
    else:
        seconds = 0

    chat_id = session["chat_id"]

    # If seconds is 0, disable antiraid
    if seconds == 0:
        await set_antiraid_duration(chat_id, 0)
        await set_antiraid_enabled(chat_id, False)
        await set_antiraid_expiry(chat_id, 0)
        await callback_query.answer("⚠️ Antiraid disabled because duration was set to 0.", show_alert=False)
    else:
        await set_antiraid_duration(chat_id, seconds)
        if await get_antiraid_enabled(chat_id):
            await set_antiraid_expiry(chat_id, int(time.time()) + seconds)
        await callback_query.answer(f"✅ Duration set to {number} {unit}(s).", show_alert=False)

    del pending_antiraid_actions[session_id]
    await show_antiraid_menu(client, callback_query, chat_id, edit=True)

async def antiraid_back_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_antiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return
    chat_id = session["chat_id"]
    del pending_antiraid_actions[session_id]
    await show_antiraid_menu(client, callback_query, chat_id, edit=True)
    await callback_query.answer()

async def antiraid_cancel_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_antiraid_actions.get(session_id)
    if session and session["user_id"] == callback_query.from_user.id:
        del pending_antiraid_actions[session_id]
        chat_id = session["chat_id"]
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)
    else:
        await callback_query.answer("Cancelled.", show_alert=False)

async def antiraid_punish_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) < 4:
        return
    action_type = parts[2]  # "menu", "action", or "back"

    if action_type == "menu":
        # Format: antiraid:punish:menu:{chat_id}
        chat_id = int(parts[3])
    elif action_type == "action":
        # Format: antiraid:punish:action:{action}:{chat_id}
        if len(parts) < 5:
            return
        punish_action = parts[3]
        chat_id = int(parts[4])
    elif action_type == "back":
        # Format: antiraid:punish:back:{chat_id}
        chat_id = int(parts[3])
        # Return to main antiraid menu
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)
        await callback_query.answer()
        return
    else:
        return

    user_id = callback_query.from_user.id

    # Verify admin and permissions
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
        if member.status != enums.ChatMemberStatus.OWNER:
            if not (member.privileges and member.privileges.can_change_info):
                await callback_query.answer("❌ You need 'Change Group Info' permission.", show_alert=True)
                return
            if not (member.privileges and member.privileges.can_restrict_members):
                await callback_query.answer("❌ You need 'Ban Members' permission.", show_alert=True)
                return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action_type == "menu":
        # Show punishment selection menu
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔇 Mute", callback_data=f"antiraid:punish:action:mute:{chat_id}"),
             InlineKeyboardButton("👢 Kick", callback_data=f"antiraid:punish:action:kick:{chat_id}")],
            [InlineKeyboardButton("🚫 Ban", callback_data=f"antiraid:punish:action:ban:{chat_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"antiraid:punish:back:{chat_id}")]
        ])
        await callback_query.edit_message_text(
            "Select the punishment for raiding users:",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await callback_query.answer()
        return

    elif action_type == "action":
        punish_action = parts[3]
        await set_antiraid_punish_action(chat_id, punish_action)
        # Set duration to 0 (permanent) for now
        await set_antiraid_punish_duration(chat_id, 0)
        await callback_query.answer(f"✅ Punishment set to {punish_action.upper()}", show_alert=False)
        # Refresh main antiraid menu
        await show_antiraid_menu(client, callback_query, chat_id, edit=True)
        return

pending_autoantiraid_actions = {}   # session_id -> {chat_id, user_id, message_id, threshold_str}

async def autoantiraid_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]
    chat_id = int(parts[2])
    user_id = callback_query.from_user.id

    # Verify admin
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
            await callback_query.answer("❌ You are not an admin!", show_alert=True)
            return
        if member.status != enums.ChatMemberStatus.OWNER:
            if not (member.privileges and member.privileges.can_change_info):
                await callback_query.answer("❌ You need 'Change Group Info' permission.", show_alert=True)
                return
    except Exception:
        await callback_query.answer("❌ Failed to verify admin status.", show_alert=True)
        return

    if action == "enable":
        # Enable requires a threshold > 0. If threshold is 0, we need to ask for it.
        threshold = await get_autoantiraid_threshold(chat_id)
        if threshold == 0:
            await callback_query.answer("Please set a threshold first using 'Set Threshold'.", show_alert=True)
            return
        # Otherwise, enable is already on? Actually threshold >0 means enabled.
        await callback_query.answer("Autoantiraid is already enabled when threshold > 0.", show_alert=True)
        await show_autoantiraid_menu(client, callback_query, chat_id, edit=True)
        return

    elif action == "disable":
        await set_autoantiraid_threshold(chat_id, 0)
        await callback_query.answer("❌ Autoantiraid disabled.", show_alert=False)
        await show_autoantiraid_menu(client, callback_query, chat_id, edit=True)
        return

    elif action == "set_threshold":
        session_id = str(uuid.uuid4())
        pending_autoantiraid_actions[session_id] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": callback_query.message.id,
            "threshold_str": ""
        }
        # Show number pad
        keyboard = build_number_pad_autoantiraid(session_id, "")
        await callback_query.edit_message_text(
            "🔢 **Set Autoantiraid Threshold**\n\nEnter the number of joins per minute (max 999):\n\nCurrent: 0",
            reply_markup=keyboard,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await callback_query.answer()
        return

def build_number_pad_autoantiraid(session_id: str, current: str) -> InlineKeyboardMarkup:
    """Build number pad for autoantiraid threshold entry."""
    rows = []
    for i in range(1, 10, 3):
        row = []
        for j in range(3):
            digit = str(i + j)
            row.append(InlineKeyboardButton(digit, callback_data=f"autoantiraid_digit:{session_id}:{digit}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("0", callback_data=f"autoantiraid_digit:{session_id}:0"),
        InlineKeyboardButton("⌫ Clear", callback_data=f"autoantiraid_clear:{session_id}")
    ])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data=f"autoantiraid_back:{session_id}"),
        InlineKeyboardButton("✅ OK", callback_data=f"autoantiraid_ok:{session_id}")
    ])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"autoantiraid_cancel:{session_id}")])
    return InlineKeyboardMarkup(rows)

async def autoantiraid_digit_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 3:
        return
    session_id = parts[1]
    digit = parts[2]
    session = pending_autoantiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    new_str = session["threshold_str"] + digit
    if len(new_str) > 3:   # limit to 3 digits
        await callback_query.answer("Maximum 3 digits allowed.", show_alert=True)
        return
    session["threshold_str"] = new_str
    keyboard = build_number_pad_autoantiraid(session_id, new_str)
    await callback_query.edit_message_text(
        f"🔢 **Set Autoantiraid Threshold**\n\nEnter the number of joins per minute (max 999):\n\nCurrent: {new_str or '0'}",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await callback_query.answer()

async def autoantiraid_clear_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_autoantiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return
    session["threshold_str"] = ""
    keyboard = build_number_pad_autoantiraid(session_id, "")
    await callback_query.edit_message_text(
        "🔢 **Set Autoantiraid Threshold**\n\nEnter the number of joins per minute (max 999):\n\nCurrent: 0",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await callback_query.answer()

async def autoantiraid_ok_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_autoantiraid_actions.get(session_id)
    if not session or session["user_id"] != callback_query.from_user.id:
        await callback_query.answer("Session expired.", show_alert=True)
        return

    threshold_str = session["threshold_str"]
    if not threshold_str or int(threshold_str) == 0:
        await callback_query.answer("Threshold must be a positive number.", show_alert=True)
        return
    threshold = int(threshold_str)
    if threshold > 999:
        await callback_query.answer("Threshold cannot exceed 999.", show_alert=True)
        return

    await set_autoantiraid_threshold(session["chat_id"], threshold)
    await callback_query.answer(f"✅ Threshold set to {threshold}", show_alert=False)
    del pending_autoantiraid_actions[session_id]
    await show_autoantiraid_menu(client, callback_query, session["chat_id"], edit=True)

async def autoantiraid_back_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_autoantiraid_actions.get(session_id)
    if session and session["user_id"] == callback_query.from_user.id:
        del pending_autoantiraid_actions[session_id]
        await show_autoantiraid_menu(client, callback_query, session["chat_id"], edit=True)
    else:
        await callback_query.answer("Cancelled.", show_alert=False)

async def autoantiraid_cancel_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    session_id = parts[1]
    session = pending_autoantiraid_actions.get(session_id)
    if session and session["user_id"] == callback_query.from_user.id:
        del pending_autoantiraid_actions[session_id]
        await show_autoantiraid_menu(client, callback_query, session["chat_id"], edit=True)
    else:
        await callback_query.answer("Cancelled.", show_alert=False)

async def broadcast_confirm_callback(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    parts = data.split(":")
    if len(parts) != 2:
        return
    action, user_id_str = parts[0], parts[1]
    user_id = int(user_id_str)
    if callback_query.from_user.id != user_id:
        return await callback_query.answer("This confirmation is not for you.", show_alert=True)
    if action == "broadcast_confirm":
        data = pending_broadcast.pop(user_id, None)
        if not data:
            return await callback_query.answer("Broadcast data not found.", show_alert=True)
        text = data["text"]
        media = data["media"]
        pin = data["pin"]
        mode = data["mode"]
        # Determine which chats to broadcast to
        query = {}
        if mode == "group":
            query["type"] = {"$in": ["group", "supergroup"]}
        elif mode == "private":
            query["type"] = "private"
        # else "all" – no filter
        chats = await active_chats_col.find(query).to_list(length=None)
        if not chats:
            await callback_query.edit_message_text("No active chats found for broadcast.")
            return
        sent_count = 0
        pinned_count = 0
        for chat_data in chats:
            chat_id = chat_data["chat_id"]
            try:
                if media:
                    mtype, fid = media
                    if mtype == "photo":
                        msg = await client.send_photo(chat_id, fid, caption=text, parse_mode=enums.ParseMode.HTML)
                    elif mtype == "video":
                        msg = await client.send_video(chat_id, fid, caption=text, parse_mode=enums.ParseMode.HTML)
                    elif mtype == "document":
                        msg = await client.send_document(chat_id, fid, caption=text, parse_mode=enums.ParseMode.HTML)
                    elif mtype == "animation":
                        msg = await client.send_animation(chat_id, fid, caption=text, parse_mode=enums.ParseMode.HTML)
                    elif mtype == "sticker":
                        msg = await client.send_sticker(chat_id, fid)
                        if text:
                            await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
                    else:
                        msg = await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
                else:
                    msg = await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
                sent_count += 1
                if pin and chat_data["type"] in ("group", "supergroup"):
                    # Try to pin
                    try:
                        await msg.pin(disable_notification=True)
                        pinned_count += 1
                    except:
                        pass
            except Exception as e:
                print(f"Failed to send to {chat_id}: {e}")
        await callback_query.edit_message_text(
            f"Broadcast completed.\nSent to {sent_count} chats.\nPinned in {pinned_count} groups (where possible)."
        )
    elif action == "broadcast_cancel":
        pending_broadcast.pop(user_id, None)
        await callback_query.edit_message_text("❌ Broadcast cancelled.")
    await callback_query.answer()

def main():
    app = Client("rose_clone", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

    # Command handlers
    commands = {
        "start": start, "clone": clone, "help": help_cmd, "reload": reload_bot, "language": language,
        "owner": owner_group, "antiraid": antiraid_cmd, "connection": connection, "disable": disable_cmd,
        "enable": enable_cmd, "disabled": disabled_cmds, "setglog": set_global_log,
        "logchannel": log_channel, "mics": mics, "setwelcome": setwelcome, "resetwelcome": resetwelcome,
        "welcome": welcome_toggle, "setgoodbye": setgoodbye, "resetgoodbye": resetgoodbye,
        "goodbye": goodbye_toggle, "setrules": setrules, "rules": rules, "setnote": setnote, "setnight": nightmode_cmd,
        "get": get_note, "delnote": delnote, "warn": warn, "dwarn": dwarn, "warns": warns, "warnings": warnings, "warnconfig": warnings, "swarn": swarn,
        "resetwarns": resetwarns, "unwarn": unwarn, "warnmode": warnmode_cmd, "warninfo": warninfo_cmd,  "resetallwarns": resetallwarns, "warntime": warntime_cmd,
        "mute": mute, "dmute": dmute, "smute": smute, "tmute": tmute, "rmwarn": unwarn, "resetwarn": resetwarns,
        "unmute": unmute, "ban": ban, "sban": sban, "tban": tban, "dban": dban, "unban": unban,
        "kick": kick, "dkick": dkick, "skick": skick, "kickme": kickme, "lock": lock, "unlock": unlock,
        "pin": pin_message, "unpin": unpin_message, "cleanservice": cleanservice, "cleancommands": clean_cmd_toggle,
        "cleanfor": clean_cmd_for, "captcha": captcha_toggle, "flood": flood_cmd, "setfloodtimer": setfloodtimer_cmd,
        "setfloodmode": setfloodmode_cmd, "clearflood": clearflood, "approve": approve, "unapprove": unapprove,"unapproveall": unapproveall,
        "approved": approved, "approval": approval,"adminlist": adminlist, "promote": promote, "demote": demote,
        "setgtitle": setgtitle, "resetgtitle": resetgtitle, "setgpic": setgpic, "delgpic": delgpic, "setgdesc": setgdesc, "delgdesc": delgdesc,
        "admincache": admincache, "id": id_cmd, "info": info, "purge": purge, "report": report, "autoantiraid": autoantiraid_cmd,
        "admins": admins_cmd, "del": del_message, "pinned": pinned_cmd, "spin": spin_message, "unpinall": unpinall_message, "permapin": permapin_command,
        "formatting": formatting, "fpromote": federation_admin_manager, "fdemote": federation_admin_manager,"leavefed" :leavefed_cmd, "chatfed": chatfed_cmd, "quietfed": quietfed_cmd,
        "setvcmsg": set_vc_msg, "vcmsg": vcmsg_toggle, "setvcinvite": set_vc_invite, "vcinvite": vcinvite_toggle, "fedadmins": fedadmins_cmd, "fedstat": fedstat_cmd,
        "newfed": new_fed, "subfed": subscribe_fed, "unsubfed": unsubscribe_fed,  "joinfed": join_fed_group, "fedinfo": fed_info, "delfed": del_fed, "fban": fban_user,
        "unfban": unfban_user, "utag": utag, "atag": atag, "cancel": canceltag, "spurge": spurge,
        "cancelpurge": cancelpurge, "purgeuser": purgeuser, "purgebots": purgebots, "purgemedia": purgemedia,
        "purgelinks": purgelinks, "renamefed": rename_fed, "transferfed": transfer_fed, "myfeds": my_feds,
        "fbanlist": fban_list, "feddemoteme": feddemoteme, "bioconfig": bioconfig_cmd, "allow": allow_bio_user,
        "unallow": unallow_bio_user, "aplist": aplist_bio, "filter": filter_cmd_handler, "stop": stop_filter_handler,
        "filters": list_filters_handler,"antichannelpin": antichannelpin_command,"cleanlinked": cleanlinked_command, "anonadmin": anonadmin_command,
        "chats": chats_cmd, "gban": gban_cmd,
        "gunban": gunban_cmd, 
        "pchats": pchats_cmd,
        "getlink": getlink_cmd,
        "broadcast": broadcast_cmd,
        "gcast": gcast_cmd,
        "pcast": pcast_cmd,
        "broadcastpin": broadcastpin_cmd,
        "gcastpin": gcastpin_cmd,
        "pcastpin": pcastpin_cmd,
        "broadcastunpin": broadcastunpin_cmd,
        "gcastunpin": gcastunpin_cmd,
        "pcastunpin": pcastunpin_cmd,
        "addsudo": add_sudo,
        "rmsudo": rm_sudo,
        "sudolist": sudo_list, "syncchats": syncchats_cmd,
        "gchats": gchats_cmd, "track": track_toggle,
        "setlog": setlog_command,
        "unsetlog": unsetlog_command,
        "logchannel": logchannel_cmd,        # यह पुराने /logchannel को replace करेगा
        "logcategories": logcategories_cmd,
        "log": log_enable,
        "nolog": log_disable,
    }

    # Multi-word commands (exact match)
    multi_word_commands = {
        "lock all": lockall,
        "unlock all": unlockall,
    }

    # Final COMMAND_HANDLERS dictionary
    COMMAND_HANDLERS = {**commands, **multi_word_commands, "autoantiraid": autoantiraid_cmd}


    for name, fn in commands.items():
        app.add_handler(MessageHandler(fn, filters.command(name)))

    # Custom command dispatcher handler
    app.add_handler(MessageHandler(custom_command_dispatcher, filters.command(list(set(cmd.split()[0] for cmd in COMMAND_HANDLERS)))))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(start_buttons, filters.regex(r"^start:")))
    app.add_handler(CallbackQueryHandler(help_buttons, filters.regex(r"^help:")))
    app.add_handler(CallbackQueryHandler(captcha_verify, filters.regex(r"^captcha:")))
    app.add_handler(CallbackQueryHandler(join_vc_callback, filters.regex("^joinvc_")))
    app.add_handler(CallbackQueryHandler(fed_admin_list_callback, filters.regex(r"^fed_admins:")))
    app.add_handler(CallbackQueryHandler(fed_callback_handler, filters.regex("^fed:")))
    app.add_handler(CallbackQueryHandler(confirm_delfed_callback, filters.regex(r"^confirm_delfed:")))
    app.add_handler(CallbackQueryHandler(confirm_delfed_callback, filters.regex(r"^cancel_delfed")))
    app.add_handler(CallbackQueryHandler(bio_callbacks_handler, filters.regex(r"^(cfg_|setwarn_|del_msg|bio_)")))
    app.add_handler(CallbackQueryHandler(prove_admin_callback, filters.regex(r"^prove_admin:")))
    app.add_handler(CallbackQueryHandler(warnmode_callback, filters.regex(r"^warnmode:")))
    app.add_handler(CallbackQueryHandler(warnlimit_callback, filters.regex(r"^warnlimit:")))
    app.add_handler(CallbackQueryHandler(remove_warn_callback, filters.regex(r"^rmwarn:"))) 
    app.add_handler(CallbackQueryHandler(resetall_callback, filters.regex(r"^resetall:")))
    app.add_handler(CallbackQueryHandler(prove_warn_callback,filters.regex("^provewarn:")))  
    app.add_handler(CallbackQueryHandler(proveatag_callback,filters.regex("^proveatag:"))) 
    app.add_handler(CallbackQueryHandler(join_request_callback, filters.regex(r"^jr:")))
    # In main(), add the new callback handler
    app.add_handler(CallbackQueryHandler(unpinall_confirm_callback, filters.regex(r"^unpinall:")))
    app.add_handler(CallbackQueryHandler(antichannelpin_callback, filters.regex(r"^antichannelpin:")))
    app.add_handler(CallbackQueryHandler(cleanlinked_callback, filters.regex(r"^cleanlinked:")))
    app.add_handler(CallbackQueryHandler(anonadmin_callback, filters.regex(r"^anonadmin:")))
    app.add_handler(CallbackQueryHandler(unapproveall_callback, filters.regex(r"^unapproveall:")))
    app.add_handler(CallbackQueryHandler(join_fed_callback, filters.regex(r"^joinfed:")))
    app.add_handler(CallbackQueryHandler(leavefed_callback, filters.regex(r"^leavefed:")))
    app.add_handler(CallbackQueryHandler(quietfed_callback, filters.regex(r"^quietfed:")))
    app.add_handler(CallbackQueryHandler(flood_callback, filters.regex(r"^flood_")))
    app.add_handler(CallbackQueryHandler(setfloodmode_action_callback, filters.regex(r"^setfloodmode_action:")))
    app.add_handler(CallbackQueryHandler(setfloodmode_duration_callback, filters.regex(r"^floodmode_")))
    app.add_handler(CallbackQueryHandler(clearflood_callback, filters.regex(r"^clearflood:")))
    # In main(), after other callback handlers
    app.add_handler(CallbackQueryHandler(warnmode_digit_callback, filters.regex(r"^warnmode_(digit|clear|back|ok)")))
    app.add_handler(CallbackQueryHandler(warn_buttons, filters.regex(r"^(rmwarn|resetwarn):")))
    app.add_handler(CallbackQueryHandler(nightmode_callback, filters.regex(r"^nightmode:")))
    app.add_handler(CallbackQueryHandler(nightmode_hour_callback, filters.regex(r"^nightmode_hour:")))
    app.add_handler(CallbackQueryHandler(nightmode_ampm_callback, filters.regex(r"^nightmode_ampm:")))
    app.add_handler(CallbackQueryHandler(nightmode_cancel_callback, filters.regex(r"^nightmode_cancel:")))
    app.add_handler(CallbackQueryHandler(antiraid_punish_callback, filters.regex(r"^antiraid:punish:")))
    app.add_handler(CallbackQueryHandler(antiraid_callback, filters.regex(r"^antiraid:")))
    app.add_handler(CallbackQueryHandler(antiraid_digit_callback, filters.regex(r"^antiraid_digit:")))
    app.add_handler(CallbackQueryHandler(antiraid_clear_callback, filters.regex(r"^antiraid_clear:")))
    app.add_handler(CallbackQueryHandler(antiraid_unit_callback, filters.regex(r"^antiraid_unit:")))
    app.add_handler(CallbackQueryHandler(antiraid_back_callback, filters.regex(r"^antiraid_back:")))
    app.add_handler(CallbackQueryHandler(antiraid_cancel_callback, filters.regex(r"^antiraid_cancel:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_callback, filters.regex(r"^autoantiraid:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_digit_callback, filters.regex(r"^autoantiraid_digit:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_clear_callback, filters.regex(r"^autoantiraid_clear:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_ok_callback, filters.regex(r"^autoantiraid_ok:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_back_callback, filters.regex(r"^autoantiraid_back:")))
    app.add_handler(CallbackQueryHandler(autoantiraid_cancel_callback, filters.regex(r"^autoantiraid_cancel:")))
    app.add_handler(CallbackQueryHandler(broadcast_confirm_callback, filters.regex(r"^(broadcast_confirm|broadcast_cancel):")))
    
    # Group handlers
    app.add_handler(MessageHandler(on_new_members, filters.new_chat_members), group=1)
    app.add_handler(MessageHandler(on_left_member, filters.left_chat_member))
    app.add_handler(MessageHandler(vc_started, filters.video_chat_started), group=1)
    app.add_handler(MessageHandler(vc_ended, filters.video_chat_ended), group=1)
    app.add_handler(MessageHandler(vc_invited, filters.video_chat_members_invited), group=1)
    app.add_handler(MessageHandler(bio_and_link_scanner, filters.group & ~filters.service & ~filters.command(list(commands.keys()))), group=4)
    app.add_handler(MessageHandler(service_cleaner, filters.service), group=2)
    app.add_handler(MessageHandler(track_activity, filters.group & ~filters.service), group=3)
    app.add_handler(MessageHandler(message_tracker, filters.group & ~filters.service), group=4)
    app.add_handler(MessageHandler(filter_handler, filters.group & ~filters.service), group=5)
    app.add_handler(MessageHandler(safety_handler, filters.group & ~filters.service), group=6)
    app.add_handler(MessageHandler(clean_cmd_handler, filters.command(list(commands.keys()))), group=7)
    app.add_handler(MessageHandler(disabled_cmd_guard, filters.command(list(commands.keys()))), group=-1)
    app.add_handler(MessageHandler(admins_cmd, filters.command("admins") | filters.regex(r"^@admins$")))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(pin_event_handler, filters.pinned_message), group=8)
    app.add_handler(MessageHandler(cleanlinked_handler, filters.group), group=9)
    app.add_handler(MessageHandler(unsubscribe_fed, filters.command("unsubfed")))
    app.add_handler(MessageHandler(nightmode_handler, filters.group & ~filters.service), group=3)
    app.add_handler(ChatMemberUpdatedHandler(on_chat_member_update))
    
    
    async def start_bot():
        await app.start()
        web_task = asyncio.create_task(run_web())
        await permapin_messages_col.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        asyncio.create_task(expire_warns_loop())
        asyncio.create_task(expire_antiraid_loop())
        print("Bot is now online and running!")
        try:
            await idle()
        finally:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
            await app.stop()

    app.run(start_bot())

if __name__ == "__main__":
    main()
