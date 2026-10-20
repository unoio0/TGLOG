import json
import os
import asyncio
import logging
import sqlite3
from datetime import datetime

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    MessageMediaUnsupported,
    Document,
    DocumentAttributeVideo,
    DocumentAttributeAnimated,
    DocumentAttributeSticker,
    UpdateReadMessagesContents,
    User,
    Channel,
    Chat,
    Photo,
)

try:
    from keyword_alerter import init_keyword_alerter
except ImportError:
    init_keyword_alerter = None

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
MEDIA_DIR = os.path.join(DATA_DIR, "media_cache")
os.makedirs(MEDIA_DIR, exist_ok=True)

# 配置：优先检查 data 目录，若不存在则回退至脚本所在目录
_config_path = os.path.join(DATA_DIR, "config.json")
if not os.path.exists(_config_path):
    _config_path = os.path.join(SCRIPT_DIR, "config.json")

with open(_config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

API_ID = cfg["api_id"]
API_HASH = cfg["api_hash"]
PHONE = cfg["phone"]
PASSWORD = cfg.get("password")
BOT_TOKEN = cfg["bot_token"]
LOG_CHANNEL = cfg["log_channel_id"]

ENABLED = cfg.get("enabled", True)
LISTEN_OUTGOING = cfg.get("listen_outgoing", False)
IGNORE_BOTS = cfg.get("ignore_bots", True)
LOG_DELETED = cfg.get("log_deleted", True)
LOG_EDITED = cfg.get("log_edited", True)
LOG_TEXT = cfg.get("log_text", True)
LOG_PHOTOS = cfg.get("log_photos", True)
LOG_VIDEOS = cfg.get("log_videos", True)
LOG_DOCUMENTS = cfg.get("log_documents", True)
LOG_SELF_DESTRUCT = cfg.get("log_self_destruct", True)
LOG_LINKS = cfg.get("log_links", True)

LOG_GROUPS = cfg.get("log_groups", True)
LOG_CHANNELS = cfg.get("log_channels", True)
LOG_PRIVATE = cfg.get("log_private", True)
MAX_VIDEO_SIZE_MB = cfg.get("max_video_size_mb", 10)
MAX_PHOTO_SIZE_MB = cfg.get("max_photo_size_mb", 10)

MODE = cfg.get("mode", "all")
WHITELIST_CHATS = set()
WHITELIST_CHAT_SENDERS = {}

for item in cfg.get("whitelist", []):
    if isinstance(item, int) or (isinstance(item, str) and item.lstrip('-').isdigit()):
        WHITELIST_CHATS.add(int(item))
    elif isinstance(item, dict):
        cid = item.get("chat_id")
        if cid is not None:
            try:
                cid = int(cid)
                WHITELIST_CHATS.add(cid)
                raw_senders = item.get("sender_ids", [])
                if isinstance(raw_senders, int):
                    WHITELIST_CHAT_SENDERS[cid] = {raw_senders}
                elif isinstance(raw_senders, (list, set)):
                    senders_set = {int(x) for x in raw_senders if str(x).lstrip('-').isdigit()}
                    if senders_set:
                        WHITELIST_CHAT_SENDERS[cid] = senders_set
            except (ValueError, TypeError):
                pass

WHITELIST = WHITELIST_CHATS
BLACKLIST = set(cfg.get("blacklist", []))
IGNORED_IDS = set(cfg.get("ignored_ids", []))
IGNORED_IDS.add(LOG_CHANNEL)
CACHE_SIZE = cfg.get("cache_size", 50000)
FORWARD_RULES = cfg.get("forward_rules", [])
FORWARD_SOURCE_IDS = {r.get("source_chat_id") for r in FORWARD_RULES if r.get("source_chat_id")}
for rule in FORWARD_RULES:
    target = rule.get("target_chat_id")
    if target:
        IGNORED_IDS.add(target)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tg-logger")

client = TelegramClient(os.path.join(DATA_DIR, "session"), API_ID, API_HASH)
bot = TelegramClient(os.path.join(DATA_DIR, "bot_session"), API_ID, API_HASH)

MEDIA_TYPE_NAMES = {
    "text": "文本",
    "photo": "图片",
    "video": "视频",
    "document": "文档",
    "self_destruct": "阅后即焚",
    "sticker": "贴纸/表情包",
    "link": "链接",
}

# ---------------------------------------------------------------------------
# SQLite 持久化消息缓存
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(DATA_DIR, "cache.db")
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        chat_id INTEGER,
        msg_id INTEGER,
        sender_name TEXT,
        chat_name TEXT,
        text TEXT,
        media_type TEXT,
        media_path TEXT,
        media_size INTEGER DEFAULT 0,
        edit_date TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, msg_id)
    )
