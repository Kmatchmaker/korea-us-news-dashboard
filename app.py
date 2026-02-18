import re
import hashlib
from datetime import datetime
from urllib.parse import quote, urljoin

import pandas as pd
import requests
import streamlit as st
import feedparser
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# -----------------------------
# Config
# -----------------------------
CONFIG_PATH = "config.yaml"
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/1.0)"
HEADERS = {"User-Agent": USER_AGENT}

# 기본 표시 연도(원하면 2026 -> "전체" 로 바꿔도 됨)
DEFAULT_YEAR_FILTER = 2026


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()

states_cfg = cfg.get("states", {})
companies = cfg.get("companies", [])
korean_queries = cfg.get("korean_queries", [])
us_sources = cfg.get("us_sources", [])


# -----------------------------
# Utilities
# -----------------------------
_ws = re.compile(r"\s+")


def norm_text(s: str) -> str:
    return _ws.sub(" ", (s or "").strip())


def norm_query_for_url(q: str) -> str:
    # 줄바꿈/여러 공백 제거 -> URL-safe encode
    q = norm_text(q)
    return quote(q)


def safe_parse_date(s: str):
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
        return dt
    except Exception:
        return None


def detect_state(text: str) -> str:
    t = norm_text(text).lower()
    for code, names in (states_cfg or {}).items():
        # config.yaml에서 states를 ["Georgia", ...] 형태로 뒀을 수도 있고
        # {names:[...]} 형태일 수도 있으니 둘 다 지원
        if isinstance(names, dict):
            names_list = names.get("names", [])
        else:
            names_list = names

        for n in names_list:
            if norm_text(str(n)).lower() in t:
                return code
    return "Global"


def detect_company(text: str) -> str:
    t = norm_text(text)
    for c in sorted(companies, key=len, reverse=True):
        if c and c in t:
            return c
    return "Unknown"


def make_core(title: str, summary: str | None = None) -> str:
    s = norm_text(summary or "")
    if s:
        return (s[:180] + "…") if len(s) > 180 else s
    t = norm_text(title)
    return (t[:180] + "…") if len(t) > 180 else t


def make_id(title: str, url: str, provider: str) -> str:
    raw = f"{provider}||{norm_text(title)}||{norm_text(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def to_display_date(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.strftime("%Y.%m.%d")
    except Exception:
        return ""


def link_md(label: str, url: str) -> str:
    return f"[{label}]({url})"


# -----------------------------
# Fetch: Korean (Google News RSS)
# -----------------------------
@st.cache_data(ttl=60 * 30)  # 30분 캐시(스트림릿 재실행 시 과도한 호출 방지)
def fetch_korean_news(limit_per_query: int = 20):
    results = []

    for q in korean_queries:
        q_encoded = norm_query_for_url(q)
        url = f"https://news.google.com/rss/search?q={q_encoded}&hl=ko&gl=KR&ceid=KR:ko"

        feed = feedparser.parse(url)

        for e in feed.entries[:limit_per_query]:
            title = norm_text(getattr(e, "title", ""))
            link = norm_text(getattr(e, "link", ""))
            published_raw = getattr(e, "published", "") or getattr(e, "updated", "")
            dt = safe_parse_date(published_raw)

            summary = getattr(e, "summary", None) or getattr(e, "description", None)

            if not title or not link:
                continue

            results.append(
                {
                    "provider": "KOREAN",
                    "source": "Google News (KR)",
                    "state": detect_state(title),
                    "company": detect_company(title),
                    "published_at": dt,
                    "core": make_core(title, summary),
                    "title": title,
                    "url": link,
                }
            )

    # 중복 제거
    dedup = {}
    for r in results:
        k = make_id(r["title"], r["url"], r["provider"])
        dedup[k] = r

    return list(dedup.values())


# -----------------------------
# Fetch: US sources (HTML list)
# -----------------------------
def guess_items_from_page(html: str, base_url: str, max_items: int = 40):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # 1) article 기반
    for art in soup.select("article"):
        a = art.select_one("a[href]")
        if not a:
            continue
        title = norm_text(a.get_text(" "))
        href = norm_text(a.get("href", ""))
        if not title or not href:
            continue
        full_url = urljoin(base_url, href)

        # 날짜 후보
        date_text = None
        time_tag = art.select_one("time")
        if time_tag:
            date_text = norm_text(time_tag.get("datetime") or time_tag.get_text(" "))

        items.append((title, full_url, date_text))

    # 2) 그래도 없으면 링크 리스트
    if not items:
        for a in soup.select("a[href]"):
            title = norm_text(a.get_text(" "))
            href = norm_text(a.get("href", ""))
            if not title or not href:
                continue
            if len(title) < 12:  # 너무 짧은 메뉴/내비게이션 제외
                continue
            full_url = urljoin(base_url, href)
            items.append((title, full_url, None))

    # 중복 제거 + 상위 N개
    seen = set()
    out = []
    for t, u, d in items:
        key = (t, u)
        if key in seen:
            continue
        seen.add(key)
        out.append((t, u, d))
        if len(out) >= max_items:
            break
    return out


