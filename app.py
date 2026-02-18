import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote, urljoin

import pandas as pd
import requests
import streamlit as st
import feedparser
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# -----------------------------
# Settings
# -----------------------------
CONFIG_PATH = "config.yaml"
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/2.0)"
HEADERS = {"User-Agent": USER_AGENT}

DEFAULT_YEAR_FILTER = 2026          # 기본 2026년만 보여주기 (원하면 "전체"로 바꾸면 됨)
TOP_OTHER_UPDATES = 12              # 대기업 외 '기타 주요 업데이트' 노출 개수


# -----------------------------
# Load config
# -----------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()
states_cfg = cfg.get("states", {})
companies = cfg.get("companies", [])
priority_companies = cfg.get("priority_companies", [])  # << 추가
korean_queries = cfg.get("korean_queries", [])
us_sources = cfg.get("us_sources", [])


# -----------------------------
# Utilities
# -----------------------------
_ws = re.compile(r"\s+")
_html_tag = re.compile(r"<[^>]+>")


def norm_text(s: str) -> str:
    return _ws.sub(" ", (s or "").strip())


def strip_html(s: str) -> str:
    return norm_text(_html_tag.sub(" ", s or ""))


def norm_query_for_url(q: str) -> str:
    q = norm_text(q)
    return quote(q)


