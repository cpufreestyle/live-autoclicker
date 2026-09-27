"""调度模块：每个监控窗口一条扫描线程，共享 OCR 实例、冷却表与鼠标。"""
from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass, field

from core.capture import (ScreenCapture, WindowLocator, bring_to_front, frame_changed,
                          drop_topmost, frame_digest, grab_window, is_minimized, looks_blank,
window_identity_at, point_on_screen, move_window_into_view, restore_window_position,
                          point_owned_by_session, window_rect_for)
import core.capture as capture_mod
from core.clicker import Clicker
from core.config import Monitor, Settings, Target
from core.detector import (CLICK_GUARD_RADIUS_PX, ClickGuard, CooldownTracker, Detector,
                          SelfWindowGuard, resolve_region)
from core.ocr import OcrEngine, cap_cpu_threads

# 单帧超时也不许零空闲：每帧之后至少让出「设定间隔 × 这个比例」的时间
MIN_IDLE_RATIO = 0.3
# 画面“几乎没变”时连 OCR+模板一起跳过，省掉直播里最贵的一段。只有缩略图都近乎
# 一致才跳，且最多连跳这么多轮就得强制实扫一次，保证不漏掉稍后弹出的按钮。
STATIC_SKIP_MAX = 3
STATIC_DIFF_MIN = 1.2
def _cover_label(x: int, y: int) -> str:
    """当前盖住某个屏幕点的窗口，写成一句人能看懂的话（问不出来就退回「别的窗口」）。

    只说「被别的窗口盖着」等于没说——最常见的就是本程序自己的界面压在直播窗上，
    把窗口名写进日志，用户一眼就知道该挪谁。
    """
    title, klass, process = window_identity_at(x, y)
    name = (title or klass or process or "").strip()
    if not name:
        return "别的窗口"
    return f"「{name}」({process})" if process else f"「{name}」"


# 抢前台是抢用户焦点，同一窗口两次尝试之间至少隔这么久，别变成焦点轰炸
RAISE_RETRY_SEC = 4.0
# 每扫这么多帧，往日志里报一次这个窗口到底在干什么
PROGRESS_EVERY = 40


@dataclass
class Event:
    kind: str          # log | highlight | state | monitor
    payload: dict = field(default_factory=dict)


@dataclass
class MonitorState:
    """单个窗口的运行统计，随扫描事件回传 UI。"""
    name: str
    scans: int = 0
    hits: int = 0
    clicks: int = 0
    last_error: str = ""
    last_hit_at: float = 0.0
    busy: bool = False

    def snapshot(self) -> dict:
        return {"name": self.name, "scans": self.scans, "hits": self.hits,
                "clicks": self.clicks, "last_error": self.last_error,
                "last_hit_at": self.last_hit_at, "busy": self.busy}


