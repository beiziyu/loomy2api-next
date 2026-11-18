#!/usr/bin/env python3
"""
Loomy2API · 把 Loomy 桌面端登录态（imodel provider）反代成 OpenAI 兼容 API

上游：https://loomyad.xunfei.cn/api/v1
认证：登录 session 同时作为 `Authorization: Bearer` 与 `token` 头（桌面端 useSessionAuth 行为）

对外：
  GET  /v1/models                 OpenAI 模型列表
  POST /v1/chat/completions       OpenAI 对话（支持 stream=true SSE 透传）
  GET  /admin                     管理面板
  /admin/api/*                    面板接口

环境变量：
  LOOMY2API_CONFIG    配置文件路径，默认 ./config.json
  LOOMY2API_DAILY_DIR Loomy-Daily 目录（复用其签到/任务脚本），默认 ../Loomy-Daily
"""

# 声明：与科大讯飞无任何从属关系，仅供与自己账号的互操作性、个人使用；请勿用于滥用、转售或压垮上游服务。见 NOTICE.md。
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Optional

import httpx
import sms_pool
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LOOMY2API_CONFIG") or (HERE / "config.json"))
DAILY_DIR = Path(os.environ.get("LOOMY2API_DAILY_DIR") or (HERE.parent / "Loomy-Daily"))
LOG_DIR = HERE / "logs"
TZ = timezone(timedelta(hours=8))

UPSTREAM_DEFAULT = "https://loomyad.xunfei.cn/api/v1"
ACCOUNT_BASE = "https://account.xfinfr.com"
SEND_CODE_PATH = "/login/phone/sendMsgCode"
LOGIN_PATH = "/login/phone/checkCode"
# 讯飞账号服务（account.xfinfr.com）签名用的 AK/SK：
# 默认值就是 Loomy 官方桌面客户端 resources/.env.prod 里内置的那一对（每个安装包都有）。
# 想换成自己客户端的，在 config.json 里写 account_access_key_id / account_access_key_secret。
DEFAULT_ACCESS_KEY_ID = "2thryby66wxi53sk"
DEFAULT_ACCESS_KEY_SECRET = "zsak6eadrbawz683wf5r3m2snrwj868r"


def access_key_pair() -> tuple[str, str]:
    cfg_id = str(CFG.get("account_access_key_id") or "").strip()
    cfg_secret = str(CFG.get("account_access_key_secret") or "").strip()
    return (cfg_id or DEFAULT_ACCESS_KEY_ID, cfg_secret or DEFAULT_ACCESS_KEY_SECRET)
APP_ID = "GM3LOOMY"
MODEL_ID = "Web"
DEVICE_ID = "web"
CLIENT_UA = "Loomy|Desktop|Electron|macOS"
CLIENT_VERSION = "1.0.0"
CLIENT_UA_GATEWAY = "Loomy|Desktop|Electron|macOS"
SUCCESS_CODE = "000000"

SESSION_ERROR_CODES = {"100002", "100001", "100003", "100004", "401", "403"}
SESSION_ERROR_HINTS = ("token", "session", "登录", "未授权", "过期")

