import streamlit as st
import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from datetime import datetime

# ------------------------
# 설정 불러오기
# ------------------------
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))

states = cfg["states"]
companies = cfg["companies"]

# ------------------------
# State & Company 추출 함수
# ------------------------
def detect_state(text):
    for code, names in states.items():
        for n in names:
            if n.lower() in text.lower():
                return code
    return "Global"

def detect_company(text):
    for c in companies:
        if c in text:
            return c
    return "Unknown"

# ------------------------
# 한국 뉴스 RSS 수집
# ------------------------
def fetch_korean_news():
    results = []
    for q in cfg["korean_queries"]:
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        for e in feed.entries[:10]:
            title = e.title
            link = e.link
            published = getattr(e, "published", "")

            results.append({
                "State": detect_state(title),
                "Company": detect_company(title),
                "Date": published[:16],
                "Core": title,
                "Source": "한국뉴스",
                "URL": link
            })

    return results

# ------------------------
# 미국 주정부 뉴스 수집
# ------------------------
def fetch_us_news():
    results = []
    for src in cfg["us_sources"]:
        name = src["name"]
        url = src["url"]

        try:
            html = requests.get(url, timeout=10).text
            soup = BeautifulSoup(html, "html.parser")

            links = soup.select("a")[:15]

            for a in links:
                title = a.get_text().strip()
                href = a.get("href")

                if not title or not href:
                    continue

                if href.startswith("/"):
                    href = url + href

                results.append({
                    "State": detect_state(title),
                    "Company": detect_company(title),
                    "Date": datetime.today().strftime("%Y-%m-%d"),
                    "Core": title,
                    "Source": name,
                    "URL": href
                })

        except:
            continue

    return results

# ------------------------
# Streamlit UI
# ------------------------
st.set_page_config(page_title="한국기업 미국 동남부 뉴스 상황판", layout="wide")

st.title("📰 미국 동남부 진출 한국기업 뉴스 상황판")
st.caption("GitHub 웹에서만 관리 가능 / Streamlit 자동 배포")

tab1, tab2 = st.tabs(["🇰🇷 한국어 뉴스", "🇺🇸 미국 주정부/기관 뉴스"])

with tab1:
    st.subheader("한국어 뉴스")
    data = fetch_korean_news()
    st.dataframe(data)

with tab2:
    st.subheader("미국 주정부/기관 뉴스")
    data = fetch_us_news()
    st.dataframe(data)
