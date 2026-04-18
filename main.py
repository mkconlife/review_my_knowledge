import os
import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger
from .knowledge_system import KnowledgeSystem

@register("review_my_knowledge", "mkconlife", "高中知识点复习助手", "2.2.1", "https://github.com/mkconlife/astrbot_plugin_knowledge")
class KnowledgePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        self.data_dir = StarTools.get_data_dir("review_my_knowledge")
        os.makedirs(self.data_dir, exist_ok=True)

        config = context.get_config()
        self.plugin_config = {
            'llm_judge': config.get("llm_judge", True),
            'llm_threshold': config.get("llm_threshold", 0.85),
            'use_llm_for_explanation': config.get("use_llm_for_explanation", False),
            'default_kb': config.get("default_kb", "biology_mistakes"),
            'session_timeout': config.get("session_timeout", 30),
            'max_content_length': config.get("max_content_length", 10000),
            'message_max_length': config.get("message_max_length", 3000)
        }
        logger.info(f"插件配置已加载: {self.plugin_config}")

        self.kb_system = KnowledgeSystem(self.data_dir, context, self.plugin_config)

        logger.info(f"知识点复习插件已加载，数据目录: {self.data_dir}")

    async def initialize(self):
        await self.kb_system.initialize()
        logger.info("知识系统数据库已初始化")

    def _truncate_message(self, msg: str) -> str:
        """截断超长消息（限制为字符数，非字节数）"""
        max_len = self.plugin_config.get('message_max_length', 3000)
        if len(msg) > max_len:
            suffix = "\n...（内容过长已截断）"
            # 确保截断后总长度不超过 max_len
            truncate_at = max(0, max_len - len(suffix))
            # 安全截断：避免在多字节字符中间截断
            # Python 的字符串切片已按 Unicode 码位处理，此处直接切片
            msg = msg[:truncate_at] + suffix
        return msg

    def _get_user_name(self, event: AstrMessageEvent) -> str:
        """获取用户名"""
        name = event.get_sender_name() or event.get_sender_id()
        if not name:
            name = f"unknown_{event.session_id or 'default'}"
        return name

    # ==================== 新命令 ====================

    @filter.command("列出复习册")
    async def list_books(self, event: AstrMessageEvent):
        '''列出所有复习册。读取 settings.txt 中的 FILES 和 DESCRIPTION'''
        try:
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.txt")

            if not os.path.exists(settings_path):
                yield event.plain_result("settings.txt 不存在，请先运行 Init.py 初始化")
                return

            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(settings_path, encoding='utf-8')

            if not cfg.has_section('DATABASE'):
                yield event.plain_result("settings.txt 中未找到 [DATABASE] 段")
                return

            files_str = cfg.get('DATABASE', 'FILES', fallback='')
            desc_str = cfg.get('DATABASE', 'DESCRIPTION', fallback='')

            files = [f.strip('"').strip("'").strip() for f in files_str.split(',') if f.strip()]
            descriptions = [d.strip('"').strip("'").strip() for d in desc_str.split(',') if d.strip()]

            if not files:
                yield event.plain_result("复习册列表为空，请在 settings.txt 中配置 FILES")
                return

            msg = "复习册列表\n" + "=" * 30 + "\n"
            for i, fname in enumerate(files):
                desc = descriptions[i] if i < len(descriptions) else "暂无描述"
                msg += f"\n{i+1}. 【{fname}】{desc}"

            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"列出复习册失败: {e}")
            yield event.plain_result(f"列出复习册失败: {str(e)}")

    @filter.command("开始复习错题")
    async def start_mistake_review(self, event: AstrMessageEvent, kb_name: str = ""):
        '''开始复习错题（单题模式）。用法: /开始复习错题 <复习册名>'''
        session_id = event.unified_msg_origin
        user_name = self._get_user_name(event)

        if not kb_name:
            kb_name = self.plugin_config.get("default_kb", "biology_mistakes")

        try:
            # 单题模式，固定 count=1
            results = await self.kb_system.start_mistake_review(kb_name, 1, user_name, session_id)

            if not results:
                yield event.plain_result(f"复习册 {kb_name} 暂无可复习的题目")
                return

            if results and 'error' in results[0]:
                yield event.plain_result(results[0]['error'])
                return

            q = results[0]
            msg = f"错题复习 【{kb_name}】\n" + "=" * 30 + "\n"
            msg += f"\n[{q['category']}] {q['question']}\n"
            msg += f"   题型: {q['question_type']} | ID: {q['id']}\n"
            msg += f"   总提问: {q['total_ask']} | 你的提问: {q['ask_you']}\n"

            msg += "\n作答格式: /作答 <答案>"
            msg += "\n出示答案: /出示答案"
            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"开始复习错题失败: {e}")
            yield event.plain_result(f"开始复习错题失败: {str(e)}")

    @filter.command("作答")
    async def submit_answer(self, event: AstrMessageEvent, *, answers: str = ""):
        '''提交答案。用法: /作答 <答案> 或多题: 1.答案1;答案2\n2.答案3'''
        session_id = event.unified_msg_origin
        user_name = self._get_user_name(event)

        if not answers or not answers.strip():
            yield event.plain_result("答案不能为空")
            return

        try:
            # 获取所有待回答问题
            all_pending = await self.kb_system.db.get_all_pending(session_id)

            if not all_pending:
                yield event.plain_result("当前无待回答问题，请先 /开始复习错题")
                return

            # 解析用户答案
            answer_lines = [line.strip() for line in answers.strip().split('\n') if line.strip()]

            # 尝试去除序号
            parsed_answers = []
            for line in answer_lines:
                # 匹配 "1." 或 "1、" 等序号前缀
                match = re.match(r'^\d+[\.、]\s*(.*)', line)
                if match:
                    parsed_answers.append(match.group(1).strip())
                else:
                    parsed_answers.append(line)

            # 检查答案数量是否匹配
            if len(parsed_answers) != len(all_pending):
                yield event.plain_result(
                    f"答案数量不匹配：共有 {len(all_pending)} 道题目待回答，"
                    f"您提供了 {len(parsed_answers)} 个答案\n"
                    f"请使用格式：1.答案1\n2.答案2\n3.答案3...\n"
                    f"请重新使用 /作答 <答案> 提交正确答案"
                )
                return

            # 批量处理每道题
            results = []
            for i, (pending, user_answer) in enumerate(zip(all_pending, parsed_answers), 1):
                entry_id = pending['entry_id']
                q_type = pending.get('question_type', '单填空')

                # 限制答案长度
                user_answer = user_answer[:1000]

                # 获取标准答案（pending['answers'] 已经是 list，无需再次解析）
                correct_answers = pending['answers']

                # 验证答案数据完整性
                if not correct_answers and q_type != '开放':
                    logger.error(f"题目 {entry_id} 答案数据异常，题型: {q_type}")
                    yield event.plain_result("题目数据异常，请联系管理员")
                    return

                # 判定答案
                if q_type == '多填空':
                    # 确保 correct_answers 是 List[List[str]]
                    if correct_answers and isinstance(correct_answers, list):
                        if not isinstance(correct_answers[0], list):
                            correct_answers = [[ans] for ans in correct_answers]
                    else:
                        correct_answers = []

                    match_result = self.kb_system.matcher.match_multi(user_answer, correct_answers)
                    is_correct = match_result['matched']

                    # 规则匹配失败时，使用 LLM 二次判定
                    if not is_correct and self.plugin_config.get('llm_judge', True):
                        llm_result = await self.kb_system.llm_judge.judge_multi(
                            user_answer, correct_answers, pending.get('subject', '通用'), pending['content'])
                        if llm_result['matched']:
                            is_correct = True
                            match_result = llm_result
                else:
                    if isinstance(correct_answers, list) and len(correct_answers) > 0:
                        if isinstance(correct_answers[0], list):
                            correct_answers = correct_answers[0]
                    else:
                        correct_answers = []

                    match_result = self.kb_system.matcher.match_single(user_answer, correct_answers, question_type=q_type)
                    is_correct = match_result['matched']

                    # 规则匹配失败时，使用 LLM 二次判定
                    if not is_correct and self.plugin_config.get('llm_judge', True):
                        llm_result = await self.kb_system.llm_judge.judge_single(
                            user_answer, correct_answers, pending.get('subject', '通用'), pending['content'])
                        if llm_result['matched']:
                            is_correct = True
                            match_result = llm_result

                # 记录答题结果 C/W
                stats = await self.kb_system.db.record_answer(entry_id, user_name, is_correct, user_answer)
                await self.kb_system.db.mark_answered(pending['id'])

                # 记录到日志（独立错误处理，不影响数据库操作）
                try:
                    await self.kb_system.user_log.record_result(user_name, entry_id, is_correct)
                except Exception as log_err:
                    logger.warning(f"记录答题结果到日志失败: {log_err}")

                results.append({
                    'index': i,
                    'question': pending['content'],
                    'user_answer': user_answer,
                    'is_correct': is_correct,
                    'correct_answer': match_result['display'],
                    'stats': stats,
                    'q_type': q_type,
                    'match_result': match_result
                })

            # 构建回复
            msg = f"作答结果（共 {len(results)} 题）\n" + "=" * 30 + "\n"
            for r in results:
                status = "✅ 正确" if r['is_correct'] else "❌ 错误"
                msg += f"\n第{r['index']}题 [{r['q_type']}] {status}\n"
                msg += f"  题目: {r['question']}\n"
                msg += f"  你的答案: {r['user_answer']}\n"
                msg += f"  标准答案: {r['correct_answer']}\n"
                msg += f"  统计: 对{r['stats']['c']} 错{r['stats']['w']}\n"

                # 多填空显示逐空判定
                if r['q_type'] == '多填空' and 'blank_results' in r['match_result']:
                    mr = r['match_result']
                    msg += f"  逐空判定 ({mr.get('correct_count', 0)}/{mr.get('total_count', 0)}):\n"
                    for blank in mr['blank_results']:
                        icon = "✅" if blank['is_correct'] else "❌"
                        msg += f"    第{blank['blank_index']}空: {icon} {blank['user_answer']}\n"

            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"作答失败: {e}")
            yield event.plain_result(f"作答失败: {str(e)}")

    @filter.command("出示答案")
    async def show_answer(self, event: AstrMessageEvent):
        '''出示答案（不判定）。用法: /出示答案'''
        session_id = event.unified_msg_origin
        user_name = self._get_user_name(event)

        try:
            pending = await self.kb_system.db.get_pending(session_id)

            if not pending:
                yield event.plain_result("当前无待回答问题，请先 /开始复习错题")
                return

            entry_id = pending['entry_id']

            # 注意：total_ask 已在展示题目时 +1，出示答案时不再重复计数
            # C/W 保持不变，也不记录 exhibit（出示答案 ≠ 知识点展示）
            # 出示答案后标记为已回答，防止后续重复作答
            await self.kb_system.db.mark_answered(pending['id'])

            # 获取标准答案（pending['answers'] 已经是 list，无需再次解析）
            correct_answers = pending['answers']

            q_type = pending.get('question_type', '单填空')

            if q_type == '多填空' and correct_answers:
                if isinstance(correct_answers, list) and len(correct_answers) > 0:
                    if isinstance(correct_answers[0], list):
                        display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
                    else:
                        display = str(correct_answers[0]) if correct_answers else '无'
                else:
                    display = '无'
            else:
                display = str(correct_answers[0]) if isinstance(correct_answers, list) and len(correct_answers) > 0 else '无'

            msg = f"标准答案\n" + "=" * 30 + "\n"
            msg += f"题目: {pending['content']}\n"
            msg += f"答案: {display}\n"
            msg += f"\n解析: {pending.get('explanation', '暂无解析')}\n"
            msg += "\n(统计已更新，C/W 不变，该题已从待答队列移除)"

            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"出示答案失败: {e}")
            yield event.plain_result(f"出示答案失败: {str(e)}")

    @filter.command("开始复习知识点")
    async def start_knowledge_review(self, event: AstrMessageEvent, kb_name: str = "", count: int = 1):
        '''开始复习知识点。用法: /开始复习知识点 <复习册名> [数量N] (N<=10)'''
        session_id = event.unified_msg_origin
        user_name = self._get_user_name(event)

        if not kb_name:
            kb_name = self.plugin_config.get("default_kb", "biology_mistakes")

        if count < 1:
            count = 1
        elif count > 10:
            count = 10

        try:
            results = await self.kb_system.start_knowledge_review(kb_name, count, user_name, session_id)

            if not results:
                yield event.plain_result(f"复习册 {kb_name} 暂无可复习的知识点")
                return

            if results and 'error' in results[0]:
                yield event.plain_result(results[0]['error'])
                return

            msg = f"知识点复习 【{kb_name}】\n" + "=" * 30 + "\n"
            for item in results:
                msg += f"\n【{item['category']}】{item['content']}\n"
                # 如果是回退到题目模式，显示答案
                if item.get('is_question_fallback'):
                    ans_display = item.get('answers_display', '')
                    if ans_display:
                        msg += f"   答案: {ans_display}\n"
                    if item.get('explanation'):
                        exp = item['explanation']
                        if len(exp) > 100:
                            exp = exp[:100] + "..."
                        msg += f"   解析: {exp}\n"
                    msg += f"   (该复习册无纯知识点条目，当前展示为题目+答案模式)\n"
                else:
                    if item.get('answers'):
                        ans = item['answers']
                        if isinstance(ans, list) and len(ans) > 0:
                            if isinstance(ans[0], list):
                                msg += f"   答案: {'; '.join([o[0] for o in ans if o])}\n"
                            else:
                                msg += f"   答案: {', '.join(str(a) for a in ans)}\n"
                    if item.get('explanation'):
                        exp = item['explanation']
                        if len(exp) > 100:
                            exp = exp[:100] + "..."
                        msg += f"   解析: {exp}\n"
                msg += f"   总展示: {item['total_exhibit']} | 你的展示: {item['exhibit_you']}\n"

            msg += "\n提示: 可使用 /生成解析 为当前条目生成详细解析"
            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"开始复习知识点失败: {e}")
            yield event.plain_result(f"开始复习知识点失败: {str(e)}")

    @filter.command("搜索知识点")
    async def search_content(self, event: AstrMessageEvent, *, content: str = ""):
        '''搜索知识点。用法: /搜索知识点 <关键词>'''
        if not content or not content.strip():
            yield event.plain_result("请提供搜索关键词，如: /搜索知识点 光合作用")
            return

        try:
            results = await self.kb_system.search_content(content)

            if results and 'error' in results[0]:
                yield event.plain_result(results[0]['error'])
                return

            msg = f"搜索结果: {content}\n" + "=" * 30 + "\n"
            msg += f"共找到 {len(results)} 条结果\n"

            for i, item in enumerate(results, 1):
                msg += f"\n{i}. [{item['kb_name']}] 【{item['category']}】{item['content']}\n"
                if item.get('answers'):
                    ans = item['answers']
                    if isinstance(ans, list) and len(ans) > 0:
                        if isinstance(ans[0], list):
                            msg += f"   答案: {'; '.join([o[0] for o in ans if o])}\n"
                        else:
                            msg += f"   答案: {', '.join(str(a) for a in ans)}\n"

            yield event.plain_result(self._truncate_message(msg))

        except Exception as e:
            logger.error(f"搜索知识点失败: {e}")
            yield event.plain_result(f"搜索知识点失败: {str(e)}")

    @filter.command("生成解析")
    async def generate_explanation(self, event: AstrMessageEvent):
        '''生成解析。用法: /生成解析（在作答完毕、出示答案或复习知识点后使用）'''
        session_id = event.unified_msg_origin

        try:
            # 从已回答的 pending_questions 中获取最近一次出示的题目
            pending = await self.kb_system.db.get_latest_answered(session_id)

            if not pending:
                yield event.plain_result(
                    "当前无最近出示记录。\n"
                    "请先执行以下操作之一，再使用 /生成解析：\n"
                    "1. /开始复习错题 并 /作答\n"
                    "2. /出示答案\n"
                    "3. /开始复习知识点"
                )
                return

            entry_id = pending['entry_id']
            kb_name = pending.get('kb_name') or ''

            yield event.plain_result(f"正在为条目 {entry_id} 生成解析，请稍候...")

            result = await self.kb_system.generate_and_update_explanation(kb_name, entry_id)

            if result.get('success'):
                msg = f"解析生成成功\n" + "=" * 30 + "\n"
                msg += f"条目ID: {entry_id}\n"
                msg += f"复习册: {kb_name}\n\n"
                msg += f"解析内容:\n{result['explanation']}"
                yield event.plain_result(self._truncate_message(msg))
            else:
                error_msg = result.get('error', '未知错误')
                if '已有解析' in error_msg:
                    msg = f"该条目已有解析\n" + "=" * 30 + "\n"
                    msg += f"现有解析: {result.get('existing_explanation', '')}\n\n"
                    msg += "如需重新生成，请先手动清空该条目的解析内容"
                    yield event.plain_result(self._truncate_message(msg))
                else:
                    yield event.plain_result(f"生成解析失败: {error_msg}")

        except Exception as e:
            logger.error(f"生成解析失败: {e}")
            yield event.plain_result(f"生成解析失败: {str(e)}")

    # ==================== 保留旧命令 ====================

    @filter.command("我的统计")
    async def my_stats(self, event: AstrMessageEvent, kb_name: str = ""):
        '''查看个人答题统计。用法: /我的统计 [知识库名]'''
        user_name = self._get_user_name(event)

        try:
            if kb_name:
                # 从日志获取统计
                user_log = await self.kb_system.user_log.get_user_stats(user_name)
                log_total_ask = user_log.get('total_ask', 0)
                log_total_exhibit = user_log.get('total_exhibit', 0)

                # 从数据库获取统计
                db_total_errors = await self.kb_system.db.get_user_error_sum(kb_name, user_name)

                # 合并统计：取日志和数据库中的较大值（避免数据不一致）
                msg = f"【{user_name}】在 [{kb_name}] 的统计\n" + "=" * 30 + "\n"
                msg += f"总提问: {log_total_ask}\n"
                msg += f"总展示: {log_total_exhibit}\n"
                msg += f"总错误: {db_total_errors}\n"

                # 显示条目统计
                entries = user_log.get('entries', {})
                if entries:
                    msg += "\n各条目统计:\n"
                    for eid, stat in list(entries.items())[:10]:
                        msg += f"  {eid[:20]}...: 提问{stat['ask']} 展示{stat['exhibit']} 对{stat['c']} 错{stat['w']}\n"

                yield event.plain_result(self._truncate_message(msg))
            else:
                user_stats = await self.kb_system.user_log.get_user_stats(user_name)
                yield event.plain_result(f"{user_name} 的总统计：\n总提问: {user_stats.get('total_ask', 0)}\n总展示: {user_stats.get('total_exhibit', 0)}")

        except Exception as e:
            logger.error(f"获取统计失败: {e}")
            yield event.plain_result(f"获取统计失败: {str(e)}")

    @filter.command("重载复习册")
    async def reload_kb(self, event: AstrMessageEvent):
        '''重新初始化知识系统（管理用）'''
        try:
            await self.kb_system.initialize()
            yield event.plain_result("复习册已重新初始化")
        except Exception as e:
            logger.error(f"重载复习册失败: {e}")
            yield event.plain_result(f"重载失败: {str(e)}")

    async def terminate(self):
        if hasattr(self, 'kb_system'):
            await self.kb_system.close()
        logger.info("知识点复习插件已卸载")