DEFAULT_CONFIG: dict[str, Any] = {
    "listen": {"host": "127.0.0.1", "port": 4556},
    "upstream_base": UPSTREAM_DEFAULT,
    "loomy_version": "0.9.39",
    "api_keys": [],
    "panel_password": "",
    "default_model": "",
    "fallback_model": "qwen3.8-flash",
    "daily_time": "08:45",
    "daily_enabled": True,
    "balance_interval_min": 30,
    "identity_mode": "per_account",
    "strategy": "balance",
    "cooldown_seconds": 300,
    "max_retries": 2,
    "accounts": [],
    "usage": {"requests": 0, "ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0},
    "daily_state": {},
    "models_catalog": {},
    "sms_pool": dict(sms_pool.DEFAULT_CFG),
}

CONFIG_LOCK = asyncio.Lock()
MODEL_CACHE: dict[str, Any] = {"at": 0.0, "data": []}


# ---------------------------------------------------------------- config


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    cfg[k] = v
        except Exception as error:  # noqa: BLE001
            print(f"[config] 读取失败，使用默认: {error}", flush=True)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


CFG = load_config()


def now_ts() -> int:
    return int(time.time())


def mask(token: str) -> str:
    token = str(token or "")
    if len(token) <= 8:
        return token[:2] + "***"
    return token[:4] + "*" * 6 + token[-4:]


def log_line(name: str, record: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        record = {"ts": datetime.now(TZ).strftime("%F %T"), **record}
        with open(LOG_DIR / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- upstream


CAMPUS_DEVICE_ID_PREFIX = "loomy-campus-"


def new_identity(mode: Optional[str] = None) -> dict[str, str]:
    """每个账号一套独立设备标识。

    per_account（默认）：devid = web-<16hex>，campus_device_id = loomy-campus-<uuid4>
    client：完全照客户端原样（devid=web），行为最保守
    """
    mode = str(mode or CFG.get("identity_mode") or "per_account").lower()
    if mode == "client":
        return {"devid": DEVICE_ID, "ua": CLIENT_UA, "modelid": MODEL_ID,
                "version": CLIENT_VERSION, "campus_device_id": ""}
    return {
        "devid": "web-%s" % uuid.uuid4().hex[:16],
        "ua": CLIENT_UA,
        "modelid": MODEL_ID,
        "version": CLIENT_VERSION,
        "campus_device_id": CAMPUS_DEVICE_ID_PREFIX + str(uuid.uuid4()),
    }


def ensure_identity(account: dict[str, Any], *, rebind: bool = False) -> dict[str, str]:
    """账号没有身份时补一套；rebind=True 强制换新。"""
    identity = account.get("identity")
    if rebind or not isinstance(identity, dict) or not identity.get("devid"):
        account["identity"] = new_identity()
        account["identity_at"] = now_ts()
        account["identity_rebinds"] = int(account.get("identity_rebinds") or 0) + (1 if rebind else 0)
    return account["identity"]


def traceparent() -> str:
    return "00-%s-%s-01" % (os.urandom(16).hex(), os.urandom(8).hex())


def upstream_headers(session: str, *, stream: bool = False, chat_id: str = "",
                     devid: str = "") -> dict[str, str]:
    headers = {
        "Authorization": "Bearer %s" % session,
        "token": session,
        "Accept": "text/event-stream" if stream else "application/json",
        "Content-Type": "application/json",
        "User-Agent": CLIENT_UA_GATEWAY,
        "loomy-version": str(CFG.get("loomy_version") or "0.9.39"),
        "traceparent": traceparent(),
    }
    if devid:
        headers["X-Loomy-Device-ID"] = devid
    if chat_id:
        headers["ChatId"] = chat_id
        headers["MsgId"] = "%s-%s" % (chat_id, uuid.uuid4().hex[:8])
        headers["TurnId"] = uuid.uuid4().hex
    return headers


def upstream_url(path: str) -> str:
    base = str(CFG.get("upstream_base") or UPSTREAM_DEFAULT).rstrip("/")
    return base + path


PROVIDER_ERROR_HINTS = ("503", "504", "暂时不可用", "no_provider", "gateway time-out", "gateway timeout",
                        "service unavailable", "overloaded", "busy", "限流", "rate limit", "排队")


def classify_failure(message: str) -> str:
    """把上游失败分成 session（要换号）/ provider（上游模型侧，换号没用）/ other。"""
    text = str(message or "").lower()
    if any(hint in text for hint in ("session", "token", "登录", "未授权", "过期")):
        return "session"
    if any(hint.lower() in text for hint in PROVIDER_ERROR_HINTS):
        return "provider"
    return "other"


def is_session_error(payload: Any, status_code: int = 200) -> bool:
    if status_code in (401, 403):
        return True
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("code") or "").strip()
    if code in SESSION_ERROR_CODES:
        return True
    text = " ".join(str(payload.get(k) or "") for k in ("desc", "message", "msg", "error"))
    if code and code != SUCCESS_CODE and any(h in text.lower() for h in SESSION_ERROR_HINTS):
        return True
    return False


def envelope_message(payload: Any, fallback: str = "") -> str:
    if isinstance(payload, dict):
        for key in ("desc", "message", "msg", "error_description"):
            if payload.get(key):
                return str(payload[key])
        if isinstance(payload.get("error"), (str, dict)):
            err = payload["error"]
            return err if isinstance(err, str) else str(err.get("message") or err)
        if payload.get("code"):
            return "code=%s" % payload["code"]
    return fallback or "上游返回异常"


RR_STATE: dict[str, int] = {"idx": 0}


def in_cooldown(account: dict[str, Any]) -> bool:
    return int(account.get("cooldown_until") or 0) > now_ts()


def cooldown_left(account: dict[str, Any]) -> int:
    return max(0, int(account.get("cooldown_until") or 0) - now_ts())


def set_cooldown(account: dict[str, Any], seconds: Optional[int] = None, reason: str = "") -> None:
    wait = max(5, int(seconds if seconds is not None else (CFG.get("cooldown_seconds") or 300)))
    account["cooldown_until"] = now_ts() + wait
    account["cooldown_reason"] = str(reason)[:160]
    account["cooldown_count"] = int(account.get("cooldown_count") or 0) + 1
    save_config(CFG)


def clear_cooldown(account: dict[str, Any]) -> None:
    if account.pop("cooldown_until", None) is not None:
        account.pop("cooldown_reason", None)


def pick_accounts(exclude: set[str]) -> list[dict[str, Any]]:
    """按策略返回候选账号顺序：已过滤启停/session/失效/冷却。"""
    pool = [
        a for a in CFG.get("accounts", [])
        if a.get("enabled", True) and a.get("session") and a.get("state") != "invalid" and a.get("id") not in exclude
    ]
    if not pool:
        pool = [
            a for a in CFG.get("accounts", [])
            if a.get("enabled", True) and a.get("session") and a.get("id") not in exclude
        ]
    if not pool:
        return []
    ready = [a for a in pool if not in_cooldown(a)]
    if not ready:
        # 全部在冷却：宁可硬试也不能彻底不可用
        ready = pool
    strategy = str(CFG.get("strategy") or "balance").lower()
    if strategy == "round_robin":
        start = RR_STATE.get("idx", 0) % len(ready)
        ordered = ready[start:] + ready[:start]
        RR_STATE["idx"] = (start + 1) % max(1, len(ready))
        return ordered
    if strategy == "lru":
        return sorted(ready, key=lambda a: int(a.get("last_used_at") or 0))
    # balance（默认）：积分多的优先，同分按最久未用
    def score(a: dict[str, Any]) -> tuple[float, int]:
        points = a.get("points")
        usable = points if isinstance(points, (int, float)) else 0
        return (-float(usable), int(a.get("last_used_at") or 0))

    return sorted(ready, key=score)


def mark_used(account: dict[str, Any]) -> None:
    account["last_used_at"] = now_ts()
    account["req_count"] = int(account.get("req_count") or 0) + 1


def account_by_id(account_id: str) -> Optional[dict[str, Any]]:
    for account in CFG.get("accounts", []):
        if account.get("id") == account_id:
            return account
    return None


def mark_account(account: dict[str, Any], ok: bool, error: str = "") -> None:
    if ok:
        account["state"] = "ok"
        account["fail_count"] = 0
        account["last_error"] = ""
        account["last_ok_at"] = now_ts()
        clear_cooldown(account)
    else:
        account["fail_count"] = int(account.get("fail_count") or 0) + 1
        account["last_error"] = error[:300]
        account["last_error_at"] = now_ts()
        if is_session_error({"code": "", "desc": error}) or account["fail_count"] >= 3:
            account["state"] = "invalid"
    save_config(CFG)


_bump_lock = asyncio.Lock()


async def bump_usage(ok: bool, tin: int = 0, tout: int = 0) -> None:
    async with _bump_lock:
        usage = CFG.setdefault("usage", {"requests": 0, "ok": 0, "fail": 0, "tokens_in": 0, "tokens_out": 0})
        usage["requests"] = int(usage.get("requests") or 0) + 1
        usage["ok" if ok else "fail"] = int(usage.get("ok" if ok else "fail") or 0) + 1
        usage["tokens_in"] = int(usage.get("tokens_in") or 0) + tin
        usage["tokens_out"] = int(usage.get("tokens_out") or 0) + tout
        save_config(CFG)


def normalize_model(model: str) -> str:
    name = str(model or "").strip()
    if name.startswith("imodel/"):
        name = name[len("imodel/"):]
    if not name:
        name = str(CFG.get("default_model") or "").replace("imodel/", "")
    return name


# ---------------------------------------------------------------- points / daily


def extract_points(payload: Any) -> Optional[int]:
    """尽力从余额响应里取出一个分值：多版本接口字段不一致，按语义匹配。"""
    found: list[int] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, key)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            low = key.lower()
            if any(t in low for t in ("balance", "point", "credit", "remain", "available", "total")):
                found.append(int(node))

    walk(payload)
    return max(found) if found else None


async def fetch_balance(account: dict[str, Any], client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, verify=False, trust_env=False)
    try:
        identity = ensure_identity(account)
        response = await client.get(upstream_url("/points/balance"),
                                    headers=upstream_headers(account["session"], devid=identity.get("devid", "")))
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": response.text[:400]}
        points = extract_points(payload)
        account["points"] = points if points is not None else account.get("points")
        account["points_at"] = now_ts()
        account["points_raw"] = payload if points is None else account.get("points_raw", {})
        if is_session_error(payload, response.status_code):
            account["state"] = "invalid"
            account["last_error"] = envelope_message(payload)
        elif response.status_code == 200 and (points is not None or str(payload.get("code") or SUCCESS_CODE) == SUCCESS_CODE):
            account["state"] = "ok"
        save_config(CFG)
        return {"ok": True, "points": account.get("points"), "raw": payload}
    except Exception as error:  # noqa: BLE001
        account["last_error"] = str(error)[:300]
        save_config(CFG)
        return {"ok": False, "error": str(error)}
    finally:
        if own:
            await client.aclose()


async def fetch_models(force: bool = False) -> list[dict[str, Any]]:
    if not force and MODEL_CACHE["data"] and time.time() - MODEL_CACHE["at"] < 600:
        return MODEL_CACHE["data"]
    accounts = pick_accounts(set())
    if not accounts:
        return MODEL_CACHE["data"]
    async with httpx.AsyncClient(timeout=20, verify=False, trust_env=False) as client:
        for account in accounts[:3]:
            try:
                response = await client.get(upstream_url("/models"), headers=upstream_headers(account["session"]))
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list) and data:
                    MODEL_CACHE["data"] = data
                    MODEL_CACHE["at"] = time.time()
                    update_catalog(data)
                    mark_account(account, True)
                    return data
                if is_session_error(payload, response.status_code):
                    mark_account(account, False, envelope_message(payload))
            except Exception:  # noqa: BLE001
                continue
    return MODEL_CACHE["data"]


def _model_limit(item: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
    ctx = limit.get("context") or item.get("context_length") or item.get("context_window")
    outp = limit.get("output") or item.get("max_output_tokens") or item.get("max_tokens")
    ctx = int(ctx) if isinstance(ctx, (int, float)) and ctx else None
    outp = int(outp) if isinstance(outp, (int, float)) and outp else None
    return ctx, outp


def update_catalog(raw_models: list[dict[str, Any]]) -> None:
    """把每次 /models 的结果并进目录：出现的标上线并刷新末见，消失的标下线但不删除。"""
    now = now_ts()
    catalog = CFG.setdefault("models_catalog", {})
    seen: set[str] = set()
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or item.get("model") or "").strip()
        if not raw_id:
            continue
        seen.add(raw_id)
        ctx, outp = _model_limit(item)
        entry = catalog.get(raw_id) or {"first_seen": now, "seen_count": 0}
        entry.update({
            "raw_id": raw_id,
            "id": "imodel/%s" % raw_id,
            "name": str(item.get("name") or raw_id),
            "context": ctx,
            "max_output": outp,
            "reasoning": bool(item.get("reasoning")),
            "tool_call": bool(item.get("tool_call")),
            "attachment": bool(item.get("attachment")),
            "protocol": str(item.get("protocol") or "openai_chat"),
            "is_default_model": bool(item.get("is_default_model")),
            "is_default_small_model": bool(item.get("is_default_small_model")),
            "last_seen": now,
            "online": True,
            "seen_count": int(entry.get("seen_count") or 0) + 1,
        })
        entry.setdefault("first_seen", now)
        catalog[raw_id] = entry
    for raw_id, entry in catalog.items():
        if raw_id not in seen:
            entry["online"] = False
    save_config(CFG)


def catalog_list() -> list[dict[str, Any]]:
    catalog = CFG.get("models_catalog") or {}
    rows = []
    for entry in catalog.values():
        row = dict(entry)
        name = str(row.get("name") or "")
        match = re.search(r"[xX]\s*(\d+(?:\.\d+)?)\s*[)）]?\s*$", name + " ")
        row["ratio"] = ("x" + match.group(1)) if match else ""
        rows.append(row)
    rows.sort(key=lambda r: (0 if r.get("online") else 1, -int(r.get("last_seen") or 0), str(r.get("raw_id"))))
    return rows


