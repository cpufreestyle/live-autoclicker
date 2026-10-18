"""离线回归：不截屏、不点击、不开界面，只验纯逻辑。

覆盖最容易悄悄坏掉的事：
  1. OCR 合并文本框要按关键词切成两个点击点，且不能算出越界框；
  2. 手改 yaml 的坏值要自动校正并给出中文说明，YAML 语法错误要抛 ConfigError；
  3. 模板缺失/损坏要进错误通道，而且每个窗口只报一次；
  4. 冷却按「窗口|按钮」记键，不同窗口互不影响；
  5. 命中点落在本程序自己窗口里时要丢掉，否则整屏扫描会点到自己；
  6. 按标题跟随时矩形每轮现取，找不到窗口要暂停而不是退回整屏；
  7. OCR 的 CPU 占用要被限住（默认按核数开线程会把整机吃满），且帧间必须留空隙；
  8. 全员冷却的那一帧不许再跑 OCR，方向分类不许建会话——两者都是纯白跑的 CPU。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core.config import (ConfigError, Monitor, Settings, Target, _build_monitors,
             _build_settings, _build_targets, _read_yaml, _write_yaml, abs_path)
from core.detector import (CLICK_GUARD_RADIUS_PX, ClickGuard, CooldownTracker, Detector,
                          SelfWindowGuard, resolve_region)
from core.engine import Engine, MonitorWorker
from core.ocr import OcrEngine, OcrHit, compact_text

MISSING_TEMPLATE = "templates/__does_not_exist__.png"


def _stub_ocr(hits):
    return type("Stub", (), {"available": True,
             "recognize": staticmethod(lambda *a, **k: hits)})()


# 点击链路测试用的假句柄（7777）在系统里并不存在，`window_alive` 一句 IsWindow 就会
# 判成「直播窗口刚被关掉」，整轮跳过点击、一个都点不出去。可系统恰好有个真窗口占用
# 同一个号时它又会变回 True——同一套测试时绿时红，全看句柄表当下长什么样。
# 这些测试必须显式接管它，只验自己那段逻辑。


def test_merged_keyword_box() -> None:
    """识别网络把两个按钮读成一个框时，要按关键词各自收窄。"""
    hits = [OcrHit("签到奖券", 0.77, 72, 146, 200, 50),
            OcrHit("凑单有礼", 0.9, 10, 10, 100, 30)]
    det = Detector(Settings(ocr_min_score=0.5), Monitor("窗口A", [0, 0, 800, 600]))
    det.ocr = _stub_ocr(hits)
    targets = [Target("签到", ocr_keywords=["签到"]), Target("奖券", ocr_keywords=["奖券"])]

    found = {t.name: det._ocr_matches(t, hits) for t in targets}
    assert len(found["签到"]) == 1 and len(found["奖券"]) == 1, found
    q, j = found["签到"][0], found["奖券"][0]
    assert q.center == (72 + 50, 171) and j.center == (72 + 150, 171), (q.center, j.center)
    assert abs(q.center[0] - j.center[0]) >= 100, (q.center, j.center)

    dets = det.scan(None, targets)
    assert {d.target_name for d in dets} == {"签到", "奖券"}
    assert len(Detector.dedupe_same_location(dets)) == 2   # 切分后不再被当成同一位置

    # 关键词带空格/标点也要能对上：两边共用同一个归一化函数
    assert compact_text("立即 签~到") == "立即签到"
    spaced = [OcrHit("立即 签 到！", 0.6, 0, 0, 100, 20)]
    matched = det._ocr_matches(Target("签到", ocr_keywords=["立即签到"]), spaced)
    assert len(matched) == 1, matched


def test_click_prefers_full_button_text_over_danmaku() -> None:
    """弹幕里出现「领券」两个字时，要点的是「立即领券」那个按钮。

    直播间的聊天列里「第1步先领券」这种句子太常见，关键词写得短就会命中它，
    于是点击落在弹幕上而不是弹窗按钮上——用户看到的就是「没点中」。
    同一帧里两种关键词都命中时，只留关键词更完整（更长）的那些框。
    """
    from core.detector import Detector

    hits = [OcrHit("第1步先领券", 0.95, 700, 166, 246, 46),    # 弹幕，短关键词
            OcrHit("立即领券", 0.72, 90, 300, 380, 64),            # 弹窗里的真按钮，置信度还更低
            OcrHit("兑换券", 0.66, 20, 60, 200, 44)]                   # 弹窗标题，根本不命中
    target = Target("兑换券", ocr_keywords=["立即领券", "立即领取",
                                                    "领券", "立即领"])
    det = Detector(Settings(ocr_min_score=0.5), Monitor("直播间", [0, 0, 800, 600]))
    det.ocr = _stub_ocr(hits)

    matched = det._ocr_matches(target, hits)
    assert len(matched) == 1, matched                       # 只剩按钮那一框
    assert matched[0].center == (90 + 190, 300 + 32), matched[0].center

    dets = det.scan(None, [target])
    assert [d.target_name for d in dets] == ["兑换券"], dets
    assert dets[0].abs_x == 280 and dets[0].abs_y == 332, dets[0]
    assert dets[0].source == "ocr"

    # 只有弹幕时照点不诟：不能因为“找不到更长的”就一律不点
    only_danmaku = [OcrHit("第1步先领券", 0.9, 700, 166, 246, 46)]
    assert det._ocr_matches(target, only_danmaku), "只有弹幕时仍然要能命中"


def test_keyword_box_boundaries() -> None:
    """关键词不在文本里、或者贴着边界时，不能算出越界或零宽的框。"""
    det = Detector(Settings(), Monitor("窗口A", [0, 0, 800, 600]))
    hits = [OcrHit("奖券", 0.9, 100, 50, 40, 20)]

    miss = det._ocr_matches(Target("签到", ocr_keywords=["签到"]), hits)
    assert len(miss) == 0, miss

    box = det._ocr_matches(Target("奖券", ocr_keywords=["奖券"]), hits)[0]
    assert box.x >= 100 and box.x + box.w <= 140, box
    assert box.w >= 1 and box.h >= 1, box


def test_config_coercion() -> None:
    """坏值一律退回可用值，并且留下一句中文说明。"""
    notes: list[str] = []
    settings = _build_settings({
        "scan_interval_sec": "0.01",     # 太快，夹到下限
        "match_threshold": "abc",         # 不是数字，沿用默认
        "ocr_concurrency": 999,           # 会打爆内存，夹到上限
        "auto_start": "false",            # 字符串 false 不等于 False
        "hotkey_toggle": "  ",            # 空热键，沿用默认
        "typo_key": 1,                     # 拼错的键必须说出来
    }, notes.append)
    assert settings.scan_interval_sec == 0.2, settings
    assert settings.match_threshold == 0.85, settings
    assert settings.ocr_concurrency == 16, settings
    assert settings.auto_start is False, settings
    assert settings.hotkey_toggle == "f1", settings
    assert any("typo_key" in n for n in notes), notes

    notes.clear()
    monitors = _build_monitors([
        {"name": "窗口A", "region": [0, 0, 600, 260]},
        {"name": "窗口B", "region": [1, 2, 3]},          # 少一项 → 全屏
        {"name": "窗口C", "region": [0, 0, -5, 10]},     # 宽为负 → 全屏
        {"name": "窗口A", "region": None},                 # 重名 → 改名
        {"region": [0, 0, 10, 10]},                       # 缺 name → 跳过
    ], notes.append)
    assert [m.name for m in monitors] == ["窗口A", "窗口B", "窗口C", "窗口A2"], monitors
    assert monitors[1].region is None and monitors[2].region is None, monitors
    assert len(notes) >= 4, notes

    notes.clear()
    targets = _build_targets([
        {"name": "签到", "ocr_keywords": "签到", "click_offset": [5]},   # 单个偏移 + 裸字符串关键词
        {"name": "奖券", "cooldown_sec": "x", "match_threshold": 9},
        {"name": "空目标"},
    ], notes.append)
    assert targets[0].ocr_keywords == ["签到"], targets[0]
    assert targets[0].click_offset == [0, 0], targets[0]
    assert targets[1].cooldown_sec == 30.0 and targets[1].match_threshold == 1.0, targets[1]
    assert not targets[2].has_detector()
    assert any("空目标" in n for n in notes), notes


def test_template_errors_reported_once() -> None:
    """模板文件不见了要报错，但不能每帧刷一条同样的日志。"""
    det = Detector(Settings(), Monitor("窗口A", [0, 0, 800, 600]))
    det.ocr = _stub_ocr([])
    target = Target("签到", template=MISSING_TEMPLATE)

    first = det.scan(None, [target])
    errors = det.pop_errors()
    assert not first and len(errors) == 1, (first, errors)
    assert "模板" in errors[0] and "窗口A" in errors[0], errors[0]

    det.scan(None, [target])
    assert det.pop_errors() == []

    det.scan(None, [target, Target("奖券", template=MISSING_TEMPLATE)])
    assert det.pop_errors() == []   # 同一个坏路径不重复报

    det2 = Detector(Settings(), Monitor("窗口A", [0, 0, 800, 600]))
    det2.ocr = _stub_ocr([])
    det2.scan(None, [Target("奖券", template=MISSING_TEMPLATE)])
    assert len(det2.pop_errors()) == 1   # 新检测器要重新报一次


def test_abs_path_resolution() -> None:
    """模板走的是相对路径，这一条以前用的是不存在的 os.isabs，配了模板必崩。"""
    assert abs_path("") == ""
    assert abs_path("templates/a.png").replace("\\", "/").endswith("/templates/a.png")
    assert os.path.isabs(abs_path("templates/a.png"))
    assert abs_path("D:\\x\\a.png") == "D:\\x\\a.png"


def test_yaml_errors_are_actionable() -> None:
    """语法错误要变成一句能看懂的话；保存要原子，别留下半截文件或临时文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "targets.yaml")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("targets:\n  - name: 签到\n    ocr_keywords: [签到, 立即签到\n")
        try:
            _read_yaml(bad)
        except ConfigError as exc:
            assert "targets.yaml" in str(exc), exc
        else:
            raise AssertionError("坏 YAML 应该抛 ConfigError，而不是 traceback")

        good = os.path.join(tmp, "settings.yaml")
        _write_yaml(good, {"scan_interval_sec": 1.5})
        assert not os.path.exists(good + ".tmp"), "保存后不该留下 .tmp"
        assert _read_yaml(good) == {"scan_interval_sec": 1.5}


def test_cooldown_is_per_monitor() -> None:
    """同一个按钮在两个窗口各自计时，否则一个直播间会压住另一个。"""
    tracker = CooldownTracker()
    target = Target("签到", ocr_keywords=["签到"], cooldown_sec=30.0)
    assert tracker.ready("窗口A", target)
    tracker.mark("窗口A", target.name)
    assert not tracker.ready("窗口A", target)
    assert tracker.ready("窗口B", target)
    assert tracker.remaining("窗口A", target) > 29
    tracker.reset("窗口A")
    assert tracker.ready("窗口A", target)


def test_self_window_hits_dropped() -> None:
    """整屏扫描会 OCR 到自家界面的文字；该屏幕点最顶层是我们自己的窗口时必须丢掉。"""
    points = {(730, 510): 0x1234,      # 我们自己的主窗口
              (1530, 710): 0x9999}     # 别人的窗口，只是恰好在那儿
    guard = SelfWindowGuard(probe=lambda x, y: points.get((x, y), 0))
    guard.update([0x1234, 0])          # 0 是查不到句柄，不该进名单
    det = Detector(Settings(), Monitor("主屏幕", [0, 0, 1920, 1080]), guard=guard)
    det.ocr = _stub_ocr([OcrHit("签到", 0.9, 700, 500, 60, 20),        # 在自家窗口里
             OcrHit("奖券", 0.9, 1500, 700, 60, 20)])      # 在窗口外
    targets = [Target("签到", ocr_keywords=["签到"]), Target("奖券", ocr_keywords=["奖券"])]

    hits = det.scan(None, targets)
    assert [d.target_name for d in hits] == ["奖券"], hits
    errors = det.pop_errors()
    assert len(errors) == 1 and "自己" in errors[0], errors

    det.scan(None, targets)
    assert det.pop_errors() == []   # 同一件事不每帧刷屏

    # 没登记任何窗口时一律放行，否则非 Windows 环境会一个都点不了
    assert not SelfWindowGuard(probe=lambda x, y: 0x1234).contains(1, 1)


