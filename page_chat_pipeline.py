"""页面模式（page mode）服务器侧管线：生成链接、登记待发意图、读回回复；「发」这一步由官方页面自己完成。"""

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from urllib.parse import urlencode

import aiohttp

import functions

logger = logging.getLogger("page_chat_pipeline")

PAGE_MODE_ENV = "DEEPSEEKER_PAGE_MODE"
PAGE_TIMEOUT_ENV = "DEEPSEEKER_PAGE_TIMEOUT"
# [Snc2] 读回形态归位：可选，默认关。详见 docs/GUIDE.md 第 3 章。
# 页模式把「发」交给官方页面，读回却仍用 functions.get_headers() 的安卓头，
# 同一会话里发/读形态对不上。本开关让读回改穿 web 形态，只换形状，
# 令牌宿主与出口一律不动；有效性未验证，故默认关、显式开、无静默回退。
PAGE_WEB_READ_ENV = "DEEPSEEKER_PAGE_WEB_READ"
# web 读回头的键集合（单一事实源，与读回适配器的键集合对齐）。
WEB_READ_HEADER_KEYS = (
    "accept", "accept-language", "content-type", "origin", "referer",
    "user-agent", "authorization",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
)
READ_NEVER_HAS = ("x-ds-pow-response", "x-hif-leim")  # 读回不带，只有 completion 带
_WEB_READ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_WEB_READ_BASE = "https://chat.deepseek.com"
POLL_INTERVAL_S = 2.0
AUTOSEND_MAX_LEN = 800
PENDING_MAX = 64

RESPONSE_TYPES = {"RESPONSE", "TEMPLATE_RESPONSE"}
THINK_TYPES = {"THINK"}
_FINISHED = {"FINISHED", "FINISH", "DONE", "COMPLETED", "SUCCESS", "OK"}
_WIP = {"PENDING", "WIP", "GENERATING", "IN_PROGRESS", "RUNNING", "CREATED", ""}


