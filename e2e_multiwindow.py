"""多窗口 E2E：三个假直播窗口 + 热更新（移除 B / 新增 C），校验逐窗口统计与 stats 持久化。"""
from __future__ import annotations

import os
import queue
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.capture import ScreenCapture, set_process_dpi_awareness
from core.config import Monitor, Settings, Target
from core.engine import Engine

set_process_dpi_awareness()

BUTTON = (600, 260)          # 假窗口尺寸；按钮画在窗口底部
_cap = ScreenCapture()


def _regions() -> dict[str, tuple[int, int, int, int]]:
    """按当前屏幕排三个假窗口。

    写死坐标会在分辨率变小时被 Windows 裁掉：窗口仍在、但按钮那一条落在屏幕外，
    截图截到黑边，测试就以「识别不到」的形式失败，看起来像引擎的 bug。
    """
    left, top, width, height = _cap.full_region
    bw, bh = BUTTON
    gap, bottom = 40, 120      # bottom 留够，别让任务栏压住按钮那一条
    if width < bw * 3 + gap * 2 or height < bh + bottom + gap:
        raise SystemExit(f"屏幕 {width}x{height} 放不下三个 {bw}x{bh} 的假窗口，"
                         f"请把分辨率调到 {(bw * 3 + gap * 2)}x{(bh + bottom + gap)} 以上再跑。")
    y = top + height - bh - bottom
    return {name: (left + i * (bw + gap), y, bw, bh) for i, name in
            enumerate(("窗口A", "窗口B", "窗口C"))}


REGION = _regions()
LAYOUTS = {
    "窗口A": (REGION["窗口A"][:2], ["签到", "奖券"]),
    "窗口B": (REGION["窗口B"][:2], ["签到"]),
    "窗口C": (REGION["窗口C"][:2], ["奖券"]),
}


