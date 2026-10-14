"""把点位场景画成监视画面。不依赖 OpenCV / Pillow，用 numpy 生成 PNG。"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime, timezone

import numpy as np

from youju.vision.catalog import SCENES

W, H = 960, 540

PALETTES = {
    "night": {"sky": (12, 22, 28), "ground": (28, 36, 34), "accent": (214, 140, 40), "fog": (18, 40, 42)},
    "ember": {"sky": (28, 14, 10), "ground": (42, 22, 16), "accent": (220, 84, 32), "fog": (80, 28, 16)},
    "day": {"sky": (48, 78, 92), "ground": (62, 74, 58), "accent": (236, 200, 96), "fog": (90, 110, 108)},
    "yard": {"sky": (20, 28, 36), "ground": (46, 52, 44), "accent": (232, 196, 64), "fog": (36, 48, 52)},
}


def encode_png(rgb: np.ndarray) -> bytes:
    height, width, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 4)) + chunk(b"IEND", b"")


def _rect(img: np.ndarray, x, y, w, h, color, alpha=1.0):
    hgt, wid = img.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(wid, int(x + w)), min(hgt, int(y + h))
    if x1 <= x0 or y1 <= y0:
        return
    if alpha >= 1:
        img[y0:y1, x0:x1] = color
        return
    patch = img[y0:y1, x0:x1].astype(np.float32)
    img[y0:y1, x0:x1] = np.clip(patch * (1 - alpha) + np.array(color) * alpha, 0, 255).astype(np.uint8)


def _vline(img, x, y0, y1, color, width=2):
    _rect(img, x, y0, width, y1 - y0, color)


def _hline(img, x0, x1, y, color, width=2):
    _rect(img, x0, y, x1 - x0, width, color)


def draw_scene(scenario: str, tick: int, boxes: list[dict] | None = None, title: str = "") -> bytes:
    scene = SCENES.get(scenario) or SCENES["tower"]
    pal = PALETTES[scene["palette"]]
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[:, :] = pal["sky"]
    horizon = int(H * 0.58)
    img[horizon:, :] = pal["ground"]
    for i in range(8):
        _rect(img, 40 + i * 120, 20, 70, horizon - 40, pal["fog"], 0.18 + (i % 3) * 0.05)
    t = tick % 24
    if scenario == "tower":
        _rect(img, 430, 70, 28, horizon - 40, (70, 78, 74))
        _rect(img, 400, 160, 90, 10, (90, 96, 90))
        _rect(img, 400, 260, 90, 10, (90, 96, 90))
        _rect(img, 360 + t, 210, 22, 70, pal["accent"])
        _rect(img, 368 + t, 190, 14, 16, (210, 180, 140))
    elif scenario == "hotwork":
        _rect(img, 80, horizon - 40, W - 160, 50, (36, 24, 20))
        glow = 0.35 + 0.15 * np.sin(tick / 2)
        _rect(img, 220, horizon - 90, 180, 80, pal["accent"], float(glow))
        _rect(img, 250, horizon - 130, 40, 50, (180, 60, 20), 0.7)
        _rect(img, 620, 200, 28, 90, (90, 90, 100), 0.4)
    elif scenario == "gate":
        _rect(img, 80, 80, 80, horizon - 80, (90, 96, 92))
        _rect(img, W - 160, 80, 80, horizon - 80, (90, 96, 92))
        _rect(img, 160, 90, W - 320, 18, (200, 200, 190))
        walk = 180 + (t * 18) % 420
        _rect(img, walk, 230, 24, 70, (40, 48, 70))
        _rect(img, walk + 4, 214, 16, 16, (210, 180, 140))
        _rect(img, walk + 90, 250, 22, 64, pal["accent"])
    else:
        _hline(img, 40, W - 40, 220, pal["accent"], 4)
        arm = 80 + t * 16
        _rect(img, arm, 120, 220, 16, (180, 186, 190))
        _rect(img, arm + 200, 80, 16, 90, (180, 186, 190))
        _rect(img, 120, horizon - 30, 200, 24, (70, 76, 70))

    scan = int((tick * 17) % H)
    _hline(img, 0, W, scan, (120, 220, 200), 1)
    _rect(img, 0, 0, W, 36, (6, 10, 12), 0.72)
    _rect(img, 16, 10, 10, 10, (220, 64, 48))
    _rect(img, W - 120, 8, 104, 20, (20, 28, 24), 0.8)

    now = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    # 时间戳用色块宽度表示秒，前端会叠中文标题
    _rect(img, W - 110, 12, 8 + (int(now[-2:]) % 50), 12, (90, 210, 180))

    for box in boxes or []:
        x, y, bw, bh = box.get("box") or [0, 0, 0, 0]
        color = (234, 179, 8) if box.get("level") != "high" else (239, 68, 68)
        _rect(img, x * W, y * H, bw * W, 3, color)
        _rect(img, x * W, (y + bh) * H - 3, bw * W, 3, color)
        _vline(img, x * W, y * H, (y + bh) * H, color, 3)
        _vline(img, (x + bw) * W - 3, y * H, (y + bh) * H, color, 3)

    if title:
        _rect(img, 36, 10, min(420, 12 * len(title)), 16, (180, 230, 220), 0.35)
    return encode_png(img)
