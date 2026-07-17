import asyncio
import re
import logging
import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL    = "https://www.e-uchina.net"
ID_START    = 5000
ID_END      = 9000
SLEEP_MS    = 1500
OUTPUT_FILE = "okinawa_fudosan_companies.csv"

# --- column names as unicode escapes to avoid encoding issues ---
COL_KAISHA   = "\u4f1a\u793e\u540d"        # 会社名
COL_JUSHO    = "\u6240\u5728\u5730"        # 所在地
COL_EIGYO    = "\u55b6\u696d\u6642\u9593"  # 営業時間
COL_TEIKYUU  = "\u5b9a\u4f11\u65e5"        # 定休日
COL_MENKYO   = "\u514d\u8a31"              # 免許
COL_BIKO     = "\u5099\u8003_\u99d0\u8eca\u5834"  # 備考_駐車場

PROPERTY_TYPES = {
    "jukyo":   "\u8cbc\u8cb8_\u4f4f\u5c45\u7528",   # 賃貸_住居用
    "jigyo":   "\u8cbc\u8cb8_\u4e8b\u696d\u7528",   # 賃貸_事業用
    "yard":    "\u8cbc\u8cb8_\u571f\u5730",           # 賃貸_土地
    "parking": "\u8cbc\u8cb8_\u99d0\u8eca\u5834",    # 賃貸_駐車場
    "tochi":   "\u58f2\u8cb7_\u571f\u5730",           # 売買_土地
    "house":   "\u58f2\u8cb7_\u4e00\u6238\u5efa\u3066", # 売買_一戸建て
    "mansion": "\u58f2\u8cb7_\u30de\u30f3\u30b7\u30e7\u30f3", # 売買_マンション
    "sonota":  "\u58f2\u8cb7_\u305d\u306e\u4ed6",    # 売買_その他
}

def parse_property_counts(soup, company_id):
    counts = {col: "0" for col in PROPERTY_TYPES.values()}
    for type_key, col_name in PROPERTY_TYPES.items():
        link = (
            soup.find("a", href=f"{BASE_URL}/fudosan_kaisha/{company_id}/{type_key}")
            or soup.find("a", href=f"/fudosan_kaisha/{company_id}/{type_key}")
        )
        if not link:
            continue
        text = link.get_text(strip=True)
        # Extract last number in text e.g. "住居用88件" -> "88"
        m = re.findall(r"\d+", text)
        if m:
            counts[col_name] = m[-1]
    return counts

def parse_info_table(soup):
    info = {}
    CHIZU = "\u5730\u56f3"  # 地図
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        cell_texts = []
        for cell in cells:
            for a in cell.find_all("a"):
                if a.get_text(strip=True) in [CHIZU, "map"]:
                    a.decompose()
            cell_texts.append(cell.get_text(strip=True))
        if len(cell_texts) >= 4:
            if cell_texts[0]: info[cell_texts[0]] = cell_texts[1]
            if cell_texts[2]: info[cell_texts[2]] = cell_texts[3]
        elif len(cell_texts) >= 2:
            if cell_texts[0]: info[cell_texts[0]] = cell_texts[1]
    return info

def is_company_page(soup):
    h1 = soup.find("h1")
    if not h1:
        return False
    text = h1.get_text(strip=True)
    SITE = "\u3046\u3061\u306a\u30fc\u3089\u3044\u3075"  # うちなーらいふ
    return bool(text) and SITE not in text

async def fetch_company(page, company_id):
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
        except PlaywrightTimeout:
            if attempt < 3:
                logger.warning(f"ID={company_id} timeout ({attempt}/3), retrying...")
                await page.wait_for_timeout(3000)
            else:
                logger.warning(f"ID={company_id} skip after 3 timeouts")
                return None
        except Exception as e:
            logger.warning(f"ID={company_id} error: {e}")
            return None

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")
    if not is_company_page(soup):
        return None

    data = {"ID": company_id, "URL": url}
    try:
        data[COL_KAISHA] = soup.find("h1").get_text(strip=True)
    except:
        data[COL_KAISHA] = ""

    JUSHO  = "\u4f4f\u6240"
    EIGYO  = "\u55b6\u696d\u6642\u9593"
    TEIKYUU= "\u5b9a\u4f11\u65e5"
    MENKYO = "\u514d\u8a31"
    BIKO   = "\u5099\u8003"

    info = parse_info_table(soup)
    data[COL_JUSHO]   = info.get(JUSHO, "")
    data["TEL"]       = info.get("TEL", "")
    data[COL_EIGYO]   = info.get(EIGYO, "")
    data[COL_TEIKYUU] = info.get(TEIKYUU, "")
    data[COL_MENKYO]  = info.get(MENKYO, "")
    data["HP"]        = info.get("HP", "")
    data[COL_BIKO]    = info.get(BIKO, "")

    counts = parse_property_counts(soup, company_id)
    data.update(counts)
    await page.wait_for_timeout(SLEEP_MS)
    return data

async def main():
    logger.info("=== scraping start ===")
    logger.info(f"ID range: {ID_START} to {ID_END}")
    records = []
    total = ID_END - ID_START + 1

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

        for company_id in range(ID_START, ID_END + 1):
            elapsed = company_id - ID_START + 1
            if elapsed % 50 == 0:
                logger.info(f"progress: {elapsed}/{total}  found: {len(records)}")

            record = await fetch_company(page, company_id)
            if record:
                name    = record.get(COL_KAISHA, "")
                jukyo   = record.get(PROPERTY_TYPES["jukyo"], "0")
                jigyo   = record.get(PROPERTY_TYPES["jigyo"], "0")
                yard    = record.get(PROPERTY_TYPES["yard"], "0")
                parking = record.get(PROPERTY_TYPES["parking"], "0")
                tochi   = record.get(PROPERTY_TYPES["tochi"], "0")
                house   = record.get(PROPERTY_TYPES["house"], "0")
                mansion = record.get(PROPERTY_TYPES["mansion"], "0")
                sonota  = record.get(PROPERTY_TYPES["sonota"], "0")
                logger.info(
                    f"  ID={company_id} [{name}] "
                    f"\u4f4f\u5c45:{jukyo} \u4e8b\u696d:{jigyo} \u571f\u5730:{yard} \u99d0\u8eca:{parking} "
                    f"\u58f2\u571f:{tochi} \u4e00\u6238:{house} mansion:{mansion} \u4ed6:{sonota}"
                )
                records.append(record)

        await browser.close()

    if not records:
        logger.error("no data found")
        return

    COLUMNS = [
        "ID", COL_KAISHA, COL_JUSHO, "TEL", COL_EIGYO, COL_TEIKYUU,
        COL_MENKYO, "HP", COL_BIKO,
        PROPERTY_TYPES["jukyo"],  PROPERTY_TYPES["jigyo"],
        PROPERTY_TYPES["yard"],   PROPERTY_TYPES["parking"],
        PROPERTY_TYPES["tochi"],  PROPERTY_TYPES["house"],
        PROPERTY_TYPES["mansion"],PROPERTY_TYPES["sonota"],
        "URL",
    ]
    df = pd.DataFrame(records)
    df = df.reindex(columns=[c for c in COLUMNS if c in df.columns])
    df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    logger.info(f"=== done: {len(df)} records -> {OUTPUT_FILE} ===")

    print("\n=== total property counts ===")
    for key, col in PROPERTY_TYPES.items():
        if col in df.columns:
            print(f"  {col}: {df[col].astype(int).sum():,}")

if __name__ == "__main__":
    asyncio.run(main())
