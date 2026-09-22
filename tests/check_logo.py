"""校验 logo.png 的结构与关键像素。

图标由 tools/make_logo.py 生成，本脚本用于确认生成结果没坏
（PNG 结构、长宽比、渐变与图形位置），不依赖任何图像库。

用法：python tests/check_logo.py [logo.png 路径]
"""

import struct
import sys
import zlib
from pathlib import Path

# Windows 控制台默认 GBK，打印中文与 ✅ / ❌ 会 UnicodeEncodeError，这里统一切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_LOGO = Path(__file__).resolve().parent.parent / "logo.png"
path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOGO)
data = path.read_bytes()

assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG 文件头不对"

pos = 8
idat = b""
ihdr = None
while pos < len(data):
    length = struct.unpack(">I", data[pos : pos + 4])[0]
    tag = data[pos + 4 : pos + 8]
    body = data[pos + 8 : pos + 8 + length]
    crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
    assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag!r} 块 CRC 校验失败"
    if tag == b"IHDR":
        ihdr = struct.unpack(">IIBBBBB", body)
    elif tag == b"IDAT":
        idat += body
    pos += 12 + length

width, height, depth, color_type, _, _, _ = ihdr
print(
    f"尺寸 {width}x{height}｜位深 {depth}｜颜色类型 {color_type}（6=RGBA）｜所有块 CRC 通过"
)

if width != height:
    print("  FAIL  长宽比不是 1:1（AstrBot 官方建议 1:1）")
if width > 512:
    print(f"  warn  尺寸 {width} 偏大，官方推荐 256x256")

raw = zlib.decompress(idat)
stride = width * 4


def pixel(rel_x: float, rel_y: float) -> tuple[int, int, int, int]:
    """按相对坐标（0~1）取像素。"""
    x = min(int(rel_x * width), width - 1)
    y = min(int(rel_y * height), height - 1)
    offset = y * (stride + 1) + 1 + x * 4
    return tuple(raw[offset : offset + 4])


def is_white(rgba: tuple[int, int, int, int]) -> bool:
    return all(channel > 240 for channel in rgba[:3])


def is_blue(rgba: tuple[int, int, int, int]) -> bool:
    return rgba[2] > 180 and rgba[0] < 120


checks = [
    ("PNG 长宽比 1:1", width == height),
    ("左上角透明（圆角）", pixel(0.008, 0.008)[3] < 20),
    ("图标内部不透明", pixel(0.5, 0.78)[3] > 250),
    ("顶部为蓝色渐变", is_blue(pixel(0.5, 0.05))),
    ("底部为紫色渐变", pixel(0.5, 0.95)[2] > 180 and pixel(0.5, 0.95)[0] > 90),
    ("气泡内为白", is_white(pixel(0.5, 0.59))),
    ("气泡中间圆点为主题蓝", is_blue(pixel(0.5, 0.443))),
    ("气泡左侧圆点为主题蓝", is_blue(pixel(0.363, 0.443))),
    ("右上星芒为白", is_white(pixel(0.781, 0.23))),
    ("气泡外仍是渐变（非白）", not is_white(pixel(0.12, 0.47))),
]

ok = True
for name, passed in checks:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    ok = ok and passed

print("logo 校验通过 ✅" if ok else "logo 存在问题 ❌")
raise SystemExit(0 if ok else 1)
