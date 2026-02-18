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
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/USGovOnly/1.0)"
HEADERS = {"User-Agent": USER_AGENT}

CACHE_TTL_SEC = 60 * 20
DEFAULT_YEAR_FILTER = 2026

TOP_COMPANY_MAX = 10        # 기업당 1개 최신/중요, 최대 10개 기업만
OTHER_MAX = 20              # 나머지 업데이트 목록


# ============================
# LOAD CONFIG
# ============================
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


cfg = load_config()
states_cfg = cfg.get("states", {})  # GA/TN/AL/SC/FL 등
priority_companies = cfg.get("priority_companies", ["현대", "SK", "LG", "한화", "고려아연"])
korean_queries = cfg.get("korean_queries", [])
us_sources = cfg.get("us_sources", [])


# ============================
# TEXT UTILS
# ============================
_ws = re.compile(r"\s+")
_html_tag = re.compile(r"<[^>]+>")


def norm_text(s: str) -> str:
    return _ws.sub(" ", (s or "").strip())


def strip_html(s: str) -> str:
    return norm_text(_html_tag.sub(" ", s or ""))


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
# STATE DETECTION (오탐 줄이기)
# - "SC" 같은 약어는 단어 경계로만 인식
# - "Georgia"는 주정부 사이트에서만 나오게 할 거라 크게 문제 감소
# ============================
STATE_ABBR = ["GA", "TN", "AL", "SC", "FL"]
STATE_ABBR_RE = {abbr: re.compile(rf"(?<![A-Z0-9]){abbr}(?![A-Z0-9])") for abbr in STATE_ABBR}


def detect_state_strict(text: str, source_url: str) -> str:
    t = norm_text(text)

    # 1) 긴 이름 먼저 (South Carolina / Tennessee 등)
    tl = t.lower()
    for code, names in (states_cfg or {}).items():
        # config.yaml에서 names가 list라고 가정(이전 대화 기준)
        for n in names:
            n_norm = norm_text(str(n))
            # 약어는 별도 처리
            if n_norm.upper() in STATE_ABBR:
                continue
            if n_norm and n_norm.lower() in tl:
                return code

    # 2) 약어는 "단독 토큰"만
    for abbr, rx in STATE_ABBR_RE.items():
        if rx.search(t):
            return abbr

    # 3) 도메인 힌트(가능하면)
    host = (urlparse(source_url).netloc or "").lower()
    if "georgia" in host:
        return "GA"
    if "tnecd" in host or "tennessee" in host:
        return "TN"
    if "alabama" in host:
        return "AL"
    if "sccommerce" in host or "southcarolina" in host:
        return "SC"
    if "florida" in host:
        return "FL"

    return "Global"


# ============================
# COMPANY DETECTION
# - TOP5는 alias로 묶고
# - 그 외는 "기사에 나온 회사명"을 제목에서 뽑아 표시
# ============================
TOP5_ALIASES = {
    "현대": ["현대", "현대차", "Hyundai", "기아", "Kia"],
    "SK": ["SK", "SK온", "SK hynix", "SK하이닉스", "하이닉스", "SK Innovation", "SK이노베이션"],
    "LG": ["LG", "LG에너지솔루션", "LG Energy Solution", "LG화학", "LG Chem"],
    "한화": ["한화", "Hanwha", "한화큐셀", "Qcells", "Q CELLS"],
    "고려아연": ["고려아연", "Korea Zinc", "KoreaZinc"],
}

STOPWORDS = {
    "미국", "한국", "조지아", "테네시", "앨라배마", "알라배마", "플로리다", "사우스캐롤라이나", "캐롤라이나",
    "투자", "공장", "설립", "증설", "확장", "진출", "계약", "수주", "공급", "체결", "발표", "확정", "최대",
    "주정부", "정부", "위원회", "뉴스", "보도자료", "경제개발", "카운티", "시", "주", "시장", "프로젝트",
    "press", "release", "news", "governor", "department", "commerce", "economic", "development",
    "georgia", "tennessee", "alabama", "florida", "carolina",
}


def detect_company_from_title(title: str) -> str:
    t = norm_text(title)

    # 1) TOP5 alias 우선
    for canon, aliases in TOP5_ALIASES.items():
        for a in aliases:
            if a and a in t:
                return canon

    # 2) 제목 맨 앞 토큰(“OOO, …” / “OOO - …” / “OOO: …”)
    m = re.match(r"^([가-힣A-Za-z0-9&/]+)", t)
    if m:
        cand = m.group(1)
        if len(cand) >= 2 and cand.lower() not in STOPWORDS:
            return cand

    # 3) 제목에서 회사명 후보 토큰 찾기(한글/영문/숫자 혼합 2~20자)
    # 너무 일반적인 단어는 STOPWORDS로 걸러냄
    tokens = re.findall(r"[가-힣A-Za-z0-9&/]{2,20}", t)
    for tok in tokens:
        if tok.lower() in STOPWORDS:
            continue
        # 회사명처럼 보이도록 “형태” 힌트(중공업/금속/오토/EPC 등) 있으면 우선
        if re.search(r"(중공업|금속|오토|EPC|전자|에너지|화학|건설|모빌리티|테크|산업|소재)", tok):
            return tok
    # 형태 힌트가 없어도 첫 후보를 반환(너가 원한 “기사에 나온 기업명” 최대 반영)
    for tok in tokens:
        if tok.lower() in STOPWORDS:
            continue
        return tok

    return "미확인기업"


