"""Draw the app icon: three timeline lanes (dialogue, music, effects) on a dark tile. Writes AppIcon.icns + a PNG."""
import sys
from PIL import Image, ImageDraw

out_icns, out_png = sys.argv[1], sys.argv[2]
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# macOS icon grid: 824px tile centered, ~185px corner radius
pad = 100
tile = (pad, pad, S - pad, S - pad)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):  # soft top-to-bottom gradient
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=(int(38 - 14 * t), int(46 - 16 * t), int(42 - 15 * t), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(tile, radius=185, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
d.rounded_rectangle(tile, radius=185, outline=(255, 255, 255, 28), width=4)

lanes = [
    ((116, 165, 255), [(0.06, 0.52), (0.58, 0.94)]),   # DX
    ((95, 207, 146), [(0.20, 0.86)]),                  # MX
    ((189, 147, 255), [(0.06, 0.30), (0.40, 0.68)]),   # FX
]
x0, x1 = pad + 70, S - pad - 70
lane_h, gap = 150, 58
top = (S - (3 * lane_h + 2 * gap)) // 2
for li, (col, clips) in enumerate(lanes):
    y = top + li * (lane_h + gap)
    d.rounded_rectangle((x0, y + lane_h // 2 - 3, x1, y + lane_h // 2 + 3), radius=3, fill=(255, 255, 255, 30))
    for a, b in clips:
        cx0, cx1 = x0 + (x1 - x0) * a, x0 + (x1 - x0) * b
        d.rounded_rectangle((cx0, y, cx1, y + lane_h), radius=28, fill=col + (255,))
        # waveform ticks inside the clip
        n = int((cx1 - cx0) / 22)
        for k in range(1, n):
            xx = cx0 + k * (cx1 - cx0) / n
            h = (lane_h * 0.18) + (lane_h * 0.22) * abs(((k * 37) % 11) / 10 - 0.5) * 2
            d.rounded_rectangle((xx - 4, y + lane_h / 2 - h, xx + 4, y + lane_h / 2 + h), radius=4, fill=(20, 26, 23, 150))
    # crossfade wedge between DX clips
    if li == 0:
        mx = x0 + (x1 - x0) * 0.55
        d.polygon([(mx - 40, y + lane_h), (mx + 40, y), (mx + 40, y + lane_h)], fill=(240, 166, 64, 255))

img.save(out_png)
img.save(out_icns, sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
print("ok")
