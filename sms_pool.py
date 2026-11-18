#!/usr/bin/env python3
"""
接码平台适配器（默认按国内接码平台通用 HTTP 形态，可为任意平台改路径与字段）

约定：
  取号  GET {base}{path_get_phone}?token=..&projectId=..&num=1
  取码  GET {base}{path_get_code}?token=..&projectId=..&phone=..
  释放  GET {base}{path_release}?token=..&phone=..
  拉黑  GET {base}{path_blacklist}?token=..&phone=..

路径里的占位符：{token} {project} {phone} {num} {item_id}
响应兼容：JSON（任意层级含手机号/验证码字段）、纯文本、以及 "手机号----验证码" 这种拼接串。
"""

# 声明：与科大讯飞无任何从属关系，仅供与自己账号的互操作性、个人使用；请勿用于滥用、转售或压垮上游服务。见 NOTICE.md。
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional

import httpx

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
CODE_KEYS = ("code", "smscode", "sms_code", "vcode", "verifycode", "verify_code", "msgcode", "msg_code", "content", "sms", "text", "msg")
PHONE_KEYS = ("phone", "mobile", "phoneNumber", "phone_number", "number", "num")
IGNORE_CODE_KEYS = ("msgid", "msgId", "phone", "mobile", "id", "orderid", "order_id", "taskid", "task_id", "traceid", "timestamp", "time")

DEFAULT_CFG: dict[str, Any] = {
    "enabled": False,
    "provider": "texin",
    "base_url": "",
    "username": "",
    "password": "",
    "token": "",
    "project_id": "",
    "item_id": "",
    "path_login": "/Login/?username={username}&password={password}&type=json",
    "path_balance": "/Balance/?token={token}&type=json",
    "path_get_phone": "/GetPhone/?token={token}&id={project}&type=json",
    "path_get_code": "/GetMsg/?token={token}&id={project}&phone={phone}&type=json",
    "path_release": "/Cancel/?token={token}&id={project}&phone={phone}&type=json",
    "path_blacklist": "/Addblack/?token={token}&id={project}&phone={phone}&type=json",
    "code_regex": "",          # 留空自动识别 4~6 位数字
    "phone_regex": "",
    "poll_interval": 3,        # 秒
    "code_timeout": 120,       # 秒
    "concurrency": 1,
    "retry_get_phone": 3,
    "attempts": 10,            # 每个账号最多消耗几个号码（不来码立刻换号）
    "block_prefixes": "192",   # 屏蔽号段（逗号分隔），192 广电号段收不到讯飞短信
    "prefer_prefixes": "",     # 优先号段（逗号分隔），如 170 实测能收到讯飞短信
    "blacklist_on_fail": True,
    "release_after_success": False,
    "note_prefix": "接码",
}


class PoolError(Exception):
    """接码平台返回业务错误。"""


def _deep_strings(node: Any, key: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_deep_strings(v, str(k)))
    elif isinstance(node, list):
        for item in node:
            out.extend(_deep_strings(item, key))
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        out.append((key, str(node)))
    return out


ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}")


def clean_text(raw_text: str = "") -> str:
    """去掉 JSON/字符串里的 \\uXXXX 转义，否则转义里的数字会被误判成手机号/验证码。"""
    return ESCAPE_RE.sub(" ", raw_text or "")


def parse_phone(payload: Any, raw_text: str = "") -> Optional[str]:
    for key, value in _deep_strings(payload):
        if key in PHONE_KEYS:
            match = PHONE_RE.search(value)
            if match:
                return match.group(0)
    match = PHONE_RE.search(clean_text(raw_text))
    return match.group(0) if match else None


