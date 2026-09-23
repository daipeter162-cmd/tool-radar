#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tool-radar —— 海外工具品类热度采集

数据源（都是官方免费 API，不爬页面、不需要逆向）：
  - GitHub Search API     开源项目的 star / 活跃度，反映开发者侧热度
  - Hacker News (Algolia) 讨论热度，反映早期声量

产出：
  data/history.csv        长表历史，每次采集追加一行一项目，方便以后随便透视
  data/latest.json        最近一次快照，用于下次做 diff
  reports/YYYY-MM-DD.md   人类可读报告：品类概览 / 增速榜 / 新进入榜

用法：
  python collect.py                        # 采集 + 出报告
  python collect.py --report-only          # 不联网，用最新快照重出报告
  python collect.py --only "MCP 生态"       # 只采指定品类

环境变量：
  GITHUB_TOKEN  可选。有 token 时搜索接口限额 30 次/分，没有则 10 次/分。
                在 GitHub Actions 里会自动带上（用内置的 secrets.GITHUB_TOKEN）。
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = DATA_DIR / "history.csv"
LATEST_PATH = DATA_DIR / "latest.json"

HISTORY_FIELDS = [
    "date", "category", "source", "name", "stars", "forks",
    "open_issues", "pushed_at", "created_at", "url", "description",
]


# ---------------------------------------------------------------- 基础设施

def log(msg):
    print(msg, flush=True)


def http_json(url, params=None, headers=None, retries=3):
    """GET 一个 JSON 接口，带指数退避重试。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=headers or {})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            # 403/429 通常是限流，等一下再试
            if e.code in (403, 429) and attempt < retries - 1:
                wait = 8 * (attempt + 1)
                log(f"    ! HTTP {e.code}（疑似限流），{wait}s 后重试…")
                time.sleep(wait)
                continue
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} {url} {detail}") from e
        except Exception as e:  # noqa: BLE001 — 网络抖动，重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(3)
                continue
            raise
    raise RuntimeError(f"请求失败: {url}") from last_err


def load_dotenv():
    """从项目根目录的 .env 读密钥（该文件已在 .gitignore 里）。

    只做最简单的事：一行一个 KEY=VALUE，`#` 开头是注释。
    已存在的环境变量优先，不会被 .env 覆盖 —— 这样 CI 里注入的
    secrets 永远盖过本地文件。
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"找不到配置文件: {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 数据源

GITHUB_SEARCH = "https://api.github.com/search/repositories"


def collect_github(spec, token, defaults):
    """按 GitHub 搜索语法取一页项目（按 star 倒序）。"""
    query = spec["github"]
    min_stars = spec.get("min_stars", defaults["min_stars"])
    limit = spec.get("limit", defaults["limit"])
    active_days = spec.get("active_within_days", defaults["active_within_days"])

    q = f"{query} stars:>{min_stars}"
    if active_days:
        since = (date.today() - timedelta(days=active_days)).isoformat()
        q += f" pushed:>{since}"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "tool-radar",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = http_json(
        GITHUB_SEARCH,
        {"q": q, "sort": "stars", "order": "desc", "per_page": limit},
        headers,
    )

    rows = []
    for item in data.get("items", []):
        rows.append({
            "name": item["full_name"],
            "stars": item.get("stargazers_count", 0),
            "forks": item.get("forks_count", 0),
            "open_issues": item.get("open_issues_count", 0),
            "pushed_at": (item.get("pushed_at") or "")[:10],
            "created_at": (item.get("created_at") or "")[:10],
            "url": item.get("html_url", ""),
            "description": (item.get("description") or "").replace("\n", " ").strip()[:180],
        })
    return rows


HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def collect_hn(spec, defaults):
    """取该品类在 Hacker News 上近期的讨论。无需 token。"""
    keyword = spec.get("hn")
    if not keyword:
        return []
    days = spec.get("hn_days", defaults["hn_days"])
    limit = spec.get("hn_limit", defaults["hn_limit"])
    since = int(time.time()) - days * 86400

    data = http_json(HN_SEARCH, {
        "query": keyword,
        "tags": "story",
        "numericFilters": f"created_at_i>{since},points>5",
        "hitsPerPage": limit,
    })

    rows = []
    for hit in data.get("hits", []):
        title = hit.get("title") or ""
        if not title:
            continue
        rows.append({
            "title": title,
            "points": hit.get("points") or 0,
            "comments": hit.get("num_comments") or 0,
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "date": (hit.get("created_at") or "")[:10],
        })
    rows.sort(key=lambda r: -r["points"])
    return rows


def collect_showhn(spec):
    """Show HN = 「我做了个东西」发布会。这是最直接的竞品雷达。

    不按品类拆，全局抓一次 —— 新品出现在哪个赛道，看标题就知道了。
    """
    days = spec.get("days", 7)
    limit = spec.get("limit", 40)
    min_points = spec.get("min_points", 3)
    since = int(time.time()) - days * 86400

    data = http_json(HN_SEARCH, {
        "tags": "show_hn",
        "numericFilters": f"created_at_i>{since},points>{min_points}",
        "hitsPerPage": limit,
    })

    rows = []
    for hit in data.get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "title": title,
            "points": hit.get("points") or 0,
            "comments": hit.get("num_comments") or 0,
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "date": (hit.get("created_at") or "")[:10],
        })
    rows.sort(key=lambda r: -r["points"])
    return rows


PH_GRAPHQL = "https://api.producthunt.com/v2/api/graphql"

# 故意不用 postedAfter 参数：多依赖一个 schema 假设就多一个失败点。
# 改成拉最新 N 条，再在本地按日期过滤。
_PH_FIELDS = """
        name
        tagline
        url
        votesCount
        commentsCount
        createdAt"""

_PH_TOPICS = """
        topics(first: 5) {
          edges { node { name } }
        }"""


def _ph_query(with_topics):
    """按票数排序取帖。

    实测必须用 VOTES，不能用 NEWEST 或 RANKING —— 后两者返回的都是
    刚发布几小时的帖子，票数永远是 0，等于没有热度信号。
    VOTES 返回的是近期票数最高的一批（实测跨约三周），正是我们要的。

    另外实测 first 超过 20 无效，服务端就返回 20 条，所以靠单次翻页
    拿不到更多；靠每天跑一次 + history.csv 累积。
    """
    fields = _PH_FIELDS + (_PH_TOPICS if with_topics else "")
    return (
        "query TopPosts($first: Int!) {"
        "  posts(first: $first, order: VOTES) {"
        "    edges { node {" + fields + "} }"
        "  }"
        "}"
    )


