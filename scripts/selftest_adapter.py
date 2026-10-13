"""在假 CEP 宿主上跑一遍 pymiere 适配器的真实逻辑（不需要装 Premiere）。

覆盖：连接状态 → 导入素材 → 建序列（含真实预设解析）→ 追加/插入/移动/修剪片段
     → 标记 → 音量 → 读时间线快照。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\selftest_adapter.py
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PORT = int(__import__("os").environ.get("AIPR_SELFTEST_PORT", "3101"))
PANEL_URL = f"http://127.0.0.1:{PORT}"

PASS = "  [OK]  "
FAIL = "  [!!]  "

failures: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL}{name}{(' -> ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def wait_for_host(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(PANEL_URL, timeout=1) as response:
                if response.read().decode("utf-8") == "Premiere is alive":
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.3)
    return False


def make_asset(name: str, duration: float, kind: str = "video"):
    from app.models.media import MediaAsset, MediaKind

    path = Path("E:/media") / name
    return MediaAsset(
        id=MediaAsset.make_id(path),
        path=str(path),
        name=name,
        stem=Path(name).stem,
        relative_path=name,
        kind=MediaKind(kind),
        extension=Path(name).suffix,
        size_bytes=1024,
        modified_at=datetime.now(timezone.utc),
        duration_seconds=duration,
        width=1920,
        height=1080,
        fps=30.0,
        probe_source="ffprobe",
    )


def main() -> int:
    print("启动假 CEP 宿主（Node 仿真器）…")
    host = subprocess.Popen(
        ["node", str(ROOT / "scripts" / "selftest_bridge.mjs"), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    from pymiere import core as pymiere_core

    pymiere_core.PANEL_URL = PANEL_URL
    pymiere_core.is_premiere_running = lambda: (True, 0)

    try:
        if not wait_for_host():
            print(f"{FAIL}仿真器没有起来")
            return 1
        report("假宿主就绪（面板协议可用）", True)

        from app.core.config import Settings
        from app.models.plan import SequenceSpec
        from app.services.premiere.pymiere_adapter import PymierePremiereAdapter

        settings = Settings(
            media_roots=str(ROOT / "media"),
            workspace_dir=str(ROOT / "workspace"),
            premiere_backend="pymiere",
            llm_api_key=None,
        )
        adapter = PymierePremiereAdapter(settings)

        status = adapter.status()
        report("status() 能连上", status.connected, status.message)
        report("读到 Premiere 版本", status.version == "26.0.0", str(status.version))

        video_a = make_asset("clip_a.mp4", 8.0)
        video_b = make_asset("clip_b.mp4", 6.0)
        names = adapter.import_media([video_a, video_b], "aiPr 素材")
        report("import_media 回查素材名", names == ["clip_a.mp4", "clip_b.mp4"], str(names))

        spec = SequenceSpec(name="SelfTest Sequence", width=1920, height=1080, fps=30.0)
        timeline = adapter.create_sequence(spec)
        report("create_sequence 建立的是空序列", timeline.sequence_name == "SelfTest Sequence"
               and not any(track.clips for track in timeline.tracks))

        first = adapter.append_clip(video_a, ref="a", track="V1", in_point=0.0, out_point=3.0)
        report("append_clip 起点为 0", abs(first.start) < 1e-6, f"{first.start} → {first.end}")
        report("append_clip 生效时长", abs((first.end - first.start) - 3.0) < 0.01,
               f"{first.end - first.start:.3f}s")

        second = adapter.append_clip(video_b, ref="b", track="V1", in_point=1.0, out_point=4.0)
        report("第二个片段接在轨道末尾", abs(second.start - 3.0) < 0.01, f"{second.start} → {second.end}")

        third = adapter.insert_clip(video_a, ref="c", track="V2", at=1.0, in_point=0.0, out_point=2.0)
        report("insert_clip 落在指定时刻", abs(third.start - 1.0) < 0.01, str(third.start))

        moved = adapter.move_clip(third, to_time=2.5, track=None)
        report("move_clip 移动到 2.5s", abs(moved.start - 2.5) < 0.01, str(moved.start))

        trimmed = adapter.trim_clip(first, in_point=0.5, out_point=2.0)
        report("trim_clip 时长变 1.5s", abs((trimmed.end - trimmed.start) - 1.5) < 0.02,
               f"{trimmed.start:.2f} → {trimmed.end:.2f}")

        adapter.set_volume(second, db=-6.0)
        report("set_volume 写进 Volume 组件", True)

        adapter.add_marker(at=1.5, name="看点", color="red", duration=0.5)
        snapshot = adapter.snapshot()
        report(
            "snapshot 能读出标记",
            any(m.name == "看点" and abs(m.at - 1.5) < 0.01 for m in snapshot.markers),
            str([(m.name, m.at) for m in snapshot.markers]),
        )

        v1 = snapshot.track("V1")
        report("V1 上有 2 个片段", len(v1.clips) == 2, str(len(v1.clips)))
        report(
            "V2 上的片段位置正确",
            snapshot.track("V2").clips[0].start == 2.5,
            str(snapshot.track("V2").clips[0].start),
        )
        report("时间线总轨道数", len(snapshot.tracks) == 4, str(len(snapshot.tracks)))

        # 预设解析：仿真器的 app.path 指向真实的 Premiere 安装目录
        preset = adapter._resolve_sequence_preset(SequenceSpec(name="p", width=1920, height=1080, fps=25.0))
        report("自动挑选 1080p25 序列预设", Path(preset).is_file(), Path(preset).name)
        portrait = adapter._resolve_sequence_preset(
            SequenceSpec(name="p", width=1080, height=1920, fps=30.0)
        )
        report("竖屏能挑到 Portrait 预设", "portrait" in Path(portrait).name.lower(), Path(portrait).name)
    except Exception as error:  # noqa: BLE001 - 自测脚本要把异常变成可读输出
        import traceback

        traceback.print_exc()
        report("适配器自测未抛异常", False, str(error))
    finally:
        pymiere_core.eval_script = lambda *args, **kwargs: None
        if host.stdout:
            host.terminate()
            try:
                output = host.stdout.read()
                if output:
                    print("\n--- 仿真器输出 ---")
                    print(output.strip()[:2000])
            except Exception:  # noqa: BLE001
                pass
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            host.kill()

    print()
    if failures:
        print(f"适配器自测失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("适配器自测全部通过：pymiere 适配器在仿真器上行为正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
