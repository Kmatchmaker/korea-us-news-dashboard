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


# ============================
# SETTINGS
# ============================
CONFIG_PATH = "config.yaml"
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/3.0)"
HEADERS = {"User-Agent": USER_AGENT}

DEFAULT_YEAR_FILTER = 2026  # 기본 2026년 (원하면 "전체"로 바꿔도 됨)

TOP5_MAX = 10               # TOP 섹션에서 최대 기업 수(요청: 최대 10개 기업 보이기)
OTHER_MAX = 20              # 기타(신규 투자/진출/사업현황) 표시 개수
CACHE_TTL_SEC = 60 * 20     # 20분 캐시


# ============================
# LOAD CONFIG
# ============================
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()

states_cfg = cfg.get("states", {})  # ex) GA: ["Georgia","조지아","GA"] 형태
priority_companies = cfg.get("priority_companies", ["현대", "SK", "LG", "한화", "고려아연"])

korean_queries = cfg.get("korean_queries", [])
us_sources = cfg.get("us_sources", [])  # (선택) 주정부/기관 페이지들
us_queries = cfg.get(
    "us_queries",
    [
        '(Georgia OR Tennessee OR Alabama OR Florida OR "South Carolina" OR GA OR TN OR AL OR FL OR SC) '
        '(Korean OR Korea OR "South Korean" OR "한국") '
        '(investment OR invest OR plant OR factory OR expansion OR contract OR subsidiary OR announce OR "economic development") '
        '(Hyundai OR SK OR LG OR Hanwha OR "Korean company" OR supplier)'
    ],
)


# ============================
# TEXT UTILS
# ============================
_ws = re.compile(r"\s+")
_html_tag = re.compile(r"<[^>]+>")


def norm_text(s: str) -> str:
    return _ws.sub(" ", (s or "").strip())


def strip_html(s: str) -> str:
    return norm_text(_html_tag.sub(" ", s or ""))


def norm_query_for_url(q: str) -> str:
    # 줄바꿈/다중공백 제거 후 URL 인코딩
    return quote(norm_text(q))


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


def make_id(provider: str, title: str, url: str) -> str:
    raw = f"{provider}||{norm_text(title)}||{norm_text(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================
# STATE DETECTION
# ============================
def detect_state(text: str) -> str:
    t = norm_text(text).lower()
    for code, names in (states_cfg or {}).items():
        # names can be list or dict(names=[...])
        if isinstance(names, dict):
            names_list = names.get("names", [])
        else:
            names_list = names
        for n in names_list:
            if norm_text(str(n)).lower() in t:
                return code
    return "Global"


# ============================
# COMPANY DETECTION (NO MANUAL 100 LIST)
# - TOP5는 확실히 캐치
# - 그 외는 한국 기업명 패턴으로 자동 추출
# ============================
# TOP5 표기 통일(타이틀에서 다양한 표기를 한 이름으로 묶기)
TOP5_ALIASES = {
    "현대": ["현대", "현대차", "Hyundai", "HYUNDAI", "기아", "Kia", "KIA"],
    "SK": ["SK", "SK온", "SK온", "SK hynix", "SK하이닉스", "하이닉스", "SK이노베이션", "SK Innovation"],
    "LG": ["LG", "LG에너지솔루션", "LG Energy Solution", "LG화학", "LG Chem"],
    "한화": ["한화", "Hanwha", "HANWHA", "한화큐셀", "Qcells", "Q CELLS"],
    "고려아연": ["고려아연", "Korea Zinc", "KoreaZinc", "KOREA ZINC"],
}

