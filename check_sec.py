import json
import os
import sys
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CIK           = "0001819994"
COMPANY       = "Rocket Lab (RKLB)"
MAX_FILINGS   = 20
CACHE_TAG     = "sec-cache"          # persistent release that stores the cache
CACHE_ASSET   = "last_seen_filings.json"

# GitHub — injected by workflow via env
GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO  = os.environ["GITHUB_REPOSITORY"]   # e.g. "yourname/yourrepo"
GH_API   = "https://api.github.com"

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Optional extra notification channels (GitHub Secrets)
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
NTFY_TOPIC      = os.getenv("NTFY_TOPIC")

SEC_HEADERS = {
    "User-Agent": "sec-monitor/1.0 bitcointest0206@gmail.com",
    "Accept": "application/json",
}

# ── SEC EDGAR ─────────────────────────────────────────────────────────────────
def fetch_latest_filings() -> list[dict]:
    padded = CIK.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    recent   = data.get("filings", {}).get("recent", {})
    acc_nums = recent.get("accessionNumber", [])
    forms    = recent.get("form", [])
    dates    = recent.get("filingDate", [])
    docs     = recent.get("primaryDocument", [])
    descs    = recent.get("primaryDocDescription", [])

    filings = []
    for i in range(min(MAX_FILINGS, len(acc_nums))):
        acc_clean = acc_nums[i].replace("-", "")
        cik_short = CIK.lstrip("0")
        doc       = docs[i] if i < len(docs) else ""
        filings.append({
            "accessionNumber": acc_nums[i],
            "form":        forms[i]  if i < len(forms)  else "",
            "filingDate":  dates[i]  if i < len(dates)  else "",
            "description": descs[i]  if i < len(descs)  else "",
            "primaryDocument": doc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc_clean}/{doc}",
            "indexUrl": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}&type=&dateb=&owner=include&count=10",
        })
    return filings

# ── GitHub Release cache helpers ──────────────────────────────────────────────
def _get_release_by_tag(tag: str) -> dict | None:
    r = requests.get(f"{GH_API}/repos/{GH_REPO}/releases/tags/{tag}", headers=GH_HEADERS, timeout=10)
    return r.json() if r.ok else None

