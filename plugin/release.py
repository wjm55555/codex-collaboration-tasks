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

VERSION = "0.6.1"
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


def github_version(root):
    if not GITHUB_REPO:
        return None
    cache = root / "github-release"
    checked = root / "github-checked"
    if not checked.exists() or time.time() - checked.stat().st_mtime >= CHECK_INTERVAL:
        checked.touch()

        def check():
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Collaboration-Tasks",
                },
            )
            try:
                with github_open(request, timeout=5) as response:
                    release = json.loads(response.read(128 * 1024))
                version = release["tag_name"].removeprefix("v")
                version_tuple(version)
                assets = {entry["name"] for entry in release["assets"]}
                if {RELEASE_ASSET, RELEASE_ASSET + ".sig"} <= assets:
                    with tempfile.NamedTemporaryFile(
                        "w", dir=root, delete=False
                    ) as temp:
                        temp.write(version)
                    Path(temp.name).replace(cache)
            except (KeyError, OSError, TypeError, ValueError):
                return  # Keep the last known release if GitHub can't be reached.

        threading.Thread(target=check, daemon=True).start()
    return cache.read_text() if cache.is_file() else None


def github_package(root):
    version = github_version(root)
    if not version or version_tuple(version) <= version_tuple(VERSION):
        raise ValueError("GitHub 暂无可下载的新版本")
    url = (
        f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{RELEASE_ASSET}"
    )
    with github_open(url, timeout=15) as response:
        archive = response.read(MAX_PACKAGE + 1)
    with github_open(url + ".sig", timeout=15) as response:
        signature = response.read(1024)
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
    if version_tuple(version) <= version_tuple(VERSION):
        raise ValueError("当前已是最新版本")
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
        if receiver:
            with sqlite3.connect(
                (root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True
            ) as db:
                busy = db.execute(
                    "SELECT 1 FROM tasks WHERE status IN ('queued','dispatching','running','desktop_queued','desktop_running') LIMIT 1"
                ).fetchone()
            if busy:
                raise ValueError("还有任务在执行，请完成后再更新接收端")
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
