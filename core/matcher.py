"""模板匹配模块：基于 OpenCV 的多尺度 matchTemplate。"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class MatchResult:
    x: int          # 命中区域左上角(相对截图帧)
    y: int
    w: int
    h: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


class TemplateError(Exception):
    """模板文件缺失或读不出来。

    这种错必须冒到调用方：静默返回空列表的话，用户看到的就是「模板明明配了
    却永远不命中」，根本猜不到是文件被挪走或损坏了。
    """


class TemplateMatcher:
    """加载模板图片并在大图中做匹配，支持多尺度缩放以适配不同分辨率。

    同屏多窗口会反复用同一张模板，故按「路径+修改时间」缓存，换图后自动失效。
    """

    def __init__(self, scales: list[float] | None = None) -> None:
        # 同一台机器上分辨率固定，默认只做原始尺度；跨分辨率再补 0.9/1.1
        self.scales = scales or [1.0]
        self._cache: dict[str, tuple[float, np.ndarray]] = {}

    def _load_gray(self, path: str) -> np.ndarray:
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            raise TemplateError(f"模板文件不存在或无法访问: {path}") from exc
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise TemplateError(f"模板图片无法解码（损坏或格式不支持）: {path}")
        self._cache[path] = (mtime, img)
        return img

    def match(self, frame_bgr: np.ndarray, template_path: str,
              threshold: float = 0.85) -> list[MatchResult]:
        """在 frame 中查找 template，返回所有超过阈值的命中(已做帧内 NMS)。

        模板读不出来时抛 TemplateError，由调用方决定怎么提示用户。
        """
        tpl = self._load_gray(template_path)
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        results: list[MatchResult] = []
        for scale in self.scales:
            if abs(scale - 1.0) < 1e-6:
                scaled = tpl
            else:
                scaled = cv2.resize(tpl, None, fx=scale, fy=scale,
                                    interpolation=cv2.INTER_LINEAR)
            th, tw = scaled.shape[:2]
            if th > gray.shape[0] or tw > gray.shape[1] or th < 5 or tw < 5:
                continue

            res = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= threshold)
            for x, y in zip(xs, ys):
                results.append(MatchResult(int(x), int(y), int(tw), int(th),
                                           float(res[y, x])))

        return nms_matches(results, iou_threshold=0.3)


def nms_matches(matches: list[MatchResult], iou_threshold: float = 0.3) -> list[MatchResult]:
    """对 MatchResult 列表做非极大值抑制，保留置信度更高的框。"""
    if not matches:
        return []
    ordered = sorted(matches, key=lambda m: m.confidence, reverse=True)
    kept: list[MatchResult] = []
    for cand in ordered:
        if all(_iou(cand, k) < iou_threshold for k in kept):
            kept.append(cand)
    return kept


def _iou(a: MatchResult, b: MatchResult) -> float:
    ax2, ay2 = a.x + a.w, a.y + a.h
    bx2, by2 = b.x + b.w, b.y + b.h
    ix1, iy1 = max(a.x, b.x), max(a.y, b.y)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0
