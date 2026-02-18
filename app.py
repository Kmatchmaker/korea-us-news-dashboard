import re
import hashlib
from urllib.parse import urljoin, urlparse
from datetime import timezone

import pandas as pd
import requests
import streamlit as st
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# ============================
# SETTINGS
# ============================
CONFIG_PATH = "config.yaml"
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/USGovOnly/4.0)"
HEADERS = {"User-Agent": USER_AGENT}

CACHE_TTL_SEC = 60 * 20
DEFAULT_YEAR_FILTER = 2026

TOP_COMPANY_MAX = 10   # TOP 기업 섹션에서 최대 10개 기업
OTHER_MAX = 30         # 기타(신규 투자/진출/확장) 표시 개수

SIMILARITY_THRESHOLD = 0.86  # 유사 기사 제거 강도(0.80~0.92)


# ============================
# LOAD CONFIG
# ============================
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()
states_cfg = cfg.get("states", {})
priority_companies = cfg.get("priority_companies", ["현대", "SK", "LG", "한화", "고려아연"])
us_sources = cfg.get("us_sources", [])


# ============================
# TEXT UTILS
# ============================
_ws = re.compile(r"\s+")
_punct = re.compile(r"[^0-9A-Za-z가-힣 .:/_-]+")
_digits = re.compile(r"\b\d+(?:[.,]\d+)*\b")


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


def make_id(provider: str, title: str, url: str) -> str:
    raw = f"{provider}||{norm_text(title)}||{norm_text(url)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ============================
# STATE DETECTION (오탐 최소화)
# ============================
STATE_ABBR = ["GA", "TN", "AL", "SC", "FL"]
STATE_ABBR_RE = {abbr: re.compile(rf"(?<![A-Z0-9]){abbr}(?![A-Z0-9])") for abbr in STATE_ABBR}


def detect_state_strict(text: str, source_url: str) -> str:
    t = norm_text(text)
    tl = t.lower()

    # 1) 긴 이름 먼저
    for code, names in (states_cfg or {}).items():
        for n in names:
            nn = norm_text(str(n))
            if nn.upper() in STATE_ABBR:
                continue
            if nn and nn.lower() in tl:
                return code

    # 2) 약어는 단독 토큰일 때만
    for abbr, rx in STATE_ABBR_RE.items():
        if rx.search(t):
            return abbr

    # 3) 도메인 힌트(소스 기준)
    host = (urlparse(source_url).netloc or "").lower()
    if "gov.georgia.gov" in host or "georgia.org" in host:
        return "GA"
    if "tnecd.com" in host:
        return "TN"
    if "madeinalabama.com" in host:
        return "AL"
    if "sccommerce.com" in host:
        return "SC"
    if "floridajobs.org" in host:
        return "FL"

    return "Global"


# ============================
# COMPANY DETECTION (절대 '기타' 금지)
# ============================
TOP5_ALIASES = {
    "현대": ["현대", "현대차", "Hyundai", "기아", "Kia"],
    "SK": ["SK", "SK온", "SK hynix", "SK하이닉스", "하이닉스", "SK Innovation", "SK이노베이션"],
    "LG": ["LG", "LG에너지솔루션", "LG Energy Solution", "LG화학", "LG Chem"],
    "한화": ["한화", "Hanwha", "한화큐셀", "Qcells", "Q CELLS"],
    "고려아연": ["고려아연", "Korea Zinc", "KoreaZinc"],
}

COMPANY_SUFFIX_HINT = re.compile(
    r"(중공업|금속|오토|EPC|전자|에너지|화학|건설|모빌리티|테크|산업|소재|전기|바이오)$"
)

STOPWORDS = {
    # 인물/직함/기관 성격
    "gov", "gov.", "governor", "office", "official", "statement", "commissioner", "deputy",
    "press", "release", "news", "department", "commerce", "economic", "development", "authority",
    # 지역/일반
    "us", "u.s", "usa", "america", "american",
    "georgia", "tennessee", "alabama", "florida", "carolina", "south", "north",
    "미국", "한국", "조지아", "테네시", "앨라배마", "알라배마", "플로리다", "사우스캐롤라이나", "캐롤라이나",
    "주정부", "정부", "위원회", "경제개발", "카운티", "county", "city", "state",
    # 행동 단어
    "invest", "investment", "invests", "announce", "announces", "announced",
    "expansion", "expand", "contract", "agreement", "facility", "plant", "factory",
    "투자", "공장", "설립", "증설", "확장", "진출", "계약", "수주", "공급", "체결", "협약",
}


