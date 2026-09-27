"""检测模块：对单个监控窗口的一帧执行模板匹配 + OCR，并管理冷却与坐标换算。"""
from __future__ import annotations

import difflib
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from core.capture import window_at, window_rect_for
from core.config import Monitor, Settings, Target, abs_path
from core.matcher import MatchResult, TemplateError, TemplateMatcher, nms_matches
from core.ocr import OcrEngine, OcrHit, compact_text


@dataclass(frozen=True)
class Region:
    """这一帧要截的屏幕矩形。box=None 表示整屏；error 非空表示这轮没法扫。"""
    box: tuple[int, int, int, int] | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _describe_identity(monitor: Monitor) -> str:
    """把当前配置的窗口身份说成一句人话，用于“暂停扫描”那句提示。"""
    parts = []
    if monitor.window_title.strip():
        parts.append(f"标题含「{monitor.window_title}」")
    if monitor.window_class.strip():
        parts.append(f"类名含「{monitor.window_class}」")
    if monitor.window_process.strip():
        parts.append(f"进程「{monitor.window_process}」")
    return "且".join(parts) or "任何"


def resolve_region(monitor: Monitor,
                   finder: Callable[[Monitor], tuple[int, int, int, int] | None] = window_rect_for) -> Region:
    """把监控窗口配置解析成实际矩形。

    填了 window_title 就跟着窗口走：直播窗口挪位置、改大小、换个直播间都不用重框。
    找不到窗口时不退回整屏——那会把桌面、甚至本程序的界面当成直播画面去点。
    """
    if not monitor.tracked:
        return Region(monitor.region_tuple)
    rect = finder(monitor)
    if rect is None:
        return Region(error=f"找不到{_describe_identity(monitor)}的可见窗口，该窗口暂停扫描。"
                            f"（selftest.py --windows 可以看当前窗口的标题/类名/进程名）")
    origin_x, top, width, height = rect
    crop = int(monitor.crop_bottom)
    if crop:
        if crop >= height:
            return Region(error=f"底部裁切 {crop}px 已经不小于窗口高度 {height}px，"
                                f"画面会被裁没，请把 crop_bottom 调小。")
        height -= crop
    cut_left = int(getattr(monitor, "crop_left", 0) or 0)
    box_left = 0
    if cut_left:
        if cut_left >= width:
            return Region(error=f"左侧裁切 {cut_left}px 已经不小于窗口宽度 {width}px，"
                                f"画面会被裁没，请把 crop_left 调小。")
        box_left = cut_left
        width -= cut_left
    right = int(getattr(monitor, "crop_right", 0) or 0)
    if right:
        if right >= width:
            return Region(error=f"右侧裁切 {right}px 已经不小于窗口宽度 {width}px，"
                                f"画面会被裁没，请把 crop_right 调小。")
        width -= right
    return Region((origin_x + box_left, top, width, height))


@dataclass
class Detection:
    monitor_name: str
    target_name: str
    source: str            # "template" 或 "ocr"
    confidence: float
    box: tuple[int, int, int, int]  # 帧内相对坐标 (x, y, w, h)，用于高亮
    abs_x: int             # 绝对屏幕坐标，已含区域偏移与 click_offset
    abs_y: int


