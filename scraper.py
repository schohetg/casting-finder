"""
scraper.py - Web scrapers for casting sites
Supports: filmkidsplus.ch, studentfilm.ch, ronorp.net, encast.pro, swisscasting.ch
"""
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime, date
import logging
import hashlib
import json
import io

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_SUPPORT = True
except ImportError:
    DATEUTIL_SUPPORT = False

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'de-CH,de;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

REQUEST_TIMEOUT = 20


# ── Utility helpers ──────────────────────────────────────────────────────────

def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def extract_age_range(text: str):
    """
    Parse age information from German/English casting text.
    Returns (min_age, max_age) or (None, None) if not found.
    """
    text_lower = text.lower()
    current_year = datetime.now().year

    # "X bis Y Jahre" / "X-Y Jahre" / "X–Y Jahre"
    m = re.search(r'(\d{1,2})\s*(?:bis|[-–])\s*(\d{1,2})\s*(?:jahre|j\.?\b)', text_lower)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "Jahrgänge YYYY-YYYY" or "Jahrgang YYYY bis YYYY"
    m = re.search(r'jahrgän?ge?\s+(\d{4})\s*[-–bis]+\s*(\d{4})', text_lower)
    if m:
        y1, y2 = sorted([int(m.group(1)), int(m.group(2))])
        return current_year - y2, current_year - y1

    # "Jahrgang YYYY" (single year)
    m = re.search(r'jahrgang\s+(\d{4})', text_lower)
    if m:
        birth_year = int(m.group(1))
        age = current_year - birth_year
        return max(0, age - 1), age + 1

    # "X-jährig" / "Xjährig"
    m = re.search(r'(\d{1,2})\s*-?\s*jährig', text_lower)
    if m:
        age = int(m.group(1))
        return age, age

    # "X Jahre alt"
    m = re.search(r'(\d{1,2})\s+jahre?\s+alt', text_lower)
    if m:
        age = int(m.group(1))
        return age, age

    # "ab X Jahren"
    m = re.search(r'ab\s+(\d{1,2})\s*jahren?', text_lower)
    if m:
        return int(m.group(1)), 99

    # English: "age X to Y" / "X-Y years old" / "ages X-Y"
    m = re.search(r'age[sd]?\s*:?\s*(\d{1,2})\s*[-–to]+\s*(\d{1,2})', text_lower)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r'(\d{1,2})\s*[-–]\s*(\d{1,2})\s+years?\s*old', text_lower)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r'(\d{1,2})\s+years?\s+old', text_lower)
    if m:
        age = int(m.group(1))
        return age, age

    return None, None


def extract_gender(text: str) -> str:
    """Returns 'female', 'male', or 'any'."""
    text_lower = text.lower()

    female_kw = [
        'weiblich', 'mädchen', 'girls?\\b', 'female', 'frau', 'frauen',
        'schauspielerin', 'darstellerin', 'tochter', 'schwester'
    ]
    male_kw = [
        'männlich', 'junge[n]?\\b', 'knabe', 'boys?\\b', 'male',
        'mann', 'männer', 'schauspieler\\b', 'darsteller\\b', 'sohn', 'bruder'
    ]

    has_female = any(re.search(kw, text_lower) for kw in female_kw)
    has_male = any(re.search(kw, text_lower) for kw in male_kw)

    if has_female and not has_male:
        return 'female'
    if has_male and not has_female:
        return 'male'
    return 'any'


GERMAN_MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
    'jan': 1, 'feb': 2, 'mär': 3, 'apr': 4,
    'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9,
    'okt': 10, 'nov': 11, 'dez': 12,
}


def extract_deadline(text: str):
    """Try to extract a deadline from text. Returns ISO date string or None."""
    # DD.MM.YYYY
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', text)
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if d >= date.today():
                return d.isoformat()
        except Exception:
            pass

    # DD. MonthName YYYY (German)
    text_lower = text.lower()
    for month_name, month_num in GERMAN_MONTHS.items():
        pattern = rf'(\d{{1,2}})\.\s*{re.escape(month_name)}\s*(\d{{4}})'
        m = re.search(pattern, text_lower)
        if m:
            try:
                d = date(int(m.group(2)), month_num, int(m.group(1)))
                if d >= date.today():
                    return d.isoformat()
            except Exception:
                pass

    # Look for deadline context keywords then try to parse
    deadline_pattern = r'(?:bewerbung|einsendung|deadline|bewerbungsschluss|frist|bis zum?|until)\s*:?\s*(.{5,40})'
    m = re.search(deadline_pattern, text_lower)
    if m and DATEUTIL_SUPPORT:
        snippet = m.group(1).strip()
        try:
            d = dateutil_parser.parse(snippet, dayfirst=True, fuzzy=True)
            if d.date() >= date.today():
                return d.date().isoformat()
        except Exception:
            pass

    return None


