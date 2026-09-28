import os
import json
import tempfile
from config import pconf, plugin_config, conf, write_plugin_config  # noqa: F401  # re-exported via `from .plugin import *`
from common.log import logger


def _write_json_atomically(path, config):
    """Store ``config`` at ``path`` through a sibling file, then swap it in.

    Writing straight into the file truncates it before the new bytes are there,
    so anything that fails while serialising — a full disk, an interrupted
    update — leaves a half-written store behind. ``PluginManager._load_all_config``
    reads ``plugins/config.json`` with a bare ``json.load`` whose failure is only
    logged, so one damaged write takes every plugin's configuration with it
    instead of reporting anything to the user.
    """
    fd, temporary = tempfile.mkstemp(prefix=".config.json.", suffix=".tmp",
                                     dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        raise


class Plugin:
    def __init__(self):
        self.handlers = {}

    def load_config(self) -> dict:
        """
        加载当前插件配置
        :return: 插件配置字典
        """
        # 优先获取 plugins/config.json 中的全局配置
        plugin_conf = pconf(self.name)
        if not plugin_conf:
            # 全局配置不存在，则获取插件目录下的配置
            plugin_config_path = os.path.join(self.path, "config.json")
            if os.path.exists(plugin_config_path):
                with open(plugin_config_path, "r", encoding="utf-8") as f:
                    plugin_conf = json.load(f)

                # 写入全局配置内存
                write_plugin_config({self.name: plugin_conf})
        return plugin_conf

    def save_config(self, config: dict):
        try:
            write_plugin_config({self.name: config})
            # 写入全局配置
            global_config_path = "./plugins/config.json"
            if os.path.exists(global_config_path):
                _write_json_atomically(global_config_path, plugin_config)
            # 写入插件配置
            plugin_config_path = os.path.join(self.path, "config.json")
            if os.path.exists(plugin_config_path):
                _write_json_atomically(plugin_config_path, config)

        except Exception as e:
            logger.warn("save plugin config failed: {}".format(e))

    def get_help_text(self, **kwargs):
        return "暂无帮助信息"

    def reload(self):
        pass
