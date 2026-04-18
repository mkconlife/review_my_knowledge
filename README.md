# review_my_knowledge - 知识点复习助手

一个基于 Python 的 AstrBot 插件，提供智能化的知识点复习与错题回顾系统。支持单填空、多填空、判断题、开放题等多种题型，具备 LLM 智能判定、错题优先级排序、个人学习统计等功能。

**版本：** 2.2.1

**AstrBot 版本要求：** >=4.16, <5

---

## 功能特性

- **多种题型支持**：单填空、多填空、判断、开放题
- **智能错题复习**：基于用户答题历史自动优先级排序
  - 从未做过的题目优先（优先级 1000）
  - 错误次数 > 正确次数的题目优先（优先级 500+）
  - 6 小时内已做过的题目降权（-200 惩罚）
- **知识点纯展示复习**：无需作答，快速浏览知识点
- **LLM 智能判定**：可选启用大语言模型进行答案二次判定（规则匹配失败时）
- **个人学习统计**：追踪每个用户的提问、展示、正确/错误次数
- **全局知识点搜索**：跨所有复习册搜索知识点内容
- **安全设计**：Prompt 注入防护、SQL 注入防护、输入长度限制、超时保护

---

## 命令列表

| 命令 | 参数 | 说明 |
|------|------|------|
| `/列出复习册` | 无 | 显示所有已配置的复习册及其描述 |
| `/开始复习错题` | `<复习册名>` | 开始错题复习（单题模式，每次 1 题） |
| `/作答` | `<答案>` | 提交答案，格式：`答案` 或 `1.答案1\n2.答案2` |
| `/出示答案` | 无 | 显示当前题目答案（不判定对错） |
| `/开始复习知识点` | `<复习册名> [数量N]` | 知识点纯展示复习，N 最大为 10 |
| `/搜索知识点` | `<关键词>` | 在所有复习册中搜索知识点 |
| `/生成解析` | `<复习册名> <条目ID>` | 使用 LLM 为指定条目生成详细解析 |
| `/我的统计` | `[知识库名]` | 查看个人学习统计 |
| `/重载复习册` | 无 | 重新初始化复习系统（管理用） |

---

## 安装步骤

### 1. 安装插件

从 AstrBot 插件市场搜索并安装 `review_my_knowledge`，或手动将插件文件放入：

```
AstrBot/data/plugins/review_my_knowledge/
```

### 2. 准备复习册文件

复习册文件为 `.txt` 格式，放置在工作目录中。格式示例：

```txt
一.2023级高二第二学期化学周测试题(一)
1.(Q)氢氧化铜悬液与乙醛加热反应现象___,与乙酸常温反应现象___[产生砖红色沉淀;溶解]
乙酸甲酸中的羟基H显酸性,在常温下酸碱中和,加热后发生氧化反应
2.(Q)判断:聚氯乙烯为塑料，具有毒性[对]
3.煤的干馏可得到:(1.)气态:炉煤气,(2.)液态:焦煤油,(3.)固态:焦炭
```

**格式说明：**

| 元素 | 格式 | 示例 |
|------|------|------|
| 类别标题 | `一.`、`二.` 等开头 | `一.化学周测试题(一)` |
| 题目条目 | `(Q)` 标记 | `1.(Q)题目___[答案]` |
| 判断题 | `(Q)判断:` 前缀 | `(Q)判断:题目内容[对/错]` |
| 纯知识点 | 无 `(Q)` 标记 | `3.知识点内容` |
| 答案格式 | `[]` 包裹 | `[答案1;答案2]`（多填空） |
| 可选答案 | `/` 或 `\|` 分隔 | `[对/正确/√]` |
| 解析 | 题目下一行 | `解析内容文本` |

### 3. 初始化配置

```bash
# 进入工作目录（包含 Init.py）
python Init.py
```

`Init.py` 会自动创建：
- `transfer.py` - 复习册格式转换工具
- `settings.txt` - 配置文件模板

### 4. 转换复习册格式

```bash
python transfer.py --file 复习册.txt
```

转换后生成插件可识别的条目格式文件。

### 5. 配置 settings.txt

编辑 `settings.txt`：

