"""EPUB 检查器入口脚本：既可直接跑，也是 PyInstaller 打包的入口。

用法：
    python epub_checker_app.py            正常启动界面
    python epub_checker_app.py --smoke    只做自检（打包后验证用），打印版本后退出
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--smoke" in argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import epub_core as core
        import epub_gui_qt as gui
        print("SMOKE OK gui=%s core=%s mode=%s"
              % (gui.VERSION, core.__version__, os.environ["QT_QPA_PLATFORM"]))
        return 0

    import epub_gui_qt as gui
    return gui.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
