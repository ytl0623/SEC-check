import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

# ── Configuration ─────────────────────────────────────────────────────────────
MARKET_CAP_THRESHOLD = 1_000_000_000_000  # 1 Trillion USD (1T 美金)

EXTRA_WATCHLIST = [
    {"name": "Anthropic (ANT)",           "cik": "0002130357"},
    {"name": "Rocket Lab (RKLB)",         "cik": "0001819994"},
    {"name": "SpaceX (SPCX)",             "cik": "0001181412"},
    {"name": "Intuitive Machines (LUNR)", "cik": "0001844452"},
    {"name": "AST SpaceMobile (ASTS)",    "cik": "0001780312"},
    {"name": "Planet Labs (PL)",          "cik": "0001836833"},
    {"name": "Redwire (RDW)",             "cik": "0001819810"},
]

MAX_FILINGS   = 20
SCAN_LIMIT    = 150

EXCLUDE_FORMS = {"4", "4/A"}
EXCLUDE_DESCS = {"FORM 4"}
CACHE_TAG     = "sec-cache"
CACHE_ASSET   = "last_seen_filings.json"

GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO  = os.environ["GITHUB_REPOSITORY"]
GH_API   = "https://api.github.com"

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

SEC_HEADERS = {
    "User-Agent": "sec-monitor/1.0 bitcointest0206@gmail.com",
    "Accept": "application/json",
}


# ── Dynamic Watchlist (> $1T Market Cap) ──────────────────────────────────────
def _fetch_sec_ticker_map() -> dict[str, dict]:
    """從 SEC 官方取得所有美股 Ticker -> CIK 對照表"""
    url = "https://www.sec.gov/files/company_tickers.json"
    r = requests.get(url, headers=SEC_HEADERS, timeout=15)
    r.raise_for_status()
    raw = r.json()

    ticker_map = {}
    for entry in raw.values():
        ticker = str(entry.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        ticker_map[ticker] = {
            "cik": str(entry["cik_str"]).zfill(10),
            "title": entry.get("title", ticker),
        }
    return ticker_map


def _fetch_mega_caps_tradingview(min_mcap: float) -> list[dict]:
    """主資料源：透過 TradingView Scanner API 篩選美股市值 > 1T 公司"""
    url = "https://scanner.tradingview.com/america/scan"
    payload = {
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": min_mcap},
            {"left": "is_primary", "operation": "equal", "right": True},
        ],
        "options": {"lang": "en"},
        "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": ["name", "description", "market_cap_basic"],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 100],
    }
    r = requests.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()

    results = []
    for row in r.json().get("data", []):
        cols = row.get("d", [])
        if len(cols) >= 3 and cols[2] is not None:
            results.append({
                "ticker": str(cols[0]).upper(),
                "name": str(cols[1]),
                "market_cap": float(cols[2]),
            })
    return results


def _fetch_mega_caps_nasdaq(min_mcap: float) -> list[dict]:
    """備援資料源：透過 Nasdaq Screener API 篩選美股市值 > 1T 公司"""
    url = "https://api.nasdaq.com/api/screener/stocks"
    params = {"tableonly": "true", "limit": 100, "marketcap": "mega"}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()

    rows = r.json().get("data", {}).get("rows", [])
    results = []
    for row in rows:
        mcap_str = str(row.get("marketCap", "")).replace(",", "").replace("$", "").strip()
        if not mcap_str:
            continue
        try:
            mcap = float(mcap_str)
        except ValueError:
            continue
        if mcap >= min_mcap:
            results.append({
                "ticker": str(row.get("symbol", "")).strip().upper(),
                "name": str(row.get("name", "")).strip(),
                "market_cap": mcap,
            })

    results.sort(key=lambda x: x["market_cap"], reverse=True)
    return results


def build_dynamic_watchlist(min_mcap: float = MARKET_CAP_THRESHOLD) -> list[dict]:
    """組合市值 > 1T 公司清單並對應 SEC CIK"""
    ticker_map = _fetch_sec_ticker_map()

    try:
        candidates = _fetch_mega_caps_tradingview(min_mcap)
        source = "TradingView"
    except Exception as e:
        print(f"  [warn] TradingView screener failed ({e}), falling back to Nasdaq API...")
        candidates = _fetch_mega_caps_nasdaq(min_mcap)
        source = "Nasdaq"

    watchlist = []
    seen_ciks = set()

    for item in candidates:
        raw_ticker = item["ticker"]
        # SEC 格式中點號以連字號表示（例如 BRK.B -> BRK-B）
        sec_ticker = raw_ticker.replace(".", "-")
        sec_info = ticker_map.get(sec_ticker) or ticker_map.get(raw_ticker)

        if not sec_info:
            print(f"  [skip] {raw_ticker} ({item['name']}) — 無對應 SEC CIK（可能為非美股掛牌）")
            continue

        cik = sec_info["cik"]
        # 避免雙重股權（如 GOOG/GOOGL 或 BRK.A/BRK.B）導致同一個 CIK 重複抓取
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)

        mcap_t = item["market_cap"] / 1e12
        display_name = f"{item['name']} ({raw_ticker})"
        watchlist.append({
            "name": display_name,
            "cik": cik,
            "note": f"Market Cap: ${mcap_t:.2f}T (Source: {source})",
        })

    # 合併手動固定追蹤的公司（若 CIK 不重複）
    for extra in EXTRA_WATCHLIST:
        cik = extra["cik"].lstrip("0").zfill(10)
        if cik not in seen_ciks:
            seen_ciks.add(cik)
            watchlist.append({**extra, "cik": cik})

    return watchlist