def _ph_post(query, first, token, retries=4):
    """POST GraphQL。

    必须重试：实测从国内网络访问 PH 的 API，约 1/3 的请求会在 TLS 层
    被中断（SSL: UNEXPECTED_EOF_WHILE_READING），但重试就能过。
    """
    body = json.dumps({"query": query, "variables": {"first": first}}).encode("utf-8")
    data = None
    for attempt in range(retries):
        req = urllib.request.Request(
            PH_GRAPHQL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "tool-radar",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001 — 连接抖动，重试
            if attempt == retries - 1:
                raise
            wait = 2 * (attempt + 1)
            log(f"    PH 连接中断（{type(e).__name__}），{wait}s 后重试"
                f"（第 {attempt + 2}/{retries} 次）")
            time.sleep(wait)

    # GraphQL 层面的错误（schema 写错等），重试没用，直接抛出
    if data and data.get("errors"):
        raise RuntimeError(f"{str(data['errors'])[:250]}")
    return data


def collect_producthunt(spec, token):
    """Product Hunt 新品。覆盖 GitHub 看不到的闭源 SaaS。

    没有 token 就直接跳过，不报错 —— 本地没配 token 时不该拖垮整次采集。
    """
    if not token:
        return []
    days = spec.get("days", 7)
    first = spec.get("limit", 50)

    try:
        data = _ph_post(_ph_query(True), first, token)
    except RuntimeError as e:
        # 只有 GraphQL schema 层面的错误才值得去掉 topics 重试。
        # 网络错误已在 _ph_post 内部重试过，换个查询串也救不回来。
        log(f"    PH 带 topics 查询被拒，改用精简查询: {e}")
        data = _ph_post(_ph_query(False), first, token)

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    for edge in (data.get("data", {}).get("posts") or {}).get("edges", []):
        node = edge.get("node") or {}
        created = (node.get("createdAt") or "")[:10]
        if created and created < cutoff:
            continue
        topics = [
            e["node"]["name"]
            for e in ((node.get("topics") or {}).get("edges") or [])
            if e.get("node")
        ]
        rows.append({
            "name": node.get("name") or "",
            "tagline": (node.get("tagline") or "").replace("\n", " ").strip(),
            "votes": node.get("votesCount") or 0,
            "comments": node.get("commentsCount") or 0,
            "date": created,
            # PH 返回的 url 带一长串 utm 追踪参数，砍掉，否则表格没法看
            "url": (node.get("url") or "").split("?")[0],
            # 用 / 分隔而不是逗号：topic 名里本身含逗号时，CSV 会被引号包裹，
            # 用 cut / awk 这类工具解析会错位
            "topics": " / ".join(topics),
        })
    rows.sort(key=lambda r: -r["votes"])
    return rows


# ---------------------------------------------------------------- 落盘

def append_history(today, collected, extra):
    """把本次采集写入长表 CSV。

    三个源共用一张表，靠 `source` 列区分（github / producthunt / showhn），
    这样以后可以用一条查询同时看开源项目和闭源新品的走势。

    同一天重复跑会替换当天数据，而不是再追一份 —— 否则手动触发测试
    或者任务重跑都会在历史表里堆重复行。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open(newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date") != today]

    def blank(**kw):
        row = {f: "" for f in HISTORY_FIELDS}
        row.update(date=today, **kw)
        return row

    for category, payload in collected.items():
        for row in payload["github"]:
            rows.append(blank(
                category=category, source="github", name=row["name"],
                stars=row["stars"], forks=row["forks"],
                open_issues=row["open_issues"], pushed_at=row["pushed_at"],
                created_at=row["created_at"], url=row["url"],
                description=row["description"],
            ))

    for row in extra.get("producthunt", []):
        rows.append(blank(
            category=row["topics"], source="producthunt", name=row["name"],
            stars=row["votes"], created_at=row["date"], url=row["url"],
            description=row["tagline"],
        ))

    for row in extra.get("show_hn", []):
        rows.append(blank(
            source="showhn", name=row["title"], stars=row["points"],
            created_at=row["date"], url=row["url"],
            description=row["hn_url"],
        ))

    with HISTORY_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def load_prev_snapshot():
    if not LATEST_PATH.exists():
        return {}
    try:
        with LATEST_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_snapshot(today, collected, extra):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": today,
        "github": {
            cat: {r["name"]: {"stars": r["stars"], "url": r["url"]} for r in p["github"]}
            for cat, p in collected.items()
        },
        "hn": {
            cat: {r["title"]: {"points": r["points"]} for r in p["hn"]}
            for cat, p in collected.items()
        },
        "showhn": {r["title"]: {"points": r["points"], "url": r["url"]}
                   for r in extra.get("show_hn", [])},
        "producthunt": {r["name"]: {"votes": r["votes"], "url": r["url"]}
                        for r in extra.get("producthunt", [])},
    }
    with LATEST_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 分析

def compute_movers(prev, collected):
    """算出「star 增速榜」和「新进入榜」。第一次采集时两者都为空。"""
    prev_cats = (prev or {}).get("github", {})
    movers, newcomers = [], []

    for category, payload in collected.items():
        prev_repos = prev_cats.get(category, {})
        for row in payload["github"]:
            prev_info = prev_repos.get(row["name"])
            if prev_info is None:
                newcomers.append({"category": category, **row})
            else:
                delta = row["stars"] - prev_info.get("stars", 0)
                if delta != 0:
                    movers.append({
                        "category": category,
                        "name": row["name"],
                        "stars": row["stars"],
                        "delta": delta,
                        "url": row["url"],
                        "description": row["description"],
                    })

    movers.sort(key=lambda x: -x["delta"])
    newcomers.sort(key=lambda x: -x["stars"])
    return movers, newcomers


def compute_new_discussions(prev, collected):
    prev_hn = (prev or {}).get("hn", {})
    fresh = []
    for category, payload in collected.items():
        known = prev_hn.get(category, {})
        for row in payload["hn"]:
            if row["title"] not in known:
                fresh.append({"category": category, **row})
    fresh.sort(key=lambda x: -x["points"])
    return fresh


# ---------------------------------------------------------------- 报告

def cut(text, n):
    """截断时加省略号，别把词切一半就没了。"""
    text = (text or "").replace("|", "\\|").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def md_table(headers, rows):
    if not rows:
        return "（无）\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def compute_recent(collected, recent_days):
    """近期新建的项目 —— 比总榜更能反映「什么新东西在起量」。"""
    cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
    recent = []
    for category, payload in collected.items():
        for row in payload["github"]:
            if row["created_at"] and row["created_at"] >= cutoff:
                recent.append({"category": category, **row})
    recent.sort(key=lambda x: -x["stars"])
    return recent


def build_report(today, collected, prev, movers, newcomers, fresh_hn,
                 recent, recent_days, extra):
    is_first = not prev
    lines = [f"# 海外工具品类热度 · {today}", ""]

    if is_first:
        lines += [
            "> **首次采集**，没有历史基线，所以增速榜为空。",
            "> 明天再跑一次，变化量就出来了 —— 变化量才是信号，单次排名不是。",
            "",
        ]

    # 品类概览
    lines += ["## 品类概览", ""]
    overview = []
    for category, payload in collected.items():
        repos = payload["github"]
        if repos:
            stars = [r["stars"] for r in repos]
            median = int(statistics.median(stars))
            newest = max((r["created_at"] for r in repos if r["created_at"]), default="-")
        else:
            median, newest = 0, "-"
        overview.append([
            category, len(repos), median, newest,
            sum(1 for n in newcomers if n["category"] == category),
        ])
    overview.sort(key=lambda r: -r[2])
    lines.append(md_table(
        ["品类", "收录", "中位 star", "最新项目创建于", "新进入"],
        overview,
    ))

    # 增速榜 —— 核心信号
    lines += ["", "## star 增速榜", ""]
    if is_first:
        lines.append("（首次采集，无对比基线）\n")
    elif not movers:
        lines.append("（与上次相比没有变化）\n")
    else:
        lines.append(md_table(
            ["品类", "项目", "star", "增量", "说明"],
            [[m["category"], f"[{m['name']}]({m['url']})", m["stars"],
              f"**+{m['delta']}**", cut(m["description"], 70)] for m in movers[:20]],
        ))

    # 新进入
    lines += ["", "## 新进入榜（上次没有、这次出现了）", ""]
    if is_first:
        lines.append("（首次采集，全部都是新进入）\n")
    else:
        lines.append(md_table(
            ["品类", "项目", "star", "创建于", "说明"],
            [[n["category"], f"[{n['name']}]({n['url']})", n["stars"],
              n["created_at"], cut(n["description"], 70)] for n in newcomers[:20]],
        ))

    # 近期新建 —— 找机会最该看的一栏
    lines += ["", f"## 近期新建（{recent_days} 天内创建，按 star 排）", ""]
    if recent:
        lines.append(md_table(
            ["品类", "项目", "star", "创建于", "说明"],
            [[r["category"], f"[{r['name']}]({r['url']})", r["stars"],
              r["created_at"], cut(r["description"], 70)] for r in recent[:25]],
        ))
    else:
        lines.append(f"（{recent_days} 天内没有新建且 star 达标的新项目）\n")

    # Product Hunt 新品 —— 闭源 SaaS 的入场信号，GitHub 看不到这块
    ph = extra.get("producthunt", [])
    lines += ["", f"## Product Hunt 高票榜（近 {extra.get('ph_days', 30)} 天，按票数）", ""]
    if ph:
        known_ph = (prev or {}).get("producthunt", {})
        listed = sorted(ph, key=lambda p: (p["name"] in known_ph, -p["votes"]))
        lines.append(md_table(
            ["产品", "票数", "评论", "发布", "话题", "一句话"],
            [[("🆕 " if p["name"] not in known_ph else "") + f"[{p['name']}]({p['url']})",
              p["votes"], p["comments"], p["date"],
              cut(p["topics"], 18), cut(p["tagline"], 55)] for p in listed[:25]],
        ))
    else:
        lines.append("（没有数据 —— 检查 PH_API_TOKEN 是否配好）\n")

    # Show HN —— 「我做了个东西」发布会
    show = extra.get("show_hn", [])
    lines += ["", f"## Show HN（近 {extra.get('show_hn_days', 7)} 天）", ""]
    if show:
        known_show = (prev or {}).get("showhn", {})
        listed = sorted(show, key=lambda h: (h["title"] in known_show, -h["points"]))
        lines.append(md_table(
            ["标题", "点数", "评论", "发布"],
            [[("🆕 " if h["title"] not in known_show else "") + f"[{cut(h['title'], 65)}]({h['url']})",
              h["points"], h["comments"], h["date"]] for h in listed[:25]],
        ))
    else:
        lines.append("（无）\n")

    # HN 讨论
    lines += ["", "## Hacker News 新讨论", ""]
    if fresh_hn:
        lines.append(md_table(
            ["品类", "标题", "点数", "评论", "日期"],
            [[h["category"], f"[{cut(h['title'], 60)}]({h['url']})", h["points"],
              h["comments"], h["date"]] for h in fresh_hn[:20]],
        ))
    else:
        lines.append("（无新讨论）\n")

    # 明细
    lines += ["", "---", "", "## 各品类明细", ""]
    for category, payload in collected.items():
        lines += [f"### {category}", ""]
        if payload["github"]:
            lines.append(md_table(
                ["项目", "star", "fork", "最近提交", "说明"],
                [[f"[{r['name']}]({r['url']})", r["stars"], r["forks"],
                  r["pushed_at"], cut(r["description"], 60)] for r in payload["github"]],
            ))
        if payload["hn"]:
            lines += ["", "HN 讨论：", ""]
            lines.append(md_table(
                ["标题", "点数", "评论"],
                [[f"[{cut(h['title'], 60)}]({h['url']})", h["points"], h["comments"]]
                 for h in payload["hn"]],
            ))
        lines.append("")

    lines += ["---", "",
              f"生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
              ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="海外工具品类热度采集")
    parser.add_argument("--report-only", action="store_true",
                        help="不联网，用 data/latest.json 重出报告")
    parser.add_argument("--only", nargs="+", metavar="品类",
                        help="只采集名字里包含这些字串的品类")
    args = parser.parse_args()

    # --only 是调试用的：只采部分品类。此时**不写任何持久化数据** ——
    # 因为 append_history 是按日期整体替换的，用 --only 跑会把当天
    # 其他品类的数据一起抹掉，报告也会被残缺版本覆盖。
    debug = bool(args.only)

    load_dotenv()

    config = load_config()
    defaults = config["defaults"]
    categories = config["categories"]

    if args.only:
        picked = {
            name: spec for name, spec in categories.items()
            if any(k in name for k in args.only)
        }
        if not picked:
            sys.exit(f"--only 没匹配到任何品类。可选：{', '.join(categories)}")
        categories = picked

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    ph_token = os.environ.get("PH_API_TOKEN", "").strip()
    today = date.today().isoformat()
    prev = load_prev_snapshot()

    extra = {
        "show_hn": [],
        "producthunt": [],
        "show_hn_days": config.get("show_hn", {}).get("days", 7),
        "ph_days": config.get("producthunt", {}).get("days", 7),
    }

    if args.report_only:
        if not prev:
            sys.exit("没有 data/latest.json，无法只出报告。先跑一次完整采集。")
        log(f"[report-only] 使用快照 {prev.get('date', '?')}")
        # report-only 模式下没有原始行，从快照重建最小结构
        collected = {
            cat: {
                "github": [
                    {"name": n, "stars": v["stars"], "forks": 0, "open_issues": 0,
                     "pushed_at": "", "created_at": "", "url": v["url"],
                     "description": ""}
                    for n, v in repos.items()
                ],
                "hn": [],
            }
            for cat, repos in (prev.get("github") or {}).items()
        }
        movers, newcomers, fresh_hn = [], [], []
    else:
        collected = {}
        log(f"[采集] {today} · {len(categories)} 个品类")
        log(f"       GitHub token: {'有' if token else '无（限额较低）'}"
            f" · Product Hunt token: {'有' if ph_token else '无（跳过 PH）'}")

        for i, (name, spec) in enumerate(categories.items(), 1):
            log(f"  ({i}/{len(categories)}) {name}")
            github_rows, hn_rows = [], []
            try:
                github_rows = collect_github(spec, token, defaults)
                log(f"      GitHub {len(github_rows)} 条")
            except Exception as e:  # noqa: BLE001 — 单个品类失败不该拖垮整次
                log(f"      ! GitHub 失败: {e}")
            try:
                hn_rows = collect_hn(spec, defaults)
                log(f"      HN {len(hn_rows)} 条")
            except Exception as e:  # noqa: BLE001
                log(f"      ! HN 失败: {e}")
            collected[name] = {"github": github_rows, "hn": hn_rows}
            time.sleep(2)  # 对搜索接口客气一点

        # 两个全局源，不按品类拆
        global_sources = (
            ("show_hn", "Show HN",
             lambda: collect_showhn(config.get("show_hn", {}))),
            ("producthunt", "Product Hunt",
             lambda: collect_producthunt(config.get("producthunt", {}), ph_token)),
        )
        for key, label, fetch in global_sources:
            try:
                extra[key] = fetch()
                log(f"  {label}: {len(extra[key])} 条")
            except Exception as e:  # noqa: BLE001 — 源挂了不该拖垮整次采集
                log(f"  ! {label} 失败: {e}")

        movers, newcomers = compute_movers(prev, collected)
        fresh_hn = compute_new_discussions(prev, collected)
        if not debug:
            append_history(today, collected, extra)
            save_snapshot(today, collected, extra)

    recent_days = defaults["recent_days"]
    recent = compute_recent(collected, recent_days)
    report = build_report(today, collected, prev if not args.report_only else None,
                          movers, newcomers, fresh_hn, recent, recent_days,
                          extra if not args.report_only else {})
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"{today}{'.debug' if debug else ''}.md"
    report_path.write_text(report, encoding="utf-8")

    total = sum(len(p["github"]) for p in collected.values())
    log("")
    log(f"[完成] {total} 个项目 · 报告: {report_path}")
    if movers:
        log(f"[增速榜] 前 3: "
            + ", ".join(f"{m['name']} +{m['delta']}" for m in movers[:3]))
    if newcomers:
        log(f"[新进入] {len(newcomers)} 个")
    if recent:
        log(f"[近期新建] {len(recent)} 个，最新: "
            + ", ".join(f"{r['name']}({r['created_at']})" for r in recent[:3]))
    if extra["producthunt"]:
        log(f"[PH 新品] {len(extra['producthunt'])} 个，最热: "
            + ", ".join(f"{p['name']}({p['votes']}票)"
                        for p in extra["producthunt"][:3]))
    if extra["show_hn"]:
        log(f"[Show HN] {len(extra['show_hn'])} 个，最热: "
            + ", ".join(f"{h['title'][:40]}({h['points']}点)"
                        for h in extra["show_hn"][:3]))


if __name__ == "__main__":
    main()
