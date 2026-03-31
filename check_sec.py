import json
import os
import sys
import requests
from datetime import datetime, timezone

# ── Watchlist ─────────────────────────────────────────────────────────────────
WATCHLIST = [
    {
        "name":  "Rocket Lab (RKLB)",
        "cik":   "0001819994",
        "emoji": "🚀",
    },
    {
        "name":  "Kneron / Spark I Acquisition (SPKL)",
        "cik":   "0001884046",
        "emoji": "🧠",
        "note":  "SPAC vehicle for Kneron's Nasdaq listing — watch for S-4/F-4 merger filings",
    },
]

MAX_FILINGS   = 20
CACHE_TAG     = "sec-cache"
CACHE_ASSET   = "last_seen_filings.json"   # stores { cik: [accessionNumbers] }

# GitHub — injected by workflow
GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO  = os.environ["GITHUB_REPOSITORY"]
GH_API   = "https://api.github.com"

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Optional extra channels
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
NTFY_TOPIC      = os.getenv("NTFY_TOPIC")

SEC_HEADERS = {
    "User-Agent": "sec-monitor/1.0 bitcointest0206@gmail.com",
    "Accept": "application/json",
}

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
def fetch_latest_filings(cik: str) -> list[dict]:
    padded = cik.lstrip("0").zfill(10)
    url    = f"https://data.sec.gov/submissions/CIK{padded}.json"
    r      = requests.get(url, headers=SEC_HEADERS, timeout=15)
    r.raise_for_status()
    data   = r.json()

    recent = data.get("filings", {}).get("recent", {})
    acc    = recent.get("accessionNumber", [])
    forms  = recent.get("form", [])
    dates  = recent.get("filingDate", [])
    docs   = recent.get("primaryDocument", [])
    descs  = recent.get("primaryDocDescription", [])

    filings = []
    cik_short = cik.lstrip("0")
    for i in range(min(MAX_FILINGS, len(acc))):
        acc_clean = acc[i].replace("-", "")
        doc       = docs[i] if i < len(docs) else ""
        filings.append({
            "accessionNumber": acc[i],
            "form":        forms[i] if i < len(forms) else "",
            "filingDate":  dates[i] if i < len(dates) else "",
            "description": descs[i] if i < len(descs) else "",
            "primaryDocument": doc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc_clean}/{doc}",
            "indexUrl": (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK={cik}&type=&dateb=&owner=include&count=10"
            ),
        })
    return filings

# ── GitHub Release cache ──────────────────────────────────────────────────────
def _get_release_by_tag(tag: str) -> dict | None:
    r = requests.get(f"{GH_API}/repos/{GH_REPO}/releases/tags/{tag}", headers=GH_HEADERS, timeout=10)
    return r.json() if r.ok else None

