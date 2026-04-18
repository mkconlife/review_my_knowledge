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


def copy_files_to_plugin(files: list, target_dir: str, use_docker: bool = False, container_name: str = "astrbot"):
    """
    将文件复制到插件目录
    Docker 模式下使用 docker cp 命令避免权限问题
    """
    if use_docker:
        # Docker 模式下，使用 docker cp 命令复制文件到容器
        import subprocess

        # 确保容器存在
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                logger.error(f"容器 {container_name} 不存在或未运行")
                return
        except FileNotFoundError:
            logger.error("未找到 docker 命令，请确保 Docker 已安装")
            return
        except subprocess.TimeoutExpired:
            logger.error("Docker 命令超时")
            return

        for filename in files:
            source = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

            if not os.path.exists(source):
                logger.warning(f"源文件不存在: {source}")
                continue

            try:
                dest_path = f"{target_dir}/{filename}"
                subprocess.run(
                    ["docker", "cp", source, f"{container_name}:{dest_path}"],
                    capture_output=True, text=True, timeout=30, check=True
                )
                logger.info(f"[Docker] 已复制: {filename} -> {container_name}:{dest_path}")
            except subprocess.CalledProcessError as e:
                logger.error(f"复制文件失败 {filename}: {e.stderr.strip()}")
            except subprocess.TimeoutExpired:
                logger.error(f"复制文件超时 {filename}")
    else:
        # 本地模式，尝试直接复制
        try:
            os.makedirs(target_dir, exist_ok=True)
        except PermissionError:
            # 如果目录创建失败，尝试使用 sudo
            import subprocess
            try:
                subprocess.run(
                    ["sudo", "mkdir", "-p", target_dir],
                    capture_output=True, timeout=10, check=True
                )
                logger.info(f"[本地] 使用 sudo 创建目录: {target_dir}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.error(f"创建目录失败 {target_dir}: {e}")
                logger.error("提示: 请确保目标目录有写入权限，或使用 sudo 运行此脚本")
                return

        for filename in files:
            source = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            dest = os.path.join(target_dir, filename)

            if not os.path.exists(source):
                logger.warning(f"源文件不存在: {source}")
                continue

            try:
                shutil.copy2(source, dest)
                logger.info(f"[本地] 已复制: {filename} -> {target_dir}")
            except PermissionError:
                # 权限不足时尝试使用 sudo cp
                import subprocess
                try:
                    subprocess.run(
                        ["sudo", "cp", source, dest],
                        capture_output=True, timeout=10, check=True
                    )
                    logger.info(f"[本地] 使用 sudo 复制: {filename} -> {target_dir}")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                    logger.error(f"复制文件失败 {filename}: {e}")
                    logger.error("提示: 请手动修复目标目录权限或使用 sudo 运行此脚本")
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

    # 先复制 settings.txt 到插件目录
    copy_settings_to_plugin(settings_path, target_dir, use_docker)

    # 复制复习册文件
    copy_files_to_plugin(settings['files'], target_dir, use_docker)

    logger.info("配置完成!")


def copy_settings_to_plugin(settings_path: str, target_dir: str, use_docker: bool = False, container_name: str = "astrbot"):
    """
    将 settings.txt 复制到插件目录
    Docker 模式下使用 docker cp 命令避免权限问题
    """
    import subprocess

    dest_path = f"{target_dir}/settings.txt"

    if use_docker:
        # Docker 模式下，使用 docker cp 命令
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                logger.error(f"容器 {container_name} 不存在或未运行")
                return
        except FileNotFoundError:
            logger.error("未找到 docker 命令，请确保 Docker 已安装")
            return
        except subprocess.TimeoutExpired:
            logger.error("Docker 命令超时")
            return

        try:
            subprocess.run(
                ["docker", "cp", settings_path, f"{container_name}:{dest_path}"],
                capture_output=True, text=True, timeout=30, check=True
            )
            logger.info(f"[Docker] 已复制: settings.txt -> {container_name}:{dest_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"复制 settings.txt 失败: {e.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.error("复制 settings.txt 超时")
    else:
        # 本地模式
        try:
            os.makedirs(target_dir, exist_ok=True)
            shutil.copy2(settings_path, dest_path)
            logger.info(f"[本地] 已复制: settings.txt -> {target_dir}")
        except PermissionError:
            try:
                subprocess.run(
                    ["sudo", "cp", settings_path, dest_path],
                    capture_output=True, timeout=10, check=True
                )
                logger.info(f"[本地] 使用 sudo 复制: settings.txt -> {target_dir}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.error(f"复制 settings.txt 失败: {e}")
                logger.error("提示: 请手动修复目标目录权限或使用 sudo 运行此脚本")
        except Exception as e:
            logger.error(f"复制 settings.txt 失败: {e}")


if __name__ == "__main__":
    configure()