class CooldownTracker:
    """跨线程共享的冷却表：同一按钮在不同窗口各自独立计时。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(monitor_name: str, target_name: str) -> str:
        return f"{monitor_name}|{target_name}"

    def ready(self, monitor_name: str, target: Target) -> bool:
        with self._lock:
            last = self._last.get(self.key(monitor_name, target.name))
        if last is None:
            return True
        return (time.monotonic() - last) >= target.cooldown_sec

    def mark(self, monitor_name: str, target_name: str) -> None:
        with self._lock:
            self._last[self.key(monitor_name, target_name)] = time.monotonic()

    def remaining(self, monitor_name: str, target: Target) -> float:
        with self._lock:
            last = self._last.get(self.key(monitor_name, target.name))
        if last is None:
            return 0.0
        return max(0.0, target.cooldown_sec - (time.monotonic() - last))

    def cooling_gap(self, monitor_name: str, targets: Iterable[Target]) -> float:
        """所有目标都在冷却时，返回还要等多久；有一个能点就返回 0。

        调用方拿它决定「这一帧干脆别扫」：冷却中的目标就算检出也不会被点，
        一帧 0.5~3s 的 OCR 完全白跑。
        """
        delays = [self.remaining(monitor_name, t) for t in targets]
        if not delays or any(d <= 0 for d in delays):
            return 0.0
        return min(delays)

    def reset(self, monitor_name: str | None = None) -> None:
        """不带参数清空全部；带窗口名只清该窗口。"""
        with self._lock:
            if monitor_name is None:
                self._last.clear()
            else:
                prefix = f"{monitor_name}|"
                for key in [k for k in self._last if k.startswith(prefix)]:
                    self._last.pop(key, None)


class SelfWindowGuard:
    """判断某个屏幕点是不是正落在本程序自己的窗口上。

    整屏扫描时 OCR 会把自家界面的「签到」「奖券」列表项和日志文字读成命中，点下去
    还会往日志里追加一行含关键词的文字，下一帧再读到，自激不停。只按矩形判断会误伤
    盖在我们上面的直播窗口，所以直接问 Windows 该点最顶层是谁。
    """

    def __init__(self, probe: Callable[[int, int], int] = window_at) -> None:
        self._probe = probe
        # 界面线程整体替换元组、扫描线程只读：属性赋值本身是原子的，不必加锁
        self._hwnds: tuple[int, ...] = ()

    def update(self, hwnds: Iterable[int]) -> None:
        self._hwnds = tuple({int(h) for h in hwnds if h})

    def contains(self, x: int, y: int) -> bool:
        return bool(self._hwnds) and self._probe(x, y) in self._hwnds


# 刚点过的地方附近，隔多久不再点第二下。直播间一张弹窗里往往同时写着
# 「立即领券」和「奖券」（分属两个目标、各有一份冷却，互不阻塞），真机日志里
# 每张弹窗就被点两次：19:42:57 兑换券 (1503,678) → 19:42:59 奖券 (1481,543)。
# 两张坐标同属一张弹窗，第一下点下去弹窗就没了，第二下纯属多点，还多一次
# 移动鼠标的动静。用「同一片区域刚点过」来消掉它。
CLICK_GUARD_SEC = 2.5
CLICK_GUARD_RADIUS_PX = 240


class ClickGuard:
    """每个监控窗口各自记住最近几次点击的位置和时间。

    和冷却的区别：冷却按「窗口|目标」算，两个不同目标各点各的；这里按「位置」算，
    同一片区域刚点过就安静一会儿，一张弹窗只点一下。
    """

    def __init__(self) -> None:
        self._recent: dict[str, list[tuple[float, int, int]]] = {}
        self._lock = threading.Lock()
        self._keep = 24

    def record(self, monitor_name: str, x: int, y: int) -> None:
        with self._lock:
            items = self._recent.setdefault(monitor_name, [])
            items.append((time.monotonic(), int(x), int(y)))
            del items[:-self._keep]        # 只留最近几次，长跑不涨内存

    def nearest_gap(self, monitor_name: str, x: int, y: int, within_sec: float) -> float | None:
        """时间窗内最近一次点击离 (x, y) 多少像素；没有就 None。"""
        if within_sec <= 0:
            return None
        with self._lock:
            items = self._recent.get(monitor_name)
            if not items:
                return None
            now = time.monotonic()
            best: float | None = None
            for ts, px, py in items:
                if now - ts > within_sec:
                    continue
                gap = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
                if best is None or gap < best:
                    best = gap
            return best

    def reset(self, monitor_name: str | None = None) -> None:
        """界面上的「重置冷却」要能把这份压制一起清掉，否则重置完还是点不动。"""
        with self._lock:
            if monitor_name is None:
                self._recent.clear()
            else:
                self._recent.pop(monitor_name, None)


class Detector:
    """绑定到某一个监控窗口的检测器。

    多窗口时每个窗口各持有一个 Detector，但共享 ocr、cooldowns 与 guard 实例。
    """

    def __init__(self, settings: Settings, monitor: Monitor,
                 ocr_engine: OcrEngine | None = None,
                 cooldowns: CooldownTracker | None = None,
                 guard: SelfWindowGuard | None = None) -> None:
        self.settings = settings
        self.monitor = monitor
        self.matcher = TemplateMatcher(scales=settings.scales)
        self.ocr = ocr_engine or OcrEngine()
        self.cooldowns = cooldowns or CooldownTracker()
        self.guard = guard or SelfWindowGuard()
        self.errors: list[str] = []
        # 已经上报过的错误类别：同一帧错误每帧都会发生，只报第一次
        self._reported: set[str] = set()
        # 「为什么没点上」的诊断：没匹配上的关键词 -> (最像的 OCR 文本, 相似度)
        self.near_misses: dict[str, tuple[str, float]] = {}
        # 同一帧 OCR 究竟读到了什么（最多 5 段，去重），供体检日志举例
        self.ocr_samples: list[str] = []
        self.last_ocr_hits = 0

    def pop_errors(self) -> list[str]:
        errors, self.errors = self.errors, []
        return errors

    def _report(self, key: str, message: str) -> None:
        if key in self._reported:
            return
        self._reported.add(key)
        self.errors.append(f"[{self.monitor.name}] {message}")

    def _recovered(self, key: str) -> None:
        """问题恢复后清掉标记，下次再坏还能报出来。"""
        self._reported.discard(key)

    def targets_for(self, targets: list[Target]) -> list[Target]:
        """该窗口实际需要扫描的目标。"""
        name = self.monitor.name
        return [t for t in targets if t.enabled and t.has_detector() and t.applies_to(name)]

    def cooling_gap(self, targets: list[Target]) -> float:
        """该窗口所有目标都在冷却时还要等多久；有一个能点就是 0。"""
        return self.cooldowns.cooling_gap(self.monitor.name, self.targets_for(targets))

    def scan(self, frame, targets: list[Target],
             region: tuple[int, int, int, int] | None = None,
             run_ocr: bool = True,
             note_near_miss: bool = False) -> list[Detection]:
        """返回本帧所有「可点击」命中（已去重并排除冷却中的目标）。

        region 必须传截图时用的那个矩形：按标题跟随的窗口每轮矩形都在变，
        再读一遍配置会让帧内坐标和绝对坐标对不上，点歪。
        run_ocr=False 时只跑模板匹配——OCR 是整个循环最贵的一段，靠它降频。
        """
        box = self.monitor.region_tuple if region is None else region
        self.near_misses = {}
        self.ocr_samples = []
        self.last_ocr_hits = 0
        offset_x, offset_y = (box[0], box[1]) if box else (0, 0)
        # 冷却中的目标这轮点不了，先挑掉：只剩冷却目标时整帧 OCR 是白跑
        ready = [t for t in self.targets_for(targets)
                 if self.cooldowns.ready(self.monitor.name, t)]
        candidates: dict[str, list[tuple[MatchResult, str]]] = {}

        ocr_hits: list[OcrHit] | None = None
        if run_ocr and any(t.ocr_keywords for t in ready) and self.ocr.available:
            try:
                ocr_hits = self.ocr.recognize(frame, max_side=self.monitor.max_side(self.settings),
                                              min_side=int(self.settings.ocr_min_side))
                self._recovered("ocr")
                self.last_ocr_hits = len(ocr_hits or [])
            except Exception as exc:  # OCR 失败不应中断该窗口的扫描循环
                self._report("ocr", f"OCR 识别失败: {exc}（可把 ocr_max_side 调小，如 1280）")
                ocr_hits = None

        for target in ready:
            found: list[tuple[MatchResult, str]] = []
            if target.template:
                path = abs_path(target.template)
                threshold = target.match_threshold or self.settings.match_threshold
                try:
                    matches = self.matcher.match(frame, path, threshold)
                    self._recovered(path)   # 补回模板后清标记，下次再丢还能提醒
                except TemplateError as exc:
                    self._report(path, str(exc))
                    matches = []
                found.extend((m, "template") for m in matches)
            if ocr_hits is not None and target.ocr_keywords:
                found.extend((m, "ocr") for m in self._ocr_matches(target, ocr_hits,
                                                                   note_near_miss))
            if found:
                candidates[target.name] = found

        detections: list[Detection] = []
        for target in ready:
            pairs = candidates.get(target.name)
            if not pairs:
                continue
            best, source = max(pairs, key=lambda p: p[0].confidence)
            dx, dy = target.click_offset
            abs_x = offset_x + best.center[0] + int(dx)
            abs_y = offset_y + best.center[1] + int(dy)
            if self.guard.contains(abs_x, abs_y):
                # 只在每个窗口报一次：这个状态不会「自愈」，反复提醒等于刷屏
                self._report("self", "命中点落在本程序自己的窗口里，已忽略。"
                                    "整屏扫描时请把直播窗口挪到界面之外，或给该窗口框选区域。")
                continue
            detections.append(Detection(
                monitor_name=self.monitor.name,
                target_name=target.name,
                source=source,
                confidence=best.confidence,
                box=(best.x, best.y, best.w, best.h),
                abs_x=abs_x,
                abs_y=abs_y,
            ))
        return detections

    def _ocr_matches(self, target: Target, hits: list[OcrHit],
                     note_near_miss: bool = False) -> list[MatchResult]:
        keys = [k for k in (compact_text(kw) for kw in target.ocr_keywords) if k]
        # (命中的关键词字数, 收窄后的框)：字数越多越像真按钮，界时靠它排序
        pairs: list[tuple[int, MatchResult]] = []
        for hit in hits:
            if hit.score < self.settings.ocr_min_score:
                continue
            text = compact_text(hit.text)
            # 同一句话可能同时命中长关键词和它的短尾巴（「立即领券」含着「领券」），
            # 这一框只算命中其中最长的那个。
            matched = [k for k in keys if k in text]
            if matched:
                best_key = max(matched, key=len)
                pairs.append((len(best_key), _keyword_box(hit, best_key, text)))
        if not pairs and note_near_miss and keys:
            # 只在周期性体检那几帧算，避免每帧都做一次全文相似度
            best_text, best_ratio = None, -1.0
            seen: list[str] = []
            for hit in hits:
                if hit.score < self.settings.ocr_min_score:
                    continue
                text = compact_text(hit.text)
                if not text:
                    continue
                seen.append(text)
                ratio = max(_best_ratio(key, text) for key in keys)
                if ratio > best_ratio:
                    best_text, best_ratio = text, ratio
            # 相似度为 0 也要留文本：那一帧里 OCR 读到的东西正好说明「画面里有什么」
            self.near_misses[target.name] = (best_text or "", max(best_ratio, 0.0))
            self.ocr_samples = list(dict.fromkeys(seen))[:5]
        if not pairs:
            return []
        # 整帧只留命中最长关键词的那些框。直播间弹幕、聊天记录里出现
        # 「领券」两个字太常见了（“第1步先领券”就是一句弹幕）；只要同帧还有
        # 「立即领券」这种完整按钮文案，就得点按钮，不能点到弹幕上去。
        longest = max(n for n, _ in pairs)
        return nms_matches([box for n, box in pairs if n == longest], iou_threshold=0.4)

    @staticmethod
    def dedupe_same_location(detections: list[Detection]) -> list[Detection]:
        """同一窗口内不同目标命中同一像素位置时只保留置信度最高者，避免重复点击。"""
        keep: list[Detection] = []
        for det in sorted(detections, key=lambda d: d.confidence, reverse=True):
            cx, cy = det.abs_x, det.abs_y
            if all((cx - k.abs_x) ** 2 + (cy - k.abs_y) ** 2 > 24 ** 2 for k in keep):
                keep.append(det)
        return sorted(keep, key=lambda d: d.confidence, reverse=True)


def _best_ratio(key: str, text: str) -> float:
    """key 在 text 上任意位置滑动能得到的最高相似度（1.0 = 完全一致）。

    只给诊断用，不拿它决定要不要点：模糊匹配一旦放开，弹幕里意思相近的字
    就会被点错，比「漏点一次」糟糕得多。
    """
    n = len(key)
    if not n or len(text) < n:
        return 0.0
    return max(difflib.SequenceMatcher(None, key, text[j:j + n]).ratio()
               for j in range(len(text) - n + 1))


def _keyword_box(hit: OcrHit, key: str, text: str) -> MatchResult:
    """把 OCR 文本框收窄到关键词所在的那一段。

    识别网络常把紧挨着的两个按钮读成一个框（"签到奖券"）。直接点框中心会让
    两个目标算出同一个像素，从而点错按钮；按字符占比切分能把它们分开。
    """
    start = text.find(key)
    if start < 0 or not text:
        return MatchResult(hit.x, hit.y, hit.w, hit.h, hit.score)
    x = hit.x + int(round(hit.w * start / len(text)))
    w = int(round(hit.w * len(key) / len(text)))
    w = max(1, min(w, hit.x + hit.w - x))
    return MatchResult(x, hit.y, w, hit.h, hit.score)
