#!/usr/bin/env python3
"""Trending Radar task helper. Runs inside the daily scheduled Claude session.

Usage:
  python3 run.py plan  --db DBDIR --out OUTDIR [--date YYYY-MM-DD]
      DBDIR holds the artifact database dump: DBDIR/meta/*.json and DBDIR/repos/*.json
      Fetches data/latest.json from the data repo, merges with the db, applies rules,
      picks research candidates and downloads their READMEs. Writes into OUTDIR:
        writes/NNN.json   batches (<=50 entries) for ArtifactData "batch"
        candidates.json   repos to research: [{key, full_name, description, language, stars, readme}]
        run.json          counters for meta/runs
  python3 run.py apply --out OUTDIR --research RESEARCH.json
      RESEARCH.json = [{"key":..., "summary":..., "score":1-5}] (from the model)
      Appends one more batch file with summary/score writes + the meta/runs update.
"""
import argparse, datetime as dt, glob, json, os, re, subprocess, sys, urllib.request

def load_dir(d):
    out = {}
    for p in glob.glob(os.path.join(d, "*.json")):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        out[os.path.splitext(os.path.basename(p))[0]] = doc.get("data", doc)  # tolerate {data:..} wrappers
    return out

def key_of(name): return name.replace("/", "__")

def fetch(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "trending-radar"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  fetch failed {url}: {e}", file=sys.stderr)
        return None

def fetch_readme(full_name, max_chars):
    for name in ("README.md", "readme.md", "README.MD", "Readme.md", "README.rst", "README", "README.txt"):
        t = fetch(f"https://raw.githubusercontent.com/{full_name}/HEAD/{name}", timeout=20)
        if t:
            t = re.sub(r"<img[^>]*>|<!--.*?-->|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)", "", t, flags=re.S)
            t = re.sub(r"\n{3,}", "\n\n", t)
            return t[:max_chars]
    return ""

def matches_rule(repo, rules):
    name = repo["full_name"].lower(); owner = name.split("/")[0]
    text = (name + " " + (repo.get("description") or "")).lower()
    lang = (repo.get("language") or "").lower()
    for r in rules:
        v = str(r.get("value", "")).lower().strip()
        if not v: continue
        t = r.get("type")
        if t == "owner" and owner == v: return r
        if t == "language" and lang == v: return r
        if t == "keyword" and v in text: return r
    return None

def plan(a):
    today = a.date or dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).date().isoformat()
    meta = load_dir(os.path.join(a.db, "meta"))
    repos = load_dir(os.path.join(a.db, "repos"))
    settings = meta.get("settings", {})
    rules = meta.get("rules", {}).get("rules", [])
    dismissed = meta.get("dismissed", {}).get("items", {})
    data_repo = settings.get("data_repo") or ""
    if a.latest:
        with open(a.latest, encoding="utf-8") as f: raw = f.read()
    else:
        if not data_repo:
            sys.exit("settings.data_repo is empty; set it on the page (Cài đặt) first")
        raw = fetch(f"https://raw.githubusercontent.com/{data_repo}/HEAD/data/latest.json")
        if not raw:
            sys.exit(f"could not fetch data/latest.json from {data_repo}")
    latest = json.loads(raw)
    stale = latest.get("date") != today
    writes = []
    seen = len(latest.get("repos", []))
    new_count = filtered = 0
    for it in latest.get("repos", []):
        k = key_of(it["full_name"])
        gained = max((ap.get("gained", 0) for ap in it.get("appearances", [])), default=0)
        best = max(it.get("appearances", []), key=lambda ap: ap.get("gained", 0), default={})
        periods = sorted({ap.get("period") for ap in it.get("appearances", [])})
        if k in dismissed:
            continue
        cur = repos.get(k)
        if cur:
            patch = {"last_seen": latest.get("date", today), "stars": max(cur.get("stars", 0), it.get("stars", 0)),
                     "gained_max": max(cur.get("gained_max", 0), gained), "periods_seen": sorted(set(cur.get("periods_seen", [])) | set(periods))}
            if cur.get("last_seen") != latest.get("date", today):
                patch["seen_days"] = cur.get("seen_days", 1) + 1
                if "daily" in periods: patch["daily_days"] = cur.get("daily_days", 0) + 1
            if gained >= cur.get("gained_max", 0): patch["gained_period"] = best.get("period", "")
            if not cur.get("description") and it.get("description"): patch["description"] = it["description"]
            writes.append({"op": "update", "collection": "repos", "doc_id": k, "data": patch})
            repos[k] = {**cur, **patch}
        else:
            rule = matches_rule(it, rules)
            if rule:
                filtered += 1; continue
            doc = {"full_name": it["full_name"], "description": it.get("description", ""), "language": it.get("language", ""),
                   "stars": it.get("stars", 0), "forks": it.get("forks", 0), "gained_max": gained, "gained_period": best.get("period", ""),
                   "periods_seen": periods, "first_seen": latest.get("date", today), "last_seen": latest.get("date", today),
                   "seen_days": 1, "daily_days": 1 if "daily" in periods else 0, "status": "new", "wanted": False}
            writes.append({"op": "set", "collection": "repos", "doc_id": k, "data": doc})
            repos[k] = doc; new_count += 1
    # archive old untouched "new" repos: fold into archive/YYYY-MM and delete
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=int(settings.get("archive_after_days", 28)))).isoformat()
    archived = 0
    arch_add = {}
    for k, r in list(repos.items()):
        if r.get("status") == "new" and not r.get("wanted") and str(r.get("last_seen", "")) < cutoff:
            month = str(r.get("first_seen", today))[:7]
            arch_add.setdefault(month, []).append({"full_name": r["full_name"], "language": r.get("language", ""), "stars": r.get("stars", 0),
                                                   "gained_max": r.get("gained_max", 0), "first_seen": r.get("first_seen"), "summary": (r.get("summary") or "")[:300], "score": r.get("score")})
            writes.append({"op": "delete", "collection": "repos", "doc_id": k}); archived += 1; del repos[k]
    # candidates
    limit = int(settings.get("research_limit", 15)); min_days = int(settings.get("min_daily_days", 2)); max_chars = int(settings.get("readme_chars", 12000))
    auto = settings.get("auto_research", True)
    pool = [r for r in repos.values() if not r.get("summary") and r.get("status") in ("new", "saved")]
    wanted = [r for r in pool if r.get("wanted")]
    eligible = [r for r in pool if not r.get("wanted") and ("weekly" in r.get("periods_seen", []) or "monthly" in r.get("periods_seen", []) or r.get("daily_days", 0) >= min_days)]
    eligible.sort(key=lambda r: -r.get("gained_max", 0))
    picked = wanted + (eligible[:max(0, limit - len(wanted))] if auto else [])
    cands = []
    for r in picked:
        print(f"readme {r['full_name']}")
        cands.append({"key": key_of(r["full_name"]), "full_name": r["full_name"], "description": r.get("description", ""), "language": r.get("language", ""),
                      "stars": r.get("stars", 0), "gained_max": r.get("gained_max", 0), "readme": fetch_readme(r["full_name"], max_chars)})
    os.makedirs(os.path.join(a.out, "writes"), exist_ok=True)
    for i in range(0, len(writes), 50):
        with open(os.path.join(a.out, "writes", f"{i//50:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(writes[i:i+50], f, ensure_ascii=False)
    with open(os.path.join(a.out, "candidates.json"), "w", encoding="utf-8") as f:
        json.dump(cands, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "archive_add.json"), "w", encoding="utf-8") as f:
        json.dump(arch_add, f, ensure_ascii=False)
    run = {"date": today, "data_date": latest.get("date"), "stale_data": stale, "seen": seen, "new_count": new_count, "filtered": filtered,
           "archived": archived, "candidates": len(cands), "researched": 0, "languages": latest.get("languages", []),
           "note": ("dữ liệu trending là của " + str(latest.get("date"))) if stale else ""}
    with open(os.path.join(a.out, "run.json"), "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in run.items() if k != "languages"}, ensure_ascii=False))
    print(f"batches: {(len(writes)+49)//50}, candidates: {len(cands)}, taste: {settings.get('taste_text','')[:80]!r}, model: {settings.get('research_model','haiku')}")

def apply(a):
    with open(os.path.join(a.out, "run.json"), encoding="utf-8") as f: run = json.load(f)
    with open(a.research, encoding="utf-8") as f: research = json.load(f)
    meta = load_dir(os.path.join(a.db, "meta")) if a.db else {}
    writes = []
    n = 0
    for r in research:
        if not r.get("key") or not r.get("summary"): continue
        score = r.get("score")
        try: score = max(1, min(5, int(score)))
        except Exception: score = None
        writes.append({"op": "update", "collection": "repos", "doc_id": r["key"], "data": {"summary": str(r["summary"]).strip(), "score": score, "wanted": False, "researched_at": run["date"]}})
        n += 1
    run["researched"] = n
    runs = meta.get("runs", {}).get("runs", [])
    runs = [x for x in runs if x.get("date") != run["date"]] + [{k: run[k] for k in ("date", "seen", "new_count", "filtered", "archived", "researched", "note")}]
    writes.append({"op": "set", "collection": "meta", "doc_id": "runs", "data": {"runs": runs[-90:]}})
    st = meta.get("settings", {})
    if run.get("languages") and run["languages"] != st.get("languages_seen"):
        writes.append({"op": "update", "collection": "meta", "doc_id": "settings", "data": {"languages_seen": run["languages"]}})
    arch_path = os.path.join(a.out, "archive_add.json")
    if os.path.exists(arch_path):
        with open(arch_path, encoding="utf-8") as f: arch = json.load(f)
        archive = load_dir(os.path.join(a.db, "archive")) if a.db else {}
        for month, items in arch.items():
            cur = archive.get(month, {}).get("items", [])
            writes.append({"op": "set", "collection": "archive", "doc_id": month, "data": {"items": (cur + items)[-1500:]}})
    existing = sorted(glob.glob(os.path.join(a.out, "writes", "*.json")))
    start = len(existing)
    for i in range(0, len(writes), 50):
        with open(os.path.join(a.out, "writes", f"{start + i//50:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(writes[i:i+50], f, ensure_ascii=False)
    print(f"apply: {n} summaries, {(len(writes)+49)//50} more batch files (total {start + (len(writes)+49)//50})")

if __name__ == "__main__":
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan"); s.add_argument("--db", required=True); s.add_argument("--out", required=True); s.add_argument("--date"); s.add_argument("--latest", help="local latest.json (testing)"); s.set_defaults(fn=plan)
    s = sub.add_parser("apply"); s.add_argument("--out", required=True); s.add_argument("--research", required=True); s.add_argument("--db"); s.set_defaults(fn=apply)
    a = p.parse_args(); a.fn(a)
