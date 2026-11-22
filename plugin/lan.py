"""Bonjour discovery and the initial TLS check for local Task Center devices."""

import base64
import hashlib
import hmac
import http.client
import json
import re
import ssl
import subprocess
from urllib.parse import urlsplit

SERVICE = "_codex-tc._tcp"
DNS_SD = "/usr/bin/dns-sd"
PIN = r"[A-Za-z0-9_-]{43}"


def fingerprint(ca):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(ssl.PEM_cert_to_DER_cert(ca)).digest())
        .decode()
        .rstrip("=")
    )


def advertise(config, ca):
    instance = config["dns"].removesuffix(".local")[:55]
    if config["port"] != 43871:
        instance += "-" + str(config["port"])
    args = ["-i", config["wifi_device"]] if config.get("wifi_device") else []
    return subprocess.Popen(
        [
            DNS_SD,
            *args,
            "-R",
            instance,
            SERVICE,
            "local.",
            str(config["port"]),
            "fp=" + fingerprint(ca),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def discover():
    with subprocess.Popen(
        [DNS_SD, "-Z", SERVICE, "local."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        try:
            stdout, _ = process.communicate(timeout=1.35)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, _ = process.communicate()
    endpoints, pins = {}, {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].endswith("." + SERVICE):
            continue
        if fields[1] == "SRV" and len(fields) >= 6:
            host = fields[5].rstrip(".")
            if (
                re.fullmatch(r"[a-zA-Z0-9-]{1,63}\.local", host, re.IGNORECASE)
                and fields[4].isdigit()
            ):
                endpoints[fields[0]] = (host, int(fields[4]))
        elif fields[1] == "TXT":
            pin = re.search(r'"fp=(' + PIN + r')"(?=\s|$)', line)
            if pin:
                pins[fields[0]] = pin[1]
    return {
        "devices": [
            {
                "name": host[:-6] if port == 43871 else f"{host[:-6]}:{port}",
                "endpoint": f"{host}:{port}",
                "pin": pins[instance],
            }
            for instance, (host, port) in endpoints.items()
            if instance in pins and 1 <= port <= 65535
        ]
    }


def verified_authority(endpoint, pin):
    target = urlsplit("//" + endpoint)
    # Fetch public CA material before sending any device credential.
    bootstrap = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bootstrap.check_hostname = False
    bootstrap.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(
        target.hostname, target.port, timeout=4, context=bootstrap
    )
    try:
        conn.request("POST", "/pair/authority", b"{}")
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("接收端没有返回设备证书")
        data = json.loads(response.read(32768))
        ca = data.get("ca") if isinstance(data, dict) else None
        if not isinstance(ca, str) or not hmac.compare_digest(pin, fingerprint(ca)):
            raise ValueError("设备证书已变化，请重新搜索接收方")
        return ca
    finally:
        conn.close()
