"""
高中知识点复习系统 - 异步SQLite版 + LLM智能判定（安全修复版）
支持题型：单填空、多填空、判断题、开放题
修复内容：JSON解析保护、空列表保护、LLM超时、Prompt注入防护、消息截断
"""

import aiosqlite
import json
import re
import os
import unicodedata
import asyncio
import hashlib
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from astrbot.api import logger

# ==================== 配置 ====================

LOGIC_TRUE = {'正确', '对', '是', '√', 'yes', 'true', '1', 't', '正确✓'}
LOGIC_FALSE = {'错误', '错', '否', '×', 'no', 'false', '0', 'f', '不对', '错误✗'}

VALID_SUBJECTS = {'生物', '化学', '物理', '通用'}

QUESTION_TYPES = {
    '单填空': 'single',
    '多填空': 'multi',
    '判断': 'judge',
    '开放': 'open'
}
# 注意：'选择' 题型暂未实现，如需支持请添加 match_choice 方法

# ==================== 安全工具函数 ====================

def escape_like(text: str) -> str:
    """转义 LIKE 查询中的特殊字符 % 和 _"""
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

def sanitize_for_prompt(text: str, max_length: int = 500) -> str:
    """
    清理用户输入，防止Prompt注入
    """
    if not text:
        return ""
    # 截断长度
    text = text[:max_length]
    # 转义特殊字符
    text = text.replace('{', '{{').replace('}', '}}')
    # 移除控制字符
    text = ''.join(char for char in text if unicodedata.category(char)[0] != 'C' or char in '\n\r\t')
    return text.strip()

def safe_json_loads(data: str, default=None, field_name: str = '') -> Any:
    """
    安全的JSON解析，失败返回默认值
    """
    if not data:
        return default if default is not None else []
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"JSON解析失败 [{field_name}]: {e}, data={data[:100]}...")
        return default if default is not None else []


# ==================== 用户日志管理 ====================

class UserLogManager:
    """
    用户复习日志文件管理
    日志格式: data/user_logs/<user_name>.log
    内容: JSON 格式，每行一条记录
    """

    MAX_LOG_LINES = 1000  # 日志文件最大行数，超过则轮转

    def __init__(self, data_dir: str):
        self.log_dir = os.path.join(data_dir, "user_logs")
        os.makedirs(self.log_dir, exist_ok=True)
        # 并发安全：per-user 锁防止读-改-写丢失更新
        self._user_locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_user_lock(self, user_name: str) -> asyncio.Lock:
        """获取或创建 per-user 锁"""
        async with self._global_lock:
            if user_name not in self._user_locks:
                self._user_locks[user_name] = asyncio.Lock()
            return self._user_locks[user_name]

    def _get_log_path(self, user_name: str) -> str:
        safe_name = re.sub(r'[^\w\-]', '_', user_name)
        return os.path.join(self.log_dir, f"{safe_name}.log")

    async def _ensure_log_exists(self, user_name: str):
        """确保日志文件存在，不存在则创建（异步锁保护防止并发初始化竞态）"""
        log_path = self._get_log_path(user_name)
        lock = await self._get_user_lock(user_name)
        async with lock:
            if not os.path.exists(log_path):
                with open(log_path, 'w', encoding='utf-8') as f:
                    # 写入初始空结构
                    initial = {
                        "user_name": user_name,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "total_ask": 0,
                        "total_exhibit": 0,
                        "entries": {}
                    }
                    f.write(json.dumps(initial, ensure_ascii=False) + '\n')

    def _read_log_sync(self, user_name: str) -> Dict:
        """同步读取用户日志文件（不调用 _ensure_log_exists，用于锁内读取）"""
        log_path = self._get_log_path(user_name)
        last_line = ""
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    last_line = line

        if last_line:
            return json.loads(last_line)
        return self._empty_log(user_name)

    async def _read_log(self, user_name: str) -> Dict:
        """读取用户日志（带 ensure 检查，用于锁外读取）"""
        await self._ensure_log_exists(user_name)
        return self._read_log_sync(user_name)

    def _write_log(self, user_name: str, data: Dict):
        """追加写入用户日志，并在超过行数限制时轮转（使用原子写入防止竞态条件）"""
        log_path = self._get_log_path(user_name)

        # 读取现有内容
        lines = []
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except FileNotFoundError:
            pass

        line_count = len(lines)

        # 如果超过限制，执行日志轮转：丢弃所有历史，仅保留最新状态
        # 每行都是完整 JSON 快照，无需保留历史记录
        if line_count >= self.MAX_LOG_LINES:
            new_content = json.dumps(data, ensure_ascii=False) + '\n'
        else:
            new_content = ''.join(lines) + json.dumps(data, ensure_ascii=False) + '\n'

        # 原子写入：先写临时文件，再重命名
        temp_path = log_path + '.tmp'
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(temp_path, log_path)
        except Exception:
            # 如果原子写入失败，回退到追加模式
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(data, ensure_ascii=False) + '\n')
            finally:
                # 清理临时文件
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _empty_log(self, user_name: str) -> Dict:
        return {
            "user_name": user_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_ask": 0,
            "total_exhibit": 0,
            "entries": {}
        }

    async def record_ask(self, user_name: str, entry_id: str):
        """记录提问（异步锁保护读-改-写）"""
        await self._ensure_log_exists(user_name)  # 在锁外确保文件存在
        lock = await self._get_user_lock(user_name)
        async with lock:
            log = self._read_log_sync(user_name)  # 锁内同步读取，避免重入死锁
            log["total_ask"] = log.get("total_ask", 0) + 1

            if entry_id not in log["entries"]:
                log["entries"][entry_id] = {"ask": 0, "exhibit": 0, "c": 0, "w": 0}
            log["entries"][entry_id]["ask"] += 1

            self._write_log(user_name, log)

    async def record_exhibit(self, user_name: str, entry_id: str):
        """记录展示（异步锁保护读-改-写）"""
        await self._ensure_log_exists(user_name)  # 在锁外确保文件存在
        lock = await self._get_user_lock(user_name)
        async with lock:
            log = self._read_log_sync(user_name)  # 锁内同步读取，避免重入死锁
            log["total_exhibit"] = log.get("total_exhibit", 0) + 1

            if entry_id not in log["entries"]:
                log["entries"][entry_id] = {"ask": 0, "exhibit": 0, "c": 0, "w": 0}
            log["entries"][entry_id]["exhibit"] += 1

            self._write_log(user_name, log)

    async def record_result(self, user_name: str, entry_id: str, is_correct: bool):
        """记录答题结果（异步锁保护读-改-写，只更新 c/w）"""
        await self._ensure_log_exists(user_name)  # 在锁外确保文件存在
        lock = await self._get_user_lock(user_name)
        async with lock:
            log = self._read_log_sync(user_name)  # 锁内同步读取，避免重入死锁
            if entry_id not in log["entries"]:
                log["entries"][entry_id] = {"ask": 0, "exhibit": 0, "c": 0, "w": 0}

            # 只更新 c/w，不更新 total_ask 和 ask（展示时已计数）
            if is_correct:
                log["entries"][entry_id]["c"] += 1
            else:
                log["entries"][entry_id]["w"] += 1

            self._write_log(user_name, log)

    async def get_user_stats(self, user_name: str) -> Dict:
        """获取用户统计"""
        return await self._read_log(user_name)

    async def get_entry_ask(self, user_name: str, entry_id: str) -> int:
        """获取某题目的提问次数（仅作为数据库为零时的回退数据源）"""
        log = await self._read_log(user_name)
        return log.get("entries", {}).get(entry_id, {}).get("ask", 0)

    async def get_entry_exhibit(self, user_name: str, entry_id: str) -> int:
        """获取某题目的展示次数（仅作为数据库为零时的回退数据源）"""
        log = await self._read_log(user_name)
        return log.get("entries", {}).get(entry_id, {}).get("exhibit", 0)

# ==================== LLM判定提示词 ====================

