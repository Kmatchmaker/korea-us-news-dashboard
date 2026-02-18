import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import streamlit as st
import feedparser
import yaml
from dateutil import parser as dateparser


# ============================
# CONFIG
# ============================
CONFIG_PATH = "config.yaml"

MAX_COMPANY_SHOW = 10   # 최대 10개 기업만 표시


# ============================
# Load config
# ============================
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()

companies = cfg.get("companies", [])
priority_companies = cfg.get("priority_companies", companies[:10])

korean_queries = cfg.get("korean_queries", [])

# 미국용 추가 검색식 (기업 진출 뉴스 놓치지 않기)
us_queries = cfg.get(
    "us_queries",
    [
        '(Georgia OR Tennessee OR Alabama OR Florida OR "South Carolina") '
        '(Korean company OR Hyundai OR Samsung OR SK OR LG OR Hanwha OR Dongwon) '
        '(investment OR plant OR expansion OR contract OR subsidiary)'
    ],
)

states_cfg = cfg.get("states", {})


# ============================
# Utils
# ============================
_ws = re.compile(r"\s+")


def norm_text(s: str) -> str:
    return _ws.sub(" ", (s or "").strip())


def safe_parse_date(s: str):
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
        if dt and not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def detect_state(text: str) -> str:
    t = text.lower()
    for code, names in states_cfg.items():
        for n in names:
            if n.lower() in t:
                return code
    return "Global"


def detect_company(text: str) -> str:
    for c in sorted(priority_companies, key=len, reverse=True):
        if c in text:
            return c
    return "Unknown"


# ============================
# TAG 자동 분류
# ============================
def classify_tag(text: str) -> str:
    if any(k in text for k in ["투자", "공장", "설립", "증설", "진출"]):
        return "[신규 투자]"
    if any(k in text for k in ["수주", "계약", "공급", "체결"]):
        return "[수주 대박]"
    if any(k in text for k in ["증자", "출자", "공시"]):
        return "[자본 증자]"
    if any(k in text for k in ["판매", "기록", "돌파", "매출"]):
        return "[판매 기록]"
    return "[주요 뉴스]"


# ============================
# Fetch Google News RSS
# ============================
def fetch_google_news(queries, provider="KOREAN"):
    rows = []
    for q in queries:
        q_encoded = quote(norm_text(q))
        url = f"https://news.google.com/rss/search?q={q_encoded}&hl=ko&gl=KR&ceid=KR:ko"

        feed = feedparser.parse(url)

        for e in feed.entries[:40]:
            title = norm_text(e.title)
            link = norm_text(e.link)
            published = getattr(e, "published", "")

            dt = safe_parse_date(published)

            company = detect_company(title)
            if company == "Unknown":
                continue

            tag = classify_tag(title)

            rows.append(
                {
                    "기업명": company,
                    "주(State)": detect_state(title),
                    "뉴스 발행일": dt.strftime("%Y.%m.%d") if dt else "",
                    "핵심 내용": f"{tag} {title}",
                    "원문 확인": link,
                }
            )

    return rows


# ============================
# 기업당 최신 1개만 선택
# ============================
def pick_latest_per_company(rows):
    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df["뉴스 발행일_dt"] = pd.to_datetime(df["뉴스 발행일"], errors="coerce")
    df = df.sort_values("뉴스 발행일_dt", ascending=False)

    # 기업당 최신 1개
    top = df.groupby("기업명", as_index=False).head(1)

    # 최대 10개 기업만
    top = top.head(MAX_COMPANY_SHOW)

    return top.drop(columns=["뉴스 발행일_dt"])


# ============================
# Streamlit UI
# ============================
st.set_page_config(page_title="미국 진출 한국기업 뉴스 상황판", layout="wide")

st.title("📰 미국 동남부 진출 한국기업 뉴스 TOP10 상황판")
st.caption("기업당 최신 뉴스 1개씩 자동 표시 + 원문 링크 클릭 가능")

tab1, tab2 = st.tabs(["🇰🇷 한국어 뉴스", "🇺🇸 미국발 뉴스/주정부 포함"])


# ----------------------------
# 한국어 뉴스 탭
# ----------------------------
with tab1:
    st.subheader("대기업 최신 뉴스 (기업당 1개)")

    rows = fetch_google_news(korean_queries, provider="KOREAN")
    top = pick_latest_per_company(rows)

    if top.empty:
        st.warning("기업 뉴스가 없습니다. config.yaml 기업명을 확장하세요.")
    else:
        st.dataframe(
            top,
            use_container_width=True,
            hide_index=True,
            column_config={
                "원문 확인": st.column_config.LinkColumn("원문 확인")
            },
        )


# ----------------------------
# 미국 뉴스 탭
# ----------------------------
with tab2:
    st.subheader("미국 주정부/현지언론 포함 최신 뉴스 (기업당 1개)")

    rows = fetch_google_news(us_queries, provider="US")
    top = pick_latest_per_company(rows)

    if top.empty:
        st.warning("미국 뉴스가 없습니다. us_queries를 확장하세요.")
    else:
        st.dataframe(
            top,
            use_container_width=True,
            hide_index=True,
            column_config={
                "원문 확인": st.column_config.LinkColumn("원문 확인")
            },
        )

st.markdown("---")
st.write("✅ 표시 방식: 기업당 최신 기사 1개 + 투자/수주/증자/판매기록 자동 태그 + 원문 링크 제공")
