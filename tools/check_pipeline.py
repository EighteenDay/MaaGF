#!/usr/bin/env python3
"""MaaGF pipeline 静态检查（零依赖，可在 CI 直接跑）。

检查内容：
  错误（会让 CI 失败）
    - JSON 解析失败
    - 跨文件节点重名（后加载的会静默覆盖前一个）
    - next / interrupt / on_error 指向不存在的节点
    - recognition/action 引用的模板图片不存在
    - ROI / target / begin / end 超出声明的分辨率
    - interface 里的任务入口、option 引用、default_case 不合法
  警告（默认只提示，--strict 时也算失败）
    - 从任务入口不可达的节点
    - 纯分发节点形成的环（无识别、无动作、无延时，可能空转）
    - OCR 期望词过弱（全是 1-2 字或纯数字，容易误匹配）
    - 两个节点的识别条件完全相同（可能互相抢占）
    - 未被任何节点引用的图片

用法：
    python tools/check_pipeline.py
    python tools/check_pipeline.py --strict
    python tools/check_pipeline.py --resource-dir assets/resource --resolution 1280x800
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

PREFIXES = ("[JumpBack]", "[Anchor]", "!")

# 默认分辨率：少女前线桌面端窗口客户区
DEFAULT_RESOLUTION = (1280, 800)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def strip_prefix(ref) -> str:
    if isinstance(ref, dict):
        ref = ref.get("name", "")
    text = str(ref)
    for prefix in PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def node_refs(node: dict) -> list[str]:
    """收集一个节点引用的其它节点（next / interrupt / on_error）。"""
    refs: list[str] = []
    for field in ("next", "interrupt", "on_error"):
        value = node.get(field)
        if not value:
            continue
        for item in value if isinstance(value, list) else [value]:
            target = strip_prefix(item)
            if target:
                refs.append(target)
    return refs


def iter_coords(value):
    """递归取出所有坐标（[x,y] 或 [x,y,w,h]）。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_coords(item)
        return
    if isinstance(value, (list, tuple)):
        if len(value) in (2, 4) and all(isinstance(v, (int, float)) for v in value):
            yield tuple(value)
            return
        for item in value:
            yield from iter_coords(item)