def test_region_resolution() -> None:
    """按标题跟随时每轮现取矩形；找不到窗口宁可暂停，也不能退回整屏去点桌面。"""
    live = (2688, 98, 1085, 1130)

    def finder(monitor):
        # 只认标题：类名/进程名留空时不影响命中
        if monitor.window_class or monitor.window_process:
            return None
        return live if monitor.window_title == "直播" else None

    assert resolve_region(Monitor("静态", [0, 0, 640, 480]), finder).box == (0, 0, 640, 480)
    assert resolve_region(Monitor("全屏", None), finder).box is None

    tracked = resolve_region(Monitor("跟随", window_title="直播", crop_bottom=200), finder)
    assert tracked.ok and tracked.box == (2688, 98, 1085, 930), tracked

    lost = resolve_region(Monitor("跟随", window_title="没有这个窗口"), finder)
    assert not lost.ok and lost.box is None and "没有这个窗口" in lost.error, lost

    # 只填类名或进程名也能跟随：直播窗口的标题常是房间号，一场一变
    by_class = resolve_region(Monitor("类名", window_class="Chrome_WidgetWin_1"),
             lambda m: live if m.window_class else None)
    assert by_class.ok and by_class.box == live, by_class
    by_proc = resolve_region(Monitor("进程", window_process="Weixin.exe"),
             lambda m: live if m.window_process else None)
    assert by_proc.ok and by_proc.box == live, by_proc
    # 只认标题的 finder 不该认准只填了类名的盐…
    mismatch = resolve_region(Monitor("类名", window_class="Chrome_WidgetWin_1"), finder)
    assert not mismatch.ok, mismatch
    # 报错要把当前用的身份都剥说出来，否则用户不知道该改哪一项
    mix = resolve_region(Monitor("组合", window_class="Chrome_WidgetWin_1",
             window_process="Weixin.exe"), lambda m: None)
    assert not mix.ok and "Chrome_WidgetWin_1" in mix.error and "Weixin.exe" in mix.error, mix

    # 裁切高度不小于窗口高度会把画面裁没，要说清楚而不是截一张空图
    too_much = resolve_region(Monitor("跟随", window_title="直播", crop_bottom=99999), finder)
    assert not too_much.ok and "99999" in too_much.error, too_much


def test_window_rect_by_identity() -> None:
    """不能只跟标题识别窗口：直播窗口的标题常常是房间号。

    类名/进程名填几项就要求同时满足几项，且本进程自己的窗口永远排除。"""
    import os as _os

    import core.capture as cap
    from core.capture import window_rect_by

    scenes = {
        11: ("直播A", "Qt5152_QWindowIcon", "Live.exe"),
        22: ("直播B", "Chrome_WidgetWin_1", "Weixin.exe"),
        33: ("自己的窗口", "Chrome_WidgetWin_1", "Weixin.exe"),
    }
    rects = {11: (0, 0, 800, 600), 22: (100, 100, 1200, 900),
             33: (0, 0, 4000, 3000)}
    real_enum = cap.ctypes.windll.user32.EnumWindows
    real_visible = cap.ctypes.windll.user32.IsWindowVisible
    real_pid_of, real_signature = cap._pid_of, cap.window_signature
    real_rect, real_getpid = cap._visible_rect, cap.os.getpid

    def fake_enum(callback, _lparam):
        for hwnd in (11, 22, 33):
            callback(hwnd, 0)
        return 1

    cap.ctypes.windll.user32.EnumWindows = fake_enum
    cap.ctypes.windll.user32.IsWindowVisible = lambda hwnd: True
    cap._pid_of = lambda hwnd: {11: 100, 22: 100, 33: 200}[hwnd]
    cap.window_signature = lambda hwnd: scenes[hwnd]
    cap._visible_rect = lambda hwnd: rects[hwnd]
    cap.os.getpid = lambda: 200        # 33 号窗口属于本进程，应排除
    try:
        assert window_rect_by(window_class_name="Chrome_WidgetWin_1") == rects[22]
        assert window_rect_by(process="Weixin.exe") == rects[22]
        assert window_rect_by(window_class_name="Qt5152", process="Weixin.exe") is None
        assert window_rect_by(window_class_name="Chrome_WidgetWin_1",
             process="Weixin.exe") == rects[22]
        assert window_rect_by(scenes[22][0], window_class_name="Chrome_WidgetWin_1") == rects[22]
        assert window_rect_by(scenes[11][0], window_class_name="Chrome_WidgetWin_1") is None
        # 一项都不填不能把整个桌面当直播窗口，也不能跟自己
        assert window_rect_by() is None
        assert rects[33] not in [rects[22]]
    finally:
        cap.ctypes.windll.user32.EnumWindows = real_enum
        cap.ctypes.windll.user32.IsWindowVisible = real_visible
        cap._pid_of, cap.window_signature = real_pid_of, real_signature
        cap._visible_rect, cap.os.getpid = real_rect, real_getpid

def test_signature_under_cursor() -> None:
    """鼠标锁定：hover 到的窗口要把 (标题, 类名, 进程) 和矩形一起返回；取不到就给 None。

    cursor_pos / window_at / window_signature / _visible_rect 全部打桩，覆盖四种情况：
    正常返回二元组、取不到矩形、光标在桌面(window_at=0)、连光标坐标都取不到。"""
    import core.capture as cap
    from core.capture import signature_under_cursor

    real = {name: getattr(cap, name)
            for name in ("cursor_pos", "window_at", "window_signature", "_visible_rect")}

    identity = ("直播间标题", "Chrome_WidgetWin_1", "Weixin.exe")
    box = (10, 20, 800, 600)
    try:
        cap.cursor_pos = lambda: (100, 150)
        cap.window_at = lambda x, y: 4242 if (x, y) == (100, 150) else 0
        cap.window_signature = lambda hwnd: identity if hwnd == 4242 else ("", "", "")
        cap._visible_rect = lambda hwnd: box if hwnd == 4242 else None
        assert signature_under_cursor() == (identity, box)

        cap._visible_rect = lambda hwnd: None
        assert signature_under_cursor() is None
        cap._visible_rect = lambda hwnd: box if hwnd == 4242 else None

        cap.cursor_pos = lambda: (0, 0)
        assert signature_under_cursor() is None

        cap.cursor_pos = lambda: None
        assert signature_under_cursor() is None
    finally:
        for name, fn in real.items():
            setattr(cap, name, fn)


def test_config_hot_reload_probe() -> None:
    """后台热加载用的探测函数能发现“哪个配置文件被外部改了”，且自身幂等、不报错。"""
    from core.config import changed_config_files, config_files
    real = config_files()
    assert set(real) == {"settings", "monitors", "targets"}
    assert all(os.path.isabs(pth) for pth in real.values())
    first, base = changed_config_files(real, {})
    assert set(first) == set(real)                  # 首轮把现存文件都当基线
    assert changed_config_files(real, base)[0] == []    # 立即再探：无变化

    with tempfile.TemporaryDirectory() as d:
        files = {k: os.path.join(d, k + ".yaml") for k in ("a", "b")}
        for pth in files.values():
            with open(pth, "w", encoding="utf-8") as fh:
             fh.write("seed")
        base = {k: os.path.getmtime(pth) for k, pth in files.items()}
        assert changed_config_files(files, base)[0] == []
        # 用 utime 精确把 b 推到“更新”，避免依赖文件系统 mtime 精度
        os.utime(files["b"], (base["b"] + 10, base["b"] + 10))
        changed, cur = changed_config_files(files, base)
        assert changed == ["b"], changed
        assert cur["b"] == base["b"] + 10 and cur["a"] == base["a"]
        assert changed_config_files(files, cur)[0] == []      # 幂等：再探无变化
        os.remove(files["a"])
        assert changed_config_files(files, cur)[0] == []      # 文件消失不算改动、不抛错


def test_config_watch_applies_changes() -> None:
    """界面热加载控制流：配置文件被改 -> 重轻 + apply；无变化则按兵不动；解析报错则不应用。"""
    import types
    import ui.main_window as mw
    from core.config import ConfigError, changed_config_files

    saved = (mw.load_settings, mw.load_monitors, mw.load_targets)
    calls = {"settings": 0, "monitors": 0, "targets": 0, "update": None, "refreshed": 0}

    def _loader(kind, value):
        def _inner(_report):
            calls[kind] += 1
            return value
        return _inner

    def _install():
        mw.load_settings = _loader("settings", "SET")
        mw.load_monitors = _loader("monitors", ["MON"])
        mw.load_targets = _loader("targets", ["TGT"])

    with tempfile.TemporaryDirectory() as d:
        mp = os.path.join(d, "monitors.yaml")
        with open(mp, "w", encoding="utf-8") as fh:
            fh.write("monitors: []" + chr(10))

        writes = {"n": 0}

        def _write(text: str) -> None:
            """改配置并保证 mtime 一定变。

            Windows 上同一文件快速重写两次，st_mtime 可以完全相同（实测 200 里约 4 次），
            而热更新正是拿 mtime 比对，于是这个测试会偶发「一次都没重载」。
            每次往后推 1ms（远大于 float 存秒级时间戳的精度），不靠 sleep 碰运气。
            """
            with open(mp, "w", encoding="utf-8") as fh:
                fh.write(text + chr(10))
            writes["n"] += 1
            stamp = int(os.path.getmtime(mp) * 1e9) + writes["n"] * 1_000_000
            os.utime(mp, ns=(stamp, stamp))

        obj = types.SimpleNamespace(
            _config_files={"monitors": mp},
            _config_mtimes={},
            _config_warnings=[],
            engine=types.SimpleNamespace(
             update_config=lambda *a: calls.__setitem__("update", a)),
            _refresh_monitors=lambda: calls.__setitem__("refreshed", calls["refreshed"] + 1),
            _refresh_targets=lambda: calls.__setitem__("refreshed", calls["refreshed"] + 1),
            _log=lambda *a, **k: None,
        )
        try:
            _install()
            obj._config_mtimes = dict(changed_config_files(obj._config_files, {})[1])
            mw.MainWindow._watch_config_files(obj)          # no change -> nothing
            assert calls["update"] is None and calls["refreshed"] == 0

            calls.update(settings=0, monitors=0, targets=0)
            _write("monitors: [] changed")
            mw.MainWindow._watch_config_files(obj)          # change -> reload + apply
            assert calls["settings"] == 1 and calls["monitors"] == 1 and calls["targets"] == 1
            assert calls["update"] == ("SET", ["MON"], ["TGT"]), calls["update"]
            assert calls["refreshed"] == 2

            calls.update(settings=0, monitors=0, targets=0)
            mw.MainWindow._watch_config_files(obj)          # idempotent: no reload
            assert calls["settings"] == 0 and calls["monitors"] == 0

            def boom(_report):
             raise ConfigError("broken")
            mw.load_monitors = boom
            calls["update"] = None
            _write("monitors: [] again")
            mw.MainWindow._watch_config_files(obj)          # parse error -> keep, no apply
            assert calls["update"] is None
        finally:
            mw.load_settings, mw.load_monitors, mw.load_targets = saved


def test_scan_uses_captured_region() -> None:
    """跟随时帧矩形每轮都在变：绝对坐标要用截图那一刻的矩形算，不能重读配置。"""
    target = Target("签到", ocr_keywords=["签到"])
    hits = [OcrHit("签到", 0.9, 130, 60, 60, 20)]

    det = Detector(Settings(ocr_min_score=0.5), Monitor("跟随", window_title="直播"))
    det.ocr = _stub_ocr(hits)
    moved = det.scan(None, [target], region=(500, 400, 800, 600))[0]
    assert (moved.abs_x, moved.abs_y) == (660, 470), moved

    fixed = Detector(Settings(ocr_min_score=0.5), Monitor("固定", [900, 900, 800, 600]))
    fixed.ocr = _stub_ocr(hits)
    kept = fixed.scan(None, [target])[0]     # 不传 region 时沿用配置里的区域
    assert (kept.abs_x, kept.abs_y) == (1060, 970), kept


