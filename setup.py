import hashlib
import http.client
import ipaddress
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
DEFAULT_PROFILE = Path.home() / ".codex"
PROFILE = Path(os.environ.get("CODEX_HOME", DEFAULT_PROFILE)).expanduser().resolve()
ROOT = (
    Path.home() / "Library/Application Support/LAN Task Center"
    if PROFILE == DEFAULT_PROFILE.resolve()
    else PROFILE / "lan-task-center"
)
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
LABEL = "local.lan-task-center"
if PROFILE != DEFAULT_PROFILE.resolve():
    LABEL += "." + hashlib.sha256(os.fsencode(PROFILE)).hexdigest()[:12]
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def save(path, obj):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    Path(f.name).replace(path)


def network():
    ports = subprocess.check_output(
        ["/usr/sbin/networksetup", "-listallhardwareports"], text=True
    )
    match = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\w+)", ports)
    if not match:
        raise ValueError("这台 Mac 没有可用的 Wi-Fi 接口。")
    device = match[1]
    ip = subprocess.run(
        ["/usr/sbin/ipconfig", "getifaddr", device],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    host = subprocess.check_output(
        ["/usr/sbin/scutil", "--get", "LocalHostName"], text=True
    ).strip()
    if not re.fullmatch(r"[\w-]+", host, re.ASCII):
        raise ValueError("请在 macOS「共享」中设置本地主机名，再安装接收端。")
    return device, ip, host + ".local"


def install():
    # A local marketplace must survive moving the original download or unpacking an update.
    market = ROOT / "marketplace"
    plug = market / "plugin"
    for relative in (
        ".agents/plugins/marketplace.json",
        "plugin/.codex-plugin/plugin.json",
        "plugin/assets/collaboration.png",
    ):
        dst = market / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BASE / relative, dst)
    modules = (
        "client.py",
        "panel.html",
        "worker.py",
        "host.py",
        "relay.py",
        "lan.py",
        "release.py",
    )
    # Keep copies for older installations that start the MCP module from the marketplace.
    for name in ("client.py", "panel.html", "lan.py", "release.py"):
        temp = plug / (name + ".new")
        shutil.copyfile(BASE / "plugin" / name, temp)
        temp.replace(plug / name)
    mcp = json.loads((BASE / "plugin/.mcp.json").read_text())
    spec = mcp["mcpServers"]["task-center"]
    spec.update(
        command="/usr/bin/python3",
        args=["-B", str(ROOT / "client.py"), "mcp"],
        env={"TASK_CENTER_STATE": str(ROOT)},
    )
    save(plug / ".mcp.json", mcp)
    configured = json.loads(
        subprocess.check_output([CODEX, "plugin", "marketplace", "list", "--json"])
    )
    old = next(
        (m for m in configured["marketplaces"] if m["name"] == "lan-task-center"), None
    )
    if old and Path(old["root"]).resolve() != market.resolve():
        subprocess.run(
            [CODEX, "plugin", "marketplace", "remove", "lan-task-center"], check=True
        )
        old = None
    if not old:
        subprocess.run([CODEX, "plugin", "marketplace", "add", str(market)], check=True)
    subprocess.run([CODEX, "plugin", "add", "task-center@lan-task-center"], check=True)
    icon = ROOT / "assets" / "collaboration.png"
    icon.parent.mkdir(exist_ok=True)
    for name in (*modules, "assets/collaboration.png"):
        target = ROOT / name
        temp = target.with_suffix(target.suffix + ".new")
        shutil.copyfile(BASE / "plugin" / name, temp)
        temp.replace(target)


def certificates(dns, addresses, relay_host=None):
    # cert.pem is the authority already trusted by paired devices. Rotate the
    # server certificate independently so an address change preserves identities.
    ca = ROOT / "cert.pem"
    with tempfile.TemporaryDirectory(dir=ROOT) as work:
        tmp = Path(work)

        def openssl(*args):
            subprocess.run(
                ["/usr/bin/openssl", *map(str, args)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        if not ca.exists():
            cfg = tmp / "ca.cnf"
            cfg.write_text(
                "[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=Task Center Pairing CA\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
            )
            openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-nodes",
                "-days",
                "3650",
                "-config",
                cfg,
                "-keyout",
                tmp / "ca.key",
                "-out",
                tmp / "ca.pem",
            )
            (tmp / "ca.key").replace(ROOT / "key.pem")
            (tmp / "ca.pem").replace(ca)
        sans = [
            "DNS:" + dns,
            "IP:127.0.0.1",
            *[
                "IP:" + str(ipaddress.IPv4Address(ip))
                for ip in sorted({ip for ip in addresses if ip and ip != "127.0.0.1"})
            ],
        ]
        if relay_host:
            try:
                address = ipaddress.ip_address(relay_host)
            except ValueError:
                if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", relay_host):
                    raise ValueError("中继的主机名无效")
                sans.append("DNS:" + relay_host)
            else:
                sans.append("IP:" + str(address))
        ext = tmp / "leaf.cnf"
        ext.write_text(
            "[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName="
            + ",".join(sans)
            + "\n"
        )
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=" + dns,
            "-keyout",
            tmp / "server-key.pem",
            "-out",
            tmp / "server.csr",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            tmp / "server.csr",
            "-CA",
            ca,
            "-CAkey",
            ROOT / "key.pem",
            "-set_serial",
            "0x" + secrets.token_hex(16),
            "-days",
            "365",
            "-sha256",
            "-extfile",
            ext,
            "-extensions",
            "leaf",
            "-out",
            tmp / "server.pem",
        )
        for name in ("server.pem", "server-key.pem"):
            (tmp / name).replace(ROOT / name)


