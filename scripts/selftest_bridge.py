"""aiPr Link 桥接自测（Python 侧）。

在没有 Premiere 的机器上，用 pymiere 的真实客户端去打一个「假 CEP 宿主」，
验证面板的 HTTP 协议 + ExtendScript 运行时 + 对象/集合往返 + 错误序列化是否正确。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\selftest_bridge.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PORT = int(os.environ.get("AIPR_SELFTEST_PORT", "3100"))
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


def start_host() -> subprocess.Popen:
    script = ROOT / "scripts" / "selftest_bridge.mjs"
    return subprocess.Popen(
        ["node", str(script), str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def main() -> int:
    print("启动假 CEP 宿主（Node）…")
    host = start_host()
    # 把 pymiere 指向假宿主：不需要真的 Premiere 在跑
    import pymiere
    from pymiere import core as pymiere_core
    from pymiere.core import ExtendScriptError

    pymiere_core.PANEL_URL = PANEL_URL
    pymiere_core.is_premiere_running = lambda: (True, 0)

    try:
        if not wait_for_host():
            output = ""
            if host.stdout:
                host.terminate()
                try:
                    output = host.stdout.read()
                except Exception:  # noqa: BLE001
                    output = ""
            print(f"{FAIL}假宿主没有起来，输出：\n{output}")
            return 1

        report("GET / 返回 'Premiere is alive'", True)

        version = pymiere.objects.app.version
        report("读取 app.version", version == "26.0.0", str(version))

        project_name = pymiere.objects.app.project.name
        report("读取 app.project.name（中文往返）", project_name == "仿真工程", str(project_name))

        children = pymiere.objects.app.project.rootItem.children
        report("集合长度（ProjectItemCollection.numItems）", len(children) == 2, f"len={len(children)}")
        report("集合按索引取值", children[0].name == "premiere_shot.mp4", str(children[0].name))

        report("普通表达式求值", pymiere_core.eval_script("1+1;") == 2)
        report("字符串往返（中文）", pymiere_core.eval_script('"中文字符串";') == "中文字符串")

        # 建一个序列后再验证集合层级
        pymiere_core.eval_script("app.project.createNewSequence('Protocol Sequence', '');")
        sequences = pymiere.objects.app.project.sequences
        report("集合长度（SequenceCollection.numSequences）", len(sequences) == 1, f"len={len(sequences)}")
        report("序列名字读回", sequences[0].name == "Protocol Sequence", str(sequences[0].name))

        tracks = sequences[0].videoTracks
        report("集合长度（TrackCollection.numTracks）", len(tracks) == 2, f"len={len(tracks)}")
        report("Time 对象能读回秒数", abs(sequences[0].getSettings().videoFrameRate.seconds - 1 / 30) < 1e-6)

        try:
            pymiere_core.eval_script("throw new Error('boom');")
        except ExtendScriptError as error:
            report(
                "ExtendScript 错误被正确序列化",
                "boom" in str(error),
                str(error).replace("\n", " | ")[:120],
            )
        else:
            report("ExtendScript 错误被正确序列化", False, "没有抛出 ExtendScriptError")

        # 面板必须在每次调用前记录源码，错误信息才能定位到行
        report(
            "错误信息带上了出错源码行",
            "throw new Error" in str(_last_error_message(pymiere, ExtendScriptError)),
        )
    finally:
        # 关闭宿主前先把 eval_script 变成空操作：pymiere 对象析构时还会回连一次，
        # 否则退出时会刷一屏无害的 ConnectionError。
        pymiere_core.eval_script = lambda *args, **kwargs: None
        host.terminate()
        try:
            host.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            host.kill()

    print()
    if failures:
        print(f"自测失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("桥接自测全部通过：面板协议与 ExtendScript 运行时可用。")
    return 0


def _last_error_message(pymiere, extend_script_error) -> str:
    from pymiere.core import eval_script

    try:
        eval_script("throw new Error('boom');")
    except extend_script_error as error:
        return str(error)
    except Exception as error:  # noqa: BLE001
        return str(error)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
