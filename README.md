# Reddit Link Checker & Report Generator

A real-world Python automation tool that monitors Reddit posts and comments at scale — detecting removed, deleted, and purged content — and exports a colour-coded PDF report.

Built because Reddit blocked all unauthenticated API access in 2024. This tool bypasses that entirely using a real headless browser.

---

## What It Does

- **Reads URLs from Google Sheets** (column 1, any number of links)
- **Checks each link in parallel** using 5 headless browsers simultaneously
- **Detects all removal states** — including hard-purged comments that most tools miss
- **Exports a colour-coded PDF report** with status, date, upvote count, and summary stats

---

## Sample Output

> 📄 See [`sample_output.pdf`](sample_output.pdf) for a full example report.

| Colour | Meaning |
|--------|---------|
| 🟢 Green | Active Comment — Target Week |
| 🟡 Yellow | Active Post — Target Week |
| 🟣 Purple | Recent (after last Friday) |
| 🟠 Orange | Older (before last-to-last Friday) |
| 🔴 Red | Deleted / Removed |
| ⚫ Gray | Duplicate |

---

## Why Not Use the Reddit API?

Reddit disabled unauthenticated JSON access in 2024. Hitting `reddit.com/*.json` returns **403 Forbidden** for most endpoints regardless of headers.

This tool uses **Playwright** (headless Chromium) on `old.reddit.com` instead — which is server-side rendered and works reliably without any credentials.

### Two types of removed content (both detected)

| Type | What Reddit does | DOM result |
|------|-----------------|------------|
| **Soft-removed** | Keeps comment in DB, sets body = `[removed]` | `#thing_t1_id` exists, body = `[removed]` |
| **Hard-purged** | Wipes from DB entirely ("this comment no longer exists") | `#thing_t1_id` is completely absent from the page |

Most tools only catch soft-removed. This catches both.

---

## Tech Stack

- **Python 3.10+**
- **Playwright** — headless browser automation
- **gspread + google-auth** — Google Sheets integration
- **ReportLab** — PDF generation

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Google Sheets credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → New Project
2. Enable **Google Sheets API** and **Google Drive API**
3. Go to **IAM & Admin → Service Accounts → Create Service Account**
4. Under the service account → **Keys → Add Key → JSON** → download as `credentials.json`
5. Place `credentials.json` in the same folder as `reddit_report.py`
6. **Share your Google Sheet** with the service account email (Viewer access is enough)

> Full step-by-step with screenshots: see [`SETUP.md`](SETUP.md)

### 3. Configure the script

Open `reddit_report.py` and update the top section:

```python
CREDENTIALS_FILE = "credentials.json"    # path to your service account JSON
SHEET_ID         = "YOUR_SHEET_ID_HERE"  # ID or full URL of your Google Sheet
URL_COLUMN       = 1                     # column containing Reddit URLs (1 = col A)
MAX_WORKERS      = 5                     # parallel browsers — raise for faster runs
```

**Finding your Sheet ID:**
```
https://docs.google.com/spreadsheets/d/THIS_IS_YOUR_SHEET_ID/edit
```

### 4. Run

```bash
python reddit_report.py
```

The PDF is saved as `Reddit_Report_YYYYMMDD_HHMMSS.pdf` in the same folder.

---

## Google Sheet Format

Your sheet just needs Reddit URLs in column A — one per row. Headers and blank rows are skipped automatically.

```
https://www.reddit.com/r/Python/comments/abc123/comment/def456/
https://www.reddit.com/r/MachineLearning/comments/xyz789/
https://www.reddit.com/r/startups/comments/abc456/comment/ghi789/
```

---

## Speed Reference

| URLs | MAX_WORKERS | Approx. time |
|------|-------------|--------------|
| 50   | 5           | ~2 min |
| 100  | 5           | ~4 min |
| 200  | 10          | ~4 min |
| 500  | 15          | ~8 min |

---

## Project Structure

```
reddit_report.py   ← main script (single file, no extra modules needed)
requirements.txt   ← Python dependencies
SETUP.md           ← Google Sheets credentials walkthrough
sample_output.pdf  ← example PDF output
credentials.json   ← your service account file (not committed — add to .gitignore)
```

---

## .gitignore

Make sure you never commit your credentials:

```
credentials.json
*.pdf
__pycache__/
```

---

## License

MIT — free to use, fork, and adapt for your own monitoring workflows.
