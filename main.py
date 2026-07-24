"""
SSH批量执行工具 - 主入口
支持 GUI 和 CLI 两种模式
"""

import sys


def main():
    """主入口函数"""
    # 检查命令行参数
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        # CLI模式 - 移除 --cli 参数后再传给 CLI 解析器
        sys.argv.pop(1)
        from cli import main_cli

        main_cli()
    else:
        # GUI模式（默认）
        from gui import main_gui

        main_gui()


if __name__ == "__main__":
    main()
