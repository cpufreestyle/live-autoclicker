"""命令行自检：不开 GUI 也能验证识别效果，用于调阈值和挑关键词。

用法：
  python selftest.py                        # 逐个监控窗口截图，打印 OCR 文字与目标命中
  python selftest.py --image shot.png       # 用本地截图检测（忽略窗口区域，按整幅图）
  python selftest.py --ocr-only             # 只打印识别到的全部文字
  python selftest.py --ocr-bench            # 对每个窗口扫几档 ocr_max_side，报耗时与文字召回
  python selftest.py --window 窗口A          # 只检测指定名称的监控窗口
  python selftest.py --windows              # 列出屏幕上的窗口标题，用来挑「窗口标题包含」
  python selftest.py --pick            # 鼠标移到直播窗口上停好别动，倒计时结束自动锁定，读出类名/进程/标题
  python selftest.py --pick --write    # 同上，并把身份写回 monitors.yaml 的「直播间」条目（倒计时期间别点鼠标）
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
import time

import cv2

from core.capture import (ScreenCapture, cursor_pos, list_windows,
                          set_process_dpi_awareness, window_at, window_process,
                          window_signature, _visible_rect)
from core.config import (BASE_DIR, Monitor, load_monitors, load_settings,
                        load_targets)
from core.detector import CooldownTracker, Detector, resolve_region
from core.ocr import OcrEngine, compact_text
from core.runlog import RunLogger

# Windows 控制台默认 GBK，中文帮助信息会乱码，尽早切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="直播按钮识别自检")
    parser.add_argument("--image", help="用截图文件代替实时截屏")
    parser.add_argument("--ocr-only", action="store_true", help="只打印 OCR 结果")
    parser.add_argument("--window", help="只检测该名称的监控窗口")
    parser.add_argument("--list", action="store_true", help="列出监控窗口后退出")
    parser.add_argument("--windows", action="store_true",
                        help="列出屏幕上的可见窗口标题与矩形后退出")
    parser.add_argument("--pick", action="store_true",
                        help="鼠标停到直播窗口上等倒计时，自动锁定并读出类名/进程/标题，给出现成配置")
    parser.add_argument("--write", action="store_true",
                        help="配合 --pick：把读到的窗口身份写回 monitors.yaml 的「直播间」条目")
    parser.add_argument("--ocr-bench", action="store_true",
                        help="扫几档 ocr_max_side，报告每档耗时与文字召回，用来选速度/准确率的折中点")
    parser.add_argument("--ocr-provider", choices=("auto", "cpu", "directml"), default=None,
                        help="临时指定 OCR 后端对比：auto/cpu/directml（默认按 settings.yaml 的 ocr_provider）")
    args = parser.parse_args()

    set_process_dpi_awareness()
    # 自检的 OCR / 命中结果也落盘一份，方便和程序运行的日志对照
    logger = RunLogger(BASE_DIR)
    logger.start()
    try:
        return _dispatch(args, logger)
    finally:
        logger.stop()


def _dispatch(args, logger) -> int:
    if args.windows:
        # 类名和进程名也打出来：直播窗口的标题常是房间号，换个房间就变，类名/进程名才稳定
        for title, klass, proc, (l, t, w, h) in list_windows(with_identity=True):
            print(f"{w}x{h} @ {l},{t}  类名={klass or '-'}  进程={proc or '-'}  {title}")
        return 0

    if args.pick:
        return _pick_and_report(write=args.write)

    settings = load_settings()
    targets = load_targets()
    monitors = load_monitors()
    if args.ocr_provider:
        settings.ocr_provider = args.ocr_provider

    if args.list:
        for m in monitors:
            region = _describe(m)
            print(f"{'[on ]' if m.enabled else '[off]'} {m.name}: {region} "
                  f"间隔={m.scan_interval_sec or '全局'} 适用目标="
                  f"{', '.join(t.name for t in targets if t.applies_to(m.name)) or '无'}")
        return 0

    if args.window:
        monitors = [m for m in monitors if m.name == args.window]
        if not monitors:
            print(f"找不到监控窗口: {args.window}", file=sys.stderr)
            return 1

    ocr = OcrEngine(threads=settings.ocr_threads, provider=settings.ocr_provider)
    frame = None
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            print(f"无法读取图片: {args.image}", file=sys.stderr)
            return 1
        monitors = [Monitor(name="图片", region=None)]

    capture = None if frame is not None else ScreenCapture()
    exit_code = 0
    try:
        for monitor in monitors:
            grabbed = (frame, None) if frame is not None else _grab(capture, monitor)
            if grabbed is None:
                exit_code = 2
                continue
            local, region = grabbed
            print(f"\n=== {monitor.name} · {_describe(monitor)}"
                  f" → 实际截到 {local.shape[1]}x{local.shape[0]} ===")
            if args.ocr_bench:
                # 自己会跑完整 sweep，别先白跑一次默认参数的识别
                _bench_ocr(ocr, local, monitor, settings)
                continue
            if ocr.available:
                started = time.perf_counter()
                try:
                    hits = ocr.recognize(local, max_side=monitor.max_side(settings),
                                         min_side=int(settings.ocr_min_side))
                except Exception as exc:
                    print(f"OCR 失败: {exc}（把 ocr_max_side 调小，如 1280）", file=sys.stderr)
                    logger.write("error", f"[{monitor.name}] selftest OCR 失败: {exc}")
                    exit_code = 2
                    continue
                print(f"OCR 文本 {len(hits)} 条，耗时 {time.perf_counter() - started:.2f}s"
                      f"（后端={_backend_name(ocr)}）")
                logger.write("info", f"[{monitor.name}] selftest OCR {len(hits)} 条文本，"
                                     f"后端={_backend_name(ocr)}")
                if args.ocr_only:
                    for hit in hits:
                        print(f"  ({hit.x},{hit.y} {hit.w}x{hit.h}) {hit.score:.2f} {hit.text}")
            else:
                print("未安装 rapidocr-onnxruntime，OCR 通道不可用。")
            if args.ocr_only:
                continue
            _report(monitor, local, settings, targets, ocr, region, logger)
    finally:
        if capture is not None:
            capture.close()
    return exit_code


def _describe(m: Monitor) -> str:
    if m.tracked:
        crops = [f"底部裁 {m.crop_bottom}px" if m.crop_bottom else "",
                 f"左侧裁 {m.crop_left}px" if getattr(m, 'crop_left', 0) else "",
                 f"右侧裁 {m.crop_right}px" if getattr(m, 'crop_right', 0) else ""]
        crop = "，".join(c for c in crops if c)
        return f"跟随标题「{m.window_title}」{crop}"
    return "全屏" if not m.region else f"{m.region[2]}x{m.region[3]} @ {m.region[0]},{m.region[1]}"


def _grab(capture, monitor: Monitor):
    """按配置截一帧，返回 (帧, 实际矩形)。矩形必须一并交给检测器：按标题跟随时
    每轮都在变，再解析一次会让帧内坐标和绝对坐标对不上。"""
    resolved = resolve_region(monitor)
    if not resolved.ok:
        print(f"[{monitor.name}] {resolved.error}", file=sys.stderr)
        return None
    try:
        return capture.grab(resolved.box), resolved.box
    except Exception as exc:
        print(f"[{monitor.name}] 截图失败: {exc}", file=sys.stderr)
        return None


def _report(monitor, frame, settings, targets, ocr, region=None, logger=None) -> None:
    detector = Detector(settings, monitor, ocr, CooldownTracker())
    detections = detector.scan(frame, targets, region=region)
    print(f"命中目标 {len(detections)} 个:")
    for det in detections:
        print(f"  {det.target_name} [{det.source}] conf={det.confidence:.2f} "
              f"→ 绝对坐标 ({det.abs_x}, {det.abs_y})")
        if logger is not None:
            logger.write("info", f"[{monitor.name}] selftest 命中「{det.target_name}」"
                                 f"({det.source}, {det.confidence:.2f}) → ({det.abs_x}, {det.abs_y})")
        if logger is not None:
            logger.write("info", f"[{monitor.name}] selftest 命中「{det.target_name}」"
                                 f"({det.source}, {det.confidence:.2f}) → ({det.abs_x}, {det.abs_y})")
    for err in detector.pop_errors():
        print(f"  {err}", file=sys.stderr)
    if not detections and logger is not None:
        logger.write("warn", f"[{monitor.name}] selftest 未命中任何目标")
    if not detections and logger is not None:
        logger.write("warn", f"[{monitor.name}] selftest 未命中任何目标")
    if not detections:
        applicable = [t.name for t in targets if t.applies_to(monitor.name)]
        print("  无。可尝试调低 match_threshold，或用 --ocr-only 看屏幕文字后补充关键词。"
              + (f"（本窗口适用目标: {', '.join(applicable)}）" if applicable else "（本窗口没有适用目标）"))


# ocr_max_side 的取舍：太小小字读不出来，太大每帧都是白花时间。
# OCR 占整个扫描循环 94% 以上的时间（实测见交接.md），所以这个参数值一档的差价很大。
_BENCH_SIDES = [640, 960, 1280, 1600, 1920]
# 超过这个宽度的帧不再单独测「不缩放」那一档：4K/5K 屏跑一次要几十秒，值不回票价。
NATIVE_SIDE_LIMIT = 2560


def _bench_sides(frame) -> list[int]:
    """要扫的档位：固定几档 + 原尺寸那一档。

    原尺寸档是关键：直播窗口常常比 1920 宽（实测微信直播窗 1836x1069，已经贴着 1920），
    所有固定档都在缩图，召回率全被压着，量出来的「最好」并不是真的最好。
    """
    sides = list(_BENCH_SIDES)
    longest = max(int(frame.shape[0]), int(frame.shape[1]))
    if longest > sides[-1] and longest <= NATIVE_SIDE_LIMIT:
        sides.append(longest)
    return sides


def _backend_name(ocr) -> str:
    """实际跑在 GPU 还是 CPU。建会话之前问是 CPU（还没加载模型），用 getattr 兜住替身。"""
    return getattr(ocr, "gpu_note", "") or "CPU"


def _bench_ocr(ocr: OcrEngine, frame, monitor: Monitor, settings) -> None:
    """对同一帧扫几档 ocr_max_side，报告每档耗时、文本框数、文字召回。

    基线取最大那一档的识别结果：分辨率最高、最可能把字读全，就当作「这块屏幕上的文字」。
    不拿各档并集当基线——不同档位把文本行切开的位置不一样，并集标准太严，
    每一档都会被判成「漏字」，反而挑不出该用哪一档。
    """
    if not ocr.available:
        print("未安装 rapidocr-onnxruntime，OCR 通道不可用。")
        return
    min_side = int(settings.ocr_min_side)
    runs = 3

    def once(max_side):
        started = time.perf_counter()
        for _ in range(runs):
            hits = ocr.recognize(frame, max_side=max_side, min_side=min_side)
        return (time.perf_counter() - started) / runs * 1000, hits

    measured = [(side, once(side)) for side in _bench_sides(frame)]
    best_side, (best_cost, best_hits) = max(measured, key=lambda x: x[0])
    texts_of = lambda hits: [compact_text(h.text) for h in hits if compact_text(h.text)]
    reference = texts_of(best_hits)

    def recall(hits):
        if not reference:
            return 1.0
        got = texts_of(hits)
        kept = sum(1 for t in got
                   if difflib.get_close_matches(t, reference, n=1, cutoff=0.75))
        return kept / len(reference)

    rows = [(side, cost, hits, recall(hits)) for side, (cost, hits) in measured]
    best_recall = max(r for _, _, _, r in rows)

    print(f"\n=== {monitor.name} · {_describe(monitor)}"
          f" → {frame.shape[1]}x{frame.shape[0]}（ocr_min_side={min_side}，"
          f"后端={_backend_name(ocr)}）===")
    print(f"{'ocr_max_side':>12s} {'ms/帧':>8s} {'文本框':>7s} {'文字召回':>9s}")
    for side, cost, hits, ratio in rows:
        mark = "  <- 当前设置" if side == monitor.max_side(settings) else ""
        print(f"{side:12d} {cost:8.0f} {len(hits):7d} {ratio * 100:8.0f}%{mark}")

    # 召回和最好那一档相差不超过 2 个百分点算「一样全」：小档就是会把两行并成一行，
    # 这种差别不该把它一槽否掉。
    good = [(side, cost) for side, cost, _, ratio in rows
            if cost > 0 and ratio >= best_recall - 0.02]
    cheapest = min(cost for _, cost in good)
    # 与最快那档相差不到 10% 算「一样快」，这时取最小的：小档不给机器添负担，
    # 真遇到小字再往上调也容易；计时总有抖动，卡着小数点后几位挑没意义。
    near = [side for side, cost in good if cost <= cheapest * 1.10]
    side = min(near)
    gain = (best_cost - cheapest) / best_cost * 100
    if side == best_side:
        print(f"建议：ocr_max_side={side} 就是召回最高的一档，没有更便宜的替代"
              f"（最小可用的那档和它耗时 {cheapest / best_cost * 100:.0f}%，不值得为省这点时间漏字）")
    else:
        print(f"建议：ocr_max_side={side} 时召回和最高那档差不多，比用 {best_side} 快 {gain:.0f}%"
              f"（小字按钮多的窗口，改完先实点一次确认）")

_CR = chr(13)
_LF = chr(10)
_CRLF = _CR + _LF


# 常见终端 / 控制台宿主进程名：--pick 倒计时锁定时用它识别“鼠标还停在终端上”的误操作，
# 免得把终端的窗口身份当成直播窗口写进配置、回头点错地方。
_TERMINAL_PROCS = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "windowsterminal.exe", "windowsterminalpreview.exe",
    "mintty.exe", "conemu.exe", "conemu64.exe",
}


def _read_cursor_window():
    """读一次光标下的窗口：返回 ((标题, 类名, 进程), (left, top, width, height))。

    光标停在桌面 / 任务栏、终端窗口、或本程序自己的进程上时返回 None——这些都不是要跟踪的
    直播窗口，写进配置反而会点错东西。进程名读不到(空)时不猜，交给后面 klass/proc 判空。"""
    point = cursor_pos()
    if point is None:
        return None
    hwnd = window_at(point[0], point[1])
    if not hwnd:
        return None
    proc = (window_process(hwnd) or "").lower()
    if proc in _TERMINAL_PROCS or proc == os.path.basename(sys.executable).lower():
        return None
    box = _visible_rect(hwnd)
    if box is None:
        return None
    return window_signature(hwnd), box


def _lock_live_window(seconds: int = 5):
    """倒计时期间持续采样光标下的窗口，返回最后一个有效（非终端 / 非自身）的窗口采样。

    整段倒计时都停在终端/桌面等无效位置则返回 None。这样只需把鼠标悬停到直播窗口上等
    倒计时结束即可，不必像“按回车锁定”那样把注意力带回终端，也就不会错失锁定窗口。"""
    locked = None
    for remaining in range(max(1, seconds), 0, -1):
        print("\r请在 %d 秒内把鼠标移到直播窗口画面上，停好别动、别点 … %d "
              % (seconds, remaining), end="", flush=True)
        time.sleep(1.0)
        sample = _read_cursor_window()
        if sample is not None:
            locked = sample
    print()
    return locked


def _pick_and_report(write: bool = False) -> int:
    """鼠标悬停到直播窗口上，倒计时结束自动锁定，读出窗口身份并给出现成配置。

    默认只打印；加 --write 就直接写回 monitors.yaml 的「直播间」条目（先备份再自检）。
    若这组类名/进程同时命中多个可见窗口，说明身份不够唯一：引擎保存后只会取面积最大的
    那个，未必是直播窗口，盲目写入反而可能误点，所以这种情况拒绝写入。"""
    result = _lock_live_window()
    if result is None:
        print("倒计时结束还没锁定到直播窗口：请确认直播窗口可见（没被最小化或完全遮挡）、"
              "也没停在终端上，倒计时期间把鼠标稳稳放到直播窗口画面上再试一次。", file=sys.stderr)
        return 1
    (title, klass, proc), (left, top, width, height) = result
    klass, proc = klass or "", proc or ""
    print()
    print("已锁定鼠标处窗口 %dx%d @ %d,%d" % (width, height, left, top))
    print("  标题 = %s" % (title or "(空)"))
    print("  类名 = %s" % (klass or "(空)"))
    print("  进程 = %s" % (proc or "(空)"))

    if not klass and not proc:
        print("这个窗口既没有类名也没有进程名，没法用身份跟踪；"
              "请在「直播间」条目里填固定 region。", file=sys.stderr)
        return 1

    matches = _count_identity_matches(klass, proc)
    print()
    print("可粘贴到 monitors.yaml「直播间」条目的窗口身份：")
    print("  window_class: '%s'" % klass)
    print("  window_process: '%s'" % proc)
    if matches > 1:
        print("  注意：当前有 %d 个可见窗口同时命中这组类名/进程，类名可能太通用"
              "（如 Chrome_WidgetWin_1）。" % matches)
        print("  保存后会取面积最大的那个，未必是直播窗口；可再补 window_title"
              "（当前标题）收窄，或改用固定 region。")
    else:
        print("  （这组身份当前只命中直播窗口，可安全写入。）")

    if not write:
        return 0
    if matches > 1:
        print("因为有歧义，未写入。请按上面提示收紧身份后重试。", file=sys.stderr)
        return 3
    return _write_live_identity(klass, proc)


def _count_identity_matches(klass: str, proc: str) -> int:
    """统计当前屏幕上有几个可见窗口会命中这组（类名, 进程），排除自己的控制台。

    语义与引擎的窗口查找一致：非空的类名/进程都要同时满足（子串包含）。用来判断
    --pick 读到的身份是否够唯一；类名像 Chrome_WidgetWin_1 时往往会命中一堆窗口。"""
    k = klass.lower()
    p = proc.lower()
    me = os.path.basename(sys.executable).lower()
    hits = 0
    for _title, wklass, wproc, _box in list_windows(with_identity=True):
        wklass = (wklass or "").lower()
        wproc = (wproc or "").lower()
        if me and wproc == me:
            continue
        if (not k or k in wklass) and (not p or p in wproc):
            hits += 1
    return hits


def _write_live_identity(klass: str, proc: str) -> int:
    """把类名/进程写进 config/monitors.yaml 的「直播间」条目，保留注释头和其它条目。

    只改 window_class / window_process 的值（写成带单引号的标量），写前备份成 .bak，
    写后重新加载校验，出了问题能从备份还原。刻意不用 save_monitors：那样会经过
    yaml.safe_dump，把文件头部的说明注释和其它条目的行内注释全冲掉。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "monitors.yaml")
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            original = fh.read()
    except OSError as exc:
        print("读不到 %s: %s" % (path, exc), file=sys.stderr)
        return 1

    lines = original.splitlines(keepends=True)
    start = _find_monitor_block(lines, "直播间")
    if start is None:
        print("monitors.yaml 里没找到名为「直播间」的条目，未写入；请手动添加或改名。",
              file=sys.stderr)
        return 1

    for key, value in (("window_class", klass), ("window_process", proc)):
        end = len(lines)
        for i in range(start + 1, len(lines)):
            head = lines[i].rstrip(_CRLF)
            if head.lstrip().startswith("-") and _entry_name(head) is not None:
                end = i
                break
        _set_scalar_in_block(lines, start, end, key, value)

    backup = path + ".bak"
    try:
        with open(backup, "w", encoding="utf-8", newline="") as fh:
            fh.write(original)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
    except OSError as exc:
        print("写入 %s 失败: %s" % (path, exc), file=sys.stderr)
        return 1

    try:
        monitors = load_monitors()
    except Exception as exc:
        _restore(path, original)
        print("写完后解析失败，已还原（备份在 %s）: %s"
              % (os.path.basename(backup), exc), file=sys.stderr)
        return 1
    live = next((m for m in monitors if m.name == "直播间"), None)
    if live is None or live.window_class != klass or live.window_process != proc:
        _restore(path, original)
        print("写完后校验不一致，已还原。请检查 monitors.yaml 是否被改过。", file=sys.stderr)
        return 1
    print("已写入 %s（原文件备份为 %s）。"
          % (path, backup))
    print("提示：程序只读它所在目录下的 config；若你运行的是 exe，请把上面两衏")
    print("      身份填进 exe 所在目录下的 config/monitors.yaml，或直接用界面「编辑窗口」填写。")
    print("保存即热更新：运行中的程序下一轮扫描会用新身份跟窗，无需重启。")
    return 0