def model_details(raw_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """上游 /models 的原始字段：limit.context=上下文，limit.output=单次最大输出。"""
    out = []
    for item in raw_models:
        mid = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not mid:
            continue
        limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
        ctx = limit.get("context") or item.get("context_length") or item.get("context_window")
        outp = limit.get("output") or item.get("max_output_tokens") or item.get("max_tokens")
        out.append({
            "id": "imodel/%s" % mid,
            "raw_id": mid,
            "name": str(item.get("name") or mid),
            "context": int(ctx) if isinstance(ctx, (int, float)) and ctx else None,
            "max_output": int(outp) if isinstance(outp, (int, float)) and outp else None,
            "reasoning": bool(item.get("reasoning")),
            "tool_call": bool(item.get("tool_call")),
            "attachment": bool(item.get("attachment")),
            "protocol": str(item.get("protocol") or "openai"),
            "is_default_model": bool(item.get("is_default_model")),
            "is_default_small_model": bool(item.get("is_default_small_model")),
        })
    return out


def model_objects(raw_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in raw_models:
        mid = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not mid:
            continue
        out.append({
            "id": "imodel/%s" % mid,
            "object": "model",
            "created": int(item.get("created") or 0),
            "owned_by": str(item.get("owned_by") or "loomy"),
            "raw_id": mid,
        })
    return out


async def probe_ttft(model: str, account: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """只等第一个 SSE 包就断开：测"能不能用 + 首包多快"，不消耗整段生成。"""
    accounts = [account] if account else pick_accounts(set())
    accounts = [a for a in accounts if a]
    if not accounts:
        return {"ok": False, "error": "没有可用账号"}
    acc = accounts[0]
    mark_used(acc)
    identity = ensure_identity(acc)
    headers = upstream_headers(acc["session"], stream=True, chat_id="probe-%s" % uuid.uuid4().hex,
                               devid=identity.get("devid", ""))
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
               "stream": True, "max_tokens": 8, "enable_thinking": False}
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), verify=False, trust_env=False)
    started = time.time()
    try:
        request = client.build_request("POST", upstream_url("/chat/completions"), headers=headers, json=payload)
        response = await client.send(request, stream=True)
        if response.status_code >= 400 or "event-stream" not in response.headers.get("content-type", ""):
            raw = await response.aread()
            await response.aclose()
            try:
                body_json = json.loads(raw.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                body_json = {"raw": raw.decode("utf-8", "replace")[:200]}
            message = envelope_message(body_json, "HTTP %s" % response.status_code)
            return {"ok": False, "error": message, "kind": classify_failure(message),
                    "ms": int((time.time() - started) * 1000), "account": acc.get("note")}
        ttft = None
        head = ""
        try:
            async for line in response.aiter_lines():
                if ttft is None:
                    ttft = int((time.time() - started) * 1000)
                if line.startswith("data:") and "[DONE]" not in line:
                    try:
                        piece = json.loads(line[5:].strip())
                        delta = (piece.get("choices") or [{}])[0].get("delta") or {}
                        head = head or str(delta.get("content") or delta.get("reasoning_content") or "")
                    except Exception:  # noqa: BLE001
                        pass
                break
        finally:
            await response.aclose()
        return {"ok": True, "ttft_ms": ttft, "head": head[:40], "model": model,
                "account": acc.get("note"), "points": (CFG.get("accounts") and acc.get("points"))}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)[:160], "kind": classify_failure(str(error)),
                "ms": int((time.time() - started) * 1000)}
    finally:
        await client.aclose()


def run_daily_job(reason: str) -> dict[str, Any]:
    """复用 Loomy-Daily 的签到 + 新手任务脚本，一次跑完所有启用账号。"""
    script = DAILY_DIR / "loomy_daily.py"
    python = DAILY_DIR / ".venv" / "bin" / "python"
    if not script.exists():
        return {"ok": False, "error": "未找到 %s" % script}
    lines = []
    for account in CFG.get("accounts", []):
        if account.get("enabled", True) and account.get("session"):
            lines.append("%s|%s" % (account.get("note") or account["id"], account["session"]))
    if not lines:
        return {"ok": False, "error": "没有可用账号"}
    env = dict(os.environ)
    env["LOOMY_TOKENS"] = "&".join(lines)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [str(python) if python.exists() else sys.executable, str(script)],
            cwd=str(DAILY_DIR), env=env, capture_output=True, text=True, timeout=900,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}
    log_line("daily.log", {"reason": reason, "rc": proc.returncode, "tail": output[-1500:]})
    return {"ok": proc.returncode == 0, "rc": proc.returncode, "output": output[-4000:]}


async def run_daily_all() -> dict[str, Any]:
    """对所有启用账号跑 每日积分 + 新手任务，返回汇总。"""
    accounts = [a for a in CFG.get("accounts", []) if a.get("enabled", True) and a.get("session")]
    if not accounts:
        return {"ok": False, "error": "没有可用账号"}
    results = []
    for account in accounts:
        try:
            result = await run_tasks(account, True)
        except Exception as error:  # noqa: BLE001
            result = {"account": account.get("note"), "error": str(error)}
        results.append(result)
        await asyncio.sleep(0.6)
    claimed = sum(len([c for c in r.get("claims", []) if c.get("ok")]) for r in results)
    CFG["tasks_last_run"] = {"at": datetime.now(TZ).strftime("%F %T"), "accounts": len(results), "claimed": claimed}
    save_config(CFG)
    log_line("tasks.log", {"reason": "daily", "accounts": len(results), "claimed": claimed})
    return {"ok": True, "accounts": len(results), "claimed": claimed,
            "output": "账号 %d 个，领取 %d 项" % (len(results), claimed)}


async def balance_sweeper() -> None:
    while True:
        try:
            accounts = [a for a in CFG.get("accounts", []) if a.get("session")]
            if accounts:
                async with httpx.AsyncClient(timeout=25, verify=False, trust_env=False) as client:
                    for account in accounts:
                        if not account.get("enabled", True):
                            continue
                        await fetch_balance(account, client)
                        await asyncio.sleep(0.4)
            await fetch_models(force=True)
        except Exception as error:  # noqa: BLE001
            print("[balance] %s" % error, flush=True)
        interval = max(5, int(CFG.get("balance_interval_min") or 30))
        await asyncio.sleep(interval * 60)


async def daily_scheduler() -> None:
    while True:
        try:
            if CFG.get("daily_enabled", True):
                target = str(CFG.get("daily_time") or "08:45")
                now = datetime.now(TZ)
                today = now.strftime("%Y-%m-%d")
                state = CFG.setdefault("daily_state", {})
                if now.strftime("%H:%M") == target and state.get("date") != today:
                    state["date"] = today
                    save_config(CFG)
                    result = await run_daily_all()
                    state["last"] = {"at": now.strftime("%F %T"), "ok": result.get("ok"), "tail": (result.get("output") or result.get("error") or "")[-600:]}
                    save_config(CFG)
                    await asyncio.sleep(60)
        except Exception as error:  # noqa: BLE001
            print("[daily] %s" % error, flush=True)
        await asyncio.sleep(20)


# ---------------------------------------------------------------- SMS login


def _encode_component(raw: bytes) -> str:
    safe = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
    return "".join(chr(b) if b in safe else "%%%02X" % b for b in raw)


def _escaped_path(path: str) -> str:
    clean = path if path.startswith("/") else "/" + path
    if len(clean) > 1 and clean.endswith("/"):
        clean = clean[:-1]
    return "/".join(_encode_component(seg.encode()) for seg in clean.split("/"))