def is_excluded(form: str, description: str) -> bool:
    return (
        form.strip().upper() in EXCLUDE_FORMS
        or description.strip().upper() in EXCLUDE_DESCS
    )


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
    for i in range(min(SCAN_LIMIT, len(acc))):
        form = forms[i] if i < len(forms) else ""
        desc = descs[i] if i < len(descs) else ""

        if is_excluded(form, desc):
            continue

        acc_clean = acc[i].replace("-", "")
        doc       = docs[i] if i < len(docs) else ""
        filings.append({
            "accessionNumber": acc[i],
            "form":        form,
            "filingDate":  dates[i] if i < len(dates) else "",
            "description": desc,
            "primaryDocument": doc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc_clean}/{doc}",
            "indexUrl": (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK={cik}&type=&dateb=&owner=include&count=10"
            ),
        })

        if len(filings) >= MAX_FILINGS:
            break

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
        print("  [info] No cache release found — first run.")
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

    raw = r.json()
    if isinstance(raw, list):
        print("  [warn] Old list-format cache detected — discarding and starting fresh.")
        return {}

    return {cik: set(nums) for cik, nums in raw.items()}


def save_cache(cache: dict[str, list[str]], watchlist: list[dict]):
    """cache = { cik: [accessionNumbers] }"""
    content = json.dumps(cache, indent=2).encode()

    release = _get_release_by_tag(CACHE_TAG)
    if not release:
        companies = " | ".join(w["name"] for w in watchlist)
        release = _create_release(
            tag       = CACHE_TAG,
            name      = "SEC Filing Cache (do not delete)",
            body      = f"Auto-managed by SEC monitor (> $1T Market Cap). Current: {companies}",
            prerelease= True,
        )
        print("  [info] Created new sec-cache release.")

    for asset in release.get("assets", []):
        if asset["name"] == CACHE_ASSET:
            _delete_asset(asset["id"])

    _upload_asset(release["upload_url"], CACHE_ASSET, content)
    print("  [ok] Cache uploaded to GitHub Release.")


# ── Publish GitHub Release for new filings ────────────────────────────────────
def publish_filing_release(company: dict, new_filings: list[dict]) -> str:
    now        = datetime.now(timezone.utc)
    form_types = ", ".join(dict.fromkeys(f["form"] for f in new_filings))
    tag        = f"filing-{company['cik'].lstrip('0')}-{now.strftime('%Y%m%d-%H%M%S')}"
    name       = f"{company['name']} — New Filing: {form_types} ({now.strftime('%Y-%m-%d')})"

    lines = [f"## New SEC Filing(s) — {company['name']}\n"]
    if note := company.get("note"):
        lines.append(f"> {note}\n")
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
    print(f"  [ok] GitHub Release published: {rel['html_url']}")
    return rel["html_url"]


def write_github_summary(company: dict, new_filings: list[dict], release_url: str):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(f"## {company['name']} — {len(new_filings)} New Filing(s)\n\n")
        fh.write("| Form | Filed | Description | Link |\n|------|-------|-------------|------|\n")
        for f in new_filings:
            desc = f["description"] or f["primaryDocument"]
            fh.write(f"| `{f['form']}` | {f['filingDate']} | {desc} | [View]({f['url']}) |\n")
        fh.write(f"\n[GitHub Release]({release_url})\n\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(timezone.utc).isoformat()
    print(f"[{now_str}] Fetching companies with Market Cap > $1T...")

    try:
        watchlist = build_dynamic_watchlist(MARKET_CAP_THRESHOLD)
    except Exception as e:
        print(f"[fatal] Failed to build dynamic watchlist: {e}", file=sys.stderr)
        sys.exit(1)

    if not watchlist:
        print("[fatal] Watchlist is empty — aborting to protect cache.", file=sys.stderr)
        sys.exit(1)

    print(f"[ok] Tracking {len(watchlist)} companies (> $1T):")
    for w in watchlist:
        print(f"  - {w['name']} | CIK: {w['cik']} | {w.get('note', '')}")
    print()

    cache     = load_cache()
    # 保留既有快取紀錄，避免某公司市值短暫跌破 1T 又漲回時重複發送舊文件通知
    new_cache = {cik: list(nums) for cik, nums in cache.items()}
    any_error = False

    for company in watchlist:
        cik  = company["cik"]
        name = company["name"]
        print(f"── {name} ({cik})")

        try:
            filings = fetch_latest_filings(cik)
            time.sleep(0.15)  # 遵守 SEC EDGAR 每秒最多 10 次請求的限制
        except Exception as e:
            print(f"  [error] Failed to fetch: {e}", file=sys.stderr)
            any_error = True
            continue

        seen        = cache.get(cik, set())
        all_acc     = [f["accessionNumber"] for f in filings]
        new_filings = [f for f in filings if f["accessionNumber"] not in seen]

        new_cache[cik] = all_acc

        if not new_filings:
            print("  [ok] No new filings.")
            continue

        print(f"  [new] {len(new_filings)} new filing(s):")
        for f in new_filings:
            print(f"     - [{f['form']}] {f['filingDate']} — {f['description'] or f['primaryDocument']}")

        release_url = publish_filing_release(company, new_filings)
        write_github_summary(company, new_filings, release_url)
        print()

    save_cache(new_cache, watchlist)
    print("\n[ok] All done.")
    if any_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
