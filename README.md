# tool-radar

海外工具品类热度采集。每天自动跑，把变化量攒下来。

**核心思路：单次排名没有意义，变化量才是信号。** 所以它每次都存快照，和上一次对比，输出「谁在涨、谁是新来的」。

## 数据源

| 源 | 用途 | 需要 token |
|---|---|---|
| GitHub Search API | 开源项目的 star / 活跃度 | 可选（有 token 限额高 3 倍） |
| Hacker News (Algolia) | 讨论热度，早期声量 | 不需要 |

都是官方免费接口，**没有爬虫**，不会因为对方改版而挂掉。

## 快速开始

```bash
cd tool-radar

python collect.py                 # 采集 + 出报告
python collect.py --only "MCP"    # 只采名字含 "MCP" 的品类
python collect.py --report-only   # 不联网，用上次快照重出报告
```

有 GitHub token 的话（限额从 10 次/分 提到 30 次/分）：

```bash
GITHUB_TOKEN=ghp_xxx python collect.py
```

## 输出

```
data/history.csv          长表历史，每行 = 某天某品类某项目，以后可以随便透视
data/latest.json          最近一次快照，用于做 diff
reports/YYYY-MM-DD.md     人类可读的日报
```

**报告怎么读，按价值排序：**

1. **star 增速榜** — 对比上次涨了多少。这是最核心的信号，涨得快 = 需求在起
2. **近期新建** — 180 天内创建的项目。新玩家进场的地方，机会最多
3. **新进入榜** — 上次没出现、这次冒出来的
4. **Hacker News 新讨论** — 早期声量，通常领先于 GitHub 数据
5. **各品类明细** — 完整列表，用来查

## 配置

全在 `config.json`，改完直接生效，不用动代码。

```json
{
  "defaults": {
    "min_stars": 100,
    "limit": 25,
    "active_within_days": 0,
    "recent_days": 180,
    "hn_days": 30,
    "hn_limit": 10
  },
  "categories": {
    "AI Agent": {
      "github": "\"ai agent\" in:name,description",
      "hn": "AI agent"
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `github` | GitHub 搜索语法，直接拼进 API 的 `q` |
| `hn` | Hacker News 关键词，留空则跳过 HN |
| `min_stars` | star 下限，过滤掉噪声项目 |
| `limit` | 每个品类取多少条 |
| `active_within_days` | 只要最近 N 天有提交的（0 = 不限） |
| `recent_days` | 「近期新建」板块的时间窗口 |

### 加一个品类

在 `categories` 里加一条就行：

```json
"AI 教育": {
  "github": "\"ai tutor\" in:name,description",
  "hn": "AI tutor"
}
```

## 查询语法怎么选（踩过的坑）

**别用 `topic:`。** GitHub topic 是用户自己打的标签，热门项目会把所有热门 topic 都打一遍蹭曝光。实测：

```
topic:vector-database  → 返回 anything-llm、llama_index（不是向量数据库）
topic:mcp              → 返回 n8n、JavaGuide、dify（只是打了 tag）
```

结果就是每个品类的榜单都被同样几个巨头占据，信号全被稀释。

**用短语搜索，干净得多：**

```
"model context protocol" in:name,description   → 全是真 MCP 项目
"vector database" in:name,description          → milvus / qdrant / weaviate
```

可用限定符：`in:name`、`in:description`、`in:readme`、`in:topics`
可用过滤：`stars:>100`、`pushed:>2026-01-01`、`language:python`

## 部署到 GitHub Actions（推荐）

不用自己的机器一直开着，免费。

1. 在 GitHub 建个仓库（public 无限免费；private 每月 2000 分钟免费额度，这个任务每天只跑 1 分钟）
2. 把 `tool-radar/` 里的内容 push 上去
3. 进 Actions 页面，手动触发一次 `采集品类热度` 验证
4. 之后每天 UTC 01:17（北京时间 09:17）自动跑，结果自动 commit 回仓库

`GITHUB_TOKEN` 是 Actions 内置的，不用自己配。

**本地定时**（不想用 GitHub）：Windows 任务计划程序 / Linux cron 调
`python collect.py` 即可，但电脑得开着。

### ⚠️ 别让本地和 Actions 同时写数据

这是实际踩过的坑，一定要看。

`data/` 和 `reports/` 是生成物，**本地跑和 Actions 跑都会改它们**。如果两边都提交再合并，
Git 会把两份追加内容**拼在一起** —— `history.csv` 直接翻倍，而且**不会有冲突提示**，
因为两边都是「在文件末尾追加」，Git 判定为非冲突，静默拼接。

**结论：把 Actions 当唯一的数据写入方。**

本地只在调试时跑，而且**只提交代码，不提交数据**：

```bash
git add collect.py config.json README.md .gitattributes .github/
git commit -m "改了什么"
git push          # 注意：不含 data/ 和 reports/
```

如果确实要把本地采的数据传上去，先 `git pull`，再把远端数据取回来对一遍：

```bash
git pull
git checkout origin/main -- data/ reports/
# 确认没有重复行之后再提交
```

验证有没有重复行（同一天的行数应该等于品类数 × 每类条数）：

```bash
tail -n +2 data/history.csv | cut -d, -f1 | sort | uniq -c
```

正常情况是每天一行、只有一个计数；如果某天的数字翻倍，就是重复了。

## 已知限制

- **只有开源项目。** GitHub 覆盖不到闭源 SaaS 工具（很多消费级 AI 产品不开源）。想看那部分，得加导航站数据源
- **首次运行没有基线**，增速榜是空的。跑第二次才有意义
- **未认证时 GitHub 搜索接口 10 次/分**，11 个品类会触发限流。代码里有退避重试，会自己恢复，但会慢一点。配个 token 更省事
- HN 数据对小众品类覆盖较差（比如 MCP 一天可能只有 1 条）

## 下一步可以加的数据源

按性价比排：

1. **Product Hunt** — 有官方 GraphQL API，需申请 token。覆盖闭源工具新品
2. **AI 工具导航站**（toolify / futurepedia / theresanaiforthat）— 能拿到完整品类地图，但要写爬虫，对方改版就得修
3. **Reddit** — 有 API，看垂直社区讨论
4. **定时推送** — 增速榜有异动时推送到微信（Server酱）/ 邮件
