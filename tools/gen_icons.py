#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成达芬奇风格图标（原创设计）：深色圆底 + RGB 三色环 + 中心向下箭头。

说明：本图标为原创绘制，仅采用与 DaVinci Resolve / Proxy Generator 一致的
「深色圆 + RGB 色环」视觉语言，不复制 Blackmagic Design 的官方图标素材。"""
import math
import os
import struct
import zlib


def write_png(path, w, h, pixel_fn):
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            r, g, b, a = pixel_fn(x, y)
            row += bytes((r, g, b, a))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        c += struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        return c

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def hsv2rgb(h, s, v):
    h = h % 360.0
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


def in_triangle(px, py, p0, p1, p2):
    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])
    d1 = sign((px, py), p0, p1)
    d2 = sign((px, py), p1, p2)
    d3 = sign((px, py), p2, p0)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def make_icon(size):
    s = float(size)
    cx = cy = s / 2.0
    R_bg = s * 0.48       # 圆底半径
    R_o = s * 0.445       # 色环外半径
    R_i = s * 0.285       # 色环内半径
    dark = (18, 18, 22)   # 近黑圆底
    white = (245, 246, 250)
    aa = 1.5              # 抗锯齿过渡带（像素）

    def pixel(x, y):
        dx = x - cx
        dy = y - cy
        dist = math.hypot(dx, dy)
        if dist >= R_bg + aa:
            return 0, 0, 0, 0
        if dist > R_bg - aa:
            alpha = int(max(0.0, (R_bg + aa - dist) / (2 * aa)) * 255)
        else:
            alpha = 255
        # 色环（RGB 三色，红在顶部）
        if R_i <= dist <= R_o:
            ang = math.degrees(math.atan2(dy, dx)) + 90.0
            r, g, b = hsv2rgb(ang, 0.82, 0.92)
            return r, g, b, alpha
        # 中心深色盘 + 白色向下箭头（竖条 + 三角头）
        r, g, b = dark
        if abs(dx) <= 0.075 * s and -0.13 * s <= dy <= 0.06 * s:
            r, g, b = white
        p0 = (cx, cy + 0.28 * s)
        p1 = (cx - 0.21 * s, cy + 0.05 * s)
        p2 = (cx + 0.21 * s, cy + 0.05 * s)
        if in_triangle(x, y, p0, p1, p2):
            r, g, b = white
        return r, g, b, alpha

    return pixel


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    write_png(os.path.join(base, "ICON.PNG"), 256, 256, make_icon(256))
    write_png(os.path.join(base, "ICON_256.PNG"), 256, 256, make_icon(256))
    write_png(os.path.join(base, "app", "ui", "images", "icon_64.png"), 64, 64, make_icon(64))
    write_png(os.path.join(base, "app", "ui", "images", "icon_256.png"), 256, 256, make_icon(256))
    print("icons generated (DaVinci-style)")


if __name__ == "__main__":
    main()