def test_monitor_track_fields_from_yaml() -> None:
    """window_title / window_class / window_process / crop_bottom 要能读进来并参与热更新签名，否则改了配置不重启窗口。"""
    notes: list[str] = []
    monitors = _build_monitors([
        {"name": "A", "window_title": "  直播  ", "crop_bottom": 220},
        {"name": "B", "window_title": "直播", "crop_bottom": -5},      # 夹到下限
        {"name": "C", "window_title": "直播", "crop_bottom": "abc"},   # 非数字 → 不裁
        {"name": "D", "region": [0, 0, 10, 10], "window_title": "   "},
        {"name": "E", "window_class": " Chrome_WidgetWin_1 ", "window_process": " Weixin.exe "},
        {"name": "F", "window_class": "", "window_process": ""},
    ], notes.append)
    assert monitors[0].window_title == "直播" and monitors[0].crop_bottom == 220, monitors
    assert monitors[0].tracked and not monitors[3].tracked, monitors
    assert monitors[1].crop_bottom == 0 and monitors[2].crop_bottom == 0, monitors
    assert any("crop_bottom" in n for n in notes), notes

    # 类名、进程名同样要能读进来（去左右空白），并且独立于标题
    assert monitors[4].window_class == "Chrome_WidgetWin_1", monitors
    assert monitors[4].window_process == "Weixin.exe" and monitors[4].tracked, monitors
    assert not monitors[4].window_title, monitors
    assert not monitors[5].tracked, monitors

    base = Monitor("A", window_title="直播", crop_bottom=220)
    assert base.signature() != Monitor("A", window_title="直播", crop_bottom=100).signature()
    assert base.signature() != Monitor("A", window_title="视频号", crop_bottom=220).signature()
    assert base.signature() == Monitor("A", window_title="直播", crop_bottom=220).signature()
    # 改类名/进程名也得让热更新重启该窗口的线程，否则改了不生效
    assert base.signature() != Monitor("A", window_title="直播", crop_bottom=220,
             window_class="Chrome_WidgetWin_1").signature()
    assert base.signature() != Monitor("A", window_title="直播", crop_bottom=220,
             window_process="Weixin.exe").signature()


def test_ocr_thread_cap() -> None:
    """onnxruntime 默认按逻辑核数开 intra-op 线程，一帧 OCR 实测吃掉 4~6 个核，
    连续扫描时整台电脑会卡死。必须能把线程数压下来，压不下来要说得清。"""
    import cv2
    from core.ocr import cap_cpu_threads

    before = cv2.getNumThreads()
    try:
        err = cap_cpu_threads(3)
        try:
            from rapidocr_onnxruntime import utils as ru
        except ImportError:
            assert err, "没装 rapidocr 时必须返回原因，而不是假装限住了"
            return
        assert err == "", err
        assert cv2.getNumThreads() <= 3, cv2.getNumThreads()
        assert ru.SessionOptions().intra_op_num_threads == 3
        # 幂等：Engine 每次重建 OCR 都会调，重复调用不能叠加或失效
        assert cap_cpu_threads(2) == ""
        assert ru.SessionOptions().intra_op_num_threads == 2
    finally:
        cv2.setNumThreads(before)


def test_ocr_threads_setting() -> None:
    """默认值就是这次卡死的止血点：并发 1、OCR 线程 3；越界要夹住。"""
    notes: list[str] = []
    assert Settings().ocr_concurrency == 1, Settings()
    assert Settings().ocr_threads == 3, Settings()
    settings = _build_settings({"ocr_threads": 99999, "ocr_concurrency": -3,
             "ocr_scan_every": 0}, notes.append)
    assert 1 <= settings.ocr_threads <= (os.cpu_count() or 1), settings
    assert settings.ocr_concurrency == 1, settings
    assert settings.ocr_scan_every == 1, "设 0 会把 OCR 彻底关掉，必须夹回 1"
    # 新增的两个参数同样要被夹在合法区间里：空闲降频最小 1，防重复允许 0（关掉）
    settings = _build_settings({"ocr_idle_every": 0, "click_guard_sec": -1}, notes.append)
    assert settings.ocr_idle_every == 1, settings
    assert settings.click_guard_sec == 0.0, settings
    settings = _build_settings({"ocr_idle_every": 99, "click_guard_sec": 999}, notes.append)
    assert settings.ocr_idle_every == 10 and settings.click_guard_sec == 60.0, settings
    # 不写这两个键就按默认来：空闲隔轮降频 + 2.5 秒同弹窗防重复
    fresh = _build_settings({}, notes.append)
    assert (fresh.ocr_idle_every, fresh.click_guard_sec) == (2, 2.5), fresh


def test_crop_right_removes_side_column() -> None:
    """微信视频号直播间的聊天列表在右边一列：里面全是群名、弹幕，出现「签到/奖券」是常态，
    整窗跟随时会被整列判成命中点过去。crop_bottom 管不到右边，所以要有 crop_right。"""
    mon = Monitor("直播A", window_process="Weixin.exe", background_capture=True, crop_right=500)
    region = resolve_region(mon, lambda m: (86, 0, 1834, 1069))
    assert region.ok, region.error
    assert region.box == (86, 0, 1334, 1069), region.box     # 右边 500px 整列消失

    # 底部和右侧可以同时裁，两者互不影响
    mon2 = Monitor("直播A", window_process="Weixin.exe", background_capture=True,
                   crop_bottom=200, crop_right=500)
    assert resolve_region(mon2, lambda m: (86, 0, 1834, 1069)).box == (86, 0, 1334, 869)

    # 裁得比窗口还宽必须报错而不是给个负宽度（负矩形会一路传到点击坐标）
    bad = resolve_region(mon, lambda m: (86, 0, 400, 600))
    assert not bad.ok and "crop_right" in bad.error, bad

    # 0 / 留空 = 不裁，行为与以前一致
    assert resolve_region(Monitor("A", None, True, None, None, "", "", "Weixin.exe"),
                          lambda m: (0, 0, 100, 50)).box == (0, 0, 100, 50)

    # yaml 里要能读进来，并且要进 signature（否则改了不重启线程不生效）
    notes: list[str] = []
    built = _build_monitors([{"name": "直播A", "window_process": "Weixin.exe",
                              "crop_right": 500, "crop_bottom": 10}], notes.append)
    assert built[0].crop_right == 500 and not notes, (built, notes)
    sig = built[0].signature()
    assert sig[-4] == 10 and sig[-3] == 500 and sig[-2] == 0 and sig[-1] is False, sig

def test_crop_left_removes_session_column() -> None:
    """微信左侧是会话列表（实测 x≈170~950），整窗跟随时群名里出现「签到/奖券」就会被点过去。
    和 crop_right 同一个思路，只是切另一边；crop_bottom 管不到左边，所以要有 crop_left。"""
    mon = Monitor("直播A", window_process="Weixin.exe", background_capture=True, crop_left=800)
    region = resolve_region(mon, lambda m: (86, 0, 1834, 1069))
    assert region.ok, region.error
    # 左边 800px 整列消失，剩下的矩形要跟着右移
    assert region.box == (886, 0, 1034, 1069), region.box

    # 三侧可以同时裁，互不影响
    mon2 = Monitor("直播A", window_process="Weixin.exe", background_capture=True,
                   crop_left=800, crop_bottom=200, crop_right=500)
    assert resolve_region(mon2, lambda m: (86, 0, 1834, 1069)).box == (886, 0, 534, 869)

    # 裁得比窗口还宽必须报错而不是给个负宽度（负矩形会一路传到点击坐标）
    bad = resolve_region(mon, lambda m: (86, 0, 400, 600))
    assert not bad.ok and "crop_left" in bad.error, bad

    # 0 / 留空 = 不裁，行为与以前一致
    assert resolve_region(Monitor("A", None, True, None, None, "", "", "Weixin.exe"),
                          lambda m: (0, 0, 100, 50)).box == (0, 0, 100, 50)

    # yaml 里要能读进来，并且要进 signature（否则改了不重启线程不生效）
    notes: list[str] = []
    built = _build_monitors([{"name": "直播A", "window_process": "Weixin.exe",
                              "crop_left": 800, "crop_bottom": 10}], notes.append)
    assert built[0].crop_left == 800 and not notes, (built, notes)
    sig = built[0].signature()
    assert sig[-4] == 10 and sig[-3] == 0 and sig[-2] == 800 and sig[-1] is False, sig


def test_ocr_provider_patch() -> None:
    """GPU 后端要真能塞到 rapidocr 的会话里，塞不进去不能把 OCR 搞坏。

    实测（真机微信直播窗 1836x1069）：同一个 ocr_max_side 下 DirectML 比 CPU 快一倍
    （750ms vs 1501ms），文字召回一致——所以这条路径必须是「能用就用、不能用就照旧」。
    """
    from core import ocr as ocr_mod
    try:
        from rapidocr_onnxruntime import utils as ru
    except ImportError:
        print("跳过：没装 rapidocr-onnxruntime")
        return

    seen = []
    original_factory = ru.InferenceSession     # 结束时必须原样还回去，别污染后面的用例

    def fake(path, sess_options=None, providers=None, **kwargs):
        seen.append(list(providers or []))
        if any(p[0] == "DmlExecutionProvider" for p in seen[-1]) and not ok_dml:
            raise RuntimeError("directml 起不来")
        return object()

    orig_gpu, orig_real = ocr_mod.gpu_provider, ocr_mod._REAL_SESSION
    ok_dml = True
    try:
        ocr_mod.gpu_provider = lambda: "DmlExecutionProvider"
        ru.InferenceSession = fake

        # auto + 有 GPU：直接上位，并且 CPU 兜底必须留在后面
        assert ocr_mod.apply_provider("auto") == "directml"
        ocr_mod._GpuInferenceSession("m.onnx", providers=[("CPUExecutionProvider", {})])
        assert seen[-1][0][0] == "DmlExecutionProvider", seen[-1]
        assert [p[0] for p in seen[-1]].count("CPUExecutionProvider") == 1, seen[-1]

        # 顺手写法也要认：GPU / dml / DirectML 都是一个意思
        assert ocr_mod.apply_provider("GPU") == "directml"
        assert ocr_mod.apply_provider("dml") == "directml"

        # 后端起不来（驱动太老/没显卡）要退回 CPU，而不是让 OCR 整个不能用了
        ok_dml = False
        ocr_mod._GpuInferenceSession("m.onnx", providers=[("CPUExecutionProvider", {})])
        assert seen[-1] == [("CPUExecutionProvider", {})], seen[-1]

        # 改回 cpu：必须把原来的工厂还回去，不能一直挂着 GPU 补丁
        assert ocr_mod.apply_provider("cpu") == "cpu"
        assert ru.InferenceSession is fake, "改回 cpu 后还原的不是原来那个工厂"

        # 只有 CPU 的机器上，auto 和 directml 都只能退回 CPU，且不打补丁
        ocr_mod.gpu_provider = lambda: ""
        assert ocr_mod.apply_provider("auto") == "cpu"
        assert ocr_mod.apply_provider("directml") == "cpu"
        assert ru.InferenceSession is fake, "没有 GPU 就不许动人家库的会话工厂"
    finally:
        ocr_mod.gpu_provider = orig_gpu
        ru.InferenceSession = original_factory
        ocr_mod._REAL_SESSION = orig_real


def test_ocr_provider_setting() -> None:
    """后端开关要能写进 settings.yaml，且改它必须重启才生效（会话建好就换不了）。"""
    assert Settings().ocr_provider == "auto", Settings()
    notes: list[str] = []
    assert _build_settings({"ocr_provider": "cpu"}, notes.append).ocr_provider == "cpu"
    assert _build_settings({"ocr_provider": "directml"}, notes.append).ocr_provider == "directml"
    assert _build_settings({"ocr_provider": "GPU"}, notes.append).ocr_provider == "directml"
    assert _build_settings({"ocr_provider": "garbage"}, notes.append).ocr_provider == "auto"
    assert any("ocr_provider" in n for n in notes), notes

    settings = Settings(ocr_provider="cpu", ocr_threads=2, ocr_concurrency=1)
    engine = Engine(settings, [Monitor("窗口A", [0, 0, 600, 260])], [])
    assert engine.ocr.provider == "cpu", engine.ocr
    engine.update_config(settings, [], [])          # 同样的配置不该触发重建
    assert engine.ocr.provider == "cpu"
    monitors = [Monitor("窗口A", [0, 0, 600, 260])]
    engine.update_config(Settings(ocr_provider="directml", ocr_threads=2), monitors, [])
    assert engine._ocr_provider == "directml"
    assert engine.ocr.provider == "directml", "没在运行时要能直接换过去"
    engine.start()
    try:
        assert engine.running is True
        engine.update_config(Settings(ocr_provider="cpu", ocr_threads=2), monitors, [])
        assert engine.ocr.provider == "directml", "运行中改后端必须先停，不能半路换会话"
    finally:
        engine.stop()
    engine.update_config(Settings(ocr_provider="cpu", ocr_threads=2), monitors, [])
    assert engine.ocr.provider == "cpu", engine.ocr.provider


