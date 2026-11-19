#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loomy 本地注册器（GUI + CLI）
=================================
在**你自己的电脑**上跑：接码平台取号 → 请求 Loomy 短信验证码 → 轮询取码 → 登录拿 session
→ 一键上传到已部署的 Loomy2API 服务（只传 session，不上传账号密码）。

特性
----
* 零依赖：只用 Python 标准库（Tkinter + urllib），Mac / Windows 双击即用
* 并发注册：多路同时撒号，谁先来码算谁
* 失败即换号：超时立刻拉黑换下一个号码
* 结果一键**导出 txt 文件**（`备注|session` 每行），拿去服务端面板「批量导入」
* CLI 模式：`python3 loomy_registrar_gui.py --cli 5 --out a.txt` 无界面批量跑并导出
* **脚本不含任何账号密码**：接码平台账号/项目 ID 由使用者在界面里自己填，只存本机

使用
----
    python3 loomy_registrar_gui.py                     # 打开本地界面 http://127.0.0.1:8899
    python3 loomy_registrar_gui.py --cli 5             # 命令行注册 5 个，结果打印到终端
    python3 loomy_registrar_gui.py --cli 5 --out a.txt # 命令行注册并把 备注|session 写入 a.txt

声明：仅用于你自己有权使用的账号；请勿滥用、转售或压垮上游服务。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from email.utils import formatdate
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib import request as urlrequest

# ------------------------------------------------------------------ 常量

APP_TITLE = "Loomy 本地注册器"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "registrar.json")
HERE = os.path.dirname(os.path.abspath(__file__))
META_FILE = os.path.join(HERE, "registrar_meta.json")

ACCOUNT_BASE = "https://account.xfinfr.com"
SEND_CODE_PATH = "/login/phone/sendMsgCode"
LOGIN_PATH = "/login/phone/checkCode"
MODEL_BASE = "https://loomyad.xunfei.cn/api/v1"

ACCESS_KEY_ID = "2thryby66wxi53sk"
ACCESS_KEY_SECRET = "zsak6eadrbawz683wf5r3m2snrwj868r"
APP_ID, MODEL_ID, CLIENT_VERSION, DEVICE_ID, CLIENT_UA = "GM3LOOMY", "Web", "1.0.0", "web", "Loomy|Desktop|Electron|macOS"

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
CODE_RE = re.compile(r"(?<!\d)(\d{4,6})(?!\d)")
IGNORE_CODE_KEYS = ("msgid", "msg_id", "phone", "mobile", "id", "orderid", "order_id",
                    "taskid", "task_id", "traceid", "timestamp", "time")

DEFAULT_CFG: Dict[str, Any] = {
    # 接码平台：使用者在界面里填自己的（脚本不含任何账号密码）
    "base_url": "",
    "username": "",
    "password": "",
    "project_id": "",
    "path_login": "/Login/?username={username}&password={password}&type=json",
    "path_balance": "/Balance/?token={token}&type=json",
    "path_get_phone": "/GetPhone/?token={token}&id={project}&type=json",
    "path_get_code": "/GetMsg/?token={token}&id={project}&phone={phone}&type=json",
    "path_release": "/Cancel/?token={token}&id={project}&phone={phone}&type=json",
    "path_blacklist": "/Addblack/?token={token}&id={project}&phone={phone}&type=json",
    # 注册参数
    "count": 5,
    "concurrency": 5,
    "code_timeout": 60,
    "attempts": 5,
    "block_prefixes": "192,162,167",
    "prefer_prefixes": "1709524",
    "note_prefix": "本地注册",
}


# ------------------------------------------------------------------ 小工具

def load_cfg() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            stored = json.load(handle)
        for key in DEFAULT_CFG:          # 只认已知键，丢弃历史遗留字段
            if key in stored:
                cfg[key] = stored[key]
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_cfg(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=1)
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass


def mask(value: str, keep: int = 4) -> str:
    value = str(value or "")
    return value[:keep] + "***" + value[-2:] if len(value) > keep + 2 else value