class FakeWindow(tk.Toplevel):
    def __init__(self, root, name):
        (left, top), labels = LAYOUTS[name]
        w, h = REGION[name][2], REGION[name][3]
        super().__init__(root)
        self.overrideredirect(True)
        self.geometry(f"{w}x{h}+{left}+{top}")
        self.attributes("-topmost", True)
        canvas = tk.Canvas(self, width=w, height=h, bg="#101418", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(w // 2, 24, text=f"直播间 {name}", fill="#e8e8e8", font=("Microsoft YaHei", 14))
        x = 40
        for text in labels:
            canvas.create_rectangle(x, h - 92, x + 150, h - 34, fill="#ff4d4f", outline="")
            canvas.create_text(x + 75, h - 63, text=text, fill="white", font=("Microsoft YaHei", 16, "bold"))
            x += 155   # 故意紧挨着摆：OCR 会把两个按钮读成同一个文本框
        self.lift()
        self.focus_force()


class Rec:
    def __init__(self):
        self.lock = __import__("threading").Lock()
        self.clicks: list[tuple[str, int, int]] = []

    def click(self, x, y, **kw):
        with self.lock:
            self.clicks.append((f"{x},{y}", x, y))


def drain(eng):
    logs = []
    while True:
        try:
            ev = eng.events.get_nowait()
        except queue.Empty:
            return logs
        if ev.kind == "log":
            logs.append(f"{ev.payload['level']}: {ev.payload['message']}")


def settle(root, names, pumps=10):
    """等窗口真的画出来，并确认每个按钮的红块确实落在截图里。

    量尺寸查不出问题：Tk 报的是它自己要的尺寸，窗口被屏幕边缘裁掉时照样报 600x260。
    只有截屏能看到真相，而裁掉或没画出来的部分恰好是底部的按钮，于是失败会长成
    「OCR 认不到」，看起来像引擎退化。
    """
    for _ in range(pumps):
        root.update()
    deadline = time.monotonic() + 3.0
    while True:
        missing = [(name, text) for name in names
                   for text, ok in _buttons_seen(name).items() if not ok]
        if not missing:
            return
        if time.monotonic() > deadline:
            detail = "；".join(f"{name} 区域 {REGION[name]} 缺 {text}" for name, text in missing)
            raise SystemExit(f"截屏里找不到按钮的红块：{detail}。"
                             f"虚拟屏幕 {_cap.full_region}，窗口要么没画出来要么被裁到屏幕外，"
                             f"测试前提不成立。")
        root.update()
        time.sleep(0.05)


def _buttons_seen(name) -> dict[str, bool]:
    """每个按钮的红块有没有真的出现在截图里。取左上角那块纯色，正中心是白字。"""
    left, top, w, h = REGION[name]
    frame = _cap.grab((left, top, w, h))
    out = {}
    for i, text in enumerate(LAYOUTS[name][1]):
        b, g, r = (int(v) for v in frame[h - 82, 40 + i * 155 + 15][:3])
        out[text] = r > 150 and r - g > 60 and r - b > 60
    return out


def main():
    root = tk.Tk()
    root.geometry("100x100+0+0")
    root.attributes("-alpha", 0.01)
    win = {n: FakeWindow(root, n) for n in ("窗口A", "窗口B")}
    settle(root, ("窗口A", "窗口B"), pumps=15)
    time.sleep(1.0)

    settings = Settings(scan_interval_sec=0.8, jitter_sec=0.2, ocr_max_side=1600, ocr_concurrency=2)
    monitors = [Monitor("窗口A", list(REGION["窗口A"])), Monitor("窗口B", list(REGION["窗口B"]))]
    targets = [Target("签到", ocr_keywords=["签到"], cooldown_sec=600.0),
               Target("奖券", ocr_keywords=["奖券"], cooldown_sec=600.0, monitors=["窗口A", "窗口C"])]
    eng = Engine(settings, monitors, targets)
    rec = Rec()
    eng.clicker = rec
    assert eng.start()
    logs = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 6.0:
        root.update()
        logs += drain(eng)
        time.sleep(0.05)

    states = eng.monitor_states()
    print("== 运行中 ==")
    for name, st in states.items():
        print(f"  {name}: scans={st['scans']} hits={st['hits']} clicks={st['clicks']}")
    live = dict(stats_snapshot(eng))
    print("  stats:", live)
    print("  clicks:", rec.clicks)
    assert states["窗口A"]["clicks"] == 2, states["窗口A"]
    assert states["窗口B"]["hits"] == 1, states["窗口B"]  # 奖券 限定窗口，不应命中 B
    assert len({(c[1], c[2]) for c in rec.clicks}) == len(rec.clicks), \
        f"两个目标算出了同一个点击点: {rec.clicks}"

    # 热更新：移除窗口B，新增窗口C
    win["窗口C"] = FakeWindow(root, "窗口C")
    settle(root, ("窗口C",))
    time.sleep(0.8)
    monitors2 = [Monitor("窗口A", list(REGION["窗口A"])), Monitor("窗口C", list(REGION["窗口C"]))]
    eng.update_config(settings, monitors2, targets)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 6.0:
        root.update()
        logs += drain(eng)
        time.sleep(0.05)

    print("== 热更新后 ==")
    for name, st in eng.monitor_states().items():
        print(f"  {name}: scans={st['scans']} hits={st['hits']} clicks={st['clicks']}")
    print("  stats:", eng.stats)
    eng.stop()
    logs += drain(eng)
    after = dict(eng.stats)
    print("== 停止后 stats ==", after)
    print("---- 日志 ----")
    for line in logs:
        print("  ", line)

    st = eng.monitor_states()
    assert set(st) == {"窗口A", "窗口C"}, f"states 未随热更新裁剪: {list(st)}"
    assert st["窗口A"]["clicks"] == 2, st["窗口A"]
    assert st["窗口C"]["clicks"] == 1, st["窗口C"]
    assert "窗口B" not in st or st.get("窗口B", {}).get("clicks") == 1, st.get("窗口B")
    assert after == live_from(st), f"停止后 stats 归零: {after}"
    assert live["clicks"] == 3, live
    print("OK 多窗口 + 热更新 + 停止后统计 全部通过")
    for w in win.values():
        w.destroy()
    root.destroy()


def stats_snapshot(eng):
    return eng.stats


def live_from(states):
    return {"monitors": len(states),
            "scans": sum(s["scans"] for s in states.values()),
            "hits": sum(s["hits"] for s in states.values()),
            "clicks": sum(s["clicks"] for s in states.values())}


if __name__ == "__main__":
    main()