# 자동 추출 패턴(너무 공격적이면 노이즈 생길 수 있어서 “기업명같은 것” 위주로)
AUTO_PATTERNS = [
    r"([가-힣A-Za-z]{2,20}전자)",
    r"([가-힣A-Za-z]{2,20}중공업)",
    r"([가-힣A-Za-z]{2,20}산업)",
    r"([가-힣A-Za-z]{2,20}에너지)",
    r"([가-힣A-Za-z]{2,20}화학)",
    r"([가-힣A-Za-z]{2,20}건설)",
    r"([가-힣A-Za-z]{2,20}모빌리티)",
    r"([가-힣A-Za-z]{2,20}테크)",
    r"([가-힣A-Za-z]{2,20}EPC)",
    r"([가-힣A-Za-z]{2,20}오토)",
    r"([가-힣A-Za-z]{2,20}금속)",
    r"([가-힣A-Za-z]{2,20}소재)",
    r"([가-힣A-Za-z]{2,20}전기)",
]


def detect_company_auto(title: str) -> str:
    t = norm_text(title)

    # 1) TOP5 alias 우선
    for canon, aliases in TOP5_ALIASES.items():
        for a in aliases:
            if a and a in t:
                return canon

    # 2) 자동 패턴
    for p in AUTO_PATTERNS:
        m = re.search(p, t)
        if m:
            name = m.group(1)
            # 너무 흔한 단어/기관/지역이 잡히는 것 방지 (가벼운 안전장치)
            if len(name) >= 2 and name not in ["한국", "미국", "조지아", "테네시", "플로리다"]:
                return name

    return "기타 한국기업"


# ============================
# TAG / IMPORTANCE
# ============================
INVEST = ["투자", "공장", "설립", "증설", "진출", "확장", "신규", "라인", "캠퍼스"]
DEAL = ["수주", "계약", "공급", "체결", "MOU", "협약", "파트너십"]
CAPITAL = ["증자", "출자", "공시"]
SALES = ["판매", "기록", "돌파", "매출", "실적"]
GOV = ["정부", "범부처", "위원회", "MOU 이행", "전략투자"]


def classify_tag(text: str) -> str:
    if any(k in text for k in GOV):
        return "[정책/정부]"
    if any(k in text for k in INVEST):
        return "[신규 투자]"
    if any(k in text for k in DEAL):
        return "[수주/계약]"
    if any(k in text for k in CAPITAL):
        return "[자본/공시]"
    if any(k in text for k in SALES):
        return "[실적/판매]"
    return "[주요]"


def importance_score(title: str, provider: str, company: str) -> int:
    text = title
    score = 0
    if company in priority_companies:
        score += 100
    if any(k in text for k in GOV):
        score += 40
    if any(k in text for k in INVEST):
        score += 35
    if any(k in text for k in DEAL):
        score += 25
    if any(k in text for k in CAPITAL):
        score += 20
    if any(k in text for k in SALES):
        score += 15
    if provider == "KOREAN":
        score += 5
    return score


def icon_for_company(company: str) -> str:
    return "👑" if company in priority_companies else "💎"


# ============================
# FETCH: Google News RSS (KR)
# ============================
@st.cache_data(ttl=CACHE_TTL_SEC)
def fetch_google_news_kr(queries: list[str], provider_label: str):
    rows = []
    for q in queries:
        q_encoded = norm_query_for_url(q)
        url = f"https://news.google.com/rss/search?q={q_encoded}&hl=ko&gl=KR&ceid=KR:ko"

        feed = feedparser.parse(url)
        for e in feed.entries[:60]:
            title = norm_text(getattr(e, "title", ""))
            link = norm_text(getattr(e, "link", ""))
            published_raw = getattr(e, "published", "") or getattr(e, "updated", "")
            dt = safe_parse_date(published_raw)

            summary_html = getattr(e, "summary", None) or getattr(e, "description", None)
            summary = strip_html(summary_html or "")

            if not title or not link:
                continue

            company = detect_company_auto(title)
            state = detect_state(title)

            tag = classify_tag(title)
            core = summary if summary else title
            core = (core[:180] + "…") if len(core) > 180 else core

            rows.append(
                {
                    "provider": provider_label,
                    "source": "Google News (KR)",
                    "title": title,
                    "url": link,
                    "published_at": dt,
                    "state": state,
                    "company": company,
                    "tag": tag,
                    "core": core,
                    "score": importance_score(title, provider_label, company),
                }
            )

    # dedup
    dedup = {}
    for r in rows:
        dedup[make_id(r["provider"], r["title"], r["url"])] = r
    return list(dedup.values())


