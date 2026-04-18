"""
初始化脚本 - 创建 transfer.py 和 settings.txt
"""
import os
import sys
import platform


def create_transfer_py(plugin_dir: str):
    """创建 transfer.py，内容为 tool_simple.py 的内容"""
    transfer_path = os.path.join(plugin_dir, "transfer.py")

    # 读取 tool_simple.py 的内容
    tool_path = os.path.join(plugin_dir, "tool_simple.py")
    if os.path.exists(tool_path):
        with open(tool_path, 'r', encoding='utf-8') as f:
            tool_content = f.read()
    else:
        tool_content = r'''"""
txt 文件转插件条目格式工具
将原始试题 txt 文件转换为 /添加条目 命令可识别的格式

输入格式示例:
一.章节标题
1.(Q)判断:题目内容[对/错]
解析内容(可选)
2.(Q)题目___,题目___[答案1;答案2]
3.纯知识点内容(无Q标记)

输出格式示例:
知识库名
ID=Q_xxxxxxxxxx
CATEGORY=章节标题
SUBJECT=化学
[判断](Q)题目内容[对/错]
解析:解析内容
"""

import argparse
import re
import os
import hashlib


def detect_question_type(content):
    """检测题型"""
    # 检查是否有 (Q)判断 标记
    if content.startswith('判断:') or content.startswith('判断：'):
        return '判断'
    # 检查是否有多个填空标记(___或问号)
    # 注意: 同时统计 ASCII问号(?) 和中文问号(？)
    blank_count = content.count('___') + content.count('？') + content.count('?')
    if blank_count > 1:
        return '多填空'
    if blank_count == 1:
        return '单填空'
    # 以问号结尾的可能是开放题
    if content.rstrip().endswith('？') or content.rstrip().endswith('?'):
        return '开放'
    # 默认为单填空
    return '单填空'


def extract_answers(content):
    """
    从内容中提取答案 [...] 部分
    返回: (答案字符串, 清理后的内容, 原始内容用于题型检测)
    """
    # 查找最后一个 [...] 作为答案
    matches = list(re.finditer(r'\[([^\]]+)\]', content))
    if not matches:
        return None, content, content

    last_match = matches[-1]
    answer_str = last_match.group(1).strip()

    # 移除答案部分（保留问号用于题型检测）
    content_without_answer = content[:last_match.start()].rstrip()

    # 清理后的内容（移除末尾问号，用于最终输出）
    cleaned_content = content_without_answer.rstrip('？?').rstrip()

    return answer_str, cleaned_content, content_without_answer


def parse_answer_string(answer_str, question_type):
    """
    解析答案字符串
    判断题: 返回 [答案]
    单填空: 返回 [答案1/答案2/可选答案]
    多填空: 返回 [[答案1|可选1];[答案2|可选2]] -> 列表的列表
    """
    if not answer_str:
        return []

    # 判断题直接返回
    if question_type == '判断':
        return [answer_str]

    # 检查是否为多填空答案（用分号分隔）
    if ';' in answer_str or '；' in answer_str:
        # 统一分隔符
        answer_str = answer_str.replace('；', ';')
        parts = [p.strip() for p in answer_str.split(';') if p.strip()]
        # 每个空可能有多个可接受答案（用斜杠或竖线分隔）
        result = []
        for part in parts:
            # 支持 / 或 | 作为可接受答案分隔符
            options = re.split(r'[/|]', part)
            options = [o.strip() for o in options if o.strip()]
            result.append(options)
        return result
    else:
        # 单填空答案，可能有多个可接受答案
        options = re.split(r'[/|]', answer_str)
        options = [o.strip() for o in options if o.strip()]
        return options


def clean_question_text(text):
    """清理题目文本，移除答案标记"""
    # 移除所有 [...] 标记（包括中间的答案提示）
    while '[' in text and ']' in text:
        text = re.sub(r'\[[^\]]*\]', '', text)
    return text.strip()


def generate_id(kb_name, category, content, index):
    """生成唯一ID"""
    raw = f"{kb_name}_{category}_{content}_{index}"
    return f"Q_{hashlib.md5(raw.encode()).hexdigest()[:10]}"


def convert_file(input_file, subject='化学', kb_name=''):
    """
    转换文件
    
    参数:
        input_file: 输入txt文件路径
        subject: 学科 (默认: 化学)
        kb_name: 知识库名称 (默认: 使用文件名)
    
    返回:
        转换后的文本
    """
    if not kb_name:
        kb_name = os.path.splitext(os.path.basename(input_file))[0]

    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    entries = []
    current_category = '未分类'
    entry_index = 0
    pending_explanation = []  # 待处理的解析行

    def flush_explanation():
        """将待处理的解析附加到最后一个条目"""
        if pending_explanation and entries:
            exp_text = '\n'.join(pending_explanation).strip()
            if exp_text:
                if entries[-1].get('explanation'):
                    entries[-1]['explanation'] += '\n' + exp_text
                else:
                    entries[-1]['explanation'] = exp_text
        pending_explanation.clear()

    for line in lines:
        line = line.rstrip('\n').rstrip()
        stripped = line.strip()

        # 跳过空行
        if not stripped:
            continue

        # 检测章节标题 (一、二、三... 或 一.二.三.)
        chapter_match = re.match(r'^[一二三四五六七八九十百]+[.、]\s*(.+)', stripped)
        if chapter_match:
            flush_explanation()
            current_category = chapter_match.group(1).strip()
            continue

        # 检测题目 (以数字.开头)
        entry_match = re.match(r'^(\d+)[.、]\s*(.*)', stripped)
        if entry_match:
            flush_explanation()

            number = entry_match.group(1)
            rest = entry_match.group(2)

            # 检查是否有 (Q) 标记
            has_q = '(Q)' in rest
            if has_q:
                rest = rest.replace('(Q)', '', 1)

            # 提取答案 (返回: 答案字符串, 清理后的内容, 原始内容含问号)
            answer_str, cleaned_text, raw_text = extract_answers(rest)

            # 用原始内容（含问号）检测题型
            q_type = detect_question_type(raw_text)

            # 解析答案
            answers = parse_answer_string(answer_str, q_type) if answer_str else []

            # 进一步清理题目文本（移除中间可能存在的 [...] 答案提示）
            final_content = clean_question_text(cleaned_text)

            entry_index += 1
            entry = {
                'id': generate_id(kb_name, current_category, final_content, entry_index),
                'category': current_category,
                'subject': subject,
                'question_type': q_type,
                'is_question': has_q,
                'content': final_content,
                'answers': answers,
                'explanation': '',
            }
            entries.append(entry)
        else:
            # 非题目行，作为解析处理
            pending_explanation.append(stripped)

    # 处理最后的解析
    flush_explanation()

    # 生成输出
    output_lines = []
    for entry in entries:
        output_lines.append(kb_name)
        output_lines.append(f"ID={entry['id']}")
        output_lines.append(f"CATEGORY={entry['category']}")
        output_lines.append(f"SUBJECT={entry['subject']}")

        q_type = entry['question_type']
        content = entry['content']
        answers = entry['answers']

        if entry['is_question']:
            if answers:
                if q_type == '多填空':
                    # 多填空格式: [答案1|可选1;答案2|可选2]
                    ans_parts = []
                    for a in answers:
                        if isinstance(a, list):
                            ans_parts.append('|'.join(a))
                        else:
                            ans_parts.append(str(a))
                    ans_str = ';'.join(ans_parts)
                    output_lines.append(f"[{q_type}](Q){content}[{ans_str}]")
                elif q_type == '判断':
                    # 判断题格式: [对/错]
                    ans_str = '/'.join(str(a) for a in answers)
                    output_lines.append(f"[{q_type}](Q){content}[{ans_str}]")
                else:
                    # 单填空格式: [答案1/答案2]
                    ans_str = '/'.join(str(a) for a in answers)
                    output_lines.append(f"[{q_type}](Q){content}[{ans_str}]")
            else:
                output_lines.append(f"[{q_type}](Q){content}")
        else:
            # 纯知识点
            if answers:
                if isinstance(answers[0], list):
                    ans_parts = []
                    for a in answers:
                        if isinstance(a, list):
                            ans_parts.append('|'.join(a))
                        else:
                            ans_parts.append(str(a))
                    ans_str = ';'.join(ans_parts)
                else:
                    ans_str = '/'.join(str(a) for a in answers)
                output_lines.append(f"{content}[{ans_str}]")
            else:
                output_lines.append(content)

        # 添加解析
        if entry.get('explanation'):
            exp = entry['explanation'].strip()
            if exp:
                output_lines.append(f"解析:{exp}")

        output_lines.append('')

    output_text = chr(10).join(output_lines)
    return output_text


def main():
    parser = argparse.ArgumentParser(description='将试题 txt 文件转换为插件可识别的格式')
    parser.add_argument('--file', required=True, help='输入文件路径')
    parser.add_argument('--subject', default='化学', help='学科 (默认: 化学)')
    parser.add_argument('--kb_name', default='', help='知识库名称 (默认: 文件名)')
    parser.add_argument('--output', default='', help='输出文件路径 (默认: 输入文件名_import.txt)')
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"错误: 文件不存在: {args.file}")
        return

    result = convert_file(args.file, args.subject, args.kb_name)

    output_path = args.output or os.path.splitext(args.file)[0] + '_import.txt'
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(result)

    print(f"转换完成: {output_path}")
    print(f"共生成 {result.count('ID=')} 个条目")


if __name__ == '__main__':
    main()
'''

    with open(transfer_path, 'w', encoding='utf-8') as f:
        f.write(tool_content)
    print(f"已创建: {transfer_path}")