```ini
[DATABASE]
FILES="chemistry.txt","biology.txt","physics.txt"
DESCRIPTION="化学复习册","生物复习册","物理复习册"

[INSTALLATION]
PATH=Docker   # 或本地绝对路径，如 /path/to/astrbot
```

### 6. 传输文件到插件目录

```bash
python configure.py
```

该脚本会根据 `settings.txt` 中的 `PATH` 设置，自动将复习册文件复制到正确的插件目录。

---

## 复习册格式详解

### 基本结构

```
<知识库名>
ID=<唯一ID>
CATEGORY=<分类>
SUBJECT=<生物|化学|物理|通用>
[单填空](Q)问题内容[答案1/答案2/答案3]
解析: 解析文本
```

### 题型格式

**单填空：**
```
[单填空](Q)问题___[答案]
```

**多填空：**
```
[多填空](Q)问题___和___[答案1|可选1;答案2|可选2]
```

**判断题：**
```
[判断](Q)判断:题目内容[对/错]
```

**开放题：**
```
[开放](Q)问题内容?
```

**纯知识点（无作答）：**
```
纯知识点内容文本（不带(Q)标记）
```

### 原始 txt 格式示例

```txt
一.章节标题
1.(Q)判断:题目内容[对/错]
解析内容（可选，放在题目下一行）
2.(Q)题目___,题目___[答案1;答案2]
3.纯知识点内容（无Q标记，纯展示）
```

`transfer.py` 会自动识别题型并转换为插件条目格式。

---

## 配置选项

在 AstrBot WebUI 插件配置页面中可调整以下参数：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `llm_judge` | bool | `true` | 启用 LLM 智能判定 |
| `llm_threshold` | float | `0.85` | LLM 判定阈值 |
| `use_llm_for_explanation` | bool | `false` | 使用 LLM 生成解析 |
| `default_kb` | string | `"biology_mistakes"` | 默认复习册名 |
| `session_timeout` | int | `30` | 会话超时时间（分钟） |
| `max_content_length` | int | `10000` | 最大内容长度 |
| `message_max_length` | int | `3000` | 消息最大长度 |

---

## 数据存储

插件使用以下位置存储数据：

| 类型 | 位置 | 说明 |
|------|------|------|
| 数据库 | `data/knowledge_plugin/knowledge.db` | SQLite 数据库，存储条目、统计、用户记录 |
| 用户日志 | `data/knowledge_plugin/user_logs/<用户名>.log` | JSON 格式日志，自动轮转（最大 1000 行） |
| 复习册文件 | `data/plugins/review_my_knowledge/` | 转换后的 .txt 复习册文件 |
| 配置文件 | `data/plugins/review_my_knowledge/settings.txt` | 复习册列表配置 |

**注意：** 根据 AstrBot 规范，持久化数据存储在 `data` 目录下，防止插件更新/重装时丢失。

---

## LLM 智能判定系统

本插件支持使用大语言模型（LLM）进行答案智能判定和解析生成。

### 调用方式

插件通过 AstrBot 框架的 Provider 接口调用 LLM，所有 LLM 调用均包含：
- **超时保护**：120 秒超时限制，防止请求挂起
- **Prompt 注入防护**：用户输入经过 `sanitize_for_prompt()` 清理
- **输入长度限制**：问题 1000 字符、答案 500 字符、学科 50 字符
- **JSON 解析保护**：支持提取代码块中的 JSON，带异常处理

### LLM 使用场景

| 场景 | 方法 | 说明 |
|------|------|------|
| 单填空判定 | `judge_single()` | 规则匹配失败时，LLM 二次判定答案是否正确 |
| 多填空判定 | `judge_multi()` | 规则匹配失败时，LLM 逐空判定并返回详细结果 |
| 生成解析 | `generate_explanation()` | 为缺少解析的条目自动生成详细解题思路 |

### 判定逻辑

1. **优先规则匹配**：使用 `AnswerMatcher` 进行字面完全匹配
2. **LLM 二次判定**：规则匹配失败且启用 `llm_judge` 时调用
3. **阈值判定**：LLM 返回结果需满足 `is_correct and confidence >= llm_threshold`（默认 0.85）