def http_get(url: str, timeout: int = 25) -> str:
    req = urlrequest.Request(url, headers={"Accept": "application/json, text/plain, */*"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ 接码平台

def pipe_ok(raw: str) -> Tuple[bool, List[str]]:
    text = (raw or "").strip()
    if "|" not in text:
        return False, []
    head, *rest = text.split("|")
    return head.strip() == "1", [item.strip() for item in rest]


class SmsPlatform:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base = str(cfg.get("base_url") or "").rstrip("/")
        self.token = ""

    def _url(self, key: str, phone: str = "") -> str:
        path = str(self.cfg.get(key) or DEFAULT_CFG.get(key) or "")
        if not path:
            raise RuntimeError("接口路径未配置：%s" % key)
        values = {
            "token": self.token,
            "project": str(self.cfg.get("project_id") or ""),
            "username": str(self.cfg.get("username") or ""),
            "password": str(self.cfg.get("password") or ""),
            "phone": phone,
            "num": "1",
        }
        out = path
        for k, v in values.items():
            out = out.replace("{%s}" % k, urlparse.quote(str(v), safe=""))
        return out if out.startswith("http") else self.base + (out if out.startswith("/") else "/" + out)

    def login(self) -> str:
        raw = http_get(self._url("path_login"))
        ok, fields = pipe_ok(raw)
        if ok and fields:
            self.token = fields[0]
            return self.token
        try:
            data = json.loads(raw)
            token = (data.get("data") or {}).get("token") if isinstance(data.get("data"), dict) else data.get("data")
            if token:
                self.token = str(token)
                return self.token
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("平台登录失败：%s" % raw[:120])

    def balance(self) -> str:
        last_raw = ""
        for attempt in range(3):
            if attempt or not self.token:
                try:
                    self.token = ""       # 每轮重新登录，避免 token 失效拿到 data=null
                    self.login()
                except Exception as exc:  # noqa: BLE001
                    return "登录失败：%s" % str(exc)[:80]
            raw = http_get(self._url("path_balance"))
            last_raw = raw
            ok, fields = pipe_ok(raw)
            if ok and fields:
                return fields[0]
            try:
                data = json.loads(raw)
                inner = data.get("data")
                if isinstance(inner, dict):
                    money = inner.get("money") or inner.get("balance")
                    if money is not None:
                        return str(money)
                if isinstance(inner, str) and inner:
                    return inner
                if str(data.get("code")) == "1" and inner is None:
                    return "0"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        return "平台原文：" + last_raw[:100]

    def get_phone(self) -> str:
        if not self.token:
            self.login()
        raw = http_get(self._url("path_get_phone"))
        ok, fields = pipe_ok(raw)
        if ok and fields:
            match = PHONE_RE.search(fields[0])
            if match:
                return match.group(0)
        # JSON 形态：{"stat":true,"data":"137..."} 或 data 为对象
        match = PHONE_RE.search(raw or "")
        if match:
            return match.group(0)
        raise RuntimeError("取号失败：%s" % raw[:80])

    def get_code(self, phone: str) -> Optional[str]:
        if not self.token:
            self.login()
        raw = http_get(self._url("path_get_code", phone))
        ok, fields = pipe_ok(raw)
        candidate = " ".join(fields) if (ok and fields) else raw
        # 去掉 \uXXXX 转义再匹配，避免把转义里的数字当验证码
        text = re.sub(r"\\u[0-9a-fA-F]{4}", " ", candidate).replace(phone, " ")
        text = re.sub(r'"(code|stat|status)"\s*:\s*-?\d+', " ", text)
        match = CODE_RE.search(text)
        return match.group(1) if match else None

    def release(self, phone: str) -> None:
        try:
            http_get(self._url("path_release", phone))
        except Exception:  # noqa: BLE001
            pass

    def blacklist(self, phone: str) -> None:
        try:
            http_get(self._url("path_blacklist", phone))
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ Loomy 登录

def new_identity() -> Dict[str, str]:
    return {"devid": "web-%s" % uuid.uuid4().hex[:16], "modelid": MODEL_ID,
            "version": CLIENT_VERSION, "ua": CLIENT_UA,
            "campus_device_id": "loomy-campus-%s" % uuid.uuid4()}


def _escaped_path(path: str) -> str:
    safe = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
    clean = path if path.startswith("/") else "/" + path
    return "/".join("".join(chr(b) if b in safe else "%%%02X" % b for b in seg.encode())
                    for seg in clean.split("/"))


def signed_post(path: str, payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    digest = base64.b64encode(hashlib.md5(body.encode()).digest()).decode() if body else ""
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    nonce = str(uuid.uuid4())
    sts = "\n".join(["POST", _escaped_path(path), "", digest, "application/json", date, nonce, "", ""])
    signature = base64.b64encode(hmac.new(ACCESS_KEY_SECRET.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    headers = {"Authorization": "account %s:%s" % (ACCESS_KEY_ID, signature), "Date": date,
               "Nonce": nonce, "Content-Type": "application/json"}
    if digest:
        headers["Content-MD5"] = digest
    req = urlrequest.Request(ACCOUNT_BASE + path, data=body.encode(), headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def base_envelope(identity: Dict[str, str]) -> Dict[str, Any]:
    return {"appid": APP_ID, "modelid": identity["modelid"], "version": identity["version"],
            "devid": identity["devid"], "ua": identity["ua"], "traceid": os.urandom(16).hex()}


def loomy_send_code(phone: str, identity: Dict[str, str]) -> str:
    raw = signed_post(SEND_CODE_PATH, {"base": base_envelope(identity),
                                       "param": {"ccode": "86", "phone": phone, "expire": 300}})
    data = json.loads(raw)
    if str(data.get("code")) != "000000":
        raise RuntimeError("Loomy 发码失败：%s" % (data.get("desc") or data.get("code")))
    inner = data.get("data") or {}
    msgid = inner.get("msgid") or inner.get("msgId") or ""
    if not msgid:
        raise RuntimeError("Loomy 未返回 msgid")
    return msgid


def loomy_login(phone: str, code: str, msgid: str, identity: Dict[str, str]) -> Dict[str, str]:
    raw = signed_post(LOGIN_PATH, {"base": base_envelope(identity),
                                   "param": {"ccode": "86", "phone": phone, "mcode": code,
                                             "msgid": msgid, "expire": 14 * 24 * 3600}})
    data = json.loads(raw)
    if str(data.get("code")) != "000000":
        raise RuntimeError("登录失败：%s" % (data.get("desc") or data.get("code")))
    session = (data.get("data") or {}).get("session") or ""
    if not session:
        raise RuntimeError("登录成功但无 session")
    return {"session": session, "userid": str((data.get("data") or {}).get("userid") or "")}


def check_session(session: str) -> Optional[int]:
    """校验 session 并返回积分；无效返回 None。"""
    ident = new_identity()
    req = urlrequest.Request(MODEL_BASE + "/points/balance",
                             headers={"Authorization": "Bearer " + session, "token": session,
                                      "X-Loomy-Device-ID": ident["devid"],
                                      "User-Agent": CLIENT_UA, "loomy-version": "0.9.39",
                                      "traceparent": "00-%s-%s-01" % (os.urandom(16).hex(), os.urandom(8).hex())})
    try:
        with urlrequest.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return None
    if str(payload.get("code")) != "000000":
        return None
    found: List[int] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, key)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            if any(t in key.lower() for t in ("balance", "point", "credit", "remain", "available", "total")):
                found.append(int(node))

    walk(payload)
    return max(found) if found else 0


# ------------------------------------------------------------------ 注册任务

def load_meta() -> Dict[str, Any]:
    try:
        with open(META_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_meta(meta: Dict[str, Any]) -> None:
    try:
        with open(META_FILE, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=1)
        os.chmod(META_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass


def remember_session(item: Dict[str, Any]) -> None:
    meta = load_meta()
    meta[item["session"]] = {"note": item.get("note", ""), "phone": item.get("phone", ""),
                             "points": item.get("points"), "at": int(time.time())}
    save_meta(meta)


_refreshing = {"sessions": set(), "lock": threading.Lock()}


def refresh_points_async(sessions: List[str]) -> None:
    """后台给缺失积分的 session 补查（每个 session 一次 /points/balance）。"""
    def worker():
        meta = load_meta()
        for session in sessions:
            with _refreshing["lock"]:
                if session in _refreshing["sessions"]:
                    continue
                _refreshing["sessions"].add(session)
            try:
                points = check_session(session)
                entry = meta.setdefault(session, {})
                entry["points"] = points
                entry["at"] = int(time.time())
                save_meta(meta)
            except Exception:  # noqa: BLE001
                pass
            finally:
                with _refreshing["lock"]:
                    _refreshing["sessions"].discard(session)
            time.sleep(0.3)

    threading.Thread(target=worker, daemon=True).start()


def saved_path() -> str:
    return os.path.join(HERE, "loomy_sessions_%s.txt" % time.strftime("%Y%m%d"))


def saved_count() -> int:
    path = saved_path()
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip() and not line.startswith("#"))


def saved_all() -> List[str]:
    path = saved_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def saved_recent(limit: int = 30) -> List[str]:
    path = saved_path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    return lines[-limit:]


def append_result_file(item: Dict[str, Any]) -> str:
    """每成功一个就立刻追加到本地文件，避免关闭页面/崩溃丢号。"""
    path = saved_path()
    try:
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            if new_file:
                handle.write("# %s 自动保存（备注|session），可直接粘到服务端“批量导入 session”\n"
                             % time.strftime("%F %T"))
            handle.write("%s|%s\n" % (item["note"], item["session"]))
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return path


class Registrar:
    def __init__(self, cfg: Dict[str, Any], log):
        self.cfg = cfg
        self.log = log
        self.stop_flag = threading.Event()
        self.ok = 0
        self.fail = 0
        self.done = 0
        self.results: List[Dict[str, Any]] = []
        self.current = ""
        self.lock = threading.Lock()
        self.platform = SmsPlatform(cfg)

    def _blocked(self, phone: str) -> bool:
        block = [p.strip() for p in str(self.cfg.get("block_prefixes") or "").split(",") if p.strip()]
        return any(phone.startswith(p) for p in block)

    def one(self, index: int) -> None:
        timeout = max(20, int(self.cfg.get("code_timeout") or 60))
        attempts = max(1, int(self.cfg.get("attempts") or 1))
        last = ""
        for attempt in range(1, attempts + 1):
            if self.stop_flag.is_set():
                return
            phone = ""
            try:
                prefer = [x.strip() for x in str(self.cfg.get("prefer_prefixes") or "").split(",") if x.strip()]
                for _ in range(6):
                    phone = self.platform.get_phone()
                    if self._blocked(phone):
                        self.log("[%d] 屏蔽号段 %s，换号" % (index, phone))
                        self.platform.release(phone)
                        phone = ""
                        continue
                    if prefer and not any(phone.startswith(x) for x in prefer):
                        self.log("[%d] 非优先号段 %s（prefer=%s），换号" % (index, phone, ",".join(prefer)))
                        self.platform.release(phone)
                        phone = ""
                        continue
                    break
                if not phone:
                    raise RuntimeError("取不到可用号码")

                self.current = phone
                identity = new_identity()
                msgid = loomy_send_code(phone, identity)
                self.log("[%d] %s 已发码，等待短信…" % (index, phone))

                deadline = time.time() + timeout
                code = None
                while time.time() < deadline and not self.stop_flag.is_set():
                    code = self.platform.get_code(phone)
                    if code:
                        break
                    time.sleep(4)
                if not code:
                    raise RuntimeError("等码超时(%ss)" % timeout)

                info = loomy_login(phone, code, msgid, identity)
                points = check_session(info["session"])
                note = "%s%s" % (self.cfg.get("note_prefix") or "本地", time.strftime("%m%d-%H%M%S"))
                item = {"note": note, "phone": phone, "session": info["session"], "points": points}
                with self.lock:
                    self.ok += 1
                    self.results.append(item)
                saved = append_result_file(item)
                remember_session(item)
                self.log("[%d] ✅ %s 登录成功（积分 %s）｜已落盘 %s"
                         % (index, phone, points, os.path.basename(saved)))
                return
            except Exception as exc:  # noqa: BLE001
                self.current = ""
                last = str(exc)
                self.log("[%d] 第 %d/%d 次失败：%s" % (index, attempt, attempts, last))
                if phone:
                    self.platform.blacklist(phone)
                if attempt < attempts:
                    self.log("[%d] 换号重试" % index)
            finally:
                pass
        with self.lock:
            self.fail += 1
        self.log("[%d] ❌ 放弃：%s" % (index, last))

    def run(self, count: int, concurrency: int) -> List[Dict[str, Any]]:
        self.stop_flag.clear()
        self.ok = self.fail = self.done = 0
        self.results = []
        lock_seat = threading.Semaphore(max(1, min(concurrency, 20)))

        def worker(i: int) -> None:
            with lock_seat:
                if self.stop_flag.is_set():
                    return
                self.one(i)
                with self.lock:
                    self.done += 1

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(1, count + 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return self.results

    def stop(self) -> None:
        self.stop_flag.set()


# ------------------------------------------------------------------ 上传

# ------------------------------------------------------------------ CLI

def run_cli(count: int, out_path: str = "") -> int:
    cfg = load_cfg()
    if not (cfg.get("base_url") and cfg.get("username") and cfg.get("password") and cfg.get("project_id")):
        print("❌ 先在 GUI 里填好接码平台配置（会存到 registrar.json），或用 --cli 前手动编辑该文件")
        return 2
    out = queue.Queue()

    def logger(msg: str) -> None:
        out.put(msg)
        print(msg, flush=True)

    reg = Registrar(cfg, logger)
    results = reg.run(count, int(cfg.get("concurrency") or 5))
    print("\n=== 结果 %d 个 ===" % len(results))
    lines = ["%s|%s" % (item["note"], item["session"]) for item in results]
    for line in lines:
        print(line)
    target = out_path or ("loomy_sessions_%s.txt" % time.strftime("%Y%m%d_%H%M")) if results else ""
    if target:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(target, 0o600)
        print("已导出：%s（%d 条）" % (target, len(lines)))
    return 0


# ------------------------------------------------------------------ GUI（本地网页版）

PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<title>Loomy 本地注册器</title>
<style>
:root{--bg:#0b0f14;--panel:#121820;--line:#233040;--text:#e6edf6;--muted:#8fa3b8;--accent:#4ea8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}
h1{font-size:17px;margin:0}.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);padding:2px 8px;border-radius:99px}
main{padding:16px 22px 60px;max-width:1100px;margin:0 auto}
section{margin-bottom:18px}h2{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin:0 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>div{flex:1;min-width:120px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
input{width:100%;background:#0d141c;border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 10px;font-size:13px}
button{background:#171f29;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--accent);color:var(--accent)}button.primary{background:linear-gradient(180deg,#2b7fd4,#1f66b0);border-color:#2b7fd4;color:#fff}
pre{background:#0a1018;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12px;
font-family:ui-monospace,Menlo,monospace;max-height:320px;margin:0;color:#b9cde3;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:12.5px}
th{color:var(--muted);background:#171f29}.mono{font-family:ui-monospace,Menlo,monospace}
.hint{color:var(--muted);font-size:12px}.ok{color:#3ddc97}.bad{color:#ff5d5d}
</style></head><body>
<header><h1>🌙 Loomy 本地注册器</h1><span class="tag">注册在本机 · 只上传 session</span>
<span class="tag" id="bal">余额 -</span><span style="flex:1"></span>
<span class="tag" id="statOk" style="color:#3ddc97">成功 0</span>
<span class="tag" id="statFail" style="color:#ff5d5d">失败 0</span>
<span class="tag" id="statProg">进度 0/0</span>
<span class="tag mono" id="statCur">—</span>
<button onclick="ping()" style="padding:6px 10px">自检</button>
<span class="hint" id="st">就绪</span></header>
<main>
<section><h2>接码平台</h2><div class="card">
<div class="row">
 <div style="flex:2"><label>平台地址</label><input id="base_url"></div>
 <div><label>项目 ID</label><input id="project_id"></div>
 <div><label>平台账号</label><input id="username"></div>
 <div><label>平台密码（已保存则留空不改）</label><input id="password" type="password" placeholder="留空则沿用已保存的密码"></div>
</div></div></section>
<section><h2>注册参数</h2><div class="card">
<div class="row">
 <div><label>数量</label><input id="count" type="number" min="1"></div>
 <div><label>并发</label><input id="concurrency" type="number" min="1" max="20"></div>
 <div><label>等码秒数</label><input id="code_timeout" type="number" min="20"></div>
 <div><label>换号次数</label><input id="attempts" type="number" min="1"></div>
 <div><label>备注前缀</label><input id="note_prefix"></div>
 <div><label>屏蔽号段</label><input id="block_prefixes"></div>
</div>
<div class="row" style="margin-top:12px">
 <div style="flex:0 0 130px"><button class="primary" style="width:100%" onclick="start()">开始注册</button></div>
 <div style="flex:0 0 100px"><button style="width:100%" onclick="stop()">停止</button></div>
 <div style="flex:0 0 110px"><button style="width:100%" onclick="refreshBalance()">查余额</button></div>
 <div style="flex:0 0 150px"><button style="width:100%" onclick="save()">保存配置</button></div>
</div></div></section>
<section><h2>导出结果</h2><div class="card">
<div class="row">
 <div style="flex:0 0 200px"><label>&nbsp;</label><button class="primary" style="width:100%" onclick="exportFile()">导出 session 文件</button></div>
 <div style="flex:0 0 170px"><label>&nbsp;</label><button style="width:100%" onclick="copyAll()">复制全部</button></div>
</div>
<div class="hint" style="margin-top:8px">
 导出为 <span class="mono">loomy_sessions_时间.txt</span>，每行 <span class="mono">备注|session</span>；
 把文件拿到服务端面板「批量导入 session」贴上即可（服务端会自动校验、去重、生成独立设备标识）。
</div>
</div></section>
<section><h2>结果 <span class="hint" id="cnt" style="text-transform:none;letter-spacing:0"></span></h2>
<table><thead><tr><th>备注</th><th>手机号</th><th>session</th><th>积分</th></tr></thead>
<tbody id="rows"><tr><td colspan="4" class="hint">暂无</td></tr></tbody></table></section>
<section><h2>日志</h2><pre id="log">（未开始）</pre></section>
<div class="hint" style="margin-top:10px">build __BUILD__ · 若看到旧界面请按 Cmd/Ctrl+Shift+R 强制刷新</div>
</main>
<script>
const F=['base_url','project_id','username','password','count','concurrency','code_timeout','attempts','note_prefix','block_prefixes'];
let RESULTS=[];let REFILL=0;
const $=id=>document.getElementById(id);
async function api(path,opt){const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opt||{}));
const t=await r.text();let d;try{d=t?JSON.parse(t):null}catch(e){d=t}if(!r.ok)throw new Error((d&&d.detail)||t);return d}
async function load(){const d=await api('api/state');F.forEach(k=>{if(d.config[k]!==undefined&&!(k==='password'&&d.config[k]==='***'))$(k).value=d.config[k]});
$('bal').textContent='余额 '+(d.balance||'-');
$('statOk').textContent='成功 '+(d.ok||0);
$('statFail').textContent='失败 '+(d.fail||0);
$('statProg').textContent='进度 '+(d.done||0)+'/'+(d.total||0);
$('statCur').textContent=d.current||'—';
$('st').textContent=d.running?'注册中…':(d.done?('本批完成：成功 '+d.ok+' 失败 '+d.fail):'就绪');
RESULTS=d.results||[];
$('cnt').textContent='本批成功 '+d.ok+' · 失败 '+d.fail+' · 进度 '+d.done+'/'+d.total+' ｜ 累计已落盘 '+(d.saved_total||0)+' 条 '+(d.save_file||'');
render();$('log').textContent=(d.logs||[]).join('\\n')||'（未开始）';$('log').scrollTop=1e9;
if(d.save_file)$('st').textContent='已自动保存到 '+d.save_file+(d.running?'（注册中）':'');
if(d.running){setTimeout(load,1500)}
if(!d.running&&RESULTS.some(r=>r.points==null)&&REFILL<6){REFILL++;setTimeout(load,3000)}}
function render(){const b=$('rows');if(!RESULTS.length){b.innerHTML='<tr><td colspan="4" class="hint">暂无</td></tr>';return}
b.innerHTML=RESULTS.map(r=>`<tr><td>${r.note}</td><td class="mono">${r.phone}</td><td class="mono">${r.session.slice(0,12)}…</td><td class="mono">${r.points??'-'}</td></tr>`).join('')}
function payload(){const o={};F.forEach(k=>{let v=$(k).value.trim();if(['count','concurrency','code_timeout','attempts'].includes(v))v=Number(v);
if(['count','concurrency','code_timeout','attempts'].includes(k))v=Number(v)||0;o[k]=v});return o}
async function save(){
 try{await api('api/save',{method:'POST',body:JSON.stringify(payload())});$('st').textContent='配置已保存';return true}
 catch(e){$('st').textContent='保存失败：'+e.message;alert('保存失败：'+e.message);return false}}
async function start(){
 try{
  $('st').textContent='正在提交…';
  await api('api/save',{method:'POST',body:JSON.stringify(payload())});
  const r=await api('api/start',{method:'POST',body:JSON.stringify(payload())});
  $('st').textContent='已提交，目标 '+(r.total||'?')+' 个，等待首个结果…';
  load();
 }catch(e){$('st').textContent='启动失败：'+e.message;alert('启动失败：'+e.message+'；若提示 409 说明已有任务在跑，点停止后再试')}}
async function stop(){
 try{await api('api/stop',{method:'POST'});$('st').textContent='已请求停止'}
 catch(e){$('st').textContent='停止失败：'+e.message}}
async function refreshBalance(){
 try{const d=await api('api/balance',{method:'POST'});
   $('bal').textContent='余额 '+(d.balance||d.error||'-');
   $('st').textContent=d.ok?('余额已更新：'+d.balance):('查余额失败：'+(d.error||''));}
 catch(e){$('st').textContent='查余额请求失败：'+e.message}}
function ping(){fetch('api/state').then(r=>r.json()).then(d=>{$('st').textContent='自检通过：接口可达，build 2026-11-19d'});}
function exportFile(){if(!RESULTS.length){$('st').textContent='还没有成功的 session';return}location.href='api/export'}
function copyAll(){const t=RESULTS.map(r=>r.note+'|'+r.session).join('\\n');navigator.clipboard.writeText(t);$('st').textContent='已复制 '+RESULTS.length+' 条'}
load();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    cfg: Dict[str, Any] = {}
    reg: Optional[Registrar] = None
    logs: List[str] = []
    total = 0

    def log_message(self, fmt, *args):  # noqa: D102
        try:
            print("[http] %s %s" % (self.command, self.path), flush=True)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, code: int, body: Any, ctype: str = "application/json; charset=utf-8") -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    BUILD = "2026-11-19d"

    def _merged_results(self) -> List[Dict[str, Any]]:
        """落盘累计 + 本批内存，按 session 去重（内存优先，带手机号和积分）。"""
        meta = load_meta()
        merged: Dict[str, Dict[str, Any]] = {}
        for line in saved_all():
            note, _, session = line.partition("|")
            if not session:
                continue
            info = meta.get(session) or {}
            merged[session] = {"note": note or info.get("note", ""), "session": session,
                               "phone": info.get("phone", ""), "points": info.get("points")}
        reg = self.__class__.reg
        for item in (getattr(reg, "results", []) if reg else []):
            merged[item["session"]] = item
        # 缺积分的后台补查（限流，不阻塞页面）
        missing = [k for k, v in merged.items() if v.get("points") is None]
        if missing:
            refresh_points_async(missing[:40])
        return list(merged.values())

    def _page(self) -> str:
        """把配置直接渲染进输入框，避免依赖前端回填/缓存。"""
        import html as _html
        page = PAGE.replace("__BUILD__", self.BUILD)
        for key in ("base_url", "project_id", "username", "count", "concurrency",
                    "code_timeout", "attempts", "note_prefix", "block_prefixes"):
            value = _html.escape(str(self.cfg.get(key) if self.cfg.get(key) is not None else ""), quote=True)
            page = page.replace('id="%s"' % key, 'id="%s" value="%s"' % (key, value), 1)
        return page

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, self._page(), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/export"):
            results = self._merged_results()
            text = "".join("%s|%s\n" % (r["note"], r["session"]) for r in results)
            if not text:
                today = os.path.join(HERE, "loomy_sessions_%s.txt" % time.strftime("%Y%m%d"))
                if os.path.exists(today):
                    with open(today, encoding="utf-8") as handle:
                        text = "".join(line for line in handle if not line.startswith("#"))
                if not text:
                    self._send(400, {"detail": "还没有成功的 session"})
                    return
            filename = "loomy_sessions_%s.txt" % time.strftime("%Y%m%d_%H%M")
            raw = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/api/state"):
            cfg_view = {k: self.cfg.get(k, DEFAULT_CFG.get(k)) for k in DEFAULT_CFG}
            cfg_view["password"] = "***" if cfg_view.get("password") else ""
            reg = self.reg
            self._send(200, {
                "config": cfg_view, "balance": self.cfg.get("_balance", ""),
                "running": bool(reg and not reg.stop_flag.is_set() and reg.done < self.total),
                "ok": getattr(reg, "ok", 0), "fail": getattr(reg, "fail", 0),
                "done": getattr(reg, "done", 0), "total": self.total,
                "current": getattr(reg, "current", "") if reg else "",
                "results": self._merged_results(), "logs": self.logs[-200:],
                "save_file": os.path.basename(saved_path()) if os.path.exists(saved_path()) else "",
                "saved_total": saved_count(),
                "saved_recent": saved_recent(30),
            })
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if self.path.startswith("/api/save"):
            for key in list(DEFAULT_CFG):
                if key not in payload:
                    continue
                if key == "password" and not str(payload.get(key) or "").strip():
                    continue          # 留空 = 不改已保存的密码
                self.cfg[key] = payload[key]
            save_cfg(self.cfg)
            self._send(200, {"ok": True})
            return
        if self.path.startswith("/api/balance"):
            try:
                self.cfg["_balance"] = SmsPlatform(self.cfg).balance()
                self._send(200, {"ok": True, "balance": self.cfg["_balance"]})
            except Exception as exc:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(exc)[:160]})
            return
        if self.path.startswith("/api/start"):
            for key in list(DEFAULT_CFG):
                if key not in payload:
                    continue
                if key == "password" and not str(payload.get(key) or "").strip():
                    continue          # 留空 = 不改已保存的密码
                self.cfg[key] = payload[key]
            save_cfg(self.cfg)
            handler = self

            def logger(msg: str) -> None:
                handler.logs.append("%s %s" % (time.strftime("[%H:%M:%S]"), msg))
                del handler.logs[:-400]

            self.__class__.reg = Registrar(self.cfg, logger)
            self.__class__.total = int(self.cfg.get("count") or 1)
            threading.Thread(target=self.__class__.reg.run,
                             args=(self.__class__.total, int(self.cfg.get("concurrency") or 5)),
                             daemon=True).start()
            self._send(200, {"ok": True, "total": self.__class__.total})
            return
        if self.path.startswith("/api/stop"):
            if self.__class__.reg:
                self.__class__.reg.stop()
            self._send(200, {"ok": True})
            return
        self._send(404, {"detail": "not found"})


def run_gui(port: int = 8899) -> int:
    import webbrowser
    from http.server import ThreadingHTTPServer

    cfg = load_cfg()
    _Handler.cfg = cfg
    _Handler.logs = ["%s 启动本地界面；账号密码只保存在 %s" % (time.strftime("[%H:%M:%S]"), CONFIG_FILE)]
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = "http://127.0.0.1:%d/" % port
    print("本地注册器已启动：%s   （Ctrl+C 退出）" % url, flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已退出", flush=True)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--cli" in args:
        idx = args.index("--cli")
        count = int(args[idx + 1]) if len(args) > idx + 1 else 1
        out_path = args[args.index("--out") + 1] if "--out" in args and len(args) > args.index("--out") + 1 else ""
        return run_cli(count, out_path)
    port = 8899
    if "--port" in args:
        try:
            port = int(args[args.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    return run_gui(port)


if __name__ == "__main__":
    sys.exit(main())