def _entry_name(comment_stripped_line: str):
    """从一个 '- name: xxx' 行取出条目名，取不到返回 None。"""
    body = comment_stripped_line.split("#", 1)[0].strip()
    if not body.startswith("- name:"):
        return None
    raw = body[len("- name:"):].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in (chr(39), chr(34)):
        raw = raw[1:-1]
    return raw or None


def _find_monitor_block(lines, name: str):
    for i, line in enumerate(lines):
        if _entry_name(line.rstrip(_CRLF)) == name:
            return i
    return None


def _set_scalar_in_block(lines, start: int, end: int, key: str, value: str) -> None:
    for i in range(start, end):
        content = lines[i].rstrip(_CRLF)
        head = content.split("#", 1)[0]
        if head.split(":", 1)[0].strip() == key:
            if lines[i].endswith(_CRLF):
                eol = _CRLF
            elif lines[i][-1:] in (_CR, _LF):
                eol = lines[i][-1:]
            else:
                eol = _LF
            indent = content[:len(content) - len(content.lstrip())]
            lines[i] = "%s%s: '%s'%s" % (indent, key, value, eol)
            return
    name_line = lines[start]
    eol = _CRLF if name_line.endswith(_CRLF) else _LF
    name_content = name_line.rstrip(_CRLF)
    indent = name_content[:len(name_content) - len(name_content.lstrip())] + "  "
    lines.insert(start + 1, "%s%s: '%s'%s" % (indent, key, value, eol))


def _restore(path: str, original: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(original)


if __name__ == "__main__":
    raise SystemExit(main())
