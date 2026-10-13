"""检查 aiPr Link 桥接与 Premiere Pro 的连通性。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\check_premiere.py

它会依次验证：
1. 本机是否装了 pymiere（Python 侧驱动库）；
2. CEP 面板是否在 127.0.0.1:3000 上监听（需要 Premiere 已打开、已打开工程、且面板已加载）；
3. 是否能真正读到 Premiere 的版本号与当前工程名。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

OK = "  [OK]  "
FAIL = "  [!!]  "

PANEL_URL = "http://127.0.0.1:3000"


def check_pymiere() -> bool:
    try:
        import pymiere  # noqa: F401
    except ImportError as exc:
        print(f"{FAIL}pymiere 不可用：{exc}")
        print("        请执行：pip install -r backend/requirements-premiere.txt")
        return False
    except Exception as exc:  # noqa: BLE001 - distutils 缺失等历史原因
        print(f"{FAIL}pymiere 导入失败：{exc}")
        print("        提示：pymiere 依赖 Python 3.12 起已移除的 distutils，请确认已安装 setuptools")
        return False
    print(f"{OK}pymiere 已安装")
    return True


def check_panel() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(PANEL_URL, timeout=3) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"{FAIL}连不上 {PANEL_URL}：{exc.reason}")
        print("        请确认：1) Premiere 已启动并打开了工程；2)【窗口 > 扩展 > aiPr Link】面板已打开")
        print("        面板没安装的话先跑：scripts\\install-premiere-bridge.ps1")
        return False

    if body != "Premiere is alive":
        print(f"{FAIL}端口有响应但内容异常：{body[:120]}")
        return False
    print(f"{OK}CEP 面板已就绪（{PANEL_URL}）")
    return True


def check_extendscript() -> bool:
    try:
        import pymiere
    except Exception:  # noqa: BLE001 - 上一步已经报过错了
        return False

    try:
        version = pymiere.objects.app.version
        project = pymiere.objects.app.project
        project_name = getattr(project, "name", None) or "<未保存的新工程>"
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL}ExtendScript 执行失败：{exc}")
        return False

    print(f"{OK}Premiere 版本：{version}")
    print(f"{OK}当前工程：{project_name}")
    return True


def main() -> int:
    print("检查 AI 驱动 Premiere 的前置条件……")
    results = [check_pymiere(), check_panel()]
    if all(results):
        results.append(check_extendscript())

    print()
    if all(results):
        print("一切就绪：可以切换 AIPR_PREMIERE_BACKEND=pymiere 并提交剪辑任务了。")
        return 0

    print("还有环节没通，按上面的提示处理后重试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
