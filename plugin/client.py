import base64
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

from release import VERSION

ROOT = Path(
    os.environ.get(
        "TASK_CENTER_STATE", Path.home() / "Library/Application Support/LAN Task Center"
    )
)
# Keep this address stable across releases and already-open desktop sessions.
URI = "ui://task-center/panel.html"
MIME = "text/html;profile=mcp-app"
META = {
    "ui": {"resourceUri": URI},
    "openai/ui": {"entrypoints": [{"type": "global"}]},
}
LIMIT = 16 * 1024 * 1024
STRING = {"type": "string"}
FILES = {"type": "array", "items": STRING, "maxItems": 8}
ACTIONS = {
    "state": ("读取我的任务", {}),
    "sync_state": ("仅拉取已变化的任务列表", {"revision": STRING}),
    "check_updates": ("立即检查协作任务更新", {}),
    "set_auto_update": ("设置自动安装协作任务更新", {"enabled": {"type": "boolean"}}),
    "devices": ("查看已授权的发布设备", {}),
    "apply_update": (
        "安装协作任务更新",
        {"source": {"enum": ["lan", "github"]}, "automatic": {"type": "boolean"}},
    ),
    "discover_lan": ("搜索同一 Wi-Fi 上的接收方", {}),
    "request_lan": ("向接收方请求连接", {"endpoint": STRING, "pin": STRING}),
    "decide_connection": (
        "同意或拒绝同一 Wi-Fi 上的设备申请",
        {"device": STRING, "allow": {"type": "boolean"}},
    ),
    "device_mode": (
        "设置这台设备的接单方式",
        {"device": STRING, "mode": {"enum": ["inherit", "manual", "auto"]}},
    ),
    "revoke_device": ("撤销一台设备的发布和查看权限", {"device": STRING}),
    "publish": ("发布任务，可附上已选择的文件", {"text": STRING, "files": FILES}),
    "chats": ("搜索本机可派送的 Codex 对话", {"query": STRING}),
    "set_mode": ("设置新任务的接单方式", {"mode": {"enum": ["manual", "auto"]}}),
    "accept": (
        "将编辑后的请求派送到所选对话",
        {
            "id": STRING,
            "thread": STRING,
            "prompt": STRING,
            "include": {"type": "array", "items": {"type": "integer"}},
            "files": FILES,
        },
    ),
    "open_thread": ("在 Codex 打开这项任务的派送对话", {"id": STRING}),
    "withdraw": ("撤回尚在目标对话队列中的派送", {"id": STRING}),
    "decline": ("拒绝本次任务", {"id": STRING}),
    "deliver": (
        "将确认的结果和文件发回本任务",
        {"id": STRING, "text": STRING, "files": FILES},
    ),
    "ask": ("向发布者询问完成这项任务所需的信息", {"id": STRING, "text": STRING}),
    "reply": (
        "向本任务的接收方补充说明或附件",
        {"id": STRING, "text": STRING, "files": FILES},
    ),
    "pick_files": ("从这台 Mac 选择附件", {}),
    "stage_upload": ("添加拖入或粘贴的附件", {"name": STRING, "data": STRING}),
    "discard_file": ("移除尚未发送的附件", {"key": STRING}),
    "save_file": (
        "将任务附件另存到这台 Mac",
        {"id": STRING, "slot": {"type": "integer"}},
    ),
}


