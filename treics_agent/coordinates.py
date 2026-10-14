from __future__ import annotations

import copy
import math


GEOMETRY = {"x", "y", "end_x", "end_y", "width", "height"}


def coordinate_space(model: str, preference: str = "auto") -> str:
    if preference != "auto":
        return preference
    family = model.lower().rsplit("/", 1)[-1]
    return "normalized_1000" if family.startswith(("qwen3-vl", "qwen3vl")) else "pixels"


def to_pixels(action: str, arguments: dict, screen: dict | None, space: str) -> tuple[dict, dict | None]:
    coordinates = {key: value for key, value in arguments.items() if key in GEOMETRY}
    if space == "pixels" or not coordinates:
        return arguments, None
    if not screen:
        raise ValueError("A current screenshot is required for coordinate actions.")
    if any(type(value) is not int or not 0 <= value <= 1000 for value in coordinates.values()):
        raise ValueError("Normalized coordinate arguments must be integers on the 0–1000 grid.")
    converted = dict(arguments)
    width, height = screen["width"], screen["height"]
    if action == "zoom_screenshot":
        x, y, w, h = (arguments[key] for key in ("x", "y", "width", "height"))
        if w < 1 or h < 1 or x + w > 1000 or y + h > 1000:
            raise ValueError("The normalized zoom rectangle must fit inside the current 0–1000 image grid.")
        left, top = math.floor(x * width / 1000), math.floor(y * height / 1000)
        right, bottom = math.ceil((x + w) * width / 1000), math.ceil((y + h) * height / 1000)
        converted.update(x=left, y=top, width=max(1, right - left), height=max(1, bottom - top))
    else:
        for key, value in coordinates.items():
            size = width if key.endswith("x") else height
            converted[key] = min(size - 1, round(value * size / 1000))
    mapping = {"space": space, "model_arguments": coordinates,
               "image_arguments": {key: converted[key] for key in coordinates},
               "image_width": width, "image_height": height, "view": screen.get("view", "desktop")}
    return converted, mapping


def model_screen(screen: dict | None, space: str) -> dict | None:
    if not screen:
        return None
    value = {key: data for key, data in screen.items() if key != "base64"}
    value["coordinate_space"] = space
    if space == "normalized_1000":
        value.update(pixel_width=screen["width"], pixel_height=screen["height"], width=1000, height=1000)
    return value


def model_recovery(recovery: dict | None, screen: dict | None, space: str) -> dict | None:
    if recovery is None or space == "pixels":
        return recovery
    value = copy.deepcopy(recovery)
    for item in [*value.get("failed_attempts", []), value.get("gui_feedback", {}),
                 value.get("repeated_observation", {})]:
        if arguments := item.get("arguments"):
            item["arguments"] = {key: data for key, data in arguments.items() if key not in GEOMETRY}
        if item.get("desktop_target"):
            item["desktop_target_coordinate_space"] = "native_desktop_pixels"
    review = value.get("click_review")
    if review:
        review.pop("arguments", None)
        review["desktop_target_coordinate_space"] = "native_desktop_pixels"
        inspection = review.get("inspection")
        if inspection and screen and (point := inspection.get("last_click_in_image")):
            inspection["last_click_in_image"] = {
                "x": round(point["x"] * 1000 / screen["width"]),
                "y": round(point["y"] * 1000 / screen["height"]),
            }
            inspection["coordinate_space"] = space
    return value


def model_history(observations: list[dict], events: list[dict], space: str) -> list[dict]:
    if space == "pixels":
        return observations
    values = copy.deepcopy(observations)
    mappings = {(event["step"], event["kind"]): event["data"].get("metrics", {}).get("coordinate_mapping")
                for event in events}
    for item in values:
        data = item["data"]
        if "arguments" in data and any(key in data["arguments"] for key in GEOMETRY):
            mapping = mappings.get((item["step"], item["kind"]))
            data["arguments"] = {key: value for key, value in data["arguments"].items() if key not in GEOMETRY}
            if mapping and mapping.get("space") == space:
                data["arguments"].update(mapping["model_arguments"])
                data["arguments_coordinate_space"] = "normalized_1000_of_historical_image"
            else:
                data["historical_coordinates_omitted"] = True
        if click := data.get("result", {}).get("last_click"):
            click.pop("source", None)
            click["coordinate_space"] = "native_desktop_pixels"
    return values