class MonitorWorker(threading.Thread):
    """一个直播窗口的扫描循环：截图 → 检测 → 点击 → 汇报。"""

    def __init__(self, engine: "Engine", monitor: Monitor) -> None:
        super().__init__(name=f"scan-{monitor.name}", daemon=True)
        self.engine = engine
        self.monitor = monitor
        self.stop_event = threading.Event()
        self.state = engine.get_state(monitor.name)
        self.detector = Detector(engine.settings, monitor, engine.ocr, engine.cooldowns,
                                 engine.guard)
        # 本轮实际截图用的矩形：按标题跟随的窗口每轮都可能不一样
        self.region = monitor.region_tuple
        # 每个线程一个粘性定位器：同一身份续跟上次那个窗口，别在多个同身份窗口间跳变
        self._locator = WindowLocator()
        self._click_lost_logged = False
        self._last_digest = None
        self._static_skips = 0
        # 体检计数：静止跳过 / 冷却跳过 / OCR 跑了几次
        self._skips_static = 0
        self._skips_cooling = 0
        self._ocr_runs = 0
        self._ocr_hits = 0
        self._progress_due = True          # 首帧后就报一次，让用户立刻看到在跑
        self._slow_scans = 0
        self._region_lost = False
        self._cooling = False
        self._round = 0
        self._last_raise_at = 0.0
        # 连续多少轮什么都没扫到（用来把 OCR 降频），以及被「同弹窗防重复」拦下的次数
        self._idle_rounds = 0
        self._guard_skipped = 0
        self._guard_logged: set[str] = set()
        # 刚点完一发：下一轮强制做一次完整 OCR，确认弹窗已经消失
        self._ocr_force = False
        # 距上次 OCR 过了几轮（用轮数而不是「第几轮取模」，降频的节奏才稳定）
        self._ocr_since = 1 << 30
        # 本层借来的置顶窗口：点完必须还回去，不然用户的直播窗会一直飘在最上面
        # 为点某一发而临时挪进屏幕的窗口 [(hwnd, 原 left, 原 top)]：直播窗和它
        # 上面的弹窗可能是两个独立顶层窗，都要点完原样还回去。
        self._moved: list[tuple[int, int, int]] = []
        self._raised_hwnd = 0
        self.ocr_due = True

    def run(self) -> None:
        try:
            capture = ScreenCapture()
        except Exception as exc:
            self.engine.log("error", f"[{self.monitor.name}] 无法初始化截屏: {exc}")
            return
        self.engine.log("info", f"[{self.monitor.name}] 开始监控 {self._describe(capture)}，"
                                f"截图区域 {self.region}。")
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                pause = self._next_pause()
                cooling = self.detector.cooling_gap(self.engine.targets)
                if cooling > 0:
                    # 所有按钮都在冷却：这一帧就算检出也点不了，截图和 OCR 全是白跑。
                    # 冷却最长能配到几分钟，所以等待以扫描间隔为上限，
                    # 好让「停止」「重置冷却」这些操作还能及时响应。
                    if not self._cooling:
                        self._cooling = True
                        self.engine.log("info", f"[{self.monitor.name}] 全部按钮冷却中，"
                                                f"约 {cooling:.0f}s 后再扫。")
                    self._skips_cooling += 1
                    self.stop_event.wait(min(cooling, pause))
                    continue
                self._cooling = False
                self._round += 1
                self.ocr_due = self._ocr_due_this_round()
                # 体检要在这一帧的 process_frame 之前把开关立起来，否则相似度算不出来
                if self.state.scans % PROGRESS_EVERY == 0:
                    self._progress_due = True
                self.state.busy = True
                try:
                    resolved = resolve_region(self.monitor, self._region_finder)
                    if not resolved.ok:
                        self._note_region_lost(resolved.error)
                    else:
                        self._note_region_found()
                        self.region = resolved.box
                        frame = self._grab(capture, resolved.box)
                        if frame is None:
                            self.state.last_error = "本帧截图失败（后台抓窗与整屏截图都失败）"
                        elif self._frame_is_static(frame):
                            # 画面和上轮几乎逐像素一致：没有新弹窗/按钮可点，
                            # 连 OCR + 模板都省掉（有连跳上限兜底，不影响命中）。
                            self.state.last_error = ""
                            self._skips_static += 1
                        else:
                            self.engine.process_frame(self, frame)
                            self.state.last_error = ""
                except Exception as exc:
                    self.state.last_error = str(exc)[:120]
                    self.engine.log("error", f"[{self.monitor.name}] 扫描异常: {exc}")
                finally:
                    self.state.busy = False

                for err in self.detector.pop_errors():
                    self.state.last_error = err[:120]
                    self.engine.log("warn", err)

                self.state.scans += 1
                if self._progress_due:
                    self._log_progress()
                self.engine.events.put(Event("monitor", self.state.snapshot()))

                elapsed = time.monotonic() - started
                self._warn_if_slow(elapsed)
                self.stop_event.wait(self._idle_wait(pause, elapsed))
        finally:
            capture.close()
            self.engine.log("info", f"[{self.monitor.name}] 已停止监控。")

    def _note_region_lost(self, message: str) -> None:
        """目标窗口没了（关掉/最小化）只说一次，然后安静地等它回来。"""
        if self._region_lost:
            return
        self._region_lost = True
        self.state.last_error = message[:120]
        self.engine.log("warn", f"[{self.monitor.name}] {message}")

    def _note_region_found(self) -> None:
        if not self._region_lost:
            return
        self._region_lost = False
        self.engine.log("info", f"[{self.monitor.name}] 已找到目标窗口，继续扫描。")

    def stop(self) -> None:
        self.stop_event.set()

    def _region_finder(self, monitor: Monitor):
        """按标题/类名/进程找窗口矩形。默认走粘性定位（续跟上次那个，不来回跳）；
        回归脚本用自定义 find_window 覆盖 engine.find_window 时直接用它。"""
        base = self.engine.find_window
        if base is window_rect_for:
            return self._locator.box(monitor)
        return base(monitor)

    def note_click_lost(self) -> None:
        """点击前发现直播窗口消失了（关掉/最小化）：只说一次，然后安静地等它回来。"""
        if self._click_lost_logged:
            return
        self._click_lost_logged = True
        self.engine.log("warn", f"[{self.monitor.name}] 点击前发现直播窗口已消失，"
                                f"本轮跳过；窗口回来后自动继续。")

    def release_raised_window(self) -> None:
        """把点击前借来的改动还回去：置顶还层次、位置还原处。点完立刻调。"""
        for hwnd, left, top in self._moved:
            restore_window_position(hwnd, left, top)
        self._moved.clear()
        if not self._raised_hwnd:
            return
        drop_topmost(self._raised_hwnd)
        self._raised_hwnd = 0

    def bring_window_on_screen(self, hwnd: int, target_name: str) -> bool:
        """直播窗口停在屏幕外时，先把它挪进可见区域再点，返回有没有真挪。

        点坐标落在虚拟桌面外面，Windows 会把光标夹到屏幕边缘，点下去点的是边缘上
        那个窗口——命中越多错得越远。显示器被拔掉、远程桌面（ToDesk/VNC）改过桌面尺寸
        之后，窗口还留在老坐标上是常事。这里只做最小平移，点完由
        release_raised_window() 原样还原，和借置顶是同一套借还逻辑。
        """
        if not hwnd or not getattr(self.engine.settings, "bring_window_on_screen", True):
            return False
        if any(moved[0] == int(hwnd) for moved in self._moved):
            return True
        origin = move_window_into_view(hwnd)
        if origin is None:
            return False
        self._moved.append((int(hwnd), origin[0], origin[1]))
        self.engine.log("info", f"[{self.monitor.name}] 直播窗口停在屏幕外（原位置 "
                                f"{origin[0]},{origin[1]} 不在当前桌面内），已临时挪进可见区域再点，"
                                f"点完还原。")
        return True

    def raise_target_window(self, hwnd: int, target_name: str, cover: str = "别的窗口") -> bool:
        """把被挡住的直播窗口调到前台，好让这一发点得进去。

        只在「已经命中按钮、但那个点归别的窗口所有」时才调用，所以不会平白抢焦点。
        返回有没有真的动过窗口（供日志和测试判断）。
        """
        if not hwnd or not getattr(self.engine.settings, "raise_before_click", True):
            return False
        now = time.monotonic()
        if now - self._last_raise_at < RAISE_RETRY_SEC:
            return False
        self._last_raise_at = now
        was_minimized = is_minimized(hwnd)
        raised = bring_to_front(hwnd)
        if raised or was_minimized:
            how = "从最小化恢复并切到前台" if was_minimized else "切到前台"
            self.engine.log("info", f"[{self.monitor.name}] 命中「{target_name}」但它被{cover}"
                                    f"挡着，已把直播窗口{how}后再点。")
        if raised:
            self._raised_hwnd = int(hwnd)
        return raised

    def track_shift(self) -> tuple[bool, int, int]:
        """点之前按当前窗口矩形算出相对本轮截图的位移，返回 (窗口还在吗, dx, dy)。
        截图到点击隔着几次 OCR（可能一两秒），窗口可能被拖动；把命中点整体平移这段位移
        才点得中。窗口已消失返回 (False, 0, 0)，上层据此跳过点击。"""
        if not self.monitor.tracked:
            return True, 0, 0
        origin = self.region[:2] if self.region else (0, 0)
        current = self._region_finder(self.monitor)
        if current is None:
            return False, 0, 0
        return True, current[0] - origin[0], current[1] - origin[1]

    def _frame_is_static(self, frame) -> bool:
        """帧和上轮实扫几乎一致就跳过。直播画面大多在动，只有真卡住/黑屏/“等待开播”、
        或大弹窗已挂起但目标还在冷却时才稳定到可被跳过。连续跳过有上限，触顶强制实扫
        一次并重置基线；只对“缩略图都近乎相同”为真，误判方向永远是“白扫一次”，绝不漏点。"""
        digest = frame_digest(frame)
        stale = digest is None or self._last_digest is None
        moved = (not stale) and frame_changed(self._last_digest, digest, STATIC_DIFF_MIN)
        if self._static_skips >= STATIC_SKIP_MAX or stale or moved:
            self._last_digest = digest
            self._static_skips = 0
            return False
        self._static_skips += 1
        return True

    def _grab(self, capture, box):
        """按配置取这一帧。默认先整屏 mss；background_capture 开着且窗口句柄在手时，
        直播窗被无关窗口盖住、或整屏抓到黑帧（窗口整个在屏幕外），才改走 PrintWindow
        后台抓窗口自己的像素；后台也失败/黑屏仍回退 mss，不会因抓不到而漏扫。

        为什么默认整屏：直播间的「兑换券 / 红包」弹窗多是盖在直播窗上的独立顶层窗口，
        PrintWindow 只能拍到直播窗自己的像素，会把弹窗整块漏掉。2026-09-26 真机连扫 68 帧
        一帧都没拍到过弹窗、用户连报「检测不到」，根因就在这。
        """
        hwnd = self._locator.current_hwnd() if self.monitor.background_capture else 0
        try:
            frame = capture.grab(box)
        except Exception:
            frame = None
        if hwnd and (frame is None or looks_blank(frame)
                     or self._window_is_covered(hwnd, box)):
            shot = grab_window(hwnd)
            if shot is not None and not looks_blank(shot[0]):
                grabbed, rect = shot
                # 截图、检测、坐标换算必须用同一份矩形。后台抓窗是单独向 Windows
                # 要矩形，和本轮 resolve_region 隔了一次调用，窗口刚好在这中间动了
                # 一像素，点击点就会偏。宁可回退整屏白扫一次，也不能点歪。
                if tuple(rect) == tuple(box):
                    return grabbed
        if frame is not None:
            return frame
        return capture.grab(box)

    def _window_is_covered(self, hwnd: int, box) -> bool:
        """直播窗口的中心点现在归不归它自己（或嵌在它里面的弹窗）所有。

        被无关窗口盖住时，整屏截图拍到的只是盖它的东西，直播窗里的按钮再大也扫不出来，
        这时该用后台抓窗看窗口自己的像素（点击仍由提窗逻辑兑底）。
        """
        cx = box[0] + box[2] // 2
        cy = box[1] + box[3] // 2
        if not point_on_screen(cx, cy):
            return True
        return not point_owned_by_session(hwnd, cx, cy)

    def _next_pause(self) -> float:
        settings = self.engine.settings
        base = self.monitor.interval(settings)
        jitter = min(settings.jitter_sec, base * 0.5)
        return max(0.05, base + random.uniform(-jitter, jitter))

    def _ocr_due_this_round(self) -> bool:
        """OCR 是整个循环里最贵的一段（实测一帧 0.4~3.5s，画面里字越多越慢）。

        `ocr_scan_every=2` 就隔轮才跑一次，模板匹配仍然每轮做。另外：连着好几轮
        什么都没扫到时自动降频到 `ocr_idle_every`，一有命中立刻恢复每轮跑——直播
        绝大多数时间没有弹窗，那段 OCR 是白烧 CPU。代价是弹窗最坏晚一个扫描间隔
        被点到：会一直挂到被点掉的弹窗没损失，但自己几秒就消失的弹窗（红包倒计时
        之类）可能整轮错过，那种场景把 `ocr_idle_every` 填 1。刚点完一发则强制跑
        一次完整 OCR，好尽快确认弹窗已经消失。
        好尽快确认弹窗已经消失。
        """
        if self._ocr_force:
            self._ocr_force = False
            self._ocr_since = 0
            return True
        every = max(1, int(self.engine.settings.ocr_scan_every))
        idle_every = max(every, int(getattr(self.engine.settings, "ocr_idle_every", every)))
        cadence = idle_every if self._idle_rounds >= idle_every else every
        self._ocr_since += 1
        if self._ocr_since < cadence:
            return False
        self._ocr_since = 0
        return True

    def _idle_wait(self, pause: float, elapsed: float) -> float:
        """帧与帧之间必须留出空隙。

        单帧比设定间隔还慢时 `pause - elapsed` 是负数，线程就成了连续不断地
        截屏+推理，一秒都不让出 CPU；OCR 线程数被限住之后，就是这个 100% 占空比
        把桌面拖没的。留 30% 间隔的空隙不影响命中率——按钮会一直显示到被点掉。
        """
        return max(pause - elapsed, pause * MIN_IDLE_RATIO)

    def _warn_if_slow(self, elapsed: float) -> None:
        settings = self.engine.settings
        budget = self.monitor.interval(settings) + settings.jitter_sec
        if elapsed <= budget:
            self._slow_scans = 0
            return
        self._slow_scans += 1
        if self._slow_scans % 10 == 1:
            self.engine.log("warn", f"[{self.monitor.name}] 单帧耗时 {elapsed:.1f}s，超过设定间隔。"
                                    f"建议缩小该窗口区域、调低 ocr_max_side 或加大扫描间隔。")

    def _log_progress(self) -> None:
        """把这个窗口这段时间到底在干什么讲清楚。

        「点了启动但什么都没发生」最常见的三种原因：OCR 从没把按钮文字读出来、
        读出来了但和关键词对不上、或者整帧都在被跳过。这三样都写进日志，
        用户自己就能判断该改哪里，不用回来问「为什么没点击」。
        """
        self._progress_due = False
        parts = [f"已扫 {self.state.scans} 帧", f"命中 {self.state.hits}",
                 f"点击 {self.state.clicks}"]
        if self._ocr_runs:
            parts.append(f"OCR {self._ocr_runs} 次/{self._ocr_hits} 个文本框")
        if self._skips_cooling:
            parts.append(f"冷却中跳过 {self._skips_cooling} 帧")
        if self._skips_static:
            parts.append(f"画面没变跳过 {self._skips_static} 帧")
        if self._guard_skipped:
            parts.append(f"同弹窗重复点击拦下 {self._guard_skipped} 次")
        near = self.detector.near_misses
        if near:
            shown = "、".join(f"『{t}』" for t in self.detector.ocr_samples[:5])
            bits = []
            for key, (text, ratio) in sorted(near.items(), key=lambda kv: -kv[1][1]):
                if ratio >= 0.5:
                    bits.append(f"「{key}」最像 {text!r}(相似度 {ratio:.2f})")
                elif shown:
                    bits.append(f"「{key}」完全对不上（OCR 只读到 {shown}）")
                else:
                    bits.append(f"「{key}」完全对不上")
            parts.append("关键词没匹配上：" + "；".join(bits) +
                         "——字对不上就把关键词改成 OCR 读到的样子，或改用模板匹配")
        self.engine.log("info", f"[{self.monitor.name}] " + "，".join(parts))

    def _describe(self, capture: ScreenCapture) -> str:
        if self.monitor.tracked:
            crops = []
            if self.monitor.crop_bottom:
                crops.append(f"底 {self.monitor.crop_bottom}px")
            if getattr(self.monitor, "crop_right", 0):
                crops.append(f"右 {self.monitor.crop_right}px")
            if getattr(self.monitor, "crop_left", 0):
                crops.append(f"左 {self.monitor.crop_left}px")
            crop = f"，裁掉{'/'.join(crops)}" if crops else ""
            return f"跟随标题含「{self.monitor.window_title}」的窗口{crop}"
        region = self.monitor.region_tuple
        if region:
            return f"区域 ({region[0]}, {region[1]}, {region[2]}x{region[3]})"
        l, t, w, h = capture.full_region
        return f"全屏 ({w}x{h} @ {l},{t})"