def native_file(script, *args):
    import subprocess

    p = subprocess.run(
        ["/usr/bin/osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode and "(-128)" not in p.stderr:
        raise ValueError("无法打开文件选择窗口")
    return p.stdout.rstrip("\n")


def local_action(action, args):
    import shutil
    import subprocess

    if action in ("check_updates", "set_auto_update"):
        from release import update_status, write_json

        if action == "set_auto_update":
            enabled = args.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("请选择是否自动安装更新")
            write_json(ROOT / "update-preferences.json", {"autoInstall": enabled})
        return update_status(ROOT, force=action == "check_updates")
    if action == "apply_update":
        from release import apply_update, auto_enabled, github_package, receiver_busy

        source = args.get("source")
        if source not in ("lan", "github"):
            raise ValueError("请选择更新来源")
        if source == "lan" and (ROOT / "host.json").is_file():
            raise ValueError("接收端请通过 GitHub 或最新安装包更新")
        if args.get("automatic") is True and not auto_enabled(ROOT):
            raise ValueError("自动安装更新未开启")
        if receiver_busy(ROOT):
            return {"waiting": True}
        update = (
            github_package(ROOT) if source == "github" else api("update_package", {})
        )
        return apply_update(ROOT, update)
    if action == "discover_lan":
        from lan import discover

        return discover()
    if action == "request_lan":
        return request_lan(args)
    if action == "sync_state":
        from release import update_status

        updates = update_status(ROOT)
        if not (ROOT / "connection.json").is_file():
            state = {"connection": "unpaired"}
        else:
            try:
                state = api(action, args)
            except ValueError as exc:
                state = {"syncError": str(exc)}
        state["updates"] = updates
        if updates.get("version"):
            state["githubVersion"] = updates["version"]
        return state
    stage = ROOT / "selections"
    if action == "discard_file":
        key = args.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("无效的附件")
        shutil.rmtree(stage / key, ignore_errors=True)
        return {"removed": True}
    if action == "pick_files":
        script = """set selectedFiles to choose file with prompt "添加任务附件" with multiple selections allowed
set paths to {}
repeat with f in selectedFiles
    set end of paths to POSIX path of f
end repeat
set AppleScript's text item delimiters to ASCII character 0
return paths as text"""
        picked = native_file(script)
        if not picked:
            return {"files": []}
        paths = [Path(name) for name in picked.split(chr(0))]
        if len(paths) > 8 or sum(p.stat().st_size for p in paths) > LIMIT:
            raise ValueError("一次最多 8 个文件，合计 16 MiB")
        result = []
        for path in paths:
            key = secrets.token_hex(16)
            folder = stage / key
            folder.mkdir(parents=True, mode=0o700)
            shutil.copyfile(path, folder / path.name)
            result.append({"key": key, "name": path.name, "size": path.stat().st_size})
        return {"files": result}
    if action == "stage_upload":
        name = args.get("name")
        encoded = args.get("data")
        if (
            not isinstance(name, str)
            or not name
            or name in (".", "..")
            or Path(name).name != name
            or any(ord(c) < 32 for c in name)
            or len(name.encode()) > 255
        ):
            raise ValueError("文件名称无效")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((LIMIT + 2) // 3):
            raise ValueError("文件超过 16 MiB")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > LIMIT:
            raise ValueError("文件超过 16 MiB")
        key = secrets.token_hex(16)
        folder = stage / key
        folder.mkdir(parents=True, mode=0o700)
        (folder / name).write_bytes(content)
        return {"files": [{"key": key, "name": name, "size": len(content)}]}
    if action == "open_thread":
        target = api("destination", args)["thread"]
        subprocess.run(["/usr/bin/open", "codex://threads/" + target], check=True)
        return {"opened": True}
    if action == "save_file":
        item = api("download", args)
        script = """on run argv
return POSIX path of (choose file name with prompt "保存任务附件" default name (item 1 of argv))
end run"""
        dest = native_file(script, item["name"])
        if dest:
            Path(dest).write_bytes(base64.b64decode(item["data"]))
        return {"saved": bool(dest)}
    if action not in ("publish", "accept", "deliver", "reply"):
        return api(action, args)
    keys = args.get("files", [])
    if (
        not isinstance(keys, list)
        or len(keys) > 8
        or not all(
            isinstance(k, str) and re.fullmatch(r"[0-9a-f]{32}", k) for k in keys
        )
    ):
        raise ValueError("请选择有效的附件")
    paths = []
    for key in keys:
        found = list((stage / key).glob("*"))
        if len(found) != 1:
            raise ValueError("请重新选择附件")
        paths.append(found[0])
    if sum(p.stat().st_size for p in paths) > LIMIT:
        raise ValueError("附件合计最多 16 MiB")
    files = [
        {"name": p.name, "data": base64.b64encode(p.read_bytes()).decode()}
        for p in paths
    ]
    data = api(action, {**args, "files": files})
    for key in set(keys):
        shutil.rmtree(stage / key, ignore_errors=True)
    return data


def request_lan(args):
    import http.client
    import ssl
    import subprocess
    import tempfile

    from lan import PIN, verified_authority

    if (ROOT / "host.json").is_file():
        raise ValueError("当前协作任务已安装为接收端")
    endpoint, pin = args.get("endpoint"), args.get("pin")
    if (
        not isinstance(endpoint, str)
        or not re.fullmatch(
            r"[a-zA-Z0-9-]{1,63}\.local:[0-9]{1,5}", endpoint, re.IGNORECASE
        )
        or not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535
        or not isinstance(pin, str)
        or not re.fullmatch(PIN, pin)
    ):
        raise ValueError("请选择刚搜索到的接收端")
    target = urlsplit("//" + endpoint)
    name = (
        subprocess.run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()[:64]
        or "这台 Mac"
    )
    token = secrets.token_urlsafe(32)
    ca = verified_authority(endpoint, pin)
    conn = http.client.HTTPSConnection(
        target.hostname,
        target.port,
        timeout=4,
        context=ssl.create_default_context(cadata=ca),
    )
    try:
        conn.request(
            "POST", "/pair/request", json.dumps({"name": name, "token": token}).encode()
        )
        response = conn.getresponse()
        data = json.load(response)
        if response.status != 200:
            raise ValueError(data.get("error", "连接申请失败"))
    finally:
        conn.close()
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=ROOT, delete=False) as saved:
        json.dump({"url": "https://" + endpoint, "ca": ca, "token": token}, saved)
    Path(saved.name).replace(ROOT / "connection.json")
    return {"connection": "pending"}


def api(action, args):
    import http.client
    import ssl

    saved_connection = ROOT / "connection.json"
    if not saved_connection.is_file():
        raise ValueError("请先在协作任务连接同一 Wi-Fi 上的接收方。")
    config = json.loads(saved_connection.read_text())
    target = urlsplit(config["url"])
    context = ssl.create_default_context(cadata=config["ca"])
    conn = http.client.HTTPSConnection(
        target.hostname, target.port, timeout=3, context=context
    )
    try:
        # The MCP bridge also handles other desktop requests. A sleeping Mac
        # must not hold that connection for the full file transfer timeout.
        conn.connect()
    except ssl.SSLCertVerificationError as exc:
        conn.close()
        raise ValueError("接收方的证书或地址已变化，请重新搜索接收方。") from exc
    except OSError as exc:
        conn.close()
        raise ValueError(
            "无法连接接收端，请确认接收方 Mac 已唤醒、接单服务已启动，以及两台 Mac 是否在同一个可互访的 Wi-Fi。"
        ) from exc
    try:
        conn.sock.settimeout(5 if action in ("state", "sync_state") else 60)
        conn.request(
            "POST",
            "/api/" + action,
            json.dumps(args).encode(),
            {
                "Authorization": "Bearer " + config["token"],
                "Content-Type": "application/json",
            },
        )
        res = conn.getresponse()
        data = (
            json.loads(res.read(3 * 1024 * 1024))
            if action == "update_package"
            else json.load(res)
        )
        if res.status == 409 and action == "sync_state":
            return data
        if res.status == 403 and action in ("state", "sync_state"):
            raise ValueError("连接申请未获批准或授权已失效，请重新连接。")
        if res.status != 200:
            raise ValueError(data.get("error", "操作失败"))
        return data
    except (OSError, http.client.HTTPException) as exc:
        if action in (
            "state",
            "sync_state",
            "devices",
            "chats",
            "destination",
            "download",
            "update_package",
        ):
            raise ValueError("接收端暂时没有响应，请稍后重试。") from exc
        raise ValueError(
            "连接在传输中中断，请先刷新任务记录，核对这次提交是否送达。"
        ) from exc
    finally:
        conn.close()


def entry_icons():
    image = (Path(__file__).parent / "assets" / "collaboration.png").read_bytes()
    return [
        {
            "src": "data:image/png;base64," + base64.b64encode(image).decode(),
            "mimeType": "image/png",
            "sizes": ["96x96"],
        }
    ]


def rpc(method, params):
    if method == "initialize":
        return {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}, "resources": {}},
            "serverInfo": {
                "name": "task-center",
                "title": "协作任务",
                "version": VERSION,
                "icons": entry_icons(),
            },
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        tools = [
            {
                "name": "open_center",
                "title": "协作任务",
                "description": "打开协作任务",
                "icons": entry_icons(),
                "inputSchema": {"type": "object", "properties": {}},
                "_meta": META,
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            }
        ]
        for key, (desc, fields) in ACTIONS.items():
            schema = {
                "type": "object",
                "properties": fields,
                "required": [k for k in fields if k not in ("files", "automatic")],
                "additionalProperties": False,
            }
            tools.append(
                {
                    "name": key,
                    "description": desc,
                    "inputSchema": schema,
                    "annotations": {
                        "readOnlyHint": key
                        in ("state", "sync_state", "devices", "chats"),
                        "openWorldHint": False,
                    },
                    "_meta": {
                        "ui": {
                            "visibility": ["app", "model"]
                            if key in ("state", "publish", "reply")
                            else ["app"]
                        }
                    },
                }
            )
        return {"tools": tools}
    if method == "resources/list":
        return {"resources": [{"uri": URI, "name": "协作任务", "mimeType": MIME}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    # The desktop may retain an earlier entrypoint until its catalog refreshes.
    if method == "resources/read" and params["uri"] in (
        URI,
        "ui://task-center/panel-v2.html",
        "ui://task-center/inbox-v3.html",
    ):
        return {
            "contents": [
                {
                    "uri": params["uri"],
                    "mimeType": MIME,
                    "text": (Path(__file__).parent / "panel.html").read_text(),
                }
            ]
        }
    if method == "tools/call":
        action = params["name"]
        if action == "open_center":
            return {"content": [{"type": "text", "text": "协作任务"}], "_meta": META}
        if action not in ACTIONS:
            raise KeyError(action)
        try:
            data = local_action(action, params.get("arguments", {}))
        except (ValueError, OSError) as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "任务状态已读取"
                        if action == "sync_state"
                        else json.dumps(data, ensure_ascii=False)
                    ),
                }
            ],
            "structuredContent": data,
        }
    raise KeyError(method)


