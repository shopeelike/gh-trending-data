#!/usr/bin/env python3
"""Scrape github.com/trending and write data/<date>.json + data/latest.json.

Runs inside GitHub Actions (github.com is reachable there).
Config: languages.txt (one GitHub language slug per line, '' or 'any' = all).
Periods: daily + weekly every day, monthly on the 1st (or PERIODS env).
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def load_languages():
    path = os.path.join(ROOT, "languages.txt")
    langs = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            s = line.strip().lower()
            if s and not s.startswith("#"):
                langs.append("" if s in ("any", "all", "*") else s)
    if "" not in langs:
        langs.insert(0, "")
    return langs


def periods_for(today):
    env = os.environ.get("PERIODS")
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    ps = ["daily", "weekly"]
    if today.day == 1:
        ps.append("monthly")
    return ps


def to_int(text):
    m = re.search(r"[\d,]+", text or "")
    return int(m.group(0).replace(",", "")) if m else 0


def fetch(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  {r.status_code} {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  error {e} {url}", file=sys.stderr)
        time.sleep(3 * (i + 1))
    return None


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("article.Box-row")
    out = []
    for art in rows:
        h2 = art.find("h2")
        a = h2.find("a") if h2 else None
        href = a.get("href", "") if a else ""
        full_name = href.strip("/")
        if not full_name or full_name.count("/") != 1:
            continue
        desc_el = art.find("p")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
        lang_el = art.select_one("[itemprop=programmingLanguage]")
        lang = lang_el.get_text(strip=True) if lang_el else ""
        stars = 0
        forks = 0
        for link in art.select("a[href]"):
            h = link.get("href", "")
            if h.endswith("/stargazers"):
                stars = to_int(link.get_text())
            elif h.endswith("/forks"):
                forks = to_int(link.get_text())
        gained = 0
        for span in art.find_all("span"):
            t = span.get_text(" ", strip=True)
            if re.search(r"stars? (today|this week|this month)", t):
                gained = to_int(t)
                break
        out.append({
            "full_name": full_name,
            "description": desc,
            "language": lang,
            "stars": stars,
            "forks": forks,
            "gained": gained,
        })
    return out


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    langs = load_languages()
    periods = periods_for(today)
    seen = {}
    lists = []
    for period in periods:
        for lang in langs:
            path = "/trending" + (f"/{urllib.parse.quote(lang)}" if lang else "")
            url = f"https://github.com{path}?since={period}"
            print(f"fetch {url}")
            html = fetch(url)
            if html is None:
                lists.append({"period": period, "language": lang or "any", "count": 0, "error": True})
                continue
            items = parse(html)
            lists.append({"period": period, "language": lang or "any", "count": len(items)})
            for rank, it in enumerate(items, 1):
                rec = seen.setdefault(it["full_name"], {**it, "appearances": []})
                # keep the largest star count we saw
                rec["stars"] = max(rec["stars"], it["stars"])
                if not rec["description"] and it["description"]:
                    rec["description"] = it["description"]
                if not rec["language"] and it["language"]:
                    rec["language"] = it["language"]
                rec["appearances"].append({
                    "period": period,
                    "language": lang or "any",
                    "rank": rank,
                    "gained": it["gained"],
                })
            time.sleep(1.5)

    ok_lists = [l for l in lists if not l.get("error")]
    if not ok_lists or all(l["count"] == 0 for l in ok_lists):
        print("No data parsed; GitHub markup may have changed.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "date": today.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "periods": periods,
        "languages": [l or "any" for l in langs],
        "lists": lists,
        "repos": sorted(seen.values(), key=lambda r: -max(a["gained"] for a in r["appearances"])),
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    for name in (f"{today.isoformat()}.json", "latest.json"):
        with open(os.path.join(ROOT, "data", name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(seen)} repos from {len(ok_lists)} lists")


if __name__ == "__main__":
    main()
