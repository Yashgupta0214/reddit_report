#!/usr/bin/env python3
"""
Reddit Link Check Report Generator
────────────────────────────────────────────────────────────────
Reads URLs from Google Sheets (col 1), checks status with 5 parallel
headless browsers, outputs a colour-coded PDF matching your layout.

One-time setup:
  pip install playwright gspread google-auth reportlab
  playwright install chromium

Google Sheets setup (see SETUP.md for screenshots):
  1. console.cloud.google.com → New Project
  2. Enable "Google Sheets API" + "Google Drive API"
  3. IAM → Service Accounts → Create → download credentials.json
  4. Share your Google Sheet with the service-account email (Viewer is enough)

Usage:
  python reddit_report.py
"""

import asyncio, sys, re
from datetime    import date, datetime, timedelta
from dataclasses import dataclass
from typing      import Optional
from urllib.parse import urlparse

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Playwright
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ReportLab
from reportlab.lib            import colors
from reportlab.lib.pagesizes  import A4
from reportlab.lib.units      import mm
from reportlab.lib.enums      import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.styles     import ParagraphStyle
from reportlab.platypus       import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer, HRFlowable, KeepTogether,
)

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION  — edit these before running
# ══════════════════════════════════════════════════════════════

CREDENTIALS_FILE = "service_account.json"     # path to your service-account JSON
SHEET_ID         = "1BAu_FNH2YSmxoLJw6lSfyNj0w03iO47leyJ_2n8L7pk"   # sheet ID or full URL
URL_COLUMN       = 1                      # column that holds the Reddit links

MAX_WORKERS      = 5                      # parallel browsers (increase for faster runs)
PAGE_TIMEOUT     = 30_000                 # ms per page load
ITEM_TIMEOUT     =  8_000                 # ms to wait for a DOM element

OUTPUT_PDF = f"Reddit_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

# ══════════════════════════════════════════════════════════════
#  COLOUR PALETTE  (matches your PDF layout)
# ══════════════════════════════════════════════════════════════

C_GREEN  = colors.HexColor("#C6EFCE")   # Active Comment — Target Week
C_YELLOW = colors.HexColor("#FFEB9C")   # Active Post    — Target Week
C_PURPLE = colors.HexColor("#DDD9FF")   # Recent         — After Last Friday
C_ORANGE = colors.HexColor("#FCE4D6")   # Older          — Before Last-to-Last Friday
C_RED    = colors.HexColor("#FFC7CE")   # Deleted / Removed
C_GRAY   = colors.HexColor("#D9D9D9")   # Duplicate
C_HEADER = colors.HexColor("#2D2D2D")   # Table header background

# ══════════════════════════════════════════════════════════════
#  DATA MODEL
# ══════════════════════════════════════════════════════════════

@dataclass
class Row:
    original_url : str
    clean_url    : str         = ""
    link_type    : str         = "COMMENT"   # COMMENT | POST
    status       : str         = "ERROR"     # ACTIVE | ACTIVE (Recent) | DELETED/MISSING | REMOVED | DELETED | ERROR
    note_date    : Optional[date] = None     # creation date fetched from Reddit
    upvotes      : int         = 0
    is_duplicate : bool        = False
    row_color    : object      = None
    error_msg    : str         = ""

# ══════════════════════════════════════════════════════════════
#  DATE HELPERS
# ══════════════════════════════════════════════════════════════

def week_boundaries():
    """Return (last_friday, last_to_last_friday) relative to today."""
    today = date.today()
    days_back = (today.weekday() - 4) % 7 or 7   # days since last Friday
    last_fri      = today - timedelta(days=days_back)
    prev_last_fri = last_fri - timedelta(days=7)
    return last_fri, prev_last_fri

def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d/%m/%y") if d else ""

# ══════════════════════════════════════════════════════════════
#  URL HELPERS
# ══════════════════════════════════════════════════════════════

def bare_path(url: str) -> str:
    return urlparse(url).path.rstrip("/") + "/"

def to_old(url: str) -> str:
    return "https://old.reddit.com" + bare_path(url)

def to_www(url: str) -> str:
    return "https://www.reddit.com" + bare_path(url)

def is_comment(url: str) -> bool:
    return "/comment/" in url

