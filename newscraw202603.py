#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优申内娱 - 新闻聚合抓取脚本（最终优化版 + 关键词大类循环修复）
功能：
- 从外部 JSON 文件读取 RSS 源，支持自动禁用连续失败的源
- 使用 DeepSeek API 为每条新闻生成约150字纯文本摘要（自动适配中英文，无HTML）
- 每日/每月/每年 Token 预算控制，防止超额
- 根据关键词自动分类
- 发送 HTML 邮件到指定邮箱，每次最多5条
- 选取规则：优先不同源，英文优先，时间倒序，并增加大类循环（每个大类发过一次后，等所有大类都发过再重复）
- 邮件中显示去重后总数及各网站去重后分布（含发送数量）、Token 消耗统计（本次、今日、汇总累计）
- 语言检测采用中英文字符比例，避免误判
- 使用 logging 模块，支持日志输出到文件
- 全局异常捕获，错误时停留显示信息
"""

import os
import smtplib
import feedparser
import json
import requests
import re
import time
import logging
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime, date
from collections import defaultdict

# ====================== 常量配置 ======================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 日志配置
LOG_FILE = os.path.join(SCRIPT_DIR, "news_crawler.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 邮件配置
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465
FROM_EMAIL = "526103916@qq.com"
TO_EMAIL = "526103916@qq.com"

# 去重配置
SENT_TITLES_FILE = os.path.join(SCRIPT_DIR, "sent_titles.json")
MAX_SENT_TITLES = 2000

# 关键词循环记录文件
KEYWORD_ROTATION_FILE = os.path.join(SCRIPT_DIR, "keyword_rotation.json")

# DeepSeek API 配置
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DAILY_TOKEN_BUDGET = 70000
# MONTHLY_TOKEN_BUDGET = 300000
# YEARLY_TOKEN_BUDGET = 5000000   # 年度预算，可根据需要调整
API_MAX_RETRIES = 1
API_TIMEOUT = 10
RETRY_DELAY = 1                  # 重试间隔（秒）

# RSS 源配置
RSS_SOURCES_FILE = os.path.join(SCRIPT_DIR, "rss_sources.json")
MAX_FAILURES = 3

# Token 用量记录文件
USAGE_FILE = os.path.join(SCRIPT_DIR, "token_usage.json")

# 邮件限制
MAX_NEWS_PER_EMAIL = 5          # 每次最多发送5条新闻

# 代理设置（服务器上无需代理）
PROXY = None
REQUEST_TIMEOUT = 15
MAX_ENTRIES_PER_SOURCE = 15

# 文本处理常量
PARAGRAPH_CHUNK_SIZE = 80        # 段落强制切分长度（字符数）

# 需要从外部文件加载的全局变量
FROM_PASSWORD = None
DEEPSEEK_API_KEY = None
KEYWORDS = None

# ====================== 辅助函数 ======================
def clean_text(text):
    """清理文本中的换行和多余空白"""
    return ' '.join(text.replace('\n', ' ').replace('\r', ' ').split()) if text else ''

def detect_language(text):
    """
    检测文本主体语言：统计中文字符与英文字母数量，返回 'zh' 或 'en'
    方案三：避免因少量中文（如人名、注释）误判为中文
    """
    chinese = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    english = sum(1 for ch in text if ch.isalpha() and ch.isascii())
    return 'zh' if chinese > english else 'en'

def parse_published(published_str):
    """
    将发布时间字符串转换为时间戳（秒），用于排序。
    如果解析失败，返回 0（视为最早时间）。
    """
    if not published_str or published_str == '未知时间':
        return 0
    # 尝试多种常见格式
    for fmt in ['%a, %d %b %Y %H:%M:%S %Z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%SZ', '%a, %d %b %Y %H:%M:%S %z']:
        try:
            dt = datetime.strptime(published_str, fmt)
            return dt.timestamp()
        except:
            continue
    # 如果都不匹配，返回0
    return 0

# ====================== 关键词循环管理 ======================
def load_keyword_rotation():
    """加载每个关键词大类的最后发送时间（Unix时间戳），返回字典 {tag: last_timestamp}"""
    try:
        with open(KEYWORD_ROTATION_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('rotation', {})
    except:
        return {}

def save_keyword_rotation(rotation):
    """保存关键词大类最后发送时间"""
    with open(KEYWORD_ROTATION_FILE, 'w', encoding='utf-8') as f:
        json.dump({'rotation': rotation}, f, ensure_ascii=False, indent=2)

def update_keyword_rotation(tags):
    """更新一批关键词大类的最后发送时间为当前时间"""
    rotation = load_keyword_rotation()
    now = time.time()
    for tag in tags:
        rotation[tag] = now
    save_keyword_rotation(rotation)

def get_tag_priority(tag):
    """
    获取大类的优先级分数（越小越优先）。
    如果从未发送过，返回 0（最优先）；否则返回最后发送时间戳（越早越优先）。
    """
    rotation = load_keyword_rotation()
    last = rotation.get(tag, 0)
    return last

def news_sort_key(news):
    """
    排序键：先按大类循环优先级（最后发送时间早的优先），再按语言（英文优先），再按时间倒序。
    """
    tag = news.get('primary_tag', '综合新闻')
    priority = get_tag_priority(tag)   # 越小越优先
    lang_score = 0 if news.get('language') == 'en' else 1
    timestamp = parse_published(news.get('published', ''))
    return (priority, lang_score, -timestamp)

# ====================== 去重 ======================
def load_sent_titles():
    try:
        with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return set(data.get('titles', [])), data.get('titles', [])
    except Exception:
        return set(), []

def save_sent_titles(titles_list):
    if len(titles_list) > MAX_SENT_TITLES:
        titles_list = titles_list[-MAX_SENT_TITLES:]
    with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump({'titles': titles_list}, f, ensure_ascii=False, indent=2)

def add_sent_titles(new_titles):
    global sent_titles_set, sent_titles_list
    for t in new_titles:
        if t not in sent_titles_set:
            sent_titles_set.add(t)
            sent_titles_list.append(t)
    save_sent_titles(sent_titles_list)

sent_titles_set, sent_titles_list = load_sent_titles()

# ====================== RSS 源管理 ======================
def load_rss_sources():
    try:
        with open(RSS_SOURCES_FILE, 'r', encoding='utf-8') as f:
            sources = json.load(f)
            for s in sources:
                s.setdefault('enabled', True)
                s.setdefault('fail_count', 0)
            return sources
    except Exception:
        return []

def save_rss_sources(sources):
    with open(RSS_SOURCES_FILE, 'w', encoding='utf-8') as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

def update_fail_count(source_url, success):
    sources = load_rss_sources()
    for s in sources:
        if s['url'] == source_url:
            if success:
                s['fail_count'] = 0
            else:
                s['fail_count'] = s.get('fail_count', 0) + 1
                if s['fail_count'] >= MAX_FAILURES and s.get('enabled', True):
                    s['enabled'] = False
                    logger.warning(f"源 {s['name']} 连续失败 {MAX_FAILURES} 次，已自动禁用")
            break
    save_rss_sources(sources)

# ====================== Token 预算管理（含永久总累计） ======================
def load_usage():
    try:
        with open(USAGE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            today = date.today().isoformat()
            current_month = datetime.now().strftime("%Y-%m")
            current_year = str(datetime.now().year)
            if data.get('date') != today:
                data['today'] = 0
                data['date'] = today
            if data.get('month') != current_month:
                data['monthly'] = 0
                data['month'] = current_month
            if data.get('year') != current_year:
                data['yearly'] = 0
                data['year'] = current_year
            # 如果 total 字段不存在，则从已有的累计中取最大值作为初始值（继承历史）
            if 'total' not in data:
                data['total'] = max(data.get('yearly', 0), data.get('monthly', 0), data.get('today', 0))
            return data
    except Exception:
        return {
            'date': date.today().isoformat(),
            'today': 0,
            'month': datetime.now().strftime("%Y-%m"),
            'monthly': 0,
            'year': str(datetime.now().year),
            'yearly': 0,
            'total': 0
        }

def save_usage(usage):
    with open(USAGE_FILE, 'w', encoding='utf-8') as f:
        json.dump(usage, f, ensure_ascii=False, indent=2)

def check_usage_available(estimated_tokens):
    usage = load_usage()
    if usage['today'] + estimated_tokens > DAILY_TOKEN_BUDGET:
        logger.warning(f"今日 Token 预算不足 (已用 {usage['today']}/{DAILY_TOKEN_BUDGET})，本次调用跳过")
        return False
    # 以下两段取消
    # if usage['monthly'] + estimated_tokens > MONTHLY_TOKEN_BUDGET:
    #   logger.warning(f"本月 Token 预算不足 (已用 {usage['monthly']}/{MONTHLY_TOKEN_BUDGET})，本次调用跳过")
    #   return False
    # if usage['yearly'] + estimated_tokens > YEARLY_TOKEN_BUDGET:
    #   logger.warning(f"年度 Token 预算不足 (已用 {usage['yearly']}/{YEARLY_TOKEN_BUDGET})，本次调用跳过")
    #   return False
    return True

def deduct_usage(actual_tokens):
    usage = load_usage()
    usage['today'] += actual_tokens
    usage['monthly'] += actual_tokens
    usage['yearly'] += actual_tokens
    usage['total'] += actual_tokens   # 永久总累计
    save_usage(usage)

def get_usage_stats():
    usage = load_usage()
    return usage['today'], usage['monthly'], usage['yearly'], usage['total']

# ====================== RSS 抓取 ======================
def get_news_from_rss(url, source_name):
    news = []
    try:
        proxies = {"http": PROXY, "https": PROXY} if PROXY else None
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers, proxies=proxies)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo:
            logger.warning(f"解析 {url} 时出现异常: {feed.bozo_exception}")
        for entry in feed.entries[:MAX_ENTRIES_PER_SOURCE]:
            title = clean_text(entry.get('title', ''))
            if not title:
                continue
            raw_summary = clean_text(entry.get('summary', ''))
            clean_summary = re.sub(r'<[^>]+>', '', raw_summary)
            clean_summary = re.sub(r'\s+', ' ', clean_summary).strip()
            news.append({
                'title': title,
                'link': entry.get('link', ''),
                'summary': clean_summary[:300],
                'published': entry.get('published') or entry.get('pubDate') or '未知时间',
                'source_name': source_name
            })
        return news
    except Exception as e:
        logger.error(f"抓取失败 {source_name} ({url[:50]}...) | 错误：{str(e)[:50]}")
        return []

# ====================== 关键词分类 ======================
def match_tags(text):
    if KEYWORDS is None:
        return ["综合新闻"]
    txt = text.lower()
    matched = []
    for tag, keywords in KEYWORDS.items():
        if tag == "综合新闻":
            continue
        for kw in keywords:
            if kw.lower() in txt:
                matched.append(tag)
                break
    return matched or ["综合新闻"]

# ====================== AI 摘要生成（返回实际消耗 Token） ======================
def generate_ai_summary(title, summary):
    if not DEEPSEEK_API_KEY:
        return re.sub(r'<[^>]+>', '', summary[:300]) if summary else re.sub(r'<[^>]+>', '', title[:300]), 0

    estimated_tokens = max(400, (len(title) + len(summary)) // 2 + 400)
    if not check_usage_available(estimated_tokens):
        return (re.sub(r'<[^>]+>', '', summary[:300]) if summary else re.sub(r'<[^>]+>', '', title[:300])), 0

    lang = detect_language(title + summary)

    if lang == 'zh':
        system_msg = (
            "你是一个严格的新闻摘要助手。\n"
            "【重要】你必须用中文回复。\n"
            "摘要要求：\n"
            "1. 只输出纯文本，不要任何 HTML 标签、图片、链接。\n"
            "2. 总字数控制在150字以内（包含观点）。\n"
            "3. 先客观概括核心事实（约120字），最后用一句话表达个人观点（约30字）。\n"
            "4. 按照逻辑意义分成2-3个自然段，段落之间用空行（两个换行符）分隔。"
        )
        lang_instruction = "用中文回复。"
    else:
        system_msg = (
            "You are a strict news summarizer.\n"
            "【Important】You MUST reply in English.\n"
            "Requirements:\n"
            "1. Output plain text only, no HTML tags, images, or links.\n"
            "2. Total length strictly within 150 words (including opinion).\n"
            "3. Summarize facts first (~120 words), then add a short personal opinion (~30 words).\n"
            "4. Divide into 2-3 paragraphs by logical meaning, separated by blank lines (two newline characters)."
        )
        lang_instruction = "Reply in English."

    prompt = f"""请概括以下新闻的核心内容，总字数严格控制在150字以内（包含观点）。
