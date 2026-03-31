"""
SEC EDGAR New Filing Monitor — Rocket Lab (CIK: 0001819994)
Compares latest filings against a local cache and sends notifications for new ones.
"""

import json
import os
import sys
import requests
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CIK        = "0001819994"
COMPANY    = "Rocket Lab (RKLB)"
CACHE_FILE = "last_seen_filings.json"
MAX_FILINGS = 20   # how many recent filings to fetch each run

# Notification channels — controlled by env vars (set as GitHub Secrets)
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL")    # optional
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL") # optional
NTFY_TOPIC     = os.getenv("NTFY_TOPIC")           # optional  e.g. "my-sec-alerts"

# ── SEC EDGAR API ─────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "sec-monitor/1.0 your-email@example.com",  # ← 改成你的 email
    "Accept": "application/json",
}

def fetch_latest_filings(cik: str) -> list[dict]:
    """Fetch recent filings from SEC EDGAR submissions API."""
    padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    acc_nums   = recent.get("accessionNumber", [])
    forms      = recent.get("form", [])
    filed_dates = recent.get("filingDate", [])
    descriptions = recent.get("primaryDocument", [])
    doc_descriptions = recent.get("primaryDocDescription", [])

    filings = []
    for i in range(min(MAX_FILINGS, len(acc_nums))):
        acc_clean = acc_nums[i].replace("-", "")
        filings.append({
            "accessionNumber": acc_nums[i],
            "form":            forms[i] if i < len(forms) else "",
            "filingDate":      filed_dates[i] if i < len(filed_dates) else "",
            "primaryDocument": descriptions[i] if i < len(descriptions) else "",
            "description":     doc_descriptions[i] if i < len(doc_descriptions) else "",
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/"
                f"{acc_clean}/{descriptions[i] if i < len(descriptions) else ''}"
            ),
            "indexUrl": (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK={cik}&type=&dateb=&owner=include&count=10"
            ),
        })
    return filings

# ── Cache helpers ─────────────────────────────────────────────────────────────
def load_cache() -> set[str]:
    p = Path(CACHE_FILE)
    if not p.exists():
        return set()
    with open(p) as f:
        return set(json.load(f))

def save_cache(acc_nums: list[str]):
    with open(CACHE_FILE, "w") as f:
        json.dump(acc_nums, f)

# ── Notifications ─────────────────────────────────────────────────────────────
def build_message(new_filings: list[dict]) -> str:
    lines = [f"🚀 {COMPANY} — {len(new_filings)} new SEC filing(s) detected!\n"]
    for f in new_filings:
        lines.append(
            f"  📄 Form: {f['form']}\n"
            f"     Filed: {f['filingDate']}\n"
            f"     Desc:  {f['description'] or f['primaryDocument']}\n"
            f"     Link:  {f['url']}\n"
        )
    lines.append(f"\n🕐 Checked at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return "\n".join(lines)

def notify_slack(text: str):
    if not SLACK_WEBHOOK:
        return
    payload = {"text": f"```{text}```"}
    requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    print("  ✅ Slack notified")

def notify_discord(text: str):
    if not DISCORD_WEBHOOK:
        return
    payload = {"content": f"```{text}```"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    print("  ✅ Discord notified")

def notify_ntfy(text: str, new_filings: list[dict]):
    if not NTFY_TOPIC:
        return
    form_types = ", ".join(set(f["form"] for f in new_filings))
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=text.encode("utf-8"),
        headers={
            "Title": f"🚀 {COMPANY} new filing: {form_types}",
            "Priority": "high",
            "Tags": "rocket,chart_with_upwards_trend",
        },
        timeout=10,
    )
    print("  ✅ ntfy notified")

def write_github_summary(new_filings: list[dict]):
    """Write a step summary to GitHub Actions UI (optional but nice)."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a") as f:
        f.write(f"## 🚀 {COMPANY} — New SEC Filings\n\n")
        f.write(f"| Form | Filed Date | Description | Link |\n")
        f.write(f"|------|-----------|-------------|------|\n")
        for fil in new_filings:
            desc = fil["description"] or fil["primaryDocument"]
            f.write(f"| `{fil['form']}` | {fil['filingDate']} | {desc} | [View]({fil['url']}) |\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.utcnow().isoformat()}] Checking SEC EDGAR for {COMPANY}…")

    try:
        filings = fetch_latest_filings(CIK)
    except Exception as e:
        print(f"❌ Failed to fetch filings: {e}", file=sys.stderr)
        sys.exit(1)

    seen = load_cache()
    current_acc_nums = [f["accessionNumber"] for f in filings]

    new_filings = [f for f in filings if f["accessionNumber"] not in seen]

    if not new_filings:
        print("✅ No new filings found.")
        save_cache(current_acc_nums)
        return

    print(f"🔔 Found {len(new_filings)} new filing(s):")
    for f in new_filings:
        print(f"   • [{f['form']}] {f['filingDate']} — {f['description'] or f['primaryDocument']}")

    msg = build_message(new_filings)
    notify_slack(msg)
    notify_discord(msg)
    notify_ntfy(msg, new_filings)
    write_github_summary(new_filings)

    save_cache(current_acc_nums)
    print("✅ Cache updated.")

if __name__ == "__main__":
    main()