def detect_company_from_title(title: str) -> str:
    t = norm_text(title)

    # 1) TOP5 alias 우선
    for canon, aliases in TOP5_ALIASES.items():
        for a in aliases:
            if a and a in t:
                return canon

    # 2) 제목 맨 앞 토큰
    m = re.match(r"^([가-힣A-Za-z0-9&/.\-]{2,40})", t)
    if m:
        cand = m.group(1).strip(".,:-–—")
        cl = cand.lower()
        if cand and (cl not in STOPWORDS) and cl not in {"the", "a", "an"}:
            return cand

    # 3) 토큰 후보들
    tokens = re.findall(r"[가-힣A-Za-z0-9&/.\-]{2,40}", t)
    cleaned = []
    for tok in tokens:
        tok2 = tok.strip(".,:-–—()[]{}\"'")
        if not tok2:
            continue
        if tok2.lower() in STOPWORDS:
            continue
        cleaned.append(tok2)

    # 3-1) 접미 힌트 우선
    for tok in cleaned:
        if COMPANY_SUFFIX_HINT.search(tok):
            return tok

    # 3-2) 영문 회사 접미 우선
    for tok in cleaned:
        if re.search(r"(Inc\.?|LLC|L\.L\.C\.|Corp\.?|Corporation|Co\.?|Company)$", tok, re.IGNORECASE):
            return tok

    # 3-3) 남은 것 중 첫 후보
    if cleaned:
        return cleaned[0]

    return "미확인"


def icon_for_company(company_plain: str) -> str:
    return "👑" if company_plain in priority_companies else "💎"


# ============================
# TAG / IMPORTANCE
# ============================
INVEST_EN = ["invest", "investment", "plant", "facility", "expansion", "factory", "site", "build", "construct", "manufacturing"]
INVEST_KO = ["투자", "공장", "설립", "증설", "확장", "진출", "신규"]
DEAL_EN = ["contract", "deal", "supply", "agreement", "award", "wins", "signed"]
DEAL_KO = ["수주", "계약", "공급", "체결", "협약", "파트너십"]
CAPITAL_KO = ["증자", "출자", "공시"]
SALES_KO = ["판매", "기록", "돌파", "매출", "실적"]


def classify_tag(text: str) -> str:
    tl = text.lower()
    if any(k in text for k in INVEST_KO) or any(k in tl for k in INVEST_EN):
        return "[신규 투자]"
    if any(k in text for k in DEAL_KO) or any(k in tl for k in DEAL_EN):
        return "[수주/계약]"
    if any(k in text for k in CAPITAL_KO):
        return "[자본/공시]"
    if any(k in text for k in SALES_KO):
        return "[실적/판매]"
    return "[주요]"


def importance_score(title: str, company_plain: str) -> int:
    score = 0
    if company_plain in priority_companies:
        score += 100
    tag = classify_tag(title)
    if tag == "[신규 투자]":
        score += 35
    elif tag == "[수주/계약]":
        score += 25
    elif tag == "[자본/공시]":
        score += 20
    elif tag == "[실적/판매]":
        score += 15
    else:
        score += 5
    return score


# ============================
# DEDUP: 유사 기사 제거 (Jaccard)
# ============================
def title_signature(title: str, company_plain: str) -> set:
    s = norm_text(title)
    if company_plain:
        s = s.replace(company_plain, " ")
    s = _digits.sub(" ", s)
    s = _punct.sub(" ", s)
    s = norm_text(s).lower()
    toks = [t for t in s.split() if t and t not in STOPWORDS and len(t) >= 2]
    return set(toks)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def dedup_similar(rows: list[dict]) -> list[dict]:
    kept = []
    kept_sigs = []
    rows_sorted = sorted(rows, key=lambda r: (r["_when_sort"], r["_score"]), reverse=True)

    for r in rows_sorted:
        sig = r["_sig"]
        dup = False
        for ks in kept_sigs:
            if jaccard(sig, ks) >= SIMILARITY_THRESHOLD:
                dup = True
                break
        if not dup:
            kept.append(r)
            kept_sigs.append(sig)

    return kept


# ============================
# SOURCE-SPECIFIC PARSERS
# ============================
MONTH_RX_LONG = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}")
MONTH_RX_SHORT = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}")
DOT_DATE_RX = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")  # TNECD 메인 "Recent News"에 자주 등장