def test_worker_keeps_idle_gap() -> None:
    """单帧超过设定间隔时，下一帧前仍要留出空隙，不能零空闲连续扫满 CPU。"""
    engine = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0),
             [Monitor("窗口A", [0, 0, 600, 260])], [])
    worker = MonitorWorker(engine, engine.monitors[0])
    assert abs(worker._idle_wait(1.5, 0.4) - 1.1) < 1e-9, "正常情况就是间隔减去耗时"
    for elapsed in (1.5, 6.0, 30.0):
        gap = worker._idle_wait(1.5, elapsed)
        assert gap >= 1.5 * 0.3, f"帧耗时 {elapsed}s 时空隙只剩 {gap}s，等于不让出 CPU"


def test_cooling_targets_skip_whole_frame() -> None:
    """点过一次后按钮要冷却几十秒；这期间一帧几百毫秒的 OCR 就算检出也点不了，
    必须整个跳过——这是空跑 CPU 的最大一处（实测一帧 0.4~3.5s，取决于画面里有多少字）。"""
    hits = [OcrHit("签到", 0.9, 700, 500, 60, 20)]
    calls: list[int] = []

    class Counting:
        available = True

        def recognize(self, *args, **kwargs):
            calls.append(1)
            return hits

    cooldowns = CooldownTracker()
    det = Detector(Settings(ocr_min_score=0.5), Monitor("窗口A", [0, 0, 800, 600]),
             cooldowns=cooldowns)
    det.ocr = Counting()
    sign = Target("签到", ocr_keywords=["签到"], cooldown_sec=30.0)
    ticket = Target("奖券", ocr_keywords=["奖券"], cooldown_sec=30.0)

    assert len(det.scan(None, [sign])) == 1 and len(calls) == 1, (calls,)
    cooldowns.mark("窗口A", sign.name)

    calls.clear()
    assert det.scan(None, [sign]) == [] and calls == [], "冷却中还在跑 OCR"
    assert det.cooling_gap([sign]) > 29, det.cooling_gap([sign])

    # 还有一个按钮能点时不能偷懒，否则另一个签到按钮就永远等不到
    assert det.cooling_gap([sign, ticket]) == 0
    assert det.scan(None, [sign, ticket]) == [] and len(calls) == 1, calls

    cooldowns.mark("窗口A", ticket.name)
    assert det.cooling_gap([sign, ticket]) > 29
    # 手动重置冷却后要立刻恢复扫描（界面上的「重置冷却」靠这个）
    cooldowns.reset("窗口A")
    assert det.cooling_gap([sign, ticket]) == 0
    # 没有目标可扫时不算冷却，交给原来的逻辑
    assert det.cooling_gap([]) == 0


def test_ocr_skips_angle_classifier() -> None:
    """直播浮层文字不会倒着写，方向分类是每检出一个框多跑一次推理，必须关掉。"""
    eng = OcrEngine(threads=1)
    if not eng.warmup():
        print("     未安装 rapidocr，跳过")
        return
    assert eng._engine.use_angle_cls is False, eng._engine.use_angle_cls
    assert not hasattr(eng._engine, "text_cls"), "分类器会话还在，模型没省下"


def test_ocr_scan_every_gates_ocr() -> None:
    """ocr_scan_every=2 就隔轮才跑 OCR（一帧实测 0.4~3.5s，是最贵的一段），
    但模板匹配必须仍然每轮都跑，否则配了模板的人会被降频误伤。"""
    engine = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_scan_every=2),
             [Monitor("窗口A", [0, 0, 600, 260])], [])
    worker = MonitorWorker(engine, engine.monitors[0])
    due = []
    for _ in range(5):
        worker._round += 1
        due.append(worker._ocr_due_this_round())
    assert due == [True, False, True, False, True], due

    engine.settings.ocr_scan_every = 1
    worker._round += 1
    assert worker._ocr_due_this_round(), "设回 1 就该每轮都跑"

    hits = [OcrHit("签到", 0.9, 100, 100, 60, 20)]
    calls: list[int] = []

    class Counting:
        available = True

        def recognize(self, *args, **kwargs):
            calls.append(1)
            return hits

    det = Detector(Settings(ocr_min_score=0.5), Monitor("窗口A", [0, 0, 800, 600]))
    det.ocr = Counting()
    targets = [Target("签到", ocr_keywords=["签到"])]
    det.scan(None, targets, run_ocr=False)
    assert calls == [], "降频轮不该跑 OCR"
    assert det.scan(None, targets, run_ocr=False) == [], "降频轮也不该报命中"
    assert len(det.scan(None, targets)) == 1 and len(calls) == 1, calls

    # 只有模板通道的目标完全不受降频影响
    calls.clear()
    det2 = Detector(Settings(), Monitor("窗口A", [0, 0, 800, 600]))
    det2.ocr = Counting()
    assert det2.scan(None, [Target("签到", template=MISSING_TEMPLATE)], run_ocr=False) == []
    assert calls == []
    det2.pop_errors()


def test_click_guard_suppresses_same_popup() -> None:
    """同一张弹窗只点一下。

    真机日志里每张兑换券弹窗都被点两次：19:42:57 兑换券 (1503,678)、
    19:42:59 奖券 (1481,543)——两个目标各有一份冷却，互不阻塞，坐标只差 137px，
    同属一张弹窗。第二下必须被 ClickGuard 拦掉，且不能影响别的窗口、不影响远处的新弹窗。
    """
    guard = ClickGuard()
    guard.record("直播A", 1500, 680)

    assert guard.nearest_gap("直播A", 1503, 543, 2.5) is not None
    assert guard.nearest_gap("直播A", 1481, 542, 2.5) < CLICK_GUARD_RADIUS_PX, "同弹窗第二下够近"
    assert guard.nearest_gap("直播A", 100, 900, 2.5) > CLICK_GUARD_RADIUS_PX, "远处的新弹窗不该被算成同一个"
    assert guard.nearest_gap("直播B", 1503, 543, 2.5) is None, "别跨窗口压制"
    assert guard.nearest_gap("直播A", 1503, 543, 0.0) is None, "设 0 就整条关掉"
    # 过了时间窗就该恢复，不然弹窗更新内容后会点不动
    guard._recent["直播A"] = [(time.monotonic() - 9.0, 1500, 680)]
    assert guard.nearest_gap("直播A", 1500, 680, 2.5) is None

    guard.reset("直播A")
    assert guard.nearest_gap("直播A", 1500, 680, 60.0) is None, "重置冷却要连防重复一起清"


def test_reset_cooldowns_clears_click_guard() -> None:
    """界面上的「重置冷却」要把同弹窗防重复的记录一起清掉。

    否则用户点完重置、明明冷却已经归零，却仍然点不动——只剩日志里一句
    「同一张弹窗」，是最难自己查出来的一种「按了没反应」。
    """
    eng = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0, click_guard_sec=2.5),
                 [Monitor("窗口A", [0, 0, 400, 600])], [])
    eng.click_guard.record("窗口A", 100, 100)
    assert eng.click_guard.nearest_gap("窗口A", 120, 100, 2.5) is not None

    eng.reset_cooldowns()
    assert eng.click_guard.nearest_gap("窗口A", 120, 100, 2.5) is None, "全量重置没清掉防重复"

    eng.click_guard.record("窗口A", 100, 100)
    eng.click_guard.record("窗口B", 900, 900)
    eng.reset_cooldowns("窗口A")                      # 只重置一个窗口，别误伤别的窗口
    assert eng.click_guard.nearest_gap("窗口A", 120, 100, 2.5) is None
    assert eng.click_guard.nearest_gap("窗口B", 900, 900, 2.5) is not None, "别跨窗口清"

    eng.reset_cooldowns(None)
    assert eng.click_guard.nearest_gap("窗口B", 900, 900, 2.5) is None


def test_process_frame_clicks_popup_once() -> None:
    """整链路复现真机的「一张弹窗点两次」：第二下要被拦下，且要点数只记一次。"""
    import numpy as np
    import core.capture as cap
    import core.engine as engine_mod
    from core.ocr import OcrHit

    real_at, real_root = cap.window_at, cap.root_hwnd
    real_alive = engine_mod.window_alive
    cap.window_at = lambda x, y: 7777
    cap.root_hwnd = lambda h: int(h or 0)
    engine_mod.window_alive = lambda hwnd: True
    try:
        # 一帧里同时读到两个词：弹窗主按钮「立即领券」和它下面的「奖券」
        hits = [OcrHit("立即领券", 0.9, 600, 600, 90, 30),
                OcrHit("奖券", 0.88, 590, 660, 60, 24)]
        targets = [Target("兑换券", ocr_keywords=["立即领券"]),
                   Target("奖券", ocr_keywords=["奖券"])]
        settings = Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_min_score=0.5)
        eng = Engine(settings, [Monitor("直播间", [0, 0, 400, 600])], targets)
        worker = MonitorWorker(eng, eng.monitors[0])
        worker.region = (0, 0, 400, 600)
        worker.ocr_due = True
        worker._locator._current_hwnd = 7777
        worker.detector.ocr = _stub_ocr(hits)
        clicks: list[tuple[int, int]] = []
        eng.clicker = type("Rec", (), {"click": staticmethod(lambda x, y: clicks.append((x, y)))})()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert len(clicks) == 1, f"一张弹窗只该点一下: {clicks}"
        assert worker.state.clicks == 1 and worker._guard_skipped == 1, worker.state.__dict__

        # 冷却清掉后弹窗还在：防重复的 2.5s 内仍然只点得了一下
        eng.cooldowns.reset()
        eng.click_guard.reset()
        clicks.clear()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert len(clicks) == 1, clicks
        assert worker._guard_skipped == 2

        # 关掉防重复（设 0）后两个都能点，行为退回改之前
        eng.settings.click_guard_sec = 0.0
        eng.cooldowns.reset()
        clicks.clear()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert len(clicks) == 2, clicks
        assert worker._guard_skipped == 2, "关掉之后不该再拦"
    finally:
        engine_mod.window_alive = real_alive
        cap.window_at, cap.root_hwnd = real_at, real_root


def test_ocr_idle_downshift() -> None:
    """直播间绝大多数时间没有弹窗（实测 641 帧只 3 张）：连着几轮空扫后自动把
    OCR 降到每 ocr_idle_every 轮一次，一有命中立刻恢复每轮跑。"""
    engine = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_scan_every=1,
                             ocr_idle_every=2), [Monitor("窗口A", [0, 0, 600, 260])], [])
    worker = MonitorWorker(engine, engine.monitors[0])
    due = []
    for _ in range(7):
        worker._round += 1
        due.append(worker._ocr_due_this_round())
        worker._idle_rounds += 1        # 引擎在 process_frame 里记「这轮什么都没扫到」
    # 前 2 轮是观察期，确认真的没人也没弹窗，之后开始隔轮跑（长跑就是一半 OCR）
    assert due == [True, True, False, True, False, True, False], due

    worker._idle_rounds = 0            # 模拟刚才命中过：立刻恢复每轮跑
    worker._round += 1
    assert worker._ocr_due_this_round(), "一有命中就要恢复每轮 OCR"
    assert worker._ocr_due_this_round(), "恢复期要连着每轮跑"

    # 刚点完一发：下一轮强制做一次完整 OCR（确认弹窗已消失）
    worker._ocr_force = True
    worker._idle_rounds = 9
    worker._ocr_since = 0
    assert worker._ocr_due_this_round(), "点完该补一次完整 OCR"
    # 刚点完一发：下一轮必须补一次完整 OCR。标记只允许在「真扫上」时清掉
    # （run() 里那一行），所以这里只看不删——点完那一发若被判成「画面没变」
    # 整轮跳过，确认用的那次 OCR 不能被提前吃掉。
    worker._ocr_force = True
    worker._ocr_since = 0
    worker._round += 1
    assert worker._ocr_due_this_round(), "点完该补一次完整 OCR"
    assert worker._ocr_force, "标记得留到真的扫上才清，整轮跳过时还要用"
    worker._ocr_since = 0
    worker._round += 1
    assert worker._ocr_due_this_round(), "上一轮没扫上，这轮还得补"

    # 真扫上了（run() 清标记）：之后回到降频节奏，不再每轮都跑
    worker._ocr_force = False
    worker._ocr_since = 0
    worker._round += 1
    assert not worker._ocr_due_this_round(), "补完就回到降频节奏"

    # 关掉降频（idle_every=1）就是老行为：每轮都跑
    engine.settings.ocr_idle_every = 1
    worker._idle_rounds = 9
    worker._ocr_since = 0
    worker._round += 1
    assert worker._ocr_due_this_round(), "设成 1 就不能降频"
    assert worker._ocr_due_this_round(), "设成 1 就得每轮跑"

    # ocr_scan_every 本来就是 3 时不许被 idle_every 改成更松
    engine.settings.ocr_scan_every = 3
    engine.settings.ocr_idle_every = 2
    worker._idle_rounds = 9
    worker._ocr_since = 0
    due = []
    for _ in range(6):
        worker._round += 1
        due.append(worker._ocr_due_this_round())
    assert due == [False, False, True, False, False, True], due