def icon_for_company(company: str) -> str:
    return "👑" if company in priority_companies else "💎"


# ============================
# TAG / IMPORTANCE
# ============================
INVEST = ["invest", "investment", "plant", "facility", "expansion", "factory", "site", "build", "built", "construct"]
INVEST_KO = ["투자", "공장", "설립", "증설", "확장", "진출", "신규"]
DEAL = ["contract", "deal", "supply", "agreement", "award", "wins", "signed", "signs"]
DEAL_KO = ["수주", "계약", "공급", "체결", "협약", "파트너십"]
CAPITAL_KO = ["증자", "출자", "공시"]
SALES_KO = ["판매", "기록", "돌파", "매출", "실적"]


def classify_tag(text: str) -> str:
    tl = text.lower()
    if any(k in text for k in INVEST_KO) or any(k in tl for k in INVEST):
        return "[신규 투자]"
    if any(k in text for k in DEAL_KO) or any(k in tl for k in DEAL):
        return "[수주/계약]"
    if any(k in text for k in CAPITAL_KO):
        return "[자본/공시]"
    if any(k in text for k in SALES_KO):
        return "[실적/판매]"
    return "[주요]"


def importance_score(title: str, company: str) -> int:
    score = 0
    if company in priority_companies:
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
# FETCH: US GOV SOURCES ONLY (HTML list)
# ============================
def guess_items_from_page(html: str, base_url: str, max_items: int = 80):
    soup = BeautifulSoup(html, "html.parser")
    host = (urlparse(base_url).netloc or "").lower()

    items = []

    # -----------------------------
    # (A) GA Governor Press: /press-releases/YYYY-MM-DD/slug 패턴만 정확히 추출
    # -----------------------------
    if "gov.georgia.gov" in host:
        for a in soup.select('a[href*="/press-releases/"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)
            if not re.search(r"/press-releases/\d{4}-\d{2}-\d{2}/", full_url):
                continue

            text = norm_text(a.get_text(" "))
            if not text:
                continue

            # 예: "Gov. Kemp: ... February 04, 2026" 같은 한 줄에서 제목/날짜 분리
            m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}", text)
            date_text = m.group(0) if m else None
            title = text.replace(date_text, "").strip() if date_text else text

            items.append((title, full_url, date_text))

    # -----------------------------
    # (B) Georgia.org Press Releases: "Read More" 링크 or 제목 블록에서 press-releases 링크 추출
    # -----------------------------
    elif "georgia.org" in host:
        # /press-releases 내부 링크만
        for a in soup.select('a[href*="/press-releases"]'):
            href = a.get("href", "")
            full_url = urljoin(base_url, href)

            # georgia.org는 목록 항목이 "### Feb 4, 2026 ... Read More" 형태라
            # 날짜가 텍스트에 들어가 있거나, 같은 카드 안에 있음
            text = norm_text(a.get_text(" "))
            if not text:
                continue

            # "Read More"만 잡히는 경우가 있으니, 주변(부모)에서 제목을 끌어올림
            if text.lower() in {"read more", "read more\u00a0"}:
                parent_text = norm_text(a.find_parent().get_text(" ")) if a.find_parent() else ""
                text = parent_text if parent_text else text

            m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}", text)
            date_text = m.group(0) if m else None
            title = text.replace(date_text, "").replace("Read More", "").strip()

            # 너무 짧은 잡음 제외
            if len(title) < 12:
                continue

            items.append((title, full_url, date_text))

    # -----------------------------
    # (C) Generic fallback: article->a 우선, 없으면 의미있는 a만
    # -----------------------------
    else:
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
            # "메인 콘텐츠" 위주로만 훑기 (메뉴/푸터 잡음 줄임)
            main = soup.select_one("main") or soup
            for a in main.select("a[href]"):
                title = norm_text(a.get_text(" "))
                href = norm_text(a.get("href", ""))
                if not title or not href:
                    continue
                if len(title) < 12:
                    continue
                full_url = urljoin(base_url, href)
                items.append((title, full_url, None))

    # -----------------------------
    # Dedup + cap
    # -----------------------------
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
def fetch_us_gov_only(sources: list[dict]):
    rows = []

    for src in sources:
        name = src.get("name", "US Government Source")
        url = src.get("url")
        if not url:
            continue

        # (선택) 도메인 화이트리스트: source_url과 다른 도메인으로 튀는 링크는 제외
        src_host = (urlparse(url).netloc or "").lower()

        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            items = guess_items_from_page(r.text, url, max_items=60)
        except Exception:
            continue

        for title, link, date_text in items:
            link_host = (urlparse(link).netloc or "").lower()

            # 다른 도메인으로 튀는 링크(광고/외부뉴스) 제거 (오탐 줄이기)
            if src_host and link_host and (src_host not in link_host):
                # 단, georgia.org 같은 경우 press
