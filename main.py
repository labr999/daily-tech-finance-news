#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日科技/財經新聞日報
1. 從多個國際重要網站的 RSS 抓新聞
2. 依「跨來源被報導次數」+「來源權重」+「時間新舊」評分排序
3. 各取科技、財經前 20 條
4. 將標題翻譯成繁體中文，中英對照
5. 組成 HTML Email 寄出
"""

import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os

import feedparser
from deep_translator import GoogleTranslator

# ---------- 設定區 ----------

TAIPEI_TZ = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 30  # 只看過去幾小時內的新聞

# 來源權重：數字越大代表越重要（用來在同分時排序，以及輔助評分）
TECH_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/feed/", 3),
    ("The Verge", "https://www.theverge.com/rss/index.xml", 3),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", 3),
    ("Wired", "https://www.wired.com/feed/rss", 2),
    ("Engadget", "https://www.engadget.com/rss.xml", 2),
    ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", 3),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html", 3),
    ("Reuters Tech (Google News)",
     "https://news.google.com/rss/search?q=when:1d+allinurl:reuters.com+technology&hl=en-US&gl=US&ceid=US:en", 3),
]

FINANCE_FEEDS = [
    ("CNBC Finance", "https://www.cnbc.com/id/10001147/device/rss/rss.html", 3),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/", 2),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", 3),
    ("The Economist Finance", "https://www.economist.com/finance-and-economics/rss.xml", 3),
    ("Bloomberg (Google News)",
     "https://news.google.com/rss/search?q=when:1d+allinurl:bloomberg.com&hl=en-US&gl=US&ceid=US:en", 3),
    ("Reuters Business (Google News)",
     "https://news.google.com/rss/search?q=when:1d+allinurl:reuters.com+business&hl=en-US&gl=US&ceid=US:en", 3),
    ("Financial Times (Google News)",
     "https://news.google.com/rss/search?q=when:1d+allinurl:ft.com&hl=en-US&gl=US&ceid=US:en", 3),
]

TOP_N = 20

# ---------- 工具函式 ----------

def normalize_title(title: str) -> str:
    """把標題正規化，方便判斷是否為同一則新聞被多來源報導。"""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_similarity(a: str, b: str) -> float:
    """簡易字詞重疊率，用來聚合同一事件的不同報導。"""
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    union = len(wa | wb)
    return inter / union if union else 0.0


def fetch_feed_entries(name: str, url: str, weight: int):
    entries = []
    try:
        parsed = feedparser.parse(url)
        for idx, e in enumerate(parsed.entries[:30]):
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "")
            if not title or not link:
                continue
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    published = datetime(*getattr(e, key)[:6], tzinfo=timezone.utc)
                    break
            if published is None:
                published = datetime.now(timezone.utc)  # 沒有時間戳就當作剛發布
            entries.append({
                "title": title,
                "link": link,
                "source": name,
                "weight": weight,
                "published": published,
                "rank_in_feed": idx,  # 在該 RSS 中的順序，越前面通常越重要
            })
    except Exception as exc:
        print(f"[警告] 抓取 {name} 失敗：{exc}")
    return entries


def collect_and_score(feeds):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    raw = []
    for name, url, weight in feeds:
        raw.extend(fetch_feed_entries(name, url, weight))

    # 過濾太舊的新聞
    raw = [r for r in raw if r["published"] >= cutoff]

    # 依標題相似度聚類，同一事件多來源報導會合併，並記錄涵蓋的來源清單
    clusters = []
    for item in raw:
        norm = normalize_title(item["title"])
        placed = False
        for c in clusters:
            if title_similarity(norm, c["norm_title"]) >= 0.55:
                c["sources"].add(item["source"])
                c["items"].append(item)
                # 用權重較高、時間較新的當代表標題/連結
                if item["weight"] > c["best"]["weight"] or (
                    item["weight"] == c["best"]["weight"] and item["published"] > c["best"]["published"]
                ):
                    c["best"] = item
                placed = True
                break
        if not placed:
            clusters.append({
                "norm_title": norm,
                "sources": {item["source"]},
                "items": [item],
                "best": item,
            })

    # 評分：跨來源數 * 10 + 來源權重 + 排序位置分數 + 新鮮度分數
    scored = []
    for c in clusters:
        best = c["best"]
        cross_source_score = (len(c["sources"]) - 1) * 10  # 被越多不同來源報導分數越高
        weight_score = best["weight"]
        rank_score = max(0, 5 - best["rank_in_feed"])  # 在來源中排越前面分越高
        age_hours = (now - best["published"]).total_seconds() / 3600
        freshness_score = max(0, 10 - age_hours / 3)
        total = cross_source_score + weight_score + rank_score + freshness_score
        scored.append({
            "title": best["title"],
            "link": best["link"],
            "sources": sorted(c["sources"]),
            "score": total,
            "published": best["published"],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:TOP_N]
    attach_translations(top)
    return top


# ---------- 翻譯 ----------

def attach_translations(items):
    """幫每一則新聞的標題加上繁體中文翻譯（title_zh）。逐則翻譯失敗時退回原文。"""
    translator = GoogleTranslator(source="auto", target="zh-TW")
    for item in items:
        try:
            item["title_zh"] = translator.translate(item["title"])
        except Exception as exc:
            print(f"[警告] 翻譯失敗，改用原文：{item['title'][:40]}... ({exc})")
            item["title_zh"] = item["title"]
        time.sleep(0.3)  # 避免對免費翻譯服務發送過於密集的請求


# ---------- Email 組裝與寄送 ----------

def build_html(tech_news, finance_news) -> str:
    today = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")

    def section_html(title, items):
        rows = []
        for i, n in enumerate(items, 1):
            sources = "、".join(n["sources"])
            title_zh = n.get("title_zh", n["title"])
            rows.append(f"""
            <tr>
              <td style="padding:8px 6px;border-bottom:1px solid #eee;vertical-align:top;color:#888;">{i}</td>
              <td style="padding:8px 6px;border-bottom:1px solid #eee;">
                <a href="{n['link']}" style="color:#1a0dab;text-decoration:none;font-weight:600;">{title_zh}</a>
                <div style="color:#555;font-size:12.5px;margin-top:3px;">{n['title']}</div>
                <div style="color:#999;font-size:12px;margin-top:2px;">來源：{sources}</div>
              </td>
            </tr>""")
        return f"""
        <h2 style="color:#c0392b;border-bottom:2px solid #c0392b;padding-bottom:6px;">{title}</h2>
        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          {''.join(rows) if rows else '<tr><td style="padding:8px;color:#999;">今日暫無符合條件的新聞</td></tr>'}
        </table>
        """

    return f"""
    <html>
    <body style="font-family:Helvetica,Arial,sans-serif;max-width:680px;margin:0 auto;padding:16px;color:#222;">
      <h1 style="text-align:center;">📰 每日科技財經日報 — {today}</h1>
      {section_html("💻 科技新聞 Top 20", tech_news)}
      <div style="height:20px;"></div>
      {section_html("💰 財經新聞 Top 20", finance_news)}
      <p style="color:#aaa;font-size:12px;text-align:center;margin-top:24px;">
        本日報由 GitHub Actions 自動產生並寄送
      </p>
    </body>
    </html>
    """


def send_email(html_body: str):
    sender = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    today = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📰 每日科技財經日報 — {today}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=context)
        server.login(sender, app_password)
        server.sendmail(sender, recipient, msg.as_string())
    print(f"已寄送日報至 {recipient}")


def main():
    print("抓取科技新聞...")
    tech_news = collect_and_score(TECH_FEEDS)
    print(f"科技新聞：{len(tech_news)} 則")

    print("抓取財經新聞...")
    finance_news = collect_and_score(FINANCE_FEEDS)
    print(f"財經新聞：{len(finance_news)} 則")

    html = build_html(tech_news, finance_news)
    send_email(html)


if __name__ == "__main__":
    main()
