"""MaaGF 实机冒烟/诊断脚本（需要游戏已运行）。

用法：
    python tools/runtime_smoke.py                     # 截图 + 两个锚点 OCR 探测 + 0.环境检查
    python tools/runtime_smoke.py --task 收电点赞 --seconds 90

输出的截图和 MaaFramework 日志都落在 debug/runtime-test/（已被 .gitignore 忽略）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from maa.controller import Win32Controller
from maa.define import MaaWin32InputMethodEnum as Input
from maa.define import MaaWin32ScreencapMethodEnum as Screencap
from maa.pipeline import JOCR, JRecognitionType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "debug" / "runtime-test"

# 与 assets/resource/pipeline/0环境检查.json 保持一致
ANCHORS = {
    "右侧-研发": ([820, 330, 220, 80], ["研发"]),
    "右侧-编成": ([1060, 414, 210, 99], ["编成"]),
}


def find_game_window(title: str = "少女前线", klass: str = "UnityWndClass"):
    for window in Toolkit.find_desktop_windows():
        if window.window_name == title and window.class_name == klass:
            return window
    return None


def grab(controller: Win32Controller):
    job = controller.post_screencap()
    job.wait()
    if not job.succeeded:
        return None
    return job.get()


def save_png(image, path: Path) -> None:
    try:
        from PIL import Image

        arr = image.get() if hasattr(image, "get") else image
        Image.fromarray(arr[:, :, ::-1] if arr.shape[2] == 3 else arr).save(path)
        print(f"截图: {path} ({arr.shape[1]}x{arr.shape[0]})")
    except Exception as exc:
        print(f"!! 截图保存失败: {exc}")


def probe_ocr(tasker: Tasker, image, label: str, roi, expected) -> None:
    job = tasker.post_recognition(JRecognitionType.OCR, JOCR(roi=roi, expected=expected), image)
    job.wait()
    detail = job.get()
    if detail is None or not detail.nodes:
        print(f"  [{label}] 无结果")
        return
    reco = detail.nodes[0].recognition
    texts = []
    for item in (reco.all_results or []):
        text = getattr(item, "text", None)
        score = getattr(item, "score", None)
        if text is not None:
            texts.append(f"{text}({score:.2f})" if isinstance(score, float) else str(text))
    print(f"  [{label}] hit={reco.hit} box={reco.box} 结果={texts[:6]}")


def run_task(tasker: Tasker, entry: str, seconds: float | None) -> None:
    print(f"\n>>> 任务: {entry}")
    job = tasker.post_task(entry)
    deadline = None if seconds is None else time.monotonic() + seconds
    while job.running:
        if deadline is not None and time.monotonic() > deadline:
            print(f"    到达 {seconds}s 上限，请求停止")
            tasker.post_stop()
            break
        time.sleep(0.2)
    job.wait()

    detail = job.get()
    if detail is None:
        print("!! 没有 TaskDetail")
        return
    status = detail.status
    print(f"    结果: succeeded={status.succeeded} failed={status.failed} 节点={len(detail.nodes)}")
    for node in detail.nodes:
        reco = node.recognition
        hit = getattr(reco, "hit", None)
        box = getattr(reco, "box", None)
        print(f"      {'✓' if node.completed else '✗'} {node.name!r} hit={hit} box={box}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="", help="额外运行的任务入口名")
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--no-guard", action="store_true", help="跳过 0.环境检查")
    parser.add_argument(
        "--mouse",
        default="SendMessageWithCursorPos",
        choices=["SendMessageWithCursorPos", "SendMessage", "PostMessage"],
        help="鼠标输入方式（默认 SendMessageWithCursorPos）",
    )
    parser.add_argument(
        "--luna-move",
        default="",
        help="任务开始前给 Luna 发一条 MOVE，格式 'x,y'（需 Luna 已在运行）",
    )
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Toolkit.init_option(str(ROOT))

    window = find_game_window()
    if window is None:
        print("!! 找不到「少女前线」窗口")
        return 2
    print(f"窗口: hwnd={window.hwnd} {window.class_name} / {window.window_name}")

    resource = Resource()
    load = resource.post_bundle(ROOT / "assets" / "resource")
    load.wait()
    print(f"资源: succeeded={load.succeeded} loaded={resource.loaded} nodes={len(resource.node_list)}")
    if not load.succeeded:
        return 3

    mouse_method = getattr(Input, args.mouse)
    controller = Win32Controller(
        window.hwnd,
        screencap_method=Screencap.FramePool,
        mouse_method=mouse_method,
        keyboard_method=Input.SendMessage,
    )
    # 与 MFAAvalonia 行为一致：不做缩放，按原始尺寸识别（pipeline 的坐标是 1280×800）
    controller.set_screenshot_use_raw_size(True)
    conn = controller.post_connection()
    conn.wait()
    print(f"控制器: succeeded={conn.succeeded} mouse={args.mouse}")
    if not conn.succeeded:
        return 4

    tasker = Tasker()
    tasker.set_log_dir(LOG_DIR)
    if not tasker.bind(resource, controller):
        print("!! Tasker 绑定失败")
        return 5

    image = grab(controller)
    if image is None:
        print("!! 截图失败")
        return 6
    save_png(image, LOG_DIR / "screen.png")

    print("\n锚点探测（与 0环境检查 相同参数）:")
    for label, (roi, expected) in ANCHORS.items():
        probe_ocr(tasker, image, label, roi, expected)

    if args.luna_move:
        try:
            sys.path.insert(0, str(ROOT / "tools" / "hack" / "luna"))
            from client import LunaClient

            x, y = (int(v) for v in args.luna_move.split(","))
            print(f"\nLuna MOVE {x} {y} -> {LunaClient().move(x, y)}")
        except Exception as exc:
            print(f"!! Luna MOVE 失败: {exc}")

    if not args.no_guard:
        run_task(tasker, "环境检查", None)
    if args.task:
        run_task(tasker, args.task, args.seconds)

    tasker.post_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