class Engine:
    """多窗口自动点击引擎。

    OCR 实例与冷却表全局共享：模型只加载一次，推理用信号量限流；
    冷却按「窗口 + 按钮」记键，因此同一个签到按钮在两个直播间各自只点一次。
    """

    def __init__(self, settings: Settings, monitors: list[Monitor], targets: list[Target],
                 events: queue.Queue | None = None,
                 run_logger=None) -> None:
        self.settings = settings
        self.monitors = monitors
        self.targets = targets
        self.events: queue.Queue = events or queue.Queue()
        # 落盘日志（排障用）；不传就是不写，测试与脚本环境零副作用
        from core.runlog import RunLogger
        self.run_logger: RunLogger | None = run_logger
        self.cooldowns = CooldownTracker()
        # 同一片区域刚点过就安静一会儿（防一张弹窗被连着点两下）
        self.click_guard = ClickGuard()
        # 界面线程每 150ms 刷新本程序自己的窗口矩形，扫描线程据此丢弃「点到自己」
        self.guard = SelfWindowGuard()
        # 按标题跟随时用它在每轮扫描前现取矩形；回归脚本里换成假窗口的实时几何
        # 接受整个 Monitor：除标题外还能按窗口类名/进程名识别
        self.find_window = window_rect_for
        self._ocr_concurrency = max(1, settings.ocr_concurrency)
        self._ocr_threads = max(1, settings.ocr_threads)
        self._ocr_provider = settings.ocr_provider
        self.ocr = OcrEngine(gate=threading.Semaphore(self._ocr_concurrency),
                             threads=settings.ocr_threads,
                             provider=settings.ocr_provider)
        self.clicker = self._make_clicker(settings)
        self._clicker_cfg = self._clicker_config(settings)
        self._workers: dict[str, MonitorWorker] = {}
        # 统计状态独立于线程存放：停止后界面仍能看到本次运行的累计数字
        self._states: dict[str, MonitorState] = {}
        # 可重入：start/update_config 持锁创建 worker 时还会再次进入 get_state
        self._lock = threading.RLock()
        self._running = threading.Event()
        # 现在就限好 OpenCV 的线程数；OCR 那半在模型建会话前再设一次（见 OcrEngine）
        note = cap_cpu_threads(settings.ocr_threads)
        if note:
            self.log("warn", note)

    def get_state(self, name: str) -> MonitorState:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                state = MonitorState(name)
                self._states[name] = state
            return state

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return self._running.is_set()

    def start(self) -> bool:
        """启动所有启用的监控窗口；没有任何可用窗口时返回 False。"""
        if self._running.is_set():
            return True
        enabled = [m for m in self.monitors if m.enabled]
        if not enabled:
            self.log("warn", "没有启用的监控窗口，请先在「监控窗口」里新增并勾选。")
            return False
        self._warmup_ocr()  # 放在锁外：加载模型要一两秒，别把界面一起卡住
        with self._lock:
            if self._running.is_set():
                return True
            # 先重置统计再建线程：worker 构造时会认领自己的状态对象
            self._states = {m.name: MonitorState(m.name) for m in enabled}
            self._workers = {m.name: MonitorWorker(self, m) for m in enabled}
            for worker in self._workers.values():
                worker.start()
            self._running.set()
        self.log("info", f"引擎已启动，同时监控 {len(enabled)} 个窗口。")
        self._emit_state()
        return True

    def _warmup_ocr(self) -> None:
        if not any(t.ocr_keywords for t in self.targets if t.enabled) or not self.ocr.available:
            return
        self.log("info", "正在加载 OCR 模型…")
        self.ocr.warmup()
        if self.ocr.gpu_note:
            self.log("info", self.ocr.gpu_note)
        elif self.ocr.cap_note:
            self.log("warn", self.ocr.cap_note)

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers = {}
        for worker in workers:
            worker.stop()
        deadline = time.monotonic() + 5.0
        for worker in workers:
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(timeout=remaining)
        stuck = [w.monitor.name for w in workers if w.is_alive()]
        if stuck:
            self.log("warn", f"以下窗口的扫描线程未在 5s 内退出（可能正忙）：{', '.join(stuck)}")
        self._running.clear()
        self.log("info", "引擎已停止。")
        self._emit_state()

    def toggle(self) -> bool:
        if self.running:
            self.stop()
        else:
            self.start()
        return self.running

    @property
    def stats(self) -> dict:
        with self._lock:
            states = list(self._states.values())
        return {"monitors": len(states),
                "scans": sum(s.scans for s in states),
                "hits": sum(s.hits for s in states),
                "clicks": sum(s.clicks for s in states)}

    def monitor_states(self) -> dict:
        """界面刷新表格用；停止后仍保留本轮累计值。"""
        with self._lock:
            return {name: state.snapshot() for name, state in self._states.items()}

    # ---------- 配置热更新 ----------
    def update_config(self, settings: Settings, monitors: list[Monitor], targets: list[Target]) -> None:
        """UI 改配置后调用。运行中按窗口签名增删线程，未变化的窗口不受影响。"""
        self.settings = settings
        self.monitors = monitors
        self.targets = targets
        config = self._clicker_config(settings)
        if config != self._clicker_cfg:
            # 重建点击器会连那把全局锁一起换：正在点击的线程持旧锁，新线程持新锁，
            # 两次点击可能同时进行。只在后端/人性化真的变了时才换。
            self._clicker_cfg = config
            self.clicker = self._make_clicker(settings)
        if (settings.ocr_concurrency != self._ocr_concurrency
                or settings.ocr_threads != self._ocr_threads
                or settings.ocr_provider != self._ocr_provider):
            # 推理会话一旦建好就改不了线程数、也换不了后端，运行中的 worker 也还握着
            # 旧的 OcrEngine，所以只能等停止后重建；OpenCV 那半是全局的，现在就生效。
            note = cap_cpu_threads(settings.ocr_threads)
            if self.running:
                self.log("warn", "OCR 并发数 / 线程数 / 后端变更需停止后再启动才生效。")
            else:
                self._ocr_concurrency = settings.ocr_concurrency
                self._ocr_threads = settings.ocr_threads
                self._ocr_provider = settings.ocr_provider
                self.ocr = OcrEngine(gate=threading.Semaphore(max(1, settings.ocr_concurrency)),
                                     threads=settings.ocr_threads,
                                     provider=settings.ocr_provider)
            if note:
                self.log("warn", note)
        if not self.running:
            with self._lock:
                known = {m.name for m in monitors}
                for name in [k for k in self._states if k not in known]:
                    self._states.pop(name, None)
            return

        desired = {m.name: m for m in monitors if m.enabled}
        with self._lock:
            current = dict(self._workers)
        removed = [name for name in current if name not in desired]
        changed = [name for name, w in current.items()
                   if name in desired and w.monitor.signature() != desired[name].signature()]

        for name in removed:
            self._shutdown_worker(current[name])
            with self._lock:
                self._workers.pop(name, None)
                self._states.pop(name, None)
            self.log("info", f"[{name}] 窗口已移除或停用，停止其扫描线程。")
        for name in changed:
            self._shutdown_worker(current[name])
            with self._lock:
                self._workers.pop(name, None)
            self.log("info", f"[{name}] 配置已变更，重启该窗口扫描。")
        with self._lock:
            for name, monitor in desired.items():
                if name in self._workers:
                    continue
                worker = MonitorWorker(self, monitor)
                self._workers[name] = worker
                worker.start()

    @staticmethod
    def _shutdown_worker(worker: MonitorWorker, timeout: float = 3.0) -> None:
        worker.stop()
        worker.join(timeout=timeout)

    def reset_cooldowns(self, monitor_name: str | None = None) -> None:
        self.cooldowns.reset(monitor_name)
        self.click_guard.reset(monitor_name)

    # ---------- 扫描结果处理 ----------
    def process_frame(self, worker: MonitorWorker, frame) -> None:
        worker.detector.settings = self.settings  # 参数热更新对已运行的窗口同样生效
        detections = worker.detector.scan(frame, self.targets, region=worker.region,
                                         run_ocr=worker.ocr_due,
                                         note_near_miss=worker._progress_due)
        if worker.detector.last_ocr_hits:
            worker._ocr_runs += 1
            worker._ocr_hits += worker.detector.last_ocr_hits
        detections = Detector.dedupe_same_location(detections)
        # 这一轮扫到了东西（哪怕最后没点出去）就别算空闲，下一轮照常跑 OCR
        worker._idle_rounds = 0 if detections else worker._idle_rounds + 1
        if not detections:
            return
        worker.state.hits += len(detections)
        worker.state.last_hit_at = time.time()

        tracked_hwnd = worker._locator.current_hwnd()
        # 从截图到点击隔着几次 OCR，窗口可能已被拖动；按当前矩形把命中点平移过去再点。
        # 挪窗 / 提窗都会改变窗口矩形，所以每动一次就重算一次位移，点的永远是最新位置。
        def _points(sx, sy):
            box = worker.region
            ox = (box[0] + sx) if box else sx
            oy = (box[1] + sy) if box else sy
            hits = [(det, det.abs_x + sx, det.abs_y + sy) for det in detections]
            return hits, ox, oy

        present, shift_x, shift_y = worker.track_shift()
        if not present:
            worker.note_click_lost()
            return

        for _ in range(2):
            points, ox, oy = _points(shift_x, shift_y)
            acted = False
            # 1) 命中点落在屏幕外：Windows 会把光标夹到屏幕边缘，
            #    先把直播窗口挪进可见区域（点完还原）
            off = [p for p in points if not point_on_screen(p[1], p[2])]
            if off:
                acted = worker.bring_window_on_screen(tracked_hwnd, off[0][0].target_name)
                # 弹窗多是独立顶层窗口，挪直播窗不一定挪得动它；把这一发命中点
                # 现在所属的窗口也挪进屏，点完一起还原。
                owner = capture_mod.window_at(off[0][1], off[0][2])
                if owner and capture_mod.root_hwnd(owner) != capture_mod.root_hwnd(tracked_hwnd):
                    acted = worker.bring_window_on_screen(owner, off[0][0].target_name) or acted
            # 2) 命中点被别的窗口盖着：后台抓窗只负责看得见，点击只发给最顶层，
            #    先把直播窗口临时顶到前台（点完还层次）
            blocked = next((p for p in points
                            if not point_owned_by_session(tracked_hwnd, p[1], p[2])), None)
            if blocked is not None:
                acted = worker.raise_target_window(tracked_hwnd, blocked[0].target_name,
                                                   _cover_label(blocked[1], blocked[2])) or acted
            if not acted:
                break
            present, shift_x, shift_y = worker.track_shift()
            if not present:
                worker.note_click_lost()
                worker.release_raised_window()
                return
        else:
            points, ox, oy = _points(shift_x, shift_y)
        blocked_logged = False
        # 顺手问一句这个区域现在被谁盖着，写进告警里；只在真被挡住时才用得上
        cover = _cover_label(ox + 1, oy + 1)
        offscreen_logged = False
        for det in detections:
            x, y, w, h = det.box
            self.events.put(Event("highlight", {
                "box": (ox + x, oy + y, w, h), "label": f"{worker.monitor.name}·{det.target_name}",
                "sec": self.settings.highlight_sec}))
            click_x, click_y = det.abs_x + shift_x, det.abs_y + shift_y
            if not point_on_screen(click_x, click_y):
                # 移进屏幕也没办法（或者开关关了）：坑里的坑，块被夹到屏幕边缘
                if not offscreen_logged:
                    offscreen_logged = True
                    self.log("warn", f"[{worker.monitor.name}] 命中「{det.target_name}」但点击点落在屏"
                                     f"幕可见范围外（Windows 会把它夹到屏幕边缘，点中的另有其人），"
                                     f"已跳过。把直播窗口移回屏幕内，或在“点击与启动”里允许"
                                     f"自动把窗口挪进屏幕。")
                continue
            if not point_owned_by_session(tracked_hwnd, click_x, click_y):
                if not blocked_logged:
                    blocked_logged = True
                    self.log("warn", f"[{worker.monitor.name}] 命中「{det.target_name}」但那个位置"
                                     f"被别的窗口盖着，这一发点下去会点到别人，已跳过。"
                                     f"挡着的是{cover}。把直播窗口切到最前面就能点"
                                     f"（后台抓窗只负责看得见，不负责点得到）。")
                continue
            gap = self.click_guard.nearest_gap(worker.monitor.name, click_x, click_y,
                                               self.settings.click_guard_sec)
            if gap is not None and gap <= CLICK_GUARD_RADIUS_PX:
                # 同一张弹窗里的另一个词，刚才已经点过一下了：这一下省掉。
                # 目标的冷却照常计时，之后弹窗还在照样会补点，不会真的漏。
                worker._guard_skipped += 1
                if det.target_name not in worker._guard_logged:
                    worker._guard_logged.add(det.target_name)
                    self.log("info", f"[{worker.monitor.name}] 命中「{det.target_name}」但 "
                                     f"{gap:.0f}px 外刚刚点过（同一张弹窗），先跳过；"
                                     f"要每见每点就把「点击与启动」里的同弹窗防重复设为 0。")
                continue
            try:
                self.clicker.click(click_x, click_y)
            except Exception as exc:
                self.log("error", f"[{worker.monitor.name}] 点击「{det.target_name}」失败: {exc}")
                continue
            self.click_guard.record(worker.monitor.name, click_x, click_y)
            worker._ocr_force = True
            self.cooldowns.mark(worker.monitor.name, det.target_name)
            worker.state.clicks += 1
            self.log("info", f"[{worker.monitor.name}] 命中「{det.target_name}」({det.source}, "
                             f"置信度 {det.confidence:.2f}) → 点击 ({click_x}, {click_y})")
        # 借来的置顶在这一帧点完就还，别让直播窗一直压在别的窗口上面
        worker.release_raised_window()

    # ---------- 事件与日志 ----------
    def log(self, level: str, message: str) -> None:
        self.events.put(Event("log", {"level": level, "message": message, "time": time.time()}))
        # 日志同时落盘：界面文本框只留最近 400 行，排障时要看更早的命中/异常
        if self.run_logger is not None:
            self.run_logger.write(level, message)

    def _emit_state(self) -> None:
        self.events.put(Event("state", {"running": self.running}))

    def _make_clicker(self, settings: Settings) -> Clicker:
        backend, humanize = self._clicker_config(settings)
        return Clicker(backend=backend, humanize=humanize)

    @staticmethod
    def _clicker_config(settings: Settings) -> tuple[str, bool]:
        return ("pydirectinput" if settings.use_pydirectinput else "pyautogui",
                settings.humanize_click)
