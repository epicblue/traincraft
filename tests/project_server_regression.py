#!/usr/bin/env python3
"""TRAINCRAFT 项目结构、静态资源、存档格式与 Python 服务器回归测试。"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
PASSED = 0


def check(name: str, fn) -> None:
    global PASSED
    fn()
    PASSED += 1
    print(f"✓ {name}")


def load_server_module():
    path = ROOT / "server.py"
    spec = importlib.util.spec_from_file_location("traincraft_server_regression", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Python 3.13 dataclasses expects the module to be registered during execution.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SERVER = load_server_module()


def test_split_project_integrity() -> None:
    html_path = ROOT / "index.html"
    css_path = ROOT / "assets" / "traincraft.css"
    js_path = ROOT / "assets" / "traincraft.js"
    for path in (html_path, css_path, js_path):
        assert path.is_file() and path.stat().st_size > 100, path

    html = html_path.read_text("utf-8")
    css = css_path.read_text("utf-8")
    js = js_path.read_text("utf-8")
    assert html_path.stat().st_size < 50_000, "index.html became too large again"
    assert "<style>" not in html and "<script>" not in html
    assert 'href="assets/traincraft.css?v=' in html
    assert 'src="assets/traincraft.js?v=' in html
    assert "#trackKeyboardPalette" in css
    assert js.count("(() => {") == 1 and js.rstrip().endswith("})();")
    assert "http://" not in css and "https://" not in css

    version_query = re.search(r'traincraft\.js\?v=(\d+)', html)
    feature_version = re.search(r'trackFeatureVersion:(\d+)', js)
    assert version_query and feature_version
    assert version_query.group(1) == feature_version.group(1)

    ids = set(re.findall(r'\bid="([^"]+)"', html))
    refs = set(re.findall(r"getElementById\('([^']+)'\)", js))
    assert not (refs - ids), f"missing DOM ids: {sorted(refs - ids)}"
    assert len(ids) >= 200 and len(refs) >= 200


def test_backup_gzip_roundtrip() -> None:
    backup: dict[str, Any] = {
        "v": 3,
        "backupFormat": "TRAINCRAFT_BACKUP",
        "worldVersion": 13,
        "worldWidth": 128,
        "undergroundDepth": 10,
        "trackFeatureVersion": 41,
        "world": [["1,0,1", 1], ["4,1,4", 37], ["4,-11,4", 36]],
        "trackPieces": [
            {"id": 1, "x": 4, "y": 1, "z": 4, "shape": "turntable", "turntableAxis": 1, "turntableExit": 1},
            {"id": 2, "x": 5, "y": 1, "z": 4, "shape": "detector", "detectorMode": 3, "detectorCount": 7, "detectorTargetId": 1},
            {"id": 3, "x": 6, "y": 1, "z": 4, "shape": "depot", "depotStop": True},
            {"id": 4, "x": 8, "y": 1, "z": 4, "shape": "station", "stationStop": True, "stationDwell": 8, "stationOrder": 3, "stationService": False},
        ],
        "customTrackTemplates": [{"id": "custom-test", "name": "测试蓝图", "specs": [{"x": 0, "y": 0, "z": 0, "rot": 0, "shape": "straight"}]}],
    }
    raw = json.dumps(backup, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(raw)
    restored = json.loads(gzip.decompress(packed).decode("utf-8"))
    assert restored == backup
    assert packed[:2] == b"\x1f\x8b"


def make_legacy_world() -> dict[str, int]:
    world: dict[str, int] = {}
    for x in range(64):
        for z in range(64):
            world[f"{x},0,{z}"] = 1
            if (x, z) != (3, 3):  # An intentionally mined shallow hole.
                world[f"{x},-1,{z}"] = 3
            world[f"{x},-2,{z}"] = 36
    for i in range(64):
        world[f"63,1,{i}"] = 6
        world[f"{i},1,63"] = 6
    world["10,2,10"] = 4  # A player building that must survive migration.
    return world


def test_server_world_migration() -> None:
    with tempfile.TemporaryDirectory(prefix="traincraft-regression-") as tmp:
        data_dir = Path(tmp)
        room_path = data_dir / "legacy.json"
        original = make_legacy_world()
        room_path.write_text(
            json.dumps({"world": original, "shared": {"worldVersion": 12}}, ensure_ascii=False),
            "utf-8",
        )
        room = SERVER.Room("legacy", data_dir)
        assert room.shared["worldVersion"] == 13
        assert room.shared["worldWidth"] == 128
        assert room.shared["undergroundDepth"] == 10
        assert room.world["10,2,10"] == 4
        assert "3,-1,3" not in room.world, "mined shallow hole was refilled"
        assert room.world["4,-2,4"] != 36, "old shallow bedrock was not opened"
        assert room.world["4,-11,4"] == 36
        assert room.world["100,-11,100"] == 36
        assert room.world["100,-10,100"] in {3, 20, 35}
        assert room.world["87,0,81"] == 18  # Large lake.
        assert room.world["11,0,90"] == 11  # Extended road.
        assert room.world.get("63,1,20") != 6 and room.world.get("20,1,63") != 6
        assert room_path.is_file() and not room_path.with_suffix(".tmp").exists()
        persisted = json.loads(room_path.read_text("utf-8"))
        assert persisted["shared"]["worldVersion"] == 13
        assert len(room.world) > 190_000


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.messages: list[str] = []

    async def send_str(self, text: str) -> None:
        self.messages.append(text)

    async def close(self, **_kwargs) -> None:
        self.closed = True


async def test_server_message_validation_async() -> None:
    with tempfile.TemporaryDirectory(prefix="traincraft-message-") as tmp:
        room = SERVER.Room("messages", Path(tmp))
        room.schedule_save = lambda: None  # type: ignore[method-assign]
        host = SERVER.Client(FakeWebSocket(), "host")
        guest = SERVER.Client(FakeWebSocket(), "guest", can_build=False)
        room.clients = [host, guest]
        room.host = host

        await room.process(host, {"kind": "block", "x": 127, "y": 128, "z": 127, "t": 37})
        assert room.world["127,128,127"] == 37
        await room.process(host, {"kind": "block", "x": 128, "y": 1, "z": 1, "t": 1})
        await room.process(host, {"kind": "block", "x": 1, "y": -12, "z": 1, "t": 1})
        assert "128,1,1" not in room.world and "1,-12,1" not in room.world

        await room.process(guest, {"kind": "block", "x": 5, "y": 1, "z": 5, "t": 2})
        assert "5,1,5" not in room.world
        assert any(json.loads(message).get("kind") == "permission" for message in guest.ws.messages)

        # Cat commands remain independent from build permission.
        await room.process(guest, {"kind": "catCommand", "mode": "stay", "stayX": 300, "stayZ": -5})
        cat = room.shared["cat"]
        assert cat["mode"] == "stay" and cat["stayX"] == 128.0 and cat["stayZ"] == 0.0
        await room.process(guest, {"kind": "catRide", "ridingTrain": True})
        assert room.shared["cat"]["ridingTrain"] is True

        guest.ws.messages.clear()
        await room.process(host, {"kind": "player", "x": 1, "y": 2, "z": 3, "yaw": 0, "pitch": 0, "ridingTrain": True})
        relayed = [json.loads(message) for message in guest.ws.messages if json.loads(message).get("kind") == "player"]
        assert relayed and relayed[-1]["ridingTrain"] is True

        # Batch processing is capped at 512 valid block edits.
        rows = [{"x": i % 128, "y": 1, "z": (i // 128) % 128, "t": 2} for i in range(700)]
        await room.process(host, {"kind": "blocks", "items": rows})
        edited = sum(1 for value in room.world.values() if value == 2)
        assert edited == 512


def test_server_message_validation() -> None:
    asyncio.run(test_server_message_validation_async())


async def test_http_assets_async() -> None:
    with tempfile.TemporaryDirectory(prefix="traincraft-http-") as tmp:
        app = SERVER.make_app(ROOT, Path(tmp))
        async with TestClient(TestServer(app)) as client:
            cases = [
                ("/", "text/html", "assets/traincraft.js?v=41"),
                ("/index.html", "text/html", "TRAINCRAFT"),
                ("/assets/traincraft.css?v=41", "text/css", "#trackKeyboardPalette"),
                ("/assets/traincraft.js?v=41", "javascript", "trackFeatureVersion:41"),
                ("/api/rooms", "application/json", "rooms"),
            ]
            for url, content_type, needle in cases:
                response = await client.get(url)
                text = await response.text()
                assert response.status == 200, (url, response.status)
                assert content_type in response.headers.get("Content-Type", "")
                assert needle in text
            missing = await client.get("/assets/not-found.js")
            assert missing.status == 404
            traversal = await client.get("/assets/../server.py")
            assert traversal.status == 404


def test_http_assets() -> None:
    asyncio.run(test_http_assets_async())


def test_server_limits_and_room_names() -> None:
    assert SERVER.MAX_CLIENTS == 8
    assert SERVER.MAX_MESSAGE == 32 * 1024 * 1024
    for valid in ("home", "family-home", "room_8", "A" * 32):
        assert SERVER.ROOM_RE.fullmatch(valid)
    for invalid in ("", "has space", "../escape", "A" * 33, "中文"):
        assert not SERVER.ROOM_RE.fullmatch(invalid)
    encoded = SERVER.safe_json({"text": "橘子", "value": 1})
    assert "橘子" in encoded and " " not in encoded


def main() -> None:
    check("拆分后的HTML/CSS/JS结构、版本和DOM引用", test_split_project_integrity)
    check("JSON/gzip备份格式往返", test_backup_gzip_roundtrip)
    check("旧64×64世界迁移到128×128与10层地下", test_server_world_migration)
    check("服务器坐标、权限、批量上限与猫命令", test_server_message_validation)
    check("HTTP入口、静态资源、房间API与路径保护", test_http_assets)
    check("服务器容量、消息上限、房间名与中文JSON", test_server_limits_and_room_names)
    print(f"\n项目与服务器回归测试完成：{PASSED} 组全部通过。")


if __name__ == "__main__":
    main()
