#!/usr/bin/env python3
"""MINICRAFT room server: static web hosting + authoritative WebSocket rooms."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_CLIENTS = 2
MAX_MESSAGE = 4 * 1024 * 1024


def safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Client:
    ws: web.WebSocketResponse
    name: str
    chunks: dict[str, dict[str, Any]] = field(default_factory=dict)


class Room:
    def __init__(self, room_id: str, data_dir: Path):
        self.id = room_id
        self.data_dir = data_dir
        self.clients: list[Client] = []
        self.host: Client | None = None
        self.world: dict[str, int] = {}
        self.shared: dict[str, Any] = {}
        self.password_hash = ""
        self.guest_can_build = True
        self.save_task: asyncio.Task | None = None
        self.load()

    @property
    def path(self) -> Path:
        return self.data_dir / f"{self.id}.json"

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            self.world = {str(k): int(v) for k, v in data.get("world", {}).items()}
            self.shared = data.get("shared", {}) if isinstance(data.get("shared"), dict) else {}
            self.password_hash = str(data.get("password_hash", ""))
            self.guest_can_build = bool(data.get("guest_can_build", True))
            print(f"[room {self.id}] loaded {len(self.world)} blocks")
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[room {self.id}] failed to load: {exc}")

    def schedule_save(self) -> None:
        if self.save_task and not self.save_task.done():
            self.save_task.cancel()
        self.save_task = asyncio.create_task(self._save_later())

    async def _save_later(self) -> None:
        try:
            await asyncio.sleep(2.0)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                safe_json({"world": self.world, "shared": self.shared, "password_hash": self.password_hash, "guest_can_build": self.guest_can_build, "updated": time.time()}),
                "utf-8",
            )
            tmp.replace(self.path)
            print(f"[room {self.id}] saved {len(self.world)} blocks")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[room {self.id}] save failed: {exc}")

    def snapshot(self) -> dict[str, Any]:
        return {**self.shared, "kind": "snapshot", "world": list(self.world.items())}

    async def send(self, client: Client, payload: dict[str, Any]) -> None:
        if not client.ws.closed:
            await client.ws.send_str(safe_json(payload))

    async def broadcast(self, payload: dict[str, Any], exclude: Client | None = None) -> None:
        text = safe_json(payload)
        dead: list[Client] = []
        for client in self.clients:
            if client is exclude:
                continue
            try:
                await client.ws.send_str(text)
            except Exception:
                dead.append(client)
        for client in dead:
            if client in self.clients:
                self.clients.remove(client)

    async def add(self, client: Client) -> bool:
        if len(self.clients) >= MAX_CLIENTS:
            await client.ws.send_str(safe_json({"kind": "error", "message": "房间已满（最多两人）"}))
            await client.ws.close(code=4001, message=b"room full")
            return False
        self.clients.append(client)
        if self.host is None:
            self.host = client
        role = "host" if client is self.host else "guest"
        await self.send(client, {"kind": "welcome", "role": role, "room": self.id, "clients": len(self.clients), "canBuild": self.guest_can_build})
        await self.broadcast({"kind": "hello", "name": client.name}, exclude=client)
        if self.world:
            await self.send(client, self.snapshot())
        elif self.host:
            await self.send(self.host, {"kind": "requestSnapshot"})
        print(f"[room {self.id}] {client.name} joined as {role}")
        return True

    async def remove(self, client: Client) -> None:
        if client in self.clients:
            self.clients.remove(client)
        await self.broadcast({"kind": "chat", "name": "系统", "text": f"{client.name} 已离开世界"})
        if self.host is client:
            self.host = self.clients[0] if self.clients else None
            if self.host:
                await self.send(self.host, {"kind": "welcome", "role": "host", "room": self.id, "clients": len(self.clients), "canBuild": self.guest_can_build})
        print(f"[room {self.id}] {client.name} left")

    async def process(self, client: Client, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind == "_chunk":
            chunk_id = str(payload.get("id", ""))[:80]
            total = int(payload.get("total", 0))
            index = int(payload.get("i", -1))
            if not chunk_id or not (0 < total <= 1000) or not (0 <= index < total):
                return
            entry = client.chunks.setdefault(chunk_id, {"parts": [None] * total, "count": 0})
            if len(entry["parts"]) != total:
                return
            if entry["parts"][index] is None:
                entry["parts"][index] = str(payload.get("data", ""))
                entry["count"] += 1
            if entry["count"] == total:
                client.chunks.pop(chunk_id, None)
                try:
                    await self.process(client, json.loads("".join(entry["parts"])))
                except Exception:
                    await self.send(client, {"kind": "error", "message": "大型同步数据解析失败"})
            return

        if kind == "hello":
            client.name = str(payload.get("name") or client.name)[:18]
            await self.broadcast({"kind": "hello", "name": client.name}, exclude=client)
            return

        if kind == "permission":
            if client is self.host:
                self.guest_can_build = bool(payload.get("canBuild", True))
                self.schedule_save()
                await self.broadcast({"kind": "permission", "canBuild": self.guest_can_build})
            return

        if kind == "kick":
            if client is self.host:
                for other in list(self.clients):
                    if other is not self.host:
                        await self.send(other, {"kind": "kicked", "message": "主机已将你移出房间"})
                        await other.ws.close(code=4004, message=b"kicked")
            return

        if kind == "snapshot":
            if client is not self.host:
                await self.send(client, {"kind": "error", "message": "只有主机可以覆盖完整世界"})
                return
            rows = payload.get("world")
            if not isinstance(rows, list) or len(rows) > 30000:
                return
            world: dict[str, int] = {}
            for row in rows:
                if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str):
                    try:
                        world[row[0]] = int(row[1])
                    except (TypeError, ValueError):
                        pass
            self.world = world
            self.shared = {k: v for k, v in payload.items() if k not in {"kind", "world"}}
            self.schedule_save()
            await self.broadcast(self.snapshot(), exclude=client)
            return

        if kind == "block":
            if client is not self.host and not self.guest_can_build:
                await self.send(client, {"kind": "permission", "canBuild": False})
                return
            try:
                x, y, z, block = (int(payload[k]) for k in ("x", "y", "z", "t"))
            except Exception:
                return
            if not (0 <= x < 64 and 0 <= z < 64 and 0 <= y <= 128 and 0 <= block <= 64):
                return
            key = f"{x},{y},{z}"
            if block:
                self.world[key] = block
            else:
                self.world.pop(key, None)
            self.schedule_save()
            await self.broadcast({"kind": "block", "x": x, "y": y, "z": z, "t": block}, exclude=client)
            return

        if kind == "shared":
            if client is not self.host and not self.guest_can_build:
                await self.send(client, {"kind": "permission", "canBuild": False})
                return
            self.shared = {k: v for k, v in payload.items() if k != "kind"}
            self.schedule_save()
            await self.broadcast(payload, exclude=client)
            return

        if kind == "syncRequest":
            await self.send(client, self.snapshot())
            return

        if kind in {"chat", "player", "ping", "pong", "checksum"}:
            if kind == "chat":
                payload = {
                    "kind": "chat",
                    "name": client.name,
                    "text": str(payload.get("text", ""))[:120],
                }
            await self.broadcast(payload, exclude=client)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app["root"] / "index.html")


async def rooms_api(request: web.Request) -> web.Response:
    rooms: dict[str, Room] = request.app["rooms"]
    payload = [{"id": r.id, "players": len(r.clients), "blocks": len(r.world), "locked": bool(r.password_hash)} for r in rooms.values()]
    return web.json_response({"rooms": payload}, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    room_id = request.query.get("room", "home").strip()
    name = request.query.get("name", "旅人").strip()[:18] or "旅人"
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_MESSAGE)
    await ws.prepare(request)
    if not ROOM_RE.match(room_id):
        await ws.send_str(safe_json({"kind": "error", "message": "房间名只能使用字母、数字、下划线或横线"}))
        await ws.close(code=4002, message=b"invalid room")
        return ws

    rooms: dict[str, Room] = request.app["rooms"]
    room = rooms.get(room_id)
    if room is None:
        room = Room(room_id, request.app["data_dir"])
        rooms[room_id] = room
    password = request.query.get("password", "")[:64]
    supplied_hash = hashlib.sha256(password.encode("utf-8")).hexdigest() if password else ""
    if room.password_hash and supplied_hash != room.password_hash:
        await ws.send_str(safe_json({"kind": "error", "message": "房间密码错误"}))
        await ws.close(code=4005, message=b"wrong password")
        return ws
    if not room.password_hash and password:
        if room.clients:
            await ws.send_str(safe_json({"kind": "error", "message": "该房间创建时未设置密码，请将密码留空"}))
            await ws.close(code=4006, message=b"unexpected password")
            return ws
        room.password_hash = supplied_hash
        room.schedule_save()
    client = Client(ws=ws, name=name)
    if not await room.add(client):
        return ws

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if len(msg.data) > MAX_MESSAGE:
                    await ws.close(code=4003, message=b"message too large")
                    break
                try:
                    payload = json.loads(msg.data)
                    if isinstance(payload, dict):
                        await room.process(client, payload)
                except json.JSONDecodeError:
                    await room.send(client, {"kind": "error", "message": "JSON 消息格式错误"})
            elif msg.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                break
    finally:
        await room.remove(client)
    return ws


def make_app(root: Path, data_dir: Path) -> web.Application:
    app = web.Application(client_max_size=MAX_MESSAGE)
    app["root"] = root
    app["data_dir"] = data_dir
    app["rooms"] = {}
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/rooms", rooms_api)
    app.router.add_get("/ws", websocket_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="MINICRAFT 双人共建服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="HTTP/WebSocket 端口，默认 8765")
    parser.add_argument("--data-dir", default="server_data", help="房间存档目录")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    data_dir = (root / args.data_dir).resolve()
    print(f"MINICRAFT server: http://{args.host}:{args.port}")
    print(f"Room saves: {data_dir}")
    web.run_app(make_app(root, data_dir), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
