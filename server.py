#!/usr/bin/env python3
"""MINICRAFT room server: static web hosting + authoritative WebSocket rooms."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_CLIENTS = 8
MAX_MESSAGE = 32 * 1024 * 1024


def safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Client:
    ws: web.WebSocketResponse
    name: str
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    can_build: bool = True
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
        self.dirty = False
        self.dirty_since = 0.0
        self.last_dirty = 0.0
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
        try:
            loaded_version = int(self.shared.get("worldVersion", 0) or 0)
        except (TypeError, ValueError):
            loaded_version = 0
        if self.world and loaded_version < 13:
            self._migrate_legacy_world()
            self._save_now()

    @staticmethod
    def _underground_type(x: int, y: int, z: int) -> int:
        depth = max(1, -y)
        h = (((x + 17) * 73856093) & 0xFFFFFFFF) ^ (((z + 31) * 19349663) & 0xFFFFFFFF) ^ (((depth + 7) * 83492791) & 0xFFFFFFFF)
        h &= 0xFFFFFFFF
        if (h + depth * 19) % 53 == 0 or (depth >= 7 and (h >> 4) % 41 == 0):
            return 20
        if depth <= 2:
            return 3 if h % 7 == 0 else 35
        if depth <= 5:
            return 35 if h % 5 == 0 else 3
        return 35 if h % 19 == 0 else 3

    @staticmethod
    def _outer_hash(x: int, z: int) -> int:
        return ((((x + 41) * 1103515245) & 0xFFFFFFFF) ^ (((z + 73) * 12345) & 0xFFFFFFFF)) & 0xFFFFFFFF

    def _migrate_legacy_world(self) -> None:
        def get(x: int, y: int, z: int) -> int:
            return self.world.get(f"{x},{y},{z}", 0)

        def set_block(x: int, y: int, z: int, block: int) -> None:
            key = f"{x},{y},{z}"
            if block:
                self.world[key] = block
            else:
                self.world.pop(key, None)

        # Open the former 64x64 hedge so the old world connects to the frontier.
        for i in range(64):
            if get(63, 1, i) == 6:
                set_block(63, 1, i, 0)
            if get(i, 1, 63) == 6:
                set_block(i, 1, 63, 0)

        for x in range(128):
            for z in range(128):
                outer = x >= 64 or z >= 64
                if outer and not get(x, 0, z):
                    set_block(x, 0, z, 1)
                if outer:
                    for y in range(-10, 0):
                        if not get(x, y, z):
                            set_block(x, y, z, self._underground_type(x, y, z))
                else:
                    if get(x, -2, z) == 36:
                        set_block(x, -2, z, self._underground_type(x, -2, z))
                    for y in range(-3, -11, -1):
                        if not get(x, y, z):
                            set_block(x, y, z, self._underground_type(x, y, z))
                if not get(x, -11, z):
                    set_block(x, -11, z, 36)

        # Surface biomes and water bodies mirror the browser-side migration.
        for x in range(1, 127):
            for z in range(1, 127):
                if x < 64 and z < 64:
                    continue
                current = get(x, 0, z)
                if not current or current == 1:
                    h = self._outer_hash(x, z)
                    surface = 21 if x >= 96 and z < 45 else 22 if z >= 96 else 3 if h % 37 == 0 else 1
                    set_block(x, 0, z, surface)
        for x in range(75, 100):
            for z in range(69, 94):
                if ((x - 87) ** 2 + (z - 81) ** 2) ** 0.5 < 10.2:
                    set_block(x, 0, z, 18)
        for x in range(108, 123):
            for z in range(104, 120):
                if ((x - 115) ** 2 + (z - 111) ** 2) ** 0.5 < 5.4:
                    set_block(x, 0, z, 18)
        for z in range(64, 127):
            set_block(11, 0, z, 11); set_block(12, 0, z, 11)
        for x in range(56, 113):
            set_block(x, 0, 60, 11); set_block(x, 0, 61, 11)
        for z in range(42, 69):
            set_block(103, 0, z, 11); set_block(104, 0, z, 11)

        def tree(x: int, z: int, snow: bool) -> None:
            height = 2 + self._outer_hash(x, z) % 3
            for y in range(1, height + 1):
                if not get(x, y, z): set_block(x, y, z, 5)
            for dx in range(-2, 3):
                for dz in range(-2, 3):
                    for dy in range(3):
                        if abs(dx) + abs(dz) + dy < 5 and not get(x + dx, height + dy, z + dz):
                            set_block(x + dx, height + dy, z + dz, 6)
            if snow and get(x, height + 2, z) == 6:
                set_block(x, height + 3, z, 21)

        for x in range(66, 125, 4):
            for z in range(4, 125, 4):
                ground, h = get(x, 0, z), self._outer_hash(x, z)
                if ground in (1, 21) and h % 7 == 0 and not get(x, 1, z) and abs(z - 60) > 2:
                    tree(x, z, ground == 21)
                elif ground in (22, 3) and h % 43 == 0:
                    set_block(x, 1, z, 3 if h % 2 else 20)
        for n, (cx, cy, cz) in enumerate(((76, 11, 52), (103, 13, 73), (112, 10, 28))):
            for dx in range(5 + n % 2):
                set_block(cx + dx, cy, cz, 14)
            set_block(cx + 2, cy + 1, cz, 14)
        for i in range(128):
            if i % 7 != 3:
                set_block(i, 1, 0, 6); set_block(i, 1, 127, 6)
            if i % 8 != 4:
                set_block(0, 1, i, 6); set_block(127, 1, i, 6)
        self.shared.update({"worldVersion": 13, "worldWidth": 128, "undergroundDepth": 10})
        print(f"[room {self.id}] migrated to 128x128 with 10 underground layers")

    def schedule_save(self) -> None:
        now = time.monotonic()
        if not self.dirty:
            self.dirty_since = now
        self.dirty = True
        self.last_dirty = now
        if not self.save_task or self.save_task.done():
            self.save_task = asyncio.create_task(self._save_worker())

    async def _save_worker(self) -> None:
        try:
            while self.dirty:
                await asyncio.sleep(0.5)
                now = time.monotonic()
                quiet_for = now - self.last_dirty
                dirty_for = now - self.dirty_since
                if quiet_for < 60.0 and dirty_for < 60.0:
                    continue
                self._save_now()
                await self.broadcast({"kind": "serverSaved", "at": time.time()})
                self.dirty = False
                self.dirty_since = 0.0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[room {self.id}] save worker failed: {exc}")

    def _save_now(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                safe_json({"world": self.world, "shared": self.shared, "password_hash": self.password_hash, "guest_can_build": self.guest_can_build, "updated": time.time()}),
                "utf-8",
            )
            tmp.replace(self.path)
            print(f"[room {self.id}] saved {len(self.world)} blocks")
        except Exception as exc:
            print(f"[room {self.id}] save failed: {exc}")

    async def flush(self) -> None:
        if self.save_task and not self.save_task.done():
            self.save_task.cancel()
        if self.dirty:
            self._save_now()
            self.dirty = False
            self.dirty_since = 0.0

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
            await client.ws.send_str(safe_json({"kind": "error", "message": f"房间已满（最多 {MAX_CLIENTS} 人）"}))
            await client.ws.close(code=4001, message=b"room full")
            return False
        self.clients.append(client)
        if self.host is None:
            self.host = client
        client.can_build = client is self.host or self.guest_can_build
        role = "host" if client is self.host else "guest"
        await self.send(client, {"kind": "welcome", "role": role, "room": self.id, "clients": len(self.clients), "canBuild": client.can_build, "selfId": client.id})
        await self.broadcast({"kind": "hello", "name": client.name, "clientId": client.id}, exclude=client)
        if self.world:
            await self.send(client, self.snapshot())
        elif self.host:
            await self.send(self.host, {"kind": "requestSnapshot"})
        await self.broadcast({"kind": "roomInfo", "clients": len(self.clients), "host": self.host.name if self.host else "", "players": [{"id": c.id, "name": c.name, "canBuild": c.can_build, "role": "host" if c is self.host else "guest"} for c in self.clients]})
        print(f"[room {self.id}] {client.name} joined as {role}")
        return True

    async def remove(self, client: Client) -> None:
        if client in self.clients:
            self.clients.remove(client)
        await self.broadcast({"kind": "chat", "name": "系统", "text": f"{client.name} 已离开世界"})
        if self.host is client:
            self.host = self.clients[0] if self.clients else None
            if self.host:
                self.host.can_build = True
                await self.send(self.host, {"kind": "welcome", "role": "host", "room": self.id, "clients": len(self.clients), "canBuild": self.guest_can_build, "selfId": self.host.id})
        await self.broadcast({"kind": "roomInfo", "clients": len(self.clients), "host": self.host.name if self.host else "", "players": [{"id": c.id, "name": c.name, "canBuild": c.can_build, "role": "host" if c is self.host else "guest"} for c in self.clients]})
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
            await self.broadcast({"kind": "hello", "name": client.name, "clientId": client.id}, exclude=client)
            return

        if kind == "permission":
            if client is self.host:
                allowed = bool(payload.get("canBuild", True))
                target_id = str(payload.get("clientId", ""))
                targets = [c for c in self.clients if c is not self.host and (not target_id or c.id == target_id)]
                if not target_id:
                    self.guest_can_build = allowed
                for target in targets:
                    target.can_build = allowed
                    await self.send(target, {"kind": "permission", "canBuild": allowed, "clientId": target.id})
                self.schedule_save()
                await self.broadcast({"kind": "roomInfo", "clients": len(self.clients), "host": self.host.name, "players": [{"id": c.id, "name": c.name, "canBuild": c.can_build, "role": "host" if c is self.host else "guest"} for c in self.clients]})
            return

        if kind == "kick":
            if client is self.host:
                target_id = str(payload.get("clientId", ""))
                for other in list(self.clients):
                    if other is not self.host and (not target_id or other.id == target_id):
                        await self.send(other, {"kind": "kicked", "message": "主机已将你移出房间"})
                        await other.ws.close(code=4004, message=b"kicked")
            return

        if kind == "snapshot":
            if client is not self.host:
                await self.send(client, {"kind": "error", "message": "只有主机可以覆盖完整世界"})
                return
            rows = payload.get("world")
            if not isinstance(rows, list) or len(rows) > 300000:
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
            try:
                incoming_version = int(self.shared.get("worldVersion", 0) or 0)
            except (TypeError, ValueError):
                incoming_version = 0
            if incoming_version < 13:
                self._migrate_legacy_world()
            self.schedule_save()
            await self.broadcast(self.snapshot(), exclude=client)
            return

        if kind == "blocks":
            if client is not self.host and not client.can_build:
                await self.send(client, {"kind": "permission", "canBuild": False})
                return
            incoming = payload.get("items")
            if not isinstance(incoming, list):
                return
            clean = []
            for item in incoming[:512]:
                try:
                    x, y, z, block = (int(item[k]) for k in ("x", "y", "z", "t"))
                except Exception:
                    continue
                if not (0 <= x < 128 and 0 <= z < 128 and -11 <= y <= 128 and 0 <= block <= 64):
                    continue
                key = f"{x},{y},{z}"
                if block:
                    self.world[key] = block
                else:
                    self.world.pop(key, None)
                clean.append({"x": x, "y": y, "z": z, "t": block})
            if clean:
                self.schedule_save()
                await self.broadcast({"kind": "blocks", "items": clean}, exclude=client)
            return

        if kind == "block":
            if client is not self.host and not client.can_build:
                await self.send(client, {"kind": "permission", "canBuild": False})
                return
            try:
                x, y, z, block = (int(payload[k]) for k in ("x", "y", "z", "t"))
            except Exception:
                return
            if not (0 <= x < 128 and 0 <= z < 128 and -11 <= y <= 128 and 0 <= block <= 64):
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
            if client is not self.host and not client.can_build:
                await self.send(client, {"kind": "permission", "canBuild": False})
                return
            clean = {k: v for k, v in payload.items() if k != "kind"}
            if client is not self.host:
                for key in ("worldTime", "dayCount", "raining", "weatherTimer", "adventureMode"):
                    clean.pop(key, None)
            self.shared.update(clean)
            self.schedule_save()
            await self.broadcast({"kind": "shared", **clean}, exclude=client)
            return

        if kind == "environment":
            if client is not self.host:
                return
            clean = {k: payload.get(k) for k in ("worldTime", "dayCount", "raining", "weatherTimer", "adventureMode", "cat", "trackTrain")}
            self.shared.update(clean)
            self.schedule_save()
            await self.broadcast({"kind": "environment", **clean}, exclude=client)
            return

        if kind == "syncRequest":
            await self.send(client, self.snapshot())
            return

        if kind == "chat":
            await self.broadcast({"kind": "chat", "name": client.name, "clientId": client.id, "text": str(payload.get("text", ""))[:120]}, exclude=client)
            return

        if kind == "player":
            clean = {"kind": "player", "clientId": client.id, "name": client.name}
            for key in ("x", "y", "z", "yaw", "pitch"):
                try:
                    clean[key] = float(payload.get(key, 0))
                except (TypeError, ValueError):
                    clean[key] = 0.0
            clean["style"] = int(payload.get("style", 0) or 0)
            clean["selected"] = int(payload.get("selected", 1) or 1)
            clean["flying"] = bool(payload.get("flying", False))
            clean["mining"] = bool(payload.get("mining", False))
            await self.broadcast(clean, exclude=client)
            return

        if kind == "ping":
            await self.send(client, {"kind": "pong", "id": payload.get("id")})
            return

        if kind == "checksum":
            await self.broadcast(payload, exclude=client)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app["root"] / "index.html")


async def rooms_api(request: web.Request) -> web.Response:
    rooms: dict[str, Room] = request.app["rooms"]
    payload = [{"id": r.id, "players": len(r.clients), "capacity": MAX_CLIENTS, "blocks": len(r.world), "locked": bool(r.password_hash)} for r in rooms.values()]
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


async def cleanup_rooms(app: web.Application) -> None:
    for room in list(app["rooms"].values()):
        await room.flush()


def make_app(root: Path, data_dir: Path) -> web.Application:
    app = web.Application(client_max_size=MAX_MESSAGE)
    app["root"] = root
    app["data_dir"] = data_dir
    app["rooms"] = {}
    app.on_cleanup.append(cleanup_rooms)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/rooms", rooms_api)
    app.router.add_get("/ws", websocket_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="MINICRAFT 多人共建服务器")
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
