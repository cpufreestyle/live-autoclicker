"""按标题跟随 E2E：假直播窗口挪位置、裁掉弹幕带、窗口消失时暂停。

覆盖固定区域模式测不到的三件事（用户的直播窗口位置会变，这是选这套方案的原始原因）：
  1. 每轮现取矩形——窗口挪位置后，点击坐标必须跟着走，而不是停在老地方；
  2. crop_bottom 把窗口底部的弹幕带切掉，弹幕里的「福袋」不再被当成按钮；
  3. 目标窗口被关掉/最小化时只提醒一次并暂停，窗口回来自动继续。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.capture import set_process_dpi_awareness
from core.config import Monitor, Settings, Target
from core.engine import Engine

set_process_dpi_awareness()

SIZE = (600, 300)
AT = (240, 440)
MOVED = (600, 920)
FAKE_TITLE = "直播 · 演示间"
CROP = 60                     # 切掉底部这条弹幕
DANMAKU_Y = SIZE[1] - 26      # 弹幕行的 y（相对窗口），必须落在被裁掉的那一段里


class FakeLive(tk.Toplevel):
    """无边框假直播窗口：中间两个按钮，底部一条带关键词的弹幕。"""

    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root)
        self.overrideredirect(True)
        self.geometry(f"{SIZE[0]}x{SIZE[1]}+{AT[0]}+{AT[1]}")
        self.attributes("-topmost", True)
        canvas = tk.Canvas(self, width=SIZE[0], height=SIZE[1], bg="#101418",
                           highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(SIZE[0] // 2, 26, text="直播间", fill="#e8e8e8",
                           font=("Microsoft YaHei", 14))
        for x, text in ((40, "签到"), (195, "奖券")):
            canvas.create_rectangle(x, 120, x + 150, 178, fill="#ff4d4f", outline="")
            canvas.create_text(x + 75, 149, text=text, fill="white",
                               font=("Microsoft YaHei", 16, "bold"))
        canvas.create_text(12, DANMAKU_Y, anchor="w", text="福袋 还有多久", fill="#dfe1e5",
                           font=("Microsoft YaHei", 14, "bold"))
        self.lift()

    def rect(self) -> tuple[int, int, int, int]:
        self.update_idletasks()
        return self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height()


class Follow:
    """主线程刷新的假窗口矩形；扫描线程只读这个元组，不碰 Tk 控件。"""

    def __init__(self, win: FakeLive) -> None:
        self._win = win
        self.rect: tuple[int, int, int, int] | None = None
        self.visible = True
        self.calls = 0

    def refresh(self) -> None:
        self.rect = self._win.rect() if self.visible else None

    def find(self, monitor):
        """引擎现在把整个 Monitor 交给 finder（身份可能是标题/类名/进程名三选或几项组合），
        所以这里按三项身份分别比，命中任一就算找到。"""
        self.calls += 1
        fields = [getattr(monitor, "window_title", ""),
                  getattr(monitor, "window_class", ""),
                  getattr(monitor, "window_process", "")]
        hits = [f for f in fields if f and f in FAKE_TITLE]
        return self.rect if hits else None


class Rec:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clicks: list[tuple[int, int]] = []

    def click(self, x, y, **kw) -> None:
        with self.lock:
            self.clicks.append((x, y))

    def clear(self) -> None:
        with self.lock:
            self.clicks = []


def drain(eng: Engine) -> tuple[list[str], dict[str, tuple[int, int, int, int]]]:
    """取走事件队列，返回（日志行，{目标名: 绝对框}）。高亮框就是引擎准备点的位置。"""
    logs: list[str] = []
    marks: dict[str, tuple[int, int, int, int]] = {}
    while True:
        try:
            ev = eng.events.get_nowait()
        except queue.Empty:
            return logs, marks
        if ev.kind == "log":
            logs.append(f"{ev.payload['level']}: {ev.payload['message']}")
        elif ev.kind == "highlight":
            marks[ev.payload["label"].split("·", 1)[1]] = ev.payload["box"]


def run(root: tk.Tk, eng: Engine, follow: Follow, seconds: float
        ) -> tuple[list[str], dict[str, tuple[int, int, int, int]]]:
    logs: list[str] = []
    marks: dict[str, tuple[int, int, int, int]] = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        follow.refresh()
        new_logs, new_marks = drain(eng)
        logs += new_logs
        marks.update(new_marks)
        time.sleep(0.05)
    return logs, marks


def monitor(crop: int) -> Monitor:
    return Monitor("直播A", None, window_title="直播", crop_bottom=crop)


def main() -> int:
    root = tk.Tk()
    root.geometry("100x100+0+0")
    root.attributes("-alpha", 0.01)
    win = FakeLive(root)
    follow = Follow(win)
    for _ in range(15):
        root.update()
    follow.refresh()
    start_rect = follow.rect
    time.sleep(1.0)

    settings = Settings(scan_interval_sec=0.8, jitter_sec=0.2)
    targets = [Target(n, ocr_keywords=[n], cooldown_sec=1.0) for n in ("签到", "奖券", "福袋")]
    eng = Engine(settings, [monitor(0)], targets)
    eng.find_window = follow.find
    eng.clicker = Rec()

    # 1) 不裁：弹幕里的「福袋」也会被当成按钮，说明这一带必须裁掉
    eng.start()
    logs, marks = run(root, eng, follow, 9.0)
    assert {"签到", "奖券", "福袋"} <= set(marks), f"整窗扫描应连弹幕一起命中：{marks}"
    assert marks["福袋"][1] >= start_rect[1] + DANMAKU_Y - 20, (marks["福袋"], start_rect)
    button_y = marks["签到"][1]
    print("1) 未裁切命中：", {k: v[:2] for k, v in sorted(marks.items())})

    # 2) 裁掉底部弹幕带：签到/奖券照点，福袋不再出现（改 crop_bottom 会重启该窗口线程）
    eng.reset_cooldowns()
    eng.update_config(settings, [monitor(CROP)], targets)
    logs2, _ = run(root, eng, follow, 3.0)   # 先让旧配置那一帧收尾，它还会命中弹幕
    assert any("配置已变更" in line for line in logs2), logs2
    _, marks = run(root, eng, follow, 9.0)
    assert {"签到", "奖券"} <= set(marks), f"裁掉弹幕带不该影响视频区按钮：{marks}"
    assert "福袋" not in marks, f"crop_bottom={CROP} 没切掉弹幕：{marks}"
    assert abs(marks["签到"][1] - button_y) <= 3, "裁底部不该把保留区的坐标整体平移"
    print("2) 裁切后命中：", {k: v[:2] for k, v in sorted(marks.items())})

    # 3) 窗口挪位置：点击坐标要跟着走，不用重新框选
    eng.reset_cooldowns()
    eng.clicker.clear()
    win.geometry(f"+{MOVED[0]}+{MOVED[1]}")
    root.update()
    follow.refresh()
    new_rect = follow.rect
    assert abs(new_rect[0] - MOVED[0]) <= 2 and abs(new_rect[1] - MOVED[1]) <= 2, new_rect
    _, marks = run(root, eng, follow, 9.0)
    assert {"签到", "奖券"} <= set(marks), marks
    dy = new_rect[1] - start_rect[1]
    assert abs(marks["签到"][1] - (button_y + dy)) <= 3, (marks["签到"], button_y, dy)
    assert eng.clicker.clicks, "挪位置后一次都没点击，说明矩形没跟上"
    print(f"3) 窗口挪到 {new_rect[:2]} 后：签到 {marks['签到'][:2]}，比原来低 {dy}px")

    # 4) 目标窗口消失：暂停并只说一次；窗口回来自动继续
    eng.reset_cooldowns()
    follow.visible = False
    follow.refresh()                   # 现在就置空，别等 run() 的第一轮才生效
    # 在途那一帧是按旧矩形截的，仍会点击；等它扫完、再安静过一整个扫描间隔才清点。
    # 只等 busy 变 False 是不够的：帧与帧之间本来就是 False，会刚好卡在空隙上清早了。
    idle_for = 0.0
    deadline = time.monotonic() + 12.0
    logs3: list[str] = []
    while time.monotonic() < deadline and idle_for < settings.scan_interval_sec + 1.0:
        root.update()
        time.sleep(0.05)
        idle_for = 0.0 if eng.monitor_states()["直播A"]["busy"] else idle_for + 0.05
        logs3 += drain(eng)[0]          # 顺手取走，「找不到标题」就是在这几帧里发的
    eng.clicker.clear()
    run_logs, marks = run(root, eng, follow, 4.0)
    logs3 += run_logs
    lost = [line for line in logs3 if "找不到标题" in line]
    assert len(lost) == 1, f"窗口消失只该提醒一次，不该每帧刷：{lost}"
    assert not eng.clicker.clicks and not marks, f"窗口没了还在点：{eng.clicker.clicks} {marks}"

    follow.visible = True
    root.update()
    logs4, marks = run(root, eng, follow, 6.0)
    found = [line for line in logs4 if "已找到目标窗口" in line]
    assert len(found) == 1, found
    assert {"签到"} <= set(marks), f"窗口回来后应继续扫描：{marks}"
    assert eng.clicker.clicks, "回来后应重新点击"
    print("4) 消失只提醒一次、回来后自动继续")

    eng.stop()
    print("OK 按标题跟随（挪位置 / 裁弹幕 / 窗口消失暂停）全部通过")
    win.destroy()
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
