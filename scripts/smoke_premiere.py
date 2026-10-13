"""在真机上跑一遍最小剪辑流程（需要已安装并打开 Premiere + aiPr Link 面板）。

它会新建一个测试序列、导入素材、追加/修剪片段、加标记，然后打印时间线快照。
默认跑完就把测试序列删掉，不污染你的工程；加 --keep 可以保留。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\smoke_premiere.py 素材1.mp4 素材2.png [--keep]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

OK = "  [OK]  "
FAIL = "  [!!]  "

failures: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"{OK if ok else FAIL}{name}{(' -> ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def cleanup(sequence_name: str, bin_name: str) -> None:
    """把冒烟过程新建的序列与素材箱删掉，不污染用户的工程。"""

    import pymiere

    project = pymiere.objects.app.project
    try:
        for candidate in project.sequences:
            if candidate.name == sequence_name:
                project.deleteSequence(candidate)
                print(f"      已删除测试序列 {sequence_name}")
                break
    except Exception as exc:  # noqa: BLE001
        print(f"      （清理测试序列失败，可手动删除：{exc}）")

    try:
        for item in project.rootItem.children:
            if item.name == bin_name and item.type == 2:
                item.remove()
                print(f"      已删除测试素材箱 {bin_name}")
                break
    except Exception as exc:  # noqa: BLE001
        print(f"      （清理测试素材箱失败，可手动删除：{exc}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="在真机上跑一遍最小剪辑流程")
    parser.add_argument("media", nargs="+", help="真实素材文件（视频/图片/音频均可）")
    parser.add_argument("--keep", action="store_true", help="保留测试序列（默认跑完删除）")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=float, default=25.0)
    parser.add_argument("--duration", type=float, default=3.0, help="每个片段的时长（秒）")
    args = parser.parse_args()

    from app.core.config import Settings
    from app.models.plan import SequenceSpec
    from app.services.media_scanner import describe_media_file
    from app.services.premiere.pymiere_adapter import PymierePremiereAdapter

    paths = [Path(item).expanduser().resolve() for item in args.media]
    missing = [str(item) for item in paths if not item.is_file()]
    if missing:
        print(f"{FAIL}素材不存在：{'、'.join(missing)}")
        return 1

    settings = Settings(
        media_roots=str(paths[0].parent),
        workspace_dir=str(Path(__file__).resolve().parents[1] / "workspace"),
        premiere_backend="pymiere",
        llm_api_key=None,
    )
    adapter = PymierePremiereAdapter(settings)

    status = adapter.status()
    print(f"  Premiere：{status.message}")
    print(f"  工程：{status.project_name}")
    if not status.connected:
        print(f"{FAIL}未连接，先把【窗口 > 扩展 > aiPr Link】面板打开")
        return 1
    report("连接真实 Premiere", True, f"{status.backend} {status.version}")

    sequence_name = f"aiPr Smoke {len(paths)}"
    sequence = None
    try:
        assets = [describe_media_file(path, settings=settings) for path in paths]
        report(
            "素材探测",
            True,
            "、".join(f"{asset.name}({asset.kind.value})" for asset in assets),
        )

        names = adapter.import_media(assets, "aiPr Smoke")
        report("import_media", len(names) == len(assets), "、".join(names))

        timeline = adapter.create_sequence(
            SequenceSpec(
                name=sequence_name,
                width=args.width,
                height=args.height,
                fps=args.fps,
            )
        )
        report("create_sequence", timeline.sequence_name == sequence_name, timeline.sequence_name)

        handles = []
        for index, asset in enumerate(assets, start=1):
            handle = adapter.append_clip(
                asset,
                ref=f"smoke_{index}",
                track="V1",
                in_point=0.0,
                out_point=args.duration,
            )
            handles.append(handle)
            print(
                f"      追加 {asset.name} → V1 [{handle.start:.2f}s → {handle.end:.2f}s]"
                f"（源 {handle.source_in:.2f} → {handle.source_out:.2f}）"
            )
        report("append_clip 全部成功", len(handles) == len(assets))

        if len(handles) >= 2:
            first = handles[0]
            moved = adapter.move_clip(first, to_time=1.0, track=None)
            print(f"      move_clip → 起点 {moved.start:.2f}s")
            report("move_clip", abs(moved.start - 1.0) < 0.2, f"{moved.start:.2f}s")

            trimmed = adapter.trim_clip(first, in_point=0.0, out_point=args.duration / 2)
            print(f"      trim_clip → [{trimmed.start:.2f}s → {trimmed.end:.2f}s]")
            report(
                "trim_clip",
                abs((trimmed.end - trimmed.start) - args.duration / 2) < 0.2,
                f"{trimmed.end - trimmed.start:.2f}s",
            )

        adapter.add_marker(at=0.5, name="aiPr Smoke", color="green", duration=0.0)

        snapshot = adapter.snapshot()
        report("snapshot 读取时间线", snapshot is not None and bool(snapshot.tracks))
        if snapshot is not None:
            print(f"      序列 {snapshot.sequence_name} @ {snapshot.fps:g}fps")
            for track in snapshot.tracks:
                if track.clips:
                    detail = "、".join(
                        f"{clip.name}[{clip.start:.2f}-{clip.end:.2f}]" for clip in track.clips
                    )
                    print(f"      {track.name}: {detail}")
            print(f"      标记：{[marker.name for marker in snapshot.markers]}")
            report("时间线里真的有了片段", any(track.clips for track in snapshot.tracks))
    except Exception as error:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report("冒烟流程未抛异常", False, str(error))
    finally:
        if not args.keep:
            cleanup(sequence_name, "aiPr Smoke")
    
    print()
    if failures:
        print(f"真机冒烟失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("真机冒烟通过：pymiere 适配器已能真实驱动 Premiere Pro。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