def _create_release(tag: str, name: str, body: str, prerelease=False) -> dict:
    payload = {
        "tag_name":   tag,
        "name":       name,
        "body":       body,
        "prerelease": prerelease,
        "draft":      False,
    }
    r = requests.post(f"{GH_API}/repos/{GH_REPO}/releases", headers=GH_HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def _delete_asset(asset_id: int):
    requests.delete(f"{GH_API}/repos/{GH_REPO}/releases/assets/{asset_id}", headers=GH_HEADERS, timeout=10)

def _upload_asset(upload_url: str, filename: str, content: bytes):
    # upload_url is like: https://uploads.github.com/repos/.../assets{?name,label}
    base_url = upload_url.split("{")[0]
    headers  = {**GH_HEADERS, "Content-Type": "application/json", "Accept": "application/vnd.github+json"}
    r = requests.post(f"{base_url}?name={filename}", headers=headers, data=content, timeout=15)
    r.raise_for_status()

def load_cache() -> set[str]:
    """Download cache JSON from the sec-cache release asset."""
    release = _get_release_by_tag(CACHE_TAG)
    if not release:
        print("  ℹ️  No cache release found — first run, treating all filings as new.")
        return set()

    assets = release.get("assets", [])
    asset  = next((a for a in assets if a["name"] == CACHE_ASSET), None)
    if not asset:
        return set()

    r = requests.get(
        asset["browser_download_url"],
        headers={"Authorization": f"Bearer {GH_TOKEN}"},
        timeout=10,
    )
    if not r.ok:
        return set()
    return set(r.json())

def save_cache(acc_nums: list[str]):
    """Upload updated cache JSON to the sec-cache release, replacing the old asset."""
    content = json.dumps(acc_nums, indent=2).encode()

    release = _get_release_by_tag(CACHE_TAG)
    if not release:
        # Create the cache release for the first time
        release = _create_release(
            tag       = CACHE_TAG,
            name      = "SEC Filing Cache (do not delete)",
            body      = "Auto-managed by SEC monitor workflow. Stores the list of already-seen filings.",
            prerelease= True,   # mark as pre-release so it doesn't clutter the releases page
        )
        print("  ℹ️  Created new sec-cache release.")

    # Delete old asset if it exists
    for asset in release.get("assets", []):
        if asset["name"] == CACHE_ASSET:
            _delete_asset(asset["id"])

    _upload_asset(release["upload_url"], CACHE_ASSET, content)
    print("  ✅ Cache uploaded to GitHub Release.")

# ── Publish new-filing release ────────────────────────────────────────────────
def publish_filing_release(new_filings: list[dict]):
    """Create a real GitHub Release for each batch of new filings."""
    now       = datetime.now(timezone.utc)
    form_types = ", ".join(dict.fromkeys(f["form"] for f in new_filings))  # deduplicated, ordered
    tag        = f"filing-{now.strftime('%Y%m%d-%H%M%S')}"
    name       = f"🚀 {COMPANY} — New Filing: {form_types} ({now.strftime('%Y-%m-%d')})"

    # Build markdown body
    lines = [f"## New SEC Filing(s) detected for {COMPANY}\n"]
    for f in new_filings:
        desc = f["description"] or f["primaryDocument"]
        lines += [
            f"### `{f['form']}` — {f['filingDate']}",
            f"- **Description:** {desc}",
            f"- **Document:** [{f['primaryDocument']}]({f['url']})",
            f"- **EDGAR Index:** [View all filings]({f['indexUrl']})",
            "",
        ]
    lines.append(f"---\n*Detected at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC by [sec-monitor](https://github.com/{GH_REPO})*")
    body = "\n".join(lines)

    release = _create_release(tag=tag, name=name, body=body, prerelease=False)
    print(f"  ✅ GitHub Release published: {release['html_url']}")
    return release["html_url"]

# ── Optional extra notifications ──────────────────────────────────────────────
def notify_slack(text: str):
    if SLACK_WEBHOOK:
        requests.post(SLACK_WEBHOOK, json={"text": f"```{text}```"}, timeout=10)
        print("  ✅ Slack notified")

def notify_discord(text: str):
    if DISCORD_WEBHOOK:
        requests.post(DISCORD_WEBHOOK, json={"content": f"```{text}```"}, timeout=10)
        print("  ✅ Discord notified")

def notify_ntfy(text: str, new_filings: list[dict]):
    if NTFY_TOPIC:
        form_types = ", ".join(set(f["form"] for f in new_filings))
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=text.encode(),
            headers={"Title": f"🚀 {COMPANY}: {form_types}", "Priority": "high", "Tags": "rocket"},
            timeout=10,
        )
        print("  ✅ ntfy notified")

def write_github_summary(new_filings: list[dict], release_url: str):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"## 🚀 {COMPANY} — {len(new_filings)} New Filing(s)\n\n")
        f.write("| Form | Filed | Description | Link |\n|------|-------|-------------|------|\n")
        for fil in new_filings:
            desc = fil["description"] or fil["primaryDocument"]
            f.write(f"| `{fil['form']}` | {fil['filingDate']} | {desc} | [View]({fil['url']}) |\n")
        f.write(f"\n[📦 GitHub Release]({release_url})\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Checking SEC EDGAR for {COMPANY}…")

    try:
        filings = fetch_latest_filings()
    except Exception as e:
        print(f"❌ Failed to fetch filings: {e}", file=sys.stderr)
        sys.exit(1)

    seen         = load_cache()
    new_filings  = [f for f in filings if f["accessionNumber"] not in seen]
    all_acc_nums = [f["accessionNumber"] for f in filings]

    if not new_filings:
        print("✅ No new filings found.")
        # Always refresh cache (keeps the seen list up-to-date even with no new filings)
        save_cache(all_acc_nums)
        return

    print(f"🔔 Found {len(new_filings)} new filing(s):")
    for f in new_filings:
        print(f"   • [{f['form']}] {f['filingDate']} — {f['description'] or f['primaryDocument']}")

    # 1. Publish GitHub Release (primary notification)
    release_url = publish_filing_release(new_filings)

    # 2. Optional extra channels
    summary = "\n".join(
        f"[{f['form']}] {f['filingDate']} {f['description'] or f['primaryDocument']}\n{f['url']}"
        for f in new_filings
    )
    notify_slack(f"🚀 {COMPANY} new SEC filing(s):\n{summary}")
    notify_discord(f"🚀 {COMPANY} new SEC filing(s):\n{summary}")
    notify_ntfy(summary, new_filings)

    # 3. Update cache
    save_cache(all_acc_nums)
    write_github_summary(new_filings, release_url)
    print("✅ Done.")

if __name__ == "__main__":
    main()
