"""
SSH 批量执行工具 - SSH核心模块
"""

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime

import asyncssh

# ANSI转义序列正则表达式
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def clean_ansi_escape(output: str) -> str:
    """清理ANSI转义序列（颜色代码等）"""
    return ANSI_ESCAPE_PATTERN.sub("", output)


@dataclass
class SSHResult:
    """SSH执行结果"""

    host: str
    success: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration: float = 0.0
    start_time: datetime | None = None


@dataclass
class SFTPResult:
    """SFTP传输结果"""

    host: str
    success: bool = False
    filename: str = ""
    size: int = 0
    transferred: int = 0
    error: str = ""
    duration: float = 0.0


class SSHConnection:
    """SSH连接封装"""

    def __init__(self, host_config):
        self.host_config = host_config
        self.connection = None
        self.shell = None
        self.is_root = False

    async def connect(self, timeout: int = 30) -> tuple[bool, str]:
        """建立SSH连接"""
        try:
            # 直接使用 asyncssh.connect 的参数
            kwargs = {
                "host": self.host_config.host,
                "port": self.host_config.port,
                "username": self.host_config.username,
                "known_hosts": None,
            }

            if self.host_config.use_key:
                kwargs["client_keys"] = [self.host_config.key_file]
                if self.host_config.key_passphrase:
                    kwargs["passphrase"] = self.host_config.key_passphrase
            else:
                kwargs["password"] = self.host_config.password

            # 使用 asyncio.wait_for 设置超时
            self.connection = await asyncio.wait_for(
                asyncssh.connect(**kwargs), timeout=timeout
            )

            # 创建PTY会话以支持交互式命令（所有用户都创建）
            await self._create_pty_session(timeout)

            # 如果需要切换到root用户，执行切换
            if self.host_config.switch_to_root and self.host_config.root_password:
                switch_result = await self._switch_to_root_with_pty(timeout)
                if switch_result:
                    self.is_root = True
                    return True, "连接成功，已切换到root用户"
                return False, "连接成功，但切换root用户失败"

            # 检查是否直接以root用户登录
            if self.host_config.username == "root":
                self.is_root = True
                return True, "连接成功（root用户）"

            return True, "连接成功"
        except TimeoutError:
            return False, "连接超时"
        except asyncssh.PermissionDenied:
            return False, "认证失败"
        except asyncssh.Error as e:
            return False, f"连接错误: {str(e)}"
        except Exception as e:
            return False, f"未知错误: {str(e)}"

    async def _switch_to_root_with_pty(self, timeout: int = 30) -> bool:
        """使用PTY建立持久化的root会话"""
        try:
            # 创建PTY进程（term_type参数触发PTY分配）
            # 设置环境变量禁用颜色输出
            env = {
                "TERM": "vt100",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "COLORTERM": "none",
                "LS_COLORS": "",
            }
            process = await self.connection.create_process(
                "/bin/bash",
                stdin=asyncssh.PIPE,
                stdout=asyncssh.PIPE,
                stderr=asyncssh.PIPE,
                term_type="vt100",
                env=env,
            )

            self.shell = process
            print("[ROOT切换] PTY进程创建成功")

            # 先读取初始banner
            await asyncio.sleep(0.3)
            banner = ""
            prompt_found = False
            try:
                banner = await asyncio.wait_for(
                    self.shell.stdout.read(4096), timeout=0.5
                )
                if banner:
                    banner_str = (
                        banner.decode("utf-8", errors="replace")
                        if isinstance(banner, bytes)
                        else banner
                    )
                    print(f"[ROOT切换] Shell启动输出: {repr(banner_str[:200])}")
                    # 检查banner中是否已经包含提示符
                    if any(char in banner_str for char in [">", "$", "#", "%"]):
                        print("[ROOT切换] Banner中已检测到提示符")
                        prompt_found = True
            except TimeoutError:
                pass

            # 如果banner中没有检测到提示符，再等待
            if not prompt_found:
                print("[ROOT切换] 等待shell提示符...")
                await asyncio.wait_for(self._wait_for_prompt(), timeout=8)
            else:
                print("[ROOT切换] Banner中已包含提示符，跳过等待")

            # 发送su命令（PTY中使用\r确保正确执行）
            self.shell.stdin.write("su - root\r")
            await self.shell.stdin.drain()
            print("[ROOT切换] 已发送su - root命令")

            # 短暂延迟，让su命令开始执行
            await asyncio.sleep(0.5)

            # 等待密码提示符
            print("[ROOT切换] 等待密码提示符...")
            await asyncio.wait_for(self._wait_for_password_prompt(), timeout=8)

            # 发送root密码
            self.shell.stdin.write(self.host_config.root_password + "\r")
            await self.shell.stdin.drain()
            print("[ROOT切换] 已发送root密码")

            # 短暂延迟，让认证完成
            await asyncio.sleep(0.5)

            # 等待切换成功（检测root提示符）
            print("[ROOT切换] 等待root提示符...")
            await asyncio.wait_for(self._wait_for_root_prompt(), timeout=8)

            # 额外验证：发送id命令确认身份
            self.shell.stdin.write("id\r")
            await self.shell.stdin.drain()
            await asyncio.sleep(0.3)

            verify_output = ""
            try:
                while True:
                    chunk = await asyncio.wait_for(
                        self.shell.stdout.read(1024), timeout=1
                    )
                    if chunk:
                        chunk_str = (
                            chunk.decode("utf-8", errors="replace")
                            if isinstance(chunk, bytes)
                            else chunk
                        )
                        verify_output += chunk_str
                        if "#" in chunk_str or (
                            verify_output and "uid=0" in verify_output
                        ):
                            break
                    else:
                        break
            except TimeoutError:
                pass

            if verify_output:
                print(f"[ROOT切换] 身份验证输出: {repr(verify_output[:200])}")
                if "uid=0" in verify_output:
                    print("[ROOT切换] 确认已切换到root用户(uid=0)")

            print("[ROOT切换] 切换到root用户成功")
            return True
        except TimeoutError:
            print("[ROOT切换] 超时失败")
            return False
        except Exception as e:
            print(f"[ROOT切换] 异常失败: {str(e)}")
            return False

    async def _create_pty_session(self, timeout: int = 30) -> None:
        """创建PTY会话（所有用户通用）"""
        try:
            # 创建PTY进程（term_type参数触发PTY分配）
            # 设置环境变量禁用颜色输出
            env = {
                "TERM": "vt100",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "COLORTERM": "none",
                "LS_COLORS": "",
            }
            process = await self.connection.create_process(
                "/bin/bash",
                stdin=asyncssh.PIPE,
                stdout=asyncssh.PIPE,
                stderr=asyncssh.PIPE,
                term_type="vt100",
                env=env,
            )

            self.shell = process
            print(f"[PTY会话] 创建成功，用户: {self.host_config.username}")

            # 等待shell提示符
            await asyncio.sleep(0.3)
            banner = ""
            try:
                banner = await asyncio.wait_for(
                    self.shell.stdout.read(4096), timeout=0.5
                )
                if banner:
                    banner_str = (
                        banner.decode("utf-8", errors="replace")
                        if isinstance(banner, bytes)
                        else banner
                    )
                    print(f"[PTY会话] Shell启动输出: {repr(banner_str[:200])}")
            except TimeoutError:
                pass

            print("[PTY会话] 已建立")
        except Exception as e:
            print(f"[PTY会话] 创建失败: {str(e)}")

    async def _create_root_pty_session(self, timeout: int = 30) -> None:
        """直接以root用户登录时创建PTY会话（已废弃，保留兼容）"""
        await self._create_pty_session(timeout)

    async def _wait_for_prompt(self) -> bool:
        """等待shell提示符出现"""
        try:
            collected_output = ""
            while True:
                try:
                    output = await asyncio.wait_for(
                        self.shell.stdout.read(1024), timeout=1
                    )
                    if output:
                        decoded = (
                            output.decode("utf-8")
                            if isinstance(output, bytes)
                            else output
                        )
                        collected_output += decoded
                        # 移除控制字符
                        clean_output = "".join(
                            c
                            for c in collected_output
                            if ord(c) >= 32 or c == "\n" or c == "\r"
                        )
                        print(f"[ROOT切换] 收到输出(原始): {repr(decoded)}")
                        print(f"[ROOT切换] 累计输出(清理后): {repr(clean_output)}")
                        # 检测提示符字符
                        if any(char in clean_output for char in [">", "$", "#", "%"]):
                            print("[ROOT切换] 检测到提示符")
                            return True
                except TimeoutError:
                    # 检查累计输出中是否有提示符
                    if collected_output:
                        clean_output = "".join(
                            c
                            for c in collected_output
                            if ord(c) >= 32 or c == "\n" or c == "\r"
                        )
                        if any(char in clean_output for char in [">", "$", "#", "%"]):
                            print("[ROOT切换] 在累计输出中检测到提示符")
                            return True
                    continue
        except Exception as e:
            print(f"[ROOT切换] _wait_for_prompt异常: {str(e)}")
            return False

    async def _wait_for_password_prompt(self) -> bool:
        """等待密码提示符出现"""
        try:
            while True:
                try:
                    output = await asyncio.wait_for(
                        self.shell.stdout.read(1024), timeout=1
                    )
                    if output:
                        decoded = (
                            output.decode("utf-8")
                            if isinstance(output, bytes)
                            else output
                        )
                        print(f"[ROOT切换] 收到输出: {repr(decoded)}")
                        if "Password:" in decoded or "password:" in decoded.lower():
                            return True
                        # 如果收到错误信息，打印出来
                        if (
                            "su:" in decoded
                            or "Authentication" in decoded
                            or "failure" in decoded.lower()
                        ):
                            print(f"[ROOT切换] 认证失败信息: {decoded}")
                except TimeoutError:
                    continue
        except Exception as e:
            print(f"[ROOT切换] _wait_for_password_prompt异常: {str(e)}")
            return False

    async def _wait_for_root_prompt(self) -> bool:
        """等待root提示符出现"""
        try:
            while True:
                try:
                    output = await asyncio.wait_for(
                        self.shell.stdout.read(1024), timeout=1
                    )
                    if output:
                        decoded = (
                            output.decode("utf-8")
                            if isinstance(output, bytes)
                            else output
                        )
                        print(f"[ROOT切换] 收到输出: {repr(decoded)}")
                        if "#" in decoded and "Password:" not in decoded:
                            return True
                        # 如果收到错误信息，打印出来
                        if (
                            "su:" in decoded
                            or "Authentication" in decoded
                            or "failure" in decoded.lower()
                        ):
                            print(f"[ROOT切换] 认证失败信息: {decoded}")
                except TimeoutError:
                    continue
        except Exception as e:
            print(f"[ROOT切换] _wait_for_root_prompt异常: {str(e)}")
            return False

    async def _execute_via_pty(self, command: str, timeout: int = 30) -> str:
        """通过PTY会话执行命令"""
        try:
            # 发送命令（PTY中使用\r）
            self.shell.stdin.write(command + "\r")
            await self.shell.stdin.drain()

            # 等待命令执行完成
            output = ""
            end_time = datetime.now().timestamp() + timeout

            while datetime.now().timestamp() < end_time:
                try:
                    data = await asyncio.wait_for(
                        self.shell.stdout.read(4096), timeout=1
                    )
                    if data:
                        output += (
                            data.decode("utf-8", errors="replace")
                            if isinstance(data, bytes)
                            else data
                        )
                        if "#" in output:
                            break
                except TimeoutError:
                    continue

            # 清理输出：移除命令本身和提示符
            if command + "\r\n" in output:
                output = output.replace(command + "\r\n", "", 1)
            elif command + "\r" in output:
                output = output.replace(command + "\r", "", 1)
            elif command + "\n" in output:
                output = output.replace(command + "\n", "", 1)
            if "#" in output:
                output = output.rsplit("#", 1)[0].strip()

            # 清理ANSI转义序列（颜色代码等）
            output = clean_ansi_escape(output)

            return output.strip()
        except Exception as e:
            return f"执行错误: {str(e)}"

    async def execute_interactive_sequence(
        self, commands: list[tuple[str, str]], timeout: int = 30
    ) -> list[str]:
        """
        执行交互式命令序列

        Args:
            commands: 命令列表，每个元素是 (命令, 期望的提示符/结束标记)
                      例如: [('parted', '(parted)'), ('print', '(parted)'), ('quit', '#')]
            timeout: 总超时时间

        Returns:
            每个命令的输出结果列表
        """
        results = []
        datetime.now().timestamp() + timeout

        try:
            for cmd, expected_prompt in commands:
                # 发送命令
                self.shell.stdin.write(cmd + "\r")
                await self.shell.stdin.drain()

                # 等待预期的提示符
                output = ""
                cmd_end_time = datetime.now().timestamp() + timeout

                while datetime.now().timestamp() < cmd_end_time:
                    try:
                        data = await asyncio.wait_for(
                            self.shell.stdout.read(4096), timeout=1
                        )
                        if data:
                            output += (
                                data.decode("utf-8", errors="replace")
                                if isinstance(data, bytes)
                                else data
                            )
                            # 检查是否出现预期的提示符
                            if expected_prompt in output:
                                break
                    except TimeoutError:
                        continue

                # 清理输出：移除命令本身和提示符
                if cmd + "\r\n" in output:
                    output = output.replace(cmd + "\r\n", "", 1)
                elif cmd + "\r" in output:
                    output = output.replace(cmd + "\r", "", 1)
                elif cmd + "\n" in output:
                    output = output.replace(cmd + "\n", "", 1)

                # 移除末尾的提示符
                if expected_prompt in output:
                    output = output.rsplit(expected_prompt, 1)[0].strip()

                # 清理ANSI转义序列
                output = clean_ansi_escape(output)

                results.append(output.strip())

            return results
        except Exception as e:
            results.append(f"执行错误: {str(e)}")
            return results

    async def close(self):
        """关闭连接"""
        # 关闭shell会话
        if self.shell:
            try:
                self.shell.stdin.close()
            except Exception:
                pass
            self.shell = None

        if self.connection:
            try:
                await self.connection.close()
            except Exception:
                pass
            self.connection = None

        self.is_root = False

    async def disconnect(self):
        """断开连接（别名方法）"""
        await self.close()

    async def execute(
        self, command: str, sudo: bool = False, timeout: int = 30
    ) -> SSHResult:
        r"""执行命令（通过bash shell执行，支持管道、重定向等shell特性）

        支持交互式命令序列，格式如下：
        - 普通命令: "ls /root"
        - 交互式命令序列: 使用 "|" 分隔命令，使用 ">" 分隔命令和提示符
          例如: "parted>(parted)|print>(parted)|quit>#"
          表示：执行parted，等待(parted)提示符；执行print，等待(parted)提示符；执行quit，等待#提示符
          带空格命令示例: "fdisk /dev/sda>Command\(\)|p>Command\(\)|q>#"
        """
        result = SSHResult(host=self.host_config.host)
        start_time = datetime.now()

        if not self.connection:
            result.success = False
            result.error = "未连接"
            return result

        try:
            result.start_time = start_time

            # 检查连接是否仍然有效
            if not hasattr(self.connection, "run"):
                result.success = False
                result.error = "连接对象无效"
                result.duration = (datetime.now() - start_time).total_seconds()
                return result

            # 检查是否为交互式命令序列（包含 ">" 分隔命令和提示符）
            if ">" in command and self.shell:
                # 解析交互式命令序列
                # 格式: cmd1>prompt1|cmd2>prompt2|cmd3>prompt3
                # 使用 | 分隔命令，> 分隔命令和提示符
                commands = []
                cmd_prompt_pairs = command.split("|")
                for pair in cmd_prompt_pairs:
                    pair = pair.strip()
                    if ">" in pair:
                        cmd, prompt = pair.split(">", 1)
                        commands.append((cmd.strip(), prompt.strip()))

                if commands:
                    results = await self.execute_interactive_sequence(commands, timeout)
                    result.stdout = "\n\n".join(results)
                    result.success = True
                    result.exit_code = 0
                    result.duration = (datetime.now() - start_time).total_seconds()
                    return result

            # 如果已建立PTY会话，通过PTY发送命令（所有用户都支持）
            if self.shell:
                output = await self._execute_via_pty(command, timeout)
                result.stdout = output
                result.success = True
                result.exit_code = 0
                result.duration = (datetime.now() - start_time).total_seconds()
                return result

            # 通过bash shell执行命令，支持管道、重定向等特性
            # 正确转义单引号：将 ' 替换为 '\''
            escaped_command = command.replace("'", "'\\''")
            shell_command = f"/bin/bash -c '{escaped_command}'"

            # 如果配置了sudo但未建立PTY会话，使用sudo执行
            if sudo and self.host_config.sudo_enabled:
                sudo_pass = self.host_config.sudo_password or self.host_config.password
                if sudo_pass:
                    shell_command = f"echo '{sudo_pass}' | sudo -S {shell_command}"
                else:
                    shell_command = f"sudo -S {shell_command}"

            # 使用 asyncssh 的 run 方法执行命令
            try:
                process = await asyncio.wait_for(
                    self.connection.run(shell_command, check=False), timeout=timeout
                )
            except TimeoutError:
                result.success = False
                result.error = "执行超时"
                result.duration = (datetime.now() - start_time).total_seconds()
                return result

            # 检查 process 是否有效
            if process is None:
                result.success = False
                result.error = "执行命令返回 None"
                result.duration = (datetime.now() - start_time).total_seconds()
                return result

            # 处理 SSHCompletedProcess 对象
            if hasattr(process, "exit_status"):
                actual_exit_code = process.exit_status
            elif hasattr(process, "returncode"):
                actual_exit_code = process.returncode
            else:
                actual_exit_code = 0

            result.exit_code = actual_exit_code

            # 获取输出
            stdout_data = getattr(process, "stdout", b"")
            stderr_data = getattr(process, "stderr", b"")

            if isinstance(stdout_data, bytes):
                result.stdout = stdout_data.decode("utf-8", errors="replace")
            else:
                result.stdout = str(stdout_data)
            # 清理ANSI转义序列
            result.stdout = clean_ansi_escape(result.stdout)

            if isinstance(stderr_data, bytes):
                result.stderr = stderr_data.decode("utf-8", errors="replace")
            else:
                result.stderr = str(stderr_data)
            # 清理ANSI转义序列
            result.stderr = clean_ansi_escape(result.stderr)

            result.success = actual_exit_code == 0
            if not result.success:
                # 如果有stderr输出，作为错误信息
                if result.stderr.strip():
                    result.error = result.stderr.strip()
                else:
                    result.error = f"命令执行失败，退出码: {actual_exit_code}"

        except Exception as e:
            result.success = False
            result.error = f"执行异常: {type(e).__name__}: {str(e)}"

        result.duration = (datetime.now() - start_time).total_seconds()
        return result

    async def upload_file(
        self, local_path: str, remote_path: str, progress_callback=None
    ) -> SFTPResult:
        """上传文件"""
        result = SFTPResult(
            host=self.host_config.host, filename=os.path.basename(local_path)
        )

        print(f"upload_file called for {self.host_config.host}")
        print(f"Connection status: {self.connection}")

        if not self.connection:
            result.success = False
            result.error = "未连接"
            print("Error: 连接对象为空")
            return result

        try:
            # 检查本地文件是否存在
            if not os.path.exists(local_path):
                result.success = False
                result.error = f"本地文件不存在: {local_path}"
                return result

            file_size = os.path.getsize(local_path)
            result.size = file_size

            # 处理远程路径
            if remote_path.endswith("/") or not os.path.basename(remote_path):
                remote_path = os.path.join(remote_path, os.path.basename(local_path))

            # 定义进度回调函数
            def _progress_handler(src, dst, bytes_transferred, bytes_total):
                result.transferred = bytes_transferred
                if progress_callback:
                    progress_callback(
                        self.host_config.host, bytes_transferred, bytes_total
                    )

            # 创建 SFTP 连接（使用上下文管理器自动关闭）
            try:
                async with self.connection.start_sftp_client() as sftp:
                    await sftp.put(
                        local_path, remote_path, progress_handler=_progress_handler
                    )
                    result.success = True
            except Exception as sftp_err:
                result.success = False
                result.error = (
                    f"无法创建SFTP连接: {type(sftp_err).__name__}: {str(sftp_err)}"
                )
                return result

        except Exception as e:
            result.success = False
            result.error = f"上传失败: {type(e).__name__}: {str(e)}"

        return result

    async def upload_directory(
        self, local_dir: str, remote_dir: str, progress_callback=None
    ) -> list[SFTPResult]:
        """上传整个目录到远程"""
        results = []

        if not self.connection:
            return [
                SFTPResult(host=self.host_config.host, success=False, error="未连接")
            ]

        # 获取目录中的所有文件
        files_to_upload = []
        for root, dirs, files in os.walk(local_dir):
            for filename in files:
                local_path = os.path.join(root, filename)
                # 计算相对路径
                rel_path = os.path.relpath(local_path, local_dir)
                # 计算远程路径
                remote_path = os.path.join(remote_dir, rel_path).replace(os.sep, "/")
                file_size = os.path.getsize(local_path)
                files_to_upload.append(
                    {
                        "local_path": local_path,
                        "remote_path": remote_path,
                        "size": file_size,
                        "filename": rel_path,
                    }
                )

        if not files_to_upload:
            return [
                SFTPResult(
                    host=self.host_config.host, success=True, filename=local_dir, size=0
                )
            ]

        # 创建 SFTP 连接并上传所有文件
        try:
            async with self.connection.start_sftp_client() as sftp:
                # 确保远程目录存在
                await sftp.makedirs(remote_dir, exist_ok=True)

                total_size = sum(f["size"] for f in files_to_upload)
                transferred = 0

                for file_info in files_to_upload:
                    try:
                        # 定义进度回调
                        def make_progress_handler(total_transferred):
                            def _handler(src, dst, bytes_trans, bytes_tot):
                                nonlocal total_transferred
                                total_transferred = bytes_trans
                                adjusted = total_transferred + transferred
                                if progress_callback:
                                    progress_callback(
                                        self.host_config.host, adjusted, total_size
                                    )

                            return _handler

                        # 上传文件
                        await sftp.put(
                            file_info["local_path"],
                            file_info["remote_path"],
                            progress_handler=make_progress_handler(transferred),
                        )

                        result = SFTPResult(
                            host=self.host_config.host,
                            success=True,
                            filename=file_info["filename"],
                            size=file_info["size"],
                            transferred=file_info["size"],
                        )
                        results.append(result)
                        transferred += file_info["size"]

                        if progress_callback:
                            progress_callback(
                                self.host_config.host, transferred, total_size
                            )

                    except Exception as e:
                        results.append(
                            SFTPResult(
                                host=self.host_config.host,
                                success=False,
                                filename=file_info["filename"],
                                error=f"{type(e).__name__}: {str(e)}",
                            )
                        )
        except Exception as e:
            results.append(
                SFTPResult(
                    host=self.host_config.host,
                    success=False,
                    error=f"无法创建SFTP连接: {type(e).__name__}: {str(e)}",
                )
            )

        return results

    async def download_file(
        self, remote_path: str, local_path: str, progress_callback=None
    ) -> SFTPResult:
        """下载文件"""
        result = SFTPResult(
            host=self.host_config.host, filename=os.path.basename(remote_path)
        )

        if not self.connection:
            result.success = False
            result.error = "未连接"
            return result

        try:
            # 创建 SFTP 连接（使用上下文管理器自动关闭）
            try:
                async with self.connection.start_sftp_client() as sftp:
                    file_info = await sftp.stat(remote_path)
                    file_size = file_info.size
                    result.size = file_size

                    def _progress_handler(src, dst, bytes_transferred, bytes_total):
                        result.transferred = bytes_transferred
                        if progress_callback:
                            progress_callback(
                                self.host_config.host, bytes_transferred, bytes_total
                            )

                    await sftp.get(
                        remote_path, local_path, progress_handler=_progress_handler
                    )
                    result.success = True
            except Exception as sftp_err:
                result.success = False
                result.error = (
                    f"无法创建SFTP连接: {type(sftp_err).__name__}: {str(sftp_err)}"
                )
                return result

        except Exception as e:
            result.success = False
            result.error = str(e)

        return result


