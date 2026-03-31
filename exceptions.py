class VideoPluginError(Exception):
    """视频插件基础异常。"""


class ConfigError(VideoPluginError):
    """插件配置错误。"""


class ImageCountError(VideoPluginError):
    """图片数量不符合要求。"""


class ProviderAPIError(VideoPluginError):
    """视频生成接口调用失败。"""


class TaskProcessError(VideoPluginError):
    """异步任务处理失败。"""