def mcp():
    # Codex can keep the stdio connection of an open chat across a plugin update.
    source = Path(__file__)
    revision = source.stat().st_mtime_ns
    dispatch = rpc
    for line in sys.stdin:
        req = json.loads(line)
        if "id" not in req:
            continue
        result = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            current = source.stat().st_mtime_ns
            if current != revision:
                import runpy

                sys.modules.pop("release", None)

                dispatch = runpy.run_path(str(source))["rpc"]
                revision = current
                if req["method"] != "initialize":
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/tools/list_changed",
                            }
                        ),
                        flush=True,
                    )
            result["result"] = dispatch(req["method"], req.get("params", {}))
        except (KeyError, TypeError, OSError, ValueError) as exc:
            result["error"] = {
                "code": -32602
                if req["method"] in ("tools/call", "resources/read")
                else -32601,
                "message": str(exc),
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)


def browser():
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = secrets.token_urlsafe(32)

    class Panel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/":
                return self.send_error(404)
            page = (Path(__file__).parent / "panel.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if (
                self.headers.get("Authorization") != "Bearer " + token
                or self.headers.get("Origin") != origin
            ):
                return self.send_error(403)
            action = self.path.removeprefix("/api/")
            if action not in ACTIONS:
                return self.send_error(404)
            try:
                data = local_action(
                    action,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
                self.send_response(200)
            except (ValueError, OSError) as exc:
                data = {"error": str(exc)}
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Panel)
    origin = "http://127.0.0.1:" + str(server.server_port)
    webbrowser.open(origin + "/#" + token)
    server.serve_forever()


if __name__ == "__main__":
    mcp() if sys.argv[1:] == ["mcp"] else browser()