LLM_JUDGE_PROMPT_SINGLE = """你是一位专业的教师，需要判定学生的答案是否正确。

【题目信息】
学科: {subject}
题型: 单填空题
问题: {question}
标准答案: {correct_answers}
学生答案: {user_answer}

【判定要求】
1. 仅当学生答案与标准答案字面完全一致时判定为正确
2. 如果是判断题，学生答案表达"是/对/正确"或"否/错/不正确"的含义且与标准答案逻辑一致时判定为正确

【输出格式】
请严格按以下JSON格式输出：
{{
  "is_correct": true/false,
  "confidence": 0.0-1.0,
  "reason": "判定理由"
}}"""

LLM_GENERATE_EXPLANATION_PROMPT = """你是一位专业的教师，请为以下题目生成详细解析。

【学科】
{subject}

【题型】
{question_type}

【题目】
{question}

【标准答案】
{answer}

【要求】
1. 解释答案的核心概念和原理
2. 说明解题思路和关键步骤
3. 指出常见错误和易混淆点
4. 提供记忆技巧或关联知识点（如有）
5. 语言简洁，易于高中生理解
6. 解析长度控制在200-500字

请直接输出解析内容，不要包含任何前缀或后缀。"""

LLM_JUDGE_PROMPT_MULTI = """你是一位专业的教师，需要判定学生的多填空答案是否正确。

【题目信息】
学科: {subject}
题型: 多填空题（共{blank_count}个空）
问题: {question}
标准答案（按空顺序）: {correct_answers}
学生答案（按空顺序，用;分隔）: {user_answer}

【判定要求】
1. 学生答案使用";"分隔不同空，如"光反应;暗反应"
2. 每个空单独判定，仅当字面完全一致时判定为正确
3. 标准答案中"|"表示同一空的多个可接受答案
4. 输出每个空的判定结果和整体正确率

【输出格式】
请严格按以下JSON格式输出：
{{
  "blank_results": [
    {{"blank_index": 1, "is_correct": true/false, "matched_answer": "匹配到的标准答案"}},
    {{"blank_index": 2, "is_correct": true/false, "matched_answer": "匹配到的标准答案"}}
  ],
  "correct_count": 2,
  "total_count": 2,
  "accuracy": 1.0,
  "is_correct": true/false,
  "confidence": 0.0-1.0,
  "reason": "判定理由"
}}"""