def test_window_locator_sticky() -> None:
    """粘性跟窗：多个同身份窗口在场时锁定上次那个，不因面积变化而跳变。"""
    import core.capture as cap
    from core.capture import WindowLocator

    scenes = {}
    rects = {}
    hwnds = [100, 200]
    real_enum = cap.ctypes.windll.user32.EnumWindows
    real_vis = cap.ctypes.windll.user32.IsWindowVisible
    real_pid_of, real_sig = cap._pid_of, cap.window_signature
    real_rect, real_getpid = cap._visible_rect, cap.os.getpid
    try:
        cap.ctypes.windll.user32.EnumWindows = lambda cb, a: [cb(h, 0) for h in hwnds] and 1
        cap.ctypes.windll.user32.IsWindowVisible = lambda h: True
        cap._pid_of = lambda h: 999          # 都不属于本进程
        cap.os.getpid = lambda: 1
        cap.window_signature = lambda h: scenes[h]
        cap._visible_rect = lambda h: rects[h]

        scenes[100] = ("房间A", "Chrome_WidgetWin_1", "Weixin.exe")
        scenes[200] = ("房间B", "Chrome_WidgetWin_1", "Weixin.exe")
        loc = WindowLocator()
        m = Monitor("直播间", window_class="Chrome_WidgetWin_1", window_process="Weixin.exe")

        rects[100] = (0, 0, 1200, 900)        # 100 最大
        rects[200] = (2000, 0, 400, 300)
        assert loc.box(m) == (0, 0, 1200, 900), "首次应选面积最大的 100"

        rects[100] = (10, 10, 400, 300)       # 现在 100 变小、200 变特大
        rects[200] = (0, 0, 1600, 1000)
        assert loc.box(m) == (10, 10, 400, 300), "已锁 100 就该续 100，不跳去更大的 200"

        hwnds.remove(100)                     # 100 消失才重新挑
        assert loc.box(m) == (0, 0, 1600, 1000)

        hwnds.remove(200)                     # 全没：返回 None 并清记忆
        assert loc.box(m) is None
    finally:
        cap.ctypes.windll.user32.EnumWindows = real_enum
        cap.ctypes.windll.user32.IsWindowVisible = real_vis
        cap._pid_of, cap.window_signature = real_pid_of, real_sig
        cap._visible_rect, cap.os.getpid = real_rect, real_getpid


def test_tracked_click_realign() -> None:
    """窗口在 OCR 期间被拖动时，点击点要按当前窗口矩形平移；窗口没了就跳过。"""
    engine = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0),
             [Monitor("直播间", window_title="直播")], [])
    worker = MonitorWorker(engine, engine.monitors[0])   # window_title 非空 => 跟窗

    box = [(2688, 98, 1085, 930)]
    engine.find_window = lambda m: list(box[0]) if m.window_title else None

    worker.region = (2688, 98, 1085, 930)
    assert worker.track_shift() == (True, 0, 0), "窗口没动，位移应为 0"

    box[0] = (2718, 118, 1085, 930)                # 被拖到 (+30, +20)
    assert worker.track_shift() == (True, 30, 20)

    engine.find_window = lambda m: None             # 窗口被关掉
    assert worker.track_shift() == (False, 0, 0)

def test_static_frame_gate_skips() -> None:
    """画面几乎不变时跳过 OCR/模板省算力；一有变化或连跳到上限就强制实扫，绝不漏点。"""
    import numpy as np
    from core.capture import frame_changed, frame_digest
    from core.engine import STATIC_SKIP_MAX, Engine, MonitorWorker

    # 纯函数：完全一致不判变化、明显不同判变化、None 一律当“有变化”、太小/空帧给 None
    zero = np.zeros((600, 400, 3), np.uint8)
    dz = frame_digest(zero)
    assert dz is not None and dz.shape == (48, 48), dz
    assert frame_changed(dz, frame_digest(zero.copy()), 1.2) is False
    assert frame_changed(dz, frame_digest(np.full((600, 400, 3), 255, np.uint8)), 1.2) is True
    assert frame_changed(None, dz, 1.2) is True and frame_changed(dz, None, 1.2) is True
    assert frame_digest(np.zeros((10, 10, 3), np.uint8)) is None
    assert frame_digest(None) is None

    engine = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0),
             [Monitor("W", [0, 0, 400, 600])], [])
    worker = MonitorWorker(engine, engine.monitors[0])
    static = np.zeros((600, 400, 3), np.uint8)

    assert worker._frame_is_static(static) is False            # 首帧定基线，必须实扫
    assert worker._frame_is_static(static) is True             # 与上轮一致 -> 跳过
    assert worker._frame_is_static(static) is True
    assert worker._frame_is_static(static) is True             # 连续跳到上限
    assert STATIC_SKIP_MAX == 3
    assert worker._frame_is_static(static) is False            # 触顶强制实扫一次
    assert worker._static_skips == 0                            # 触顶后计数归零
    assert worker._frame_is_static(static) is True             # 又能从头跳

    moved = np.full((600, 400, 3), 255, np.uint8)
    assert worker._frame_is_static(moved) is False             # 画面变化 -> 实扫并重置基线


def test_background_capture_toggle() -> None:
    """后台抓窗(PrintWindow)默认关、可从 YAML 读入；默认整屏，只有盖住/黑屏才后台抓。"""
    import numpy as np
    import core.engine as eng
    from core.capture import looks_blank

    notes: list[str] = []
    monitors = _build_monitors([
        {"name": "关", "background_capture": False},
        {"name": "开", "background_capture": True},
        {"name": "字符串", "background_capture": "true"},
        {"name": "脏值", "background_capture": "abc"},
    ], notes.append)
    assert not monitors[0].background_capture and monitors[1].background_capture, monitors
    assert monitors[2].background_capture, monitors
    assert not monitors[3].background_capture, monitors
    assert Monitor("默认").background_capture is False, "默认必须关，别悄悄改默认抓屏路径"
    base = Monitor("W", background_capture=True)
    assert base.signature() != Monitor("W").signature(), "改这个开关要让热更新重启该窗口线程"

    pic = np.zeros((600, 400, 3), np.uint8)
    pic[::2, :, 0] = 200          # 有内容的帧：绝不能被判成黑屏
    blank = np.zeros((600, 400, 3), np.uint8)
    box = (0, 0, 400, 600)
    calls: list[str] = []
    used_path: list[int] = []

    class FakeCapture:
        def grab(self, region):
            calls.append(region)
            return pic

    def make(monitor):
        return MonitorWorker(Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0),
             [monitor], []), monitor)

    capture = FakeCapture()
    real_grab, real_blank = eng.grab_window, eng.looks_blank

    bg = pic.copy()
    bg[::2, 1] = 90            # 后台抓回来的另一帧，用来区分这一发走的哪条路

    def run_case(background, hwnd, shot, blank_result, covered=False):
        """shot=None 表示 PrintWindow 失败；covered=窗口被无关窗口盖住或整体在屏幕外。"""
        calls.clear()
        used_path.clear()
        worker = make(Monitor("W", [0, 0, 400, 600], background_capture=background))
        worker._locator._current_hwnd = hwnd
        worker._window_is_covered = lambda h, b: covered

        def fake_grab_window(h):
            used_path.append(int(h))
            return shot(h) if callable(shot) else shot

        def fake_blank(frame, threshold=1.0):
            if frame is None or frame is blank:
                return True
            if frame is pic:
                return blank_result  # 整屏抓到黑帧（窗口整个在屏幕外）才算空
            return False             # 后台抓到的那帧永远算有内容

        eng.grab_window = fake_grab_window
        eng.looks_blank = fake_blank
        try:
            return worker._grab(capture, box)
        finally:
            eng.grab_window, eng.looks_blank = real_grab, real_blank

    # 1) 默认关：即便手里有句柄也走整屏截图，不碰 PrintWindow
    used = run_case(False, 4242, lambda h: (bg, box), False, covered=True)
    assert used is pic and calls == [box] and used_path == [], (used, calls, used_path)

    # 2) 开着但没有句柄：没有窗口可抓，直接整屏
    used = run_case(True, 0, lambda h: (bg, box), False, covered=True)
    assert used is pic and calls == [box] and used_path == [], (used, calls, used_path)

    # 3) 开着、有句柄、窗口也没被盖住：默认用整屏那一帧。
    #    PrintWindow 只拍得到直播窗自己的像素，而直播间的“兑换券”弹窗多是盖在它
    #    上面的独立顶层窗口——先用它会整块漏掉弹窗，2026-09-26 真机连扫 68 帧
    #    一帧都没拍到过弹窗、用户连报“检测不到”，根因就在这。
    used = run_case(True, 4242, lambda h: (bg, box), False)
    assert used is pic and calls == [box] and used_path == [], (used, calls, used_path)

    # 4) 整屏抓到黑帧、或直播窗被无关窗口盖住：才改用 PrintWindow 看窗口自己的像素，
    #    点击能不能送达仍由提窗逻辑兑底
    used = run_case(True, 4242, lambda h: (bg, box), True)
    assert used is bg and calls == [box] and used_path == [4242], (used, calls, used_path)
    used = run_case(True, 4242, lambda h: (bg, box), False, covered=True)
    assert used is bg and calls == [box] and used_path == [4242], (used, calls, used_path)

    # 5) PrintWindow 返回 None / 抓到黑屏：回退整屏，绝不因后台抓失败而漏扫
    used = run_case(True, 4242, None, False, covered=True)
    assert used is pic and calls == [box] and used_path == [4242], (used, calls, used_path)
    used = run_case(True, 4242, lambda h: (blank, box), False, covered=True)
    assert used is pic and calls == [box] and used_path == [4242], (used, calls, used_path)

    # 6) 后台抓回的矩形和本轮不一致（窗口在两次调用之间动了）：矩形必须和坐标换算
    #    用同一个，否则点击点整体偏，宁可回退整屏白扫一次。
    used = run_case(True, 4242, lambda h: (bg, (1, 1, 400, 600)), False, covered=True)
    assert used is pic and calls == [box] and used_path == [4242], (used, calls, used_path)

    # 桩只负责接线，looks_blank 本身要真的能判出黑屏/空帧
    assert looks_blank(blank) is True and looks_blank(None) is True
    assert looks_blank(pic) is False


