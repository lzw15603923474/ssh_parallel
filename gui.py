"""
SSH 批量执行工具 - GUI界面模块
"""

import asyncio
import csv
import json
import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from config import ConfigManager, HostConfig
from host_manager import HostManager
from ssh_core import SSHConnection, SSHPool


class ConfirmDialog:
    """确认对话框"""

    def __init__(self, parent, title, message, show_dont_ask=True):
        self.parent = parent
        self.title = title
        self.message = message
        self.show_dont_ask = show_dont_ask
        self.result = False
        self.dont_ask = False

        self._session_dont_ask = {}

    def show(self, key="default"):
        """显示对话框"""
        # 检查会话级别是否已禁用
        if self._session_dont_ask.get(key, False):
            return True

        dialog = tk.Toplevel(self.parent)
        dialog.title(self.title)
        dialog.geometry("500x300")
        dialog.resizable(True, True)
        dialog.transient(self.parent)
        dialog.grab_set()

        dialog.bind("<Return>", lambda e: self._on_confirm(dialog))
        dialog.bind("<Escape>", lambda e: self._on_cancel(dialog))

        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text=self.message, wraplength=400).pack(pady=(0, 15))

        if self.show_dont_ask:
            self.dont_ask_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                main_frame, text="本次打开后不再提醒", variable=self.dont_ask_var
            ).pack(anchor=tk.W)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(15, 0))

        ttk.Button(
            btn_frame, text="取消", command=lambda: self._on_cancel(dialog)
        ).pack(side=tk.RIGHT, padx=(10, 0))
        ttk.Button(
            btn_frame, text="确认", command=lambda: self._on_confirm(dialog)
        ).pack(side=tk.RIGHT)

        dialog.wait_window()

        if self.dont_ask:
            self._session_dont_ask[key] = True

        return self.result

    def _on_confirm(self, dialog):
        self.result = True
        if self.show_dont_ask:
            self.dont_ask = self.dont_ask_var.get()
        dialog.destroy()

    def _on_cancel(self, dialog):
        self.result = False
        dialog.destroy()


class HostTree(ttk.Treeview):
    """主机列表树"""

    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, show="headings", **kwargs)
        self.parent = parent

        self["columns"] = ("name", "host", "port", "username", "group", "status")
        self.heading("name", text="名称")
        self.heading("host", text="主机")
        self.heading("port", text="端口")
        self.heading("username", text="用户名")
        self.heading("group", text="分组")
        self.heading("status", text="状态")

        self.column("name", width=100)
        self.column("host", width=120)
        self.column("port", width=60)
        self.column("username", width=80)
        self.column("group", width=80)
        self.column("status", width=80)

        self.bind("<ButtonRelease-1>", self._on_click)
        self.bind("<Shift-ButtonRelease-1>", self._on_shift_click)
        self.bind("<Control-ButtonRelease-1>", self._on_ctrl_click)

        self._last_click_item = None
        self._shift_selecting = False

    def _on_click(self, event):
        """处理单击事件"""
        item = self.identify_row(event.y)
        if item:
            self._last_click_item = item

    def _on_shift_click(self, event):
        """处理Shift+单击事件"""
        item = self.identify_row(event.y)
        if item and self._last_click_item:
            self._shift_selecting = True
            # 获取两个item之间的所有item
            items = self.get_children("")
            start_idx = None
            end_idx = None

            for i, it in enumerate(items):
                if it == self._last_click_item:
                    start_idx = i
                if it == item:
                    end_idx = i

            if start_idx is not None and end_idx is not None:
                first = min(start_idx, end_idx)
                last = max(start_idx, end_idx)

                # 清除当前选择
                for selected in self.selection():
                    self.selection_remove(selected)

                # 选择范围内的所有item
                for i in range(first, last + 1):
                    self.selection_add(items[i])

    def _on_ctrl_click(self, event):
        """处理Ctrl+单击事件"""
        item = self.identify_row(event.y)
        if item:
            if item in self.selection():
                self.selection_remove(item)
            else:
                self.selection_add(item)

    def get_selected_host_keys(self):
        """获取选中主机的key列表"""
        keys = []
        for item in self.selection():
            values = self.item(item)["values"]
            if len(values) >= 3:
                host = values[1]
                port = values[2]
                keys.append(f"{host}:{port}")
        return keys