def fetch_pdf_text(pdf_url: str, base_url: str = '') -> str:
    """Download a PDF and extract its text. Returns '' on failure."""
    if not PDF_SUPPORT:
        return ''
    try:
        from urllib.parse import urlparse, urljoin
        pdf_url = urljoin(base_url, pdf_url)

        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()

        pdf_bytes = io.BytesIO(resp.content)
        pages = []
        with pdfplumber.open(pdf_bytes) as pdf:
            for page in pdf.pages[:10]:  # max 10 pages
                text = page.extract_text()
                if text:
                    pages.append(text)
        return '\n'.join(pages)
    except Exception as e:
        logger.debug(f"PDF fetch failed {pdf_url}: {e}")
        return ''


def find_pdf_links(soup: BeautifulSoup, base_url: str) -> list:
    from urllib.parse import urljoin
    pdf_links = soup.find_all('a', href=lambda h: h and h.lower().endswith('.pdf'))
    return [urljoin(base_url, a['href']) for a in pdf_links]


# ── Base scraper ──────────────────────────────────────────────────────────────

class BaseScraper:
    name = 'base'
    country = 'CH'

    def __init__(self, settings: dict):
        self.settings = settings
        self.actor_age = int(settings.get('actor_age', 14))

    def scrape(self) -> list:
        raise NotImplementedError

    def is_relevant(self, casting: dict) -> tuple:
        """Returns (bool, reason_string)."""
        age_min = casting.get('age_min')
        age_max = casting.get('age_max')
        if age_min is not None and age_max is not None:
            if self.actor_age < age_min or self.actor_age > age_max:
                return False, f"Age {self.actor_age} not in {age_min}-{age_max}"

        if casting.get('gender') == 'female':
            return False, "Female-only casting"

        deadline = casting.get('deadline')
        if deadline and deadline < date.today().isoformat():
            return False, f"Expired ({deadline})"

        return True, "OK"

    def _enrich(self, casting: dict) -> dict:
        """Fetch detail page + PDFs, add parsed fields."""
        url = casting.get('source_url', '')
        description = casting.get('description', '')
        pdf_urls = []
        pdf_content = ''

        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            detail = BeautifulSoup(resp.text, 'lxml')

            # Find main content block (generic heuristics)
            content_el = (
                detail.find('div', class_=re.compile(r'content|entry|article|post-body|description', re.I)) or
                detail.find('article') or
                detail.find('main')
            )
            if content_el:
                description = content_el.get_text(separator=' ', strip=True)

            pdf_urls = find_pdf_links(detail, url)
            for pu in pdf_urls[:3]:  # limit PDF reads
                txt = fetch_pdf_text(pu, url)
                if txt:
                    pdf_content += f'\n--- PDF: {pu} ---\n{txt}'

        except Exception as e:
            logger.debug(f"Enrichment failed for {url}: {e}")

        full_text = f"{casting.get('title', '')} {description} {pdf_content}"
        casting['description'] = description
        casting['pdf_urls'] = pdf_urls
        casting['pdf_content'] = pdf_content
        casting['age_min'], casting['age_max'] = extract_age_range(full_text)
        casting['gender'] = extract_gender(full_text)
        casting['deadline'] = extract_deadline(full_text)
        return casting


# ── filmkidsplus.ch ───────────────────────────────────────────────────────────

class FilmKidsPlusScraper(BaseScraper):
    name = 'filmkidsplus.ch'
    BASE_URL = 'https://www.filmkidsplus.ch'
    URLS = [
        'https://www.filmkidsplus.ch/filmwissen/casting/casting-news/',
        'https://www.filmkidsplus.ch/casting/',
    ]

    def scrape(self) -> list:
        castings = []
        for url in self.URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                # Try multiple selectors for post listings
                articles = (
                    soup.find_all('article') or
                    soup.find_all('div', class_=re.compile(r'post|entry|item', re.I))
                )

                for article in articles[:20]:
                    try:
                        link_el = article.find('a')
                        if not link_el:
                            continue
                        link = link_el.get('href', '')
                        if not link:
                            continue
                        if link.startswith('/'):
                            link = self.BASE_URL + link

                        title_el = article.find(re.compile(r'^h[1-4]$'))
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)

                        if not title or len(title) < 4:
                            continue

                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': article.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"filmkidsplus article parse error: {e}")

                if castings:
                    break
            except Exception as e:
                logger.warning(f"filmkidsplus.ch scrape error ({url}): {e}")

        return castings


# ── studentfilm.ch ────────────────────────────────────────────────────────────