def create_configure_py(plugin_dir: str):
    """创建 configure.py 配置文件传输脚本"""
    configure_path = os.path.join(plugin_dir, "configure.py")
    content = '''"""
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
'''

    with open(configure_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"已创建: {configure_path}")


def create_settings_txt(plugin_dir: str):
    """创建 settings.txt 配置模板"""
    settings_path = os.path.join(plugin_dir, "settings.txt")
    content = """[DATABASE]
FILES="键入文件名","键入文件名","键入文件名"
DESCRIPTION="键入描述","键入描述","键入描述"

[INSTALLATION]
PATH=
"""
    with open(settings_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"已创建: {settings_path}")


def detect_astrbot_installation() -> str:
    """
    检测 AstrBot 的安装方式
    返回: 'Docker' 或 绝对路径
    """
    # 检测 Docker 环境特征
    # 1. 检查 /.dockerenv 文件
    if os.path.exists('/.dockerenv'):
        return 'Docker'

    # 2. 检查 cgroup 中是否有 docker 关键字
    try:
        with open('/proc/1/cgroup', 'r', encoding='utf-8') as f:
            cgroup_content = f.read()
            if 'docker' in cgroup_content or 'kubepods' in cgroup_content:
                return 'Docker'
    except (FileNotFoundError, PermissionError):
        pass

    # 3. 检查环境变量
    if os.environ.get('container', '') == 'docker':
        return 'Docker'

    # 非 Docker 环境，尝试查找 AstrBot 安装路径
    # 常见安装路径
    common_paths = [
        '/opt/astrbot',
        '/usr/local/astrbot',
        os.path.expanduser('~/astrbot'),
        os.path.expanduser('~/.local/share/astrbot'),
    ]

    for path in common_paths:
        if os.path.isdir(path):
            return path

    # 从当前脚本位置向上查找
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):  # 最多向上查找 5 层
        if os.path.exists(os.path.join(current, 'astrbot')):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # 找不到，返回空让用户手动输入
    return ''