class SSHPool:
    """SSH连接池"""

    def __init__(self, concurrency: int = 10):
        self.connections: dict[str, SSHConnection] = {}
        self.concurrency = concurrency

    async def connect_all(
        self, host_configs: list, timeout: int = 30, progress_callback=None
    ) -> list[tuple[str, bool, str]]:
        """批量连接所有主机"""
        results = []

        async def connect_with_limit(semaphore, host_config):
            async with semaphore:
                conn = SSHConnection(host_config)
                success, msg = await conn.connect(timeout)
                if success:
                    key = f"{host_config.host}:{host_config.port}"
                    self.connections[key] = conn
                if progress_callback:
                    progress_callback(host_config.host, success, msg)
                return (host_config.host, success, msg)

        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [connect_with_limit(semaphore, hc) for hc in host_configs]
        results = await asyncio.gather(*tasks)

        return results

    async def execute_all(
        self,
        command: str,
        sudo: bool = False,
        timeout: int = 30,
        progress_callback=None,
        target_hosts=None,
    ) -> list[SSHResult]:
        """在已连接主机上执行命令（支持指定目标主机）"""
        results = []

        # 解析多行命令
        commands = []
        for line in command.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)

        # 如果指定了目标主机，只在这些主机上执行
        if target_hosts:
            target_keys = set(target_hosts)
            target_keys = target_keys.intersection(set(self.connections.keys()))
        else:
            target_keys = self.connections.keys()

        async def execute_with_limit(semaphore, key):
            async with semaphore:
                conn = self.connections.get(key)
                if not conn:
                    return SSHResult(host=key, success=False, error="未连接")

                # 检查连接是否有效
                if not conn.connection or not hasattr(conn.connection, "run"):
                    return SSHResult(host=key, success=False, error="连接已断开或无效")

                # 将多行命令合并为一个命令执行，保持上下文（如cd命令的效果）
                if commands:
                    # 使用 && 连接所有命令，确保它们在同一个shell进程中执行
                    combined_command = " && ".join(commands)
                    result = await conn.execute(combined_command, sudo, timeout)
                    result.command = combined_command
                    return result
                return SSHResult(host=key, success=False, error="无命令")

        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [execute_with_limit(semaphore, key) for key in target_keys]
        results = await asyncio.gather(*tasks)

        return results

    async def upload_all(
        self,
        local_path: str,
        remote_path: str,
        progress_callback=None,
        target_hosts=None,
    ) -> list[SFTPResult]:
        """批量上传文件"""
        results = []

        async def upload_with_limit(semaphore, key):
            async with semaphore:
                conn = self.connections.get(key)
                if conn:
                    result = await conn.upload_file(
                        local_path, remote_path, progress_callback
                    )
                    print(
                        f"上传结果 - {key}: success={result.success}, error={result.error}"
                    )
                    return result
                return SFTPResult(host=key, success=False, error="未连接")

        print(f"开始上传文件: local_path={local_path}, remote_path={remote_path}")
        print(f"可用连接数: {len(self.connections)}")
        print(f"连接keys: {list(self.connections.keys())}")
        print(f"目标主机: {target_hosts}")

        # 确定要传输的主机列表
        if target_hosts:
            keys_to_upload = [k for k in target_hosts if k in self.connections]
        else:
            keys_to_upload = list(self.connections.keys())

        print(f"实际传输主机数: {len(keys_to_upload)}")

        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [upload_with_limit(semaphore, key) for key in keys_to_upload]
        results = await asyncio.gather(*tasks)

        print(f"上传完成，结果数: {len(results)}")
        return results

    async def upload_directory_all(
        self, local_dir: str, remote_dir: str, progress_callback=None, target_hosts=None
    ) -> list[SFTPResult]:
        """批量上传目录到所有主机"""
        results = []

        async def upload_dir_with_limit(semaphore, key):
            async with semaphore:
                conn = self.connections.get(key)
                if conn:
                    dir_results = await conn.upload_directory(
                        local_dir, remote_dir, progress_callback
                    )
                    return dir_results
                return [SFTPResult(host=key, success=False, error="未连接")]

        # 确定要传输的主机列表
        if target_hosts:
            keys_to_upload = [k for k in target_hosts if k in self.connections]
        else:
            keys_to_upload = list(self.connections.keys())

        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [upload_dir_with_limit(semaphore, key) for key in keys_to_upload]
        task_results = await asyncio.gather(*tasks)

        # 展平结果
        for dir_results in task_results:
            results.extend(dir_results)

        return results

    async def download_all(
        self,
        remote_path: str,
        local_path: str,
        progress_callback=None,
        target_hosts=None,
    ) -> list[SFTPResult]:
        """批量下载文件（多节点下载同名文件时自动添加节点标识防止覆盖）"""
        results = []

        # 获取要下载的目标主机列表
        if target_hosts:
            keys_to_download = [k for k in target_hosts if k in self.connections]
        else:
            keys_to_download = list(self.connections.keys())

        # 判断是否为多节点下载（需要添加节点标识防止文件名冲突）
        multi_node_download = len(keys_to_download) > 1

        async def download_with_limit(semaphore, key, unique_local_path):
            async with semaphore:
                conn = self.connections.get(key)
                if conn:
                    result = await conn.download_file(
                        remote_path, unique_local_path, progress_callback
                    )
                    return result
                return SFTPResult(host=key, success=False, error="未连接")

        # 为每个节点生成唯一的本地路径
        download_tasks = []
        for key in keys_to_download:
            if multi_node_download:
                # 提取节点标识（IP或名称）
                node_id = key.split(":")[0]  # 获取IP部分
                # 获取远程文件名
                remote_basename = os.path.basename(remote_path.replace("/", os.sep))

                # 判断本地路径是否为目录：检查路径是否存在且是目录，或者路径以分隔符结尾
                local_is_dir = False
                if os.path.exists(local_path):
                    local_is_dir = os.path.isdir(local_path)
                else:
                    # 如果路径不存在，检查是否以目录分隔符结尾
                    local_is_dir = local_path.endswith("/") or local_path.endswith("\\")

                if local_is_dir:
                    # 本地路径是目录：目录 + 带节点标识的文件名
                    name, ext = os.path.splitext(remote_basename)
                    unique_local_path = os.path.join(
                        local_path, f"{name}_{node_id}{ext}"
                    )
                else:
                    # 本地路径是文件：提取目录，生成带节点标识的文件名
                    dirname = os.path.dirname(local_path)
                    name, ext = os.path.splitext(remote_basename)
                    unique_local_path = os.path.join(dirname, f"{name}_{node_id}{ext}")
            else:
                unique_local_path = local_path

            semaphore = asyncio.Semaphore(self.concurrency)
            download_tasks.append(
                download_with_limit(semaphore, key, unique_local_path)
            )

        results = await asyncio.gather(*download_tasks)

        return results

    async def close_all(self):
        """关闭所有连接"""
        tasks = [conn.close() for conn in self.connections.values()]
        await asyncio.gather(*tasks)
        self.connections.clear()

    def get_connected_count(self) -> int:
        """获取已连接数量"""
        return len(self.connections)
