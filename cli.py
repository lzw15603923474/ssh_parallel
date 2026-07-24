"""
SSH 批量执行工具 - CLI命令行接口
"""

import argparse
import asyncio
import csv
import json

from config import ConfigManager
from ssh_core import SSHPool, SSHResult


def print_results(results: list[SSHResult]):
    """打印执行结果"""
    print("\n" + "=" * 80)
    print("执行结果汇总")
    print("=" * 80)

    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count

    print(f"\n总节点数: {len(results)}")
    print(f"成功: {success_count}")
    print(f"失败: {fail_count}")

    print("\n" + "-" * 80)
    print("详细结果:")
    print("-" * 80)

    for result in results:
        status = "[OK]" if result.success else "[FAIL]"
        print(f"\n{status} {result.host}:")

        if result.success:
            print(f"  退出码: {result.exit_code}")
            print(f"  耗时: {result.duration:.2f}s")
            if result.stdout:
                print(f"  输出:\n{result.stdout}")
            if result.stderr:
                print(f"  错误:\n{result.stderr}")
        else:
            print(f"  错误: {result.error}")


def export_results(results: list[SSHResult], output_file: str):
    """导出结果到文件"""
    if output_file.endswith(".csv"):
        with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "主机",
                    "状态",
                    "退出码",
                    "耗时(s)",
                    "标准输出",
                    "错误输出",
                    "错误信息",
                ]
            )
            for r in results:
                writer.writerow(
                    [
                        r.host,
                        "成功" if r.success else "失败",
                        r.exit_code if r.exit_code is not None else "",
                        f"{r.duration:.2f}",
                        r.stdout[:200] if r.stdout else "",
                        r.stderr[:200] if r.stderr else "",
                        r.error if r.error else "",
                    ]
                )
    elif output_file.endswith(".json"):
        data = []
        for r in results:
            data.append(
                {
                    "host": r.host,
                    "success": r.success,
                    "exit_code": r.exit_code,
                    "duration": r.duration,
                    "stdout": r.stdout,
                    "stderr": r.stderr,
                    "error": r.error,
                }
            )
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n结果已导出到: {output_file}")


async def run_cli(args):
    """运行CLI模式"""
    config_manager = ConfigManager()
    config = config_manager.load()

    if not config.hosts:
        print("错误: 未找到主机配置，请先在GUI模式下添加主机或导入配置")
        return

    # 获取指定分组的主机
    if args.group:
        hosts = [h for h in config.hosts if h.group == args.group]
    else:
        hosts = config.hosts

    if not hosts:
        print(f"错误: 未找到主机（分组: {args.group or '全部'}）")
        return

    print(f"正在连接 {len(hosts)} 台主机...")

    pool = SSHPool(concurrency=config.connection.concurrency)
    connect_results = await pool.connect_all(hosts, timeout=config.connection.timeout)

    connected_count = sum(1 for _, success, _ in connect_results if success)
    print(f"成功连接 {connected_count}/{len(hosts)} 台主机")

    if connected_count == 0:
        await pool.close_all()
        return

    print(f"\n执行命令: {args.command}")
    print("-" * 80)

    results = await pool.execute_all(
        args.command,
        sudo=args.sudo,
        timeout=args.timeout if args.timeout else config.connection.timeout,
    )

    await pool.close_all()

    print_results(results)

    if args.output:
        export_results(results, args.output)


def main_cli():
    """CLI入口"""
    parser = argparse.ArgumentParser(description="SSH批量执行工具 - CLI模式")

    parser.add_argument("-c", "--command", required=True, help="要执行的命令")
    parser.add_argument("-g", "--group", help="主机分组名称")
    parser.add_argument("-s", "--sudo", action="store_true", help="使用sudo执行")
    parser.add_argument("-t", "--timeout", type=int, help="命令执行超时时间(秒)")
    parser.add_argument("-o", "--output", help="输出文件路径(.csv或.json)")

    args = parser.parse_args()

    asyncio.run(run_cli(args))


if __name__ == "__main__":
    main_cli()
