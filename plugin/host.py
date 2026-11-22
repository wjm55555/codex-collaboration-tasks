import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import selectors
import sqlite3
import ssl
import subprocess
import threading
import time
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from client import LIMIT, ROOT, peers
from lan import PIN, advertise, confirm_link, fingerprint, lan_endpoint
from relay import receiver_socket
from release import available, package
from worker import chats, execute, queue_state, queued_in_thread, session

DB = ROOT / "tasks.sqlite"
PENDING = queue.Queue()
LAN_ADDRESS = ""


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def files(db, tid, group, values):
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("一次最多添加 8 个文件")
    slots = []
    total = 0
    slot = db.execute(
        "SELECT coalesce(max(slot),0) FROM attachments WHERE task=?", (tid,)
    ).fetchone()[0]
    for item in values:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("data"), str)
        ):
            raise TypeError("附件信息无效")
        name = item["name"]
        if (
            not name
            or name in (".", "..")
            or Path(name).name != name
            or any(ord(ch) < 32 for ch in name)
            or len(name.encode()) > 255
        ):
            raise ValueError("附件名称无效")
        body = base64.b64decode(item["data"], validate=True)
        total += len(body)
        if total > LIMIT:
            raise ValueError("附件合计最多 16 MiB")
        slot += 1
        db.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?)", (tid, slot, group, name, body)
        )
        slots.append(slot)
    return slots