def update_settings_path(plugin_dir: str, path_value: str):
    """更新 settings.txt 中的 PATH 值"""
    settings_path = os.path.join(plugin_dir, "settings.txt")
    if not os.path.exists(settings_path):
        print(f"错误: settings.txt 不存在")
        return

    with open(settings_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.strip().startswith('PATH='):
            new_lines.append(f'PATH={path_value}\n')
        else:
            new_lines.append(line)

    with open(settings_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    print(f"已更新 PATH={path_value}")


def detect_os() -> str:
    """
    检测操作系统类型
    返回: 'windows', 'macos', 'linux' 或 'unknown'
    """
    os_name = platform.system().lower()
    if os_name == 'windows':
        return 'windows'
    elif os_name == 'darwin':
        return 'macos'
    elif os_name == 'linux':
        return 'linux'
    return 'unknown'


def get_copy_command(os_type: str) -> str:
    """
    根据操作系统返回文件复制命令
    """
    if os_type == 'windows':
        return 'copy'
    elif os_type == 'macos' or os_type == 'linux':
        return 'cp'
    return 'cp'  # 默认使用 cp


def generate_copy_instructions(os_type: str, source_dir: str, dest_dir: str, files: list) -> str:
    """
    生成操作系统特定的复制命令说明
    """
    cmd = get_copy_command(os_type)
    instructions = []

    instructions.append(f"\n检测到操作系统: {os_type.upper()}")
    instructions.append(f"推荐使用以下命令将文件复制到插件目录:\n")

    if os_type == 'windows':
        # Windows 使用 copy 命令
        for f in files:
            instructions.append(f'copy "{os.path.join(source_dir, f)}" "{dest_dir}\\"')
    else:
        # Linux/Mac 使用 cp 命令
        for f in files:
            instructions.append(f'cp "{os.path.join(source_dir, f)}" "{dest_dir}/"')

    return '\n'.join(instructions)


def main():
    """主初始化函数"""
    # 确定插件目录
    plugin_dir = os.path.dirname(os.path.abspath(__file__))

    print("=" * 40)
    print("开始初始化复习册插件...")
    print("=" * 40)

    # 1. 检测操作系统
    os_type = detect_os()
    print(f"检测到操作系统: {os_type.upper()}")
    print(f"文件复制命令: {get_copy_command(os_type)}")

    # 2. 创建 transfer.py (包含 tool_simple.py 内容)
    create_transfer_py(plugin_dir)

    # 3. 创建 configure.py
    create_configure_py(plugin_dir)

    # 4. 创建 settings.txt
    create_settings_txt(plugin_dir)

    # 5. 检测 AstrBot 安装方式
    install_path = detect_astrbot_installation()

    if install_path == 'Docker':
        print("检测到: Docker 安装")
        update_settings_path(plugin_dir, 'Docker')
    elif install_path:
        print(f"检测到: 本地安装 - {install_path}")
        update_settings_path(plugin_dir, install_path)
    else:
        print("未检测到 AstrBot 安装路径")
        user_path = input("请手动输入 AstrBot 安装路径 (或输入 'Docker'): ").strip()
        if user_path:
            update_settings_path(plugin_dir, user_path)
        else:
            print("警告: 未设置 PATH，请手动编辑 settings.txt")

    # 6. 生成文件复制说明
    files_to_copy = ['transfer.py', 'settings.txt', 'configure.py']
    dest_dir = os.path.join(install_path if install_path else '<AstrBot目录>', 'data', 'plugins', 'review_my_knowledge')
    copy_instructions = generate_copy_instructions(os_type, plugin_dir, dest_dir, files_to_copy)
    print(copy_instructions)

    print("\n" + "=" * 40)
    print("初始化完成!")
    print("=" * 40)


if __name__ == "__main__":
    main()
