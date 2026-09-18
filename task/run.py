#!/usr/bin/env python3
"""Trending Radar task helper. Runs inside the daily scheduled Claude session.

Store layout (artifact db):
  meta/settings, meta/rules, meta/dismissed   edited by the page (task only reads them)
  repos/<key>       one doc per repo. Created ONCE by the task; the page edits status/note/tags;
                    the task later only writes summary/score/wanted (pinned with a version) or deletes.
  sightings/<date>  one doc per trending day: items {key: {p:[periods], g:gained, s:stars, l:lang}},
                    languages, run counters, researched keys, deleted keys.
The set of repos in the store = union(sightings.items) - union(sightings.deleted). No listing of
repos is ever needed. Raw archive = the git repo (data/<date>.json).

Usage:
  run.py plan  --db DB --out OUT [--latest FILE] [--date D] [--force]
      DB/meta/*.json, DB/sightings/*.json (and optionally DB/repos/*.json as extra "existing" keys)
      -> OUT/candidates.json (repos to research, with README), OUT/state.json
  run.py apply --out OUT [--research R.json] [--versions V.json] [--note TEXT]
      V.json = {"<key>": <version>} for candidates/archive keys that already exist in the store
      -> OUT/writes/*.json batch files for ArtifactData "batch" (each <= 50 entries)
"""
import argparse, datetime as dt, glob, json, os, re, sys, urllib.request

def load_dir(d):
    out = {}
    for p in glob.glob(os.path.join(d, "*.json")):
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        out[os.path.splitext(os.path.basename(p))[0]] = doc.get("data", doc)
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
        if (t == "owner" and owner == v) or (t == "language" and lang == v) or (t == "keyword" and v in text):
            return r
    return None

def write_batches(out, writes):
    wdir = os.path.join(out, "writes"); ddir = os.path.join(out, "docs")
    os.makedirs(wdir, exist_ok=True); os.makedirs(ddir, exist_ok=True)
    for f in glob.glob(os.path.join(wdir, "*.json")): os.remove(f)
    for i in range(0, len(writes), 50):
        batch = []
        for j, w in enumerate(writes[i:i+50]):
            e = {k: w[k] for k in ("op", "collection", "doc_id", "if_version") if k in w}
            if "data" in w:
                fp = os.path.abspath(os.path.join(ddir, f"{i//50:03d}_{j:02d}.json"))
                with open(fp, "w", encoding="utf-8") as f: json.dump(w["data"], f, ensure_ascii=False)
                e["file_path"] = fp
            batch.append(e)
        with open(os.path.join(wdir, f"{i//50:03d}.json"), "w", encoding="utf-8") as f:
            json.dump(batch, f, ensure_ascii=False)
    return (len(writes) + 49) // 50

def history(sightings):
    """per key: days, daily_days, gained_max, stars, periods, name, desc, lang; plus researched/deleted sets"""
    h, researched, deleted = {}, set(), set()
    for date, doc in sorted(sightings.items()):
        for k, it in (doc.get("items") or {}).items():
            e = h.setdefault(k, {"days": set(), "daily_days": set(), "gained_max": 0, "stars": 0, "periods": set()})
            e["days"].add(date); ps = set(it.get("p") or []); e["periods"] |= ps
            if "daily" in ps: e["daily_days"].add(date)
            e["gained_max"] = max(e["gained_max"], it.get("g", 0)); e["stars"] = max(e["stars"], it.get("s", 0))
            for f in ("n", "d", "l"):
                if it.get(f): e[f] = it[f]
        researched |= set(doc.get("researched") or [])
        deleted |= set(doc.get("deleted") or [])
        for k in doc.get("deleted") or []:
            h.pop(k, None)
    return h, researched - deleted, deleted

