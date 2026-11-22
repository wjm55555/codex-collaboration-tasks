"""Deliver a turn to Codex and collect only the assistant message from that turn."""

import json
import os
import posixpath
import signal
import subprocess
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from pathlib import Path

CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


@contextmanager
def session(cwd, env=None, flags=(), timeout=600):
    p = subprocess.Popen(
        [CODEX, "app-server", *flags],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )

    def stop():
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)

    expired = False

    def expire():
        nonlocal expired
        expired = True
        stop()

    limit = 30 if timeout is None else timeout
    timer = threading.Timer(limit, expire)
    if timeout is not None:
        timer.start()

    def send(payload):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        p.stdin.flush()

    def call(number, method, params):
        nonlocal timer
        # A queue observer has no lifetime limit, only a deadline for each local RPC.
        if timeout is None:
            timer = threading.Timer(limit, expire)
            timer.start()
        send({"id": number, "method": method, "params": params})
        for line in p.stdout:
            event = json.loads(line)
            if event.get("id") == number and "method" not in event:
                if timeout is None:
                    timer.cancel()
                if "error" in event:
                    raise RuntimeError(event["error"]["message"][:450])
                return event["result"]
        raise RuntimeError("Codex 已退出或连接超时")

    try:
        call(
            1,
            "initialize",
            {
                "clientInfo": {"name": "lan-task-center", "version": "0.6.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        send({"method": "initialized"})
        yield call, send, p.stdout
    finally:
        timer.cancel()
        stop()
        p.wait()
        p.stdin.close()
        p.stdout.close()
        if expired:
            raise TimeoutError(
                f"等待 Codex 响应超过 {limit} 秒。请核对本轮记录后再重试。"
            )


def chats(query):
    with session("/", timeout=20) as (call, _, __):
        items = call(
            2,
            "thread/list",
            {
                "limit": 60,
                "useStateDbOnly": True,
                "archived": False,
                "sortKey": "updated_at",
                "searchTerm": query or None,
                "sourceKinds": [],
            },
        )["data"]
    return [
        {
            "id": t["id"],
            "name": t.get("name") or (t.get("preview") or "未命名对话")[:70],
            "project": Path(t["cwd"]).name,
        }
        for t in items
        if t.get("canAcceptDirectInput") is not False
    ]


def read_document(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        script = """ObjC.import('PDFKit'); function run(argv) {
          const d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
          return ObjC.unwrap(d.string) || ''; }"""
        return subprocess.check_output(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script, str(path)],
            text=True,
            timeout=30,
        ).strip()
    if suffix in (".docx", ".xlsx"):
        with zipfile.ZipFile(path) as archive:
            if sum(e.file_size for e in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError("文档展开后较大，请手动派送")
            ns = {
                "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            }
            if suffix == ".docx":
                root = ET.fromstring(archive.read("word/document.xml"))
                return "\n".join(
                    "".join(n.text or "" for n in para.findall(".//w:t", ns))
                    for para in root.findall(".//w:p", ns)
                )
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = [
                    "".join(el.itertext())
                    for el in ET.fromstring(archive.read("xl/sharedStrings.xml"))
                ]
            lines = []
            sheets = {
                n: n
                for n in sorted(archive.namelist())
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            }
            if (
                "xl/workbook.xml" in archive.namelist()
                and "xl/_rels/workbook.xml.rels" in archive.namelist()
            ):
                rels = {
                    r.get("Id"): posixpath.normpath(
                        posixpath.join("xl", r.get("Target", ""))
                    ).lstrip("/")
                    for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                }
                for item in ET.fromstring(archive.read("xl/workbook.xml")).findall(
                    ".//s:sheet", ns
                ):
                    rid = item.get(
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                    )
                    if rels.get(rid) in sheets:
                        sheets[rels[rid]] = item.get("name") or rels[rid]
            for sheet, title in sheets.items():
                lines.append("工作表 " + title)
                shared = {}
                for row in ET.fromstring(archive.read(sheet)).findall(
                    ".//s:sheetData/s:row", ns
                ):
                    cells = []
                    for c in row.findall("s:c", ns):
                        value = c.findtext("s:v", "", ns)
                        if c.get("t") == "s":
                            value = strings[int(value)]
                        elif c.get("t") == "inlineStr":
                            value = "".join(
                                t.text or "" for t in c.findall(".//s:t", ns)
                            )
                        ref = c.get("r", "")
                        formula = c.find("s:f", ns)
                        if formula is not None:
                            expression = formula.text
                            group = formula.get("si")
                            if formula.get("t") == "shared" and expression:
                                shared[group] = (ref, expression)
                            if expression:
                                source = "=" + expression
                            elif group in shared:
                                anchor, origin = shared[group]
                                source = f"共享公式，按 {anchor} 的 ={origin} 相对平移"
                            else:
                                source = "公式（文件未提供表达式）"
                            value = source + (
                                f" [缓存值 {value}]"
                                if value != ""
                                else " [无缓存值，需计算]"
                            )
                        cells.append(ref + "=" + value)
                    lines.append("\t".join(cells))
            return "\n".join(lines)
    data = path.read_bytes()
    if b"\x00" in data:
        raise ValueError("该二进制文件需派送到具体对话处理")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("该文件需要手动派送到具体对话处理") from None


def execute(target, prompt, paths, started):
    # Files live under the task's directory. A private destination only receives
    # the files which the owner selected on the dispatch form.
    inputs = [{"type": "text", "text": prompt}]
    text_size = 0
    for path in paths:
        if path.suffix.lower() in (".png", ".jpeg", ".jpg", ".webp", ".gif"):
            inputs.append({"type": "localImage", "path": str(path)})
        else:
            if target:
                inputs.append(
                    {"type": "text", "text": "本次任务的附件（本机文件）：" + str(path)}
                )
            try:
                value = read_document(path)
            except (
                ValueError,
                KeyError,
                IndexError,
                zipfile.BadZipFile,
                ET.ParseError,
                subprocess.SubprocessError,
            ):
                if target:
                    continue
                raise ValueError("此附件无法提取文本，请在收件箱中手动派送") from None
            if not value.strip() or text_size + len(value) > 100000:
                if target:
                    continue
                raise ValueError(
                    "附件未提取到文字或文字超过 10 万字，请手动派送到具体对话"
                )
            text_size += len(value)
            inputs.append({"type": "text", "text": "附件 " + path.name + "\n" + value})
    with tempfile.TemporaryDirectory(prefix="task-center-") as temp:
        root = Path(temp)
        env = None
        flags = []
        if not target:
            runtime = root / "runtime"
            runtime.mkdir()
            auth_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            (runtime / "auth.json").symlink_to(auth_root / "auth.json")
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in (
                    "PATH",
                    "TMPDIR",
                    "HTTPS_PROXY",
                    "HTTP_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "SSL_CERT_FILE",
                )
            }
            env.update(HOME=str(root), CODEX_HOME=str(runtime))
            flags = [
                "--disable",
                "memories",
                "--disable",
                "plugins",
                "--disable",
                "shell_tool",
                "--disable",
                "unified_exec",
                "--disable",
                "multi_agent",
                "--enable",
                "skip_host_skill_discovery",
                "-c",
                'web_search="disabled"',
            ]
        with session(temp, env, flags) as (call, send, stream):
            if target:
                try:
                    t = call(
                        2, "thread/resume", {"threadId": target, "excludeTurns": True}
                    )["thread"]
                except RuntimeError as exc:
                    if "active writer" not in str(
                        exc
                    ) and "live local writer" not in str(exc):
                        raise
                    # The desktop owns this conversation. Its writer drains the public queue.
                    latest = call(
                        3,
                        "thread/turns/list",
                        {"threadId": target, "itemsView": "notLoaded", "limit": 1},
                    )["data"]
                    client_id = str(uuid.uuid4())
                    receipt = call(
                        4,
                        "thread/queue/add",
                        {
                            "threadId": target,
                            "clientUserMessageId": client_id,
                            "input": inputs,
                        },
                    )["queuedSubmission"]
                    return "desktop_queued", {
                        "queue": receipt["id"],
                        "client": client_id,
                        "after": latest[0]["id"] if latest else None,
                    }
                if t["id"] != target or t.get("status", {}).get("type") == "active":
                    raise RuntimeError("所选对话正在执行其他任务，请稍后重新派送")
            else:
                t = call(
                    2,
                    "thread/start",
                    {
                        "ephemeral": True,
                        "environments": [],
                        "selectedCapabilityRoots": [],
                        "sandbox": "read-only",
                        "approvalPolicy": "never",
                        "cwd": temp,
                        "baseInstructions": "你负责完成协作任务，仅依据本任务的要求和附件回答。资料够用时直接给出最终结果；仅在缺少完成任务所必需、且发布者能够补充的信息时提出一个明确问题。",
                    },
                )["thread"]
            turn_params = {"threadId": t["id"], "input": inputs}
            if not target:
                turn_params["effort"] = "low"
                turn_params["outputSchema"] = {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["done", "question"],
                            "description": "已完成为 done；必须等发布者补充时为 question",
                        },
                        "text": {
                            "type": "string",
                            "description": "交付结果，或向发布者提出的问题",
                        },
                    },
                    "additionalProperties": False,
                    "required": ["kind", "text"],
                }
            turn = call(3, "turn/start", turn_params)["turn"]["id"]
            started(t["id"], turn)
            final = ""
            for line in stream:
                event = json.loads(line)
                if "id" in event and "method" in event:
                    send(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "请在 Codex 中处理需要额外授权的操作",
                            },
                        }
                    )
                a = event.get("params", {})
                if a.get("threadId") != t["id"]:
                    continue
                if (
                    event.get("method") == "item/completed"
                    and a.get("turnId") == turn
                    and a["item"]["type"] == "agentMessage"
                    and a["item"].get("phase") != "commentary"
                ):
                    final = a["item"]["text"]
                if event.get("method") == "turn/completed" and a["turn"]["id"] == turn:
                    if a["turn"]["status"] != "completed" or not final.strip():
                        raise RuntimeError(
                            str(a["turn"].get("error") or "本轮未返回文本结果")[:450]
                        )
                    if target:
                        return "review", final
                    result = json.loads(final)
                    if (
                        not isinstance(result, dict)
                        or result.get("kind") not in ("done", "question")
                        or not isinstance(result.get("text"), str)
                        or not result["text"].strip()
                    ):
                        raise RuntimeError(
                            "Codex 返回的任务结果无效，请在收件箱中重新派送"
                        )
                    return (
                        "waiting" if result["kind"] == "question" else "done"
                    ), result["text"].strip()
            raise RuntimeError("Codex 连接已中断，请检查目标对话的本轮记录")