要求：
1. **只输出纯文本摘要，不要包含任何HTML标签、图片、链接、引用、表情符号等。**
2. 摘要必须完整、通顺，不要截断。
3. 如果原文包含HTML或代码，请忽略它们，只总结文字内容。
4. **按照逻辑意义分成2-3个自然段**，段落之间用**空行**（即两个换行符）分隔。例如：第一段写核心事实，第二段写重要细节或影响，第三段写个人观点。
5. 先概括事实（约120字），最后用一句话表达个人观点（约30字）。观点要简短、中立。

标题：{title}
内容：{summary}
{lang_instruction}"""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.4
    }

    for attempt in range(API_MAX_RETRIES + 1):
        try:
            resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT)
            if resp.status_code == 200:
                result = resp.json()
                actual_tokens = result.get('usage', {}).get('total_tokens', estimated_tokens)
                deduct_usage(actual_tokens)
                summary_ai = result["choices"][0]["message"]["content"].strip()
                summary_ai = summary_ai.strip('"').strip("'")
                summary_ai = re.sub(r'<[^>]+>', '', summary_ai)
                if lang == 'en' and any('\u4e00' <= char <= '\u9fff' for char in summary_ai):
                    logger.warning("检测到英文新闻摘要仍包含中文，降级使用原始摘要")
                    return (re.sub(r'<[^>]+>', '', summary[:300]) if summary else re.sub(r'<[^>]+>', '', title[:300])), actual_tokens
                return summary_ai, actual_tokens
            else:
                logger.error(f"API调用失败 ({resp.status_code}): {resp.text[:100]}")
                if attempt == API_MAX_RETRIES:
                    break
                time.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f"生成要点时出错 (尝试 {attempt+1}): {e}")
            if attempt == API_MAX_RETRIES:
                break
            time.sleep(RETRY_DELAY)

    fallback = re.sub(r'<[^>]+>', '', summary[:300]) if summary else re.sub(r'<[^>]+>', '', title[:300])
    return fallback, 0

# ====================== 主抓取逻辑（去重，不生成摘要） ======================
def fetch_news_without_summary():
    """
    抓取所有 RSS 源，返回去重后的新闻列表（未排序，未生成摘要）
    同时返回每个源的去重后数量字典（用于统计）
    """
    all_news = []
    seen = set()
    source_unique_stats = defaultdict(int)
    sources = load_rss_sources()
    if not sources:
        logger.error("没有找到任何 RSS 源，请检查 rss_sources.json 文件")
        return [], {}

    for source in sources:
        if not source.get('enabled', True):
            logger.info(f"跳过已禁用的源：{source['name']}")
            continue

        logger.info(f"抓取：{source['name']}")
        items = get_news_from_rss(source['url'], source['name'])
        if items:
            update_fail_count(source['url'], success=True)
        else:
            update_fail_count(source['url'], success=False)
            continue

        for news in items:
            if news['title'] in sent_titles_set or news['title'] in seen:
                continue
            seen.add(news['title'])
            tags = match_tags(news['title'] + news['summary'])
            news['tags'] = tags
            news['primary_tag'] = tags[0] if tags else '综合新闻'
            news['language'] = detect_language(news['title'] + news['summary'])
            all_news.append(news)
            source_unique_stats[source['name']] += 1

    return all_news, source_unique_stats

def select_news_diverse_sources(all_news, max_count):
    """
    选取规则（简化版 + 大类循环优先）：
    1. 直接按大类循环优先级 + 英文优先 + 时间倒序排序。
    2. 顺序遍历，优先选择与已选新闻关键词大类不同且源不同的新闻。
    3. 如果不足，再放宽条件（允许同源但不同关键词，最后允许同关键词）。
    """
    if not all_news:
        return [], defaultdict(int)

    # 直接按大类循环优先级 + 英文优先 + 时间倒序排序
    sorted_news = sorted(all_news, key=news_sort_key)

    selected = []
    selected_keywords = set()
    selected_sources = set()

    # 第一轮：严格不同关键词 + 不同源
    for news in sorted_news:
        tag = news['primary_tag']
        source = news['source_name']
        if tag not in selected_keywords and source not in selected_sources:
            selected.append(news)
            selected_keywords.add(tag)
            selected_sources.add(source)
            if len(selected) >= max_count:
                break

    # 第二轮：如果不足，放宽到只要求不同关键词（允许重复源）
    if len(selected) < max_count:
        for news in sorted_news:
            if news in selected:
                continue
            tag = news['primary_tag']
            if tag not in selected_keywords:
                selected.append(news)
                selected_keywords.add(tag)
                if len(selected) >= max_count:
                    break

    # 第三轮：如果仍不足，直接按顺序补充（允许重复关键词和源）
    if len(selected) < max_count:
        for news in sorted_news:
            if news not in selected:
                selected.append(news)
                if len(selected) >= max_count:
                    break

    # 统计每个源的发送数量
    source_sent_counts = defaultdict(int)
    for news in selected:
        source_sent_counts[news['source_name']] += 1

    return selected, source_sent_counts

def generate_summaries_for_news(news_list):
    """为新闻列表生成摘要，返回本次消耗的总 Token"""
    total_tokens = 0
    for i, news in enumerate(news_list):
        logger.info(f"正在为第 {i+1}/{len(news_list)} 条新闻生成摘要...")
        summary_ai, tokens = generate_ai_summary(news['title'], news['summary'])
        news['summary_ai'] = summary_ai
        total_tokens += tokens
    return total_tokens

# ====================== 邮件 HTML 模板 ======================
HTML_TEMPLATE_HEAD = """
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=yes">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
            font-size: 16px;
            line-height: 1.5;
            color: #333;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            padding: 24px 20px;
        }}
        h1 {{
            color: #2c3e50;
            font-size: 26px;
            margin-top: 0;
            margin-bottom: 16px;
            border-bottom: 2px solid #3498db;
            padding-bottom: 10px;
        }}
        .meta {{
            color: #7f8c8d;
            font-size: 14px;
            margin-bottom: 20px;
        }}
        .stats {{
            background-color: #f0f7ff;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
            border-left: 4px solid #3498db;
        }}
        .stats p {{
            margin: 5px 0;
        }}
        .stats ul {{
            margin: 5px 0 0 20px;
            padding-left: 0;
        }}
        .token-stats {{
            background-color: #f0f0f0;
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 13px;
            border-left: 4px solid #e67e22;
        }}
        .tag {{
            font-size: 20px;
            font-weight: 600;
            color: #2980b9;
            margin-top: 28px;
            margin-bottom: 12px;
            border-left: 4px solid #3498db;
            padding-left: 12px;
        }}
        .news-item {{
            margin-bottom: 32px;
            padding-bottom: 20px;
            border-bottom: 1px solid #ecf0f1;
        }}
        .news-title {{
            font-size: 18px;
            font-weight: 700;
            margin-bottom: 8px;
            line-height: 1.4;
        }}
        .news-title a {{
            color: #2c3e50;
            text-decoration: none;
        }}
        .news-title a:hover {{
            color: #3498db;
            text-decoration: underline;
        }}
        .source-date {{
            font-size: 13px;
            color: #95a5a6;
            margin-bottom: 12px;
        }}
        .summary {{
            font-size: 15px;
            line-height: 1.6;
            color: #2c3e50;
            margin: 12px 0;
        }}
        .summary p {{
            margin: 0 0 12px 0;
        }}
        .link {{
            font-size: 14px;
            margin-top: 8px;
        }}
        .link a {{
            color: #3498db;
            text-decoration: none;
            font-weight: 500;
        }}
        .link a:hover {{
            text-decoration: underline;
        }}
        .footer {{
            margin-top: 32px;
            text-align: center;
            font-size: 12px;
            color: #95a5a6;
            border-top: 1px solid #ecf0f1;
            padding-top: 16px;
        }}
        @media (max-width: 600px) {{
            body {{
                padding: 12px;
            }}
            .container {{
                padding: 16px;
            }}
            h1 {{
                font-size: 22px;
            }}
            .tag {{
                font-size: 18px;
            }}
            .news-title {{
                font-size: 17px;
            }}
            .summary {{
                font-size: 14px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📰 优申内娱</h1>
        <div class="meta">更新时间：{update_time}</div>
        <div class="meta">本次共发送 {news_count} 条最新新闻</div>
        {stats_html}
        {token_html}
"""

HTML_TEMPLATE_FOOT = """
        <div class="footer">
            本邮件由优申AI新闻助理精选呈送
        </div>
    </div>
</body>
</html>
"""

def split_into_paragraphs(text):
    """将文本按逻辑分割成段落列表"""
    text = text.replace('\\n', '\n')
    if '\n\n' in text:
        parts = [p.strip() for p in text.split('\n\n') if p.strip()]
    else:
        parts = [p.strip() for p in text.split('\n') if p.strip()]
    if not parts:
        text_no_newline = text.replace('\n', ' ')
        sentences = re.split(r'(?<=[。！？!?;；\.])', text_no_newline)
        parts = [s.strip() for s in sentences if s.strip()]
    if len(parts) == 1 and len(parts[0]) > PARAGRAPH_CHUNK_SIZE:
        text = parts[0]
        parts = [text[i:i+PARAGRAPH_CHUNK_SIZE] for i in range(0, len(text), PARAGRAPH_CHUNK_SIZE)]
    if not parts:
        parts = [text.strip()]
    return parts

def send(news_list, total_unique, source_unique_stats, source_sent_counts,
         token_used, daily_usage, total_usage):
    if not news_list:
        logger.info("无新新闻")
        return True

    # 构建统计信息 HTML
    stats_lines = []
    stats_lines.append('<div class="stats">')
    stats_lines.append(f'<p>📊 本次去重后新闻总数：<strong>{total_unique}</strong> 条</p>')
    stats_lines.append('<p>📡 各网站去重后数量分布：</p>')
    stats_lines.append('<ul>')
    for name, count in sorted(source_unique_stats.items(), key=lambda x: x[1], reverse=True):
        sent = source_sent_counts.get(name, 0)
        if sent > 0:
            stats_lines.append(f'<li>{name}：{count} 条（精选 {sent} 条）</li>')
        else:
            stats_lines.append(f'<li>{name}：{count} 条</li>')
    stats_lines.append('</ul>')
    stats_lines.append('</div>')
    stats_html = '\n'.join(stats_lines)

    # Token 统计（只显示本次、今日、汇总累计）
    token_lines = []
    token_lines.append('<div class="token-stats">')
    token_lines.append('<p>💰 Token 消耗统计</p>')
    token_lines.append(f'<p>本次摘要生成：<strong>{token_used}</strong> tokens</p>')
    token_lines.append(f'<p>今日累计：<strong>{daily_usage}</strong> tokens</p>')
    token_lines.append(f'<p>汇总累计：<strong>{total_usage}</strong> tokens</p>')
    token_lines.append('</div>')
    token_html = '\n'.join(token_lines)

    html = HTML_TEMPLATE_HEAD.format(
        update_time=datetime.now().strftime('%Y-%m-%d %H:%M'),
        news_count=len(news_list),
        stats_html=stats_html,
        token_html=token_html
    )

    grouped = defaultdict(list)
    for n in news_list:
        first_tag = n['primary_tag'] if n.get('primary_tag') else "综合新闻"
        grouped[first_tag].append(n)

    for tag, items in grouped.items():
        html += f'<div class="tag">{tag}</div>'
        for item in items:
            summary_text = item.get('summary_ai', item['summary'][:300])
            parts = split_into_paragraphs(summary_text)
            summary_html = ''.join(f'<p>{p}</p>' for p in parts if p)
            summary_html = re.sub(r'<[^>]+>', '', summary_html)

            html += f"""
            <div class="news-item">
                <div class="news-title"><a href="{item['link']}" target="_blank">{item['title']}</a></div>
                <div class="source-date">来源：{item['source_name']} | 日期：{item.get('published', '未知时间')} | 语言：{'英文' if item.get('language') == 'en' else '中文'}</div>
                <div class="summary">{summary_html}</div>
                <div class="link"><a href="{item['link']}" target="_blank">阅读原文 →</a></div>
            </div>
            """

    html += HTML_TEMPLATE_FOOT

    try:
        msg = MIMEText(html, 'html', 'utf-8')
        msg['From'] = formataddr(("优申内娱", FROM_EMAIL))
        msg['To'] = TO_EMAIL
        msg['Subject'] = Header(f"优申内娱 {datetime.now().strftime('%Y-%m-%d %H:%M')}", 'utf-8')
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as s:
            s.login(FROM_EMAIL, FROM_PASSWORD)
            s.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())
        logger.info("邮件发送成功")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败：{e}")
        return False

# ====================== 入口 ======================
if __name__ == "__main__":
    try:
        # 加载配置文件
        CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
        if not os.path.exists(CONFIG_FILE):
            raise Exception(f"未找到配置文件 {CONFIG_FILE}，请创建并填入 {{'email_pass':'你的QQ邮箱授权码', 'deepseek_api_key':'你的DeepSeek API Key'}}")
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        FROM_PASSWORD = config.get("email_pass", "").strip()
        if not FROM_PASSWORD:
            raise Exception("请在 config.json 中填写 email_pass")
        DEEPSEEK_API_KEY = config.get("deepseek_api_key", "").strip()
        if not DEEPSEEK_API_KEY:
            logger.warning("未配置 deepseek_api_key，将跳过 AI 摘要，仅使用原始摘要")

        # ========== 测试 SMTP 连接 ==========
        try:
            test_smtp = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=10)
            test_smtp.login(FROM_EMAIL, FROM_PASSWORD)
            test_smtp.quit()
            logger.info("✅ SMTP 连接测试成功")
        except Exception as e:
            raise Exception(f"❌ SMTP 连接测试失败，请检查邮箱授权码: {e}")

        # ========== 测试 DeepSeek API Key（可选，失败不退出） ==========
        if DEEPSEEK_API_KEY:
            try:
                headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
                resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=10)
                if resp.status_code == 200:
                    logger.info("✅ DeepSeek API Key 测试成功")
                else:
                    logger.error(f"❌ DeepSeek API Key 无效（状态码 {resp.status_code}），将跳过 AI 摘要，仅使用原始摘要")
                    DEEPSEEK_API_KEY = None
            except Exception as e:
                logger.error(f"❌ DeepSeek API 连接测试失败: {e}，将跳过 AI 摘要，仅使用原始摘要")
                DEEPSEEK_API_KEY = None
        else:
            logger.warning("⚠️ 未配置 deepseek_api_key，将跳过 AI 摘要，仅使用原始摘要")

        # 加载 keywords.json
        KEYWORDS_FILE = os.path.join(SCRIPT_DIR, "keywords.json")
        if not os.path.exists(KEYWORDS_FILE):
            raise Exception(f"未找到关键词配置文件 {KEYWORDS_FILE}，请创建并填入正确的分类关键词。")
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            KEYWORDS = json.load(f)
        logger.info("配置文件加载成功。")

        # 开始主流程
        logger.info("开始抓取新闻...")
        all_news, source_unique_stats = fetch_news_without_summary()
        total_unique = len(all_news)
        news_to_send, source_sent_counts = select_news_diverse_sources(all_news, MAX_NEWS_PER_EMAIL)

        if news_to_send:
            logger.info(f"已选择 {len(news_to_send)} 条新闻（优先不同源+英文+大类循环）")
            logger.info(f"去重后新闻总数：{total_unique} 条")
            logger.info("各网站去重后数量分布：")
            for name, count in source_unique_stats.items():
                sent = source_sent_counts.get(name, 0)
                if sent:
                    logger.info(f"  - {name}: {count} 条（精选 {sent} 条）")
                else:
                    logger.info(f"  - {name}: {count} 条")

            token_used = generate_summaries_for_news(news_to_send)
            daily_usage, _, _, total_usage = get_usage_stats()

            ok = send(news_to_send, total_unique, source_unique_stats, source_sent_counts,
                      token_used, daily_usage, total_usage)
            if ok:
                add_sent_titles([x['title'] for x in news_to_send])
                # 更新关键词循环记录
                primary_tags = [n.get('primary_tag', '综合新闻') for n in news_to_send]
                update_keyword_rotation(primary_tags)
        else:
            logger.info("没有新新闻")
    except Exception as e:
        logger.exception(f"发生错误：{e}")
    finally:
        input("按回车键退出...")