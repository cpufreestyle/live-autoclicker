import os
import sys

# PyInstaller onedir: exe 的 _internal/onnxruntime/capi 里同时放着
# onnxruntime_pybind11_state.pyd 和它依赖的 onnxruntime.dll。
# 但 FrozenImporter 只把 _internal 根目录注册进 DLL 搜索路径，不包含这个子目录，
# 于是 import onnxruntime 直接 "DLL load failed"，OCR 整条通道静默失效。
# 这里在最早期的运行期把该目录显式加回来。
if getattr(sys, "frozen", False) and hasattr(os, "add_dll_directory"):
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        capi = os.path.join(mei, "onnxruntime", "capi")
        if os.path.isdir(capi):
            # add_dll_directory 返回的句柄必须一直持有一点，进程退出前不能释放
            _capi_cookie = os.add_dll_directory(capi)