def process(tid):
    with closing(connect()) as db:
        with db:
            if not db.execute(
                "UPDATE tasks SET status='dispatching' WHERE id=? AND status='queued'",
                (tid,),
            ).rowcount:
                return
        task = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        try:
            paths = []
            for slot in json.loads(task["selection"]):
                a = db.execute(
                    "SELECT name,body FROM attachments WHERE task=? AND slot=?",
                    (tid, slot),
                ).fetchone()
                path = ROOT / "task-files" / tid / str(slot) / a["name"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(a["body"])
                paths.append(path)

            def started(thread_id, turn_id):
                with db:
                    db.execute(
                        "UPDATE tasks SET status='running',execution=?,turn=? WHERE id=?",
                        (thread_id, turn_id, tid),
                    )

            status, answer = execute(
                task["destination"], task["prompt"], paths, started
            )
            with db:
                if status == "desktop_queued":
                    packed = json.dumps(answer)
                    db.execute(
                        "UPDATE tasks SET status=?,submission=? WHERE id=?",
                        (status, packed, tid),
                    )
                elif status == "review":
                    db.execute(
                        "UPDATE tasks SET status='review',draft=? WHERE id=?",
                        (answer, tid),
                    )
                else:
                    message = {"role": "Codex", "text": answer}
                    if status == "waiting":
                        message["kind"] = "question"
                    messages = json.loads(task["messages"]) + [message]
                    db.execute(
                        "UPDATE tasks SET status=?,messages=? WHERE id=?",
                        (status, json.dumps(messages), tid),
                    )
            if status == "desktop_queued":
                threading.Thread(
                    target=observe, args=(tid, task["destination"], packed), daemon=True
                ).start()
        except (
            ValueError,
            RuntimeError,
            OSError,
            subprocess.SubprocessError,
            KeyError,
            TypeError,
        ) as exc:
            with db:
                db.execute(
                    "UPDATE tasks SET status='failed',error=? WHERE id=?",
                    (str(exc)[:600], tid),
                )


def observe(tid, target, packed):
    # The shared receiver is free to accept another job while the original writer works.
    with closing(connect()) as db:
        receipt = json.loads(packed)
        missing_since = None
        try:
            with session("/", timeout=None) as (call, _, __):
                while True:
                    state = db.execute(
                        "SELECT status,turn FROM tasks WHERE id=? AND submission=?",
                        (tid, packed),
                    ).fetchone()
                    if not state or state[0] not in (
                        "desktop_queued",
                        "desktop_running",
                    ):
                        return
                    current = queue_state(call, target, receipt, state["turn"])
                    if current:
                        status, turn, out = current
                        if status != state["status"] or turn != state["turn"]:
                            with db:
                                db.execute(
                                    """UPDATE tasks SET status=?,turn=?,draft=?,error=? WHERE id=? AND submission=?
                                  AND status IN ('desktop_queued','desktop_running')""",
                                    (
                                        status,
                                        turn,
                                        out if status == "review" else "",
                                        out if status == "failed" else "",
                                        tid,
                                        packed,
                                    ),
                                )
                        if status in ("review", "failed"):
                            return
                    elif queued_in_thread(call, target, receipt["queue"]):
                        missing_since = None
                        # If the desktop closed after enqueueing, loading the thread here
                        # lets its own writer drain the existing item. Never enqueue it twice.
                        try:
                            call(
                                13,
                                "thread/resume",
                                {"threadId": target, "excludeTurns": True},
                            )
                        except RuntimeError as exc:
                            if "active writer" not in str(
                                exc
                            ) and "live local writer" not in str(exc):
                                raise
                    else:
                        # During dispatch, removal can be visible just before the new turn is persisted.
                        missing_since = missing_since or time.monotonic()
                        if time.monotonic() - missing_since >= 15:
                            raise RuntimeError(
                                "原对话的待发消息已移除，未找到对应执行记录。请打开原对话核对后再派送。"
                            )
                    time.sleep(3)
        except (
            ValueError,
            RuntimeError,
            OSError,
            subprocess.SubprocessError,
            KeyError,
            TypeError,
        ) as exc:
            with db:
                db.execute(
                    """UPDATE tasks SET status='failed',error=? WHERE id=? AND submission=?
                     AND status IN ('desktop_queued','desktop_running')""",
                    (str(exc)[:600], tid, packed),
                )


def dispatch(db, action, args, actor, sender):
    owner = actor == "owner"
    if (
        action
        not in ("state", "sync_state", "publish", "reply", "download", "update_package")
        and not owner
    ):
        raise PermissionError()
    if not isinstance(args, dict):
        raise TypeError("请求格式无效")
    if action == "update_package":
        return package(ROOT)
    if action == "devices":
        return {
            "devices": [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,mode FROM devices WHERE revoked=0 ORDER BY name,id"
                )
            ],
            "requests": [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,callback IS NOT NULL AS mutual FROM lan_requests WHERE created>? ORDER BY created,id",
                    (int(time.time()) - 3600,),
                )
            ],
        }
    if action == "decide_connection":
        device = args.get("device")
        allow = args.get("allow")
        if not isinstance(device, str) or type(allow) is not bool:
            raise ValueError("请选择连接申请")
        request = db.execute(
            "SELECT id,name,digest,callback FROM lan_requests WHERE id=? AND created>?",
            (device, int(time.time()) - 3600),
        ).fetchone()
        if request is None:
            raise ValueError("这条申请已处理或已过期")
        if allow:
            digest = request["digest"]
            existing = db.execute(
                "SELECT revoked FROM devices WHERE digest=?", (digest,)
            ).fetchone()
            if existing and existing[0]:
                raise ValueError("这条申请的授权已撤销，请让对方重新申请")
            if request["callback"]:
                callback = json.loads(request["callback"])
                pin = callback["pin"]
                previous = peers(ROOT).get(pin)
                config = confirm_link(callback, previous)
                config["name"] = previous["name"] if previous else request["name"]
                db.execute(
                    "INSERT INTO lan_links(pin,config) VALUES(?,?) "
                    "ON CONFLICT(pin) DO UPDATE SET config=excluded.config",
                    (pin, json.dumps(config)),
                )
            if not existing:
                db.execute(
                    "INSERT INTO devices(id,name,digest,mode) VALUES(?,?,?,'manual')",
                    (request["id"], request["name"], digest),
                )
        db.execute("DELETE FROM lan_requests WHERE id=?", (device,))
        return {"ok": True}
    if action in ("device_mode", "revoke_device"):
        device = args.get("device")
        if not isinstance(device, str):
            raise ValueError("请选择设备")
        if action == "device_mode":
            mode = args.get("mode")
            if mode not in ("manual", "auto", "inherit"):
                raise ValueError("请选择接单方式")
            changed = db.execute(
                "UPDATE devices SET mode=? WHERE id=? AND revoked=0", (mode, device)
            )
        else:
            changed = db.execute(
                "UPDATE devices SET revoked=1 WHERE id=? AND revoked=0", (device,)
            )
        if not changed.rowcount:
            raise ValueError("这台设备的授权已失效")
        return {"ok": True}
    if action == "pair_code":
        name = args.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError("请填写 1 至 64 字的设备名称")
        cfg = json.loads((ROOT / "host.json").read_text())
        route = args.get("route")
        if route not in ("lan", "relay") or route == "relay" and not cfg.get("relay"):
            raise ValueError("请先配置单位 VPN 中继")
        endpoint = (
            cfg["relay"]["endpoint"]
            if route == "relay"
            else f"{cfg['dns']}:{cfg['port']}"
        )
        secret = secrets.token_urlsafe(20)
        pin = fingerprint((ROOT / "cert.pem").read_text())
        expires = int(time.time()) + 600
        db.execute("DELETE FROM pair_codes WHERE expires<=?", (int(time.time()),))
        db.execute(
            "INSERT INTO pair_codes VALUES (?,?,?)",
            (hashlib.sha256(secret.encode()).hexdigest(), name.strip(), expires),
        )
        return {"code": f"TC1|{endpoint}|{pin}|{secret}", "expires": expires}
    if action in ("state", "sync_state"):
        mode, installation = db.execute("SELECT mode,scope FROM settings").fetchone()
        scope = hashlib.sha256(f"{installation}/{actor}".encode()).hexdigest()[:32]
        seq = db.execute(
            "SELECT coalesce(sum(seq),0) FROM revisions"
            + ("" if owner else " WHERE actor=?"),
            () if owner else (actor,),
        ).fetchone()[0]
        requests = (
            db.execute(
                "SELECT id FROM lan_requests WHERE created>? ORDER BY id",
                (int(time.time()) - 3600,),
            ).fetchall()
            if owner
            else []
        )
        upgrade = available(ROOT) if not owner else None
        revision = f"{scope}/{seq}/{upgrade or '-'}"
        if owner:
            revision += f"/{mode}/{LAN_ADDRESS}/" + ",".join(row[0] for row in requests)
        if action == "sync_state" and args.get("revision") == revision:
            return {"unchanged": True}
        rows = db.execute(
            "SELECT * FROM tasks "
            + ("" if owner else "WHERE actor=?")
            + " ORDER BY created DESC",
            () if owner else (actor,),
        )
        tasks = []
        for t in rows:
            item = {k: t[k] for k in ("id", "sender", "status", "created")}
            if item["status"] == "dispatching":
                item["status"] = "queued"
            if item["status"] == "desktop_running":
                item["status"] = "running"
            if item["status"] == "desktop_queued" and not owner:
                item["status"] = "queued"
            item["messages"] = json.loads(t["messages"])
            item["canReply"] = t["actor"] == actor
            item["files"] = [
                dict(r)
                for r in db.execute(
                    "SELECT slot,name,length(body) AS size,kind FROM attachments WHERE task=?"
                    + ("" if owner else " AND kind!='private'")
                    + " ORDER BY slot",
                    (t["id"],),
                )
            ]
            if owner:
                item.update(
                    {
                        k: t[k]
                        for k in ("destination", "prompt", "draft", "error", "turn")
                    }
                )
                item["selection"] = json.loads(t["selection"])
            tasks.append(item)
        result = {
            "owner": owner,
            "tasks": tasks,
            "draftScope": scope,
            "revision": revision,
        }
        if upgrade:
            result["updateVersion"] = upgrade
        if owner:
            result.update(
                mode=mode, lanAddress=LAN_ADDRESS, pendingDevices=len(requests)
            )
        return result
    if action == "chats":
        query = args.get("query", "")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("搜索词过长")
        return {"chats": chats(query)}
    if action == "set_mode":
        mode = args.get("mode")
        if mode not in ("manual", "auto"):
            raise ValueError("请选择手动或自动")
        db.execute("UPDATE settings SET mode=?", (mode,))
        return {"mode": mode}
    if action not in (
        "publish",
        "accept",
        "decline",
        "ask",
        "deliver",
        "withdraw",
        "reply",
        "download",
        "destination",
    ):
        raise ValueError("未知操作")
    if action == "publish":
        tid = secrets.token_hex(8)
        task = None
    else:
        tid = args.get("id")
        if not isinstance(tid, str):
            raise ValueError("缺少任务编号")
        task = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not task or (not owner and task["actor"] != actor):
            raise PermissionError()
    if action == "destination":
        if not task["destination"]:
            raise ValueError("此任务没有派送到本机已有对话")
        return {"thread": task["destination"]}
    if action == "download":
        slot = args.get("slot")
        if type(slot) != int:
            raise ValueError("附件编号无效")
        a = db.execute(
            "SELECT kind,name,body FROM attachments WHERE task=? AND slot=?",
            (tid, slot),
        ).fetchone()
        if not a or (not owner and a["kind"] == "private"):
            raise PermissionError()
        return {"name": a["name"], "data": base64.b64encode(a["body"]).decode()}
    text = args.get("prompt" if action == "accept" else "text", "")
    if not isinstance(text, str) or len(text) > 48000:
        raise ValueError("文本最多 48000 字")
    text = text.strip()
    if action == "publish":
        selected = files(db, tid, "request", args.get("files", []))
        if not text and not selected:
            raise ValueError("请填写任务或添加附件")
        text = text or "请查看并处理附件。"
        mode = db.execute(
            "SELECT coalesce((SELECT nullif(mode,'inherit') FROM devices WHERE id=?),mode) FROM settings",
            (actor,),
        ).fetchone()[0]
        status = "queued" if mode == "auto" else "pending"
        db.execute(
            """INSERT INTO tasks(id,actor,sender,status,messages,created,prompt,selection)
                   VALUES(?,?,?,?,?,?,?,?)""",
            (
                tid,
                actor,
                sender,
                status,
                json.dumps([{"role": "发布者", "text": text, "files": selected}]),
                time.time(),
                text,
                json.dumps(selected),
            ),
        )
    elif action == "accept":
        if task["status"] not in ("pending", "failed") or not text:
            raise ValueError("只可接收待接单或失败任务，提示词不能为空")
        target = args.get("thread")
        try:
            target = str(uuid.UUID(target))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("请选择一个具体的 Codex 对话") from None
        selected = args.get("include", [])
        allowed = {
            r[0]
            for r in db.execute(
                "SELECT slot FROM attachments WHERE task=? AND kind IN ('request','reply','private')",
                (tid,),
            )
        }
        if not isinstance(selected, list) or not all(
            type(s) == int and s in allowed for s in selected
        ):
            raise ValueError("请选择本任务的附件")
        selected = list(dict.fromkeys(selected)) + files(
            db, tid, "private", args.get("files", [])
        )
        db.execute(
            "UPDATE tasks SET status='queued',destination=?,prompt=?,selection=?,turn=NULL,execution=NULL,submission=NULL,draft='',error='' WHERE id=?",
            (target, text, json.dumps(selected), tid),
        )
        status = "queued"
    elif action == "withdraw":
        if task["status"] != "desktop_queued":
            raise ValueError("当前任务已离开目标对话的待发队列")
        receipt = json.loads(task["submission"])
        with session("/", timeout=20) as (call, _, __):
            deleted = call(
                2,
                "thread/queue/delete",
                {
                    "threadId": task["destination"],
                    "queuedSubmissionId": receipt["queue"],
                },
            )["deleted"]
        if not deleted:
            raise ValueError("目标对话已经取走这条消息，请查看最新状态")
        status = "pending"
        db.execute(
            "UPDATE tasks SET status=?,submission=NULL,execution=NULL,turn=NULL WHERE id=?",
            (status, tid),
        )
    elif action == "ask":
        if task["status"] not in ("pending", "failed", "review"):
            raise ValueError("当前任务无法请求补充")
        if not text:
            raise ValueError("请填写需要对方补充的问题")
        messages = json.loads(task["messages"]) + [
            {"role": "接收方", "text": text, "kind": "question"}
        ]
        status = "waiting"
        db.execute(
            "UPDATE tasks SET status=?,messages=? WHERE id=?",
            (status, json.dumps(messages), tid),
        )
    elif action == "decline":
        if task["status"] not in ("pending", "failed"):
            raise ValueError("当前状态无法拒绝")
        status = "declined"
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    elif action == "deliver":
        if task["status"] != "review":
            raise ValueError("当前没有待发回的结果")
        attached = files(db, tid, "result", args.get("files", []))
        if not text and not attached:
            raise ValueError("请填写结果或添加文件")
        messages = json.loads(task["messages"]) + [
            {"role": "接收方", "text": text or "见结果附件。", "files": attached}
        ]
        status = "done"
        db.execute(
            "UPDATE tasks SET status=?,messages=?,draft='' WHERE id=?",
            (status, json.dumps(messages), tid),
        )
    else:
        if task["actor"] != actor or task["status"] != "waiting":
            raise ValueError("只有发布者可以回复待补充的任务")
        attached = files(db, tid, "reply", args.get("files", []))
        if not text and not attached:
            raise ValueError("请填写补充说明或添加附件")
        text = text or "请查看本次补充的附件。"
        messages = json.loads(task["messages"])
        question = messages[-1]
        messages.append({"role": "发布者", "text": text, "files": attached})
        prompt = task["prompt"] or messages[0]["text"]
        prompt += "\n\n任务追问：" + question["text"] + "\n发布者补充：" + text
        mode = db.execute(
            "SELECT coalesce((SELECT nullif(mode,'inherit') FROM devices WHERE id=?),mode) FROM settings",
            (actor,),
        ).fetchone()[0]
        # Only the response to a question from this task's isolated automatic
        # turn can continue automatically. A private destination always needs a new acceptance.
        auto_question = (
            question.get("kind") == "question" and question["role"] == "Codex"
        )
        status = (
            "queued"
            if mode == "auto" and auto_question and not task["destination"]
            else "pending"
        )
        selected = json.loads(task["selection"]) + attached
        db.execute(
            "UPDATE tasks SET status=?,messages=?,prompt=?,selection=?,error='' WHERE id=?",
            (status, json.dumps(messages), prompt, json.dumps(selected), tid),
        )
    db.commit()
    if status == "queued":
        PENDING.put(tid)
    elif status == "pending" and action in ("publish", "reply"):
        subprocess.Popen(
            [
                "/usr/bin/osascript",
                "-e",
                'display notification "打开协作任务，选择对话并派送。" with title "收到新任务"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return {"id": tid, "status": status}


class Handler(BaseHTTPRequestHandler):
    timeout = 60

    def log_message(self, *args):
        pass

    def do_POST(self):
        code = 200
        try:
            length = int(self.headers.get("Content-Length", -1))
            if not 0 <= length <= 24 * 1024 * 1024:
                raise ValueError("请求过大或格式无效")
            path = self.path
            if path == "/pair/authority":
                data = {"ca": (ROOT / "cert.pem").read_text(), "mutualPairing": True}
            elif path in ("/pair/request", "/pair/mutual-request"):
                if getattr(self.server, "publisher_only", False) or length > 1024:
                    raise PermissionError()
                args = json.loads(self.rfile.read(length))
                if not isinstance(args, dict):
                    raise ValueError("设备申请格式无效")
                name, token = args.get("name"), args.get("token")
                if (
                    not isinstance(name, str)
                    or not 1 <= len(name.strip()) <= 64
                    or not name.strip().isprintable()
                    or not isinstance(token, str)
                    or not re.fullmatch(PIN, token)
                ):
                    raise ValueError("设备申请格式无效")
                callback = None
                if path == "/pair/mutual-request":
                    value = args.get("callback")
                    if (
                        not isinstance(value, dict)
                        or not lan_endpoint(value.get("endpoint"))
                        or not isinstance(value.get("pin"), str)
                        or not re.fullmatch(PIN, value["pin"])
                        or not isinstance(value.get("token"), str)
                        or not re.fullmatch(PIN, value["token"])
                        or value["pin"] == fingerprint((ROOT / "cert.pem").read_text())
                    ):
                        raise ValueError("双向连接申请格式无效")
                    callback = json.dumps(
                        {key: value[key] for key in ("endpoint", "pin", "token")}
                    )
                digest = hashlib.sha256(token.encode()).hexdigest()
                now = int(time.time())
                with closing(connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "DELETE FROM lan_requests WHERE created<=?", (now - 3600,)
                    )
                    if (
                        db.execute("SELECT count(*) FROM lan_requests").fetchone()[0]
                        >= 20
                    ):
                        raise ValueError("接收方有较多待处理申请，请稍后重试")
                    db.execute(
                        "INSERT INTO lan_requests(id,name,digest,created,callback) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(digest) DO UPDATE SET "
                        "callback=coalesce(excluded.callback,lan_requests.callback)",
                        (secrets.token_hex(12), name.strip(), digest, now, callback),
                    )
                data = {"connection": "pending"}
            elif path == "/pair/confirm":
                if getattr(self.server, "publisher_only", False) or length > 512:
                    raise PermissionError()
                args = json.loads(self.rfile.read(length))
                if not isinstance(args, dict):
                    raise ValueError("连接确认格式无效")
                token, previous = args.get("token"), args.get("previous", "")
                if (
                    not isinstance(token, str)
                    or not re.fullmatch(PIN, token)
                    or not isinstance(previous, str)
                    or previous
                    and not re.fullmatch(PIN, previous)
                ):
                    raise ValueError("连接确认格式无效")
                digest = hashlib.sha256(token.encode()).hexdigest()
                prior = hashlib.sha256(previous.encode()).hexdigest()
                with closing(connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    offer = db.execute(
                        "SELECT config,confirmed FROM lan_links WHERE offer=? AND expires>?",
                        (digest, int(time.time())),
                    ).fetchone()
                    if not offer:
                        raise PermissionError()
                    if offer["confirmed"]:
                        accepted = offer["confirmed"]
                        if (
                            accepted not in (digest, prior)
                            or not db.execute(
                                "SELECT 1 FROM devices WHERE digest=? AND revoked=0",
                                (accepted,),
                            ).fetchone()
                        ):
                            raise PermissionError()
                    else:
                        config = json.loads(offer["config"])
                        accepted = (
                            prior
                            if db.execute(
                                "SELECT 1 FROM devices WHERE digest=? AND revoked=0",
                                (prior,),
                            ).fetchone()
                            else digest
                        )
                        if accepted == digest:
                            db.execute(
                                "INSERT INTO devices(id,name,digest,mode) VALUES(?,?,?,'manual')",
                                (secrets.token_hex(12), config["name"], digest),
                            )
                        config["mutual"] = True
                        db.execute(
                            "UPDATE lan_links SET config=?,confirmed=? WHERE offer=?",
                            (json.dumps(config), accepted, digest),
                        )
                data = {"reuse": accepted != digest}
            elif path == "/pair/redeem":
                if length > 512:
                    raise ValueError("配对码格式无效")
                args = json.loads(self.rfile.read(length))
                secret = args.get("secret") if isinstance(args, dict) else None
                if not isinstance(secret, str):
                    raise ValueError("请输入配对码")
                digest = hashlib.sha256(secret.encode()).hexdigest()
                with closing(connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    code_row = db.execute(
                        "SELECT name FROM pair_codes WHERE digest=? AND expires>?",
                        (digest, int(time.time())),
                    ).fetchone()
                    if not code_row:
                        raise PermissionError()
                    db.execute("DELETE FROM pair_codes WHERE digest=?", (digest,))
                    credential = secrets.token_urlsafe(32)
                    db.execute(
                        "INSERT INTO devices(id,name,digest,mode) VALUES(?,?,?,'manual')",
                        (
                            secrets.token_hex(12),
                            code_row[0],
                            hashlib.sha256(credential.encode()).hexdigest(),
                        ),
                    )
                    data = {"token": credential}
            elif path.startswith("/api/"):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                action = path[5:]
                args = json.loads(self.rfile.read(length))
                with closing(connect()) as db, db:
                    if action not in (
                        "state",
                        "sync_state",
                        "devices",
                        "chats",
                        "download",
                        "update_package",
                    ):
                        db.execute("BEGIN IMMEDIATE")
                    else:
                        db.execute("BEGIN")
                    if (
                        self.server.server_address[0] == "127.0.0.1"
                        and not getattr(self.server, "publisher_only", False)
                        and self.client_address[0] == "127.0.0.1"
                        and hmac.compare_digest(token.encode(), self.server.owner_token)
                    ):
                        actor, sender = "owner", "本机"
                    else:
                        peer = db.execute(
                            "SELECT id,name FROM devices WHERE digest=? AND revoked=0",
                            (hashlib.sha256(token.encode()).hexdigest(),),
                        ).fetchone()
                        if not peer:
                            pending = (
                                action == "sync_state"
                                and db.execute(
                                    "SELECT 1 FROM lan_requests WHERE digest=? AND created>?",
                                    (
                                        hashlib.sha256(token.encode()).hexdigest(),
                                        int(time.time()) - 3600,
                                    ),
                                ).fetchone()
                            )
                            if not pending:
                                raise PermissionError()
                            code, data = 409, {"connection": "pending"}
                        else:
                            actor, sender = peer
                    if code == 200:
                        data = dispatch(db, action, args, actor, sender)
            else:
                raise ValueError("未知操作")
        except PermissionError:
            code, data = 403, {"error": "没有访问权限"}
        except (TypeError, ValueError, RuntimeError, OSError) as exc:
            code, data = 400, {"error": str(exc)[:350]}
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def init(db):
    db.executescript("""CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,actor TEXT,sender TEXT,status TEXT,messages TEXT,created REAL);
      CREATE TABLE IF NOT EXISTS settings(mode TEXT NOT NULL);
      INSERT INTO settings(mode) SELECT 'manual' WHERE NOT EXISTS(SELECT 1 FROM settings);
      CREATE TABLE IF NOT EXISTS attachments(task TEXT, slot INTEGER, kind TEXT, name TEXT, body BLOB, PRIMARY KEY(task,slot));
      CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,mode TEXT NOT NULL DEFAULT 'inherit',revoked INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS lan_requests(id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,created INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS lan_links(pin TEXT PRIMARY KEY,config TEXT NOT NULL,offer TEXT,expires INTEGER,confirmed TEXT);
      CREATE TABLE IF NOT EXISTS pair_codes(digest TEXT PRIMARY KEY,name TEXT NOT NULL,expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS revisions(actor TEXT PRIMARY KEY,seq INTEGER NOT NULL);
      CREATE TRIGGER IF NOT EXISTS task_insert AFTER INSERT ON tasks BEGIN
        INSERT INTO revisions VALUES(new.actor,1) ON CONFLICT(actor) DO UPDATE SET seq=seq+1; END;
      CREATE TRIGGER IF NOT EXISTS task_update AFTER UPDATE ON tasks BEGIN
        INSERT INTO revisions VALUES(new.actor,1) ON CONFLICT(actor) DO UPDATE SET seq=seq+1; END;
      DROP TABLE IF EXISTS routes;""")
    if "callback" not in {r[1] for r in db.execute("PRAGMA table_info(lan_requests)")}:
        db.execute("ALTER TABLE lan_requests ADD COLUMN callback TEXT")
    legacy = ROOT / "host.json"
    if legacy.is_file():
        for peer in json.loads(legacy.read_text()).get("peers", []):
            db.execute(
                "INSERT OR IGNORE INTO devices(id,name,digest) VALUES(?,?,?)",
                (
                    peer["id"],
                    peer["name"],
                    hashlib.sha256(peer["token"].encode()).hexdigest(),
                ),
            )
    if "scope" not in {r[1] for r in db.execute("PRAGMA table_info(settings)")}:
        db.execute("ALTER TABLE settings ADD COLUMN scope TEXT")
    db.execute("UPDATE settings SET scope=hex(randomblob(16)) WHERE scope IS NULL")
    columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    if "route" in columns:
        db.execute("ALTER TABLE tasks DROP COLUMN route")
    for key, default in (
        ("prompt", "''"),
        ("selection", "'[]'"),
        ("destination", "NULL"),
        ("draft", "''"),
        ("error", "''"),
        ("execution", "NULL"),
        ("turn", "NULL"),
        ("submission", "NULL"),
    ):
        if key not in columns:
            db.execute(
                "ALTER TABLE tasks ADD COLUMN " + key + " TEXT DEFAULT " + default
            )
    db.execute(
        "UPDATE tasks SET status='failed', error='接收服务曾中断，请先核对执行记录再派送' WHERE status IN ('running','dispatching')"
    )


def serve():
    os.umask(0o077)
    with closing(connect()) as db, db:
        init(db)
        for row in db.execute(
            "SELECT id FROM tasks WHERE status='queued' ORDER BY created"
        ):
            PENDING.put(row[0])

        tracked = db.execute(
            "SELECT id,destination,submission FROM tasks WHERE status IN ('desktop_queued','desktop_running')"
        ).fetchall()
    for row in tracked:
        threading.Thread(target=observe, args=tuple(row), daemon=True).start()

    def consume():
        while True:
            process(PENDING.get())

    threading.Thread(target=consume, daemon=True).start()
    cfg = json.loads((ROOT / "host.json").read_text())
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if (ROOT / "server.pem").is_file():
        tls.load_cert_chain(ROOT / "server.pem", ROOT / "server-key.pem")
    else:
        tls.load_cert_chain(ROOT / "cert.pem", ROOT / "key.pem")
    if cfg.get("relay"):
        threading.Thread(
            target=receive_remote, args=(cfg["relay"], tls), daemon=True
        ).start()
    serve_http(cfg, tls)


def receive_remote(config, tls):
    remote_server = SimpleNamespace(server_address=("relay", 0), publisher_only=True)

    def handle(conn):
        try:
            with conn, tls.wrap_socket(conn, server_side=True) as stream:
                stream.settimeout(60)
                Handler(stream, ("relay", 0), remote_server)
        except OSError:
            pass  # An offline publisher may abandon its TLS connection.

    while True:
        try:
            conn = receiver_socket(config)
            try:
                conn.settimeout(55)
                if conn.recv(1) == b"G":
                    threading.Thread(target=handle, args=(conn,), daemon=True).start()
                    continue
            except OSError:
                conn.close()
                raise
            conn.close()
        except OSError:
            time.sleep(3)


def serve_http(cfg, tls):
    global LAN_ADDRESS
    LAN_ADDRESS = ""
    wifi_server = None
    announcement = None

    def listener(addr):
        srv = ThreadingHTTPServer((addr, cfg["port"]), Handler)
        srv.owner_token = cfg.get("owner", "").encode()
        srv.socket = tls.wrap_socket(
            srv.socket, server_side=True, do_handshake_on_connect=False
        )
        return srv

    with listener("127.0.0.1") as local, selectors.DefaultSelector() as events:
        events.register(local, selectors.EVENT_READ)
        next_check = 0
        try:
            while True:
                if time.monotonic() >= next_check:
                    addr = cfg["ip"]
                    if cfg.get("wifi_device"):
                        address = subprocess.run(
                            ["/usr/sbin/ipconfig", "getifaddr", cfg["wifi_device"]],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                            check=False,
                        )
                        addr = address.stdout.strip() if address.returncode == 0 else ""
                    if addr != LAN_ADDRESS:
                        if wifi_server:
                            if announcement:
                                announcement.terminate()
                                announcement.wait()
                                announcement = None
                            events.unregister(wifi_server)
                            wifi_server.server_close()
                            wifi_server = None
                        LAN_ADDRESS = ""
                        if addr:
                            try:
                                wifi_server = listener(addr)
                            except OSError as exc:
                                print("Wi-Fi 监听尚未就绪：", exc, flush=True)
                            else:
                                events.register(wifi_server, selectors.EVENT_READ)
                                LAN_ADDRESS = addr
                    if wifi_server and (
                        announcement is None or announcement.poll() is not None
                    ):
                        announcement = advertise(cfg, (ROOT / "cert.pem").read_text())
                    next_check = time.monotonic() + 5
                for key, _ in events.select(max(0, next_check - time.monotonic())):
                    key.fileobj.handle_request()
        finally:
            if announcement:
                announcement.terminate()
                announcement.wait()
            if wifi_server:
                wifi_server.server_close()
            LAN_ADDRESS = ""


if __name__ == "__main__":
    serve()