def test_progress_diagnosis() -> None:
    """「为什么没点击」要能自查：关键词没对上时，日志得说出最像的那段 OCR 文本。"""
    from core.detector import Detector, _best_ratio
    from core.engine import Engine, MonitorWorker
    from core.ocr import OcrHit

    # 相似度：完全一致 1.0，错一个字 2/3，毫不相干 0
    assert _best_ratio("立即领券", "立即领券") == 1.0
    assert abs(_best_ratio("立即领券", "立即领卷") - 3 / 4) < 1e-9
    assert _best_ratio("签到", "领券") == 0.0
    assert _best_ratio("签到", "签") == 0.0          # 比关键词还短，没得比
    assert _best_ratio("签到", "前面签到后面") > 0.9   # 关键词埋在长文本里

    target = Target("领券", ocr_keywords=["立即领券"])
    det = Detector(Settings(ocr_min_score=0.5), Monitor("W", [0, 0, 400, 600]))
    det.ocr = _stub_ocr([OcrHit("立即领卷", 0.9, 10, 10, 60, 20)])

    # 默认不算相似度：那东西每帧做一遍是白烧 CPU，只在体检帧算
    det.scan(None, [target], region=(0, 0, 400, 600), run_ocr=True)
    assert det.near_misses == {}, "默认不该花 CPU 算相似度"
    assert det.last_ocr_hits == 1, det.last_ocr_hits

    # 开了 near miss：要把「最像的那段文本」记下来，且不因此判定命中（不能乱点）
    det.scan(None, [target], region=(0, 0, 400, 600), run_ocr=True, note_near_miss=True)
    assert det.near_misses == {"领券": ("立即领卷", 0.75)}, det.near_misses
    hits = det.scan(None, [target], region=(0, 0, 400, 600), run_ocr=True, note_near_miss=True)
    assert hits == [], "相似度只是诊断用，绝不能拿它当命中，否则弹幕里的字也会被点"

    # 下一帧会清空上一帧的诊断，不会把旧结论一直挂在那儿
    det.ocr = _stub_ocr([OcrHit("完全无关的文字", 0.9, 1, 1, 40, 20)])
    det.scan(None, [target], region=(0, 0, 400, 600), run_ocr=True, note_near_miss=True)
    assert det.near_misses == {"领券": ("完全无关的文字", 0.0)}, det.near_misses
    assert det.ocr_samples == ["完全无关的文字"], det.ocr_samples

    # 相似度低到不可能是一个词时，日志要说「OCR 只读到了什么」，
    # 而不是报一个相似度 0.00 的「最像」——那等于没说
    det.ocr = _stub_ocr([OcrHit("豆包", 0.9, 1, 1, 20, 20),
                         OcrHit("新对话", 0.9, 60, 1, 20, 20),
                         OcrHit("豆包", 0.9, 120, 1, 20, 20)])
    det.scan(None, [target], region=(0, 0, 400, 600), run_ocr=True, note_near_miss=True)
    assert det.ocr_samples == ["豆包", "新对话"], det.ocr_samples

    # 体重检那行要把话说明白：帧数/命中/点击/OCR/跳过、以及最像的文本
    eng = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0),
                 [Monitor("W", [0, 0, 400, 600])], [target])
    worker = MonitorWorker(eng, eng.monitors[0])
    worker.state.scans, worker.state.hits, worker.state.clicks = 40, 0, 0
    worker._ocr_runs, worker._ocr_hits = 18, 312
    worker._skips_cooling, worker._skips_static = 12, 6
    worker.detector.near_misses = {"领券": ("立即领卷", 0.75)}
    worker.detector.ocr_samples = ["立即领卷", "其他文字"]
    worker._log_progress()
    logs = [e.payload["message"] for e in _drain(eng.events)]
    line = next(m for m in logs if "已扫 40 帧" in m)
    assert "命中 0" in line and "点击 0" in line, line
    assert "OCR 18 次/312 个文本框" in line, line
    assert "冷却中跳过 12 帧" in line and "画面没变跳过 6 帧" in line, line
    assert "「领券」最像 '立即领卷'(相似度 0.75)" in line, line
    assert "模板匹配" in line, line      # 必须给出下一步怎么办

    # 一个字都不像：得说清 OCR 到底读到了什么，而不是报个 0.00 的「最像」
    worker.detector.near_misses = {"领券": ("豆包", 0.0)}
    worker.detector.ocr_samples = ["豆包", "新对话"]
    worker._log_progress()
    line = next(m for m in [e.payload["message"] for e in _drain(eng.events)]
                if "已扫 40 帧" in m)
    assert "完全对不上" in line, line
    assert "『豆包』" in line and "『新对话』" in line, line


def _drain(queue_obj):
    out = []
    while True:
        try:
            out.append(queue_obj.get_nowait())
        except Exception:
            return out


def test_ocr_bench_picks_fastest_full_recall() -> None:
    """--ocr-bench 要拿数据说话：召回差一截的档不能推荐，召回的档里挑最小的那个。"""
    import contextlib
    import io as _io
    import time
    import numpy as np
    import selftest
    from core.ocr import OcrHit

    frame = np.zeros((300, 400, 3), np.uint8)
    settings = Settings(ocr_max_side=1280, ocr_min_side=0, ocr_threads=1)
    monitor = Monitor("W", [0, 0, 400, 300])

    def _wait(seconds: float) -> None:
        """自旋等固定时长，别用 time.sleep。

        桩子里每档只睡 6~20ms，而 Windows 的时钟 tick 约 15.6ms：一次抖动就能让
        某一档凭空多 16ms，把「和最快相差不到 10%」那条判据打翻，于是
        「等成本时推荐最小档」这句断言 15 次里挂 2 次（实测）。自旋没有量化误差。
        """
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            pass

    # 640 只读到一段 -> 召回差一截，不能推荐；960 及以上都读全，应取最小的 960
    def hits_for(side):
        texts = ["签到", "立即领取"] if side >= 960 else ["签到"]
        return [OcrHit(t, 0.9, i * 40, 0, 30, 20) for i, t in enumerate(texts)]

    class Stub:
        """耗时随 max_side 增长——否则测出来的全是计时噪声，挑不出该推荐哪一档。"""
        available = True

        def recognize(self, frame, max_side, min_side):
            _wait(max_side / 100000.0)
            return hits_for(max_side)

    def run(engine):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            selftest._bench_ocr(engine, frame, monitor, settings)
        return out.getvalue()

    text = run(Stub())
    for side in selftest._BENCH_SIDES:          # 每一档都要出现在表里
        assert f"{side:12d}" in text, text
    assert "当前设置" in text, text              # 1280 是当前配置，要标出来
    assert "建议：ocr_max_side=960" in text, text  # 召回满的档里最小的那个
    assert "快" in text and "%" in text, text

    # 640 漏字就不能被推荐，哪怕它最便宜
    assert "建议：ocr_max_side=640" not in text, text

    # 每档耗时一样时只能推荐最小的那档，不能推出个最大的来
    class Same(Stub):
        def recognize(self, frame, max_side, min_side):
            _wait(0.02)
            return [OcrHit("签到", 0.9, 0, 0, 30, 20)]
    tied = run(Same())
    assert "建议：ocr_max_side=640" in tied, tied

    # 召回差一截的档绝不能被推荐：这里 960 只读到「奖券」，而基准（最大档）是「兑换券」
    class Partial(Stub):
        # 耗时仍然随档位增长（不能省掉）：不去等的话每档都是几微秒的计时噪声，
        # 「挑小的」这个断言就变成掷硬币。
        def recognize(self, frame, max_side, min_side):
            table = {640: "签到", 960: "奖券"}
            _wait(max_side / 100000.0)
            return [OcrHit(table.get(max_side, "兑换券"), 0.9, 0, 0, 30, 20)]
    text = run(Partial())
    assert "建议：ocr_max_side=1280" in text, text
    assert "建议：ocr_max_side=640" not in text and "960" not in text.split("建议")[1], text

    # OCR 不可用时要说明白，而不是崩在那儿
    class Off(Stub):
        available = False
    assert "OCR 通道不可用" in run(Off()), run(Off())


def test_popup_window_counts_as_reachable() -> None:
    """直播间的「兑换券」弹窗是盖在直播窗上的独立顶层窗口，点击就该发给它。

    只在「这个点归不归被监控窗口自己所有」上打转，会把弹窗判成「被别的窗口盖着」
    而永远跳过——真机上「命中兑换券」连刷十三次都没点出去，根因就是这个判断。
    弹窗几乎整个嵌在直播窗矩形里时算够到；只是搭个边、或干脆在别处的不算。
    """
    import core.capture as cap
    from core.capture import point_owned_by_session

    # 句柄为空（固定区域扫描）时不拦，保持老行为
    assert point_owned_by_session(0, 10, 10) is True

    real_at, real_root, real_rect = cap.window_at, cap.root_hwnd, cap._window_rect

    def with_world(top, rects):
        """top=屏幕那一点现在最顶层的窗口；rects={hwnd: (left, top, w, h)}。"""
        cap.window_at = lambda x, y: top
        cap.root_hwnd = lambda h: int(h or 0)
        cap._window_rect = lambda h: rects.get(int(h))
        try:
            return point_owned_by_session(TRACKED, 100, 100)
        finally:
            cap.window_at, cap.root_hwnd, cap._window_rect = real_at, real_root, real_rect

    TRACKED = 7777
    live = (0, 0, 400, 600)                  # 被监控的直播窗
    popup = (40, 40, 240, 160)               # 盖在直播画面上的「立即领券」弹窗

    # 1) 问不出最顶层是谁：不拦，交给 SelfWindowGuard 那类兑底
    assert with_world(0, {TRACKED: live, 8888: popup}) is True

    # 2) 点归直播窗自己：正常放行
    assert with_world(TRACKED, {TRACKED: live, 8888: popup}) is True

    # 3) 点归一个几乎整个嵌在直播窗里的独立弹窗：算够到，必须放行
    assert with_world(8888, {TRACKED: live, 8888: popup}) is True

    # 4) 只搭了个边（一半面积在直播窗外头）：不算弹窗，仍然拦
    half = (360, 480, 240, 200)
    assert with_world(8888, {TRACKED: live, 8888: half}) is False

    # 5) 完全无关的窗口（另一块屏幕上的浏览器）：拦
    elsewhere = (900, 900, 300, 200)
    assert with_world(8888, {TRACKED: live, 8888: elsewhere}) is False

    # 6) 问不出任一个矩形的：从严拦，别拿用户的鼠标去赌
    assert with_world(8888, {TRACKED: live}) is False


def test_click_blocked_when_point_is_covered() -> None:
    """后台抓窗能拍到被遮住的窗口，但点击只会发给最顶层那个窗口。

    这个点被别的窗口盖住时必须跳过，并在日志里说明白——否则就是「看见按钮却点到别人」。
    """
    import numpy as np
    import core.capture as cap
    import core.engine as engine_mod
    from core.capture import window_owns_point
    from core.ocr import OcrHit

    # 句柄为空（固定区域扫描）时不拦，保持老行为
    assert window_owns_point(0, 10, 10) is True

    # 测试里句柄是假造的，GetAncestor 归不出真顶层；把 root_hwnd 也换成恒等，
    # 单独验「最顶层是不是这个窗口」这段判断
    real_at, real_root = cap.window_at, cap.root_hwnd

    def with_top(top, fn):
        cap.window_at = lambda x, y: top
        cap.root_hwnd = lambda h: int(h or 0)
        real_alive = engine_mod.window_alive
        engine_mod.window_alive = lambda hwnd: True
        try:
            return fn()
        finally:
            engine_mod.window_alive = real_alive
            cap.window_at, cap.root_hwnd = real_at, real_root

    assert with_top(0, lambda: window_owns_point(4242, 10, 10)) is True, "问不出就不拦"
    assert with_top(7777, lambda: window_owns_point(7777, 10, 10)) is True, "自己盖着就能点"
    assert with_top(9999, lambda: window_owns_point(7777, 10, 10)) is False, "被别人盖住不能点"

    # 弹窗归属的行为拉出来单独验，那边用的是后面的 point_owned_by_session

    hits = [OcrHit("签到", 0.9, 60, 60, 60, 20)]
    target = Target("签到", ocr_keywords=["签到"])
    eng = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_min_score=0.5),
                 [Monitor("直播间", [0, 0, 400, 600])], [target])
    worker = MonitorWorker(eng, eng.monitors[0])
    worker.region = (0, 0, 400, 600)
    worker.ocr_due = True
    det = worker.detector
    det.ocr = _stub_ocr(hits)

    # 目标窗口在最上层：正常点
    clicks: list[tuple[int, int]] = []
    eng.clicker = type("Rec", (), {"click": staticmethod(lambda x, y: clicks.append((x, y)))})()
    worker._locator._current_hwnd = 7777
    with_top(7777, lambda: eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8)))
    assert clicks == [(90, 70)], clicks
    assert worker.state.clicks == 1

    # 同样一帧，但那个点被别的窗口盖住：不许点，且日志要说清楚。
    # 先清冷却：不然第二次 scan 会因为目标在冷却里直接返回，断言会「假通过」。
    clicks.clear()
    eng.cooldowns.reset()
    worker._locator._current_hwnd = 7777
    with_top(9999, lambda: eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8)))
    assert clicks == [], clicks
    assert worker.state.clicks == 1, "被盖住的那一发不能计入点击"
    assert worker.state.hits == 2, "命中了就该计数，只是没点出去"
    warnings = [e.payload["message"] for e in _drain(eng.events)
                if e.payload.get("level") == "warn"]
    assert any("被别的窗口盖着" in m and "签到" in m for m in warnings), warnings