def safe_parse_date(s: str):
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
        if dt and not dt.tzinfo:
            # timezone 없는 경우 UTC로 가정
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def to_display_date(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.astimezone(timezone.utc).strftime("%Y.%m.%d")
    except Exception:
        try:
            return dt.strftime("%Y.%m.%d")
        except Exception:
            return ""


def link_md(label: str, url: str) -> str:
    return f"[{label}]({url})"


def make_id(provider: str, title: str, url: str) -> str:
    raw = f"{provider}||{norm_text(title)}||{norm_text(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def detect_state(text: str) -> str:
    t = norm_text(text).lower()
    for code, names in (states_cfg or {}).items():
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
    # 긴 이름 우선
    for c in sorted(companies, key=len, reverse=True):
        if c and c in t:
            return c
    return "Unknown"


# -----------------------------
# Importance scoring (rules)
# -----------------------------
INVEST_TAGS = ["투자", "공장", "증설", "설립", "법인", "지사", "진출", "신규", "확장", "캠퍼스", "라인"]
DEAL_TAGS = ["수주", "계약", "공급", "체결", "MOU", "협약", "파트너십"]
STATUS_TAGS = ["실적", "매출", "판매", "점유율", "기록", "가동", "생산", "관세", "정책", "규제", "전망", "가이던스"]
US_TAGS = ["미국", "북미", "U.S.", "US", "America", "Georgia", "Tennessee", "Alabama", "Florida", "South Carolina", "GA", "TN", "AL", "FL", "SC"]


def importance_score(row: dict) -> int:
    title = row.get("title", "") or ""
    core = row.get("core", "") or ""
    text = f"{title} {core}"

    score = 0
    company = row.get("company", "Unknown")

    if company in priority_companies:
        score += 100
    if any(k in text for k in INVEST_TAGS):
        score += 35
    if any(k in text for k in DEAL_TAGS):
        score += 25
    if any(k in text for k in STATUS_TAGS):
        score += 15
    if any(k in text for k in US_TAGS):
        score += 10

    # 한국어 기사(보통 요약 품질이 좋음) 약간 가점
    if row.get("provider") == "KOREAN":
        score += 5

    return score


# -----------------------------
# Korean summary extraction (no LLM)
# - 한국어 기사: RSS summary/description + 제목 기반으로 한 줄 요약
# - 영어 기사: title/메타설명 그대로 (번역 API 없으면 완벽한 한글화 불가)
# -----------------------------
def make_korean_core(title: str, summary_html: str | None) -> str:
    summary = strip_html(summary_html or "")
    if summary:
        return (summary[:180] + "…") if len(summary) > 180 else summary
    t = norm_text(title)
    return (t[:180] + "…") if len(t) > 180 else t


def fetch_meta_description(url: str) -> str | None:
    # US 소스 등에서 "핵심내용"을 조금이라도 확보하기 위해 og:description/meta description 사용
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        og = soup.select_one('meta[property="og:description"]')
        if og and og.get("content"):
            return norm_text(og.get("content"))
        md = soup.select_one('meta[name="description"]')
        if md and md.get("content"):
            return norm_text(md.get("content"))
        return None
    except Exception:
        return None


# -----------------------------
# Fetch: Korean (Google News RSS)
# -----------------------------
@st.cache_data(ttl=60 * 20)
def fetch_korean_news():
    rows = []

    for q in korean_queries:
        q_encoded = norm_query_for_url(q)
        url = f"https://news.google.com/rss/search?q={q_encoded}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        for e in feed.entries[:40]:
            title = norm_text(getattr(e, "title", ""))
            link = norm_text(getattr(e, "link", ""))
            published_raw = getattr(e, "published", "") or getattr(e, "updated", "")
            dt = safe_parse_date(published_raw)
            summary = getattr(e, "summary", None) or getattr(e, "description", None)

            if not title or not link:
                continue

            row = {
                "provider": "KOREAN",
                "source": "Google News (KR)",
                "title": title,
                "url": link,
                "published_at": dt,
                "state": detect_state(title),
                "company": detect_company(title),
                "core": make_korean_core(title, summary),
            }
            rows.append(row)

    # dedup
    dedup = {}
    for r in rows:
        dedup[make_id(r["provider"], r["title"], r["url"])] = r
    return list(dedup.values())


# -----------------------------
# Fetch: US sources (HTML list + meta description)
# -----------------------------
def guess_items_from_page(html: str, base_url: str, max_items: int = 60):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for art in soup.select("article"):
        a = art.select_one("a[href]")
        if not a:
            continue
        title = norm_text(a.get_text(" "))
        href = norm_text(a.get("href", ""))
        if not title or not href:
            continue
        full_url = urljoin(base_url, href)

        date_text = None
        time_tag = art.select_one("time")
        if time_tag:
            date_text = norm_text(time_tag.get("datetime") or time_tag.get_text(" "))

        items.append((title, full_url, date_text))

    if not items:
        for a in soup.select("a[href]"):
            title = norm_text(a.get_text(" "))
            href = norm_text(a.get("href", ""))
            if not title or not href:
                continue
            if len(title) < 12:
                continue
            full_url = urljoin(base_url, href)
            items.append((title, full_url, None))

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
def fetch_us_news():
    rows = []
    for src in us_sources:
        name = src.get("name", "US Source")
        url = src.get("url")
        if not url:
            continue

        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = guess_items_from_page(r.text, url, max_items=50)
        except Exception:
            continue

        for title, link, date_text in items:
            dt = safe_parse_date(date_text) if date_text else None

            # 핵심내용을 조금이라도 확보 (메타 설명)
            meta_desc = fetch_meta_description(link)
            core = meta_desc if meta_desc else title

            row = {
                "provider": "US",
                "source": name,
                "title": title,
                "url": link,
                "published_at": dt,
                "state": detect_state(title),
                "company": detect_company(title),
                "core": core,
            }
            rows.append(row)

    dedup = {}
    for r in rows:
        dedup[make_id(r["provider"], r["title"], r["url"])] = r
    return list(dedup.values())


# -----------------------------
# Build & filter
# -----------------------------
def build_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인", "_when", "_score", "_provider"])

    df = pd.DataFrame(rows)
    df["_when"] = df["published_at"].apply(lambda x: x if x else None)
    df["_when"] = pd.to_datetime(df["_when"], errors="coerce", utc=True)

    # 날짜 없는 건 최근성 정렬에서 밀리게 처리
    now = pd.Timestamp.now(tz="UTC")
    df["_when"] = df["_when"].fillna(now - pd.Timedelta(days=3650))

    df["_score"] = df.apply(lambda r: importance_score(r.to_dict()), axis=1)

    df["뉴스 발행일"] = df["_when"].dt.strftime("%Y.%m.%d")
    df["주(State)"] = df["state"]
    df["기업명"] = df["company"]
    df["핵심 내용"] = df["core"]
    df["원문 확인"] = df.apply(lambda r: link_md(r["source"], r["url"]), axis=1)
    df["_provider"] = df["provider"]

    # 보기 좋은 링크(제목 클릭)
    df["기사 제목"] = df.apply(lambda r: link_md(r["title"], r["url"]), axis=1)

    # 정렬: 최근 우선, 중요도 우선
    df = df.sort_values(by=["_when", "_score"], ascending=[False, False])
    return df


def apply_year_state_filters(df: pd.DataFrame, year_filter, state_filter):
    out = df.copy()
    if year_filter != "전체":
        y = int(year_filter)
        out = out[out["뉴스 발행일"].str.startswith(str(y))]
    if state_filter != "전체":
        out = out[out["주(State)"] == state_filter]
    return out


def pick_top_one_per_company(df: pd.DataFrame, company_list: list[str]) -> pd.DataFrame:
    # 우선 기업 리스트에 해당하는 것만
    subset = df[df["기업명"].isin(company_list)].copy()
    if subset.empty:
        return subset
    # 기업당 1개(가장 최근/중요)
    top = subset.sort_values(by=["_when", "_score"], ascending=[False, False]).groupby("기업명", as_index=False).head(1)
    # 기업리스트 순서대로 보이게
    order_map = {c: i for i, c in enumerate(company_list)}
    top["_order"] = top["기업명"].map(lambda x: order_map.get(x, 9999))
    return top.sort_values(by=["_order"], ascending=True).drop(columns=["_order"])


def pick_other_updates(df: pd.DataFrame, exclude_companies: list[str], n: int) -> pd.DataFrame:
    other = df[~df["기업명"].isin(exclude_companies)].copy()
    if other.empty:
        return other
    # Unknown 회사라도 투자/진출 기사면 남기고 싶어서 score 기반 유지
    other = other.sort_values(by=["_when", "_score"], ascending=[False, False]).head(n)
    return other


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="미국 동남부 진출 한국기업 뉴스 상황판", layout="wide")
st.title("📰 미국 동남부 진출 한국기업 뉴스 상황판")
st.caption("대기업은 기업당 1개(최신/중요)만 보여주고, 그 외 신규 투자/진출/미국사업 현황도 최신순으로 별도 표시합니다.")

