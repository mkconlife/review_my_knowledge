"""
configure.py - 根据 AstrBot 安装方式配置文件传输
读取 settings.txt，根据 PATH 值决定传输方式
"""
import os
import sys
import shutil
import configparser

# 支持独立运行：尝试导入 astrbot logger，失败则使用标准 logging
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logger = logging.getLogger(__name__)


PLUGIN_NAME = "review_my_knowledge"


def read_settings(settings_path: str) -> dict:
    """读取 settings.txt 配置"""
    config = configparser.ConfigParser()
    config.read(settings_path, encoding='utf-8')

    result = {
        'files': [],
        'descriptions': [],
        'path': ''
    }

    if config.has_section('DATABASE'):
        files_str = config.get('DATABASE', 'FILES', fallback='')
        desc_str = config.get('DATABASE', 'DESCRIPTION', fallback='')

        # 解析引号分隔的列表
        result['files'] = [f.strip('"').strip("'").strip() for f in files_str.split(',') if f.strip()]
        result['descriptions'] = [d.strip('"').strip("'").strip() for d in desc_str.split(',') if d.strip()]

    if config.has_section('INSTALLATION'):
        result['path'] = config.get('INSTALLATION', 'PATH', fallback='')

    return result


def get_plugin_target_dir(path_value: str) -> str:
    """
    根据 PATH 值获取插件目标目录
    Docker: 使用 Docker 卷挂载路径
    本地路径: 直接拼接插件目录
    """
    if path_value.lower() == 'docker':
        # Docker 环境下，插件目录通常是 /app/data/plugins/<plugin_name>
        # 或者通过卷挂载的本地路径
        # 这里返回一个默认的 Docker 内路径
        return f"/app/plugins/{PLUGIN_NAME}"
    else:
        # 本地安装，直接拼接路径
        return os.path.join(path_value, "data", "plugins", PLUGIN_NAME)


def copy_files_to_plugin(files: list, target_dir: str, use_docker: bool = False):
    """
    将文件复制到插件目录
    """
    os.makedirs(target_dir, exist_ok=True)

    for filename in files:
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        dest = os.path.join(target_dir, filename)

        if not os.path.exists(source):
            logger.warning(f"源文件不存在: {source}")
            continue

        try:
            if use_docker:
                # Docker 模式下，假设已通过卷挂载，直接复制
                # 注意：如果卷未正确挂载，文件会复制到容器内临时路径
                shutil.copy2(source, dest)
                logger.info(f"[Docker] 已复制: {filename}")
                logger.warning(f"[Docker] 请确保 Docker 卷已正确挂载到 {target_dir}")
            else:
                shutil.copy2(source, dest)
                logger.info(f"[本地] 已复制: {filename} -> {target_dir}")
        except Exception as e:
            logger.error(f"复制文件失败 {filename}: {e}")


def configure():
    """主配置函数"""
    settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.txt")

    if not os.path.exists(settings_path):
        logger.error("settings.txt 不存在，请先运行 Init.py")
        return

    # 读取配置
    settings = read_settings(settings_path)
    path_value = settings['path']

    if not path_value:
        logger.error("PATH 未设置，请运行 Init.py 或手动编辑 settings.txt")
        return

    use_docker = path_value.lower() == 'docker'
    target_dir = get_plugin_target_dir(path_value)

    logger.info(f"安装方式: {'Docker' if use_docker else '本地'}")
    logger.info(f"目标目录: {target_dir}")
    logger.info(f"待传输文件: {settings['files']}")

    # 复制文件
    copy_files_to_plugin(settings['files'], target_dir, use_docker)

    logger.info("配置完成!")


if __name__ == "__main__":
    configure()