# ============================
# FETCH: US SOURCES (optional, HTML list)
# - 주정부/기관 사이트는 구조가 제각각이라 "대략적 링크 리스트" 추출
# - 제목만 가져오는 수준(요약/발행일은 사이트별 제각각)
# ============================
def guess_items_from_page(html: str, base_url: str, max_items: int = 50):
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

    # dedup + cap
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


@st.cache_data(ttl=CACHE_TTL_SEC)
def fetch_us_source_pages(sources: list[dict]):
    rows = []
    for src in sources:
        name = src.get("name", "US Source")
        url = src.get("url")
        if not url:
            continue
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            items = guess_items_from_page(r.text, url, max_items=40)
        except Exception:
            continue

        for title, link, date_text in items:
            dt = safe_parse_date(date_text) if date_text else None

            company = detect_company_auto(title)
            state = detect_state(title)

            tag = classify_tag(title)
            core = title
            core = (core[:180] + "…") if len(core) > 180 else core

            rows.append(
                {
                    "provider": "US_PAGE",
                    "source": name,
                    "title": title,
                    "url": link,
                    "published_at": dt,
                    "state": state,
                    "company": company,
                    "tag": tag,
                    "core": core,
                    "score": importance_score(title, "US", company),
                }
            )

    dedup = {}
    for r in rows:
        dedup[make_id(r["provider"], r["title"], r["url"])] = r
    return list(dedup.values())