""")
db.execute("""
    CREATE INDEX IF NOT EXISTS idx_msg_id ON messages (msg_id)
""")
db.commit()


def db_store(chat_id, msg_id, sender_name, chat_name, text, mtype, media_path=None, media_size=0, edit_date=None):
    db.execute(
        "INSERT OR REPLACE INTO messages (chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date) VALUES (?,?,?,?,?,?,?,?,?)",
        (chat_id, msg_id, sender_name, chat_name, text, mtype, media_path, media_size, str(edit_date) if edit_date else None),
    )
    db.commit()
    # 清理超出限制的旧条目
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if count > CACHE_SIZE:
        overflow = count - CACHE_SIZE
        # 删除即将清理的数据行对应的本地缓存媒体文件，避免 media_cache 目录积累无用文件
        rows = db.execute(
            "SELECT rowid, media_path FROM messages ORDER BY timestamp ASC LIMIT ?",
            (overflow,),
        ).fetchall()
        for _rowid, media_path in rows:
            if media_path:
                try:
                    os.remove(media_path)
                except OSError:
                    pass
        db.execute(
            "DELETE FROM messages WHERE rowid IN (%s)" % ",".join("?" * len(rows)),
            [r[0] for r in rows],
        )
        db.commit()


def db_get(chat_id, msg_id):
    return db.execute(
        "SELECT chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date FROM messages WHERE chat_id=? AND msg_id=?",
        (chat_id, msg_id),
    ).fetchone()


def db_get_by_msg_id(msg_id):
    """仅通过 msg_id 进行回退查找（用于缺少 chat_id 的删除事件）。

    msg_id 仅在单个聊天内唯一，因此多个聊天可能存在相同的 msg_id。
    如果匹配到多条记录，无法确定属于哪个聊天，此时返回 None，
    避免将消息归属错误甚至泄露给错误的聊天。
    """
    rows = db.execute(
        "SELECT chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date FROM messages WHERE msg_id=?",
        (msg_id,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        log.warning("msg_id %s 存在歧义，匹配了 %d 个聊天；跳过回退匹配", msg_id, len(rows))
    return None


def db_remove(chat_id, msg_id):
    row = db_get(chat_id, msg_id)
    if row and row[6]:  # media_path
        try:
            os.remove(row[6])
        except OSError:
            pass
    db.execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
    db.commit()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
# 缓存各 chat_id 解析后的类型 ("private" | "group" | "channel")，
# 避免在启用类型过滤时对每条消息都频繁请求 get_entity。
_chat_kind_cache = {}


async def _chat_kind(chat_id):
    kind = _chat_kind_cache.get(chat_id)
    if kind is not None:
        return kind
    try:
        entity = await client.get_entity(chat_id)
        if isinstance(entity, User):
            kind = "private"
        elif isinstance(entity, Chat):
            kind = "group"
        elif isinstance(entity, Channel):
            kind = "group" if (entity.megagroup or entity.gigagroup) else "channel"
        else:
            kind = "unknown"
    except Exception:
        kind = "unknown"
    _chat_kind_cache[chat_id] = kind
    return kind


async def should_log_chat(chat_id):
    if not ENABLED:
        return False
    if chat_id in IGNORED_IDS:
        return False
    if chat_id in FORWARD_SOURCE_IDS:
        return True
    if MODE == "whitelist" and chat_id not in WHITELIST:
        return False
    if MODE == "blacklist" and chat_id in BLACKLIST:
        return False

    if not LOG_GROUPS or not LOG_CHANNELS or not LOG_PRIVATE:
        kind = await _chat_kind(chat_id)
        if kind == "private" and not LOG_PRIVATE:
            return False
        if kind == "group" and not LOG_GROUPS:
            return False
        if kind == "channel" and not LOG_CHANNELS:
            return False

    return True


def is_self_destruct(msg):
    """仅将带有有效倒计时的单次查看照片/视频判定为阅后即焚（排除群组定时自动删除的ttl_period）。"""
    media = msg.media
    if media is None:
        return False
    ttl = getattr(media, "ttl_seconds", None)
    if ttl is not None and isinstance(ttl, (int, float)) and ttl > 0:
        return True
    return False


def media_type(msg):
    media = msg.media
    if media is None:
        return "text" if msg.raw_text else None
    if is_self_destruct(msg):
        return "self_destruct"
    if getattr(msg, "sticker", None) is not None:
        return "sticker"
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if isinstance(doc, Document):
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeSticker):
                    return "sticker"
                if isinstance(attr, DocumentAttributeVideo):
                    return "video"
                if isinstance(attr, DocumentAttributeAnimated):
                    return "document"
            return "document"
    if isinstance(media, MessageMediaWebPage):
        return "link"
    return "document"


def get_media_size(msg):
    """获取媒体文件大小（字节数）。"""
    media = msg.media
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if isinstance(doc, Document):
            return doc.size
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        if isinstance(photo, Photo) and photo.sizes:
            # 取最大尺寸估算
            for size in reversed(photo.sizes):
                if hasattr(size, "size"):
                    return size.size
            return 0
    return 0


def should_log_media(mtype):
    if mtype is None:
        return False
    return {
        "text": LOG_TEXT, "photo": LOG_PHOTOS, "video": LOG_VIDEOS,
        "document": LOG_DOCUMENTS, "self_destruct": LOG_SELF_DESTRUCT, "link": LOG_LINKS,
    }.get(mtype, True)


def media_within_size_limit(msg, mtype):
    """检查媒体文件大小是否在配置的上限范围内。"""
    size = get_media_size(msg)
    if mtype == "video":
        return size <= MAX_VIDEO_SIZE_MB * 1024 * 1024
    if mtype == "photo":
        return size <= MAX_PHOTO_SIZE_MB * 1024 * 1024
    return True


def format_sender_name(sender):
    if not sender:
        return "未知"
    parts = []
    if getattr(sender, "first_name", None):
        parts.append(sender.first_name)
    if getattr(sender, "last_name", None):
        parts.append(sender.last_name)
    if parts:
        return " ".join(parts)
    return getattr(sender, "title", None) or getattr(sender, "username", None) or "未知"


async def get_sender_name(msg):
    sender = await msg.get_sender()
    return format_sender_name(sender)


def is_sender_in_forward_rules(chat_id, sender_id):
    """检查特定发件人是否在指定群聊的转发规则名单中。"""
    if not FORWARD_RULES or not sender_id:
        return False
    for rule in FORWARD_RULES:
        if rule.get("source_chat_id") == chat_id:
            raw_senders = rule.get("sender_ids", [])
            if isinstance(raw_senders, int):
                match_senders = {raw_senders}
            else:
                match_senders = {int(x) for x in raw_senders if str(x).lstrip('-').isdigit()}
            if sender_id in match_senders:
                return True
    return False


def should_log_sender(chat_id, sender_id):
    """检查特定群聊的消息是否满足发件人白名单限制。"""
    if chat_id in WHITELIST_CHAT_SENDERS:
        allowed = WHITELIST_CHAT_SENDERS[chat_id]
        if sender_id not in allowed:
            return False
    return True


async def get_chat_name(msg):
    chat = await msg.get_chat()
    if not chat:
        return "未知"
    if getattr(chat, "title", None):
        return chat.title
    parts = []
    if getattr(chat, "first_name", None):
        parts.append(chat.first_name)
    if getattr(chat, "last_name", None):
        parts.append(chat.last_name)
    return " ".join(parts) if parts else "私聊"


async def cache_media(msg, mtype):
    """若在大小限制内，下载并将媒体缓存到本地磁盘。返回存储路径或 None。"""
    if mtype in ("text", "link", "sticker", None):
        return None
    if not media_within_size_limit(msg, mtype):
        return None

    try:
        path = await client.download_media(msg, file=MEDIA_DIR)
        if path:
            log.info("已缓存媒体 %s/%s -> %s", msg.chat_id, msg.id, os.path.basename(path))
        return path
    except Exception as e:
        log.warning("缓存媒体失败: %s", e)
        return None


async def send_log_from_db(header, row, extra_text=None):
    """使用数据库行记录的数据发送日志条目。"""
    # row: (chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size)
    text = header
    if extra_text:
        text += extra_text

    media_path = row[6]
    mtype = row[5]

    if media_path and os.path.exists(media_path):
        try:
            await bot.send_file(LOG_CHANNEL, media_path, caption=text, parse_mode="md")
            return
        except Exception as e:
            log.warning("无法发送已缓存媒体: %s", e)
            text += "\n_(媒体无法转发)_"

    # 媒体存在但超出体积限制或未被缓存
    if mtype in ("video", "photo") and not media_path:
        size_mb = row[7] / (1024 * 1024) if row[7] else 0
        limit = MAX_VIDEO_SIZE_MB if mtype == "video" else MAX_PHOTO_SIZE_MB
        mname = MEDIA_TYPE_NAMES.get(mtype, mtype)
        if size_mb > limit:
            text += f"\n_({mname} {size_mb:.1f}MB，未保存)_"
        elif size_mb > 0:
            text += f"\n_({mname} {size_mb:.1f}MB，未缓存)_"

    if mtype == "sticker":
        text += "\n_(贴纸/表情包)_"

    await bot.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)


async def send_log_from_msg(header, msg, extra_text=None):
    """使用实时 Telethon 消息对象发送日志条目（用于编辑事件）。"""
    text = header
    if extra_text:
        text += extra_text

    media = msg.media if msg else None

    if isinstance(media, str):
        await bot.send_file(LOG_CHANNEL, media, caption=text, parse_mode="md")
        return

    if media and not isinstance(media, (MessageMediaWebPage, MessageMediaUnsupported)):
        mtype = media_type(msg)
        if not media_within_size_limit(msg, mtype):
            size_mb = get_media_size(msg) / (1024 * 1024)
            mname = MEDIA_TYPE_NAMES.get(mtype, mtype)
            text += f"\n_({mname} {size_mb:.1f}MB，未保存)_"
            await bot.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)
            return

        try:
            dl_dir = os.path.join(DATA_DIR, "tmp")
            os.makedirs(dl_dir, exist_ok=True)
            path = await client.download_media(msg, file=dl_dir)
            if path:
                await bot.send_file(LOG_CHANNEL, path, caption=text, parse_mode="md")
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
        except Exception as e:
            log.warning("无法转发媒体: %s", e)
            text += "\n_(媒体无法转发)_"

    await bot.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)


async def get_reply_context_text(msg):
    """如果消息包含回复，提取被回复人的姓名及内容摘要，格式化为引用文本。"""
    if not getattr(msg, "is_reply", False):
        return ""
    try:
        reply_msg = await msg.get_reply_message()
        if not reply_msg:
            return ""
        r_sender = await get_sender_name(reply_msg)
        r_text = reply_msg.raw_text.strip() if reply_msg.raw_text else ""
        r_mtype = media_type(reply_msg)

        if not r_text and r_mtype and r_mtype != "text":
            mname = MEDIA_TYPE_NAMES.get(r_mtype, r_mtype)
            r_text = f"[{mname}]"
        elif r_text and r_mtype and r_mtype != "text":
            mname = MEDIA_TYPE_NAMES.get(r_mtype, r_mtype)
            r_text = f"[{mname}] {r_text}"

        if len(r_text) > 120:
            r_text = r_text[:120] + "..."

        if r_text:
            quoted = "\n> ".join(r_text.splitlines())
            return f"💬 回复 {r_sender}：\n> {quoted}\n\n"
        return f"💬 回复 {r_sender}\n\n"
    except Exception as e:
        log.warning("获取被回复消息失败: %s", e)
        return ""


async def check_and_forward(msg, sender, chat_name, media_path):
    """检查消息是否匹配特定群聊和指定人员的转发规则，若匹配则由机器人立即转发。"""
    if not FORWARD_RULES:
        return

    chat_id = msg.chat_id
    sender_id = msg.sender_id

    for rule in FORWARD_RULES:
        source_id = rule.get("source_chat_id")
        if source_id != chat_id:
            continue

        raw_senders = rule.get("sender_ids", [])
        if isinstance(raw_senders, int):
            match_senders = {raw_senders}
        else:
            match_senders = {int(x) for x in raw_senders if str(x).lstrip('-').isdigit()}

        if sender_id not in match_senders:
            continue

        target_chat = rule.get("target_chat_id") or LOG_CHANNEL
        log.info("命中转发规则: 来源群聊 %s (%s) 用户 %s (%s) -> 目标 %s", chat_name, chat_id, sender, sender_id, target_chat)

        # 格式化本地发送时间
        time_str = msg.date.astimezone().strftime("%Y-%m-%d %H:%M:%S")

        # 构造原始消息跳转链接
        chat = await msg.get_chat()
        username = getattr(chat, "username", None)
        if username:
            msg_link = f"https://t.me/{username}/{msg.id}"
        else:
            cid_str = str(chat_id)
            if cid_str.startswith("-100"):
                clean_id = cid_str[4:]
            elif cid_str.startswith("-"):
                clean_id = cid_str[1:]
            else:
                clean_id = cid_str
            msg_link = f"https://t.me/c/{clean_id}/{msg.id}"

        # 获取被回复消息摘要（若存在）
        reply_part = await get_reply_context_text(msg)

        # 组装指定格式
        content_text = msg.raw_text.strip() if msg.raw_text else ""
        text_body = f"{content_text}\n\n" if content_text else ""
        card_text = (
            f"⏩ 转发消息\n\n"
            f"{sender}\n"
            f"{reply_part}"
            f"{text_body}"
            f"{time_str}\n"
            f"原始连接：{msg_link}"
        )

        if media_path and os.path.exists(media_path):
            try:
                await bot.send_file(target_chat, media_path, caption=card_text, parse_mode="md")
                continue
            except Exception as e:
                log.warning("以 Markdown 发送媒体失败，尝试无格式发送: %s", e)
                try:
                    await bot.send_file(target_chat, media_path, caption=card_text, parse_mode=None)
                    continue
                except Exception as e2:
                    log.warning("转发媒体失败: %s", e2)
                    card_text += "\n_(媒体无法转发)_"

        if msg.media and not media_path:
            mtype = media_type(msg)
            if mtype == "sticker":
                try:
                    await bot.send_file(target_chat, msg.media, caption=card_text, parse_mode="md")
                    continue
                except Exception as e:
                    try:
                        await bot.send_file(target_chat, msg.media, caption=card_text, parse_mode=None)
                        continue
                    except Exception as e2:
                        log.warning("直推转发贴纸失败: %s", e2)
                        card_text += "\n_(贴纸无法转发)_"
            elif mtype in ("photo", "video", "document"):
                size_mb = get_media_size(msg) / (1024 * 1024)
                mname = MEDIA_TYPE_NAMES.get(mtype, mtype)
                card_text += f"\n_({mname} {size_mb:.1f}MB，未下载文件)_"

        try:
            await bot.send_message(target_chat, card_text, parse_mode="md", link_preview=False)
        except Exception as e:
            try:
                await bot.send_message(target_chat, card_text, parse_mode=None, link_preview=False)
            except Exception as e2:
                log.warning("转发消息发送失败: %s", e2)


# ---------------------------------------------------------------------------
# 事件处理器
# ---------------------------------------------------------------------------
async def on_new_message(event):
    msg = event.message
    if not await should_log_chat(msg.chat_id):
        return

    sender_entity = await msg.get_sender()
    if IGNORE_BOTS and getattr(sender_entity, "bot", False):
        if not is_sender_in_forward_rules(msg.chat_id, msg.sender_id):
            return

    can_log = should_log_sender(msg.chat_id, msg.sender_id)
    is_forward = is_sender_in_forward_rules(msg.chat_id, msg.sender_id)
    if not can_log and not is_forward:
        return

    mtype = media_type(msg)
    sender = format_sender_name(sender_entity)
    chat_name = await get_chat_name(msg)

    # 缓存媒体至本地磁盘
    media_path = None
    media_size = get_media_size(msg)
    if can_log and mtype == "self_destruct" and LOG_SELF_DESTRUCT:
        # 立即下载并发送至日志频道 — 无需等待删除事件触发
        try:
            media_path = await client.download_media(msg, file=MEDIA_DIR)
            if media_path:
                log.info("已保存阅后即焚媒体 %s/%s -> %s", msg.chat_id, msg.id, os.path.basename(media_path))
            else:
                log.warning("阅后即焚媒体 %s/%s 下载返回为空", msg.chat_id, msg.id)
        except Exception as e:
            log.warning("阅后即焚媒体 %s/%s 下载失败: %s", msg.chat_id, msg.id, e)

        # 立即发送至日志频道
        ttl = getattr(msg, "ttl_period", None) or getattr(getattr(msg, "media", None), "ttl_seconds", None) or "?"
        header = f"\u2702\ufe0f **阅后即焚消息 来自:** {sender}\n在 **{chat_name}**  `{msg.chat_id}`\n"
        body = f"\n_阅后即焚消息 (TTL: {ttl} 秒)_\n"
        if msg.raw_text:
            body += f"\n**消息文本:**\n{msg.raw_text}\n"
        if media_path and os.path.exists(media_path):
            try:
                await bot.send_file(LOG_CHANNEL, media_path, caption=header + body, parse_mode="md")
            except Exception:
                body += "\n_(媒体无法转发)_"
                await bot.send_message(LOG_CHANNEL, header + body, parse_mode="md", link_preview=False)
        else:
            body += "\n_(媒体无法保存)_"
            await bot.send_message(LOG_CHANNEL, header + body, parse_mode="md", link_preview=False)

    elif mtype in ("photo", "video", "document"):
        media_path = await cache_media(msg, mtype)

    if can_log:
        db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)
    await check_and_forward(msg, sender, chat_name, media_path)


async def on_edited(event):
    if not LOG_EDITED:
        return
    msg = event.message
    if not await should_log_chat(msg.chat_id):
        return

    sender_entity = await msg.get_sender()
    if IGNORE_BOTS and getattr(sender_entity, "bot", False):
        return

    if not should_log_sender(msg.chat_id, msg.sender_id):
        return

    mtype = media_type(msg)
    if not should_log_media(mtype) and not msg.raw_text:
        return

    # 仅在存在先前缓存版本时记录 — 跳过频道的首次历史推送
    old = db_get(msg.chat_id, msg.id)
    if old is None:
        # 首次获取该消息，直接加入缓存
        sender = format_sender_name(sender_entity)
        chat_name = await get_chat_name(msg)
        media_path = None
        media_size = get_media_size(msg)
        if mtype in ("photo", "video", "document"):
            media_path = await cache_media(msg, mtype)
        db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)
        return

    # 若编辑时间 edit_date 未变更（例如仅触发反应表情、置顶等），则跳过
    current_edit = str(msg.edit_date) if msg.edit_date else None
    stored_edit = old[8]  # edit_date 列
    if current_edit == stored_edit:
        return

    # 若消息实际文本未变，则跳过
    if old[4] == msg.raw_text:
        # 更新存储的 edit_date，避免重复检查
        db.execute("UPDATE messages SET edit_date=? WHERE chat_id=? AND msg_id=?", (current_edit, msg.chat_id, msg.id))
        db.commit()
        return

    sender = format_sender_name(sender_entity)
    chat_name = await get_chat_name(msg)

    header = f"\u270f\ufe0f **已编辑消息 来自:** {sender}\n在 **{chat_name}**  `{msg.chat_id}`\n"
    body = ""
    if old[4]:  # 原始文本
        body += f"\n**原始消息:**\n{old[4]}\n"
    if msg.raw_text:
        body += f"\n**编辑后消息:**\n{msg.raw_text}\n"

    await send_log_from_msg(header, msg, body)

    # 用新版本更新缓存
    media_path = None
    media_size = get_media_size(msg)
    if mtype in ("photo", "video", "document"):
        media_path = await cache_media(msg, mtype)
    db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)


async def on_deleted(event):
    if not LOG_DELETED:
        return
    for msg_id in event.deleted_ids:
        chat_id = event.chat_id or 0

        row = db_get(chat_id, msg_id) if chat_id else None
        if row is None:
            row = db_get_by_msg_id(msg_id)
            if row:
                chat_id = row[0]

        # 未在数据库中找到 — 尝试从 Telegram 实时获取（针对 Bot 启动前发送的较早消息）
        if row is None and chat_id:
            try:
                msgs = await client.get_messages(chat_id, ids=msg_id)
                msg = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
                if msg and msg.id:
                    sender_entity = await msg.get_sender()
                    if IGNORE_BOTS and getattr(sender_entity, "bot", False):
                        continue
                    if not should_log_sender(chat_id, msg.sender_id):
                        continue

                    sender = format_sender_name(sender_entity)
                    chat_name = await get_chat_name(msg)
                    mtype = media_type(msg)

                    if not await should_log_chat(chat_id):
                        continue
                    if not should_log_media(mtype):
                        continue

                    header = f"\u2702\ufe0f **已删除消息 来自:** {sender}\n在 **{chat_name}**  `{chat_id}`\n"
                    body = ""
                    if msg.raw_text:
                        body += f"\n**原始消息:**\n{msg.raw_text}\n"
                    await send_log_from_msg(header, msg, body)
                    continue
            except Exception as e:
                log.warning("无法从聊天 %s 获取已删除消息 %s: %s", chat_id, msg_id, e)

        if row is None:
            continue
        if not await should_log_chat(chat_id):
            continue

        mtype = row[5]
        if not should_log_media(mtype):
            continue

        sender = row[2] or "未知"
        chat_name = row[3] or "未知"
        header = f"\u2702\ufe0f **已删除消息 来自:** {sender}\n在 **{chat_name}**  `{chat_id}`\n"

        body = ""
        if row[4]:  # 文本内容
            body += f"\n**原始消息:**\n{row[4]}\n"

        await send_log_from_db(header, row, body)
        db_remove(chat_id, msg_id)


async def on_read_contents(event):
    """处理阅后即焚消息被阅读/打开的事件。"""
    if not LOG_SELF_DESTRUCT:
        return
    update = event.original_update
    if not isinstance(update, UpdateReadMessagesContents):
        return
    for msg_id in update.messages:
        row = db_get_by_msg_id(msg_id)
        if row is None or row[5] != "self_destruct":
            continue
        chat_id = row[0]
        media_path = row[6]

        sender = row[2] or "未知"
        chat_name = row[3] or "未知"
        header = f"\u2702\ufe0f **阅后即焚消息 来自:** {sender}\n在 **{chat_name}**  `{chat_id}`\n"
        body = "\n_阅后即焚消息已查看_\n"
        if row[4]:
            body += f"\n**消息文本:**\n{row[4]}\n"
        if not media_path or not os.path.exists(media_path):
            body += "\n_(媒体无法保存 — Telegram 阻止下载带有有效期的媒体)_\n"

        await send_log_from_db(header, row, body)
        db_remove(chat_id, msg_id)


# ---------------------------------------------------------------------------
# 登录流程
# ---------------------------------------------------------------------------
async def login_user():
    await client.connect()
    if await client.is_user_authorized():
        return

    log.info("正在启动账号登录认证，手机号: %s", PHONE)
    log.info("正在向 Telegram 请求验证码，请查收手机/客户端通知...")

    def get_code():
        print("\n" + "=" * 50, flush=True)
        print(">>> 请在此输入 Telegram 发送给您的登录验证码: ", end="", flush=True)
        code = input().strip()
        print("=" * 50 + "\n", flush=True)
        return code

    def get_password():
        print("\n" + "=" * 50, flush=True)
        print(">>> 请在此输入两步验证密码 (2FA): ", end="", flush=True)
        pwd = input().strip()
        print("=" * 50 + "\n", flush=True)
        return pwd

    await client.start(
        phone=PHONE,
        code_callback=get_code,
        password=PASSWORD if PASSWORD else get_password,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    bot_me = await bot.get_me()
    log.info("机器人已启动: @%s", bot_me.username)

    await login_user()

    me = await client.get_me()
    log.info("已成功登录为 %s (id=%s)", me.first_name, me.id)

    try:
        entity = await bot.get_entity(LOG_CHANNEL)
        log.info("日志目标: %s", getattr(entity, "title", entity))
    except Exception as e:
        log.error("机器人无法访问日志目标 %s (请确认已将机器人拉入群组/频道并赋予发消息权限): %s", LOG_CHANNEL, e)
        return

    cached = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    log.info("持久化缓存: %d 条消息", cached)

    # 始终同时监听接收与发送的消息，确保可以捕获并记录任何删除操作
    client.add_event_handler(on_new_message, events.NewMessage(incoming=True, outgoing=True))
    client.add_event_handler(on_edited, events.MessageEdited(incoming=True, outgoing=True))
    client.add_event_handler(on_deleted, events.MessageDeleted())
    client.add_event_handler(on_read_contents, events.Raw(UpdateReadMessagesContents))

    # 初始化频道关键词监控与 MacroDroid 告警模块
    if init_keyword_alerter:
        init_keyword_alerter(client, cfg)

    log.info("模式: %s | 启用状态: %s | 正在监听...", MODE, ENABLED)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