def page_mode_enabled():
    """默认关：仅当 DEEPSEEKER_PAGE_MODE 置位时启用。"""
    return os.getenv(PAGE_MODE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def page_timeout():
    try:
        v = float(os.getenv(PAGE_TIMEOUT_ENV, "300"))
        return v if v > 0 else 300.0
    except (TypeError, ValueError):
        return 300.0


def page_web_read_enabled():
    """默认关：仅当 DEEPSEEKER_PAGE_WEB_READ 置位时启用；关时读回逐字沿用旧头，行为不变。"""
    return os.getenv(PAGE_WEB_READ_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def web_read_headers(token):
    """web 形态读回头：键集合 = WEB_READ_HEADER_KEYS 逐字对齐；值不含本机指纹。

    令牌只在内存里拼进 authorization，绝不落盘。断言同时挡住两处漂移：
    键集合不符、或混入 PoW/leim（那是取食物 completion 才带的，读回不该有）。
    """
    h = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json",
        "origin": _WEB_READ_BASE,
        "referer": _WEB_READ_BASE + "/",
        "user-agent": _WEB_READ_UA,
        "authorization": "Bearer " + token,  # 只进内存
        "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    assert set(h) == set(WEB_READ_HEADER_KEYS), set(h) ^ set(WEB_READ_HEADER_KEYS)
    assert not (set(h) & set(READ_NEVER_HAS))
    return h


class PageChatError(Exception):
    """携带 HTTP 状态码、供调用方使用的异常。"""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail


def build_autosend_url(prompt, search=False, thinking=False, base=None):
    """构造 autosend URL，纯字符串拼接；超长 prompt 直接拒绝，绝不截断。"""
    q = (prompt or "").strip()
    if not q:
        raise PageChatError(400, "page mode: empty prompt")
    if len(q) > AUTOSEND_MAX_LEN:
        raise PageChatError(
            400, "page mode: prompt exceeds %d-char autosend limit" % AUTOSEND_MAX_LEN)
    base = (base or functions.UPSTREAM_BASES[0]).rstrip("/")
    params = {"q": q, "autosend": "1"}
    if search:
        params["search"] = "1"
    if thinking:
        params["thinking"] = "1"
    return base + "/?" + urlencode(params)


_pending = []
_pending_lock = threading.Lock()


def register_intent(url, prompt):
    intent = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "prompt_preview": re.sub(r"\s+", " ", (prompt or ""))[:60],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _pending_lock:
        _pending.append(intent)
        del _pending[:-PENDING_MAX]
    return intent


def take_pending():
    """取出全部待发 URL 并清空队列。"""
    with _pending_lock:
        out = _pending[:]
        _pending.clear()
    return out


def pending_count():
    with _pending_lock:
        return len(_pending)


async def _get_json(url, token, timeout_s=15.0):
    """单次 GET，返回 (status, body)；网络错误作为返回值，不抛出。"""
    session = await functions.get_session()
    # [Snc2] 默认关 -> 逐字沿用 functions.get_headers()（老路径，行为不变）;
    # 显式开 -> web 形态读回头。此处不做任何「失败就退回安卓头」的静默回退：
    # 失败一律走既有 _assert_alive()/超时报错，避免两种形态混进一次会话。
    headers = (web_read_headers(token) if page_web_read_enabled()
               else functions.get_headers(token))
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            body = await resp.json(content_type=None)
            return resp.status, body
    except Exception as exc:
        return None, {"_error": str(exc)[:200]}


async def _assert_alive(token):
    """读回令牌无效或被禁言时提前失败。"""
    r = await functions.probe_token_mute(token)
    v = r.get("verdict")
    if v == "invalid":
        raise PageChatError(503, "page mode: read token invalid upstream (%s)" % (r.get("detail") or ""))
    if v == "muted":
        raise PageChatError(429, "page mode: account muted until %s" % (r.get("mute_until") or "?"))


def _g(d, *names):
    """在多个候选键名间做容错查找。"""
    for n in names:
        v = d.get(n)
        if v is not None:
            return v
    return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _find_dict_lists(obj, key_pred):
    """找出「元素为 dict 且带指定键之一」的列表。"""
    found = []

    def walk(x):
        if isinstance(x, list):
            dicts = [i for i in x if isinstance(i, dict)]
            if dicts and any(any(k in d for k in key_pred) for d in dicts):
                found.append(dicts)
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)

    walk(obj)
    return found


def _ts_key(d):
    for v in (_g(d, "updated_at", "update_time", "last_message_at", "created_at", "inserted_at"),):
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _frag_text(msg, types):
    out = []
    for f in (_g(msg, "fragments") or []):
        if isinstance(f, dict) and f.get("type") in types and isinstance(f.get("content"), str):
            out.append(f["content"])
    return "".join(out)


def _locate_user_message(msgs, marker, t0):
    """按 prompt 尾部定位本轮用户消息，失败时回退到时间戳。"""
    best = (-1, None)
    for i, m in enumerate(msgs):
        role = str(_g(m, "role") or "").upper()
        if role != "USER":
            continue
        text = _norm(_frag_text(m, {"REQUEST"}) or _g(m, "content") or "")
        if marker and marker in text:
            best = (i, m)
    if best[1] is not None:
        return best
    for i, m in enumerate(msgs):
        role = str(_g(m, "role") or "").upper()
        if role != "USER":
            continue
        ts = _ts_key(m)
        if ts and ts >= t0 - 10:
            best = (i, m)
    return best


def _locate_reply(msgs, user_idx):
    """按 parent id 定位回复，失败时回退到下一条 assistant 消息。"""
    if user_idx < 0 or user_idx >= len(msgs):
        return None
    um = msgs[user_idx]
    uid = _g(um, "message_id", "messageId", "id")
    if uid is not None:
        for m in msgs:
            if str(_g(m, "role") or "").upper() != "ASSISTANT":
                continue
            pid = _g(m, "parent_id", "parentId", "parent_message_id")
            if pid is not None and str(pid) == str(uid):
                return m
    for m in reversed(msgs[user_idx + 1:]):
        if str(_g(m, "role") or "").upper() == "ASSISTANT":
            return m
    return None


def _settled(reply, unknown_status):
    """状态为已完成即判完成（或状态缺失但已有正文）。"""
    if reply is None:
        return False
    sv = _g(reply, "status", "message_status", "state", "gen_status")
    if sv is None:
        return True
    if isinstance(sv, str):
        su = sv.strip().upper()
        if su in _FINISHED:
            return True
        if su in _WIP:
            return False
        unknown_status.add(sv)
        return False
    unknown_status.add(repr(sv))
    return False


def _log_field_observation(msgs, unknown_status):
    """超时时只记录键名与状态枚举，绝不记录消息正文。"""
    try:
        roles = sorted({str(_g(m, "role")) for m in msgs if isinstance(m, dict)})
        keys = sorted({k for m in msgs[:5] if isinstance(m, dict) for k in m.keys()})
        logger.warning(
            "page mode timeout field-observation: msgs=%d roles=%s msg_keys=%s unknown_status=%s",
            len(msgs), roles, keys, sorted(unknown_status))
    except Exception:
        pass


async def poll_reply(token, prompt, timeout_s=None, interval=POLL_INTERVAL_S,
                     base=None, search=False):
    """轮询只读端点直到回复完成，返回 (body, thinking)。"""
    timeout_s = timeout_s or page_timeout()
    base = (base or functions.UPSTREAM_BASES[0]).rstrip("/")
    deadline = time.time() + timeout_s
    t0 = time.time()
    marker = _norm(prompt)[-48:]
    unknown_status = set()
    last_alive = 0.0
    msgs = []

    while time.time() < deadline:
        if time.time() - last_alive > 30:
            await _assert_alive(token)
            last_alive = time.time()

        sess_id = None
        st, page_body = await _get_json(base + "/api/v0/chat_session/fetch_page", token)
        if st == 200:
            for lst in _find_dict_lists(page_body, ("chat_session_id", "session_id")):
                cand = max(lst, key=_ts_key)
                v = _g(cand, "chat_session_id", "session_id", "id")
                if v is not None:
                    sess_id = v
                    break

        if sess_id is not None:
            st2, hist = await _get_json(
                base + "/api/v0/chat/history_messages?chat_session_id=" + str(sess_id), token)
            if st2 == 200:
                msgs = []
                for lst in _find_dict_lists(hist, ("fragments", "role")):
                    for m in lst:
                        if isinstance(m, dict) and (isinstance(m.get("fragments"), list) or "role" in m):
                            msgs.append(m)

        user_idx, user_msg = _locate_user_message(msgs, marker, t0)
        if user_msg is not None:
            reply = _locate_reply(msgs, user_idx)
            if _settled(reply, unknown_status):
                text = _frag_text(reply, RESPONSE_TYPES)
                if text.strip():
                    think = _frag_text(reply, THINK_TYPES)
                    return text, think

        await asyncio.sleep(interval)

    _log_field_observation(msgs, unknown_status)
    raise PageChatError(
        504,
        "page mode: no finished reply within %.0fs (did the carrier load the URL? "
        "is the browser account the same as the read token?)" % timeout_s)
