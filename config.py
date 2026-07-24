"""
SSH 批量执行工具 - 配置模块
"""

import json
import os
import sys
from dataclasses import dataclass, field


@dataclass
class HostConfig:
    """主机配置"""

    name: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    use_key: bool = False
    key_file: str = ""
    key_passphrase: str = ""
    sudo_enabled: bool = False
    sudo_password: str = ""
    switch_to_root: bool = False
    root_password: str = ""
    group: str = "默认分组"


@dataclass
class ConnectionConfig:
    """连接配置"""

    timeout: int = 30
    max_retries: int = 2
    concurrency: int = 10


@dataclass
class AppConfig:
    """应用配置"""

    hosts: list[HostConfig] = field(default_factory=list)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    groups: list[str] = field(default_factory=lambda: ["默认分组"])


class ConfigManager:
    """配置管理器"""

    def __init__(self):
        self.config_dir = self._get_config_dir()
        self.config_path = os.path.join(self.config_dir, "config.json")
        self._ensure_dir()

    def _get_config_dir(self):
        """获取配置目录"""
        if getattr(sys, "frozen", False):
            # PyInstaller 打包后：配置保存在可执行文件同级目录
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _ensure_dir(self):
        """确保配置目录存在"""
        os.makedirs(self.config_dir, exist_ok=True)

    def load(self) -> AppConfig:
        """加载配置"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    data = json.load(f)
                    return self._deserialize(data)
            except Exception:
                return AppConfig()
        return AppConfig()

    def save(self, config: AppConfig):
        """保存配置"""
        data = self._serialize(config)
        temp_path = self.config_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.config_path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _serialize(self, config: AppConfig) -> dict:
        """序列化配置"""
        return {
            "hosts": [self._host_to_dict(h) for h in config.hosts],
            "connection": {
                "timeout": config.connection.timeout,
                "max_retries": config.connection.max_retries,
                "concurrency": config.connection.concurrency,
            },
            "groups": config.groups,
        }

    def _deserialize(self, data: dict) -> AppConfig:
        """反序列化配置"""
        config = AppConfig()

        if "hosts" in data:
            config.hosts = [self._dict_to_host(h) for h in data["hosts"]]

        if "connection" in data:
            conn = data["connection"]
            config.connection = ConnectionConfig(
                timeout=conn.get("timeout", 30),
                max_retries=conn.get("max_retries", 2),
                concurrency=conn.get("concurrency", 10),
            )

        if "groups" in data:
            config.groups = data["groups"]

        return config

    def _host_to_dict(self, host: HostConfig) -> dict:
        """主机配置转字典"""
        return {
            "name": host.name,
            "host": host.host,
            "port": host.port,
            "username": host.username,
            "password": host.password,
            "use_key": host.use_key,
            "key_file": host.key_file,
            "key_passphrase": host.key_passphrase,
            "sudo_enabled": host.sudo_enabled,
            "sudo_password": host.sudo_password,
            "switch_to_root": host.switch_to_root,
            "root_password": host.root_password,
            "group": host.group,
        }

    def _dict_to_host(self, data: dict) -> HostConfig:
        """字典转主机配置"""
        return HostConfig(
            name=data.get("name", ""),
            host=data.get("host", ""),
            port=data.get("port", 22),
            username=data.get("username", ""),
            password=data.get("password", ""),
            use_key=data.get("use_key", False),
            key_file=data.get("key_file", ""),
            key_passphrase=data.get("key_passphrase", ""),
            sudo_enabled=data.get("sudo_enabled", False),
            sudo_password=data.get("sudo_password", ""),
            switch_to_root=data.get("switch_to_root", False),
            root_password=data.get("root_password", ""),
            group=data.get("group", "默认分组"),
        )