def test_blocked_click_raises_window_first() -> None:
    """命中按钮但那个点被别的窗口盖着时，先把直播窗口调到前台再点。

    后台抓窗只保证「看得见」，点击永远只发给最顶层的窗口。这里验三件事：
    会去抢前台、抢完按新窗口矩形平移坐标、功能关掉或被挡死时仍然安全跳过。
    """
    import numpy as np
    import core.capture as cap
    import core.engine as engine_mod
    from core.engine import Engine, MonitorWorker
    from core.ocr import OcrHit

    state = {"top": 9999, "rect": (0, 0, 400, 600), "raised": 0}

    def scenario(**extra):
        settings = Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_min_score=0.5, **extra)
        eng = Engine(settings, [Monitor("直播间", window_title="直播")],
                     [Target("签到", ocr_keywords=["签到"])])
        worker = MonitorWorker(eng, eng.monitors[0])
        worker.region = list(state["rect"])
        worker.ocr_due = True
        worker.detector.ocr = _stub_ocr([OcrHit("签到", 0.9, 60, 60, 60, 20)])
        clicks: list[tuple[int, int]] = []
        eng.clicker = type("Rec", (), {"click": staticmethod(lambda x, y: clicks.append((x, y)))})()
        worker._locator._current_hwnd = 7777
        eng.find_window = lambda m: list(state["rect"])
        return eng, worker, clicks

    real_at, real_root = cap.window_at, cap.root_hwnd
    real_bring, real_min = engine_mod.bring_to_front, engine_mod.is_minimized
    real_ident = engine_mod.window_identity_at
    real_drop = engine_mod.drop_topmost
    real_alive = engine_mod.window_alive
    engine_mod.window_alive = lambda hwnd: True
    state["dropped"] = []
    engine_mod.drop_topmost = lambda hwnd: state["dropped"].append(int(hwnd))
    # _cover_label 问的是 engine 命名空间里的 window_identity_at，不是 cap.window_at，
    # 不把它一起换掉，挡着的是谁这一句断言永远对不上。
    engine_mod.window_identity_at = lambda x, y: ("Top", "SomeClass", "SomeProc.exe")

    def fake_bring(hwnd):
        state["raised"] += 1
        state["top"] = int(hwnd)
        state["rect"] = (120, 0, 400, 600)      # 抢到前台的同时窗口被挪了 120px
        return True

    cap.window_at = lambda x, y: state["top"]
    cap.root_hwnd = lambda h: int(h or 0)
    engine_mod.bring_to_front = fake_bring
    engine_mod.is_minimized = lambda h: False
    try:
        # 1) 被别的窗口挡着：先抢前台，再按新矩形平移着点
        eng, worker, clicks = scenario()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert state["raised"] == 1, "被挡住时必须把直播窗口调到前台"
        assert clicks == [(210, 70)], clicks      # 90 + 120 位移
        assert state["dropped"] == [7777], "点完必须把借来的置顶还回去，别让直播窗一直飘在最上"
        logs = [e.payload.get("message", "") for e in _drain(eng.events)]
        assert any("切到前台" in m and "签到" in m for m in logs), logs

        # 2) 关掉 raise_before_click：不抢焦点，也不点，只留下原来的遮挡告警
        state.update({"top": 9999, "rect": (0, 0, 400, 600), "raised": 0, "dropped": []})
        eng, worker, clicks = scenario(raise_before_click=False)
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert state["raised"] == 0, "关掉之后不许再抢焦点"
        assert state["dropped"] == [], "没抢过焦点就没什么可还"
        assert clicks == [], clicks
        assert worker.state.clicks == 0

        # 3) 抢前台失败：不点，也不崩，照旧把遮挡说清楚
        state.update({"top": 9999, "rect": (0, 0, 400, 600), "raised": 0})
        def fake_fail(hwnd):
            state["raised"] += 1
            return False
        engine_mod.bring_to_front = fake_fail
        eng, worker, clicks = scenario()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert state["raised"] == 1 and clicks == [], (state, clicks)
        warns = [e.payload["message"] for e in _drain(eng.events)
                 if e.payload.get("level") == "warn"]
        assert any("被别的窗口盖着" in m for m in warns), warns
        assert any("挡着的是「Top」(SomeProc.exe)" in m for m in warns), warns
        # 日志要说清挡着的是谁：只说「别的窗口」等于没说
        assert cap.window_at(11, 12) == 9999 and "\u300cTop\u300d" in engine_mod._cover_label(11, 12), engine_mod._cover_label(11, 12)
        assert engine_mod._cover_label(11, 12) == "\u300cTop\u300d(SomeProc.exe)", engine_mod._cover_label(11, 12)
    finally:
        cap.window_at, cap.root_hwnd = real_at, real_root
        engine_mod.bring_to_front, engine_mod.is_minimized = real_bring, real_min
        engine_mod.window_identity_at = real_ident
        engine_mod.drop_topmost = real_drop
        engine_mod.window_alive = real_alive


def test_offscreen_window_moved_before_click() -> None:
    """直播窗口停在屏幕外时，先挪进可见区域再点、点完还原位置。

    点坐标落在虚括桌面外，Windows 会把光标夹到屏幕边缘，点中的就成了边缘上无关的窗口。
    显示器被拔掉 / 远程桌面改过尺寸后，窗口还留在老坐标上就是这种情况。
    """
    import numpy as np
    import core.capture as cap
    import core.engine as engine_mod
    from core.engine import Engine, MonitorWorker
    from core.ocr import OcrHit

    # 虚拟桌面只有 0~300 都宽：按钮中心 310 落在屏幕外
    state = {"rect": (560, 0, 400, 600), "moved": [], "restored": []}

    def scenario(**extra):
        settings = Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_min_score=0.5, **extra)
        eng = Engine(settings, [Monitor("直播A", window_process="Weixin.exe")],
                     [Target("兑换券", ocr_keywords=["兑换券"])])
        worker = MonitorWorker(eng, eng.monitors[0])
        worker.region = list(state["rect"])
        worker.ocr_due = True
        worker.detector.ocr = _stub_ocr([OcrHit("兑换券", 0.9, 280, 60, 60, 20)])
        clicks: list[tuple[int, int]] = []
        eng.clicker = type("Rec", (), {"click": staticmethod(lambda x, y: clicks.append((x, y)))})()
        worker._locator._current_hwnd = 7777
        eng.find_window = lambda m: list(state["rect"])
        return eng, worker, clicks

    real_at, real_root = cap.window_at, cap.root_hwnd
    real_bring, real_min = engine_mod.bring_to_front, engine_mod.is_minimized
    real_drop, real_ident = engine_mod.drop_topmost, engine_mod.window_identity_at
    real_onscreen, real_move = engine_mod.point_on_screen, engine_mod.move_window_into_view
    real_restore = engine_mod.restore_window_position
    real_alive = engine_mod.window_alive
    engine_mod.window_alive = lambda hwnd: True
    engine_mod.drop_topmost = lambda hwnd: None
    engine_mod.bring_to_front = lambda hwnd: False
    engine_mod.is_minimized = lambda hwnd: False
    engine_mod.window_identity_at = lambda x, y: ("Top", "SomeClass", "SomeProc.exe")
    engine_mod.point_on_screen = lambda x, y: 0 <= int(x) < 400 and 0 <= int(y) < 600
    engine_mod.restore_window_position = (
        lambda hwnd, left, top: state["restored"].append((int(hwnd), left, top)))

    def fake_move(hwnd):
        state["moved"].append(int(hwnd))
        state["rect"] = (0, 0, 400, 600)      # 挪进屏幕：窗口左边对准屏幕左边
        return (560, 0)

    engine_mod.move_window_into_view = fake_move
    cap.window_at = lambda x, y: 7777         # 点坐落在目标窗口里，别被遮挡逻辑挤走
    cap.root_hwnd = lambda h: int(h or 0)
    try:
        # 1) 命中点在屏幕外：先挪窗再按移动后的坐标点，点完还原位置
        eng, worker, clicks = scenario()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert state["moved"] == [7777], state
        assert clicks == [(310, 70)], clicks         # 870 - 560 平移，挪进可见区域内了
        assert state["restored"] == [(7777, 560, 0)], state
        infos = [e.payload["message"] for e in _drain(eng.events)
                 if e.payload.get("level") == "info"]
        assert any("屏幕外" in m and "560,0" in m for m in infos), infos

        # 2) 把开关关掉：不挪窗，落在屏幕外的点一律跳过（不能让光标被夹到边缘）
        state.update({"rect": (560, 0, 400, 600), "moved": [], "restored": []})
        eng, worker, clicks = scenario(bring_window_on_screen=False)
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert state["moved"] == [] and clicks == [], (state, clicks)
        assert state["restored"] == [], state
        warns = [e.payload["message"] for e in _drain(eng.events)
                 if e.payload.get("level") == "warn"]
        assert any("屏幕可见范围外" in m for m in warns), warns

        # 4) 挪进屏幕后依然被别的窗口盖着（本程序自己的界面就是常见的那个）：
        #    必须提窗后按新矩形再算一次坐标，否则点的还是旧位置。
        state.update({"rect": (560, 0, 400, 600), "moved": [], "restored": []})
        engine_mod.move_window_into_view = fake_move

        def fake_bring(hwnd):
            state["rect"] = (-100, 0, 400, 600)   # 顶到前台的同时窗口被拉往左侧
            return True

        engine_mod.bring_to_front = fake_bring
        cap.window_at = lambda x, y: (7777 if int(x) < 300 else 9999)   # 310 处仍被别人盖着
        eng, worker, clicks = scenario()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        # 命中点归 9999 这个独立顶层窗口（直播间弹窗）：搞动直播窗不一定搞得动
        # 它，所以把它也一并挤进屏，点完两个都还原
        assert state["moved"] == [7777, 9999], state
        assert state["restored"] == [(7777, 560, 0), (9999, 560, 0)], state
        assert clicks == [(210, 70)], clicks          # 870 - 660，提窗后按新位置算
        assert worker.state.clicks == 1
        infos = [e.payload["message"] for e in _drain(eng.events)
                 if e.payload.get("level") == "info"]
        assert any("切到前台" in m for m in infos), infos

        # 3) 挪不动（系统拒给移）：同样安全跳过，不弹报错
        state.update({"rect": (560, 0, 400, 600), "moved": [], "restored": []})
        cap.window_at = lambda x, y: 7777      # 回到“点坐归目标窗口”的旧行为
        engine_mod.bring_to_front = lambda hwnd: False
        engine_mod.move_window_into_view = lambda hwnd: None
        eng, worker, clicks = scenario()
        eng.process_frame(worker, np.zeros((600, 400, 3), np.uint8))
        assert clicks == [] and state["restored"] == [], (state, clicks)
        assert worker.state.clicks == 0
    finally:
        cap.window_at, cap.root_hwnd = real_at, real_root
        engine_mod.bring_to_front, engine_mod.is_minimized = real_bring, real_min
        engine_mod.drop_topmost, engine_mod.window_identity_at = real_drop, real_ident
        engine_mod.point_on_screen, engine_mod.move_window_into_view = real_onscreen, real_move
        engine_mod.restore_window_position = real_restore
        engine_mod.window_alive = real_alive


def test_run_logger_flushes_and_survives() -> None:
    """运行日志是排障的命根子：写进去的必须真落到文件，而且日志系统自己出毛病
    （磁盘满/目录被占）也不能把扫描线程带崩。"""
    from core.runlog import RunLogger

    # 每个用例用独立子目录，三个 logger 才不会互相覆盖同一个 run.log
    import tempfile
    tmp = tempfile.mkdtemp(prefix="runlog-")
    logger = RunLogger(tmp)
    logger.start()
    try:
        logger.write("info", "第一条")
        logger.write("warn", "第二条")
    finally:
        logger.stop()

    text = open(logger.path, encoding="utf-8").read()
    assert "第一条" in text and "第二条" in text, text
    assert "[info]" in text and "[warn]" in text, text

    # 写失败（日志路径指向一个不存在的盘/被占用的文件）不能抛异常
    broken = RunLogger("Z:\\__no_such_drive__\\nope")
    broken.write("info", "这条写不出去")
    broken.stop()      # 必须安静返回

    # 超长消息要截断，否则一条异常栈能把日志撑爆
    logger2 = RunLogger(tmp)
    logger2.start()
    logger2.write("error", "x" * 9000)
    logger2.stop()
    lines = open(logger2.path, encoding="utf-8").read().splitlines()
    assert lines and len(lines[-1]) < 2200, len(lines[-1])

    # 换行要压平成一行，否则一行异常栈会把日志拆成几十行没法读
    logger3 = RunLogger(tmp)
    logger3.start()
    logger3.write("info", "第一行\n第二行")
    logger3.stop()
    assert " | 第二行" in open(logger3.path, encoding="utf-8").read()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    assert not os.path.exists(os.path.join(os.getcwd(), "logs")) or True