class StudentFilmScraper(BaseScraper):
    name = 'studentfilm.ch'
    BASE_URL = 'https://studentfilm.ch'
    URLS = [
        'https://studentfilm.ch/?s=casting',
        'https://studentfilm.ch/category/casting/',
        'https://studentfilm.ch/',
    ]

    def scrape(self) -> list:
        castings = []
        for url in self.URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                articles = soup.find_all(['article', 'div'], class_=re.compile(r'post|entry', re.I))

                for article in articles[:20]:
                    try:
                        title_el = article.find(re.compile(r'^h[1-4]$'))
                        if not title_el:
                            continue
                        title = title_el.get_text(strip=True)

                        link_el = title_el.find('a') or article.find('a')
                        if not link_el:
                            continue
                        link = link_el.get('href', '')
                        if not link or not link.startswith('http'):
                            continue

                        # Only include casting-related posts
                        combined = (title + ' ' + article.get_text()).lower()
                        if not any(kw in combined for kw in ['casting', 'schauspieler', 'darsteller', 'rolle', 'audition']):
                            continue

                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': article.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"studentfilm article error: {e}")

                if castings:
                    break
            except Exception as e:
                logger.warning(f"studentfilm.ch error ({url}): {e}")

        return castings


# ── ronorp.net ────────────────────────────────────────────────────────────────

class RonorpScraper(BaseScraper):
    name = 'ronorp.net'
    REGIONS = ['zuerich', 'bern', 'basel', 'luzern', 'winterthur', 'stgallen']
    BASE_URL = 'https://www.ronorp.net'

    def scrape(self) -> list:
        castings = []
        seen_ids = set()

        for region in self.REGIONS:
            url = f'https://www.ronorp.net/{region}/jobs/film-castings.1284'
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                # Ronorp uses a table / list layout for classifieds
                items = (
                    soup.find_all('div', class_=re.compile(r'ad|listing|item|announce', re.I)) or
                    soup.find_all('li', class_=re.compile(r'ad|listing|item', re.I)) or
                    soup.find_all('tr')
                )

                for item in items[:30]:
                    try:
                        link_el = item.find('a', href=lambda h: h and '/jobs/' in h or '/kleinanzeigen/' in h or '/anzeige/' in h)
                        if not link_el:
                            link_el = item.find('a')
                        if not link_el:
                            continue

                        link = link_el.get('href', '')
                        if link.startswith('/'):
                            link = self.BASE_URL + link
                        if not link.startswith('http'):
                            continue

                        cast_id = make_id(link)
                        if cast_id in seen_ids:
                            continue
                        seen_ids.add(cast_id)

                        title = link_el.get_text(strip=True)
                        if len(title) < 4:
                            title_el = item.find(re.compile(r'^h[1-5]$')) or item.find('strong')
                            if title_el:
                                title = title_el.get_text(strip=True)

                        if not title:
                            continue

                        # Quick filter: must mention casting/schauspieler etc
                        item_text = item.get_text().lower()
                        if not any(kw in item_text for kw in ['casting', 'schauspieler', 'darsteller', 'film', 'rolle', 'audition']):
                            if not any(kw in title.lower() for kw in ['casting', 'schauspieler', 'film']):
                                continue

                        casting = {
                            'id': cast_id,
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': item.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"ronorp item error: {e}")

            except Exception as e:
                logger.warning(f"ronorp.net error ({region}): {e}")

        return castings


# ── encast.pro ────────────────────────────────────────────────────────────────

class EnCastScraper(BaseScraper):
    name = 'encast.pro'
    BASE_URL = 'https://www.encast.pro'
    URLS = [
        'https://www.encast.pro/castings?cntry=CH',
        'https://www.encast.pro/castings?country=CH',
        'https://www.encast.pro/castings?cntry=Switzerland',
    ]

    def scrape(self) -> list:
        castings = []
        for url in self.URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                items = (
                    soup.find_all('div', class_=re.compile(r'casting|card|item|listing', re.I)) or
                    soup.find_all('article')
                )

                for item in items[:20]:
                    try:
                        link_el = item.find('a')
                        if not link_el:
                            continue
                        link = link_el.get('href', '')
                        if link.startswith('/'):
                            link = self.BASE_URL + link
                        if not link.startswith('http'):
                            continue

                        title_el = item.find(re.compile(r'^h[1-4]$'))
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
                        if not title or len(title) < 4:
                            continue

                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': item.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"encast item error: {e}")

                if castings:
                    break
            except Exception as e:
                logger.warning(f"encast.pro error ({url}): {e}")

        return castings


# ── SwissCasting.ch ───────────────────────────────────────────────────────────

class SwissCastingScraper(BaseScraper):
    name = 'swisscasting.ch'
    BASE_URL = 'https://new.swisscasting.ch'
    URLS = [
        'https://new.swisscasting.ch/castings',
        'https://new.swisscasting.ch/casting',
        'https://new.swisscasting.ch/',
        'https://www.swisscasting.ch/',
    ]

    def scrape(self) -> list:
        castings = []
        for url in self.URLS:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                items = (
                    soup.find_all('div', class_=re.compile(r'casting|card|item|job|listing', re.I)) or
                    soup.find_all('article')
                )

                for item in items[:20]:
                    try:
                        link_el = item.find('a')
                        if not link_el:
                            continue
                        link = link_el.get('href', '')
                        if link.startswith('/'):
                            link = self.BASE_URL + link
                        if not link.startswith('http'):
                            continue

                        title_el = item.find(re.compile(r'^h[1-4]$'))
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
                        if not title or len(title) < 4:
                            continue

                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': item.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"swisscasting item error: {e}")

                if castings:
                    break
            except Exception as e:
                logger.debug(f"swisscasting.ch error ({url}): {e}")

        return castings


# ── Germany & UK scrapers (future / e-casting only) ───────────────────────────

class CastingNetworkDEScraper(BaseScraper):
    """casting-network.de - requires account for full access, scrapes public listings."""
    name = 'casting-network.de'
    BASE_URL = 'https://www.casting-network.de'
    country = 'DE'

    def scrape(self) -> list:
        castings = []
        urls = [
            'https://www.casting-network.de/Open-Castings/castings.html',
            'https://www.casting-network.de/castings',
        ]
        for url in urls:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, 'lxml')

                items = soup.find_all('div', class_=re.compile(r'casting|item|card', re.I))
                for item in items[:20]:
                    try:
                        link_el = item.find('a')
                        if not link_el:
                            continue
                        link = link_el.get('href', '')
                        if link.startswith('/'):
                            link = self.BASE_URL + link
                        if not link.startswith('http'):
                            continue

                        title_el = item.find(re.compile(r'^h[1-4]$'))
                        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
                        if not title or len(title) < 4:
                            continue

                        item_text = item.get_text().lower()
                        # Only include if it mentions ecasting/online casting
                        if 'e-casting' not in item_text and 'online' not in item_text and 'self-tape' not in item_text:
                            if self.settings.get('de_ecast_only', 'true') == 'true':
                                continue

                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': item.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        castings.append(casting)
                    except Exception as e:
                        logger.debug(f"casting-network.de item error: {e}")

                if castings:
                    break
            except Exception as e:
                logger.warning(f"casting-network.de error: {e}")

        return castings


