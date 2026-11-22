"""Offline-installable TCP relay for Task Center. Task TLS terminates on the receiving Mac."""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit


def receiver_socket(config, purpose=b"H"):
    address = urlsplit("//" + config["endpoint"])
    conn = socket.create_connection((address.hostname, address.port), 5)
    try:
        conn.sendall(purpose)
        with conn.makefile("rb") as response:
            challenge = response.read(32)
        if len(challenge) != 32:
            raise OSError("中继未响应认证请求")
        conn.sendall(
            hmac.digest(
                bytes.fromhex(config["key"]), purpose + challenge, hashlib.sha256
            )
        )
        if conn.recv(1) != b"K":
            raise OSError("中继未接受接收端配置")
        return conn
    except (ValueError, OSError):
        conn.close()
        raise


async def serve(bind, port, key):
    available = asyncio.Condition()
    receivers = deque()

    async def pump(reader, writer):
        while True:
            data = await reader.read(65536)
            if not data:
                if writer.can_write_eof():
                    writer.write_eof()
                return
            writer.write(data)
            await writer.drain()

    async def handle(reader, writer):
        slot = None
        done = None
        try:
            first = await asyncio.wait_for(reader.readexactly(1), 5)
            if first in (b"H", b"V"):
                challenge = secrets.token_bytes(32)
                writer.write(challenge)
                await writer.drain()
                response = await asyncio.wait_for(reader.readexactly(32), 5)
                if not hmac.compare_digest(
                    response, hmac.digest(key, first + challenge, hashlib.sha256)
                ):
                    return
                writer.write(b"K")
                await writer.drain()
                if first == b"V":
                    return
                slot = asyncio.get_running_loop().create_future()
                async with available:
                    receivers.append(slot)
                    available.notify()
                disconnected = asyncio.create_task(reader.read(1))
                try:
                    await asyncio.wait(
                        (slot, disconnected),
                        timeout=45,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    disconnected.cancel()
                    await asyncio.gather(disconnected, return_exceptions=True)
                if not slot.done():
                    return
                guest_in, guest_out, done = slot.result()
                writer.write(b"G\x16")
                await writer.drain()
                flows = [
                    asyncio.create_task(pump(guest_in, writer)),
                    asyncio.create_task(pump(reader, guest_out)),
                ]
                try:
                    await asyncio.gather(*flows)
                finally:
                    for flow in flows:
                        flow.cancel()
                    await asyncio.gather(*flows, return_exceptions=True)
            elif first == b"\x16":  # TLS ClientHello from the publishing Mac.
                async with available:
                    await asyncio.wait_for(
                        available.wait_for(lambda: bool(receivers)), 2
                    )
                    target = receivers.popleft()
                    complete = asyncio.get_running_loop().create_future()
                    target.set_result((reader, writer, complete))
                await complete
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):  # noqa: UP041
            pass
        finally:
            if slot in receivers:
                receivers.remove(slot)
            if done is not None and not done.done():
                done.set_result(None)
            writer.close()

    server = await asyncio.start_server(handle, bind, port)
    async with server:
        print(f"协作任务中继已监听 {bind}:{port}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    cli = argparse.ArgumentParser(
        description="单位 VPN 协作任务中继，无需外网或第三方 Python 包"
    )
    cli.add_argument("--bind", required=True, help="绑定服务器的内网 IPv4 地址")
    cli.add_argument("--port", type=int, default=43872)
    cli.add_argument("--config", type=Path, default=Path("接收端中继配置.json"))
    cli.add_argument("--init", action="store_true", help="首次生成接收端配置并退出")
    args = cli.parse_args()
    addr = ipaddress.IPv4Address(args.bind)
    if addr.is_unspecified or addr.is_multicast or not 1 <= args.port <= 65535:
        cli.error("请指定实际网卡地址和有效端口")
    if args.init:
        os.umask(0o077)
        with args.config.open("x") as f:
            json.dump(
                {"endpoint": f"{addr}:{args.port}", "key": secrets.token_hex(32)}, f
            )
        print("已生成接收端中继配置。只交给接收方。")
    else:
        key = bytes.fromhex(json.loads(args.config.read_text())["key"])
        asyncio.run(serve(str(addr), args.port, key))
