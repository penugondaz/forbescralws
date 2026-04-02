import os
import json
import smtplib
import hashlib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path

# -- Config ------------------------------------------------------------------
PAGES = {
    "Regional Economic Outlook (REO)": "https://www.imf.org/en/publications/reo",
    "World Economic Outlook (WEO)":    "https://www.imf.org/en/publications/weo",
    "Global Financial Stability Report (GFSR)": "https://www.imf.org/en/publications/gfsr",
    "Fiscal Monitor (FM)":             "https://www.imf.org/en/publications/fm",
    "External Sector Reports":         "https://www.imf.org/en/publications/sprolls/external-sector-reports",
}

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_APP_PASS = os.environ["GMAIL_APP_PASSWORD"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IMFCrawler/1.0)"
}

# -- Helpers -----------------------------------------------------------------

def snapshot_path(name: str) -> Path:
    safe = name.replace(" ", "_").replace("/", "-").replace("(", "").replace(")", "")
    return SNAPSHOT_DIR / f"{safe}.json"


def fetch_page(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def extract_publications(html: str, url: str) -> list:
    """
    Extract publication items from an IMF publications page.
    Tries multiple selectors to be robust across page layouts.
    Returns a list of dicts with keys: title, date, url.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Strategy 1: list items with article links (used on WEO, GFSR, FM, ESR pages)
    for item in soup.select("li.imf-item, div.imf-item, article"):
        a = item.find("a", href=True)
        title = a.get_text(strip=True) if a else item.get_text(strip=True)[:120]
        href  = a["href"] if a else ""
        date_tag = item.find(class_=lambda c: c and "date" in c.lower()) if item else None
        date  = date_tag.get_text(strip=True) if date_tag else ""
        if title and len(title) > 5:
            results.append({"title": title, "date": date, "url": href})

    if results:
        return results

    # Strategy 2: all <a> tags inside publication listing containers
    for container in soup.select(".publications-list, #publications-list, .pub-list, main"):
        for a in container.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) > 15 and "/issues/" in a["href"]:
                results.append({"title": text, "date": "", "url": a["href"]})

    if results:
        return results

    # Strategy 3: hash the meaningful text content as a fallback
    main = soup.find("main") or soup.find("body")
    content = main.get_text(separator=" ", strip=True) if main else soup.get_text()
    digest = hashlib.sha256(content.encode()).hexdigest()
    return [{"title": f"__content_hash_{digest}", "date": "", "url": url}]


def load_snapshot(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_snapshot(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def compute_fingerprint(items: list) -> str:
    """Stable fingerprint of a publications list."""
    canonical = json.dumps(
        sorted(items, key=lambda x: (x.get("title", ""), x.get("url", ""))),
        sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def diff_items(old: list, new: list) -> list:
    old_urls   = {i["url"] for i in old}
    old_titles = {i["title"] for i in old}
    added = []
    for item in new:
        if item["url"] not in old_urls and item["title"] not in old_titles:
            added.append(item)
    return added


def get_recipients() -> list:
    """
    Read NOTIFY_EMAIL from environment.
    Supports a single address or a comma-separated list.
    Returns a list of validated email strings.
    """
    raw = os.environ.get("NOTIFY_EMAIL", "")
    recipients = [r.strip() for r in raw.split(",") if r.strip() and "@" in r.strip()]
    if not recipients:
        raise ValueError(f"NOTIFY_EMAIL has no valid addresses: {repr(raw)}")
    return recipients


def send_email(subject: str, html_body: str):
    recipients = get_recipients()
    print(f"   Sending to: {recipients}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())


def build_email(changes: dict) -> str:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rows = ""
    for page_name, items in changes.items():
        rows += f"<h3 style='color:#003087;margin-bottom:4px'>{page_name}</h3><ul>"
        for item in items:
            link = item["url"]
            if link and not link.startswith("http"):
                link = "https://www.imf.org" + link
            date_str = f" <small style='color:#666'>({item['date']})</small>" if item.get("date") else ""
            rows += f"<li><a href='{link}'>{item['title']}</a>{date_str}</li>"
        rows += "</ul>"

    return f"""
    <html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto'>
    <h2 style='background:#003087;color:white;padding:12px 16px;border-radius:6px'>
        New IMF Reports Detected - {today}
    </h2>
    {rows}
    <hr/>
    <p style='color:#888;font-size:12px'>
        Sent by your IMF crawler running on GitHub Actions.<br>
        Repo: https://github.com/penugondaz/forbescralws
    </p>
    </body></html>
    """


# -- Main --------------------------------------------------------------------

def main():
    all_changes = {}

    for page_name, url in PAGES.items():
        print(f"\nCrawling: {page_name}")
        try:
            html  = fetch_page(url)
            items = extract_publications(html, url)
            print(f"   Found {len(items)} item(s)")

            spath    = snapshot_path(page_name)
            snapshot = load_snapshot(spath)

            if snapshot is None:
                # First run - save baseline, no alert
                print("   No snapshot yet - saving baseline.")
                save_snapshot(spath, {"fingerprint": compute_fingerprint(items), "items": items})
                continue

            old_items = snapshot.get("items", [])
            new_fp    = compute_fingerprint(items)
            old_fp    = snapshot.get("fingerprint", "")

            if new_fp == old_fp:
                print("   No change detected.")
            else:
                added = diff_items(old_items, items)
                print(f"   Change detected! {len(added)} new item(s).")
                if added:
                    all_changes[page_name] = added
                elif items:
                    # Content changed but cannot identify specific new items
                    all_changes[page_name] = [{
                        "title": "Page content has changed - possible new publication",
                        "date": "",
                        "url": url
                    }]

                # Update snapshot
                save_snapshot(spath, {"fingerprint": new_fp, "items": items})

        except Exception as e:
            print(f"   Error: {e}")

    if all_changes:
        print(f"\nSending email alert for {len(all_changes)} page(s)...")
        subject = f"New IMF Reports - {', '.join(all_changes.keys())}"
        send_email(subject, build_email(all_changes))
        print("   Email sent!")
    else:
        print("\nNo changes - no email sent.")


if __name__ == "__main__":
    main()
