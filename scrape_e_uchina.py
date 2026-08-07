import asyncio
import re
import logging
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL    = "https://www.e-uchina.net"
import os
ID_START    = int(os.environ.get("ID_START", 5000))
ID_END      = int(os.environ.get("ID_END",   9000))
SLEEP_MS    = 1500
OUTPUT_FILE = "okinawa_fudosan_companies.csv"

COL_KAISHA  = "\u4f1a\u793e\u540d"
COL_JUSHO   = "\u6240\u5728\u5730"
COL_EIGYO   = "\u55b6\u696d\u6642\u9593"
COL_TEIKYUU = "\u5b9a\u4f11\u65e5"
COL_MENKYO  = "\u514d\u8a31"
COL_BIKO    = "\u5099\u8003_\u99d0\u8eca\u5834"

PROPERTY_TYPES = {
    "jukyo":   "\u8cc3\u8cb8_\u4f4f\u5c45\u7528",
    "jigyo":   "\u8cc3\u8cb8_\u4e8b\u696d\u7528",
    "yard":    "\u8cc3\u8cb8_\u571f\u5730",
    "parking": "\u8cc3\u8cb8_\u99d0\u8eca\u5834",
    "tochi":   "\u58f2\u8cb7_\u571f\u5730",
    "house":   "\u58f2\u8cb7_\u4e00\u6238\u5efa\u3066",
    "mansion": "\u58f2\u8cb7_\u30de\u30f3\u30b7\u30e7\u30f3",
    "sonota":  "\u58f2\u8cb7_\u305d\u306e\u4ed6",
}


def parse_property_counts(soup: BeautifulSoup, company_id: int) -> dict:
    counts = {col: "0" for col in PROPERTY_TYPES.values()}
    for type_key, col_name in PROPERTY_TYPES.items():
        link = soup.find("a", href=f"{BASE_URL}/fudosan_kaisha/{company_id}/{type_key}") or \
               soup.find("a", href=f"/fudosan_kaisha/{company_id}/{type_key}")
        if not link:
            continue
        text = link.get_text(strip=True)
        m = re.search(r"(\d+)\u4ef6", text)
        if m:
            counts[col_name] = m.group(1)
    return counts


def parse_info_table(soup: BeautifulSoup) -> dict:
    info: dict = {}
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        cell_texts = []
        for cell in cells:
            for a in cell.find_all("a"):
                if a.get_text(strip=True) in ["\u5730\u56f3", "map"]:
                    a.decompose()
            cell_texts.append(cell.get_text(strip=True))
        if len(cell_texts) >= 4:
            if cell_texts[0]: info[cell_texts[0]] = cell_texts[1]
            if cell_texts[2]: info[cell_texts[2]] = cell_texts[3]
        elif len(cell_texts) >= 2:
            if cell_texts[0]: info[cell_texts[0]] = cell_texts[1]
    return info


def is_company_page(soup: BeautifulSoup) -> bool:
    h1 = soup.find("h1")
    if not h1:
        return False
    text = h1.get_text(strip=True)
    return bool(text) and "\u3046\u3061\u306a\u30fc\u3089\u3044\u3075" not in text


async def fetch_company(page: Page, company_id: int) -> dict | None:
    url = f"{BASE_URL}/fudosan_kaisha/{company_id}"
    for attempt in range(1, 4):
        try:
            resp = await page.goto(url, wait_until="load", timeout=45000)
            if resp and resp.status == 404:
                return None
            try:
                await page.wait_for_selector(
                    f"a[href*='/fudosan_kaisha/{company_id}/']",
                    timeout=10000
                )
            except Exception:
                pass
            break
        except Exception as e:
            if attempt < 3:
                logger.warning(f"ID={company_id} retry {attempt}/3: {e}")
                await asyncio.sleep(3)
            else:
                logger.warning(f"ID={company_id} skip: {e}")
                return None

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")

    if not is_company_page(soup):
        return None

    data: dict = {"\u4f1a\u793e\u30a2\u30a4\u30c7\u30a3": company_id,
                  "\u4f1a\u793e\u30a2\u30a4\u30c7\u30a3": company_id}

    # 会社ID・URL
    data = {"会社ID": company_id, "会社URL": url}

    try:
        data[COL_KAISHA] = soup.find("h1").get_text(strip=True)
    except Exception:
        data[COL_KAISHA] = ""

    info = parse_info_table(soup)
    data[COL_JUSHO]   = info.get("\u4f4f\u6240", "")
    data["TEL"]       = info.get("TEL", "")
    data[COL_EIGYO]   = info.get("\u55b6\u696d\u6642\u9593", "")
    data[COL_TEIKYUU] = info.get("\u5b9a\u4f11\u65e5", "")
    data[COL_MENKYO]  = info.get("\u514d\u8a31", "")
    data["HP"]        = info.get("HP", "")
    data[COL_BIKO]    = info.get("\u5099\u8003", "")

    counts = parse_property_counts(soup, company_id)
    data.update(counts)

    await page.wait_for_timeout(SLEEP_MS)
    return data


async def main():
    logger.info("=== scraping start ===")
    logger.info(f"ID range: {ID_START} to {ID_END}")

    records = []
    total_range = ID_END - ID_START + 1

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        page = await context.new_page()

        try:
            for company_id in range(ID_START, ID_END + 1):
                elapsed = company_id - ID_START + 1
                if elapsed % 50 == 0:
                    logger.info(f"progress: {elapsed}/{total_range}  found: {len(records)}")

                record = await fetch_company(page, company_id)
                if record:
                    jukyo   = record.get(PROPERTY_TYPES["jukyo"], "0")
                    tochi   = record.get(PROPERTY_TYPES["tochi"], "0")
                    house   = record.get(PROPERTY_TYPES["house"], "0")
                    logger.info(f"  ID={company_id} [{record[COL_KAISHA]}] "
                                f"jukyo:{jukyo} tochi:{tochi} house:{house}")
                    records.append(record)
        finally:
            await context.close()
            await browser.close()

    if not records:
        logger.error("no data found")
        return

    cols = [
        "会社ID", COL_KAISHA, COL_JUSHO, "TEL", COL_EIGYO, COL_TEIKYUU,
        COL_MENKYO, "HP", COL_BIKO,
    ] + list(PROPERTY_TYPES.values()) + ["会社URL"]

    df = pd.DataFrame(records, columns=cols)
    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    logger.info(f"=== done: {len(df)} companies saved to {OUTPUT_FILE} ===")


if __name__ == "__main__":
    asyncio.run(main())