def load_pipelines(pipeline_dir: Path, report: Report) -> tuple[dict, dict]:
    nodes: dict[str, dict] = {}
    source: dict[str, str] = {}
    for path in sorted(pipeline_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            report.error(f"{path.name}: JSON 解析失败：{exc}")
            continue
        if not isinstance(data, dict):
            report.error(f"{path.name}: 顶层必须是对象")
            continue
        for name, node in data.items():
            if name in nodes:
                report.error(
                    f"节点重名：`{name}` 同时出现在 {source[name]} 与 {path.name}"
                    "（后者会覆盖前者）"
                )
            nodes[name] = node
            source[name] = path.name
    return nodes, source


def check_refs(nodes: dict, source: dict, report: Report) -> None:
    for name, node in nodes.items():
        for target in node_refs(node):
            if target not in nodes:
                report.error(f"{source[name]} → `{name}` 引用了不存在的节点 `{target}`")


def check_duplicate_routes(nodes: dict, source: dict, report: Report) -> None:
    """同一节点里 next 与 on_error 指向同一目标（重复路由），客户端会报错。"""
    for name, node in nodes.items():
        next_targets = [strip_prefix(item) for item in (node.get("next") or [])]
        error_targets = [strip_prefix(item) for item in (node.get("on_error") or [])]
        for label, targets in (("next", next_targets), ("on_error", error_targets)):
            duplicates = {t for t in targets if targets.count(t) > 1}
            for target in sorted(duplicates):
                report.error(f"{source[name]} → `{name}` 的 {label} 里重复出现 `{target}`")
        overlap = set(next_targets) & set(error_targets)
        for target in sorted(overlap):
            report.error(
                f"{source[name]} → `{name}` 的 next 与 on_error 都指向 `{target}`（重复路由）"
            )


def check_images(nodes: dict, source: dict, image_dir: Path, report: Report) -> None:
    used: set[str] = set()
    for name, node in nodes.items():
        params = (node.get("recognition") or {}).get("param") or {}
        template = params.get("template")
        if isinstance(template, str):
            template = [template]
        for item in template or []:
            used.add(item)
            if not (image_dir / item).exists():
                report.error(f"{source[name]} → `{name}` 引用的图片不存在：{item}")

    if image_dir.is_dir():
        for path in sorted(image_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".png":
                rel = path.relative_to(image_dir).as_posix()
                if rel not in used:
                    report.warn(f"图片未被任何节点引用：{rel}")


def check_reachable(nodes: dict, source: dict, entries: list[str], report: Report) -> None:
    if not entries:
        report.warn("interface 里没有任务入口，跳过可达性检查")
        return
    seen = {entry for entry in entries if entry in nodes}
    queue = deque(seen)
    while queue:
        current = queue.popleft()
        for target in node_refs(nodes.get(current, {})):
            if target in nodes and target not in seen:
                seen.add(target)
                queue.append(target)
    for name in sorted(set(nodes) - seen):
        report.warn(f"节点从任务入口不可达：`{name}`（{source[name]}）")


def check_dispatch_cycles(nodes: dict, source: dict, report: Report) -> None:
    dispatch = {
        name
        for name, node in nodes.items()
        if not node.get("recognition")
        and not node.get("action")
        and not node.get("pre_delay")
        and not node.get("post_delay")
        and not node.get("repeat")
    }
    graph = {name: [t for t in node_refs(nodes[name]) if t in dispatch] for name in dispatch}
    state: dict[str, int] = {}
    reported: set[frozenset[str]] = set()

    def visit(name: str, stack: list[str]) -> None:
        state[name] = 1
        stack.append(name)
        for target in graph.get(name, []):
            if state.get(target) == 1:
                cycle = stack[stack.index(target):]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    chain = " → ".join(cycle + [cycle[0]])
                    report.warn(f"纯分发节点成环（无识别、无延时，可能空转）：{chain}")
            elif state.get(target, 0) == 0:
                visit(target, stack)
        stack.pop()
        state[name] = 2

    for name in sorted(dispatch):
        if state.get(name, 0) == 0:
            visit(name, [])


def check_coords(nodes: dict, source: dict, resolution: tuple[int, int], report: Report) -> None:
    width, height = resolution
    for name, node in nodes.items():
        for section in ("recognition", "action"):
            params = (node.get(section) or {}).get("param") or {}
            for field in ("roi", "target", "begin", "end"):
                if field not in params:
                    continue
                for coord in iter_coords(params[field]):
                    x, y = coord[0], coord[1]
                    if len(coord) == 4:
                        right, bottom = x + coord[2], y + coord[3]
                    else:
                        right, bottom = x, y
                    if x < 0 or y < 0 or right > width or bottom > height:
                        report.error(
                            f"{source[name]} → `{name}` 的 {field}{list(coord)} 超出 "
                            f"{width}×{height}（右下角 {right},{bottom}）"
                        )


def is_weak_anchor(expected) -> bool:
    if not expected:
        return False
    tokens = [expected] if isinstance(expected, str) else expected
    for token in tokens:
        text = str(token)
        if len(text) > 2 and not re.fullmatch(r"[\d.\s_-]+", text):
            return False
    return True


def reachable_sets(nodes: dict, entries: list[str]) -> dict[str, set[str]]:
    """每个任务入口可达的节点集合。"""
    result: dict[str, set[str]] = {}
    for entry in entries:
        if entry not in nodes:
            continue
        seen: set[str] = set()
        queue = deque([entry])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            for target in node_refs(nodes.get(current, {})):
                if target in nodes and target not in seen:
                    queue.append(target)
        result[entry] = seen
    return result


def check_ocr(nodes: dict, source: dict, entries: list[str], report: Report) -> None:
    per_entry = reachable_sets(nodes, entries)
    node_entries: dict[str, set[str]] = defaultdict(set)
    for entry, members in per_entry.items():
        for name in members:
            node_entries[name].add(entry)

    seen_pairs: dict[tuple, tuple[str, set[str]]] = {}
    for name, node in nodes.items():
        recognition = node.get("recognition") or {}
        if recognition.get("type") != "OCR":
            continue
        params = recognition.get("param") or {}
        expected = params.get("expected")
        roi = params.get("roi")

        if is_weak_anchor(expected):
            tokens = [expected] if isinstance(expected, str) else expected
            report.warn(
                f"{source[name]} → `{name}` 的 OCR 期望词过弱：{tokens}（易误匹配，建议收紧 ROI 或改模板图）"
            )

        key = (json.dumps(roi, sort_keys=True), json.dumps(expected, sort_keys=True))
        if key in seen_pairs:
            other, other_entries = seen_pairs[key]
            # 只有两个节点可能同时出现在同一条任务链路上，才会真的互相抢占
            if node_entries[name] & other_entries:
                report.warn(
                    f"{source[name]} → `{name}` 与 `{other}` 的 ROI 与期望词完全相同，可能互相抢占"
                )
        else:
            seen_pairs[key] = (name, node_entries[name])


def check_interface(interface_path: Path, nodes: dict, report: Report) -> list[str]:
    if not interface_path.is_file():
        report.error(f"interface 文件不存在：{interface_path}")
        return []
    try:
        data = json.loads(interface_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        report.error(f"{interface_path.name}: JSON 解析失败：{exc}")
        return []

    entries: list[str] = []
    names: set[str] = set()
    for task in data.get("task", []):
        name = task.get("name", "")
        if name in names:
            report.error(f"interface 任务重名：`{name}`")
        names.add(name)
        entry = task.get("entry", "")
        entries.append(entry)
        if entry not in nodes:
            report.error(f"interface 任务 `{name}` 的入口 `{entry}` 在 pipeline 中不存在")
        for option_name in task.get("option", []) or []:
            if option_name not in (data.get("option") or {}):
                report.error(f"interface 任务 `{name}` 引用了不存在的配置项 `{option_name}`")

    for option_name, option in (data.get("option") or {}).items():
        cases = option.get("cases") or []
        case_names = [case.get("name", "") for case in cases]
        default = option.get("default_case")
        if default and default not in case_names:
            report.error(f"配置项 `{option_name}` 的 default_case `{default}` 不在 cases 中")
        if option.get("type") == "switch" and len(cases) != 2:
            report.error(f"配置项 `{option_name}` 是 switch，但 cases 不是 2 个")
        for case in cases:
            override = case.get("pipeline_override") or {}
            for node_name, patch in override.items():
                if node_name not in nodes:
                    report.error(
                        f"配置项 `{option_name}` / `{case.get('name')}` 覆盖了不存在的节点 `{node_name}`"
                    )
                    continue
                for target in (patch.get("next") or []) if isinstance(patch, dict) else []:
                    if strip_prefix(target) not in nodes:
                        report.error(
                            f"配置项 `{option_name}` / `{case.get('name')}` 的 next 指向不存在的节点 `{target}`"
                        )
    return entries


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="MaaGF pipeline 静态检查")
    parser.add_argument("--resource-dir", default="assets/resource", help="资源目录（默认 assets/resource）")
    parser.add_argument("--interface", default="assets/interface.json", help="interface.json 路径")
    parser.add_argument("--resolution", default="1280x800", help="基准分辨率，如 1280x800")
    parser.add_argument("--strict", action="store_true", help="把警告也当作失败")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = parser.parse_args(argv)

    try:
        width, height = (int(part) for part in args.resolution.lower().split("x"))
    except ValueError:
        parser.error("--resolution 需要形如 1280x800")

    resource_dir = Path(args.resource_dir)
    pipeline_dir = resource_dir / "pipeline"
    image_dir = resource_dir / "image"
    if not pipeline_dir.is_dir():
        print(f"找不到 pipeline 目录：{pipeline_dir}", file=sys.stderr)
        return 2

    report = Report()
    nodes, source = load_pipelines(pipeline_dir, report)
    entries = check_interface(Path(args.interface), nodes, report)
    check_refs(nodes, source, report)
    check_duplicate_routes(nodes, source, report)
    check_images(nodes, source, image_dir, report)
    check_reachable(nodes, source, entries, report)
    check_dispatch_cycles(nodes, source, report)
    check_coords(nodes, source, (width, height), report)
    check_ocr(nodes, source, entries, report)

    if not args.quiet:
        for message in report.errors:
            print(f"  ✗ {message}")
        for message in report.warnings:
            print(f"  ! {message}")

    print(
        f"\n节点 {len(nodes)} 个 · 错误 {len(report.errors)} · 警告 {len(report.warnings)}"
        f" · 基准 {width}×{height}"
    )
    if report.errors:
        print("❌ 检查未通过")
        return 1
    if args.strict and report.warnings:
        print("❌ 检查未通过（--strict 下警告视为失败）")
        return 1
    print("✅ 检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