def plan(a):
    today = a.date or dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).date().isoformat()
    meta = load_dir(os.path.join(a.db, "meta"))
    sightings = load_dir(os.path.join(a.db, "sightings"))
    extra_existing = load_dir(os.path.join(a.db, "repos"))  # optional bootstrap dump
    settings = meta.get("settings", {})
    rules = meta.get("rules", {}).get("rules", [])
    dismissed = meta.get("dismissed", {}).get("items", {})
    data_repo = settings.get("data_repo") or ""
    if a.latest:
        with open(a.latest, encoding="utf-8") as f: raw = f.read()
    else:
        if not data_repo: sys.exit("settings.data_repo is empty; set it on the page (Cài đặt) first")
        raw = fetch(f"https://raw.githubusercontent.com/{data_repo}/HEAD/data/latest.json")
        if not raw: sys.exit(f"could not fetch data/latest.json from {data_repo}")
    latest = json.loads(raw)
    ddate = latest.get("date", today)
    if ddate in sightings and not a.force:
        sys.exit(f"sightings/{ddate} already exists: this trending data was processed. Nothing to do.")
    hist, researched, deleted = history(sightings)
    existing = set(hist) | set(extra_existing)
    for k, r in extra_existing.items():
        if r.get("summary"): researched.add(k)
    items, new_docs, filtered = {}, {}, 0
    for it in latest.get("repos", []):
        k = key_of(it["full_name"])
        aps = it.get("appearances", [])
        gained = max((ap.get("gained", 0) for ap in aps), default=0)
        periods = sorted({ap.get("period") for ap in aps})
        if k in dismissed: continue
        if k not in existing and matches_rule(it, rules):
            filtered += 1; continue
        items[k] = {"p": periods, "g": gained, "s": it.get("stars", 0), "l": it.get("language", ""), "n": it["full_name"]}
        if k not in existing:
            items[k]["d"] = (it.get("description") or "")[:200]
            new_docs[k] = {"key": k, "full_name": it["full_name"], "description": it.get("description", ""),
                           "language": it.get("language", ""), "stars": it.get("stars", 0), "forks": it.get("forks", 0),
                           "first_seen": ddate, "status": "new", "wanted": False}
    sightings[ddate] = {"items": items}
    hist, _, _ = history(sightings)
    limit = int(settings.get("research_limit", 15)); min_days = int(settings.get("min_daily_days", 2))
    max_chars = int(settings.get("readme_chars", 12000)); auto = settings.get("auto_research", True)
    def eligible(k):
        h = hist.get(k, {})
        return bool(h.get("periods", set()) & {"weekly", "monthly"}) or len(h.get("daily_days", ())) >= min_days
    pool = [k for k in (existing | set(new_docs)) if k not in researched and k in hist]
    wanted = [k for k in a.wanted.split(",") if k] if a.wanted else []
    wanted = [k for k in wanted if k in existing and k not in researched]
    elig = sorted([k for k in pool if k not in wanted and eligible(k)], key=lambda k: -hist[k]["gained_max"])
    picked = wanted + (elig[:max(0, limit - len(wanted))] if auto else [])
    cands = []
    for k in picked:
        h = hist[k]; r = new_docs.get(k) or extra_existing.get(k) or {}
        name = r.get("full_name") or h.get("n") or k.replace("__", "/", 1)
        print(f"readme {name}")
        cands.append({"key": k, "full_name": name, "description": r.get("description") or h.get("d", ""), "language": r.get("language") or h.get("l", ""),
                      "stars": h["stars"], "gained_max": h["gained_max"], "existing": k in existing, "readme": fetch_readme(name, max_chars)})
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=int(settings.get("archive_after_days", 28)))).isoformat()
    stale = sorted(k for k, h in hist.items() if k in existing and k not in researched and k not in items and max(h["days"]) < cutoff)
    os.makedirs(a.out, exist_ok=True)
    state = {"date": today, "data_date": ddate, "seen": len(latest.get("repos", [])), "new_count": len(new_docs), "filtered": filtered,
             "languages": latest.get("languages", []), "items": items, "new_docs": new_docs, "archive_keys": stale, "archive_cutoff": cutoff,
             "candidates": [c["key"] for c in cands], "existing_candidates": [c["key"] for c in cands if c["existing"]]}
    with open(os.path.join(a.out, "state.json"), "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False)
    with open(os.path.join(a.out, "candidates.json"), "w", encoding="utf-8") as f: json.dump(cands, f, ensure_ascii=False, indent=1)
    print(json.dumps({"date": today, "data_date": ddate, "seen": state["seen"], "new": len(new_docs), "filtered": filtered,
                      "candidates": len(cands), "existing_candidates": state["existing_candidates"],
                      "archive_candidates": stale, "model": settings.get("research_model", "haiku")}, ensure_ascii=False))

def apply(a):
    with open(os.path.join(a.out, "state.json"), encoding="utf-8") as f: st = json.load(f)
    research, versions = [], {}
    if a.research and os.path.exists(a.research):
        with open(a.research, encoding="utf-8") as f: research = json.load(f)
    if a.versions and os.path.exists(a.versions):
        with open(a.versions, encoding="utf-8") as f: versions = json.load(f)
    res = {}
    for r in research:
        if not r.get("key") or not r.get("summary"): continue
        try: score = max(1, min(5, int(r.get("score"))))
        except Exception: score = None
        res[r["key"]] = {"summary": str(r["summary"]).strip(), "score": score, "wanted": False, "researched_at": st["date"]}
    writes, done, missing = [], [], []
    for k, doc in st["new_docs"].items():
        if k in res: doc = {**doc, **res[k]}; done.append(k)
        writes.append({"op": "set", "collection": "repos", "doc_id": k, "data": doc})
    for k in st["existing_candidates"]:
        if k not in res: continue
        if k in versions:
            writes.append({"op": "update", "collection": "repos", "doc_id": k, "data": res[k], "if_version": int(versions[k])}); done.append(k)
        else: missing.append(k)
    deleted = [k for k in st["archive_keys"] if k in versions]
    for k in deleted:
        writes.append({"op": "delete", "collection": "repos", "doc_id": k, "if_version": int(versions[k])})
    writes.append({"op": "set", "collection": "sightings", "doc_id": st["data_date"], "data": {
        "date": st["data_date"], "items": st["items"], "languages": st["languages"], "researched": done, "deleted": deleted,
        "run": {"date": st["date"], "seen": st["seen"], "new_count": st["new_count"], "filtered": st["filtered"],
                "researched": len(done), "archived": len(deleted), "note": a.note or ""}}})
    n = write_batches(a.out, writes)
    print(f"apply: {len(st['new_docs'])} new repos, {len(done)} summaries, {len(deleted)} deleted, {n} batch file(s) in {a.out}/writes")
    if missing: print(f"WARNING: no version for existing candidates, summaries NOT written: {missing}")
    skipped = [k for k in st["archive_keys"] if k not in versions]
    if skipped: print(f"NOTE: {len(skipped)} stale repos not deleted (no version supplied)")

if __name__ == "__main__":
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan"); s.add_argument("--db", required=True); s.add_argument("--out", required=True); s.add_argument("--date")
    s.add_argument("--latest"); s.add_argument("--force", action="store_true"); s.add_argument("--wanted", help="comma-separated keys flagged 'wanted' on the page"); s.set_defaults(fn=plan)
    s = sub.add_parser("apply"); s.add_argument("--out", required=True); s.add_argument("--research"); s.add_argument("--versions"); s.add_argument("--note"); s.set_defaults(fn=apply)
    a = p.parse_args(); a.fn(a)
