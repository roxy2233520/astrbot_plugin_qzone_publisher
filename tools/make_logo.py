"""生成插件图标 logo.png。

只用 Python 标准库（zlib + struct）手写 PNG，因此不依赖 Pillow 等图像库，
任何人 clone 下来跑一次就能得到完全一致的图标：

    python tools/make_logo.py

图标内容：蓝紫渐变圆角底 + 白色对话气泡 + 三个点 + 右上角星芒，
整体是「说话 / 空间」的抽象表达，小尺寸下也能看清。
AstrBot 官方建议插件 Logo 长宽比 1:1、推荐尺寸 256x256，因此默认按 256 输出。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 256
SUPERSAMPLE = 4

# 渐变起止色（蓝 -> 紫）
GRADIENT_TOP = (46, 125, 247)
GRADIENT_BOTTOM = (124, 77, 255)
ACCENT = (46, 125, 247)
WHITE = (255, 255, 255)

# 以下几何量都以 0~1 的相对坐标定义，乘 SIZE 后即为像素坐标
CORNER_RADIUS = 0.1875
# 对话气泡：圆角矩形 (left, top, right, bottom, radius) + 左下角尾巴
BUBBLE = (0.207, 0.246, 0.793, 0.641, 0.0898)
TAIL = ((0.328, 0.641), (0.453, 0.641), (0.328, 0.758))
# 气泡内的三个点 (cx, cy, r)
DOTS = ((0.363, 0.443, 0.0352), (0.5, 0.443, 0.0352), (0.637, 0.443, 0.0352))
# 右上角星芒 (cx, cy, arm, half_width)
SPARKLE = (0.781, 0.230, 0.0781, 0.0215)


def in_rounded_rect(x: float, y: float, box: tuple, radius: float) -> bool:
    """判断点是否落在圆角矩形内。"""
    left, top, right, bottom = box
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def in_triangle(x: float, y: float, points: tuple) -> bool:
    """判断点是否落在三角形内（重心法）。"""
    (x1, y1), (x2, y2), (x3, y3) = points
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if denominator == 0:
        return False
    a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
    b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
    c = 1 - a - b
    return a >= 0 and b >= 0 and c >= 0


def in_circle(x: float, y: float, cx: float, cy: float, radius: float) -> bool:
    """判断点是否落在圆内。"""
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def in_sparkle(
    x: float, y: float, cx: float, cy: float, arm: float, half_width: float
) -> bool:
    """判断点是否落在四角星芒内（两个细长菱形取并集）。"""
    dx, dy = abs(x - cx), abs(y - cy)
    if dx <= half_width and dy <= arm * (1 - dx / half_width):
        return True
    return dy <= half_width and dx <= arm * (1 - dy / half_width)


def sample(x: float, y: float, scale: float) -> tuple[int, int, int, int]:
    """返回单个采样点的 RGBA。"""
    size = scale - 1
    if not in_rounded_rect(x, y, (0.0, 0.0, size, size), CORNER_RADIUS * scale):
        return (0, 0, 0, 0)

    ratio = min(max((x + y) / (2 * size), 0.0), 1.0)
    color = tuple(
        round(GRADIENT_TOP[i] + (GRADIENT_BOTTOM[i] - GRADIENT_TOP[i]) * ratio)
        for i in range(3)
    )

    for dot_x, dot_y, dot_r in DOTS:
        if in_circle(x, y, dot_x * scale, dot_y * scale, dot_r * scale):
            return (*ACCENT, 255)

    if in_sparkle(
        x,
        y,
        SPARKLE[0] * scale,
        SPARKLE[1] * scale,
        SPARKLE[2] * scale,
        SPARKLE[3] * scale,
    ):
        return (*WHITE, 255)

    bubble = tuple(value * scale for value in BUBBLE[:4])
    tail = tuple((px * scale, py * scale) for px, py in TAIL)
    if in_rounded_rect(x, y, bubble, BUBBLE[4] * scale) or in_triangle(x, y, tail):
        return (*WHITE, 255)

    return (*color, 255)


def render() -> list[bytes]:
    """渲染整张图，返回每行的 RGBA 字节。"""
    step = 1.0 / SUPERSAMPLE
    offset = step / 2
    total = SUPERSAMPLE * SUPERSAMPLE
    rows: list[bytes] = []

    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                y = py + offset + sy * step
                for sx in range(SUPERSAMPLE):
                    x = px + offset + sx * step
                    sr, sg, sb, sa = sample(x, y, SIZE)
                    r += sr
                    g += sg
                    b += sb
                    a += sa
            row += bytes((r // total, g // total, b // total, a // total))
        rows.append(bytes(row))
    return rows


def write_png(path: Path, rows: list[bytes]) -> None:
    """把 RGBA 行写成 PNG 文件（8 位真彩 + Alpha）。"""
    raw = b"".join(b"\x00" + row for row in rows)
    compressed = zlib.compress(raw, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> None:
    """生成插件根目录下的 logo.png。"""
    target = Path(__file__).resolve().parent.parent / "logo.png"
    print(f"生成 {SIZE}x{SIZE} 图标（{SUPERSAMPLE}x{SUPERSAMPLE} 超采样）...")
    rows = render()
    write_png(target, rows)
    print(f"完成: {target} ({target.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