# ==================== 数据库管理 ====================

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    @asynccontextmanager
    async def _get_conn(self):
        """获取数据库连接（每次操作创建独立连接，避免并发竞态条件）"""
        conn = await aiosqlite.connect(self.db_path, timeout=60.0)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
    
    async def close(self):
        """关闭数据库（无需手动关闭连接，每次操作后自动关闭）"""
        pass
    
    async def init_db(self):
        """初始化数据库结构"""
        async with self._get_conn() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    kb_name TEXT NOT NULL,
                    category TEXT,
                    subject TEXT CHECK(subject IN ('生物', '化学', '物理', '通用')),
                    question_type TEXT DEFAULT '单填空',
                    is_question BOOLEAN,
                    content TEXT,
                    answers TEXT,
                    explanation TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    entry_id TEXT PRIMARY KEY,
                    total_ask INTEGER DEFAULT 0,
                    total_exhibit INTEGER DEFAULT 0,
                    last_access TIMESTAMP,
                    FOREIGN KEY (entry_id) REFERENCES entries(id)
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT,
                    user_name TEXT,
                    w INTEGER DEFAULT 0,
                    c INTEGER DEFAULT 0,
                    last_answer TEXT,
                    last_time TIMESTAMP,
                    UNIQUE(entry_id, user_name),
                    FOREIGN KEY (entry_id) REFERENCES entries(id)
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pending_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    kb_name TEXT,
                    entry_id TEXT,
                    answers TEXT,
                    explanation TEXT,
                    subject TEXT,
                    content TEXT,
                    question_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    answered BOOLEAN DEFAULT 0
                )
            ''')
            
            # 新增：用户复习记录表（支持 ask_you/exhibit_you）
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_review_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT,
                    user_name TEXT,
                    ask_you INTEGER DEFAULT 0,
                    exhibit_you INTEGER DEFAULT 0,
                    last_ask_time TIMESTAMP,
                    last_exhibit_time TIMESTAMP,
                    UNIQUE(entry_id, user_name),
                    FOREIGN KEY (entry_id) REFERENCES entries(id)
                )
            ''')
            
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_kb ON entries(kb_name)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_subject ON entries(subject)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_session ON pending_questions(session_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_review ON user_review_stats(user_name, entry_id)')
            
            # 创建 FTS5 全文搜索虚拟表（提升搜索性能）
            await conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                    content, answers, explanation,
                    tokenize='unicode61'
                )
            ''')
    
    async def add_entry(self, entry: Dict) -> bool:
        """添加条目"""
        try:
            async with self._get_conn() as conn:
                await conn.execute('''
                    INSERT OR REPLACE INTO entries 
                    (id, kb_name, category, subject, question_type, is_question, content, answers, explanation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    entry['id'],
                    entry.get('kb_name', ''),
                    entry.get('category', ''),
                    entry.get('subject', '通用'),
                    entry.get('question_type', '单填空'),
                    entry.get('is_question', False),
                    entry.get('content', ''),
                    json.dumps(entry.get('answers', []), ensure_ascii=False),
                    entry.get('explanation', '')
                ))
                
                # 同步到 FTS5 表（使用 rowid 精确定位，避免 content 重复导致的多条更新）
                try:
                    await conn.execute('''
                        INSERT OR REPLACE INTO entries_fts(rowid, content, answers, explanation)
                        VALUES ((SELECT rowid FROM entries WHERE id = ?), ?, ?, ?)
                    ''', (
                        entry.get('id'),
                        entry.get('content', ''),
                        json.dumps(entry.get('answers', []), ensure_ascii=False),
                        entry.get('explanation', '')
                    ))
                except Exception as fts_err:
                    logger.warning(f"FTS5 同步失败，条目 {entry.get('id')} 将无法被搜索: {fts_err}")
                
                await conn.execute('''
                    INSERT OR IGNORE INTO stats (entry_id, total_ask, total_exhibit)
                    VALUES (?, 0, 0)
                ''', (entry['id'],))
            return True
        except Exception as e:
            logger.error(f"Add entry error: {e}")
            return False
    
    async def get_entries(self, kb_name: str, is_question: Optional[bool] = None, 
                         limit: int = 100) -> List[Dict]:
        """获取条目"""
        async with self._get_conn() as conn:
            sql = 'SELECT * FROM entries WHERE kb_name = ?'
            params = [kb_name]
            
            if is_question is not None:
                sql += ' AND is_question = ?'
                params.append(is_question)
            
            sql += ' ORDER BY RANDOM() LIMIT ?'
            params.append(limit)
            
            async with conn.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]
    
    async def get_entry_count_by_kb(self, kb_name: str) -> int:
        """获取指定知识库的条目数量"""
        async with self._get_conn() as conn:
            async with conn.execute('SELECT COUNT(*) as cnt FROM entries WHERE kb_name = ?', (kb_name,)) as cursor:
                row = await cursor.fetchone()
                return row['cnt'] if row else 0
    
    def _row_to_dict(self, row: aiosqlite.Row) -> Dict:
        """行转字典（修复：安全解析JSON）"""
        d = dict(row)
        # 安全解析answers，带字段名以便追踪
        d['answers'] = safe_json_loads(d.get('answers'), [], field_name='answers')
        return d
    
    async def increment_stat(self, entry_id: str, field: str) -> int:
        """增加统计计数"""
        # 白名单验证，防止SQL注入
        allowed_fields = {'total_ask', 'total_exhibit'}
        if field not in allowed_fields:
            raise ValueError(f"Invalid stat field: {field}")
        # 安全：field 已通过上方白名单严格校验，此处使用 f-string 是安全的
        async with self._get_conn() as conn:
            await conn.execute(f'''
                UPDATE stats SET {field} = {field} + 1, last_access = CURRENT_TIMESTAMP
                WHERE entry_id = ?
            ''', (entry_id,))
            
            async with conn.execute('SELECT * FROM stats WHERE entry_id = ?', (entry_id,)) as cursor:
                row = await cursor.fetchone()
                return row[field] if row else 0
    
    async def get_stat(self, entry_id: str) -> Dict:
        """获取统计"""
        async with self._get_conn() as conn:
            async with conn.execute('SELECT * FROM stats WHERE entry_id = ?', (entry_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else {'total_ask': 0, 'total_exhibit': 0}
    
    async def record_answer(self, entry_id: str, user_name: str, is_correct: bool, 
                           answer_text: str = '') -> Dict:
        """记录答题"""
        async with self._get_conn() as conn:
            await conn.execute('''
                INSERT INTO user_records (entry_id, user_name, w, c, last_answer, last_time)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(entry_id, user_name) DO UPDATE SET
                    w = w + excluded.w,
                    c = c + excluded.c,
                    last_answer = excluded.last_answer,
                    last_time = excluded.last_time
            ''', (
                entry_id, 
                user_name, 
                0 if is_correct else 1, 
                1 if is_correct else 0,
                answer_text[:1000]  # 限制长度
            ))
            
            # 获取用户个人统计
            async with conn.execute('''
                SELECT w, c FROM user_records WHERE entry_id = ? AND user_name = ?
            ''', (entry_id, user_name)) as cursor:
                row = await cursor.fetchone()
                user_w = row['w'] if row else 0
                user_c = row['c'] if row else 0
            
            # 获取该题总统计（所有用户）
            async with conn.execute('''
                SELECT SUM(c) as total_c, SUM(w) as total_w FROM user_records WHERE entry_id = ?
            ''', (entry_id,)) as cursor:
                total_row = await cursor.fetchone()
                total_c = total_row['total_c'] if total_row and total_row['total_c'] else 0
                total_w = total_row['total_w'] if total_row and total_row['total_w'] else 0
            
            return {
                'w': user_w, 
                'c': user_c,
                'total_w': total_w,
                'total_c': total_c
            }
    
    async def get_user_stats(self, entry_id: str, user_name: str) -> Optional[Dict]:
        """获取用户统计"""
        async with self._get_conn() as conn:
            async with conn.execute('''
                SELECT * FROM user_records WHERE entry_id = ? AND user_name = ?
            ''', (entry_id, user_name)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
    
    async def get_user_error_sum(self, kb_name: str, user_name: str) -> int:
        """获取用户在知识库的总错误次数"""
        async with self._get_conn() as conn:
            async with conn.execute('''
                SELECT SUM(ur.w) as total_errors
                FROM user_records ur
                JOIN entries e ON ur.entry_id = e.id
                WHERE e.kb_name = ? AND ur.user_name = ?
            ''', (kb_name, user_name)) as cursor:
                row = await cursor.fetchone()
                return row['total_errors'] or 0
    
    async def _cleanup_expired(self, conn):
        """清理过期的 pending_questions 记录"""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        await conn.execute('DELETE FROM pending_questions WHERE expires_at < ?', (now,))

    async def clear_pending(self, session_id: str):
        """清理指定 session 的所有待回答记录"""
        async with self._get_conn() as conn:
            await conn.execute('DELETE FROM pending_questions WHERE session_id = ?', (session_id,))

    async def add_pending(self, session_id: str, entry: Dict, expires_minutes: int = 30, answered: bool = False) -> int:
        """添加待回答问题（仅插入，不清理旧记录）
        
        参数:
            session_id: 会话ID
            entry: 条目字典
            expires_minutes: 过期时间（分钟）
            answered: 是否已回答（默认False）
        """
        expires = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
        expires_str = expires.strftime('%Y-%m-%dT%H:%M:%S+00:00')
        async with self._get_conn() as conn:
            cursor = await conn.execute('''
                INSERT INTO pending_questions 
                (session_id, kb_name, entry_id, answers, explanation, subject, content, question_type, expires_at, answered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id,
                entry.get('kb_name', ''),
                entry['id'],
                json.dumps(entry.get('answers', []), ensure_ascii=False),
                entry.get('explanation', ''),
                entry.get('subject', '通用'),
                entry.get('content', ''),
                entry.get('question_type', '单填空'),
                expires_str,
                1 if answered else 0
            ))
            return cursor.lastrowid
    
    async def get_pending(self, session_id: str) -> Optional[Dict]:
        """获取待回答问题（自动清理过期）"""
        async with self._get_conn() as conn:
            # 清理过期记录
            await self._cleanup_expired(conn)

            # 获取最新未回答
            async with conn.execute('''
                SELECT * FROM pending_questions 
                WHERE session_id = ? AND answered = 0
                ORDER BY created_at ASC LIMIT 1
            ''', (session_id,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_dict(row) if row else None
    
    async def get_all_pending(self, session_id: str) -> List[Dict]:
        """获取所有待回答问题（自动清理过期）"""
        async with self._get_conn() as conn:
            # 清理过期记录
            await self._cleanup_expired(conn)

            # 获取所有未回答
            async with conn.execute('''
                SELECT * FROM pending_questions 
                WHERE session_id = ? AND answered = 0
                ORDER BY created_at ASC
            ''', (session_id,)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]
    
    async def get_latest_answered(self, session_id: str) -> Optional[Dict]:
        """获取最近一次已回答的 pending 记录（用于 /生成解析）"""
        async with self._get_conn() as conn:
            # 清理过期记录
            await self._cleanup_expired(conn)
            # 获取最近一次已回答的记录
            async with conn.execute('''
                SELECT * FROM pending_questions 
                WHERE session_id = ? AND answered = 1
                ORDER BY created_at DESC LIMIT 1
            ''', (session_id,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_dict(row) if row else None
    
    async def mark_answered(self, pending_id: int):
        """标记为已回答"""
        async with self._get_conn() as conn:
            await conn.execute('UPDATE pending_questions SET answered = 1 WHERE id = ?', (pending_id,))

    async def increment_user_review_ask(self, entry_id: str, user_name: str) -> Dict:
        """增加用户复习提问计数 (ask_you + 1)"""
        async with self._get_conn() as conn:
            await conn.execute('''
                INSERT INTO user_review_stats (entry_id, user_name, ask_you, last_ask_time)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(entry_id, user_name) DO UPDATE SET
                    ask_you = ask_you + 1,
                    last_ask_time = excluded.last_ask_time
            ''', (entry_id, user_name, datetime.now(timezone.utc).isoformat()))

            async with conn.execute('''
                SELECT ask_you, exhibit_you FROM user_review_stats 
                WHERE entry_id = ? AND user_name = ?
            ''', (entry_id, user_name)) as cursor:
                row = await cursor.fetchone()
                return {'ask_you': row['ask_you'], 'exhibit_you': row['exhibit_you']} if row else {'ask_you': 1, 'exhibit_you': 0}

    async def increment_user_review_exhibit(self, entry_id: str, user_name: str) -> Dict:
        """增加用户复习展示计数 (exhibit_you + 1)"""
        async with self._get_conn() as conn:
            await conn.execute('''
                INSERT INTO user_review_stats (entry_id, user_name, exhibit_you, last_exhibit_time)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(entry_id, user_name) DO UPDATE SET
                    exhibit_you = exhibit_you + 1,
                    last_exhibit_time = excluded.last_exhibit_time
            ''', (entry_id, user_name, datetime.now(timezone.utc).isoformat()))

            async with conn.execute('''
                SELECT ask_you, exhibit_you FROM user_review_stats 
                WHERE entry_id = ? AND user_name = ?
            ''', (entry_id, user_name)) as cursor:
                row = await cursor.fetchone()
                return {'ask_you': row['ask_you'], 'exhibit_you': row['exhibit_you']} if row else {'ask_you': 0, 'exhibit_you': 1}

    async def get_user_review_stats(self, entry_id: str, user_name: str) -> Optional[Dict]:
        """获取用户复习统计"""
        async with self._get_conn() as conn:
            async with conn.execute('''
                SELECT * FROM user_review_stats WHERE entry_id = ? AND user_name = ?
            ''', (entry_id, user_name)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_user_review_stats_batch(self, entry_ids: List[str], user_name: str) -> Dict[str, Dict]:
        """批量获取用户复习统计"""
        if not entry_ids:
            return {}
        placeholders = ','.join(['?'] * len(entry_ids))
        async with self._get_conn() as conn:
            async with conn.execute(f'''
                SELECT * FROM user_review_stats 
                WHERE entry_id IN ({placeholders}) AND user_name = ?
            ''', (*entry_ids, user_name)) as cursor:
                rows = await cursor.fetchall()
                return {row['entry_id']: dict(row) for row in rows}

    async def get_user_records_batch(self, entry_ids: List[str], user_name: str) -> Dict[str, Dict]:
        """批量获取用户答题记录"""
        if not entry_ids:
            return {}
        placeholders = ','.join(['?'] * len(entry_ids))
        async with self._get_conn() as conn:
            async with conn.execute(f'''
                SELECT * FROM user_records 
                WHERE entry_id IN ({placeholders}) AND user_name = ?
            ''', (*entry_ids, user_name)) as cursor:
                rows = await cursor.fetchall()
                return {row['entry_id']: dict(row) for row in rows}

    async def get_stats_batch(self, entry_ids: List[str]) -> Dict[str, Dict]:
        """批量获取统计"""
        if not entry_ids:
            return {}
        placeholders = ','.join(['?'] * len(entry_ids))
        async with self._get_conn() as conn:
            async with conn.execute(f'''
                SELECT * FROM stats WHERE entry_id IN ({placeholders})
            ''', entry_ids) as cursor:
                rows = await cursor.fetchall()
                return {row['entry_id']: dict(row) for row in rows}

    async def search_entries(self, content: str) -> List[Dict]:
        """搜索条目（使用 FTS5 全文搜索）"""
        raw_term = content.strip()
        if not raw_term:
            return []
        
        async with self._get_conn() as conn:
            try:
                # FTS5 使用原始搜索词，不需要 LIKE 转义
                # JOIN entries 表获取完整字段（id, kb_name, category, subject, question_type 等）
                async with conn.execute('''
                    SELECT e.id, e.kb_name, e.category, e.subject, e.question_type,
                           e.is_question, e.content, e.answers, e.explanation
                    FROM entries_fts f
                    JOIN entries e ON e.rowid = f.rowid
                    WHERE f MATCH ?
                    ORDER BY f.rank
                    LIMIT 50
                ''', (raw_term,)) as cursor:
                    fts_rows = await cursor.fetchall()
                    if fts_rows:
                        # 从 FTS5 结果构建完整条目字典
                        results = []
                        for row in fts_rows:
                            d = {
                                'id': row[0],
                                'kb_name': row[1],
                                'category': row[2],
                                'subject': row[3],
                                'question_type': row[4],
                                'is_question': row[5],
                                'content': row[6],
                                'answers': safe_json_loads(row[7], [], field_name='answers'),
                                'explanation': row[8]
                            }
                            results.append(d)
                        return results
            except Exception as e:
                logger.warning(f"FTS5 搜索失败，回退到 LIKE 查询: {e}")
            
            # 回退到 LIKE 查询时才需要转义
            search_pattern = f"%{escape_like(raw_term)}%"
            async with conn.execute('''
                SELECT * FROM entries 
                WHERE content LIKE ? ESCAPE '\\' OR answers LIKE ? ESCAPE '\\' OR explanation LIKE ? ESCAPE '\\'
                ORDER BY kb_name, category
                LIMIT 50
            ''', (search_pattern, search_pattern, search_pattern)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]

    async def get_entry_by_id(self, entry_id: str) -> Optional[Dict]:
        """根据 ID 获取单个条目"""
        async with self._get_conn() as conn:
            async with conn.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_dict(row) if row else None

    async def update_entry_explanation(self, entry_id: str, explanation: str) -> bool:
        """仅更新条目的解析字段"""
        try:
            async with self._get_conn() as conn:
                await conn.execute(
                    'UPDATE entries SET explanation = ? WHERE id = ?',
                    (explanation, entry_id)
                )
                # 同步到 FTS5（使用 rowid 精确定位，避免 content 重复导致的多条更新）
                try:
                    await conn.execute(
                        'UPDATE entries_fts SET explanation = ? WHERE rowid = (SELECT rowid FROM entries WHERE id = ?)',
                        (explanation, entry_id)
                    )
                except Exception:
                    pass
                return True
        except Exception as e:
            logger.error(f"Update explanation error: {e}")
            return False


# ==================== 智能匹配引擎 ====================

class AnswerMatcher:
    def __init__(self):
        pass
    
    def _normalize(self, text: str) -> str:
        """基础规范化"""
        if not text:
            return ""
        text = unicodedata.normalize('NFKC', text)
        # 保留连字符，避免 "1-2" 和 "12" 误判
        return text.lower().replace(' ', '').replace('\u3000', '')
    
    def match_single(self, user_answer: str, correct_answers: List[str],
                     subject: str = '通用', question_type: str = '单填空') -> Dict[str, Any]:
        """
        单填空匹配（简化版：仅保留直接匹配和简单是非规则）
        """
        # 空答案检查 - 注意：如果 correct_answers 因数据损坏为空列表，会走此分支
        # 调用方应确保数据完整性
        if not correct_answers:
            return {'matched': True, 'rule': 'R0:开放题', 'confidence': 1.0, 'display': '（开放）'}

        if not user_answer or not user_answer.strip():
            return {
                'matched': False,
                'rule': 'R9:空答案',
                'confidence': 0.0,
                'display': correct_answers[0] if correct_answers else '无'
            }

        user = user_answer.strip()
        best_confidence = 0.0
        best_rule = "未命中"
        best_answer = correct_answers[0] if correct_answers else ''

        for correct in correct_answers:
            confidence = 0.0
            rule = ""

            u_norm = self._normalize(user)
            c_norm = self._normalize(correct)

            if u_norm == c_norm:
                confidence = 1.0
                rule = "R1:完全匹配"
            # 逻辑真/假规则仅在判断题时启用，防止非判断题误判
            elif question_type == '判断':
                if u_norm in LOGIC_TRUE and c_norm in LOGIC_TRUE:
                    confidence = 1.0
                    rule = "R7:逻辑真"
                elif u_norm in LOGIC_FALSE and c_norm in LOGIC_FALSE:
                    confidence = 1.0
                    rule = "R7:逻辑假"

            if confidence > best_confidence:
                best_confidence = confidence
                best_rule = rule
                best_answer = correct

        return {
            'matched': best_confidence > 0,  # 有匹配即为正确（只有 0.0 或 1.0）
            'confidence': best_confidence,
            'rule': best_rule,
            'display': best_answer
        }
    
    def match_multi(self, user_answer: str, correct_answers: List[List[str]], 
                    subject: str = '通用') -> Dict[str, Any]:
        """
        多填空匹配（修复：智能数量不匹配处理）
        """
        if not correct_answers:
            return {
                'matched': True, 
                'rule': 'R0:开放题', 
                'confidence': 1.0, 
                'display': '（开放）',
                'blank_results': [],
                'correct_count': 0,
                'total_count': 0,
                'accuracy': 1.0
            }
        
        if not user_answer:
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'R9:空答案',
                'display': display,
                'blank_results': [],
                'correct_count': 0,
                'total_count': len(correct_answers),
                'accuracy': 0.0
            }
        
        # 解析用户答案
        user_answers = [a.strip() for a in user_answer.split(';') if a.strip()]
        blank_count = len(correct_answers)
        
        # 智能处理数量不匹配
        if len(user_answers) < blank_count:
            # 补充空字符串
            user_answers.extend([''] * (blank_count - len(user_answers)))
        elif len(user_answers) > blank_count:
            # 合并多余答案到最后一个空
            if blank_count > 1:
                extra = ';'.join(user_answers[blank_count-1:])
                user_answers = user_answers[:blank_count-1] + [extra]
            else:
                # 只有一个空但答了多个，合并
                user_answers = [';'.join(user_answers)]
        
        # 逐空匹配
        blank_results = []
        correct_count = 0
        total_confidence = 0.0
        
        for i, (user_ans, correct_opts) in enumerate(zip(user_answers, correct_answers)):
            if not correct_opts:  # 该空无标准答案
                result = {'matched': True, 'confidence': 1.0, 'rule': 'R0:开放', 'display': user_ans}
            else:
                result = self.match_single(user_ans, correct_opts, subject)
            
            blank_results.append({
                'blank_index': i + 1,
                'is_correct': result['matched'],
                'user_answer': user_ans,
                'matched_answer': result['display'],
                'confidence': result['confidence'],
                'rule': result['rule']
            })
            if result['matched']:
                correct_count += 1
            total_confidence += result['confidence']
        
        accuracy = correct_count / blank_count if blank_count > 0 else 0
        avg_confidence = total_confidence / blank_count if blank_count > 0 else 0
        
        # 全部正确才算正确，但置信度按平均计算
        is_correct = (correct_count == blank_count)
        final_confidence = avg_confidence * (0.5 + 0.5 * accuracy)
        
        display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
        
        return {
            'matched': is_correct,
            'confidence': final_confidence,
            'rule': f'多填空:{correct_count}/{blank_count}正确' if not is_correct else '多填空:全部正确',
            'display': display,
            'blank_results': blank_results,
            'correct_count': correct_count,
            'total_count': blank_count,
            'accuracy': accuracy
        }

# ==================== LLM判定器（修复版） ====================

class LLMJudge:
    """LLM智能判定器（修复：超时保护、Prompt注入防护）"""
    
    def __init__(self, context=None, threshold=0.85):
        self.context = context
        self.threshold = threshold
    
    async def judge_single(self, user_answer: str, correct_answers: List[str], 
                          subject: str, question: str) -> Dict[str, Any]:
        """单填空LLM判定（修复：超时、异常处理、输入清理）"""
        if not self.context or not correct_answers:
            return {
                'matched': False, 
                'confidence': 0, 
                'rule': 'LLM:未配置', 
                'display': correct_answers[0] if correct_answers else ''
            }
        
        try:
            prov = self.context.get_using_provider()
            if not prov:
                return {
                    'matched': False, 
                    'confidence': 0, 
                    'rule': 'LLM:无可用模型', 
                    'display': correct_answers[0]
                }
            
            # 清理输入，防止注入
            safe_question = sanitize_for_prompt(question, 1000)
            safe_user = sanitize_for_prompt(user_answer, 500)
            safe_correct = [sanitize_for_prompt(c, 200) for c in correct_answers]
            
            prompt = LLM_JUDGE_PROMPT_SINGLE.format(
                subject=sanitize_for_prompt(subject, 50),
                question=safe_question,
                correct_answers=' / '.join(safe_correct),
                user_answer=safe_user
            )
            
            # 添加超时保护
            llm_resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=prompt,
                    system_prompt="你是一位严谨的教师，专门判定学生答案的正确性。请只输出JSON格式结果。"
                ),
                timeout=120.0  # 2分钟超时
            )
            
            response_text = llm_resp.completion_text.strip()
            
            # 提取JSON（使用 find/rfind 策略，支持嵌套对象）
            start = response_text.find('{')
            end = response_text.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = response_text[start:end+1]
            else:
                json_str = response_text
            
            result = json.loads(json_str)
            
            is_correct = result.get('is_correct', False)
            confidence = result.get('confidence', 0.0)
            reason = result.get('reason', 'LLM判定')
            
            return {
                'matched': is_correct and confidence >= self.threshold,
                'confidence': confidence,
                'rule': f"LLM:{reason}",
                'display': correct_answers[0] if correct_answers else ''
            }
            
        except asyncio.TimeoutError:
            logger.warning("LLM判定超时")
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:超时',
                'display': correct_answers[0] if correct_answers else ''
            }
        except json.JSONDecodeError as e:
            logger.error(f"LLM返回非JSON: {e}")
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:格式错误',
                'display': correct_answers[0] if correct_answers else ''
            }
        except Exception as e:
            logger.error(f"LLM判定错误: {e}")
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:异常',
                'display': correct_answers[0] if correct_answers else ''
            }
    
    async def generate_explanation(self, subject: str, question_type: str, 
                                    question: str, answer: str) -> Optional[str]:
        """调用 LLM 生成题目解析"""
        if not self.context:
            logger.warning("LLM 未配置，无法生成解析")
            return None
        
        try:
            prov = self.context.get_using_provider()
            if not prov:
                logger.warning("无可用 LLM 模型，无法生成解析")
                return None
            
            # 清理输入，防止注入
            safe_subject = sanitize_for_prompt(subject, 50)
            safe_q_type = sanitize_for_prompt(question_type, 50)
            safe_question = sanitize_for_prompt(question, 1000)
            safe_answer = sanitize_for_prompt(answer, 500)
            
            # 检查清理后的内容是否为空
            if not safe_question or not safe_answer:
                logger.warning(f"题目或答案为空，无法生成解析。question: '{safe_question}', answer: '{safe_answer}'")
                return None
            
            prompt = LLM_GENERATE_EXPLANATION_PROMPT.format(
                subject=safe_subject,
                question_type=safe_q_type,
                question=safe_question,
                answer=safe_answer
            )
            
            logger.debug(f"LLM 生成解析 prompt 长度: {len(prompt)}")
            
            # 添加超时保护和重试
            max_retries = 2
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    llm_resp = await asyncio.wait_for(
                        prov.text_chat(
                            prompt=prompt,
                            system_prompt="你是一位专业的高中教师，擅长为学生编写清晰、详细的题目解析。"
                        ),
                        timeout=120.0  # 2分钟超时
                    )
                    
                    logger.debug(f"LLM 响应对象属性: {dir(llm_resp)}")
                    response_text = llm_resp.completion_text.strip()
                    
                    if not response_text:
                        logger.warning(f"LLM 返回空解析 (尝试 {attempt + 1}/{max_retries})，响应对象: {llm_resp}")
                        last_error = "LLM 返回空响应"
                        continue
                    
                    logger.info(f"LLM 生成解析成功，长度: {len(response_text)}")
                    return response_text
                    
                except asyncio.TimeoutError:
                    logger.warning(f"LLM 生成解析超时 (尝试 {attempt + 1}/{max_retries})")
                    last_error = "LLM 调用超时"
                    continue
            
            logger.error(f"LLM 生成解析失败，已重试 {max_retries} 次: {last_error}")
            return None
            
        except Exception as e:
            logger.error(f"LLM 生成解析错误: {e}")
            return None
    
    async def judge_multi(self, user_answer: str, correct_answers: List[List[str]], 
                          subject: str, question: str) -> Dict[str, Any]:
        """多填空LLM判定（修复：超时、异常处理）"""
        if not self.context or not correct_answers:
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers]) if correct_answers else '（开放题）'
            return {
                'matched': not correct_answers,  # 空答案视为开放题，判定为正确
                'confidence': 1.0 if not correct_answers else 0, 
                'rule': 'LLM:未配置' if self.context else 'R0:开放题', 
                'display': display,
                'blank_results': [],
                'correct_count': 0,
                'total_count': len(correct_answers) if correct_answers else 0,
                'accuracy': 1.0 if not correct_answers else 0.0
            }
        
        try:
            prov = self.context.get_using_provider()
            if not prov:
                display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
                return {
                    'matched': False, 
                    'confidence': 0, 
                    'rule': 'LLM:无可用模型', 
                    'display': display,
                    'blank_results': [],
                    'correct_count': 0,
                    'total_count': len(correct_answers),
                    'accuracy': 0.0
                }
            
            # 格式化标准答案
            formatted_answers = []
            for i, opts in enumerate(correct_answers, 1):
                safe_opts = [sanitize_for_prompt(o, 100) for o in opts]
                formatted_answers.append(f"第{i}空: {' / '.join(safe_opts)}")
            
            safe_question = sanitize_for_prompt(question, 1000)
            safe_user = sanitize_for_prompt(user_answer, 800)
            
            prompt = LLM_JUDGE_PROMPT_MULTI.format(
                subject=sanitize_for_prompt(subject, 50),
                blank_count=len(correct_answers),
                question=safe_question,
                correct_answers='; '.join(formatted_answers),
                user_answer=safe_user
            )
            
            llm_resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=prompt,
                    system_prompt="你是一位严谨的教师，专门判定多填空题。请只输出JSON格式结果。"
                ),
                timeout=120.0  # 2分钟超时
            )
            
            response_text = llm_resp.completion_text.strip()
            
            # 提取JSON（使用 find/rfind 策略，支持嵌套对象）
            start = response_text.find('{')
            end = response_text.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = response_text[start:end+1]
            else:
                json_str = response_text
            
            result = json.loads(json_str)
            
            is_correct = result.get('is_correct', False)
            confidence = result.get('confidence', 0.0)
            reason = result.get('reason', 'LLM多填空判定')
            blank_results = result.get('blank_results', [])
            accuracy = result.get('accuracy', 0.0)
            
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
            
            return {
                'matched': is_correct and confidence >= self.threshold,
                'confidence': confidence,
                'rule': f"LLM:{reason}",
                'display': display,
                'blank_results': blank_results,
                'correct_count': result.get('correct_count', 0),
                'total_count': result.get('total_count', len(correct_answers)),
                'accuracy': accuracy
            }
            
        except asyncio.TimeoutError:
            logger.warning("LLM多填空判定超时")
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:超时',
                'display': display,
                'blank_results': [],
                'correct_count': 0,
                'total_count': len(correct_answers),
                'accuracy': 0.0
            }
        except json.JSONDecodeError as e:
            logger.error(f"LLM多填空返回非JSON: {e}")
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:格式错误',
                'display': display,
                'blank_results': [],
                'correct_count': 0,
                'total_count': len(correct_answers),
                'accuracy': 0.0
            }
        except Exception as e:
            logger.error(f"LLM多填空判定错误: {e}")
            display = '; '.join([opts[0] if opts else '' for opts in correct_answers])
            return {
                'matched': False,
                'confidence': 0,
                'rule': 'LLM:异常',
                'display': display,
                'blank_results': [],
                'correct_count': 0,
                'total_count': len(correct_answers),
                'accuracy': 0.0
            }
    

# ==================== 主系统（修复版） ====================

class KnowledgeSystem:
    def __init__(self, data_dir: str, context=None, config: Dict = None):
        self.data_dir = data_dir
        self.db = DatabaseManager(os.path.join(data_dir, "knowledge.db"))
        self.matcher = AnswerMatcher()
        threshold = (config or {}).get('llm_threshold', 0.85)
        self.llm_judge = LLMJudge(context, threshold)
        self.user_log = UserLogManager(data_dir)
        self.config = config or {}
    
    async def initialize(self):
        """异步初始化"""
        await self.db.init_db()
        # 从 settings.txt 配置的 txt 文件导入数据
        await self._import_from_settings()
    
    async def _import_from_settings(self):
        """从 settings.txt 配置的 txt 文件导入数据"""
        try:
            # 读取 settings.txt
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.txt")
            if not os.path.exists(settings_path):
                logger.warning("settings.txt 不存在，跳过数据导入")
                return
            
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(settings_path, encoding='utf-8')
            
            if not cfg.has_section('DATABASE'):
                logger.warning("settings.txt 中未找到 [DATABASE] 段")
                return
            
            files_str = cfg.get('DATABASE', 'FILES', fallback='')
            shownames_str = cfg.get('DATABASE', 'SHOWNAMES', fallback='')
            
            files = [f.strip('"').strip("'").strip() for f in files_str.split(',') if f.strip()]
            shownames = [n.strip('"').strip("'").strip() for n in shownames_str.split(',') if n.strip()]
            
            if not files:
                logger.info("settings.txt 中未配置 FILES，跳过数据导入")
                return
            
            # 获取插件目录路径（优先使用 Docker 挂载路径）
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            
            imported_count = 0
            for i, fname in enumerate(files):
                # 构建文件路径
                file_path = os.path.join(plugin_dir, fname)
                if not os.path.exists(file_path):
                    logger.warning(f"复习册文件不存在: {file_path}")
                    continue
                
                # 优先使用 SHOWNAMES 作为 kb_name，如果没有则使用文件名
                kb_name = shownames[i] if i < len(shownames) else os.path.splitext(fname)[0]
                
                # 读取并导入文件
                success = await self._import_txt_file(file_path, kb_name)
                imported_count += success
            
            if imported_count > 0:
                logger.info(f"成功从 {imported_count} 个文件导入条目")
            else:
                logger.info("未有新条目导入数据库")
                
        except Exception as e:
            logger.error(f"从 settings.txt 导入数据失败: {e}")
    
    async def get_all_kb_names(self) -> List[str]:
        """从 settings.txt 获取所有复习册名称"""
        try:
            settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.txt")
            if not os.path.exists(settings_path):
                logger.warning("settings.txt 不存在")
                return []
            
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(settings_path, encoding='utf-8')
            
            if not cfg.has_section('DATABASE'):
                logger.warning("settings.txt 中未找到 [DATABASE] 段")
                return []
            
            shownames_str = cfg.get('DATABASE', 'SHOWNAMES', fallback='')
            shownames = [n.strip('"').strip("'").strip() for n in shownames_str.split(',') if n.strip()]
            
            return shownames
        except Exception as e:
            logger.error(f"读取复习册列表失败: {e}")
            return []
    
    async def _import_txt_file(self, file_path: str, kb_name: str) -> int:
        """
        从 txt 文件导入数据到数据库
        返回成功导入的条目数
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 按空行分割为多个条目
            entries_text = content.split('\n\n')
            
            # 检查是否已导入过（避免重复导入导致超时）
            existing_count = await self.db.get_entry_count_by_kb(kb_name)
            if existing_count > 0:
                logger.info(f"知识库 '{kb_name}' 已存在 {existing_count} 个条目，跳过重复导入")
                return 0
            
            imported = 0
            total = len([e for e in entries_text if e.strip()])
            logger.info(f"开始导入 {os.path.basename(file_path)}，共 {total} 个条目...")
            
            for idx, entry_text in enumerate(entries_text, 1):
                if not entry_text.strip():
                    continue
                
                entry = self.parse_entry(entry_text, kb_name)
                if entry.get('valid'):
                    success = await self.db.add_entry(entry)
                    if success:
                        imported += 1
                        # 每导入 10 条记录打印一次进度
                        if imported % 10 == 0:
                            logger.info(f"  已导入 {imported}/{total} 条...")
            
            logger.info(f"从 {os.path.basename(file_path)} 成功导入 {imported} 个条目")
            return imported
            
        except Exception as e:
            logger.error(f"导入文件 {file_path} 失败: {e}")
            return 0
    
    def parse_entry(self, raw_text: str, kb_name: str = '') -> Dict:
        """
        解析条目（支持多题型，修复：健壮性增强）
        """
        lines = [l.rstrip() for l in raw_text.split('\n') if l.strip()]
        if not lines:
            return {'valid': False, 'error': '空条目'}
        
        entry = {
            'id': '',
            'kb_name': kb_name,
            'category': '未分类',
            'subject': '通用',
            'question_type': '单填空',
            'is_question': False,
            'content': '',
            'answers': [],
            'explanation': '',
            'valid': False  # 默认无效，成功解析后设为True
        }
        
        content_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            line_s = line.strip()
            
            if line_s.startswith('ID='):
                entry['id'] = line_s[3:].strip()[:100]  # 限制长度
            elif line_s.startswith('CATEGORY='):
                entry['category'] = line_s[9:].strip()[:200]
            elif line_s.startswith('SUBJECT='):
                subj = line_s[8:].strip()
                if subj not in VALID_SUBJECTS:
                    return {'valid': False, 'error': f'无效学科: {subj}'}
                entry['subject'] = subj
            elif line_s.startswith('解析:'):
                entry['explanation'] = line_s[3:].strip()[:2000]  # 限制长度
            elif line_s.startswith('[') and '](Q)' in line_s:
                type_match = re.match(r'\[(.*?)\]\(Q\)(.*)', line_s)
                if type_match:
                    q_type = type_match.group(1)
                    if q_type in QUESTION_TYPES:
                        entry['question_type'] = q_type
                    else:
                        logger.warning(f"未识别的题型 '{q_type}'，已回退到单填空")
                        entry['question_type'] = '单填空'
                    entry['is_question'] = True
                    content_lines.append(type_match.group(2).strip())
            elif line_s.startswith('(Q)'):
                entry['question_type'] = '单填空'
                entry['is_question'] = True
                content_lines.append(line_s[3:].strip())
            else:
                content_lines.append(line_s)
            
            i += 1
        
        # 解析答案
        full = ' '.join(content_lines)
        
        if entry['question_type'] == '多填空':
            # 多填空解析 [答案1|可选1;答案2|可选2]
            match = re.search(r'\[(.*?)\]$', full.strip())
            if match:
                ans_str = match.group(1).strip()
                blanks = [b.strip() for b in ans_str.split(';')]
                entry['answers'] = [[opt.strip()[:200] for opt in blank.split('|') if opt.strip()] for blank in blanks]
                # 验证答案完整性，记录警告但不阻止解析
                if any(len(a) == 0 for a in entry['answers']):
                    logger.warning(f"条目 {entry.get('id', 'unknown')} 答案存在空元素")
                entry['content'] = re.sub(r'\s*\[.*?\]$', '', full).strip()[:1000]
            else:
                entry['content'] = full[:1000]
                entry['answers'] = []
        else:
            # 单填空或其他题型
            match = re.search(r'\[(.*?)\]', full)
            if match:
                ans_str = match.group(1).strip()
                if ans_str:
                    entry['answers'] = [a.strip()[:200] for a in ans_str.split('/') if a.strip()]
                entry['content'] = re.sub(r'\s*\[.*?\]', '', full).strip()[:1000]
            else:
                entry['content'] = full[:1000]
        
        if not entry['id']:
            # 使用稳定的 MD5 哈希，避免 Python hash() 的随机化问题
            entry['id'] = f"AUTO_{hashlib.md5(raw_text.encode()).hexdigest()[:10]}"
        
        entry['valid'] = True
        return entry
    
    async def parse_and_add(self, raw_text: str, kb_name: str) -> bool:
        """解析并添加条目"""
        entry = self.parse_entry(raw_text, kb_name)
        if entry['valid']:
            return await self.db.add_entry(entry)
        return False
    
    async def start_mistake_review(self, kb_name: str, count: int, user_name: str,
                                    session_id: str = 'default') -> List[Dict]:
        """
        开始复习错题（单题模式）
        逻辑:
        1. 检查用户日志，不存在则新建
        2. 优先抽取用户未抽到的题目 (ask_you == 0)
        3. 当所有题目都抽过后，优先抽取 W > C 的条目
        4. 如果条目距上次提问 < 6小时，则不优先
        5. 仅抽取1道题目，输出包含 total_ask 和 ask_you
        
        参数:
            kb_name: 复习册名称，如果为空则从所有复习册中抽取
        """
        # 如果未指定复习册，从所有复习册中获取题目
        if not kb_name:
            all_kb = await self.get_all_kb_names()
            if not all_kb:
                return [{'error': '未配置任何复习册'}]
            
            # 从所有复习册中收集题目
            all_entries = []
            for kb in all_kb:
                entries = await self.db.get_entries(kb, is_question=True, limit=200)
                all_entries.extend(entries)
            
            if not all_entries:
                return [{'error': '所有复习册均无匹配题目'}]
        else:
            all_entries = await self.db.get_entries(kb_name, is_question=True, limit=200)
            if not all_entries:
                return [{'error': f'复习册 {kb_name} 无匹配条目'}]

        if not all_entries:
            return [{'error': '无匹配条目'}]

        # 确保用户日志存在
        await self.user_log._ensure_log_exists(user_name)

        now = datetime.now(timezone.utc)
        scored_entries = []

        # 批量查询优化：一次性获取所有统计，避免 N+1 查询
        entry_ids = [e['id'] for e in all_entries]
        stats_batch = await self.db.get_stats_batch(entry_ids)
        user_review_batch = await self.db.get_user_review_stats_batch(entry_ids, user_name)
        user_records_batch = await self.db.get_user_records_batch(entry_ids, user_name)

        for e in all_entries:
            entry_id = e['id']

            # 获取全局统计（从批量缓存）
            stat = stats_batch.get(entry_id, {'total_ask': 0, 'total_exhibit': 0})
            total_ask = stat['total_ask']

            # 获取用户个人统计（数据库为主数据源，从批量缓存）
            user_stats = user_review_batch.get(entry_id)
            ask_you = user_stats['ask_you'] if user_stats else 0
            last_ask_time = user_stats.get('last_ask_time') if user_stats else None

            # 文件日志作为备份数据源（当数据库为0时使用，仅用于数据恢复）
            if ask_you == 0:
                file_log_ask = await self.user_log.get_entry_ask(user_name, entry_id)
                if file_log_ask > 0:
                    ask_you = file_log_ask
                    logger.debug(f"条目 {entry_id} 使用文件日志恢复 ask_you 计数: {file_log_ask}")

            # 获取用户答题记录 C/W（从批量缓存）
            user_record = user_records_batch.get(entry_id)
            c_count = user_record['c'] if user_record else 0
            w_count = user_record['w'] if user_record else 0

            # 计算优先级分数
            priority = 0

            # 最高优先级: 用户未抽到过的题目
            if ask_you == 0:
                priority = 1000
            else:
                # 次优先级: W > C 的错题
                if w_count > c_count:
                    priority = 500 + (w_count - c_count)

                # 6小时冷却检查
                if last_ask_time:
                    try:
                        time_str = last_ask_time.strip()
                        if time_str.endswith('Z'):
                            time_str = time_str[:-1] + '+00:00'
                        has_tz = bool(re.search(r'[+-]\d{2}:\d{2}$', time_str))
                        if not has_tz:
                            time_str += '+00:00'
                        last_dt = datetime.fromisoformat(time_str)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        hours_diff = (now - last_dt).total_seconds() / 3600
                        if hours_diff < 6:
                            priority -= 200
                    except (ValueError, AttributeError, TypeError) as e:
                        logger.warning(f"时间解析失败: {e}, last_ask_time={last_ask_time}")
                        pass

            scored_entries.append((priority, ask_you, e, total_ask))

        # 排序: 优先级高的在前，同优先级按 ask_you 升序
        scored_entries.sort(key=lambda x: (-x[0], x[1]))

        # 仅选择第1条题目（单题模式）
        selected = scored_entries[:1]

        results = []
        # 清理该 session 的旧记录（确保无残留pending）
        await self.db.clear_pending(session_id)

        for priority, ask_you, e, total_ask in selected:
            # 更新统计
            new_ask = await self.db.increment_stat(e['id'], 'total_ask')
            await self.db.increment_user_review_ask(e['id'], user_name)
            # 添加新pending（清理后保证唯一）
            await self.db.add_pending(session_id, e)

            # 记录到日志（独立错误处理，不影响数据库操作）
            try:
                await self.user_log.record_ask(user_name, e['id'])
            except Exception as log_err:
                logger.warning(f"记录提问到日志失败: {log_err}")

            q_type = e.get('question_type', '单填空')
            if q_type == '多填空' and e.get('answers'):
                ans_display = f"（{len(e['answers'])}个空）"
            else:
                ans_display = ""

            results.append({
                'id': e['id'],
                'category': e['category'],
                'subject': e['subject'],
                'question_type': q_type,
                'question': e['content'] + ans_display,
                'total_ask': new_ask,
                'ask_you': ask_you,
                'kb_name': e.get('kb_name', kb_name or '全部')
            })

        return results

    async def start_knowledge_review(self, kb_name: str, count: int, user_name: str, session_id: str = "") -> List[Dict]:
        """
        开始复习知识点
        N 上限为 10 条
        优先展示 exhibit_you 值低的
        展示后 total_exhibit + 1, exhibit_you + 1
        如果没有纯知识点条目，回退到展示题目（同时展示答案）
        
        参数:
            kb_name: 复习册名称，如果为空则从所有复习册中抽取
        """
        if count > 10:
            count = 10
        if count < 1:
            count = 1

        # 如果未指定复习册，从所有复习册中获取知识点
        if not kb_name:
            all_kb = await self.get_all_kb_names()
            if not all_kb:
                return [{'error': '未配置任何复习册'}]
            
            # 从所有复习册中收集知识点
            all_entries = []
            for kb in all_kb:
                entries = await self.db.get_entries(kb, is_question=False, limit=200)
                all_entries.extend(entries)
            
            is_question_fallback = False
            if not all_entries:
                # 回退到题目
                for kb in all_kb:
                    entries = await self.db.get_entries(kb, is_question=True, limit=200)
                    all_entries.extend(entries)
                is_question_fallback = True
        else:
            all_entries = await self.db.get_entries(kb_name, is_question=False, limit=200)
            is_question_fallback = False

            if not all_entries:
                all_entries = await self.db.get_entries(kb_name, is_question=True, limit=200)
                is_question_fallback = True

        if not all_entries:
            return [{'error': '无匹配条目'}]

        # 确保用户日志存在（按需求：检查用户是否存在日志，如果不存在则新建）
        await self.user_log._ensure_log_exists(user_name)

        # 批量获取 exhibit_you，避免 N+1 查询
        entry_ids = [e['id'] for e in all_entries]
        user_review_batch = await self.db.get_user_review_stats_batch(entry_ids, user_name)

        # 获取每个条目的 exhibit_you 并排序
        entries_with_exhibit = []
        for e in all_entries:
            # 从数据库获取 exhibit_you（批量缓存）
            user_stats = user_review_batch.get(e['id'])
            exhibit_you = user_stats['exhibit_you'] if user_stats else 0
            # 文件日志作为备份数据源（当数据库为0时使用，仅用于数据恢复）
            if exhibit_you == 0:
                exhibit_you_file = await self.user_log.get_entry_exhibit(user_name, e['id'])
                if exhibit_you_file > 0:
                    exhibit_you = exhibit_you_file
                    logger.debug(f"条目 {e['id']} 使用文件日志恢复 exhibit_you 计数: {exhibit_you_file}")
            entries_with_exhibit.append((exhibit_you, e))

        # 按 exhibit_you 升序排序
        entries_with_exhibit.sort(key=lambda x: x[0])
        selected = entries_with_exhibit[:count]

        results = []
        for exhibit_you, e in selected:
            new_ex = await self.db.increment_stat(e['id'], 'total_exhibit')
            # exhibit_you 显示的是递增前的值（与排序逻辑保持一致）
            # 数据库中的 exhibit_you 在此调用后会 +1
            await self.db.increment_user_review_exhibit(e['id'], user_name)

            # 记录到日志（独立错误处理，不影响数据库操作）
            try:
                await self.user_log.record_exhibit(user_name, e['id'])
            except Exception as log_err:
                logger.warning(f"记录展示到日志失败: {log_err}")

            explanation = e.get('explanation', '')
            q_type = e.get('question_type', '单填空')

            # 构建答案显示
            answers_display = ''
            if e.get('answers'):
                ans = e['answers']
                if q_type == '多填空' and ans:
                    if isinstance(ans, list) and len(ans) > 0:
                        if isinstance(ans[0], list):
                            answers_display = '; '.join([opts[0] if opts else '' for opts in ans])
                        else:
                            answers_display = str(ans[0]) if ans else '无'
                else:
                    if isinstance(ans, list) and len(ans) > 0:
                        if isinstance(ans[0], list):
                            answers_display = ans[0][0] if ans[0] else '无'
                        else:
                            answers_display = ', '.join(str(a) for a in ans)

            result_item = {
                'id': e['id'],
                'category': e['category'],
                'subject': e['subject'],
                'content': e['content'],
                'answers': e['answers'],
                'explanation': explanation,
                'total_exhibit': new_ex,
                'exhibit_you': exhibit_you,  # 使用排序时的原始值，保持一致性
                'is_question_fallback': is_question_fallback,
                'question_type': q_type,
                'answers_display': answers_display,
                'kb_name': e.get('kb_name', kb_name or '全部')
            }
            results.append(result_item)

        # 如果提供了 session_id，将展示的条目添加到 pending_questions（answered=1）
        # 这样 /生成解析 可以获取到最近复习的条目
        if session_id and results:
            await self.db.clear_pending(session_id)
            for item in results:
                entry = {
                    'id': item['id'],
                    'kb_name': item.get('kb_name', kb_name or '全部'),
                    'content': item['content'],
                    'answers': item.get('answers', []),
                    'explanation': item.get('explanation', ''),
                    'subject': item.get('subject', '通用'),
                    'question_type': item.get('question_type', '单填空'),
                }
                await self.db.add_pending(session_id, entry, answered=True)

        return results

    async def search_content(self, content: str) -> List[Dict]:
        """
        搜索知识点
        向所有数据库中检索 content 值
        """
        if not content or not content.strip():
            return [{'error': '搜索内容不能为空'}]

        rows = await self.db.search_entries(content)

        if not rows:
            return [{'error': f'未找到包含 "{content}" 的知识点'}]

        results = []
        for d in rows:
            results.append({
                'id': d['id'],
                'kb_name': d['kb_name'],
                'category': d['category'],
                'subject': d['subject'],
                'question_type': d.get('question_type', '单填空'),
                'content': d['content'],
                'answers': d['answers'],
                'explanation': d.get('explanation', '')
            })

        return results
    
    async def generate_and_update_explanation(self, kb_name: str, entry_id: str, umo=None) -> Dict:
        """
        为指定条目生成解析并更新数据库
        返回: {'success': bool, 'explanation': str, 'error': str}
        """
        # 1. 获取条目（使用直接 ID 查询，避免 RANDOM 问题）
        target_entry = await self.db.get_entry_by_id(entry_id)
        
        if not target_entry:
            return {'success': False, 'error': f'未找到条目: {entry_id}'}
        
        # 验证条目属于指定的知识库
        if target_entry.get('kb_name') != kb_name:
            return {'success': False, 'error': f'条目 {entry_id} 不属于复习册 {kb_name}'}
        
        # 2. 检查是否已有解析
        explanation = target_entry.get('explanation', '').strip()
        if explanation and len(explanation) > 5:
            return {
                'success': False, 
                'error': '该条目已有解析，如需重新生成请先清空现有解析',
                'existing_explanation': explanation[:100] + '...'
            }
        
        # 3. 构建 LLM 请求参数
        subject = target_entry.get('subject', '通用')
        q_type = target_entry.get('question_type', '单填空')
        question = target_entry.get('content', '')
        
        # 构建答案字符串
        answers = target_entry.get('answers', [])
        answer_str = self._build_answer_string(q_type, answers)
        
        # 4. 调用 LLM 生成解析
        generated_explanation = await self.llm_judge.generate_explanation(
            subject, q_type, question, answer_str
        )
        
        if not generated_explanation:
            return {'success': False, 'error': 'LLM 生成解析失败'}
        
        # 5. 更新数据库（仅更新解析字段）
        success = await self.db.update_entry_explanation(entry_id, generated_explanation[:2000])
        
        if success:
            return {'success': True, 'explanation': generated_explanation}
        else:
            return {'success': False, 'error': '更新数据库失败'}
    
    def _build_answer_string(self, q_type: str, answers: list) -> str:
        """构建答案字符串用于 LLM 解析"""
        if not answers:
            return '无标准答案'
        
        # 检查是否所有答案都是空的
        if q_type == '多填空':
            if all(isinstance(a, list) and not a for a in answers):
                return '无标准答案'
        else:
            if isinstance(answers[0], list) and not answers[0]:
                return '无标准答案'
        
        try:
            if q_type == '多填空':
                if isinstance(answers[0], list):
                    return '; '.join([' / '.join(opts) for opts in answers if opts])
                return ', '.join(str(a) for a in answers)
            else:
                if isinstance(answers[0], list):
                    return ' / '.join(answers[0])
                return ', '.join(str(a) for a in answers)
        except (IndexError, TypeError):
            return '无标准答案'
    
    async def close(self):
        """关闭资源（数据库连接采用即用即关模式，无需额外清理）"""
        await self.db.close()
        logger.info("知识系统资源已释放")
