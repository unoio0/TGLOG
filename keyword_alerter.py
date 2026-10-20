import asyncio
import logging
import time
import urllib.parse
import urllib.request
from telethon import events

log = logging.getLogger("tg-logger.alerter")


def _send_http_get(target_url, timeout=8):
    """同步发送 HTTP GET 请求（供 asyncio.to_thread 异步调用）。"""
    req = urllib.request.Request(
        target_url,
        headers={"User-Agent": "TGLOG-Alerter/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


class KeywordAlertRule:
    def __init__(self, rule_dict):
        self.enabled = rule_dict.get("enabled", True)
        
        # 规范化频道匹配目标（支持数字 ID 和小写 username）
        raw_channels = rule_dict.get("channels", [])
        if isinstance(raw_channels, (int, str)):
            raw_channels = [raw_channels]
        
        self.channel_ids = set()
        self.channel_usernames = set()
        for ch in raw_channels:
            ch_str = str(ch).strip()
            if ch_str.lstrip('-').isdigit():
                self.channel_ids.add(int(ch_str))
            else:
                self.channel_usernames.add(ch_str.lstrip('@').lower())

        # 规范化关键词（转小写）
        raw_keywords = rule_dict.get("keywords", [])
        if isinstance(raw_keywords, str):
            raw_keywords = [raw_keywords]
        self.keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        self.keywords_lower = [k.lower() for k in self.keywords]

        self.webhook_url = rule_dict.get("webhook_url", "").strip()
        self.cooldown_seconds = int(rule_dict.get("cooldown_seconds", 10))
        self.last_triggered_time = 0.0

    def match_channel(self, chat_id, username):
        """检查消息来源是否匹配当前规则的频道。"""
        if chat_id in self.channel_ids:
            return True
        if username and username.lower() in self.channel_usernames:
            return True
        return False

    def match_keywords(self, text):
        """检查消息文本是否命中任意关键词，返回命中的原关键词字符串或 None。"""
        if not text or not self.keywords_lower:
            return None
        text_lower = text.lower()
        for idx, kw_lower in enumerate(self.keywords_lower):
            if kw_lower in text_lower:
                return self.keywords[idx]
        return None

    def in_cooldown(self, now):
        """检查是否处于冷却期。"""
        return (now - self.last_triggered_time) < self.cooldown_seconds


def init_keyword_alerter(client, cfg):
    """初始化并挂载关键词告警监听器到 Telethon Client。"""
    raw_rules = cfg.get("keyword_alerts", [])
    if not raw_rules:
        return

    rules = [KeywordAlertRule(r) for r in raw_rules if isinstance(r, dict) and r.get("enabled", True) and r.get("webhook_url")]
    if not rules:
        return

    log.info("已加载 %d 条关键词监控告警规则", len(rules))

    @client.on(events.NewMessage)
    async def on_keyword_message(event):
        msg = event.message
        if not msg:
            return

        chat_id = msg.chat_id
        chat = None
        username = None
        chat_title = None

        text = msg.raw_text or ""
        if not text:
            return

        now = time.time()

        for rule in rules:
            if not rule.enabled:
                continue

            # 延迟获取 chat 信息以提高性能
            if chat is None:
                try:
                    chat = await msg.get_chat()
                    username = getattr(chat, "username", None)
                    chat_title = getattr(chat, "title", str(chat_id))
                except Exception:
                    chat_title = str(chat_id)

            if not rule.match_channel(chat_id, username):
                continue

            matched_kw = rule.match_keywords(text)
            if not matched_kw:
                continue

            if rule.in_cooldown(now):
                log.info(
                    "🔔 [关键词告警] 频道 %s (%s) 命中关键词 '%s'，但处于冷却期内（防抖跳过）",
                    chat_title, chat_id, matched_kw
                )
                continue

            # 记录最后触发时间，进入冷却
            rule.last_triggered_time = now

            log.info(
                "🔔 [关键词告警] 频道 %s (%s) 命中关键词 '%s'！正在触发 MacroDroid 闹钟 Webhook...",
                chat_title, chat_id, matched_kw
            )

            # 直接请求纯净 Webhook URL（单纯响闹钟，不拼接任何额外参数）
            try:
                target_url = rule.webhook_url

                # 异步线程执行 HTTP 请求，不阻塞主事件循环
                status_code, _resp = await asyncio.to_thread(_send_http_get, target_url, timeout=8)
                log.info("⏰ [关键词告警] MacroDroid 闹钟 Webhook 触发成功 (HTTP %s)！", status_code)
            except Exception as e:
                log.warning("❌ [关键词告警] MacroDroid Webhook 请求失败: %s", e)
