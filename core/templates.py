"""模板管理：把屏幕选区保存成按钮模板图，供模板匹配使用。"""
from __future__ import annotations

import os
import re

import cv2

from core.capture import ScreenCapture
from core.config import BASE_DIR, abs_path

TEMPLATES_DIR = "templates"


def templates_dir() -> str:
    path = os.path.join(BASE_DIR, TEMPLATES_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def suggest_relative_path(name: str) -> str:
    """按目标名生成 templates/xxx.png，重名时自动追加序号。"""
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", name.strip()) or "button"
    used = {f.lower() for f in os.listdir(templates_dir())}
    filename = f"{slug}.png"
    idx = 1
    while filename.lower() in used:
        filename = f"{slug}_{idx}.png"
        idx += 1
    return f"{TEMPLATES_DIR}/{filename}"


def save_region_as_template(region: tuple[int, int, int, int],
                            relative_path: str) -> str:
    """截取屏幕区域并存盘，返回相对路径（写入 targets.yaml 用）。"""
    capture = ScreenCapture()
    try:
        frame = capture.grab(region)
    finally:
        capture.close()
    target = abs_path(relative_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not cv2.imwrite(target, frame):
        raise IOError(f"模板保存失败: {target}")
    return relative_path


def preview_size(relative_path: str) -> tuple[int, int] | None:
    path = abs_path(relative_path)
    if not os.path.exists(path):
        return None
    img = cv2.imread(path)
    if img is None:
        return None
    return img.shape[1], img.shape[0]