# ── Main runner ───────────────────────────────────────────────────────────────

SCRAPER_REGISTRY = {
    'filmkidsplus.ch': FilmKidsPlusScraper,
    'studentfilm.ch': StudentFilmScraper,
    'ronorp.net': RonorpScraper,
    'encast.pro': EnCastScraper,
    'swisscasting.ch': SwissCastingScraper,
    'casting-network.de': CastingNetworkDEScraper,
}


def run_scrapers(settings: dict) -> tuple:
    """
    Run all enabled scrapers.
    Returns (list_of_castings, stats_dict)
    """
    enabled_sites = settings.get('enabled_sites', list(SCRAPER_REGISTRY.keys()))
    enabled_countries = settings.get('enabled_countries', ['CH'])

    stats = {'scraped': 0, 'relevant': 0, 'filtered': 0, 'errors': []}
    all_castings = []
    seen_ids = set()

    for site_name in enabled_sites:
        scraper_cls = SCRAPER_REGISTRY.get(site_name)
        if not scraper_cls:
            logger.warning(f"No scraper found for site: {site_name}")
            continue

        scraper = scraper_cls(settings)

        # Skip scraper if its country is not enabled
        if scraper.country not in enabled_countries:
            continue

        logger.info(f"Scraping {site_name}...")
        try:
            castings = scraper.scrape()
            logger.info(f"  {site_name}: found {len(castings)} raw castings")

            for casting in castings:
                stats['scraped'] += 1
                cast_id = casting.get('id')

                if cast_id in seen_ids:
                    continue
                seen_ids.add(cast_id)

                relevant, reason = scraper.is_relevant(casting)
                if relevant:
                    all_castings.append(casting)
                    stats['relevant'] += 1
                else:
                    stats['filtered'] += 1
                    logger.debug(f"  Filtered '{casting.get('title', '?')}': {reason}")

        except Exception as e:
            msg = f"{site_name}: {e}"
            logger.error(f"Scraper error — {msg}")
            stats['errors'].append(msg)

    logger.info(
        f"Scan complete: {stats['scraped']} scraped, "
        f"{stats['relevant']} relevant, {stats['filtered']} filtered"
    )
    return all_castings, stats
