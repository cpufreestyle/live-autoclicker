"""GUI 冒烟：新增窗口 → 测试扫描 → 停用/复制目标 → 参数热更新 → 自动运行 → 热键启停。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.capture import set_process_dpi_awareness
from core.config import Monitor
from core.engine import Event
import ui.main_window as mw

set_process_dpi_awareness()
REGION = (200, 440, 600, 260)
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def pump(root, seconds=0.2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


class FakeWindow(tk.Toplevel):
    def __init__(self, root):
        super().__init__(root)
        w, h = REGION[2], REGION[3]
        self.overrideredirect(True)
        self.geometry(f"{w}x{h}+{REGION[0]}+{REGION[1]}")
        self.attributes("-topmost", True)
        cv = tk.Canvas(self, width=w, height=h, bg="#101418", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        x = 40
        for text in ("签到", "奖券"):
            cv.create_rectangle(x, h - 92, x + 150, h - 34, fill="#ff4d4f", outline="")
            cv.create_text(x + 75, h - 63, text=text, fill="white", font=("Microsoft YaHei", 16, "bold"))
            x += 155
        self.lift()
        self.focus_force()


class FakeClicker:
    """记录点击而不真动鼠标。"""

    def __init__(self):
        self.clicks: list[tuple[int, int]] = []

    def click(self, x, y, **kwargs):
        self.clicks.append((x, y))


class StubMonitorDialog(tk.Toplevel):
    """代替模态对话框：直接给出一个绑定到假窗口区域的 Monitor。"""
    result = None

    def __init__(self, master, src, picker_factory=None, known_names=()):
        super().__init__(master)
        self.withdraw()
        self.result = Monitor(name="测试窗口", region=list(REGION), enabled=True,
                              scan_interval_sec=1.0, ocr_max_side=None)
        self.after(0, self.destroy)


def main() -> int:
    backup = tempfile.mkdtemp(prefix="cfgbk")
    for fn in os.listdir(CONFIG_DIR):
        shutil.copy2(os.path.join(CONFIG_DIR, fn), backup)
    mw.MonitorDialog = StubMonitorDialog
    mw.messagebox = type("MB", (), {"askyesno": staticmethod(lambda *a, **k: True),
                                    "showinfo": staticmethod(lambda *a, **k: None),
                                    "showwarning": staticmethod(lambda *a, **k: None),
                                    "showerror": staticmethod(lambda *a, **k: None)})()
    root = tk.Tk()
    root.geometry("100x100+0+0")
    root.attributes("-alpha", 0.01)
    fake = FakeWindow(root)
    pump(root, 1.0)
    # 先关掉自动运行，免得构造主窗口的瞬间就真开始扫描；后面再单独验证该开关
    st = mw.load_settings()
    st.auto_start = False
    mw.save_settings(st)
    try:
        win = mw.MainWindow(root)
        pump(root, 0.4)
        assert win.mon_tree.get_children(), "监控窗口表格未渲染"
        rows = {win.mon_tree.item(i, "text") for i in win.mon_tree.get_children()}
        print("初始窗口:", rows, "目标:", {win.tree.item(i, "text") for i in win.tree.get_children()})

        win._add_monitor()          # 通过 stub 新增
        pump(root, 0.3)
        names = [win.mon_tree.item(i, "text") for i in win.mon_tree.get_children()]
        assert "测试窗口" in names, names
        print("新增后:", names)
        win.monitors[0].enabled = False   # 全屏扫描太慢，测试里只留假窗口
        win._refresh_monitors()

        win.mon_tree.selection_set(win.mon_tree.get_children()[-1])
        win._test_scan()
        pump(root, 8.0)
        log = win.log_text.get("1.0", "end")
        hits = [ln for ln in log.splitlines() if "测试命中" in ln]
        print("测试扫描日志:", hits or "(无命中)")
        assert len(hits) >= 2, f"应命中签到+奖券两个目标，实际 {hits}"

        win._toggle_monitor()       # 停用该窗口
        pump(root, 0.3)
        last = win.mon_tree.get_children()[-1]
        vals = win.mon_tree.item(last, "values")
        assert vals[0] != "✓", vals
        assert win.mon_tree.selection(), "刷新表格后选中行丢了，无法连续操作"
        print("停用后行:", vals)
        win._toggle_monitor()       # 选中项还在，直接再点一次恢复启用
        pump(root, 0.3)
        assert win.mon_tree.item(win.mon_tree.get_children()[-1], "values")[0] == "✓"

        win.tree.selection_set(win.tree.get_children()[0])
        win._copy_target_to_monitors()
        pump(root, 0.3)
        scoped = [win.tree.item(i, "values")[1] for i in win.tree.get_children()]
        print("复制后生效窗口列:", scoped)
        assert any("," in str(s) or "窗口" in str(s) for s in scoped), scoped
        assert not win.targets[0].enabled, "复制后原目标应停用，否则会和副本重复点击"

        # 参数面板写入 + 引擎热更新
        win.vars["ocr_min_side"].set(736)
        win.vars["scan_interval_sec"].set(2.0)
        win._apply_settings(persist=False)
        assert win.engine.settings.ocr_min_side == 736, win.engine.settings
        win.vars["ocr_min_side"] = tk.StringVar(value="abc")   # 模拟半截输入
        assert win._int_var("ocr_min_side", 736, 0, 4096) == 736, "非法输入应沿用上次的值"
        win.vars["ocr_min_side"] = tk.StringVar(value="1200")
        assert win._int_var("ocr_min_side", 736, 0, 4096) == 1200
        print("参数热更新 OK")

        # ---------- 打开后自动运行 ----------
        assert not win.engine.running, "auto_start=false 时不该自己启动"
        win.engine._make_clicker = lambda settings: FakeClicker()   # 别真点鼠标
        win.engine.clicker = FakeClicker()
        win.vars["auto_start"].set(True)
        win._apply_settings(persist=False)
        assert win.settings.auto_start
        win._schedule_auto_start()
        pump(root, 8.0)
        assert win.engine.running, "勾选自动运行后未自动启动\n" + win.log_text.get("1.0", "end")[-1500:]
        assert "停止" in win.run_btn.cget("text"), win.run_btn.cget("text")
        print("自动启动 OK，点击被记录:", win.engine.clicker.clicks)
        win.engine.stop()
        pump(root, 0.5)

        # ---------- 热键：监听线程只塞事件，动作必须在主线程完成 ----------
        assert not win.engine.running
        win.events.put(Event("hotkey"))
        pump(root, 8.0)
        assert win.engine.running, "热键事件没能启动引擎\n" + win.log_text.get("1.0", "end")[-800:]
        win.events.put(Event("hotkey"))
        pump(root, 6.0)
        assert not win.engine.running, "热键事件没能停止引擎"
        print("热键走事件队列启停 OK")

        # 配置不完整时：只写日志，不弹模态框
        win.monitors = []
        win._refresh_monitors()
        win._schedule_auto_start()
        pump(root, 5.0)
        assert not win.engine.running
        log = win.log_text.get("1.0", "end")
        assert "无法自动启动" in log, log[-300:]
        print("自动启动被拦截 OK")
        print("OK GUI 冒烟通过")
        return 0
    finally:
        for fn in os.listdir(backup):
            shutil.copy2(os.path.join(backup, fn), os.path.join(CONFIG_DIR, fn))
        shutil.rmtree(backup, ignore_errors=True)
        root.destroy()


if __name__ == "__main__":
    sys.exit(main())