@st.cache_data(ttl=60 * 30)
def fetch_us_news(max_items_per_source: int = 30):
    results = []
    for src in us_sources:
        name = src.get("name", "US Source")
        url = src.get("url")
        if not url:
            continue

        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = guess_items_from_page(r.text, url, max_items=max_items_per_source)
        except Exception:
            continue

        for title, link, date_text in items:
            dt = safe_parse_date(date_text) if date_text else None
            results.append(
                {
                    "provider": "US",
                    "source": name,
                    "state": detect_state(title),
                    "company": detect_company(title),
                    "published_at": dt,
                    "core": make_core(title, None),
                    "title": title,
                    "url": link,
                }
            )

    # 중복 제거
    dedup = {}
    for r in results:
        k = make_id(r["title"], r["url"], r["provider"])
        dedup[k] = r

    return list(dedup.values())


# -----------------------------
# Render
# -----------------------------
def build_table(rows):
    if not rows:
        return pd.DataFrame(columns=["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"])

    df = pd.DataFrame(rows)

    # 발행일 처리: 없으면 빈값
    df["뉴스 발행일"] = df["published_at"].apply(to_display_date)

    # 표 컬럼 매핑
    df["주(State)"] = df["state"]
    df["기업명"] = df["company"]
    df["핵심 내용"] = df["core"]
    df["원문 확인"] = df.apply(lambda r: link_md(r["source"], r["url"]), axis=1)

    # 사용자가 클릭하기 좋은 "기사 제목"도 보조로 제공
    df["기사 제목"] = df.apply(lambda r: link_md(r["title"], r["url"]), axis=1)

    return df[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "기사 제목", "원문 확인"]]


def apply_filters(df: pd.DataFrame, year_filter, state_filter, company_text, keyword_text):
    out = df.copy()

    # 연도 필터
    if year_filter != "전체":
        y = int(year_filter)
        # 뉴스 발행일이 빈값이면 제외
        out = out[out["뉴스 발행일"].str.startswith(str(y))]

    # 주 필터
    if state_filter != "전체":
        out = out[out["주(State)"] == state_filter]

    # 기업 필터
    if company_text.strip():
        c = company_text.strip().lower()
        out = out[
            out["기업명"].str.lower().str.contains(c, na=False)
            | out["기사 제목"].str.lower().str.contains(c, na=False)
        ]

    # 키워드 필터
    if keyword_text.strip():
        k = keyword_text.strip().lower()
        out = out[
            out["핵심 내용"].str.lower().str.contains(k, na=False)
            | out["기사 제목"].str.lower().str.contains(k, na=False)
        ]

    return out


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="미국 동남부 진출 한국기업 뉴스 상황판", layout="wide")
st.title("📰 미국 동남부(GA/TN/AL/SC/FL) 진출 한국기업 뉴스 상황판")
st.caption("탭으로 한국어 뉴스 vs 미국(주정부/기관) 소스를 분리해서 보여줍니다. (GitHub만으로 운영 가능)")

with st.sidebar:
    st.subheader("필터")
    year_filter = st.selectbox("발행 연도", [DEFAULT_YEAR_FILTER, 2025, 2024, "전체"], index=0)
    state_filter = st.selectbox("주(State)", ["전체", "GA", "TN", "AL", "SC", "FL", "Global"], index=0)
    company_text = st.text_input("기업명 검색(부분)", "")
    keyword_text = st.text_input("키워드(제목/내용)", "")

    st.markdown("---")
    if st.button("🔄 캐시 새로고침(강제 재수집)"):
        st.cache_data.clear()
        st.rerun()

tab1, tab2 = st.tabs(["🇰🇷 한국어 뉴스", "🇺🇸 미국(주정부·기관) 뉴스"])

with tab1:
    st.subheader("한국어 뉴스 (Google News RSS)")
    rows = fetch_korean_news()
    df = build_table(rows)
    df2 = apply_filters(df, year_filter, state_filter, company_text, keyword_text)
    st.caption(f"표시: {len(df2)}건 / 전체 수집: {len(df)}건")
    st.dataframe(df2, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("미국 소스 (주정부/기관 웹페이지)")
    rows = fetch_us_news()
    df = build_table(rows)
    df2 = apply_filters(df, year_filter, state_filter, company_text, keyword_text)
    st.caption(f"표시: {len(df2)}건 / 전체 수집: {len(df)}건")
    st.dataframe(df2, use_container_width=True, hide_index=True)

st.markdown("---")
st.write(
    "팁: 미국 소스는 사이트 구조가 바뀌면 링크 추출이 달라질 수 있어요. "
    "그럴 땐 해당 소스만 '전용 파서'로 맞춤 처리하면 정확도가 크게 올라갑니다."
)