def parse_code(payload: Any, raw_text: str = "", phone: str = "", pattern: str = "") -> Optional[str]:
    regex = re.compile(pattern) if pattern else re.compile(r"(?<!\d)(\d{4,6})(?!\d)")
    for key, value in _deep_strings(payload):
        if key in IGNORE_CODE_KEYS or key.lower() in ("msgid", "msg_id"):
            continue
        match = regex.search(value)
        if match:
            candidate = match.group(match.lastindex or 0)
            if phone and candidate == phone:
                continue
            return candidate
    text = clean_text(raw_text)
    if phone:
        text = text.replace(phone, " ")
    # 纯文本回落：整段都是 JSON 时不走这条，避免把字段结构里的数字当验证码
    if text.strip().startswith("{"):
        return None
    match = regex.search(text)
    return match.group(match.lastindex or 0) if match else None


def parse_error(payload: Any, raw_text: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "desc", "error", "info", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        for key in ("code", "status"):
            if key in payload and str(payload.get(key)) not in ("0", "1", "200", "000000"):
                return "code=%s" % payload.get(key)
    return (raw_text or "")[:200]


PIPE_KEYS = ("token", "phone", "code", "sms")


def parse_pipe(raw_text: str) -> tuple[bool, list[str]]:
    """他信文本协议：`1|字段1|字段2` 成功，`0|错误信息` 失败。"""
    text = (raw_text or "").strip()
    if "|" not in text:
        return False, []
    head, *rest = text.split("|")
    return head.strip() == "1", [item.strip() for item in rest]


def pipe_payload(raw_text: str, index: int = 0) -> str:
    ok, fields = parse_pipe(raw_text)
    if ok and len(fields) > index:
        return fields[index]
    return ""


def looks_failed(payload: Any, raw_text: str = "") -> bool:
    if isinstance(payload, dict):
        for key in ("code", "status", "errcode", "errno"):
            if key in payload:
                value = str(payload.get(key)).strip().lower()
                if value in ("0", "1", "200", "000000", "ok", "true"):
                    return False
                return True
        if payload.get("success") is False:
            return True
    text = (raw_text or "").strip().lower()
    return text in ("", "null", "false") or "失败" in text or "错误" in text


class SmsProvider:
    def __init__(self, cfg: dict[str, Any], on_token=None):
        merged = dict(DEFAULT_CFG)
        merged.update({k: v for k, v in (cfg or {}).items() if v is not None})
        self.cfg = merged
        self.base = str(merged.get("base_url") or "").rstrip("/")
        self.on_token = on_token
        self._token = str(merged.get("token") or "").strip()

    # ---------------- internals

    def _url(self, path_key: str, phone: str = "", num: int = 1) -> str:
        path = str(self.cfg.get(path_key) or "")
        values = {
            "token": str(self.cfg.get("token") or ""),
            "project": str(self.cfg.get("project_id") or ""),
            "item_id": str(self.cfg.get("item_id") or ""),
            "phone": phone,
            "num": str(num),
            "username": str(self.cfg.get("username") or ""),
            "password": str(self.cfg.get("password") or ""),
            "ts": str(int(time.time())),
        }
        out = path
        for key, value in values.items():
            out = out.replace("{%s}" % key, httpx.QueryParams({"x": value}).get("x") or "")
        if not out.startswith("http"):
            out = self.base + (out if out.startswith("/") else "/" + out)
        return out

    async def _get(self, client: httpx.AsyncClient, url: str) -> tuple[Any, str, int]:
        response = await client.get(url, headers={"Accept": "application/json, text/plain, */*"})
        text = response.text
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = None
        return payload, text, response.status_code

    # ---------------- auth

    def _need_login(self, payload: Any, text: str) -> bool:
        ok, fields = parse_pipe(text)
        if "|" in text and not ok:
            return any(word in (fields[0] if fields else text) for word in ("token", "登录", "账号", "锁定"))
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
            return any(word in message for word in ("token", "登录", "账号", "锁定"))
        return False

    async def login(self, client: httpx.AsyncClient) -> str:
        username = str(self.cfg.get("username") or "")
        password = str(self.cfg.get("password") or "")
        if not (username and password):
            raise PoolError("未配置平台账号密码，且没有可用 token")
        url = self._url("path_login")
        payload, text, status = await self._get(client, url)
        ok, fields = parse_pipe(text)
        if ok and fields:
            token = fields[0]
            self._token = token
            self.cfg["token"] = token
            if self.on_token:
                try:
                    self.on_token(token)
                except Exception:  # noqa: BLE001
                    pass
            return token
        if isinstance(payload, dict) and (payload.get("stat") is True or str(payload.get("code")) == "1"):
            data = payload.get("data")
            token = data if isinstance(data, str) else str((data or {}).get("token") or "")
            if token:
                self._token = token
                self.cfg["token"] = token
                if self.on_token:
                    self.on_token(token)
                return token
        raise PoolError("平台登录失败：%s" % (parse_error(payload, text) or text[:120] or ("HTTP %s" % status)))

    async def ensure_token(self, client: httpx.AsyncClient) -> str:
        if self._token:
            return self._token
        return await self.login(client)

    async def balance(self, client: httpx.AsyncClient) -> dict[str, Any]:
        await self.ensure_token(client)
        payload, text, status = await self._get(client, self._url("path_balance"))
        ok, fields = parse_pipe(text)
        if ok and fields:
            return {"ok": True, "balance": fields[0], "raw": text}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload["data"]
            return {"ok": True, "balance": data.get("money") or data.get("balance"), "raw": payload}
        return {"ok": False, "error": parse_error(payload, text) or ("HTTP %s" % status)}

    # ---------------- public

    async def test(self, client: httpx.AsyncClient) -> dict[str, Any]:
        token = await self.ensure_token(client)
        bal = await self.balance(client)
        url = self._url("path_get_phone")
        payload, text, status = await self._get(client, url)
        ok, fields = parse_pipe(text)
        phone = (parse_phone(None, fields[0]) if ok and fields else None) or parse_phone(payload, text)
        return {
            "url": re.sub(r"(token|password)=[^&]+", r"\1=***", url),
            "status": status,
            "login_token": (token[:8] + "***") if token else "",
            "balance": bal.get("balance"),
            "payload": payload,
            "text": text[:300],
            "phone": phone,
            "released": False,
        }

    async def get_phone(self, client: httpx.AsyncClient) -> str:
        last = ""
        for attempt in range(max(1, int(self.cfg.get("retry_get_phone") or 1))):
            await self.ensure_token(client)
            url = self._url("path_get_phone")
            payload, text, status = await self._get(client, url)
            ok, fields = parse_pipe(text)
            if ok and fields:
                phone = parse_phone(None, fields[0]) or parse_phone(None, " ".join(fields))
                if phone:
                    return phone
            elif "|" in text and not ok:
                last = (fields[0] if fields else text)[:120]
            else:
                phone = parse_phone(payload, text)
                if phone:
                    return phone
                last = parse_error(payload, text) or ("HTTP %s" % status)
            if self._need_login(payload, text):
                self._token = ""
                try:
                    await self.login(client)
                    continue
                except PoolError as error:
                    last = str(error)
            await asyncio.sleep(1.5)
        raise PoolError("取号失败：%s" % (last or "平台未返回手机号"))

    async def get_code(self, client: httpx.AsyncClient, phone: str) -> Optional[str]:
        await self.ensure_token(client)
        payload, text, _ = await self._get(client, self._url("path_get_code", phone=phone))
        ok, fields = parse_pipe(text)
        if ok and fields:
            joined = " ".join(fields)
            return parse_code(None, joined, phone, str(self.cfg.get("code_regex") or ""))
        return parse_code(payload, text, phone, str(self.cfg.get("code_regex") or ""))

    async def release(self, client: httpx.AsyncClient, phone: str) -> None:
        try:
            await self.ensure_token(client)
            await self._get(client, self._url("path_release", phone=phone))
        except Exception:  # noqa: BLE001
            pass

    async def blacklist(self, client: httpx.AsyncClient, phone: str) -> None:
        try:
            await self.ensure_token(client)
            await self._get(client, self._url("path_blacklist", phone=phone))
        except Exception:  # noqa: BLE001
            pass