# ============================
# BUILD DISPLAY TABLES
# ============================
def build_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"])

    df = pd.DataFrame(rows)

    # datetime normalize for sorting
    df["_when"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    now = pd.Timestamp.now(tz="UTC")
    df["_when"] = df["_when"].fillna(now - pd.Timedelta(days=3650))

    df["_score"] = df["score"].fillna(0).astype(int)

    df["뉴스 발행일"] = df["_when"].dt.strftime("%Y.%m.%d")
    df["주(State)"] = df["state"]
    df["기업명"] = df["company"].apply(lambda c: f"{icon_for_company(c)} {c}")
    df["핵심 내용"] = df.apply(lambda r: f"{r['tag']} {r['core']}", axis=1)

    # 링크는 LinkColumn으로 표시할 거라 URL 그대로 둠
    df["원문 확인"] = df["url"]

    # 최신/중요도 정렬
    df = df.sort_values(by=["_when", "_score"], ascending=[False, False])
    return df


def apply_year_filter(df: pd.DataFrame, year_filter):
    if df.empty:
        return df
    if year_filter == "전체":
        return df
    y = str(int(year_filter))
    return df[df["뉴스 발행일"].str.startswith(y)]


def pick_top_per_company(df: pd.DataFrame, top_companies: list[str]) -> pd.DataFrame:
    if df.empty:
        return df

    # 표시명(👑 현대) → 실제 비교는 원본 company로 해야 하므로 원본 열이 필요
    # 여기서는 "company" 원본이 df에 없으니, 표시명에서 제거
    def strip_icon(name: str) -> str:
        return norm_text(name.replace("👑", "").replace("💎", ""))

    df2 = df.copy()
    df2["_company_plain"] = df2["기업명"].apply(strip_icon)

    subset = df2[df2["_company_plain"].isin(top_companies)].copy()
    if subset.empty:
        return subset.drop(columns=["_company_plain"], errors="ignore")

    # 기업당 1개
    subset = subset.groupby("_company_plain", as_index=False).head(1)

    # 순서: top_companies 순서대로
    order_map = {c: i for i, c in enumerate(top_companies)}
    subset["_order"] = subset["_company_plain"].map(lambda x: order_map.get(x, 9999))

    subset = subset.sort_values(by=["_order"], ascending=True)
    subset = subset.head(TOP5_MAX)

    return subset.drop(columns=["_company_plain", "_order"], errors="ignore")


def pick_other_updates(df: pd.DataFrame, top_companies: list[str], n: int) -> pd.DataFrame:
    if df.empty:
        return df

    def strip_icon(name: str) -> str:
        return norm_text(name.replace("👑", "").replace("💎", ""))

    df2 = df.copy()
    df2["_company_plain"] = df2["기업명"].apply(strip_icon)

    other = df2[~df2["_company_plain"].isin(top_companies)].copy()
    if other.empty:
        return other.drop(columns=["_company_plain"], errors="ignore")

    # "기타 한국기업"도 최신 투자/진출 기사면 가치가 있으니 포함
    other = other.head(n)
    return other.drop(columns=["_company_plain"], errors="ignore")


# ============================
# UI
# ============================
st.set_page_config(page_title="미국 진출 한국기업 뉴스 상황판", layout="wide")
st.title("📰 미국 진출 한국기업 뉴스 상황판")
st.caption("TOP5(현대/SK/LG/한화/고려아연)는 기업당 1개, 그 외는 자동으로 기업명을 추출해 최신 업데이트를 보여줍니다.")

with st.sidebar:
    st.subheader("필터")
    year_filter = st.selectbox("발행 연도", [DEFAULT_YEAR_FILTER, 2025, 2024, "전체"], index=0)
    st.markdown("---")
    st.write("TOP5(고정 표시):")
    st.code(", ".join(priority_companies))
    if st.button("🔄 캐시 새로고침(강제 재수집)"):
        st.cache_data.clear()
        st.rerun()

tab1, tab2 = st.tabs(["🇰🇷 한국어 뉴스", "🇺🇸 미국(주정부/현지) 뉴스"])

# ---- Tab 1: Korean news (KR)
with tab1:
    st.subheader("⭐ TOP 기업 최신 (기업당 1개)")
    rows_kr = fetch_google_news_kr(korean_queries, provider_label="KOREAN")
    df_kr = apply_year_filter(build_df(rows_kr), year_filter)

    top_kr = pick_top_per_company(df_kr, priority_companies)
    if top_kr.empty:
        st.info("TOP 기업 뉴스를 찾지 못했습니다. korean_queries를 확장해보세요.")
    else:
        st.dataframe(
            top_kr[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
            column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
        )

    st.subheader("🆕 신규 투자·진출 및 미국 사업 현황 (자동 추출 기업)")
    other_kr = pick_other_updates(df_kr, priority_companies, OTHER_MAX)
    if other_kr.empty:
        st.info("추가 업데이트가 없습니다.")
    else:
        st.dataframe(
            other_kr[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
            column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
        )

# ---- Tab 2: US news (mix: Google News query + optional state pages)
with tab2:
    st.subheader("⭐ TOP 기업 최신 (기업당 1개)")
    rows_us_gn = fetch_google_news_kr(us_queries, provider_label="US_GNEWS")
    rows_us_pages = fetch_us_source_pages(us_sources) if us_sources else []
    rows_us_all = rows_us_gn + rows_us_pages

    df_us = apply_year_filter(build_df(rows_us_all), year_filter)

    top_us = pick_top_per_company(df_us, priority_companies)
    if top_us.empty:
        st.info("TOP 기업 미국 뉴스가 없습니다. us_queries / us_sources를 확장해보세요.")
    else:
        st.dataframe(
            top_us[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
            column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
        )

    st.subheader("🆕 신규 투자·진출 및 미국 사업 현황 (자동 추출 기업)")
    other_us = pick_other_updates(df_us, priority_companies, OTHER_MAX)
    if other_us.empty:
        st.info("추가 업데이트가 없습니다.")
    else:
        st.dataframe(
            other_us[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]],
            use_container_width=True,
            hide_index=True,
            column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
        )

st.markdown("---")
st.write("✅ 기업명은 TOP5는 고정, 나머지는 기사 제목에서 자동 추출합니다. (기업명 100개 입력할 필요 없음)")
