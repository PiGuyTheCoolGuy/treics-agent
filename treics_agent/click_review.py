from __future__ import annotations

import base64
import io
import math

from PIL import Image, ImageDraw, ImageOps


def inspection_region(frame: dict, target: dict) -> dict:
    """Choose a native desktop crop with enough surrounding context to find another control."""
    desktop_width, desktop_height = frame["desktop_width"], frame["desktop_height"]
    x, y = target["x"], target["y"]
    if not 0 <= x < desktop_width or not 0 <= y < desktop_height:
        raise ValueError("The recorded click lies outside the current desktop.")
    width, height = min(512, desktop_width), min(384, desktop_height)
    return {"x": max(0, min(x - width // 2, desktop_width - width)),
            "y": max(0, min(y - height // 2, desktop_height - height)),
            "width": width, "height": height, "scale": 3,
            "desktop_width": desktop_width, "desktop_height": desktop_height}


def image_point(frame: dict, target: dict) -> dict | None:
    source = frame["source"]
    x, y = target["x"] - source["x"], target["y"] - source["y"]
    if not 0 <= x < source["width"] or not 0 <= y < source["height"]:
        return None
    return {"x": min(frame["width"] - 1, round(x * frame["width"] / source["width"])),
            "y": min(frame["height"] - 1, round(y * frame["height"] / source["height"]))}


def marked_image(frame: dict, target: dict) -> str:
    """Mark the last input on the model's current image without changing its geometry."""
    point = image_point(frame, target)
    if point is None:
        return frame["base64"]
    with Image.open(io.BytesIO(base64.b64decode(frame["base64"]))) as original:
        image = original.convert("RGB")
    draw = ImageDraw.Draw(image)
    x, y = point["x"], point["y"]
    draw.ellipse((x - 9, y - 9, x + 9, y + 9), outline="white", width=4)
    draw.ellipse((x - 8, y - 8, x + 8, y + 8), outline="#ff4040", width=2)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85)
    return base64.b64encode(output.getvalue()).decode()


def comparison_image(before: dict | None, after: dict | None, target: dict | None) -> dict:
    """Make reference-only crops around the executed click, without changing action coordinates."""
    if not before or not after or not target:
        return {}
    if any(before.get(key) != after.get(key) for key in ("desktop_width", "desktop_height")):
        return {}
    try:
        sources = [frame["source"] for frame in (before, after)]
        left, top = (max(source[key] for source in sources) for key in ("x", "y"))
        right = min(source["x"] + source["width"] for source in sources)
        bottom = min(source["y"] + source["height"] for source in sources)
        x, y = target["x"], target["y"]
        if not left <= x < right or not top <= y < bottom:
            return {}
        width, height = min(384, right - left), min(256, bottom - top)
        left = max(left, min(x - width // 2, right - width))
        top = max(top, min(y - height // 2, bottom - height))
        region = {"x": left, "y": top, "width": width, "height": height}
        sheet = Image.new("RGB", (640, 280), "#17212e")
        draw = ImageDraw.Draw(sheet)
        for index, (frame, source, label) in enumerate(zip((before, after), sources, ("BEFORE CLICK", "AFTER CLICK"))):
            with Image.open(io.BytesIO(base64.b64decode(frame["base64"], validate=True))) as image:
                sx, sy = image.width / source["width"], image.height / source["height"]
                box = (max(0, math.floor((left - source["x"]) * sx)),
                       max(0, math.floor((top - source["y"]) * sy)),
                       min(image.width, math.ceil((left + width - source["x"]) * sx)),
                       min(image.height, math.ceil((top + height - source["y"]) * sy)))
                crop = image.convert("RGB").crop(box)
                tile = ImageOps.contain(crop, (320, 240), Image.Resampling.LANCZOS)
            ox, oy = index * 320 + (320 - tile.width) // 2, 20 + (240 - tile.height) // 2
            sheet.paste(tile, (ox, oy))
            cx = ox + round(((x - source["x"]) * sx - box[0]) * tile.width / crop.width)
            cy = oy + round(((y - source["y"]) * sy - box[1]) * tile.height / crop.height)
            # Leave the centre clear so the marker does not hide the button or label.
            draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline="white", width=4)
            draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), outline="#ff4040", width=2)
            draw.text((index * 320 + 8, 4), label, fill="white")
        draw.text((8, 264), "Red ring: actual click. Reference crops; use the full screenshot for coordinates.", fill="white")
        output = io.BytesIO()
        sheet.save(output, format="JPEG", quality=85)
        return {"base64": base64.b64encode(output.getvalue()).decode(), "desktop_region": region}
    except (KeyError, ValueError, TypeError, OSError, ZeroDivisionError):
        # Missing visual evidence must not turn delivered input into a tool failure.
        return {}