def _create_release(tag: str, name: str, body: str, prerelease=False) -> dict:
    r = requests.post(
        f"{GH_API}/repos/{GH_REPO}/releases",
        headers=GH_HEADERS,
        json={"tag_name": tag, "name": name, "body": body, "prerelease": prerelease, "draft": False},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()

def _delete_asset(asset_id: int):
    requests.delete(f"{GH_API}/repos/{GH_REPO}/releases/assets/{asset_id}", headers=GH_HEADERS, timeout=10)

def _upload_asset(upload_url: str, filename: str, content: bytes):
    base_url = upload_url.split("{")[0]
    headers  = {**GH_HEADERS, "Content-Type": "application/json"}
    r = requests.post(f"{base_url}?name={filename}", headers=headers, data=content, timeout=15)
    r.raise_for_status()

def load_cache() -> dict[str, set[str]]:
    """Returns { cik: set(accessionNumbers) }"""
    release = _get_release_by_tag(CACHE_TAG)
    if not release:
        print("  ℹ️  No cache release found — first run.")
        return {}

    asset = next((a for a in release.get("assets", []) if a["name"] == CACHE_ASSET), None)
    if not asset:
        return {}

    r = requests.get(
        asset["browser_download_url"],
        headers={"Authorization": f"Bearer {GH_TOKEN}"},
        timeout=10,
    )
    if not r.ok:
        return {}

    raw = r.json()   # { cik: [acc1, acc2, ...] }
    return {cik: set(nums) for cik, nums in raw.items()}

def save_cache(cache: dict[str, list[str]]):
    """cache = { cik: [accessionNumbers] }"""
    content = json.dumps(cache, indent=2).encode()

    release = _get_release_by_tag(CACHE_TAG)
    if not release:
        companies = " | ".join(w["name"] for w in WATCHLIST)
        release = _create_release(
            tag       = CACHE_TAG,
            name      = "SEC Filing Cache (do not delete)",
            body      = f"Auto-managed by SEC monitor. Tracks: {companies}",
            prerelease= True,
        )
        print("  ℹ️  Created new sec-cache release.")

    for asset in release.get("assets", []):
        if asset["name"] == CACHE_ASSET:
            _delete_asset(asset["id"])

    _upload_asset(release["upload_url"], CACHE_ASSET, content)
    print("  ✅ Cache uploaded to GitHub Release.")

# ── Publish GitHub Release for new filings ────────────────────────────────────
def publish_filing_release(company: dict, new_filings: list[dict]) -> str:
    now        = datetime.now(timezone.utc)
    form_types = ", ".join(dict.fromkeys(f["form"] for f in new_filings))
    tag        = f"filing-{company['cik'].lstrip('0')}-{now.strftime('%Y%m%d-%H%M%S')}"
    name       = f"{company['emoji']} {company['name']} — New Filing: {form_types} ({now.strftime('%Y-%m-%d')})"

    lines = [f"## {company['emoji']} New SEC Filing(s) — {company['name']}\n"]
    if note := company.get("note"):
        lines.append(f"> ℹ️ {note}\n")
    for f in new_filings:
        desc = f["description"] or f["primaryDocument"]
        lines += [
            f"### `{f['form']}` — {f['filingDate']}",
            f"- **Description:** {desc}",
            f"- **Document:** [{f['primaryDocument']}]({f['url']})",
            f"- **EDGAR Index:** [View all filings]({f['indexUrl']})",
            "",
        ]
    lines.append(
        f"---\n*Detected at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC "
        f"by [sec-monitor](https://github.com/{GH_REPO})*"
    )

    rel = _create_release(tag=tag, name=name, body="\n".join(lines), prerelease=False)
    print(f"  ✅ GitHub Release published: {rel['html_url']}")
    return rel["html_url"]

# ── Optional extra notifications ──────────────────────────────────────────────
def notify_slack(text: str):
    if SLACK_WEBHOOK:
        requests.post(SLACK_WEBHOOK, json={"text": f"```{text}```"}, timeout=10)

def notify_discord(text: str):
    if DISCORD_WEBHOOK:
        requests.post(DISCORD_WEBHOOK, json={"content": f"```{text}```"}, timeout=10)

def notify_ntfy(text: str, company: dict, new_filings: list[dict]):
    if NTFY_TOPIC:
        form_types = ", ".join(set(f["form"] for f in new_filings))
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode(),
            headers={
                "Title": f"{company['emoji']} {company['name']}: {form_types}",
                "Priority": "high",
                "Tags": "chart_with_upwards_trend",
            },
            timeout=10,
        )

def write_github_summary(company: dict, new_filings: list[dict], release_url: str):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(f"## {company['emoji']} {company['name']} — {len(new_filings)} New Filing(s)\n\n")
        fh.write("| Form | Filed | Description | Link |\n|------|-------|-------------|------|\n")
        for f in new_filings:
            desc = f["description"] or f["primaryDocument"]
            fh.write(f"| `{f['form']}` | {f['filingDate']} | {desc} | [View]({f['url']}) |\n")
        fh.write(f"\n[📦 GitHub Release]({release_url})\n\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(timezone.utc).isoformat()
    print(f"[{now_str}] SEC EDGAR monitor starting — {len(WATCHLIST)} companies\n")

    cache       = load_cache()       # { cik: set(accNums) }
    new_cache   = {}                 # will be saved at the end
    any_error   = False

    for company in WATCHLIST:
        cik  = company["cik"]
        name = company["name"]
        print(f"── {company['emoji']} {name} ({cik})")

        try:
            filings = fetch_latest_filings(cik)
        except Exception as e:
            print(f"  ❌ Failed to fetch: {e}", file=sys.stderr)
            any_error = True
            new_cache[cik] = list(cache.get(cik, []))
            continue

        seen        = cache.get(cik, set())
        all_acc     = [f["accessionNumber"] for f in filings]
        new_filings = [f for f in filings if f["accessionNumber"] not in seen]

        new_cache[cik] = all_acc

        if not new_filings:
            print("  ✅ No new filings.")
            continue

        print(f"  🔔 {len(new_filings)} new filing(s):")
        for f in new_filings:
            print(f"     • [{f['form']}] {f['filingDate']} — {f['description'] or f['primaryDocument']}")

        release_url = publish_filing_release(company, new_filings)

        summary = "\n".join(
            f"[{f['form']}] {f['filingDate']} {f['description'] or f['primaryDocument']}\n{f['url']}"
            for f in new_filings
        )
        msg = f"{company['emoji']} {name} new SEC filing(s):\n{summary}"
        notify_slack(msg)
        notify_discord(msg)
        notify_ntfy(summary, company, new_filings)
        write_github_summary(company, new_filings, release_url)

        print()

    save_cache(new_cache)
    print("\n✅ All done.")
    if any_error:
        sys.exit(1)

if __name__ == "__main__":
    main()