def connection(cfg, token, local=False):
    host = "127.0.0.1" if local else cfg["dns"]
    return {
        "url": "https://" + host + ":" + str(cfg["port"]),
        "token": token,
        "ca": (ROOT / "cert.pem").read_text(),
    }


def verify_connection(data, *, owner):
    if (
        not isinstance(data, dict)
        or set(data) != {"url", "ca", "token"}
        or not all(isinstance(v, str) and v for v in data.values())
    ):
        raise ValueError("请选择接收方生成的协作任务邀请 JSON。")
    u = urlsplit(data["url"])
    if (
        u.scheme != "https"
        or not u.hostname
        or not u.port
        or u.username
        or u.password
        or u.path not in ("", "/")
        or u.query
        or u.fragment
    ):
        raise ValueError("邀请文件的连接地址无效。")
    conn = http.client.HTTPSConnection(
        u.hostname,
        u.port,
        timeout=4,
        context=ssl.create_default_context(cadata=data["ca"]),
    )
    try:
        conn.request(
            "POST",
            "/api/state",
            b"{}",
            {
                "Authorization": "Bearer " + data["token"],
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        if response.status == 403:
            raise ValueError("邀请已撤销或无效，请向接收方获取新的邀请。")
        if response.status != 200 or json.load(response).get("owner") is not owner:
            raise ValueError("配对身份不匹配，请确认所选的是发布方邀请。")
    finally:
        conn.close()


def host(relay_config=None, *, lan_only=False):
    path = ROOT / "host.json"
    if path.exists():
        cfg = json.loads(path.read_text())
    else:
        sys.path.insert(0, str(BASE / "plugin"))
        from client import peers

        links = peers(ROOT)
        if links:
            save(ROOT / "peers.json", links)
        port = 43871
        if PROFILE != DEFAULT_PROFILE.resolve():
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        cfg = {"port": port, "owner": secrets.token_urlsafe(32), "peers": []}
    device, ip, dns = network()
    if lan_only:
        cfg.pop("relay", None)
    if relay_config is not None:
        sys.path.insert(0, str(BASE / "plugin"))
        from relay import receiver_socket

        endpoint = relay_config.get("endpoint", "")
        target = urlsplit("//" + endpoint)
        if (
            not target.hostname
            or not target.port
            or target.netloc != endpoint
            or target.username
            or not re.fullmatch(r"[a-f0-9]{64}", relay_config.get("key", ""))
        ):
            raise ValueError("请选择中继服务器生成的接收端配置")
        with receiver_socket(relay_config, b"V"):
            pass
        cfg["relay"] = relay_config
    public_host = (
        urlsplit("//" + cfg["relay"]["endpoint"]).hostname if cfg.get("relay") else None
    )
    certificates(dns, [cfg.get("ip"), ip], public_host)
    cfg.update(ip=ip, wifi_device=device, dns=dns)
    save(path, cfg)
    profile = connection(cfg, cfg["owner"], local=True)
    save(ROOT / "connection.json", profile)
    install()
    # The outer installer optionally carries a signed, self-contained publisher ZIP.
    version = json.loads((BASE / "plugin/.codex-plugin/plugin.json").read_text())[
        "version"
    ]
    release = BASE / "release"
    if (release / (version + ".zip")).is_file() and (
        release / (version + ".sig")
    ).is_file():
        releases = ROOT / "releases"
        releases.mkdir(mode=0o700, exist_ok=True)
        for suffix in (".zip", ".sig"):
            shutil.copyfile(release / (version + suffix), releases / (version + suffix))
    PLIST.parent.mkdir(exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump(
            {
                "Label": LABEL,
                "ProgramArguments": ["/usr/bin/python3", str(ROOT / "host.py")],
                "EnvironmentVariables": {
                    "TASK_CENTER_STATE": str(ROOT),
                    "CODEX_HOME": str(PROFILE),
                },
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ProcessType": "Interactive",
                "StandardErrorPath": str(ROOT / "host.log"),
            },
            f,
        )
    domain = "gui/" + str(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", domain + "/" + LABEL], capture_output=True, check=False
    )
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    for i in range(20):
        try:
            verify_connection(profile, owner=True)
            break
        except ConnectionRefusedError:
            if i == 19:
                raise ValueError(
                    "接收服务未能启动，请查看本机协作任务的 host.log。"
                ) from None
            time.sleep(0.2)
    print("协作任务已就绪。在 Codex 的「… → 协作任务」收发任务。")


def invite(destination, name, local=False):
    path = ROOT / "host.json"
    if not path.is_file():
        raise ValueError("请先在这台 Mac 安装接收端。")
    cfg = json.loads(path.read_text())
    # Reissue an existing pairing file without abandoning that publisher's history.
    peer = None
    if destination.is_file() and destination.stat().st_size:
        old = json.loads(destination.read_text())
        if not isinstance(old, dict):
            raise ValueError("请选择原协作任务邀请，或另选保存位置。")
        peer = next((p for p in cfg["peers"] if p["token"] == old.get("token")), None)
        if peer is None:
            raise ValueError("该文件与当前接收端的有效身份不匹配，请另选保存位置。")
    if peer is None:
        peer = {
            "id": secrets.token_hex(12),
            "name": name,
            "token": secrets.token_urlsafe(32),
        }
        cfg["peers"].append(peer)
        save(path, cfg)
    if (ROOT / "tasks.sqlite").exists():
        with sqlite3.connect(ROOT / "tasks.sqlite") as db:
            db.execute(
                "INSERT OR IGNORE INTO devices(id,name,digest) VALUES(?,?,?)",
                (
                    peer["id"],
                    peer["name"],
                    hashlib.sha256(peer["token"].encode()).hexdigest(),
                ),
            )
            if db.execute(
                "SELECT revoked FROM devices WHERE id=?", (peer["id"],)
            ).fetchone()[0]:
                raise ValueError("这台设备的授权已撤销，请生成新的配对码")
    save(destination, connection(cfg, peer["token"], local))
    print("邀请已保存。此文件对应独立的发布者身份。")


def redeem(code):
    parts = code.strip().split("|")
    if len(parts) != 4 or parts[0] != "TC1":
        raise ValueError("请粘贴完整配对码")
    _, endpoint, pin, secret = parts
    target = urlsplit("//" + endpoint)
    if (
        not target.hostname
        or not target.port
        or target.netloc != endpoint
        or target.username
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", pin)
        or not re.fullmatch(r"[A-Za-z0-9_-]{27}", secret)
    ):
        raise ValueError("配对码格式无效")
    sys.path.insert(0, str(BASE / "plugin"))
    from lan import verified_authority

    ca = verified_authority(endpoint, pin)
    conn = http.client.HTTPSConnection(
        target.hostname,
        target.port,
        timeout=5,
        context=ssl.create_default_context(cadata=ca),
    )
    try:
        conn.request("POST", "/pair/redeem", json.dumps({"secret": secret}).encode())
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("配对码已过期或已用过，请让接收方重新生成")
        token = json.load(response)["token"]
    finally:
        conn.close()
    return {"url": "https://" + endpoint, "ca": ca, "token": token}


def guest(invitation=None):
    sys.path.insert(0, str(BASE / "plugin"))
    from client import peers
    from lan import fingerprint

    data = json.loads(invitation.read_text()) if invitation else redeem(input().strip())
    verify_connection(data, owner=False)
    links = peers(ROOT)
    name = urlsplit(data["url"]).hostname.removesuffix(".local")
    links[fingerprint(data["ca"])] = {"name": name, **data}
    save(ROOT / "peers.json", links)
    host()
    print("配对成功。在 Codex 的「… → 协作任务」发布任务。")


if __name__ == "__main__":
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    ROOT.chmod(0o700)
    try:
        if (
            sys.argv[1] in ("install", "host", "publisher", "pair", "guest", "relay")
            and not Path(CODEX).is_file()
        ):
            raise ValueError("请先把包含 Codex 的 ChatGPT 桌面版安装到「应用程序」。")
        if sys.argv[1] in ("install", "publisher"):
            host()
        elif sys.argv[1] == "host":
            host(lan_only=True)
        elif sys.argv[1] == "relay":
            host(json.loads(Path(sys.argv[2]).read_text()))
        elif sys.argv[1] == "pair":
            guest()
        elif sys.argv[1] == "guest":
            guest(Path(sys.argv[2]))
        elif sys.argv[1] == "invite":
            invite(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "协作者")
        elif sys.argv[1] == "revoke":
            path = ROOT / "host.json"
            cfg = json.loads(path.read_text())
            with sqlite3.connect(ROOT / "tasks.sqlite") as db:
                db.execute("UPDATE devices SET revoked=1")
                db.execute("DELETE FROM pair_codes")
                db.execute("DELETE FROM lan_requests")
            cfg["peers"] = []
            save(path, cfg)
            print("发布者邀请已撤销。")
        else:
            raise ValueError("请选择 install、guest、invite 或 revoke 操作。")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if (
            isinstance(exc, (ConnectionError, TimeoutError))
            or isinstance(exc, OSError)
            and getattr(exc, "errno", None) in (-2, 8, 60, 65)
        ):
            sys.exit(
                "未连上接收端。请确认接收端已打开，并且当前 Wi-Fi 或单位 VPN 能访问配对地址。"
            )
        sys.exit("安装或配对未完成。" + str(exc))