with st.sidebar:
    st.subheader("필터")
    year_filter = st.selectbox("발행 연도", [DEFAULT_YEAR_FILTER, 2025, 2024, "전체"], index=0)
    state_filter = st.selectbox("주(State)", ["전체", "GA", "TN", "AL", "SC", "FL", "Global"], index=0)

    st.markdown("---")
    st.write("대기업(우선 표시):")
    st.code(", ".join(priority_companies) if priority_companies else "(config.yaml에 priority_companies 추가 필요)")

    if st.button("🔄 캐시 새로고침(강제 재수집)"):
        st.cache_data.clear()
        st.rerun()

tab1, tab2 = st.tabs(["🇰🇷 한국어 뉴스", "🇺🇸 미국(주정부·기관/언론)"])

def render(provider: str):
    if provider == "KOREAN":
        rows = fetch_korean_news()
    else:
        rows = fetch_us_news()

    df = build_df(rows)
    df = apply_year_state_filters(df, year_filter, state_filter)

    # 1) 대기업: 기업당 1개
    st.subheader("⭐ 대기업 최신 핵심 뉴스 (기업당 1개)")
    top_big = pick_top_one_per_company(df, priority_companies)
    if top_big.empty:
        st.info("해당 조건에서 대기업 뉴스가 아직 없습니다. (쿼리/기업명 매칭을 확장해보세요)")
    else:
        st.dataframe(
            top_big[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "기사 제목", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
        )

    # 2) 기타 업데이트: 투자/진출/미국 사업현황 등
    st.subheader("🆕 신규 투자·진출 및 미국 사업 현황 (최신)")
    other = pick_other_updates(df, exclude_companies=priority_companies, n=TOP_OTHER_UPDATES)
    if other.empty:
        st.info("해당 조건에서 추가 주요 업데이트가 없습니다.")
    else:
        st.dataframe(
            other[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "기사 제목", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
        )

    st.caption(f"수집 건수: {len(df)} (필터 적용 후)")

with tab1:
    st.write("한국어 뉴스는 RSS 요약/메타정보 기반으로 ‘핵심 내용’이 대체로 한국어로 잘 나옵니다.")
    render("KOREAN")

with tab2:
    st.write("미국 소스는 사이트마다 요약이 영어일 수 있습니다(og:description 기반). 필요하면 번역 옵션을 추가할 수 있어요.")
    render("US")