def comment_id(url: str) -> str:
    return bare_path(url).rstrip("/").split("/")[-1]

# ══════════════════════════════════════════════════════════════
#  PLAYWRIGHT — ASYNC CHECKS
# ══════════════════════════════════════════════════════════════

async def parse_date(container, selector: str) -> Optional[date]:
    try:
        el = await container.query_selector(selector)
        if el:
            raw = await el.get_attribute("datetime") or ""
            if raw:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except Exception:
        pass
    return None

async def parse_score(el) -> int:
    if not el:
        return 0
    # Old Reddit: title attr is "3 points" or "3 point"
    for getter in [
        lambda: el.get_attribute("title"),
        lambda: el.inner_text(),
    ]:
        try:
            text = await getter()
            token = (text or "").split()[0].replace(",", "")
            return int(token)
        except Exception:
            pass
    return 0

async def check_comment(page, cid: str, row: Row):
    # Wait for page content (old Reddit is SSR so this is fast)
    try:
        await page.wait_for_selector(".commentarea, .sitetable", timeout=ITEM_TIMEOUT)
    except PWTimeout:
        row.status = "ERROR"; row.error_msg = "Page content not loaded"; return

    # Look for the specific comment element
    thing = None
    try:
        await page.wait_for_selector(f"#thing_t1_{cid}", timeout=ITEM_TIMEOUT)
        thing = await page.query_selector(f"#thing_t1_{cid}")
    except PWTimeout:
        pass

    # ── KEY FIX: page loaded but element absent → hard-purged = REMOVED ──
    if not thing:
        row.status = "DELETED/MISSING"
        return

    # Author
    author_el = await thing.query_selector("a.author")
    author    = ((await author_el.inner_text()).strip()) if author_el else ""

    # Body — try standard MD wrapper, fall back to any usertext
    body_el = (await thing.query_selector(".usertext-body .md") or
               await thing.query_selector(".usertext-body"))
    body    = ((await body_el.inner_text()).strip()) if body_el else ""

    # Date & score
    row.note_date = await parse_date(thing, "time")
    score_el      = await thing.query_selector(".score")
    row.upvotes   = await parse_score(score_el)

    classes = (await thing.get_attribute("class") or "").split()

    if body in ("[removed]", "[ removed]") or "removed" in classes:
        row.status = "DELETED/MISSING"
    elif body == "[deleted]" or author == "[deleted]" or "deleted" in classes:
        row.status = "DELETED/MISSING"
    elif not body:
        row.status = "DELETED/MISSING"
    else:
        row.status = "ACTIVE"

async def check_post(page, row: Row):
    title_el = await page.query_selector("a.title")
    if not title_el:
        row.status = "DELETED/MISSING"; return

    author_el = await page.query_selector(".top-matter a.author")
    author    = ((await author_el.inner_text()).strip()) if author_el else ""

    row.note_date = await parse_date(page, ".tagline time")

    for sel in [".score.unvoted", ".score.likes", ".score"]:
        score_el = await page.query_selector(sel)
        if score_el:
            row.upvotes = await parse_score(score_el); break

    selftext_el = await page.query_selector(".usertext-body .md")
    selftext    = ((await selftext_el.inner_text()).strip()) if selftext_el else ""

    if selftext == "[removed]":
        row.status = "REMOVED"
    elif selftext == "[deleted]" or author == "[deleted]":
        row.status = "DELETED"
    else:
        row.status = "ACTIVE"

async def check_url(url: str, sem: asyncio.Semaphore, ctx) -> Row:
    row = Row(original_url=url, clean_url=to_www(url))
    row.link_type = "COMMENT" if is_comment(url) else "POST"

    async with sem:
        page = await ctx.new_page()
        try:
            resp = await page.goto(to_old(url), wait_until="domcontentloaded",
                                   timeout=PAGE_TIMEOUT)
            if resp and resp.status == 404:
                row.status = "DELETED/MISSING"; return row

            # Bypass age / quarantine gate if present
            for sel in ["button:has-text('Yes')", "input[value='Yes']"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=2_500)
                    if btn:
                        await btn.click()
                        await page.wait_for_load_state("domcontentloaded"); break
                except Exception:
                    pass

            if row.link_type == "COMMENT":
                await check_comment(page, comment_id(url), row)
            else:
                await check_post(page, row)

        except PWTimeout:
            row.status = "ERROR"; row.error_msg = "Timeout"
        except Exception as e:
            row.status = "ERROR"; row.error_msg = str(e)[:100]
        finally:
            await page.close()
    return row