def _is_probably_article_url(u: str) -> bool:
    # pdf 등 제외
    path = (urlparse(u).path or "").lower()
    if path.endswith(".pdf"):
        return False
    if "/docs/" in path or "/default-source/" in path:
        return False
    return True


def guess_items_from_page(html: str, base_url: str, max_items: int = 200):
    """
    소스별로 '뉴스/보도자료 상세 링크' 패턴을 우선 적용해 잡링크를 최소화한다.
    반환: (title, url, date_text_or_none)
    """
    soup = BeautifulSoup(html, "html.parser")
    host = (urlparse(base_url).netloc or "").lower()
    items = []

    # -----------------------------
    # (GA) gov.georgia.gov : /press-releases/YYYY-MM-DD/slug
    # -----------------------------
    if "gov.georgia.gov" in host:
        for a in soup.select('a[href*="/press-releases/"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not re.search(r"/press-releases/\d{4}-\d{2}-\d{2}/", full_url):
                continue
            if not _is_probably_article_url(full_url):
                continue

            text = norm_text(a.get_text(" "))
            if not text or len(text) < 12:
                continue

            m = MONTH_RX_LONG.search(text)
            date_text = m.group(0) if m else None
            title = text.replace(date_text, "").strip() if date_text else text

            items.append((title, full_url, date_text))

    # -----------------------------
    # (GA) georgia.org : /press-releases/... (외부 링크가 섞일 수 있음)
    # -----------------------------
    elif "georgia.org" in host:
        main = soup.select_one("main") or soup
        for a in main.select('a[href*="/press-releases"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not _is_probably_article_url(full_url):
                continue

            text = norm_text(a.get_text(" "))
            if not text or text.lower() in {"read more", "learn more"}:
                parent = a.find_parent()
                text = norm_text(parent.get_text(" ")) if parent else text

            m = MONTH_RX_SHORT.search(text)
            date_text = m.group(0) if m else None
            title = text.replace(date_text, "").replace("Read More", "").strip() if date_text else text

            if len(title) < 12:
                continue

            items.append((title, full_url, date_text))

    # -----------------------------
    # (TN) tnecd.com : /news/slug (메인에도 Recent News가 있음)
    # -----------------------------
    elif "tnecd.com" in host:
        main = soup.select_one("main") or soup

        # 1) /news/ 링크 우선
        for a in main.select('a[href*="/news/"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not _is_probably_article_url(full_url):
                continue

            text = norm_text(a.get_text(" "))
            if len(text) < 10:
                continue

            # 같은 블록(부모)에서 02.04.2026 같은 날짜를 찾아보기
            parent = a.find_parent()
            ptxt = norm_text(parent.get_text(" ")) if parent else ""
            m = DOT_DATE_RX.search(ptxt)
            date_text = m.group(0) if m else None

            # DOT 날짜는 parse가 애매하니 fetch 단계에서 그대로 넘김(나중 safe_parse_date로 처리)
            items.append((text, full_url, date_text))

        # 2) 그래도 부족하면 /wp-content/ 같은 건 제외하고 의미있는 링크 추가
        if not items:
            for a in main.select("a[href]"):
                href = a.get("href", "")
                full_url = urljoin(base_url, href)
                if "/news/" not in full_url:
                    continue
                if not _is_probably_article_url(full_url):
                    continue
                text = norm_text(a.get_text(" "))
                if len(text) >= 10:
                    items.append((text, full_url, None))

    # -----------------------------
    # (AL) madeinalabama.com : /news/slug 형태가 많음
    # -----------------------------
    elif "madeinalabama.com" in host:
        main = soup.select_one("main") or soup
        for a in main.select('a[href*="/news/"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not _is_probably_article_url(full_url):
                continue

            text = norm_text(a.get_text(" "))
            if len(text) < 10:
                continue

            # 부모에서 날짜 시도
            parent = a.find_parent()
            ptxt = norm_text(parent.get_text(" ")) if parent else ""
            m = MONTH_RX_LONG.search(ptxt) or MONTH_RX_SHORT.search(ptxt)
            date_text = m.group(0) if m else None

            items.append((text, full_url, date_text))

    # -----------------------------
    # (SC) sccommerce.com/news : /news/... 또는 /news-... 형태가 섞일 수 있어 넓게 잡되 main 위주
    # -----------------------------
    elif "sccommerce.com" in host:
        main = soup.select_one("main") or soup
        for a in main.select("a[href]"):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            path = (urlparse(full_url).path or "").lower()
            if "/news" not in path:
                continue
            if not _is_probably_article_url(full_url):
                continue

            text = norm_text(a.get_text(" "))
            if len(text) < 10:
                continue

            parent = a.find_parent()
            ptxt = norm_text(parent.get_text(" ")) if parent else ""
            m = MONTH_RX_LONG.search(ptxt) or MONTH_RX_SHORT.search(ptxt)
            date_text = m.group(0) if m else None

            items.append((text, full_url, date_text))

    # -----------------------------
    # (FL) floridajobs.org (DEO Press)
    # - docs/default-source pdf 링크가 많아서 강하게 제외
    # - news-center 아래 HTML 링크만 우선
    # -----------------------------
    elif "floridajobs.org" in host:
        main = soup.select_one("main") or soup
        for a in main.select("a[href]"):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not _is_probably_article_url(full_url):
                continue

            path = (urlparse(full_url).path or "").lower()
            # news-center 내부만 우선
            if "/news-center" not in path:
                continue

            text = norm_text(a.get_text(" "))
            if len(text) < 10:
                continue

            parent = a.find_parent()
            ptxt = norm_text(parent.get_text(" ")) if parent else ""
            m = MONTH_RX_LONG.search(ptxt) or MONTH_RX_SHORT.search(ptxt)
            date_text = m.group(0) if m else None

            items.append((text, full_url, date_text))

    # -----------------------------
    # Fallback (그 외 소스가 추가되더라도 최소한 동작)
    # -----------------------------
    else:
        # article 우선
        for art in soup.select("article"):
            a = art.select_one("a[href]")
            if not a:
                continue
            title = norm_text(a.get_text(" "))
            href = norm_text(a.get("href", ""))
            if not title or not href:
                continue
            full_url = urljoin(base_url, href)
            if not _is_probably_article_url(full_url):
                continue

            date_text = None
            time_tag = art.select_one("time")
            if time_tag:
                date_text = norm_text(time_tag.get("datetime") or time_tag.get_text(" "))

            if len(title) >= 10:
                items.append((title, full_url, date_text))

        if not items:
            main = soup.select_one("main") or soup
            for a in main.select("a[href]"):
                title = norm_text(a.get_text(" "))
                href = norm_text(a.get("href", ""))
                if not title or not href:
                    continue
                if len(title) < 10:
                    continue
                full_url = urljoin(base_url, href)
                if not _is_probably_article_url(full_url):
                    continue
                items.append((title, full_url, None))

    # Dedup + cap
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


# ============================
# FETCH: US GOV SOURCES ONLY
# ============================
@st.cache_data(ttl=CACHE_TTL_SEC)
def fetch_us_gov_only(sources: list[dict]):
    raw_rows = []

    for src in sources:
        name = src.get("name", "US Government Source")
        url = src.get("url")
        allow_external = bool(src.get("allow_external", False))
        if not url:
            continue

        src_host = (urlparse(url).netloc or "").lower()

        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            items = guess_items_from_page(r.text, url, max_items=200)
        except Exception:
            continue

        for title, link, date_text in items:
            link_host = (urlparse(link).netloc or "").lower()

            # 외부 도메인 링크 제거(기본)
            if (not allow_external) and src_host and link_host and (src_host not in link_host):
                continue

            # 날짜 파싱 (TNECD "02.04.2026" 케이스 보정)
            dt = None
            if date_text:
                # 02.04.2026 형식이면 month.day.year로 가정(사이트 표시가 mm.dd.yyyy)
                if DOT_DATE_RX.fullmatch(date_text):
                    try:
                        mm, dd, yyyy = date_text.split(".")
                        dt = safe_parse_date(f"{yyyy}-{mm}-{dd}")
                    except Exception:
                        dt = None
                else:
                    dt = safe_parse_date(date_text)

            company_plain = detect_company_from_title(title)
            company_display = f"{icon_for_company(company_plain)} {company_plain}"

            state = detect_state_strict(title, url)
            tag = classify_tag(title)
            score = importance_score(title, company_plain)

            when_sort = pd.to_datetime(dt, errors="coerce", utc=True)
            if pd.isna(when_sort):
                # 날짜 없으면 오래된 것으로(최신 정렬에서 밀림)
                when_sort = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3650)

            sig = title_signature(title, company_plain)

            raw_rows.append(
                {
                    "source": name,
                    "주(State)": state,
                    "기업명": company_display,
                    "뉴스 발행일": when_sort.strftime("%Y.%m.%d"),
                    "핵심 내용": f"{tag} {title}",
                    "원문 확인": link,
                    "_score": score,
                    "_when_sort": when_sort,
                    "_sig": sig,
                }
            )

    # URL/제목 기반 dedup
    dedup = {}
    for r in raw_rows:
        key = make_id("US_GOV", r["핵심 내용"], r["원문 확인"])
        dedup[key] = r
    rows = list(dedup.values())

    # 유사 기사 제거
    rows = dedup_similar(rows)

    # 최종 정렬
    rows.sort(key=lambda r: (r["_when_sort"], r["_score"]), reverse=True)
    return rows


# ============================
# DISPLAY HELPERS
# ============================
def apply_year_filter_df(df: pd.DataFrame, year_filter):
    if df.empty:
        return df
    if year_filter == "전체":
        return df
    y = str(int(year_filter))
    return df[df["뉴스 발행일"].str.startswith(y)]


def plain_company(display_name: str) -> str:
    return norm_text(display_name.replace("👑", "").replace("💎", ""))


def pick_top_company_one_each(df: pd.DataFrame, top_companies: list[str]) -> pd.DataFrame:
    if df.empty:
        return df

    d = df.copy()
    d["_plain"] = d["기업명"].apply(plain_company)

    top = d[d["_plain"].isin(top_companies)].copy()
    if top.empty:
        return top.drop(columns=["_plain"], errors="ignore")

    top = top.groupby("_plain", as_index=False).head(1)

    order = {c: i for i, c in enumerate(top_companies)}
    top["_order"] = top["_plain"].map(lambda x: order.get(x, 9999))
    top = top.sort_values("_order").head(TOP_COMPANY_MAX)

    return top.drop(columns=["_plain", "_order"], errors="ignore")


def pick_other(df: pd.DataFrame, top_companies: list[str], n: int) -> pd.DataFrame:
    if df.empty:
        return df
    d = df.copy()
    d["_plain"] = d["기업명"].apply(plain_company)
    other = d[~d["_plain"].isin(top_companies)].copy().head(n)
    return other.drop(columns=["_plain"], errors="ignore")


# ============================
# UI
# ============================
st.set_page_config(page_title="US 주정부 뉴스 전용 상황판", layout="wide")
st.title("🇺🇸 US 주정부/주(州) 산하기관 뉴스 전용: 한국기업 진출·확장 상황판")
st.caption(
    "US 탭은 config.yaml의 us_sources만 사용합니다. "
    "소스별 링크 패턴을 적용해 잡링크를 줄였고, 기업명은 '기타' 없이 제목에서 추출합니다. "
    "유사 기사는 자동 제거됩니다."
)

with st.sidebar:
    st.subheader("필터")
    year_filter = st.selectbox("발행 연도", [DEFAULT_YEAR_FILTER, 2025, 2024, "전체"], index=0)
    st.markdown("---")
    st.write("TOP5(👑):")
    st.code(", ".join(priority_companies))
    if st.button("🔄 캐시 새로고침"):
        st.cache_data.clear()
        st.rerun()

if not us_sources:
    st.warning("config.yaml의 us_sources가 비어 있습니다. 주정부/기관 뉴스 URL을 넣어주세요.")
else:
    rows = fetch_us_gov_only(us_sources)
    df = pd.DataFrame(rows)
    df = apply_year_filter_df(df, year_filter)

    st.subheader("⭐ TOP 기업 최신(기업당 1개, 최대 10개 기업)")
    top = pick_top_company_one_each(df, priority_companies)
    if top.empty:
        st.info("TOP 기업 관련 주정부/기관 뉴스가 아직 없거나 기업명 추출/alias 확장이 필요합니다.")
    st.dataframe(
        top[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]] if not top.empty else top,
        use_container_width=True,
        hide_index=True,
        column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
    )

    st.subheader("🆕 신규 투자/진출/확장 (최신, 유사 기사 제거됨)")
    other = pick_other(df, priority_companies, OTHER_MAX)
    st.dataframe(
        other[["주(State)", "기업명", "뉴스 발행일", "핵심 내용", "원문 확인"]] if not other.empty else other,
        use_container_width=True,
        hide_index=True,
        column_config={"원문 확인": st.column_config.LinkColumn("원문 확인")},
    )