def _signed_headers(method: str, path: str, body: str) -> dict[str, str]:
    content_md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode() if body else ""
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    nonce = str(uuid.uuid4())
    string_to_sign = "\n".join([method.upper(), _escaped_path(path), "", content_md5, "application/json", date, nonce, "", ""])
    key_id, key_secret = access_key_pair()
    signature = base64.b64encode(hmac.new(key_secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
    headers = {"Authorization": "account %s:%s" % (key_id, signature), "Date": date, "Nonce": nonce, "Content-Type": "application/json"}
    if content_md5:
        headers["Content-MD5"] = content_md5
    return headers


def _base_body(identity: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    ident = identity or new_identity()
    return {
        "appid": APP_ID,
        "modelid": str(ident.get("modelid") or MODEL_ID),
        "version": str(ident.get("version") or CLIENT_VERSION),
        "devid": str(ident.get("devid") or DEVICE_ID),
        "ua": str(ident.get("ua") or CLIENT_UA),
        "traceid": os.urandom(16).hex(),
    }


async def sms_send(phone: str) -> str:
    phone = normalize_phone(phone)
    identity = new_identity()
    body = json.dumps({"base": _base_body(identity), "param": {"ccode": "86", "phone": phone, "expire": 300}},
                      ensure_ascii=False, separators=(",", ":"))
    async with httpx.AsyncClient(timeout=20, verify=False, trust_env=False) as client:
        response = await client.post(ACCOUNT_BASE + SEND_CODE_PATH, headers=_signed_headers("POST", SEND_CODE_PATH, body), content=body.encode())
    payload = response.json()
    if str(payload.get("code") or "") != SUCCESS_CODE:
        raise HTTPException(400, "发码失败: %s" % envelope_message(payload))
    data = payload.get("data") or {}
    msgid = str(data.get("msgid") or data.get("msgId") or ((data.get("data") or {}).get("msgid") if isinstance(data.get("data"), dict) else "") or "")
    if not msgid:
        raise HTTPException(502, "短信已发出但未返回 msgid")
    SMS_STATE[phone] = {"msgid": msgid, "at": now_ts(), "identity": identity}
    return msgid


async def sms_login(phone: str, code: str, note: str) -> dict[str, Any]:
    phone = normalize_phone(phone)
    state = SMS_STATE.get(phone)
    if not state:
        raise HTTPException(400, "请先获取验证码")
    if now_ts() - int(state["at"]) > 300:
        raise HTTPException(400, "验证码已过期，请重新发送")
    identity = state.get("identity") or new_identity()
    body = json.dumps({
        "base": _base_body(identity),
        "param": {"ccode": "86", "phone": phone, "mcode": code, "msgid": state["msgid"], "expire": 14 * 24 * 3600},
    }, ensure_ascii=False, separators=(",", ":"))
    async with httpx.AsyncClient(timeout=20, verify=False, trust_env=False) as client:
        response = await client.post(ACCOUNT_BASE + LOGIN_PATH, headers=_signed_headers("POST", LOGIN_PATH, body), content=body.encode())
    payload = response.json()
    if str(payload.get("code") or "") != SUCCESS_CODE:
        raise HTTPException(400, "登录失败: %s" % envelope_message(payload))
    data = payload.get("data") or {}
    session = str(data.get("session") or "").strip()
    if not session:
        raise HTTPException(502, "登录成功但缺少 session")
    SMS_STATE.pop(phone, None)
    account = {
        "id": uuid.uuid4().hex[:10],
        "note": note or phone,
        "phone": phone,
        "session": session,
        "userid": str(data.get("userid") or ""),
        "identity": identity,
        "identity_at": now_ts(),
        "enabled": True,
        "state": "ok",
        "created_at": now_ts(),
        "points": None,
        "points_at": 0,
        "fail_count": 0,
        "last_error": "",
    }
    CFG.setdefault("accounts", []).append(account)
    save_config(CFG)
    await fetch_balance(account)
    return account


SMS_STATE: dict[str, dict[str, Any]] = {}


def normalize_phone(raw: str) -> str:
    text = re.sub(r"[\s\-]", "", str(raw or "").strip())
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", text):
        raise HTTPException(400, "手机号格式不对，需要 11 位大陆号码")
    return text


# ---------------------------------------------------------------- 任务中心（每日积分 + 新手任务）

TASK_DEFS = [
    ("初识 Loomy", "first_message", "发送你的第一条消息", 500),
    ("初识 Loomy", "pick_skill", "试试选择一个技能", 1000),
    ("初识 Loomy", "generate_ppt", "生成第一份 PPT", 1500),
    ("打造你的专属 Loomy", "set_schedule", "设置定时任务", 1000),
    ("打造你的专属 Loomy", "install_skill", "在技能广场安装一个技能", 1500),
    ("打造你的专属 Loomy", "configure_remote", "配置远程控制", 1000),
    ("遇见你的 AI 搭子", "create_soul", "创建你的第一个搭子", 1500),
    ("遇见你的 AI 搭子", "share_soul", "把搭子分享给朋友", 2000),
]
TASK_TOTAL = sum(item[3] for item in TASK_DEFS)


def _biz(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", "响应无法解析"
    code = str(payload.get("code") or "")
    message = envelope_message(payload) if code != SUCCESS_CODE else ""
    return code, message


async def tasks_state(account: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    """读任务清单 + 汇总已完成/待领取。"""
    identity = ensure_identity(account)
    response = await client.get(upstream_url("/onboarding/tasks"),
                                headers=upstream_headers(account["session"], devid=identity.get("devid", "")))
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = {}
    code, message = _biz(payload)
    if code != SUCCESS_CODE:
        if is_session_error(payload, response.status_code):
            mark_account(account, False, message or "session 失效")
        return {"ok": False, "error": message or ("code=%s" % code or "查询失败")}
    raw = (payload.get("data") or {}).get("tasks") or {}
    rows = []
    earned = 0
    unclaimed = []
    for group, key, title, points in TASK_DEFS:
        done = raw.get(key) is True
        if done:
            earned += points
        else:
            unclaimed.append(key)
        rows.append({"key": key, "group": group, "title": title, "points": points, "done": done})
    return {"ok": True, "rows": rows, "earned": earned, "total": TASK_TOTAL, "unclaimed": unclaimed}


async def daily_refresh(account: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    identity = ensure_identity(account)
    response = await client.post(upstream_url("/points/first-login"),
                                 headers=upstream_headers(account["session"], devid=identity.get("devid", "")),
                                 json={"campus_device_id": identity.get("campus_device_id", "")} if identity.get("campus_device_id") else {})
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = {}
    code, message = _biz(payload)
    if code != SUCCESS_CODE:
        return {"ok": False, "error": message or ("code=%s" % code)}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    balance = data.get("dailyBalance")
    if balance is None and isinstance(data.get("data"), dict):
        balance = data["data"].get("dailyBalance")
    return {"ok": True, "balance": balance}


async def claim_one(account: dict[str, Any], client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    identity = ensure_identity(account)
    response = await client.post(upstream_url("/onboarding/tasks/complete"),
                                 headers=upstream_headers(account["session"], devid=identity.get("devid", "")),
                                 json={"key": key})
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = {}
    code, message = _biz(payload)
    if code != SUCCESS_CODE:
        return {"ok": False, "error": message or ("code=%s" % code)}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"ok": True, "already": bool(data.get("alreadyCompleted") is True)}


async def run_tasks(account: dict[str, Any], do_daily: bool = True) -> dict[str, Any]:
    """完整跑一轮：每日积分 + 逐个领取未完成任务 + 回读确认。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), verify=False, trust_env=False) as client:
        result: dict[str, Any] = {"account": account.get("note") or account.get("id"), "daily": None, "claims": [], "after": None}
        if do_daily:
            result["daily"] = await daily_refresh(account, client)
        before = await tasks_state(account, client)
        if not before.get("ok"):
            result["error"] = before.get("error")
            return result
        for key in before.get("unclaimed", []):
            claim = await claim_one(account, client, key)
            result["claims"].append({"key": key, **claim})
            await asyncio.sleep(0.6)
        after = await tasks_state(account, client)
        result["after"] = {"ok": after.get("ok"), "earned": after.get("earned"),
                           "total": after.get("total"), "unclaimed": after.get("unclaimed")}
        mark_account(account, bool(after.get("ok")), "" if after.get("ok") else "任务回读失败")
        if after.get("ok"):
            await fetch_balance(account)
        return result


# ---------------------------------------------------------------- 接码平台入池


POOL_JOB: dict[str, Any] = {
    "running": False, "stop": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
    "started_at": 0, "finished_at": 0, "current": "", "logs": [],
}


def pool_log(message: str) -> None:
    POOL_JOB["logs"].append("%s %s" % (datetime.now(TZ).strftime("%H:%M:%S"), message))
    del POOL_JOB["logs"][:-400]
    log_line("pool.log", {"msg": message})


def _persist_pool_token(token: str) -> None:
    current = dict(CFG.get("sms_pool") or {})
    current["token"] = token
    CFG["sms_pool"] = current
    save_config(CFG)


def make_provider() -> "sms_pool.SmsProvider":
    return sms_pool.SmsProvider(pool_cfg(), on_token=_persist_pool_token)


def pool_cfg() -> dict[str, Any]:
    merged = dict(sms_pool.DEFAULT_CFG)
    merged.update({k: v for k, v in (CFG.get("sms_pool") or {}).items() if v is not None})
    return merged


async def _pool_one(index: int, provider: "sms_pool.SmsProvider", client: httpx.AsyncClient) -> None:
    cfg = pool_cfg()
    blocked = [p.strip() for p in str(cfg.get("block_prefixes") or "").split(",") if p.strip()]
    attempts = max(1, min(int(cfg.get("attempts") or 1), 50))
    last_error = ""
    finished = False
    try:
        for attempt in range(1, attempts + 1):
            if POOL_JOB.get("stop"):
                return
            # 屏蔽号段 / 优先号段：取到不合适就释放换号（不计入尝试次数）
            prefer = [p.strip() for p in str(cfg.get("prefer_prefixes") or "").split(",") if p.strip()]
            phone = ""
            picked = False
            for _ in range(max(1, int(cfg.get("retry_get_phone") or 1))):
                phone = await provider.get_phone(client)
                if any(phone.startswith(prefix) for prefix in blocked):
                    pool_log("[%d] 屏蔽号段 %s（block_prefixes），释放换号" % (index, phone))
                    await provider.release(client, phone)
                    continue
                if prefer and not any(phone.startswith(prefix) for prefix in prefer):
                    pool_log("[%d] 非优先号段 %s（prefer=%s），释放换号" % (index, phone, ",".join(prefer)))
                    await provider.release(client, phone)
                    continue
                picked = True
                break
            else:
                if prefer:
                    pool_log("[%d] 没取到优先号段 %s，改用当前号码 %s" % (index, ",".join(prefer), phone))
                    picked = True
            if not picked:
                # 号源全是屏蔽号段：换回不屏蔽策略，避免整批卡死
                pool_log("[%d] 连续命中屏蔽号段 %s，本次不再过滤" % (index, ",".join(blocked)))
                try:
                    result = await _pool_attempt_with_phone(index, provider, client, phone)
                    if result == "ok":
                        POOL_JOB["ok"] += 1
                        finished = True
                        return
                except Exception as error:  # noqa: BLE001
                    last_error = str(error)
                    pool_log("[%d] 失败：%s" % (index, last_error))
                raise sms_pool.PoolError(last_error or "取号失败")
            try:
                result = await _pool_attempt_with_phone(index, provider, client, phone)
                if result in ("ok", "skip"):
                    if result == "ok":
                        POOL_JOB["ok"] += 1
                    return
            except Exception as error:  # noqa: BLE001
                last_error = str(error)
                pool_log("[%d] 第 %d/%d 次失败：%s" % (index, attempt, attempts, last_error))
                if attempt < attempts:
                    pool_log("[%d] 换号重试" % index)
        POOL_JOB["fail"] += 1
        pool_log("[%d] ❌ 放弃：%s" % (index, last_error or "未知错误"))
        finished = True
    except Exception as error:  # noqa: BLE001
        if not finished:
            POOL_JOB["fail"] += 1
            pool_log("[%d] ❌ 异常结束：%s" % (index, str(error)[:160]))
    finally:
        POOL_JOB["done"] += 1


async def _pool_attempt_with_phone(index: int, provider: "sms_pool.SmsProvider", client: httpx.AsyncClient, phone: str) -> str:
    """用已取到的号码走完 发码→取码→登录，成功返回 ok。"""
    cfg = pool_cfg()
    interval = max(1, int(cfg.get("poll_interval") or 3))
    timeout = max(30, int(cfg.get("code_timeout") or 120))
    POOL_JOB["current"] = phone
    try:
        if any(a.get("phone") == phone for a in CFG.get("accounts", [])):
            pool_log("[%d] 跳过 %s：已在号池" % (index, phone))
            return "skip"
        pool_log("[%d] 取号成功 %s，请求 Loomy 验证码" % (index, phone))
        try:
            msgid = await sms_send(phone)
        except HTTPException as error:
            raise sms_pool.PoolError("Loomy 发码失败：%s" % (getattr(error, "detail", error)))
        pool_log("[%d] %s 验证码已发送（msgid=%s…）" % (index, phone, str(msgid)[:6]))

        code = ""
        wait_started = now_ts()
        deadline = wait_started + timeout
        while now_ts() < deadline and not POOL_JOB.get("stop"):
            try:
                code = await provider.get_code(client, phone) or ""
            except Exception as error:  # noqa: BLE001
                pool_log("[%d] 取码异常：%s" % (index, str(error)[:120]))
                code = ""
            if code:
                break
            await asyncio.sleep(interval)
        if not code:
            raise sms_pool.PoolError("等待验证码超时（%ss）" % (now_ts() - wait_started))
        pool_log("[%d] %s 收到验证码 %s，提交登录" % (index, phone, code))

        note = "%s%s" % (cfg.get("note_prefix") or "接码", datetime.now(TZ).strftime("%m%d-%H%M"))
        account = await sms_login(phone, code, note)
        pool_log("[%d] ✅ %s 入池成功（积分 %s）" % (index, phone, account.get("points")))
        return "ok"
    except Exception:  # noqa: BLE001
        if cfg.get("blacklist_on_fail"):
            await provider.blacklist(client, phone)
        else:
            await provider.release(client, phone)
        raise
    finally:
        if POOL_JOB["current"] == phone:
            POOL_JOB["current"] = ""


async def pool_worker(count: int) -> None:
    try:
        await _pool_worker_inner(count)
    except Exception as error:  # noqa: BLE001
        POOL_JOB["running"] = False
        pool_log("任务异常中止：%s" % str(error)[:200])


async def _pool_worker_inner(count: int) -> None:
    cfg = pool_cfg()
    total = max(1, min(int(count or 1), 100))
    POOL_JOB.update(running=True, stop=False, total=total, done=0, ok=0, fail=0,
                    started_at=now_ts(), finished_at=0, current="", logs=[])
    pool_log("启动：目标 %d 个账号，并发 %s，项目 %s" % (total, cfg.get("concurrency") or 1, cfg.get("project_id") or "-"))
    provider = make_provider()
    semaphore = asyncio.Semaphore(max(1, min(int(cfg.get("concurrency") or 1), 20)))

    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), verify=False, trust_env=False) as client:
        queue = list(range(1, total + 1))

        async def runner(i: int) -> None:
            async with semaphore:
                if POOL_JOB.get("stop"):
                    return
                await _pool_one(i, provider, client)

        await asyncio.gather(*(runner(i) for i in queue), return_exceptions=True)
    POOL_JOB["running"] = False
    POOL_JOB["finished_at"] = now_ts()
    pool_log("结束：成功 %d / 失败 %d / 共 %d" % (POOL_JOB["ok"], POOL_JOB["fail"], POOL_JOB["done"]))


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(_: FastAPI):
    LOG_DIR.mkdir(exist_ok=True)
    missing = [a for a in CFG.get("accounts", []) if a.get("session") and not (a.get("identity") or {}).get("devid")]
    if missing:
        for account in missing:
            ensure_identity(account)
        save_config(CFG)
        print("[loomy2api] 为 %d 个存量账号生成了独立设备标识" % len(missing), flush=True)
    if not CFG.get("panel_password"):
        CFG["panel_password"] = "loomy-" + uuid.uuid4().hex[:10]
        save_config(CFG)
        print("[loomy2api] 已生成面板密码: %s" % CFG["panel_password"], flush=True)
    asyncio.create_task(balance_sweeper())
    asyncio.create_task(daily_scheduler())
    print("[loomy2api] 启动完成，账号 %d 个" % len(CFG.get("accounts", [])), flush=True)
    yield


app = FastAPI(title="Loomy2API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def check_key(authorization: str = "", token: str = "", x_api_key: str = "") -> None:
    keys = [str(k).strip() for k in (CFG.get("api_keys") or []) if str(k).strip()]
    if not keys:
        return
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    supplied = supplied or token or x_api_key
    if supplied not in keys:
        raise HTTPException(401, "无效的 API key")


PANEL_COOKIE = "loomy2api_admin"


def panel_token() -> str:
    pwd = str(CFG.get("panel_password") or "")
    return hashlib.sha256(("loomy2api::" + pwd).encode()).hexdigest()[:32] if pwd else ""


def panel_ok(request: Request) -> bool:
    token = panel_token()
    if not token:
        return True
    if request.cookies.get(PANEL_COOKIE) == token:
        return True
    supplied = request.query_params.get("k") or request.headers.get("x-admin-key") or ""
    return bool(supplied) and supplied == str(CFG.get("panel_password") or "")


def panel_auth(request: Request) -> None:
    """面板鉴权：cookie（登录后种下）或 ?k=密码。指向 401 时由前端跳登录页。"""
    if not panel_ok(request):
        raise HTTPException(401, "需要登录")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    accounts = CFG.get("accounts", [])
    return {
        "ok": True,
        "accounts": len(accounts),
        "usable": len([a for a in accounts if a.get("enabled", True) and a.get("state") != "invalid"]),
        "ts": now_ts(),
    }


@app.get("/v1/models")
async def v1_models(request: Request, authorization: str = Header(""), token: str = Header(""), x_api_key: str = Header("")) -> dict[str, Any]:
    check_key(authorization, token, x_api_key)
    raw = await fetch_models()
    if request.query_params.get("detail") in ("1", "true", "yes"):
        return {"object": "list", "data": model_details(raw), "fetched_at": int(MODEL_CACHE["at"])}
    return {"object": "list", "data": model_objects(raw)}


@app.post("/v1/chat/completions")
async def v1_chat(
    request: Request,
    authorization: str = Header(""),
    token: str = Header(""),
    x_api_key: str = Header(""),
):
    check_key(authorization, token, x_api_key)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "请求体需要是 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体需要是 JSON 对象")

    body["model"] = normalize_model(body.get("model") or "")
    if not body["model"]:
        raise HTTPException(400, "缺少 model，可先请求 /v1/models")
    stream = bool(body.get("stream"))
    original_model = body["model"]

    tried: set[str] = set()
    last_error = "没有可用账号"
    fallback = str(CFG.get("fallback_model") or "").replace("imodel/", "").strip()
    fallback_used = False
    max_attempts = max(1, int(CFG.get("max_retries") or 2) + 1)
    for _ in range(max_attempts):
        candidates = pick_accounts(tried)
        if not candidates:
            break
        account = candidates[0]
        tried.add(account["id"])
        mark_used(account)
        chat_id = "chat-%s" % uuid.uuid4().hex
        identity = ensure_identity(account)
        headers = upstream_headers(account["session"], stream=stream, chat_id=chat_id,
                                   devid=identity.get("devid", ""))
        started = time.time()
        if stream:
            opened = await _open_stream(body, headers, account, started)
            if not isinstance(opened, str):
                response, client = opened
                return StreamingResponse(
                    _stream_body(response, client, account, body, started),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            last_error = opened
        else:
            result = await _json_chat(body, headers, account, started)
            if isinstance(result, Response):
                return result
            last_error = result

        kind = classify_failure(last_error)
        if kind == "session":
            # 账号登录态问题：换下一个号
            continue
        if kind == "provider" and fallback and not fallback_used and fallback != body.get("model"):
            # 上游模型侧不可用：换号没用，直接切兜底模型重试一次
            body["model"] = fallback
            fallback_used = True
            tried.clear()
            log_line("usage.log", {"event": "fallback", "from": original_model, "to": fallback, "reason": last_error[:120]})
            continue
        # 其他错误：立刻失败，不再重试（避免放大后台 503/504 记录）
        break
    await bump_usage(False)
    raise HTTPException(502, "上游不可用：%s" % last_error)


async def _json_chat(body: dict[str, Any], headers: dict[str, str], account: dict[str, Any], started: float):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), verify=False, trust_env=False) as client:
            response = await client.post(upstream_url("/chat/completions"), headers=headers, json=body)
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {"raw": response.text[:600]}
    except Exception as error:  # noqa: BLE001
        mark_account(account, False, str(error))
        log_line("usage.log", {"account": account["id"], "model": body.get("model"), "error": str(error)[:200]})
        return str(error)

    if isinstance(payload, dict) and "choices" in payload:
        usage = payload.get("usage") or {}
        mark_account(account, True)
        await bump_usage(True, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        log_line("usage.log", {"account": account["id"], "model": body.get("model"), "ms": int((time.time() - started) * 1000), "ok": True})
        return JSONResponse(payload)

    message = envelope_message(payload, "HTTP %s" % response.status_code)
    # provider 侧错误（503/504）不代表账号坏了；只有会话/额度类问题才冷却该号
    kind = classify_failure(message)
    if kind == "session" or response.status_code in (401, 403, 402, 429):
        mark_account(account, False, message)
        set_cooldown(account, reason="%s %s" % (response.status_code, message))
    log_line("usage.log", {"account": account["id"], "model": body.get("model"), "ok": False,
                           "kind": classify_failure(message), "upstream": payload})
    return message


async def _open_stream(body: dict[str, Any], headers: dict[str, str], account: dict[str, Any], started: float):
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20), verify=False, trust_env=False)
    try:
        request = client.build_request("POST", upstream_url("/chat/completions"), headers=headers, json=body)
        response = await client.send(request, stream=True)
    except Exception as error:  # noqa: BLE001
        await client.aclose()
        mark_account(account, False, str(error))
        return str(error)

    content_type = response.headers.get("content-type", "")
    if response.status_code >= 400 or "event-stream" not in content_type:
        raw = await response.aread()
        await response.aclose()
        await client.aclose()
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            payload = {"raw": raw.decode("utf-8", "replace")[:400]}
        message = envelope_message(payload, "HTTP %s" % response.status_code)
        kind = classify_failure(message)
        if kind == "session" or response.status_code in (401, 403, 402, 429):
            mark_account(account, False, message)
            set_cooldown(account, reason="%s %s" % (response.status_code, message))
        log_line("usage.log", {"account": account["id"], "model": body.get("model"), "ok": False,
                               "stream": True, "kind": kind, "upstream": payload})
        return message
    return response, client


async def _stream_body(response, client, account: dict[str, Any], body: dict[str, Any], started: float):
    tin = tout = chunks = 0
    ttft_ms = None
    try:
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("data:") and "[DONE]" not in line:
                try:
                    piece = json.loads(line[5:].strip())
                    usage = piece.get("usage") or {}
                    tin = int(usage.get("prompt_tokens") or tin or 0)
                    tout = int(usage.get("completion_tokens") or tout or 0)
                except Exception:  # noqa: BLE001
                    pass
                chunks += 1
            if ttft_ms is None:
                ttft_ms = int((time.time() - started) * 1000)
            yield (line + "\n\n").encode()
    finally:
        await response.aclose()
        await client.aclose()
        if chunks:
            mark_account(account, True)
        await bump_usage(bool(chunks), tin, tout)
        log_line("usage.log", {"account": account["id"], "model": body.get("model"), "ok": bool(chunks), "stream": True,
                               "chunks": chunks, "ttft_ms": ttft_ms, "ms": int((time.time() - started) * 1000),
                               "tokens_in": tin, "tokens_out": tout})


# ---------------------------------------------------------------- admin panel


LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Loomy2API · 登录</title>
<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:radial-gradient(900px 500px at 30% 0%,#14243a,#0b0f14);color:#e6edf6;
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
form{background:#121820;border:1px solid #233040;border-radius:14px;padding:26px 24px;width:320px}
h1{font-size:17px;margin:0 0 4px}p{color:#8fa3b8;font-size:12px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;background:#0d141c;border:1px solid #233040;color:#e6edf6;
border-radius:8px;padding:10px;font-size:14px;margin-bottom:12px}
button{width:100%;padding:10px;border-radius:8px;border:1px solid #2b7fd4;cursor:pointer;
background:linear-gradient(180deg,#2b7fd4,#1f66b0);color:#fff;font-size:14px}
.err{color:#ff8080;font-size:12px;margin-bottom:10px}</style></head><body>
<form method="post" action="__ACTION__">
  <h1>🌙 Loomy2API</h1><p>输入控制台密码</p>__ERR__
  <input type="password" name="password" placeholder="面板密码" autofocus />
  <button type="submit">进入控制台</button>
</form></body></html>"""


def ext_prefix(request: Request) -> str:
    """nginx 用 X-Forwarded-Prefix 告知外网路径前缀（如 /loomy），否则为空。"""
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def render_login(request: Request, error: str = "") -> HTMLResponse:
    action = ext_prefix(request) + "/login"
    page = LOGIN_PAGE.replace("__ACTION__", action).replace(
        "__ERR__", '<div class="err">%s</div>' % error if error else ""
    )
    return HTMLResponse(page, headers=NO_CACHE)


NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    supplied = request.query_params.get("k") or ""
    if supplied and panel_token() and supplied == str(CFG.get("panel_password") or ""):
        response = RedirectResponse(url=ext_prefix(request) + "/", status_code=303)
        response.set_cookie(PANEL_COOKIE, panel_token(), max_age=30 * 24 * 3600, httponly=True, samesite="lax", path="/")
        return response
    if not panel_ok(request):
        return render_login(request)
    page = HERE / "panel.html"
    if page.exists():
        return HTMLResponse(page.read_text("utf-8"), headers=NO_CACHE)
    return HTMLResponse("<h1>panel.html 缺失</h1>", status_code=500)


@app.post("/login")
async def login_form(request: Request):
    form = await request.form()
    supplied = str(form.get("password") or "")
    if not panel_token() or supplied == str(CFG.get("panel_password") or ""):
        response = RedirectResponse(url=ext_prefix(request) + "/", status_code=303)
        response.set_cookie(PANEL_COOKIE, panel_token(), max_age=30 * 24 * 3600, httponly=True, samesite="lax", path="/")
        return response
    return render_login(request, "密码不对")


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    return await index(request)


@app.get("/admin/api/state")
async def admin_state(request: Request) -> dict[str, Any]:
    panel_auth(request)
    accounts = []
    for account in CFG.get("accounts", []):
        accounts.append({
            "id": account.get("id"),
            "note": account.get("note"),
            "phone": account.get("phone", ""),
            "session": mask(account.get("session", "")),
            "enabled": account.get("enabled", True),
            "state": account.get("state", "ok"),
            "points": account.get("points"),
            "points_at": account.get("points_at", 0),
            "last_error": account.get("last_error", ""),
            "last_ok_at": account.get("last_ok_at", 0),
            "devid": str((account.get("identity") or {}).get("devid") or ""),
            "identity_at": account.get("identity_at", 0),
            "cooldown_left": cooldown_left(account),
            "cooldown_reason": account.get("cooldown_reason", "") if in_cooldown(account) else "",
            "req_count": int(account.get("req_count") or 0),
        })
    models = [m["id"] for m in model_objects(MODEL_CACHE["data"])]
    return {
        "accounts": accounts,
        "models": models,
        "usage": CFG.get("usage", {}),
        "api_keys": CFG.get("api_keys", []),
        "identity_mode": CFG.get("identity_mode", "per_account"),
        "strategy": CFG.get("strategy", "balance"),
        "cooldown_seconds": CFG.get("cooldown_seconds", 300),
        "max_retries": CFG.get("max_retries", 2),
        "daily_time": CFG.get("daily_time"),
        "daily_enabled": CFG.get("daily_enabled", True),
        "daily_state": CFG.get("daily_state", {}),
        "balance_interval_min": CFG.get("balance_interval_min", 30),
        "default_model": CFG.get("default_model", ""),
        "upstream_base": CFG.get("upstream_base"),
        "server_time": datetime.now(TZ).strftime("%F %T"),
    }


@app.post("/admin/api/accounts")
async def admin_add_account(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    session = str(payload.get("session") or "").strip()
    if not session:
        raise HTTPException(400, "缺少 session")
    account = {
        "id": uuid.uuid4().hex[:10],
        "note": str(payload.get("note") or "手动添加"),
        "session": session,
        "identity": new_identity(),
        "identity_at": now_ts(),
        "enabled": True,
        "state": "ok",
        "created_at": now_ts(),
        "points": None,
        "points_at": 0,
        "fail_count": 0,
        "last_error": "",
    }
    CFG.setdefault("accounts", []).append(account)
    save_config(CFG)
    await fetch_balance(account)
    return {"ok": True, "id": account["id"]}


@app.patch("/admin/api/accounts/{account_id}")
async def admin_patch_account(account_id: str, request: Request) -> dict[str, Any]:
    panel_auth(request)
    account = account_by_id(account_id) or _missing()
    payload = await request.json()
    if "enabled" in payload:
        account["enabled"] = bool(payload["enabled"])
    if "note" in payload:
        account["note"] = str(payload["note"])
    if "session" in payload and str(payload["session"]).strip():
        account["session"] = str(payload["session"]).strip()
        account["state"] = "ok"
        account["fail_count"] = 0
    if payload.get("reset"):
        account["state"] = "ok"
        account["fail_count"] = 0
        account["last_error"] = ""
    save_config(CFG)
    return {"ok": True}


@app.delete("/admin/api/accounts/{account_id}")
async def admin_del_account(account_id: str, request: Request) -> dict[str, Any]:
    panel_auth(request)
    before = len(CFG.get("accounts", []))
    CFG["accounts"] = [a for a in CFG.get("accounts", []) if a.get("id") != account_id]
    if len(CFG["accounts"]) == before:
        raise HTTPException(404, "账号不存在")
    save_config(CFG)
    return {"ok": True}


def _missing():
    raise HTTPException(404, "账号不存在")


@app.post("/admin/api/accounts/import")
async def admin_import_accounts(request: Request) -> dict[str, Any]:
    """批量导入 session。

    请求体：
      {"text": "备注|session\nsession2\n...", "validate": true, "note_prefix": ""}
    也接受 {"items": [{"session": "...", "note": "..."}]}
    兼容 `备注|session`、裸 session、`&` 分隔（Loomy-Daily 风格）。
    """
    panel_auth(request)
    payload = await request.json()
    validate = bool(payload.get("validate", True))
    prefix = str(payload.get("note_prefix") or "").strip()
    raw_items: list[tuple[str, str]] = []

    if isinstance(payload.get("items"), list):
        for item in payload["items"]:
            if isinstance(item, dict) and str(item.get("session") or "").strip():
                raw_items.append((str(item.get("note") or "").strip(), str(item["session"]).strip()))
    else:
        text = str(payload.get("text") or "")
        for chunk in text.replace("&", "\n").splitlines():
            line = chunk.strip().strip(",")
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                note, _, session = line.partition("|")
                raw_items.append((note.strip(), session.strip()))
            else:
                raw_items.append(("", line))

    if not raw_items:
        raise HTTPException(400, "没有可导入的内容")

    existing_sessions = {str(a.get("session") or "") for a in CFG.get("accounts", [])}
    existing_phones = {str(a.get("phone") or "") for a in CFG.get("accounts", []) if a.get("phone")}
    imported, duplicated, invalid = [], [], []
    claims_by_phone: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30, verify=False, trust_env=False) as client:
        for note, session in raw_items:
            if session in existing_sessions:
                duplicated.append({"note": note, "session": session[:4] + "***", "reason": "已存在"})
                continue
            points = None
            state = "ok"
            error = ""
            phone = ""
            if validate:
                try:
                    response = await client.get(upstream_url("/points/balance"),
                                                headers=upstream_headers(session, devid=new_identity()["devid"]))
                    body_json = response.json()
                    if is_session_error(body_json, response.status_code):
                        state = "invalid"
                        error = envelope_message(body_json, "session 无效")
                    else:
                        points = extract_points(body_json)
                except Exception as exc:  # noqa: BLE001
                    state = "invalid"
                    error = str(exc)[:120]
            if state == "invalid":
                invalid.append({"note": note, "session": session[:4] + "***", "error": error})
                continue

            if phone and phone in existing_phones:
                duplicated.append({"note": note, "session": session[:4] + "***", "reason": "手机号已存在"})
                continue

            account = {
                "id": uuid.uuid4().hex[:10],
                "note": (prefix + (note or "导入")) if prefix else (note or "导入-%s" % uuid.uuid4().hex[:4]),
                "phone": phone,
                "session": session,
                "identity": new_identity(),
                "identity_at": now_ts(),
                "enabled": True,
                "state": "ok",
                "points": points,
                "points_at": now_ts() if points is not None else 0,
                "created_at": now_ts(),
                "imported": True,
                "fail_count": 0,
                "last_error": "",
            }
            CFG.setdefault("accounts", []).append(account)
            existing_sessions.add(session)
            imported.append({"id": account["id"], "note": account["note"],
                             "session": session[:4] + "***", "points": points})
            await asyncio.sleep(0.15)

    save_config(CFG)
    if imported:
        asyncio.create_task(fetch_models(force=True))
    log_line("import.log", {"imported": len(imported), "duplicated": len(duplicated), "invalid": len(invalid)})
    return {"ok": True, "imported": len(imported), "duplicated": len(duplicated),
            "invalid": len(invalid), "items": imported,
            "duplicated_items": duplicated, "invalid_items": invalid,
            "total_accounts": len(CFG.get("accounts", []))}


@app.post("/admin/api/accounts/{account_id}/identity/rebind")
async def admin_rebind_identity(account_id: str, request: Request) -> dict[str, Any]:
    """给该账号换一套新的设备标识（devid / campus_device_id）。"""
    panel_auth(request)
    account = account_by_id(account_id) or _missing()
    identity = ensure_identity(account, rebind=True)
    save_config(CFG)
    return {"ok": True, "devid": identity.get("devid"), "campus_device_id": identity.get("campus_device_id")}


@app.post("/admin/api/identity/rebind-all")
async def admin_rebind_all(request: Request) -> dict[str, Any]:
    """给所有还没有设备标识的账号补一套（不覆盖已有的）。"""
    panel_auth(request)
    created = 0
    for account in CFG.get("accounts", []):
        if not (account.get("identity") or {}).get("devid"):
            ensure_identity(account)
            created += 1
    save_config(CFG)
    return {"ok": True, "created": created, "total": len(CFG.get("accounts", []))}


@app.post("/admin/api/accounts/{account_id}/cooldown/clear")
async def admin_clear_cooldown(account_id: str, request: Request) -> dict[str, Any]:
    panel_auth(request)
    account = account_by_id(account_id) or _missing()
    clear_cooldown(account)
    account["fail_count"] = 0
    account["state"] = "ok"
    save_config(CFG)
    return {"ok": True}


@app.post("/admin/api/accounts/{account_id}/balance")
async def admin_balance(account_id: str, request: Request) -> dict[str, Any]:
    panel_auth(request)
    account = account_by_id(account_id) or _missing()
    return await fetch_balance(account)


@app.post("/admin/api/sms/send")
async def admin_sms_send(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    msgid = await sms_send(str(payload.get("phone") or ""))
    return {"ok": True, "msgid": msgid}


@app.post("/admin/api/sms/login")
async def admin_sms_login(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    account = await sms_login(str(payload.get("phone") or ""), str(payload.get("code") or ""), str(payload.get("note") or ""))
    return {"ok": True, "id": account["id"], "points": account.get("points")}


@app.post("/admin/api/daily/run")
async def admin_daily_run(request: Request) -> dict[str, Any]:
    panel_auth(request)
    result = await run_daily_all()
    return result


@app.post("/admin/api/test")
async def admin_test(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    account = account_by_id(str(payload.get("account_id") or "")) if payload.get("account_id") else None
    model = normalize_model(payload.get("model") or "")
    if not model:
        cached = [m["raw_id"] for m in model_details(MODEL_CACHE["data"])] or []
        model = cached[0] if cached else str(CFG.get("default_model") or "qwen3.8-flash").replace("imodel/", "")
    return await probe_ttft(model, account)


@app.post("/admin/api/config")
async def admin_config(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    for key in ("daily_time", "daily_enabled", "balance_interval_min", "default_model", "fallback_model",
                "loomy_version", "upstream_base", "strategy", "cooldown_seconds", "max_retries", "identity_mode"):
        if key in payload:
            CFG[key] = payload[key]
    if payload.get("panel_password"):
        CFG["panel_password"] = str(payload["panel_password"]).strip()
    if "api_keys" in payload and isinstance(payload["api_keys"], list):
        CFG["api_keys"] = [str(k).strip() for k in payload["api_keys"] if str(k).strip()]
    save_config(CFG)
    return {"ok": True}


@app.get("/admin/api/models")
async def admin_models(request: Request, refresh: int = 0) -> dict[str, Any]:
    panel_auth(request)
    await fetch_models(force=bool(refresh))
    rows = catalog_list()
    online = [r for r in rows if r.get("online")]
    return {
        "models": rows,
        "online": len(online),
        "offline": len(rows) - len(online),
        "fetched_at": int(MODEL_CACHE["at"]),
    }


@app.post("/admin/api/models/{raw_id}/probe")
async def admin_model_probe(raw_id: str, request: Request) -> dict[str, Any]:
    """单模型测活：只等首包（最多 120 秒），拿到即断，不烧完整生成。"""
    panel_auth(request)
    result = await probe_ttft(raw_id)
    catalog = CFG.setdefault("models_catalog", {})
    entry = catalog.get(raw_id)
    if entry is not None:
        entry["last_probe"] = now_ts()
        entry["probe_ok"] = bool(result.get("ok"))
        entry["ttft_ms"] = result.get("ttft_ms")
        if not result.get("ok"):
            entry["probe_error"] = str(result.get("error"))[:200]
        else:
            entry.pop("probe_error", None)
        save_config(CFG)
    return result


@app.get("/admin/api/tasks")
async def admin_tasks(request: Request) -> dict[str, Any]:
    panel_auth(request)
    accounts = [a for a in CFG.get("accounts", []) if a.get("session")]
    out = []
    async with httpx.AsyncClient(timeout=30, verify=False, trust_env=False) as client:
        for account in accounts:
            state = await tasks_state(account, client)
            out.append({
                "id": account.get("id"),
                "note": account.get("note"),
                "phone": account.get("phone", ""),
                "points": account.get("points"),
                "state": account.get("state"),
                **{k: state.get(k) for k in ("ok", "rows", "earned", "total", "unclaimed", "error")},
            })
            await asyncio.sleep(0.3)
    return {"accounts": out, "task_total": TASK_TOTAL, "last_run": CFG.get("tasks_last_run")}


@app.post("/admin/api/tasks/run")
async def admin_tasks_run(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    account_id = str(payload.get("account_id") or "")
    do_daily = payload.get("daily", True)
    targets = [account_by_id(account_id)] if account_id else [a for a in CFG.get("accounts", []) if a.get("session")]
    targets = [t for t in targets if t]
    if not targets:
        raise HTTPException(400, "没有可用账号")
    results = []
    for account in targets:
        result = await run_tasks(account, bool(do_daily))
        results.append(result)
        log_line("tasks.log", {"account": account.get("note"), **{k: result.get(k) for k in ("daily", "after", "error")}})
        await asyncio.sleep(0.5)
    CFG["tasks_last_run"] = {"at": datetime.now(TZ).strftime("%F %T"), "accounts": len(results),
                             "claimed": sum(len([c for c in r.get("claims", []) if c.get("ok")]) for r in results)}
    save_config(CFG)
    return {"ok": True, "results": results}


@app.post("/admin/api/tasks/claim")
async def admin_tasks_claim(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    account = account_by_id(str(payload.get("account_id") or "")) or _missing()
    key = str(payload.get("key") or "")
    if key not in {item[1] for item in TASK_DEFS}:
        raise HTTPException(400, "未知任务 key")
    async with httpx.AsyncClient(timeout=40, verify=False, trust_env=False) as client:
        claim = await claim_one(account, client, key)
        await asyncio.sleep(0.8)
        after = await tasks_state(account, client)
    if after.get("ok"):
        await fetch_balance(account)
    return {"ok": bool(claim.get("ok")), "claim": claim, "state": after}


def _mask_pool(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for field in ("token", "password"):
        value = str(out.get(field) or "")
        out["%s_set" % field] = bool(value)
        out[field] = (value[:4] + "***") if value else ""
    return out


@app.get("/admin/api/pool")
async def admin_pool(request: Request) -> dict[str, Any]:
    panel_auth(request)
    job = {k: v for k, v in POOL_JOB.items() if k != "logs"}
    return {"config": _mask_pool(pool_cfg()), "job": job, "logs": POOL_JOB["logs"][-120:],
            "accounts": len(CFG.get("accounts", []))}


@app.post("/admin/api/pool/config")
async def admin_pool_config(request: Request) -> dict[str, Any]:
    panel_auth(request)
    payload = await request.json()
    current = dict(CFG.get("sms_pool") or {})
    for secret in ("token", "password"):
        value = str(payload.get(secret) or "")
        if value and "***" not in value:
            current[secret] = value.strip()
    for key, value in payload.items():
        if key in ("token", "password"):
            continue
        if value is not None:
            current[key] = value
    CFG["sms_pool"] = current
    save_config(CFG)
    return {"ok": True}


@app.post("/admin/api/pool/test")
async def admin_pool_test(request: Request) -> dict[str, Any]:
    panel_auth(request)
    provider = make_provider()
    if not provider.base:
        raise HTTPException(400, "还没填平台地址")
    async with httpx.AsyncClient(timeout=25, verify=False, trust_env=False) as client:
        try:
            return await provider.test(client)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}


@app.get("/admin/api/pool/balance")
async def admin_pool_balance(request: Request) -> dict[str, Any]:
    panel_auth(request)
    provider = make_provider()
    if not provider.base:
        raise HTTPException(400, "还没填平台地址")
    async with httpx.AsyncClient(timeout=20, verify=False, trust_env=False) as client:
        try:
            return await provider.balance(client)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}


@app.post("/admin/api/pool/start")
async def admin_pool_start(request: Request) -> dict[str, Any]:
    panel_auth(request)
    if POOL_JOB.get("running"):
        raise HTTPException(409, "任务已在运行")
    payload = await request.json()
    count = int(payload.get("count") or 1)
    asyncio.create_task(pool_worker(count))
    await asyncio.sleep(0.3)
    return {"ok": True, "total": POOL_JOB.get("total")}


@app.post("/admin/api/pool/stop")
async def admin_pool_stop(request: Request) -> dict[str, Any]:
    panel_auth(request)
    POOL_JOB["stop"] = True
    return {"ok": True}


@app.get("/admin/api/logs")
async def admin_logs(request: Request, lines: int = 100) -> dict[str, Any]:
    panel_auth(request)
    out: dict[str, Any] = {}
    for name in ("usage.log", "daily.log"):
        path = LOG_DIR / name
        if path.exists():
            content = path.read_text("utf-8", errors="replace").splitlines()
            out[name] = content[-min(max(lines, 1), 500):]
    return out


if __name__ == "__main__":
    import uvicorn

    listen = CFG.get("listen") or {}
    uvicorn.run(
        app,
        host=str(listen.get("host") or "127.0.0.1"),
        port=int(listen.get("port") or 4556),
        log_level="info",
        timeout_graceful_shutdown=5,
        timeout_keep_alive=30,
    )
