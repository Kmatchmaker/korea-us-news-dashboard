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
USER_AGENT = "Mozilla/5.0 (StreamlitNewsBoard/USGovOnly/DetailParse/1.0)"
HEADERS = {"User-Agent": USER_AGENT}

CACHE_TTL_SEC = 60 * 20
DEFAULT_YEAR_FILTER = 2026

TOP_COMPANY_MAX = 10
OTHER_MAX = 30

# 유사 기사 제거(0.80~0.92 조절)
SIMILARITY_THRESHOLD = 0.86

# 상세페이지 요청 개수 제한(소스가 많아지면 속도/차단 이슈 방지)
MAX_DETAIL_PER_SOURCE = 40


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
_punct = re.compile(r"[^0-9A-Za-z가-힣 ]+")
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


def make_id(*parts: str) -> str:
    raw = "||".join(norm_text(p) for p in parts if p is not None)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


# ============================
# STATE DETECTION (주 약어 오탐 최소화)
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

    # 2) 약어는 단독 토큰만
    for abbr, rx in STATE_ABBR_RE.items():
        if rx.search(t):
            return abbr

    # 3) 도메인 힌트
    h = host_of(source_url)
    if "gov.georgia.gov" in h or "georgia.org" in h:
        return "GA"
    if "tnecd.com" in h:
        return "TN"
    if "madeinalabama.com" in h:
        return "AL"
    if "sccommerce.com" in h:
        return "SC"
    if "floridajobs.org" in h:
        return "FL"

    return "Global"


# ============================
# COMPANY DETECTION (기타 금지)
# - 제목 + 본문에서 회사명 후보 추출
# ============================
TOP5_ALIASES = {
    "현대": ["현대", "현대차", "Hyundai", "기아", "Kia"],
    "SK": ["SK", "SK온", "SK hynix", "SK하이닉스", "하이닉스", "SK Innovation", "SK이노베이션"],
    "LG": ["LG", "LG에너지솔루션", "LG Energy Solution", "LG화학", "LG Chem"],
    "한화": ["한화", "Hanwha", "한화큐셀", "Qcells", "Q CELLS"],
    "고려아연": ["고려아연", "Korea Zinc", "KoreaZinc"],
}

STOPWORDS = {
    # 직함/기관/일반
    "gov", "gov.", "governor", "office", "press", "release", "news", "department", "commerce",
    "economic", "development", "authority", "commission", "county", "city", "state",
    "georgia", "tennessee", "alabama", "florida", "carolina", "south", "north",
    "미국", "한국", "조지아", "테네시", "앨라배마", "알라배마", "플로리다", "캐롤라이나",
    "주정부", "정부", "위원회", "경제개발", "카운티",
    # 행동
