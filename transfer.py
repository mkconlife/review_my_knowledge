"""
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