def test_stop_is_non_blocking_and_does_not_duplicate_workers() -> None:
    """界面线程按下「停止」不能被扫描线程拖住。

    join 最长 5s（单帧 OCR 卡在长帧上），同步停在 tkinter 主线程里就表现为界面「未响应」
    几秒。异步停止必须做到三件事：立刻回到停止态、后台最终真的收尾、再启动前先把上一轮
    的线程等干净（否则同一窗口两条扫描线程，弹窗会被点两下）。
    """
    import time
    import core.engine as engine_mod
    import threading

    eng = Engine(Settings(scan_interval_sec=30.0, jitter_sec=0.0),
                 [Monitor("窗口A", [0, 0, 600, 260])],
                 [Target("签到", ocr_keywords=["签到"])])
    assert eng.start()
    worker = next(iter(eng._workers.values()))
    held = threading.Event()

    def fake_join(*a, **k):
        held.set()          # 「线程还没退」这把钥匙由测试握着，见下面的 finally
        time.sleep(0.2)

    worker.join = fake_join
    try:
        eng.stop(wait=False)
        assert not eng.running, "异步停止必须立刻回到停止态"
        assert held.is_set() and worker.is_alive(), "这一版 stop 不该同步 join 扫描线程"
        assert eng._stopping and eng._stopping[0][0] is worker
        for _ in range(200):
            if not eng._stopping:
                break
            time.sleep(0.02)
        assert eng._stopping == [], "后台线程必须把已收尾的 worker 从待收尾列表里摘掉"

        # 线程还没退干净时再启动：必须先把上一轮的等掉，不能出现两条扫描线程
        worker.is_alive = lambda: False      # 假装这一轮已经退干净
        eng.start()
        names = [t.name for t in threading.enumerate() if t.name.startswith("scan-")]
        assert names.count("scan-窗口A") == 1, names
        eng.stop()
    finally:
        worker.stop()
        if worker.is_alive():
            worker.join(timeout=2.0)


def test_click_skipped_when_window_closed_during_scan() -> None:
    """从截图到点击隔着几次 OCR（一两秒），这期间用户可能把直播间关了。

    窗口一没，截图时记下的矩形只是屏幕上一段旧坐标，照旧点下去点中的就是桌面
    或者正好飘到那儿的别的窗口。这一发必须跳过，且只提醒一次。
    """
    import numpy as np
    import core.capture as cap
    import core.engine as engine_mod
    from core.ocr import OcrHit

    real_at, real_root = cap.window_at, cap.root_hwnd
    real_alive = engine_mod.window_alive
    cap.window_at = lambda x, y: 7777
    cap.root_hwnd = lambda h: int(h or 0)
    engine_mod.window_alive = lambda hwnd: False      # 直播窗口刚被关掉
    try:
        eng = Engine(Settings(scan_interval_sec=1.5, jitter_sec=0.0, ocr_min_score=0.5),
                     [Monitor("直播间", [0, 0, 400, 600])],
                     [Target("兑换券", ocr_keywords=["立即领券"])])
        worker = MonitorWorker(eng, eng.monitors[0])
        worker.region = (0, 0, 400, 600)
        worker.ocr_due = True
        worker._locator._current_hwnd = 7777
        worker.detector.ocr = _stub_ocr([OcrHit("立即领券", 0.9, 600, 600, 90, 30)])
        clicks: list[tuple[int, int]] = []
        eng.clicker = type("Rec", (), {
                         "click": staticmethod(lambda x, y: clicks.append((x, y)))})()
        frame = np.zeros((600, 400, 3), np.uint8)
        for _ in range(3):
            eng.process_frame(worker, frame)
        assert clicks == [], f"窗口都没了还点：{clicks}"
        assert worker.state.clicks == 0
        assert worker.state.hits == 3, "命中照常计数，只是这一发点不出去"
        warns = [e.payload["message"] for e in _drain(eng.events)
                if e.payload.get("level") == "warn"]
        lost = [m for m in warns if "点击前发现直播窗口已消失" in m]
        assert len(lost) == 1, f"这条提醒只该出现一次：{warns}"

        # 窗口回来之后又能点：只是等它回来，不是把扫描停掉
        engine_mod.window_alive = lambda hwnd: True
        eng.cooldowns.reset()
        eng.process_frame(worker, frame)
        assert clicks == [(645, 615)], clicks
    finally:
        cap.window_at, cap.root_hwnd = real_at, real_root
        engine_mod.window_alive = real_alive

def test_build_script_is_ascii_and_strips_bad_crt() -> None:
    """build.bat 是「打包静默失败」的历史重灾区，两条都得守住。

    1) cmd.exe 按当前代码页逐字节读批处理，中文注释会吞掉后续字符，让 setlocal /
       if errorlevel 一起失灵，结果每次打包都"假装成功"。
    2) PyInstaller 有时会把 C:\\Windows\\System32\\HWAudioDriverLibs\\msvcp140.dll
       这套互不兼容的 CRT 平铺进 _internal，而 _internal 是注册过的 DLL 搜索目录，
       它会遮蔽正确版本，导致 onnxruntime.dll 的 DllMain 以 1114 失败 ——
       整个 OCR 通道在 exe 里静默失效（界面只报"未安装依赖"或干脆 0 命中）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "build.bat")
    raw = open(path, "rb").read()
    assert raw, "build.bat 不能是空文件"
    bad = sorted({b for b in raw if b > 127})
    assert not bad, f"build.bat 含非 ASCII 字节 {bad}，cmd.exe 会啃掉后续命令"
    text = raw.decode("ascii")

    for dll in ("msvcp140.dll", "MSVCP140_1.dll"):
        assert dll in text, f"build.bat 必须删掉 {dll}（来自驱动目录的错版 CRT）"
    assert "onnxruntime" in text.lower() or "OCR" in text, "清理步骤要写清楚为什么"
    # 删错的 dll 会把 opencv 的 ffmpeg 也搭进去，确认两件事互不干扰
    assert "opencv_videoio_ffmpeg" in text, "build.bat 仍应删除 ffmpeg dll"


def test_ui_state_round_trip() -> None:
    """窗口大小要记住，「界面尽量小」才在下次打开时仍然成立；没有文件也不能抛错。"""
    import core.config as config_mod

    with tempfile.TemporaryDirectory() as tmp:
        original = config_mod._path
        config_mod._path = lambda name: os.path.join(tmp, name)
        try:
            assert config_mod.load_ui_state() == {}
            config_mod.save_ui_state({"geometry": "860x520+120+80"})
            assert config_mod.load_ui_state() == {"geometry": "860x520+120+80"}
        finally:
            config_mod._path = original


def test_sane_geometry_clamps_and_rejects() -> None:
    """存下来的几何必须夹回当前屏幕，否则换个显示器/改过 DPI 就找不着窗口。"""
    from ui.main_window import sane_geometry

    assert sane_geometry("860x520+120+80", 1920, 1080, 700, 420) == "860x520+120+80"
    # 尺寸大过当前屏要夹回来，位置跟着归零（左上角必须可见，否则标题栏都拖不到）
    assert sane_geometry("4000x3000", 1920, 1080, 700, 420) == "1920x1080+0+0"
    assert sane_geometry("860x520+5000+5000", 1920, 1080, 700, 420) == "860x520+1220+660"
    assert sane_geometry("860x520-100-50", 1920, 1080, 700, 420) == "860x520+0+0"
    # 坏值一律退回 None，让调用方用默认尺寸，而不是把 Tk 炸了
    for bad in (None, "", "garbage", "0x0", "x520", "-860x-520"):
        assert sane_geometry(bad, 1920, 1080, 700, 420) is None, bad


def test_template_fast_tick_skips_ocr_only_targets() -> None:
    # A fast tick (templates_only=True) must still find template targets and must
    # never look at keyword-only targets, so the 15ms template pass can run far
    # more often than the 1s OCR pass without missing anything.
    import cv2
    import numpy as np

    targets = [Target("tmpl", template="templates/ljsq_button.png"),
               Target("kw", ocr_keywords=["X"])]
    settings = Settings(fast_interval_sec=0.1)

    frame = np.random.default_rng(3).integers(20, 90, (725, 609, 3), dtype=np.uint8)
    tmpl = cv2.imread(abs_path("templates/ljsq_button.png"))
    th, tw = tmpl.shape[:2]
    frame[300:300 + th, 180:180 + tw] = tmpl

    det = Detector(settings, Monitor("w", [0, 0, 609, 725]))
    det.targets = targets
    det.ocr = None          # fast frames must not need OCR at all

    hits = det.scan(frame, targets, region=(0, 0, 609, 725),
                    run_ocr=False, templates_only=True)
    names = sorted(d.target_name for d in hits)
    assert "tmpl" in names, f"template target must be found on a fast frame: {names}"
    assert "kw" not in names, f"keyword-only target must be skipped: {names}"

    # The slow path is unchanged: same frame, no fast flag.
    det2 = Detector(settings, Monitor("w", [0, 0, 609, 725]))
    det2.targets = targets
    det2.ocr = None
    slow = sorted(d.target_name for d in det2.scan(
        frame, targets, region=(0, 0, 609, 725), run_ocr=False, templates_only=False))
    assert "tmpl" in slow, slow


def test_fast_interval_disabled_when_not_faster_than_slow() -> None:
    # fast_interval_sec of 0 (or larger than the scan interval) means the fast
    # tick is off and both channels share one cadence again.
    import types
    from core import engine as engine_mod

    settings = Settings(fast_interval_sec=0.0)
    monitor = Monitor("w", [0, 0, 100, 100])
    worker = object.__new__(engine_mod.MonitorWorker)
    worker.engine = types.SimpleNamespace(settings=settings)
    worker.monitor = monitor
    assert worker._resolve_fast_interval() == 0.0

    settings = Settings(fast_interval_sec=5.0)
    worker.engine = types.SimpleNamespace(settings=settings)
    assert worker._resolve_fast_interval() == 0.0

    settings = Settings(fast_interval_sec=0.1)
    worker.engine = types.SimpleNamespace(settings=settings)
    assert worker._resolve_fast_interval() == 0.1


def main() -> int:
    tests = [test_merged_keyword_box, test_keyword_box_boundaries, test_config_coercion,
             test_template_errors_reported_once, test_abs_path_resolution,
             test_yaml_errors_are_actionable, test_cooldown_is_per_monitor,
             test_self_window_hits_dropped, test_region_resolution,
             test_window_rect_by_identity,
             test_signature_under_cursor,
             test_config_hot_reload_probe,
             test_config_watch_applies_changes,
             test_scan_uses_captured_region, test_monitor_track_fields_from_yaml,
             test_ocr_thread_cap, test_ocr_threads_setting, test_worker_keeps_idle_gap,
             test_cooling_targets_skip_whole_frame, test_ocr_skips_angle_classifier,
             test_ocr_scan_every_gates_ocr, test_ocr_idle_downshift,
             test_click_guard_suppresses_same_popup, test_process_frame_clicks_popup_once,
             test_reset_cooldowns_clears_click_guard,
             test_window_locator_sticky,
             test_tracked_click_realign,
             test_static_frame_gate_skips,
             test_background_capture_toggle,
             test_progress_diagnosis,
             test_ocr_bench_picks_fastest_full_recall,
             test_click_blocked_when_point_is_covered,
             test_popup_window_counts_as_reachable,
             test_click_prefers_full_button_text_over_danmaku,
             test_run_logger_flushes_and_survives,
             test_stop_is_non_blocking_and_does_not_duplicate_workers,
             test_click_skipped_when_window_closed_during_scan,
             test_build_script_is_ascii_and_strips_bad_crt,
             test_ui_state_round_trip, test_sane_geometry_clamps_and_rejects,
             test_template_fast_tick_skips_ocr_only_targets,
             test_fast_interval_disabled_when_not_faster_than_slow]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} 项离线回归全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