### LLM 输出格式

**单填空/判断题：**
```json
{
  "is_correct": true/false,
  "confidence": 0.0-1.0,
  "reason": "判定理由"
}
```

**多填空：**
```json
{
  "blank_results": [
    {"blank_index": 1, "is_correct": true/false, "matched_answer": "答案"}
  ],
  "correct_count": 2,
  "total_count": 2,
  "accuracy": 1.0,
  "is_correct": true/false,
  "confidence": 0.0-1.0,
  "reason": "判定理由"
}
```

### 配置要求

- 在 AstrBot 中配置可用的 LLM Provider
- 通过 `llm_judge` 配置项启用/禁用 LLM 判定（默认启用）
- 通过 `llm_threshold` 调整判定阈值（默认 0.85）

---

## 开发相关

### 依赖

- `aiosqlite>=0.19.0` - 异步 SQLite 支持

### 目录结构

```
review_my_knowledge/
├── main.py                 # 插件入口，命令定义
├── knowledge_system.py     # 核心逻辑：数据库、匹配器、LLM判定、日志管理
├── metadata.yaml           # 插件元数据
├── requirements.txt        # Python 依赖
├── Init.py                 # 初始化脚本（插件外部运行）
├── transfer.py             # 格式转换工具（插件外部运行）
├── configure.py            # 配置文件传输脚本（插件外部运行）
└── settings.txt            # 复习册配置（由 Init.py 生成）
```

### transfer.py 命令行参数

`transfer.py` 用于将原始试题 txt 文件转换为插件可识别的格式：

```bash
python transfer.py --file 复习册.txt [--subject 学科] [--kb_name 知识库名] [--output 输出文件]
```

| 参数 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `--file` | 是 | - | 输入 txt 文件路径 |
| `--subject` | 否 | `化学` | 学科名称（生物/化学/物理/通用） |
| `--kb_name` | 否 | 文件名 | 知识库/复习册名称 |
| `--output` | 否 | `输入名_import.txt` | 输出文件路径 |

使用示例：

```bash
# 基本用法（自动使用文件名作为知识库名，学科默认为化学）
python transfer.py --file chemistry.txt

# 指定学科和知识库名
python transfer.py --file physics.txt --subject 物理 --kb_name 物理复习册

# 指定输出文件
python transfer.py --file biology.txt --subject 生物 --output biology_converted.txt
```

---

## 常见问题

**Q: 复习册文件放在哪里？**
A: 转换后的复习册文件应放在插件目录内（`data/plugins/review_my_knowledge/`），路径在 `settings.txt` 中配置。

**Q: 如何添加新的复习册？**
A: 1. 准备 txt 文件 2. 运行 `transfer.py` 转换 3. 在 `settings.txt` 添加文件名和描述 4. 运行 `configure.py` 5. 在 AstrBot 中执行 `/重载复习册`

**Q: LLM 判定有什么用？**
A: 当规则匹配失败时，可选使用配置的大语言模型进行语义判定，提高判定准确率。默认启用。

**Q: 如何配置 LLM？**
A: 在 AstrBot WebUI 中配置可用的 LLM Provider，插件会自动调用。确保 `llm_judge` 配置项为 `true`。

**Q: 数据会丢失吗？**
A: 数据库和用户日志存储在 `data` 目录下，插件更新/重装不会丢失这些数据。复习册文件需备份。

**Q: 支持哪些题型？**
A: 单填空、多填空、判断题、开放题。逻辑真/假规则仅适用于判断题，防止误判。

**Q: 错题复习是如何排序的？**
A: 优先级算法：从未做过的题目（1000分）> 错误次数多的题目（500+差值分）> 6小时内做过的题目（-200分惩罚）。

**Q: `/生成解析` 命令怎么用？**
A: 使用格式 `/生成解析 <复习册名> <条目ID>`，条目ID可在错题复习时查看。仅为无解析或解析为空的条目生成。

---

## 许可证

本项目遵循项目开源许可证。

---

## 作者

**mkconlife**

仓库地址：https://github.com/mkconlife/astrbot_plugin_knowledge