async def run_checks(urls: list[str]) -> list[Row]:
    ICONS = {
        "ACTIVE": "✅", "ACTIVE (Recent)": "🟣",
        "DELETED/MISSING": "🚫", "REMOVED": "🚫", "DELETED": "🗑️ ", "ERROR": "⚠️ ",
    }
    total   = len(urls)
    counter = {"n": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await ctx.add_cookies([
            {"name": "over18",   "value": "1",
             "domain": ".reddit.com", "path": "/"},
            {"name": "_options", "value": "%7B%22pref_quarantine_optin%22%3A%20true%7D",
             "domain": ".reddit.com", "path": "/"},
        ])

        sem = asyncio.Semaphore(MAX_WORKERS)

        async def tracked(url: str, idx: int):
            row = await check_url(url, sem, ctx)
            counter["n"] += 1
            icon = ICONS.get(row.status, "?")
            print(f"  [{counter['n']:>3}/{total}] {icon} {row.status:<22}  {row.clean_url[:60]}")
            return (idx, row)

        pairs    = await asyncio.gather(*[tracked(u, i) for i, u in enumerate(urls)])
        results  = [r for _, r in sorted(pairs)]     # restore original sheet order

        await ctx.close()
        await browser.close()

    return results

# ══════════════════════════════════════════════════════════════
#  POST-PROCESSING  — colours + duplicate detection
# ══════════════════════════════════════════════════════════════

def assign_colors(rows: list[Row]) -> list[Row]:
    last_fri, prev_fri = week_boundaries()

    seen: dict[str, bool] = {}
    for r in rows:
        if r.clean_url in seen:
            r.is_duplicate = True
        else:
            seen[r.clean_url] = True

    for r in rows:
        if r.is_duplicate:
            r.row_color = C_GRAY; continue

        s = r.status
        if s in ("DELETED/MISSING", "REMOVED", "DELETED"):
            r.row_color = C_RED; continue
        if s == "ERROR":
            r.row_color = C_ORANGE; continue

        # ACTIVE — colour by creation date
        d = r.note_date
        if d and d > last_fri:
            r.status    = "ACTIVE (Recent)"
            r.row_color = C_PURPLE
        elif d and d < prev_fri:
            r.row_color = C_ORANGE          # older than 2 weeks
        else:
            # within target week (or unknown date → default to target week)
            r.row_color = C_GREEN if r.link_type == "COMMENT" else C_YELLOW

    return rows

# ══════════════════════════════════════════════════════════════
#  GOOGLE SHEETS  — read URLs
# ══════════════════════════════════════════════════════════════

def read_sheet(creds_file: str, sheet_id: str) -> list[str]:
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    gc    = gspread.authorize(creds)
    ws    = (gc.open_by_url(sheet_id) if sheet_id.startswith("http")
             else gc.open_by_key(sheet_id)).sheet1

    raw  = ws.col_values(URL_COLUMN)
    urls = [v.strip() for v in raw if re.match(r"https?://(www\.)?reddit\.com", v.strip())]
    print(f"📋  {len(urls)} Reddit URLs read from Google Sheets")
    return urls

# ══════════════════════════════════════════════════════════════
#  PDF GENERATION
# ══════════════════════════════════════════════════════════════

def build_pdf(rows: list[Row], output_path: str):
    W, _ = A4
    MARGIN   = 15 * mm
    AVAIL_W  = W - 2 * MARGIN      # ≈ 510 pt

    # Column widths:  #   Type  Status  Note   Upv   URL
    COL_W = [18, 50, 92, 50, 28, AVAIL_W - 18 - 50 - 92 - 50 - 28]

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN)

    # ── Paragraph styles ──────────────────────────────────────
    def ps(name, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, **kw)

    S_TITLE  = ps("title",  fontName="Helvetica-Bold",   fontSize=18, textColor=C_HEADER, spaceAfter=2)
    S_SUB    = ps("sub",    fontName="Helvetica",         fontSize=9,  textColor=colors.HexColor("#555555"), spaceAfter=8)
    S_LGND_K = ps("lgk",    fontName="Helvetica-Bold",   fontSize=8,  textColor=C_HEADER)
    S_LGND_V = ps("lgv",    fontName="Helvetica",         fontSize=8,  textColor=C_HEADER)
    S_TH     = ps("th",     fontName="Helvetica-Bold",   fontSize=7,  textColor=colors.white, alignment=TA_CENTER)
    S_TD     = ps("td",     fontName="Helvetica",         fontSize=6.5, textColor=C_HEADER, wordWrap="CJK")
    S_TD_C   = ps("tdc",    fontName="Helvetica",         fontSize=6.5, textColor=C_HEADER, wordWrap="CJK", alignment=TA_CENTER)
    S_URL    = ps("url",    fontName="Helvetica",         fontSize=6,  textColor=colors.HexColor("#1155CC"), wordWrap="CJK")
    S_SUM_H  = ps("sumh",   fontName="Helvetica-Bold",   fontSize=11, textColor=C_HEADER, spaceBefore=12, spaceAfter=4)
    S_SUM_TD = ps("sumtd",  fontName="Helvetica",         fontSize=9,  textColor=C_HEADER)
    S_SUM_TH = ps("sumth",  fontName="Helvetica-Bold",   fontSize=9,  textColor=colors.white)
    S_NOTE   = ps("note",   fontName="Helvetica-Oblique", fontSize=8,  textColor=colors.HexColor("#666666"), spaceAfter=4)

    story = []

    # ── Title ─────────────────────────────────────────────────
    story.append(Paragraph("Reddit Link Check Report", S_TITLE))
    story.append(Paragraph(f"Generated: {date.today().strftime('%d %B %Y')}", S_SUB))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC")))
    story.append(Spacer(1, 5))

    # ── Color legend ──────────────────────────────────────────
    legend = [
        (C_GREEN,  "Green",  "Active Comment (Target Week)"),
        (C_YELLOW, "Yellow", "Active Post (Target Week)"),
        (C_PURPLE, "Purple", "Recent (After Last Friday)"),
        (C_ORANGE, "Orange", "Older (Before Last-to-Last Friday)"),
        (C_RED,    "Red",    "Deleted / Removed"),
        (C_GRAY,   "Gray",   "Duplicate"),
    ]
    lg_data = [
        ["", Paragraph(name, S_LGND_K), Paragraph(desc, S_LGND_V)]
        for _, name, desc in legend
    ]
    lg_table = Table(lg_data, colWidths=[11, 44, 200])
    lg_ts    = TableStyle([
        ("TOPPADDING",    (0,0),(-1,-1), 2),
        ("BOTTOMPADDING", (0,0),(-1,-1), 2),
        ("LEFTPADDING",   (0,0),(-1,-1), 3),
        ("RIGHTPADDING",  (0,0),(-1,-1), 3),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
    ])
    for i, (c, _, _) in enumerate(legend):
        lg_ts.add("BACKGROUND", (0,i),(0,i), c)
        lg_ts.add("BOX",        (0,i),(0,i), 0.4, colors.HexColor("#999999"))
    lg_table.setStyle(lg_ts)
    story.append(lg_table)
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC")))
    story.append(Spacer(1, 6))

    # ── Main data table ───────────────────────────────────────
    hdr = [Paragraph(h, S_TH) for h in ["#", "Type", "Status", "Note", "Upv", "URL"]]
    tbl_data   = [hdr]
    row_colors = []

    for i, r in enumerate(rows):
        ri   = i + 1      # 1-indexed row in table (header = row 0)
        note = "" if r.status in ("DELETED/MISSING","REMOVED","DELETED","ERROR") else fmt_date(r.note_date)
        upv  = str(r.upvotes) if r.upvotes else "0"

        tbl_data.append([
            Paragraph(str(i + 1),   S_TD_C),
            Paragraph(r.link_type,  S_TD_C),
            Paragraph(r.status,     S_TD),
            Paragraph(note,         S_TD_C),
            Paragraph(upv,          S_TD_C),
            Paragraph(r.clean_url,  S_URL),
        ])
        row_colors.append(("BACKGROUND", (0, ri), (-1, ri), r.row_color or colors.white))

    ts = TableStyle([
        # Header
        ("BACKGROUND",    (0,0),(-1, 0), C_HEADER),
        ("TEXTCOLOR",     (0,0),(-1, 0), colors.white),
        ("ALIGN",         (0,0),(-1, 0), "CENTER"),
        ("FONTNAME",      (0,0),(-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1, 0), 7),
        ("LINEBELOW",     (0,0),(-1, 0), 1.2, C_HEADER),
        # All cells
        ("FONTNAME",      (0,1),(-1,-1), "Helvetica"),
        ("FONTSIZE",      (0,1),(-1,-1), 6.5),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
        ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#BBBBBB")),
    ])
    for cmd in row_colors:
        ts.add(*cmd)

    main_tbl = Table(tbl_data, colWidths=COL_W, repeatRows=1)
    main_tbl.setStyle(ts)
    story.append(main_tbl)
    story.append(Spacer(1, 14))

    # ── Summary statistics ────────────────────────────────────
    last_fri, prev_fri = week_boundaries()

    counts = {
        "Active Comments (Target Week)": 0,
        "Active Posts (Target Week)"   : 0,
        "Recent Links (> Last Fri)"    : 0,
        "Older Links (< 2 Weeks)"      : 0,
        "Deleted / Removed"            : 0,
        "Errors"                       : 0,
        "Total Duplicates"             : 0,
    }
    for r in rows:
        if r.is_duplicate:
            counts["Total Duplicates"] += 1; continue
        s = r.status
        if   s in ("DELETED/MISSING","REMOVED","DELETED"): counts["Deleted / Removed"] += 1
        elif s == "ERROR":                                  counts["Errors"]            += 1
        elif s == "ACTIVE (Recent)":                        counts["Recent Links (> Last Fri)"] += 1
        elif s == "ACTIVE":
            d = r.note_date
            if d and d < prev_fri:
                counts["Older Links (< 2 Weeks)"] += 1
            elif r.link_type == "COMMENT":
                counts["Active Comments (Target Week)"] += 1
            else:
                counts["Active Posts (Target Week)"] += 1

    sum_hdr  = [Paragraph("Category",S_SUM_TH), Paragraph("Count",S_SUM_TH)]
    sum_data = [sum_hdr] + [
        [Paragraph(cat, S_SUM_TD),
         Paragraph(str(cnt), ps("cnt", fontName="Helvetica-Bold", fontSize=9,
                                textColor=C_HEADER, alignment=TA_CENTER))]
        for cat, cnt in counts.items()
    ]
    sum_tbl = Table(sum_data, colWidths=[190, 55])
    sum_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1, 0), C_HEADER),
        ("TEXTCOLOR",     (0,0),(-1, 0), colors.white),
        ("FONTNAME",      (0,0),(-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1, 0), 9),
        ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#BBBBBB")),
        ("TOPPADDING",    (0,0),(-1,-1), 4),
        ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ("LEFTPADDING",   (0,0),(-1,-1), 6),
        ("RIGHTPADDING",  (0,0),(-1,-1), 6),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("ALIGN",         (1,0),(1, -1), "CENTER"),
    ]))

    story.append(Paragraph("Summary Statistics", S_SUM_H))
    story.append(Paragraph("Note: (No Note)", S_NOTE))
    story.append(KeepTogether([sum_tbl]))

    doc.build(story)
    print(f"\n✅  PDF saved → {output_path}")

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("Reddit Link Check Report Generator")
    print("=" * 55)

    urls = read_sheet(CREDENTIALS_FILE, SHEET_ID)
    if not urls:
        print("❌  No Reddit URLs found in sheet column 1. Exiting.")
        sys.exit(1)

    rows = asyncio.run(run_checks(urls))
    rows = assign_colors(rows)
    build_pdf(rows, OUTPUT_PDF)

    active  = sum(1 for r in rows if "ACTIVE" in r.status and not r.is_duplicate)
    removed = sum(1 for r in rows if r.status in ("DELETED/MISSING","REMOVED","DELETED") and not r.is_duplicate)
    print(f"\n📊  {len(urls)} URLs  |  ✅ {active} active  |  🚫 {removed} removed/deleted")

if __name__ == "__main__":
    main()