def queue_state(call, target, receipt, running_turn=None):
    """Look up only turns newer than the submission and identify our input by its client ID."""
    params = {
        "threadId": target,
        "limit": 20,
        "itemsView": "notLoaded",
        "sortDirection": "desc",
    }
    while True:
        page = call(10, "thread/turns/list", params)
        for turn in page["data"]:
            if turn["id"] == receipt["after"]:
                return None
            if running_turn and turn["id"] != running_turn:
                continue
            if (
                running_turn
                and turn["completedAt"] is None
                and turn["status"] != "completed"
            ):
                return "desktop_running", turn["id"], ""
            # Paginate the individual turn: long sessions can contain many tool events.
            query = {
                "threadId": target,
                "turnId": turn["id"],
                "deferOutput": {"commandExecution": True, "mcpToolCall": True},
                "limit": 100,
                "sortDirection": "asc",
            }
            found = turn["id"] == running_turn
            final = ""
            while True:
                items = call(11, "thread/items/list", query)
                for item in items["data"]:
                    entry = item["item"]
                    if (
                        entry["type"] == "userMessage"
                        and entry.get("clientId") == receipt["client"]
                    ):
                        found = True
                    if (
                        entry["type"] == "agentMessage"
                        and entry.get("phase") != "commentary"
                    ):
                        final = entry["text"]
                query["cursor"] = items.get("nextCursor")
                if not query["cursor"]:
                    break
            if found:
                if turn["status"] == "completed" and final.strip():
                    return "review", turn["id"], final
                # A different writer's live rollout is reported as "interrupted"
                # until its actual completion marker arrives.
                if turn["status"] == "inProgress" or turn["completedAt"] is None:
                    return "desktop_running", turn["id"], ""
                return (
                    "failed",
                    turn["id"],
                    "目标对话本轮未完成，请打开派送对话查看，再决定是否重试。",
                )
        params["cursor"] = page.get("nextCursor")
        if not params["cursor"]:
            return None


def queued_in_thread(call, target, queue_id):
    params = {"threadId": target}
    while True:
        page = call(12, "thread/queue/list", params)
        if any(q["id"] == queue_id for q in page["data"]):
            return True
        params["cursor"] = page.get("nextCursor")
        if not params["cursor"]:
            return False
