# Setup Guide — Reddit Report Generator

## 1. Install Python packages
```bash
pip install playwright gspread google-auth reportlab
playwright install chromium
```

## 2. Google Sheets credentials (one-time)

**Step 1 — Create a Google Cloud project:**
- Go to https://console.cloud.google.com
- Click "New Project", give it any name, click Create

**Step 2 — Enable APIs:**
- In the project, go to "APIs & Services" → "Enable APIs"
- Search and enable: **Google Sheets API**
- Search and enable: **Google Drive API**

**Step 3 — Create a service account:**
- Go to "IAM & Admin" → "Service Accounts" → "Create Service Account"
- Give it any name (e.g. "reddit-report")
- Skip optional steps, click Done
- Click on the service account → "Keys" tab → "Add Key" → "JSON"
- A `credentials.json` file downloads automatically
- **Move it to the same folder as `reddit_report.py`**

**Step 4 — Share your Google Sheet:**
- Open your Google Sheet
- Click Share (top right)
- Paste the service account email (looks like `reddit-report@project-id.iam.gserviceaccount.com`)
- Set access to **Viewer** → Share

## 3. Configure the script

Open `reddit_report.py` and edit the top section:

```python
CREDENTIALS_FILE = "credentials.json"    # ← already set if file is in same folder
SHEET_ID         = "YOUR_SHEET_ID_HERE"  # ← replace with your sheet's ID
URL_COLUMN       = 1                     # ← column with Reddit links (1 = col A)
MAX_WORKERS      = 5                     # ← increase for faster runs (10 is safe)
```

**How to find your Sheet ID:**
Your Google Sheet URL looks like:
`https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit`
The Sheet ID is the long string between `/d/` and `/edit`:
`1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`

## 4. Run it
```bash
python reddit_report.py
```

The PDF will be saved as `Reddit_Report_YYYYMMDD_HHMMSS.pdf` in the same folder.

---

## Speed tuning

| URLs  | MAX_WORKERS | Approx. time |
|-------|-------------|---------------|
| 50    | 5           | ~2 min        |
| 100   | 5           | ~4 min        |
| 200   | 10          | ~4 min        |
| 500   | 15          | ~8 min        |

Increase `MAX_WORKERS` for faster runs — Reddit rarely rate-limits browser sessions.