class App:
    """主应用类"""

    def __init__(self, root):
        self.root = root
        self.root.title("SSH批量执行工具")
        self.root.geometry("1400x900")
        self.root.minsize(1000, 700)

        # 配置管理
        self.config_manager = ConfigManager()
        self.config = self.config_manager.load()
        self.host_manager = HostManager(self.config)

        # SSH连接池
        self.pool = SSHPool(concurrency=self.config.connection.concurrency)

        # 确认对话框
        self.confirm_dialog = ConfirmDialog(root, "确认操作", "")

        # 状态跟踪
        self.running_tasks = 0
        self.task_cancelled = False

        # 构建界面
        self._build_ui()

        # 加载主机列表
        self._update_host_tree()

        # 设置关闭回调
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        """构建主界面"""
        # 主标签页
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 主机管理页面
        self._build_host_tab()

        # 命令执行页面
        self._build_command_tab()

        # 文件传输页面
        self._build_sftp_tab()

        # 帮助页面
        self._build_help_tab()

        # 状态栏
        self._build_status_bar()

    def _build_host_tab(self):
        """构建主机管理页面"""
        host_frame = ttk.Frame(self.notebook)
        self.notebook.add(host_frame, text="主机管理")

        paned = ttk.PanedWindow(host_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：主机列表
        left_frame = ttk.Frame(paned, width=600)
        paned.add(left_frame, weight=2)

        # 分组选择
        group_frame = ttk.Frame(left_frame)
        group_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(group_frame, text="分组:").pack(side=tk.LEFT)

        self.group_var = tk.StringVar(value="全部")
        self.group_combo = ttk.Combobox(
            group_frame, textvariable=self.group_var, width=20
        )
        self._update_group_combo()
        self.group_combo.pack(side=tk.LEFT, padx=5)
        self.group_combo.bind("<<ComboboxSelected>>", self._on_group_change)

        ttk.Button(group_frame, text="管理分组", command=self._manage_groups).pack(
            side=tk.RIGHT
        )

        # 主机列表树
        tree_frame = ttk.Frame(left_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        self.host_tree = HostTree(tree_frame)
        self.host_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.host_tree.yview
        )
        self.host_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 绑定主机树选择事件，用于更新连接按钮状态
        self.host_tree.bind("<<TreeviewSelect>>", self._on_host_tree_select)

        # 主机操作按钮
        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(btn_frame, text="添加主机", command=self._add_host).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(btn_frame, text="编辑主机", command=self._edit_host).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(btn_frame, text="删除主机", command=self._delete_host).pack(
            side=tk.LEFT, padx=2
        )
        self.connect_single_btn = ttk.Button(
            btn_frame, text="连接主机", command=self._toggle_single_connection
        )
        self.connect_single_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="全部连接", command=self._connect_all).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(btn_frame, text="断开全部连接", command=self._disconnect_all).pack(
            side=tk.LEFT, padx=2
        )

        # 导入导出按钮
        io_frame = ttk.Frame(left_frame)
        io_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(io_frame, text="导入主机", command=self._import_hosts).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(io_frame, text="导出主机", command=self._export_hosts).pack(
            side=tk.LEFT, padx=2
        )

        # 右侧：配置设置和连接日志
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=1)

        # 连接日志区域
        log_frame = ttk.LabelFrame(right_frame, text="连接日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.connection_log_text = scrolledtext.ScrolledText(
            log_frame, height=15, wrap=tk.WORD
        )
        self.connection_log_text.pack(fill=tk.BOTH, expand=True)

        self.connection_log_text.tag_configure("info", foreground="#2196F3")
        self.connection_log_text.tag_configure("success", foreground="#4CAF50")
        self.connection_log_text.tag_configure("error", foreground="#F44336")
        self.connection_log_text.tag_configure("warning", foreground="#FF9800")

        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill=tk.X, pady=(5, 0))

        ttk.Button(
            log_btn_frame, text="清空日志", command=self._clear_connection_log
        ).pack(side=tk.LEFT)

        # 连接配置
        config_frame = ttk.LabelFrame(right_frame, text="连接配置", padding=10)
        config_frame.pack(fill=tk.BOTH, expand=True)

        row = 0
        ttk.Label(config_frame, text="超时时间(秒):").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.timeout_spin = ttk.Spinbox(config_frame, from_=5, to=300, width=10)
        self.timeout_spin.set(self.config.connection.timeout)
        self.timeout_spin.grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(config_frame, text="重试次数:").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.retries_spin = ttk.Spinbox(config_frame, from_=0, to=10, width=10)
        self.retries_spin.set(self.config.connection.max_retries)
        self.retries_spin.grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(config_frame, text="并发数量:").grid(
            row=row, column=0, sticky=tk.W, pady=5
        )
        self.concurrency_spin = ttk.Spinbox(config_frame, from_=1, to=50, width=10)
        self.concurrency_spin.set(self.config.connection.concurrency)
        self.concurrency_spin.grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Button(
            config_frame, text="保存配置", command=self._save_connection_config
        ).grid(row=row, column=0, columnspan=2, pady=10)

    def _build_command_tab(self):
        """构建命令执行页面"""
        cmd_frame = ttk.Frame(self.notebook)
        self.notebook.add(cmd_frame, text="命令执行")

        paned = ttk.PanedWindow(cmd_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：命令输入和节点选择
        left_frame = ttk.Frame(paned, width=600)
        paned.add(left_frame, weight=1)

        # 节点选择区域
        host_select_frame = ttk.LabelFrame(left_frame, text="选择执行节点", padding=10)
        host_select_frame.pack(fill=tk.BOTH, expand=True)

        # 全选/取消全选按钮
        select_btn_frame = ttk.Frame(host_select_frame)
        select_btn_frame.pack(fill=tk.X)
        ttk.Button(select_btn_frame, text="全选", command=self._select_all_hosts).pack(
            side=tk.LEFT
        )
        ttk.Button(
            select_btn_frame, text="取消全选", command=self._deselect_all_hosts
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(select_btn_frame, text="反选", command=self._toggle_hosts).pack(
            side=tk.LEFT, padx=5
        )

        # 节点列表
        host_list_frame = ttk.Frame(host_select_frame)
        host_list_frame.pack(fill=tk.BOTH, expand=True)

        self.host_listbox = tk.Listbox(
            host_list_frame, selectmode=tk.MULTIPLE, height=4
        )
        self.host_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        host_scrollbar = ttk.Scrollbar(
            host_list_frame, orient=tk.VERTICAL, command=self.host_listbox.yview
        )
        self.host_listbox.configure(yscrollcommand=host_scrollbar.set)
        host_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 命令输入区域
        cmd_input_frame = ttk.LabelFrame(left_frame, text="命令输入", padding=10)
        cmd_input_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # 执行模式
        mode_frame = ttk.Frame(cmd_input_frame)
        mode_frame.pack(fill=tk.X)

        ttk.Label(mode_frame, text="执行模式:").pack(side=tk.LEFT)

        self.exec_mode_var = tk.StringVar(value="parallel")
        ttk.Radiobutton(
            mode_frame, text="并行执行", variable=self.exec_mode_var, value="parallel"
        ).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(
            mode_frame, text="串行执行", variable=self.exec_mode_var, value="serial"
        ).pack(side=tk.LEFT, padx=5)

        # 命令输入框
        ttk.Label(cmd_input_frame, text="命令:").pack(anchor=tk.W)
        self.cmd_text = scrolledtext.ScrolledText(cmd_input_frame, height=6)
        self.cmd_text.pack(fill=tk.BOTH, expand=True, pady=5)

        # 选项
        options_frame = ttk.Frame(cmd_input_frame)
        options_frame.pack(fill=tk.X)

        self.sudo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="使用sudo", variable=self.sudo_var).pack(
            side=tk.LEFT, padx=5
        )

        ttk.Button(options_frame, text="执行命令", command=self._execute_command).pack(
            side=tk.RIGHT
        )

        # 执行结果表格
        result_frame = ttk.LabelFrame(left_frame, text="执行结果", padding=10)
        result_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        result_tree_frame = ttk.Frame(result_frame)
        result_tree_frame.pack(fill=tk.BOTH, expand=True)

        self.result_tree = ttk.Treeview(
            result_tree_frame,
            columns=("host", "status", "exit_code", "duration"),
            show="headings",
            height=8,
        )
        self.result_tree.heading("host", text="主机")
        self.result_tree.heading("status", text="状态")
        self.result_tree.heading("exit_code", text="退出码")
        self.result_tree.heading("duration", text="耗时(s)")

        self.result_tree.column("host", width=120)
        self.result_tree.column("status", width=80)
        self.result_tree.column("exit_code", width=80)
        self.result_tree.column("duration", width=80)

        result_scrollbar = ttk.Scrollbar(
            result_tree_frame, orient=tk.VERTICAL, command=self.result_tree.yview
        )
        self.result_tree.configure(yscrollcommand=result_scrollbar.set)

        self.result_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        result_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 操作按钮
        result_btn_frame = ttk.Frame(result_frame)
        result_btn_frame.pack(fill=tk.X)

        ttk.Button(
            result_btn_frame, text="导出结果", command=self._export_results
        ).pack(side=tk.RIGHT)
        ttk.Button(result_btn_frame, text="终止任务", command=self._cancel_tasks).pack(
            side=tk.RIGHT, padx=5
        )

        # 右侧：执行日志
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=1)

        log_frame = ttk.LabelFrame(right_frame, text="执行日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=25, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 日志标签配置
        self.log_text.tag_configure("info", foreground="#2196F3")
        self.log_text.tag_configure("success", foreground="#4CAF50")
        self.log_text.tag_configure("error", foreground="#F44336")
        self.log_text.tag_configure("warning", foreground="#FF9800")
        self.log_text.tag_configure("command", foreground="#9C27B0")
        self.log_text.tag_configure(
            "stdout", foreground="#333333"
        )  # 标准输出使用深灰色

        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill=tk.X)

        ttk.Button(log_btn_frame, text="清空日志", command=self._clear_log).pack(
            side=tk.LEFT
        )
        ttk.Button(log_btn_frame, text="复制日志", command=self._copy_log).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(log_btn_frame, text="保存日志", command=self._save_log).pack(
            side=tk.LEFT, padx=5
        )

    def _build_sftp_tab(self):
        """构建文件传输页面"""
        sftp_frame = ttk.Frame(self.notebook)
        self.notebook.add(sftp_frame, text="文件传输")

        paned = ttk.PanedWindow(sftp_frame, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：传输配置
        left_frame = ttk.Frame(paned, width=400)
        paned.add(left_frame, weight=1)

        config_frame = ttk.LabelFrame(left_frame, text="传输配置", padding=10)
        config_frame.pack(fill=tk.BOTH, expand=True)

        # 选择节点区域
        host_select_frame = ttk.LabelFrame(config_frame, text="选择传输节点")
        host_select_frame.pack(fill=tk.X, pady=(0, 10))

        host_btn_frame = ttk.Frame(host_select_frame)
        host_btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(
            host_btn_frame, text="全选", command=self._select_all_sftp_hosts
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            host_btn_frame, text="取消全选", command=self._deselect_all_sftp_hosts
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(host_btn_frame, text="反选", command=self._toggle_sftp_hosts).pack(
            side=tk.LEFT, padx=5
        )

        self.sftp_host_listbox = tk.Listbox(
            host_select_frame, selectmode=tk.MULTIPLE, height=3
        )
        self.sftp_host_listbox.pack(fill=tk.X)

        # 传输方向
        direction_frame = ttk.Frame(config_frame)
        direction_frame.pack(fill=tk.X)

        self.transfer_mode_var = tk.StringVar(value="upload")
        ttk.Radiobutton(
            direction_frame,
            text="上传",
            variable=self.transfer_mode_var,
            value="upload",
        ).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(
            direction_frame,
            text="下载",
            variable=self.transfer_mode_var,
            value="download",
        ).pack(side=tk.LEFT, padx=5)

        # 本地路径
        ttk.Label(config_frame, text="本地路径:").pack(anchor=tk.W)
        local_path_frame = ttk.Frame(config_frame)
        local_path_frame.pack(fill=tk.X)
        self.local_path_entry = ttk.Entry(local_path_frame)
        self.local_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(local_path_frame, text="文件", command=self._browse_local).pack(
            side=tk.RIGHT
        )
        ttk.Button(local_path_frame, text="目录", command=self._browse_local_dir).pack(
            side=tk.RIGHT
        )

        # 远程路径
        ttk.Label(config_frame, text="远程路径:").pack(anchor=tk.W)
        remote_path_frame = ttk.Frame(config_frame)
        remote_path_frame.pack(fill=tk.X)
        self.remote_path_entry = ttk.Entry(remote_path_frame)
        self.remote_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 传输按钮
        self.start_transfer_btn = ttk.Button(
            config_frame, text="开始传输", command=self._start_transfer
        )
        self.start_transfer_btn.pack(fill=tk.X, pady=10)
        ttk.Button(config_frame, text="终止传输", command=self._cancel_tasks).pack(
            fill=tk.X
        )

        # 传输结果
        result_frame = ttk.LabelFrame(left_frame, text="传输结果", padding=10)
        result_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.transfer_tree = ttk.Treeview(
            result_frame,
            columns=("host", "status", "file", "size"),
            show="headings",
            height=6,
        )
        self.transfer_tree.heading("host", text="主机")
        self.transfer_tree.heading("status", text="状态")
        self.transfer_tree.heading("file", text="文件")
        self.transfer_tree.heading("size", text="大小")
        self.transfer_tree.pack(fill=tk.BOTH, expand=True)

        # 右侧：传输日志
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=2)

        log_frame = ttk.LabelFrame(right_frame, text="传输日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.sftp_log_text = scrolledtext.ScrolledText(
            log_frame, height=25, wrap=tk.WORD
        )
        self.sftp_log_text.pack(fill=tk.BOTH, expand=True)

        # 进度条区域
        progress_frame = ttk.Frame(log_frame)
        progress_frame.pack(fill=tk.X, pady=(5, 0))

        # 整体进度条
        self.transfer_progress = ttk.Progressbar(
            progress_frame, mode="determinate", length=300
        )
        self.transfer_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 进度标签
        self.progress_label = ttk.Label(progress_frame, text="0%")
        self.progress_label.pack(side=tk.RIGHT, padx=5)

        # 清除回显按钮
        sftp_btn_frame = ttk.Frame(log_frame)
        sftp_btn_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(sftp_btn_frame, text="清除回显", command=self._clear_sftp_log).pack(
            side=tk.LEFT
        )

    def _clear_sftp_log(self):
        """清除文件传输日志"""
        self.sftp_log_text.delete(1.0, tk.END)
        self._sftp_log("日志已清空", "info")

    def _build_help_tab(self):
        """构建帮助页面"""
        help_frame = ttk.Frame(self.notebook)
        self.notebook.add(help_frame, text="帮助")

        # 创建滚动文本区域
        help_text = scrolledtext.ScrolledText(
            help_frame, wrap=tk.WORD, padx=10, pady=10
        )
        help_text.pack(fill=tk.BOTH, expand=True)

        # 设置标签样式
        help_text.tag_configure(
            "title", font=("微软雅黑", 14, "bold"), foreground="#1976D2"
        )
        help_text.tag_configure(
            "subtitle", font=("微软雅黑", 12, "bold"), foreground="#424242"
        )
        help_text.tag_configure("normal", font=("微软雅黑", 10), foreground="#616161")
        help_text.tag_configure(
            "code", font=("Consolas", 10), foreground="#7B1FA2", background="#F5F5F5"
        )
        help_text.tag_configure(
            "author", font=("微软雅黑", 11, "italic"), foreground="#757575"
        )

        # 帮助内容
        help_content = """

SSH 批量执行工具 - 使用帮助
════════════════════════════════════════════════════════════════════════
关于作者
────────────────────────────────────────────────────────────────────────

by 刘振威 (liuzhenwei)

版本：1.0.0

mail：liuzhenwei999@foxmail.com

如有问题或建议，请联系作者。

════════════════════════════════════════════════════════════════════════

一、功能介绍
────────────────────────────────────────────────────────────────────────

本工具是一款基于 Python 和 asyncssh 开发的 SSH 批量执行工具，支持 GUI 和 CLI 双模式，主要功能包括：

  ▶ 主机管理：添加、编辑、删除主机配置，支持分组管理，支持单个连接/断开
  ▶ 批量命令执行：同时在多台服务器上执行命令，支持交互式命令序列
  ▶ 文件传输：支持 SFTP 文件上传/下载，支持目录上传，自动避免文件名覆盖
  ▶ 用户切换：支持普通用户登录后切换到 root，或直接以 root 登录
  ▶ 导入导出：支持 Excel/CSV 格式的主机配置导入导出（含切换 root 配置）

二、操作指南
────────────────────────────────────────────────────────────────────────

2.1 主机管理

  添加主机：
  1. 点击「添加主机」按钮
  2. 填写主机名称、IP地址、端口、用户名、密码等信息
  3. 如需切换到 root 用户，勾选「切换到 root 用户」并填写 root 密码
  4. 选择分组（可选）
  5. 点击「确定」保存

  连接/断开主机：
  连接全部：点击「全部连接」按钮连接当前分组的所有主机
  单台连接：选中主机后点击「连接主机」按钮
  断开连接：选中已连接的主机后点击「断开链接」按钮
  连接成功的主机会显示绿色状态

  导入/导出主机：
  导入：支持从 Excel(.xlsx) 或 CSV 文件导入主机列表
  导出：支持导出为 Excel 或 JSON 格式，包含切换 root 用户配置

2.2 命令执行

  普通命令执行：
  1. 切换到「命令执行」页签
  2. 在节点列表中选择要执行命令的主机
  3. 在命令输入框中输入要执行的命令
  4. 如需提升权限，勾选「使用sudo」
  5. 选择执行模式：并行（同时执行）或串行（依次执行）
  6. 点击「执行命令」按钮
  7. 等待命令执行完成，查看执行结果

  交互式命令序列：
  格式：命令1>提示符1|命令2>提示符2|命令3>提示符3
  示例：parted>(parted)|print>(parted)|quit>#
  说明：使用 | 分隔命令，使用 > 分隔命令和期望的提示符

2.3 文件传输

  上传文件/目录：
  1. 切换到「文件传输」页签
  2. 选择「上传」模式
  3. 选择「文件」或「目录」按钮选择本地文件/目录
  4. 输入远程路径（只需目录，文件名自动使用源文件名）
  5. 选择目标主机
  6. 点击「开始传输」

  下载文件：
  1. 切换到「文件传输」页签
  2. 选择「下载」模式
  3. 输入远程文件路径
  4. 选择本地保存目录
  5. 选择目标主机
  6. 点击「开始传输」

三、主机配置说明
────────────────────────────────────────────────────────────────────────

| 字段 | 说明 | 必填 |
|------|------|------|
| 名称 | 主机显示名称 | 否（默认为主机地址） |
| 主机 | IP 地址或域名 | 是 |
| 端口 | SSH 端口 | 否（默认 22） |
| 用户名 | 登录用户名 | 是 |
| 密码 | 登录密码 | 是（除非使用密钥认证） |
| 密钥认证 | 使用密钥文件登录 | 否 |
| 切换到 root | 登录后自动切换到 root | 否 |
| root 密码 | root 用户密码 | 否（切换 root 时必填） |
| sudo 配置 | 启用 sudo 执行 | 否 |
| 分组 | 主机所属分组 | 否 |

四、Excel 导入导出格式
────────────────────────────────────────────────────────────────────────

支持的列名（中英文均可）：

| 中文列名 | 英文列名 | 说明 |
|----------|----------|------|
| 名称 | name | 主机显示名称 |
| 主机 | host | IP 地址或域名（必需） |
| 端口 | port | SSH 端口 |
| 用户名 | username | 登录用户名 |
| 密码 | password | 登录密码 |
| 切换root | switch_to_root | 是否切换到 root（是/否） |
| root密码 | root_password | root 用户密码 |
| 分组 | group | 主机所属分组 |

五、常见问题解答
────────────────────────────────────────────────────────────────────────

Q1: 为什么无法连接到主机？

  A: 请检查以下几点：
     1. 确保主机 IP 地址和端口正确
     2. 确保用户名和密码正确
     3. 确保目标主机的 SSH 服务已开启
     4. 确保网络连接正常，没有防火墙阻止

Q2: 切换到 root 用户失败？

  A: 请检查：
     1. root 密码是否正确
     2. 当前用户是否有权限执行 su - root
     3. /etc/pam.d/su 配置是否允许切换
     4. SSH 连接是否支持 PTY

Q3: 命令执行失败怎么办？

  A: 请检查：
     1. 命令语法是否正确
     2. 用户是否有执行该命令的权限
     3. 命令是否需要 sudo 权限
     4. 交互式命令格式是否正确

Q4: 文件上传失败？

  A: 可能的原因：
     1. 远程路径不存在或没有写入权限
     2. 本地文件被其他程序占用
     3. 网络连接中断

Q5: 多节点下载同名文件会覆盖吗？

  A: 不会。当从多个节点下载同名文件时，系统会自动在文件名后添加节点 IP 标识，
     例如：filename_192.168.1.10.txt

Q6: 交互式命令无法执行？

  A: 请确保：
     1. 使用正确的命令格式：命令>提示符|命令>提示符
     2. 提示符与实际程序的提示符完全匹配
     3. 已建立 PTY 会话（连接成功后自动建立）

Q7: 命令执行回显有乱码？

  A: 这是终端颜色代码导致的，系统已自动清理 ANSI 转义序列。
     如果仍有乱码，请检查终端编码设置。


"""

        # 插入帮助内容
        help_text.insert(tk.END, help_content)

        # 格式化标题
        help_text.tag_add("title", "1.0", "1.end")

        # 禁止编辑
        help_text.config(state=tk.DISABLED)

    def _build_status_bar(self):
        """构建状态栏"""
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_label = ttk.Label(
            status_frame, text="就绪 | 已连接: 0/0", relief=tk.SUNKEN, padding=5
        )
        self.status_label.pack(fill=tk.X, padx=5, pady=5)

    def _update_group_combo(self):
        """更新分组下拉框"""
        groups = ["全部"] + self.config.groups
        self.group_combo["values"] = groups

    def _on_group_change(self, event):
        """分组变更处理"""
        self._update_host_tree()

    def _update_host_tree(self):
        """更新主机列表树"""
        # 清除现有内容
        for item in self.host_tree.get_children():
            self.host_tree.delete(item)

        # 获取当前分组的主机
        group = self.group_var.get()
        if group == "全部":
            hosts = self.config.hosts
        else:
            hosts = [h for h in self.config.hosts if h.group == group]

        # 获取已连接状态
        connected_hosts = set(self.pool.connections.keys())

        for host in hosts:
            # 跳过空主机（主机地址为空的）
            if not host.host:
                continue

            key = f"{host.host}:{host.port}"
            status = "✓ 已连接" if key in connected_hosts else "○ 未连接"
            self.host_tree.insert(
                "",
                tk.END,
                values=(
                    host.name,
                    host.host,
                    host.port,
                    host.username,
                    host.group,
                    status,
                ),
            )

    def _connection_log(self, message, level="info"):
        """输出连接日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.connection_log_text.insert(tk.END, f"[{timestamp}] {message}\n", level)
        self.connection_log_text.see(tk.END)

    def _clear_connection_log(self):
        """清空连接日志"""
        self.connection_log_text.delete(1.0, tk.END)
        self._connection_log("日志已清空", "info")

    def _log(self, message, level="info"):
        """输出命令执行日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", level)
        self.log_text.see(tk.END)

    def _sftp_log(self, message, level="info"):
        """输出SFTP日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.sftp_log_text.insert(tk.END, f"[{timestamp}] {message}\n", level)
        self.sftp_log_text.see(tk.END)

    def _clear_log(self):
        """清空日志"""
        self.log_text.delete(1.0, tk.END)
        self._log("日志已清空", "info")

    def _copy_log(self):
        """复制日志"""
        content = self.log_text.get(1.0, tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("提示", "日志已复制到剪贴板")

    def _save_log(self):
        """保存日志"""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            title="保存日志",
        )
        if file_path:
            content = self.log_text.get(1.0, tk.END)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            messagebox.showinfo("提示", "日志已保存")

    def _manage_groups(self):
        """管理分组"""
        dialog = tk.Toplevel(self.root)
        dialog.title("分组管理")
        dialog.geometry("400x300")
        dialog.transient(self.root)
        dialog.grab_set()

        listbox = tk.Listbox(dialog)
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        for group in self.config.groups:
            listbox.insert(tk.END, group)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        def add_group():
            name = simpledialog.askstring("添加分组", "输入分组名称:")
            if name and self.host_manager.add_group(name):
                listbox.insert(tk.END, name)
                self._update_group_combo()

        def rename_group():
            selected = listbox.curselection()
            if selected:
                old_name = listbox.get(selected[0])
                new_name = simpledialog.askstring(
                    "重命名分组", "输入新名称:", initialvalue=old_name
                )
                if new_name and self.host_manager.rename_group(old_name, new_name):
                    listbox.delete(selected[0])
                    listbox.insert(selected[0], new_name)
                    self._update_group_combo()
                    self._update_host_tree()

        def delete_group():
            selected = listbox.curselection()
            if selected:
                name = listbox.get(selected[0])
                if name == "默认分组":
                    messagebox.showwarning("提示", "不能删除默认分组")
                    return
                if messagebox.askyesno("确认删除", f"确定要删除分组 '{name}' 吗?"):
                    if self.host_manager.delete_group(name):
                        listbox.delete(selected[0])
                        self._update_group_combo()
                        self._update_host_tree()

        ttk.Button(btn_frame, text="添加", command=add_group).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="重命名", command=rename_group).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="删除", command=delete_group).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

        dialog.wait_window()

    def _add_host(self):
        """添加主机"""
        self._show_host_dialog()

    def _edit_host(self):
        """编辑主机"""
        selected = self.host_tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请选择要编辑的主机")
            return

        item = self.host_tree.item(selected[0])
        values = item["values"]
        host_key = f"{values[1]}:{values[2]}"
        host = self.host_manager.get_host_by_key(host_key)

        if host:
            self._show_host_dialog(host, host_key)

    def _delete_host(self):
        """删除主机"""
        selected = self.host_tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请选择要删除的主机")
            return

        if not messagebox.askyesno("确认删除", "确定要删除选中的主机吗?"):
            return

        for item in selected:
            values = self.host_tree.item(item)["values"]
            host_key = f"{values[1]}:{values[2]}"
            self.host_manager.remove_host(host_key)

        self._update_host_tree()
        self.config_manager.save(self.config)
        self._log("主机已删除", "info")

    def _show_host_dialog(self, host=None, host_key=None):
        """显示主机编辑对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("添加主机" if not host else "编辑主机")
        dialog.geometry("450x520")
        dialog.transient(self.root)
        dialog.grab_set()

        # 默认值
        name = host.name if host else ""
        host_val = host.host if host else ""
        port = host.port if host else 22
        username = host.username if host else ""
        password = host.password if host else ""
        use_key = host.use_key if host else False
        key_file = host.key_file if host else ""
        key_passphrase = host.key_passphrase if host else ""
        sudo_enabled = host.sudo_enabled if host else False
        sudo_password = host.sudo_password if host else ""
        switch_to_root = host.switch_to_root if host else False
        root_password = host.root_password if host else ""
        group = host.group if host else "默认分组"

        # 变量
        name_var = tk.StringVar(value=name)
        host_var = tk.StringVar(value=host_val)
        port_var = tk.IntVar(value=port)
        username_var = tk.StringVar(value=username)
        password_var = tk.StringVar(value=password)
        use_key_var = tk.BooleanVar(value=use_key)
        key_file_var = tk.StringVar(value=key_file)
        key_passphrase_var = tk.StringVar(value=key_passphrase)
        sudo_var = tk.BooleanVar(value=sudo_enabled)
        sudo_password_var = tk.StringVar(value=sudo_password)
        switch_to_root_var = tk.BooleanVar(value=switch_to_root)
        root_password_var = tk.StringVar(value=root_password)
        group_var = tk.StringVar(value=group)

        row = 0
        ttk.Label(dialog, text="名称:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=name_var, width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Label(dialog, text="主机:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=host_var, width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Label(dialog, text="端口:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Spinbox(dialog, textvariable=port_var, from_=1, to=65535, width=10).grid(
            row=row, column=1, sticky=tk.W, pady=5
        )
        row += 1

        ttk.Label(dialog, text="用户名:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=username_var, width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Label(dialog, text="密码:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=password_var, show="*", width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Checkbutton(dialog, text="使用密钥认证", variable=use_key_var).grid(
            row=row, column=0, columnspan=2, padx=10, pady=5
        )
        row += 1

        key_file_frame = ttk.Frame(dialog)
        key_file_frame.grid(row=row, column=0, columnspan=2, padx=10, pady=5)
        ttk.Label(key_file_frame, text="密钥文件:").pack(side=tk.LEFT)
        ttk.Entry(key_file_frame, textvariable=key_file_var, width=25).pack(
            side=tk.LEFT
        )
        ttk.Button(
            key_file_frame,
            text="浏览",
            command=lambda: self._browse_key_file(key_file_var),
        ).pack(side=tk.LEFT)
        row += 1

        ttk.Label(dialog, text="密钥密码:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=key_passphrase_var, show="*", width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Checkbutton(dialog, text="启用sudo", variable=sudo_var).grid(
            row=row, column=0, columnspan=2, padx=10, pady=5
        )
        row += 1

        ttk.Label(dialog, text="sudo密码:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=sudo_password_var, show="*", width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        # 切换到root用户选项
        ttk.Separator(dialog, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=5
        )
        row += 1

        ttk.Checkbutton(
            dialog, text="切换至root用户", variable=switch_to_root_var
        ).grid(row=row, column=0, columnspan=2, padx=10, pady=5, sticky=tk.W)
        row += 1

        ttk.Label(dialog, text="root密码:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Entry(dialog, textvariable=root_password_var, show="*", width=30).grid(
            row=row, column=1, pady=5
        )
        row += 1

        ttk.Label(dialog, text="分组:").grid(
            row=row, column=0, sticky=tk.W, padx=10, pady=5
        )
        ttk.Combobox(
            dialog, textvariable=group_var, values=self.config.groups, width=27
        ).grid(row=row, column=1, pady=5)
        row += 1

        btn_frame = ttk.Frame(dialog)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=15)

        def on_save():
            new_host = HostConfig(
                name=name_var.get().strip(),
                host=host_var.get().strip(),
                port=port_var.get(),
                username=username_var.get().strip(),
                password=password_var.get(),
                use_key=use_key_var.get(),
                key_file=key_file_var.get().strip(),
                key_passphrase=key_passphrase_var.get(),
                sudo_enabled=sudo_var.get(),
                sudo_password=sudo_password_var.get(),
                switch_to_root=switch_to_root_var.get(),
                root_password=root_password_var.get(),
                group=group_var.get(),
            )

            if not new_host.host:
                messagebox.showwarning("提示", "请输入主机地址")
                return

            if host_key:
                self.host_manager.update_host(host_key, new_host)
            else:
                if not self.host_manager.add_host(new_host):
                    messagebox.showwarning("提示", "主机已存在")
                    return

            self.config_manager.save(self.config)
            self._update_host_tree()
            dialog.destroy()

        ttk.Button(btn_frame, text="保存", command=on_save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)

        dialog.wait_window()

    def _browse_key_file(self, var):
        """浏览密钥文件"""
        path = filedialog.askopenfilename(
            filetypes=[("密钥文件", "*.pem *.key"), ("所有文件", "*.*")]
        )
        if path:
            var.set(path)

    def _browse_local(self):
        """浏览本地文件"""
        path = filedialog.askopenfilename()
        if path:
            self.local_path_entry.delete(0, tk.END)
            self.local_path_entry.insert(0, path)

    def _browse_local_dir(self):
        """浏览本地目录"""
        path = filedialog.askdirectory()
        if path:
            self.local_path_entry.delete(0, tk.END)
            self.local_path_entry.insert(0, path)

    def _import_hosts(self):
        """导入主机"""
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("Excel文件", "*.xlsx"),
                ("CSV文件", "*.csv"),
                ("所有文件", "*.*"),
            ]
        )
        if not file_path:
            return

        try:
            if file_path.endswith(".xlsx"):
                count = self.host_manager.import_from_excel(file_path)
            elif file_path.endswith(".csv"):
                count = self.host_manager.import_from_csv(file_path)
            else:
                messagebox.showerror("错误", "不支持的文件格式")
                return

            self.config_manager.save(self.config)
            self._update_host_tree()
            messagebox.showinfo("成功", f"成功导入 {count} 台主机")
            self._log(f"从 {file_path} 导入 {count} 台主机", "success")
        except Exception as e:
            messagebox.showerror("错误", f"导入失败: {str(e)}")

    def _export_hosts(self):
        """导出主机"""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[
                ("Excel文件", "*.xlsx"),
                ("JSON文件", "*.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not file_path:
            return

        try:
            if file_path.endswith(".xlsx"):
                self.host_manager.export_to_excel(file_path)
            elif file_path.endswith(".json"):
                self.host_manager.export_to_json(file_path)
            else:
                messagebox.showerror("错误", "不支持的文件格式")
                return

            messagebox.showinfo("成功", "导出成功")
            self._log(f"导出主机列表到 {file_path}", "success")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")

    def _save_connection_config(self):
        """保存连接配置"""
        self.config.connection.timeout = int(self.timeout_spin.get())
        self.config.connection.max_retries = int(self.retries_spin.get())
        self.config.connection.concurrency = int(self.concurrency_spin.get())
        self.pool.concurrency = self.config.connection.concurrency

        self.config_manager.save(self.config)
        messagebox.showinfo("提示", "配置已保存")

    def _connect_all(self):
        """连接所有主机"""
        hosts = self.host_manager.get_hosts_by_group(self.group_var.get())
        if not hosts:
            messagebox.showinfo("提示", "没有主机需要连接")
            return

        self.confirm_dialog.title = "确认连接"
        self.confirm_dialog.message = f"确定要连接 {len(hosts)} 台主机吗?"

        if not self.confirm_dialog.show("connect"):
            return

        self._connection_log(f"正在连接 {len(hosts)} 台主机...", "info")

        # 使用共享事件循环执行连接
        if not hasattr(self, "_event_loop") or self._event_loop.is_closed():
            self._event_loop = asyncio.new_event_loop()
            threading.Thread(target=self._event_loop.run_forever, daemon=True).start()

        asyncio.run_coroutine_threadsafe(self._async_connect(hosts), self._event_loop)

    def _select_all_hosts(self):
        """全选所有节点"""
        self.host_listbox.selection_set(0, tk.END)

    def _toggle_single_connection(self):
        """连接/断开单个选中的主机"""
        selected_items = self.host_tree.selection()
        if not selected_items:
            messagebox.showwarning("提示", "请先从主机树中选择一个主机")
            return

        # 获取选中的第一个主机信息
        item = selected_items[0]
        values = self.host_tree.item(item)["values"]
        if len(values) < 4:
            messagebox.showwarning("提示", "无法获取主机信息")
            return

        host_name = values[0]
        host_ip = values[1]
        host_port = values[2]
        host_username = values[3]

        key = f"{host_ip}:{host_port}"
        is_connected = key in self.pool.connections

        if not is_connected:
            # 未连接状态：执行连接
            if messagebox.askokcancel(
                "连接确认", f"确定要连接到主机 {host_name} ({host_ip}:{host_port}) 吗？"
            ):
                self._connection_log(
                    f"正在连接到主机: {host_name} ({host_ip}:{host_port})", "info"
                )
                # 更新按钮状态
                self.connect_single_btn.config(text="连接中...", state=tk.DISABLED)
                # 确保事件循环存在
                if not hasattr(self, "_event_loop") or self._event_loop.is_closed():
                    self._event_loop = asyncio.new_event_loop()
                    threading.Thread(
                        target=self._event_loop.run_forever, daemon=True
                    ).start()
                # 执行连接
                asyncio.run_coroutine_threadsafe(
                    self._async_connect_single(host_ip, host_port, host_username),
                    self._event_loop,
                )
        else:
            # 已连接状态：执行断开
            if messagebox.askokcancel(
                "断开确认",
                f"确定要断开与主机 {host_name} ({host_ip}:{host_port}) 的连接吗？",
            ):
                self._connection_log(
                    f"正在断开与主机的连接: {host_name} ({host_ip}:{host_port})", "info"
                )
                # 更新按钮状态
                self.connect_single_btn.config(text="断开中...", state=tk.DISABLED)
                # 确保事件循环存在
                if not hasattr(self, "_event_loop") or self._event_loop.is_closed():
                    self._event_loop = asyncio.new_event_loop()
                    threading.Thread(
                        target=self._event_loop.run_forever, daemon=True
                    ).start()
                # 执行断开
                asyncio.run_coroutine_threadsafe(
                    self._async_disconnect_single(key), self._event_loop
                )

    async def _async_connect_single(self, host, port, username):
        """异步连接单个主机"""
        try:
            # 查找主机配置
            host_config = None
            for h in self.config.hosts:
                if h.host == host and h.port == port:
                    host_config = h
                    break

            if not host_config:
                raise ValueError(f"未找到主机配置: {host}:{port}")

            # 创建连接
            conn = SSHConnection(host_config)
            success, message = await conn.connect()

            if success:
                key = f"{host}:{port}"
                self.pool.connections[key] = conn
                self._connection_log(
                    f"成功连接到主机: {host_config.name} ({host}:{port})", "success"
                )
            else:
                self._connection_log(
                    f"连接主机失败: {host_config.name} ({host}:{port}) - {message}",
                    "error",
                )
        except Exception as e:
            self._connection_log(f"连接主机异常: {host}:{port} - {str(e)}", "error")
        finally:
            # 更新UI
            self._update_host_tree()
            self._update_host_list()
            self._update_single_connect_button()
            # 恢复按钮状态
            self.connect_single_btn.config(state=tk.NORMAL)

    async def _async_disconnect_single(self, key):
        """异步断开单个主机连接"""
        try:
            conn = self.pool.connections.get(key)
            if conn:
                await conn.disconnect()
                del self.pool.connections[key]
                key.split(":")[0]
                self._connection_log(f"已断开与主机的连接: {key}", "info")
        except Exception as e:
            self._connection_log(f"断开连接异常: {key} - {str(e)}", "error")
        finally:
            # 更新UI
            self._update_host_tree()
            self._update_host_list()
            self._update_single_connect_button()
            # 恢复按钮状态
            self.connect_single_btn.config(state=tk.NORMAL)

    def _on_host_tree_select(self, event):
        """主机树选择变化事件处理"""
        self._update_single_connect_button()

    def _update_single_connect_button(self):
        """根据选中节点的连接状态更新按钮文本"""
        selected_items = self.host_tree.selection()

        if not selected_items:
            self.connect_single_btn.config(text="连接主机", state=tk.NORMAL)
            return

        # 获取选中的第一个主机信息
        item = selected_items[0]
        values = self.host_tree.item(item)["values"]
        if len(values) < 2:
            self.connect_single_btn.config(text="连接主机", state=tk.NORMAL)
            return

        host_ip = values[1]
        host_port = values[2]
        key = f"{host_ip}:{host_port}"

        if key in self.pool.connections:
            self.connect_single_btn.config(text="断开链接")
        else:
            self.connect_single_btn.config(text="连接主机")

    def _deselect_all_hosts(self):
        """取消全选所有节点"""
        self.host_listbox.selection_clear(0, tk.END)

    def _toggle_hosts(self):
        """反选所有节点"""
        for i in range(self.host_listbox.size()):
            if self.host_listbox.selection_includes(i):
                self.host_listbox.selection_clear(i)
            else:
                self.host_listbox.selection_set(i)

    def _update_host_list(self):
        """更新节点列表（显示已连接的主机，只显示IP不显示端口）"""
        self.host_listbox.delete(0, tk.END)
        # 保存IP到完整key的映射
        self._host_key_map = {}
        for key in self.pool.connections.keys():
            # 只显示IP，去掉端口号
            host = key.split(":")[0]
            self.host_listbox.insert(tk.END, host)
            # 如果有多个相同IP不同端口，使用完整key作为显示
            if host in self._host_key_map:
                self._host_key_map[key] = key  # 保留完整key
            else:
                self._host_key_map[host] = key
        # 默认全选
        self._select_all_hosts()

        # 同时更新SFTP页面的节点列表
        self._update_sftp_host_list()

    def _update_sftp_host_list(self):
        """更新SFTP页面的节点列表"""
        if hasattr(self, "sftp_host_listbox"):
            self.sftp_host_listbox.delete(0, tk.END)
            # 保存IP到完整key的映射
            self._sftp_host_key_map = {}
            for key in self.pool.connections.keys():
                host = key.split(":")[0]
                self.sftp_host_listbox.insert(tk.END, host)
                if host in self._sftp_host_key_map:
                    self._sftp_host_key_map[key] = key
                else:
                    self._sftp_host_key_map[host] = key
            # 默认全选
            self._select_all_sftp_hosts()

    def _select_all_sftp_hosts(self):
        """全选SFTP页面所有节点"""
        if hasattr(self, "sftp_host_listbox"):
            self.sftp_host_listbox.selection_set(0, tk.END)

    def _deselect_all_sftp_hosts(self):
        """取消全选SFTP页面所有节点"""
        if hasattr(self, "sftp_host_listbox"):
            self.sftp_host_listbox.selection_clear(0, tk.END)

    def _toggle_sftp_hosts(self):
        """反选SFTP页面所有节点"""
        if hasattr(self, "sftp_host_listbox"):
            for i in range(self.sftp_host_listbox.size()):
                if self.sftp_host_listbox.selection_includes(i):
                    self.sftp_host_listbox.selection_clear(i)
                else:
                    self.sftp_host_listbox.selection_set(i)

    async def _async_connect(self, hosts):
        """异步连接主机"""
        results = await self.pool.connect_all(
            hosts,
            timeout=self.config.connection.timeout,
            progress_callback=self._on_connect_progress,
        )

        connected = sum(1 for _, success, _ in results if success)
        self._connection_log(
            f"连接完成: {connected}/{len(hosts)}",
            "success" if connected > 0 else "error",
        )
        self._update_host_tree()
        self._update_status_bar()
        # 更新命令执行页面的节点列表
        self._update_host_list()

    def _on_connect_progress(self, host, success, msg):
        """连接进度回调"""
        level = "success" if success else "error"
        self._connection_log(f"{host}: {msg}", level)

    def _disconnect_all(self):
        """断开所有连接"""
        if self.pool.get_connected_count() == 0:
            messagebox.showinfo("提示", "没有已连接的主机")
            return

        def run():
            asyncio.run(self.pool.close_all())
            self._update_host_tree()
            self._update_status_bar()
            self._log("已断开所有连接", "info")

        threading.Thread(target=run, daemon=True).start()

    def _execute_command(self):
        """执行命令"""
        raw_command = self.cmd_text.get(1.0, tk.END).strip()
        if not raw_command:
            messagebox.showwarning("提示", "请输入命令")
            return

        # 解析命令，去除空行和注释
        commands = []
        for line in raw_command.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)

        if not commands:
            messagebox.showwarning("提示", "没有有效的命令")
            return

        command = "\n".join(commands)

        # 获取用户选择的节点
        selected_indices = self.host_listbox.curselection()
        selected_display = [self.host_listbox.get(i) for i in selected_indices]

        if not selected_display:
            messagebox.showwarning("提示", "请选择至少一个执行节点")
            return

        # 将显示的IP转换为连接池中的完整key
        selected_hosts = []
        for host in selected_display:
            if hasattr(self, "_host_key_map") and host in self._host_key_map:
                selected_hosts.append(self._host_key_map[host])
            else:
                # 如果找不到映射，尝试查找匹配的连接
                for key in self.pool.connections.keys():
                    if key.startswith(host + ":"):
                        selected_hosts.append(key)
                        break

        if not selected_hosts:
            messagebox.showwarning("提示", "未找到选中节点的有效连接")
            return

        self.confirm_dialog.title = "确认执行命令"
        self.confirm_dialog.message = f"确定要在以下 {len(selected_display)} 台主机上执行命令吗?\n\n{', '.join(selected_display)}\n\n{command}"

        if not self.confirm_dialog.show("execute"):
            return

        # 清空之前的结果
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

        self._log(f"开始执行命令 (模式: {self.exec_mode_var.get()})", "info")
        self._log(f"目标节点: {', '.join(selected_hosts)}", "info")
        self._log(f"命令: {command}", "command")

        # 使用共享事件循环执行命令
        if not hasattr(self, "_event_loop") or self._event_loop.is_closed():
            self._event_loop = asyncio.new_event_loop()
            threading.Thread(target=self._event_loop.run_forever, daemon=True).start()

        asyncio.run_coroutine_threadsafe(
            self._async_execute(command, selected_hosts), self._event_loop
        )

    async def _async_execute(self, command, target_hosts=None):
        """异步执行命令"""
        self.task_cancelled = False

        results = await self.pool.execute_all(
            command,
            target_hosts=target_hosts,
            sudo=self.sudo_var.get(),
            timeout=self.config.connection.timeout,
        )

        if self.task_cancelled:
            self._log("任务已终止", "warning")
            return

        # 更新结果表格
        for result in results:
            status = "成功" if result.success else "失败"
            self.result_tree.insert(
                "",
                tk.END,
                values=(
                    result.host,
                    status,
                    result.exit_code if result.exit_code is not None else "-",
                    f"{result.duration:.2f}",
                ),
            )

            # 输出详细日志（清晰标识节点和命令输出）
            self._log("┌──────────────────────────────────────────────────────", "info")
            self._log(f"│ 节点: {result.host}", "info")
            self._log(
                f"│ 状态: {'✓ 成功' if result.success else '✗ 失败'}",
                "success" if result.success else "error",
            )
            self._log(
                f"│ 退出码: {result.exit_code if result.exit_code is not None else '-'}",
                "info",
            )
            self._log(f"│ 耗时: {result.duration:.2f}s", "info")

            if result.stdout:
                self._log("│", "info")
                self._log("│ [标准输出]", "info")
                # 按行输出，每行加前缀
                for line in result.stdout.strip().split("\n"):
                    self._log(f"│   {line}", "stdout")

            if result.stderr:
                self._log("│", "info")
                self._log("│ [标准错误]", "error")
                # 按行输出，每行加前缀
                for line in result.stderr.strip().split("\n"):
                    self._log(f"│   {line}", "error")

            if not result.success and result.error:
                self._log("│", "info")
                self._log(f"│ [错误信息]: {result.error}", "error")

            self._log("└──────────────────────────────────────────────────────", "info")

        success_count = sum(1 for r in results if r.success)
        self._log(f"执行完成: {success_count}/{len(results)}", "info")

    def _export_results(self):
        """导出执行结果"""
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[
                ("CSV文件", "*.csv"),
                ("JSON文件", "*.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not file_path:
            return

        # 获取结果数据
        data = []
        for item in self.result_tree.get_children():
            values = self.result_tree.item(item)["values"]
            data.append(
                {
                    "host": values[0],
                    "status": values[1],
                    "exit_code": values[2],
                    "duration": values[3],
                }
            )

        try:
            if file_path.endswith(".csv"):
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["主机", "状态", "退出码", "耗时(s)"])
                    for row in data:
                        writer.writerow(
                            [
                                row["host"],
                                row["status"],
                                row["exit_code"],
                                row["duration"],
                            ]
                        )
            elif file_path.endswith(".json"):
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

            messagebox.showinfo("成功", "结果已导出")
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {str(e)}")

    def _cancel_tasks(self):
        """终止任务"""
        self.task_cancelled = True
        self._log("正在终止任务...", "warning")

    def _start_transfer(self):
        """开始文件传输"""
        local_path = self.local_path_entry.get().strip()
        remote_path = self.remote_path_entry.get().strip()

        if not local_path:
            messagebox.showwarning("提示", "请输入本地路径")
            return

        if not remote_path:
            messagebox.showwarning("提示", "请输入远程路径")
            return

        if not os.path.exists(local_path) and self.transfer_mode_var.get() == "upload":
            messagebox.showwarning("提示", "本地文件不存在")
            return

        connected_count = self.pool.get_connected_count()
        if connected_count == 0:
            messagebox.showwarning("提示", "没有已连接的主机")
            return

        # 获取用户选择的节点
        selected_indices = self.sftp_host_listbox.curselection()
        selected_display = [self.sftp_host_listbox.get(i) for i in selected_indices]

        if not selected_display:
            messagebox.showwarning("提示", "请选择至少一个传输节点")
            return

        # 将显示的IP转换为连接池中的完整key
        selected_hosts = []
        for host in selected_display:
            if hasattr(self, "_sftp_host_key_map") and host in self._sftp_host_key_map:
                selected_hosts.append(self._sftp_host_key_map[host])
            else:
                for key in self.pool.connections.keys():
                    if key.startswith(host + ":"):
                        selected_hosts.append(key)
                        break

        if not selected_hosts:
            messagebox.showwarning("提示", "未找到选中节点的有效连接")
            return

        transfer_type = "上传" if self.transfer_mode_var.get() == "upload" else "下载"
        self.confirm_dialog.title = f"确认{transfer_type}"
        self.confirm_dialog.message = f"确定要{transfer_type}文件到以下 {len(selected_display)} 台主机吗?\n\n{', '.join(selected_display)}"

        if not self.confirm_dialog.show("transfer"):
            return

        # 清空之前的结果
        for item in self.transfer_tree.get_children():
            self.transfer_tree.delete(item)

        # 如果远程路径是目录，自动使用本地文件名/目录名
        if self.transfer_mode_var.get() == "upload":
            if remote_path.endswith("/") or not os.path.basename(remote_path):
                filename = os.path.basename(local_path.rstrip("/\\"))
                remote_path = os.path.join(remote_path, filename)
            self.remote_path_entry.delete(0, tk.END)
            self.remote_path_entry.insert(0, remote_path)

        self._sftp_log(f"开始{transfer_type}文件...", "info")
        self._sftp_log(f"本地路径: {local_path}", "info")
        self._sftp_log(f"远程路径: {remote_path}", "info")
        self._sftp_log(f"目标主机: {', '.join(selected_display)}", "info")

        # 使用共享事件循环执行传输
        if not hasattr(self, "_event_loop") or self._event_loop.is_closed():
            self._event_loop = asyncio.new_event_loop()
            threading.Thread(target=self._event_loop.run_forever, daemon=True).start()

        # 使用 run_coroutine_threadsafe 并等待结果
        future = asyncio.run_coroutine_threadsafe(
            self._async_transfer(local_path, remote_path, selected_hosts),
            self._event_loop,
        )
        # 添加回调处理传输完成
        future.add_done_callback(self._on_transfer_done)

    async def _async_transfer(self, local_path, remote_path, selected_hosts=None):
        """异步文件传输"""
        self.task_cancelled = False

        print(
            f"_async_transfer called: local_path={local_path}, remote_path={remote_path}"
        )
        print(f"Transfer mode: {self.transfer_mode_var.get()}")
        print(f"Connected hosts: {list(self.pool.connections.keys())}")
        print(f"Selected hosts: {selected_hosts}")

        if self.transfer_mode_var.get() == "upload":
            # 检测是否为目录
            if os.path.isdir(local_path):
                self._sftp_log(f"检测到上传目录: {local_path}", "info")
                results = await self.pool.upload_directory_all(
                    local_path,
                    remote_path,
                    progress_callback=self._on_transfer_progress,
                    target_hosts=selected_hosts,
                )
            else:
                results = await self.pool.upload_all(
                    local_path,
                    remote_path,
                    progress_callback=self._on_transfer_progress,
                    target_hosts=selected_hosts,
                )
        else:
            results = await self.pool.download_all(
                remote_path,
                local_path,
                progress_callback=self._on_transfer_progress,
                target_hosts=selected_hosts,
            )

        print(f"Transfer completed, results: {len(results)}")
        for r in results:
            print(f"  {r.host}: success={r.success}, error={r.error}")

        # 返回结果，由回调函数更新UI
        return results

    def _on_transfer_progress(self, host, transferred, total):
        """传输进度回调"""
        percent = int((transferred / total) * 100)
        self._sftp_log(f"{host}: {percent}% ({transferred}/{total})", "info")

        # 更新进度条
        if total > 0:
            self.transfer_progress["value"] = percent
            self.progress_label.config(text=f"{percent}%")
            self.root.update_idletasks()

    def _on_transfer_done(self, future):
        """传输完成回调"""
        try:
            results = future.result()
            print(f"Transfer callback received: {len(results)} results")
            # 更新结果表格（在主线程中执行）
            self.root.after(0, lambda: self._update_transfer_results(results))
        except Exception as e:
            print(f"Transfer callback error: {e}")
            self._sftp_log(f"传输失败: {str(e)}", "error")

    def _update_transfer_results(self, results):
        """更新传输结果表格"""
        # 更新结果表格
        for result in results:
            status = "成功" if result.success else "失败"
            size = f"{result.size / 1024:.1f}KB" if result.size else "-"
            self.transfer_tree.insert(
                "", tk.END, values=(result.host, status, result.filename, size)
            )

            if result.success:
                self._sftp_log(f"{result.host}: {result.filename} 传输成功", "success")
            else:
                self._sftp_log(f"{result.host}: {result.error}", "error")

        success_count = sum(1 for r in results if r.success)
        self._sftp_log(
            f"{('上传' if self.transfer_mode_var.get() == 'upload' else '下载')}完成: {success_count}/{len(results)}",
            "info",
        )

    def _update_status_bar(self):
        """更新状态栏"""
        connected = self.pool.get_connected_count()
        total = len(self.config.hosts)
        self.status_label.config(text=f"就绪 | 已连接: {connected}/{total}")

    def _on_close(self):
        """关闭应用"""
        if self.pool.get_connected_count() > 0:

            def run():
                asyncio.run(self.pool.close_all())

            threading.Thread(target=run, daemon=True).start()

        self.root.destroy()


def main_gui():
    """GUI入口"""
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
