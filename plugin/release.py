"""Signed publisher updates distributed by the receiver on the local network."""

import base64
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.7.3"
GITHUB_REPO = "wjm55555/codex-collaboration-tasks"
RELEASE_ASSET = "collaboration-tasks-macos.zip"
CHECK_INTERVAL = 3600
MAX_PACKAGE = 2 * 1024 * 1024
PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAyqNeC/rjlPfj4GhsG2UF
7MLVDGit71Rg4v2SEzL/QO+lLDKPYRyafqLfQlUAqXIiLYjcRKq5vKDZZe1zzLjU
gIUOl/D/kBlQm60P/MhEgmwGm2R2Eb3dhQ/rheZYljZ8zf0wGe5UgDySgphRZLeo
s6nan+Hbr37QVZjOFCWHUpvJmddtu9S5cn2ayUAdTniQbPGD6kB8zTKSBFGDeyeC
HWI8zaBFtlY1OHh71Ove/F0g8SMvxai8387FYZfPHTfZx925RQTR4RC4ujWubUbQ
0Eaw32BgUJDcwrSL6/bvO1jxzQsJSajjywx9UEDcvysKgg7tQo4vgxk2rOQwlag3
TYQBJ32wovz7Y7qSvJPxrM2wwfxIuPI8iS1z7TC5p52X1zsETwzDSCEybZPrrju+
b2Q+8jKvieWz5dlryFH3bWdij82wUihVG4zV6er4RcYiIas3qvx/GJNjUa9X8z0V
H0Qjf+IYDiZs8YdxkDph7yF5nWw312UKCjoGA5roNq1fAgMBAAE=
-----END PUBLIC KEY-----
"""


def available(root):
    directory = root / "releases"
    return (
        VERSION
        if all(
            (directory / (VERSION + suffix)).is_file() for suffix in (".zip", ".sig")
        )
        else None
    )


def package(root):
    if not available(root):
        raise ValueError("接收端暂未提供升级包")
    directory = root / "releases"
    return {
        "version": VERSION,
        "archive": base64.b64encode(
            (directory / (VERSION + ".zip")).read_bytes()
        ).decode(),
        "signature": base64.b64encode(
            (directory / (VERSION + ".sig")).read_bytes()
        ).decode(),
    }


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", version
    ):
        raise ValueError("升级包的版本号无效")
    return tuple(map(int, version.split(".")))


def github_open(request, timeout):
    # urllib otherwise skips the macOS proxy configuration when only NO_PROXY is set.
    if sys.platform == "darwin" and not urllib.request.getproxies_environment().get(
        "https"
    ):
        proxy = urllib.request.getproxies_macosx_sysconf().get("https")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy})
            )
            return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def write_json(path, data):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as temp:
        json.dump(data, temp)
    Path(temp.name).replace(path)


def auto_enabled(root):
    config = root / "update-preferences.json"
    return (
        config.is_file() and json.loads(config.read_text()).get("autoInstall") is True
    )


def installed_version(root):
    manifest = root / "marketplace/plugin/.codex-plugin/plugin.json"
    return (
        json.loads(manifest.read_text())["version"] if manifest.is_file() else VERSION
    )


def update_status(root, *, force=False):
    local = {
        "installedVersion": installed_version(root),
        "autoInstall": auto_enabled(root),
    }
    cache = root / "github-check.json"
    info = json.loads(cache.read_text()) if cache.is_file() else {"status": "idle"}
    if not GITHUB_REPO:
        return {"status": "disabled", **local}
    now = int(time.time())
    checking = info["status"] == "checking"
    running = checking and now - info["startedAt"] < 20
    check_again = 120 if info["status"] == "error" else CHECK_INTERVAL
    due = now - info.get("checkedAt", 0) >= check_again
    if not running and (force or due or checking):
        info.update(status="checking", startedAt=now)
        write_json(cache, info)

        def check():
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Collaboration-Tasks",
                },
            )
            result = {**info, "checkedAt": int(time.time())}
            try:
                with github_open(request, timeout=5) as response:
                    release = json.loads(response.read(128 * 1024))
                version = release["tag_name"].removeprefix("v")
                version_tuple(version)
                assets = {entry["name"] for entry in release["assets"]}
                if not {RELEASE_ASSET, RELEASE_ASSET + ".sig"} <= assets:
                    raise ValueError("No signed package")
                result.update(status="ok", version=version)
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                result["status"] = "error"
            write_json(cache, result)

        threading.Thread(target=check, daemon=True).start()
    return {**info, **local}


def receiver_busy(root):
    if not (root / "host.json").is_file():
        return False
    with sqlite3.connect((root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        return (
            db.execute(
                "SELECT 1 FROM tasks WHERE status IN ('queued','dispatching','running','desktop_queued','desktop_running') LIMIT 1"
            ).fetchone()
            is not None
        )


def github_package(version):
    url = (
        f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{RELEASE_ASSET}"
    )
    try:
        with github_open(url, timeout=15) as response:
            archive = response.read(MAX_PACKAGE + 1)
        with github_open(url + ".sig", timeout=15) as response:
            signature = response.read(1024)
    except OSError as exc:
        raise ValueError("未能从 GitHub 下载安装包，请稍后重试") from exc
    if len(archive) > MAX_PACKAGE:
        raise ValueError("升级包大小超出限制")
    return {
        "version": version,
        "archive": base64.b64encode(archive).decode(),
        "signature": base64.b64encode(signature).decode(),
    }


def apply_update(root, response):
    if not isinstance(response, dict):
        raise ValueError("升级包格式无效")  # noqa: TRY004
    version = response.get("version")
    installed = installed_version(root)
    if version_tuple(version) <= version_tuple(installed):
        return {"version": installed, "alreadyInstalled": True}
    encoded, signature = response.get("archive"), response.get("signature")
    if (
        not isinstance(encoded, str)
        or len(encoded) > 4 * ((MAX_PACKAGE + 2) // 3)
        or not isinstance(signature, str)
        or len(signature) > 1024
    ):
        raise ValueError("升级包格式无效")
    blob = base64.b64decode(encoded, validate=True)
    signed = base64.b64decode(signature, validate=True)
    with tempfile.TemporaryDirectory(prefix="collaboration-update-") as temp:
        stage = Path(temp)
        (stage / "trusted.pub").write_bytes(PUBLIC_KEY)
        (stage / "payload.zip").write_bytes(blob)
        (stage / "payload.sig").write_bytes(signed)
        check = subprocess.run(
            [
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(stage / "trusted.pub"),
                "-signature",
                str(stage / "payload.sig"),
                str(stage / "payload.zip"),
            ],
            capture_output=True,
            check=False,
        )
        if check.returncode:
            raise ValueError("升级包签名校验失败，请让接收方重新安装最新版")
        with zipfile.ZipFile(io.BytesIO(blob)) as payload:
            manifest_path = "plugin/.codex-plugin/plugin.json"
            prefix = "" if manifest_path in payload.namelist() else "协作任务/"
            manifest = json.loads(payload.read(prefix + manifest_path))
            if (manifest.get("name"), manifest.get("version")) != (
                "task-center",
                version,
            ):
                raise ValueError("升级包与提供的版本不一致")
            payload.extractall(stage)
        profile = (
            root.parent if root.name == "lan-task-center" else Path.home() / ".codex"
        )
        receiver = (root / "host.json").is_file()
        if receiver_busy(root):
            return {"waiting": True}
        try:
            install = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-B",
                    str(stage / prefix / "setup.py"),
                    "host" if receiver else "publisher",
                ],
                env={**os.environ, "CODEX_HOME": str(profile)},
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("等待升级超时，请稍后重试") from exc
        if install.returncode:
            raise ValueError("升级未完成，请稍后重试")
    return {"version": version}
