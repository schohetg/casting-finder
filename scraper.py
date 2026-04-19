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

    # Altersspanne / Altersangabe ohne Einheit (z.B. "18-40" im Titel oder nach Komma)
    # Matches bare "X-Y" or "X–Y" where both numbers are plausible ages (5–80)
    # Use word boundaries so "2024-04" (a year range) doesn't match
    for m in re.finditer(r'(?<!\d)(\d{1,2})\s*[-–]\s*(\d{1,2})(?!\d)', text_lower):
        a, b = int(m.group(1)), int(m.group(2))
        if 5 <= a <= 80 and 5 <= b <= 80 and a < b:
            return a, b

    # Single age mentioned near casting keywords
    for m in re.finditer(r'(?<!\d)(\d{1,2})(?!\d)', text_lower):
        age = int(m.group(1))
        if 5 <= age <= 80:
            # Only use if there is a casting keyword nearby (within 60 chars)
            start = max(0, m.start() - 60)
            end = min(len(text_lower), m.end() + 60)
            context = text_lower[start:end]
            casting_kw = ['actor', 'actress', 'schauspieler', 'darsteller', 'rolle', 'casting', 'audition']
            if any(kw in context for kw in casting_kw):
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


def extract_url_publish_date(url: str):
    """
    Extract the publication date from a WordPress-style URL like
    /2023/12/04/post-title/. Returns a date object or None.
    """
    m = re.search(r'/(\d{4})/(\d{1,2})/(\d{1,2})/', url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return None


def extract_deadline(text: str):
    """
    Try to extract a deadline from text. Returns ISO date string or None.
    NOTE: Returns the date even if it is in the past — the caller decides
    whether to filter it out. This way expired castings are flagged rather
    than silently shown with 'deadline unknown'.
    """
    # DD.MM.YYYY
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except Exception:
            pass

    # DD. MonthName YYYY (German)
    text_lower = text.lower()
    for month_name, month_num in GERMAN_MONTHS.items():
        pattern = rf'(\d{{1,2}})\.\s*{re.escape(month_name)}\s*(\d{{4}})'
        m = re.search(pattern, text_lower)
        if m:
            try:
                return date(int(m.group(2)), month_num, int(m.group(1))).isoformat()
            except Exception:
                pass

    # Look for deadline context keywords then try dateutil
    deadline_pattern = r'(?:bewerbung|einsendung|deadline|bewerbungsschluss|frist|bis zum?|until)\s*:?\s*(.{5,40})'
    m = re.search(deadline_pattern, text_lower)
    if m and DATEUTIL_SUPPORT:
        snippet = m.group(1).strip()
        try:
            d = dateutil_parser.parse(snippet, dayfirst=True, fuzzy=True)
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


# ── URL / title / content filters ────────────────────────────────────────────

# URL path segments that immediately disqualify a link
_BAD_URL_PATTERNS = re.compile(
    r'/(join|signup|register|login|kontakt|contact|impressum|datenschutz|'
    r'privacy|agb|terms|about|ueber-uns|team|newsletter|press|presse|'
    r'festival|award|news(?!/casting)|blog(?!/casting)|shop|'
    r'ticketing|veranstaltung|event(?!/casting)|stellenangebot(?!e/casting))|'
    r'CAST-PREMIUM|members/join|/user-beitraege/festivals',
    re.I
)

# The TITLE of a listing must contain at least one of these
_TITLE_CASTING_KW = re.compile(
    r'\b(casting|audition|rolle|role|gesucht|schauspieler|darsteller|actor|'
    r'actress|e-casting|self.?tape|selbstband|bewerbung|besetzung)\b',
    re.I
)

# The full page text must contain BOTH a primary AND a secondary marker
# to be accepted as a genuine open casting call.
_PRIMARY_KW = [
    'casting', 'schauspieler', 'schauspielerin', 'darsteller', 'darstellerin',
    'audition', 'actor', 'actress', 'rolle gesucht', 'role needed',
]
_SECONDARY_KW = [
    # "apply / send" signals
    'bewerb', 'apply', 'application', 'einsend', 'bewerbung an', 'bewerbung bis',
    'bewerbungsschluss', 'deadline', 'frist',
    # "we are looking" signals
    'gesucht', 'wir suchen', 'looking for', 'we are looking', 'we need',
    'we\'re looking', 'sought', 'seeking',
    # explicit open-call signals
    'open casting', 'offenes casting', 'open call', 'self-tape', 'selbstband',
    'e-casting', 'online casting',
]


def _is_open_casting(text: str) -> bool:
    """
    Two-stage check: text must contain a primary casting keyword AND
    at least one secondary 'apply / we are looking' marker.
    Festivals, membership pages, general articles etc. will typically
    fail the secondary check.
    """
    t = text.lower()
    has_primary   = any(kw in t for kw in _PRIMARY_KW)
    has_secondary = any(kw in t for kw in _SECONDARY_KW)
    return has_primary and has_secondary


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

    @staticmethod
    def _url_ok(url: str) -> bool:
        """Return False for URLs that are clearly not casting listings."""
        return not _BAD_URL_PATTERNS.search(url)

    @staticmethod
    def _title_ok(title: str) -> bool:
        """Return True only if the title looks like a casting listing."""
        return bool(_TITLE_CASTING_KW.search(title))

    def _enrich(self, casting: dict) -> dict:
        """
        Fetch detail page + PDFs, validate as open casting, add parsed fields.
        Returns None if the link is dead, irrelevant, or not an open casting.
        """
        url   = casting.get('source_url', '')
        title = casting.get('title', '')

        # ── Level 1: URL filter (no network request needed) ───────────────────
        if not self._url_ok(url):
            logger.debug(f"Bad URL pattern, skipping: {url}")
            return None

        # ── Level 2: Title filter (no network request needed) ─────────────────
        if not self._title_ok(title):
            logger.debug(f"Title not casting-like, skipping: {title!r}")
            return None

        description = casting.get('description', '')
        pdf_urls    = []
        pdf_content = ''
        page_ok     = False

        # ── Level 3: Fetch detail page ────────────────────────────────────────
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (404, 410):
                logger.debug(f"Dead link ({resp.status_code}): {url}")
                return None
            resp.raise_for_status()
            page_ok = True
            detail  = BeautifulSoup(resp.text, 'lxml')

            content_el = (
                detail.find('div', class_=re.compile(r'content|entry|article|post-body|description', re.I)) or
                detail.find('article') or
                detail.find('main')
            )
            description = (content_el or detail).get_text(separator=' ', strip=True)[:3000]

            pdf_urls = find_pdf_links(detail, url)
            for pu in pdf_urls[:3]:
                txt = fetch_pdf_text(pu, url)
                if txt:
                    pdf_content += f'\n--- PDF: {pu} ---\n{txt}'

        except Exception as e:
            logger.debug(f"Enrichment failed for {url}: {e}")

        if not page_ok and not description:
            return None

        full_text = f"{title} {description} {pdf_content}"

        # ── Level 4: Content must look like an open casting call ──────────────
        if not _is_open_casting(full_text):
            logger.debug(f"Not an open casting (failed content check): {title!r}")
            return None

        casting['description'] = description
        casting['pdf_urls']    = pdf_urls
        casting['pdf_content'] = pdf_content
        casting['age_min'], casting['age_max'] = extract_age_range(full_text)
        casting['gender']   = extract_gender(full_text)

        deadline = extract_deadline(full_text)

        # ── Fallback: use URL publication date if no deadline found ───────────
        # WordPress URLs like /2023/12/04/ reveal when the post was published.
        # Castings posted more than 3 months ago with no known future deadline
        # are almost certainly expired — mark them with the publish date so the
        # is_relevant() filter can reject them.
        if not deadline:
            pub_date = extract_url_publish_date(url)
            if pub_date:
                days_old = (date.today() - pub_date).days
                if days_old > 90:
                    deadline = pub_date.isoformat()
                    logger.debug(
                        f"Using URL publish date as expired deadline "
                        f"({pub_date}, {days_old}d old): {title!r}"
                    )

        casting['deadline'] = deadline
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
                        if casting:
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
                        if casting:
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
                        if casting:
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
                        if casting:
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
                        if casting:
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
                        casting = {
                            'id': make_id(link),
                            'title': title,
                            'source_url': link,
                            'source_site': self.name,
                            'country': self.country,
                            'description': item.get_text(separator=' ', strip=True),
                        }
                        casting = self._enrich(casting)
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"casting-network.de item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"casting-network.de error: {e}")
        return castings


# ── castforward.de ────────────────────────────────────────────────────────────

class CastForwardDEScraper(BaseScraper):
    """castforward.de — large German-language casting platform."""
    name = 'castforward.de'
    BASE_URL = 'https://www.castforward.de'
    country = 'DE'
    URLS = [
        'https://www.castforward.de/members/castings/',
        'https://www.castforward.de/casting/',
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
                    soup.find_all('div', class_=re.compile(r'casting|card|item|job', re.I)) or
                    soup.find_all('article')
                )
                for item in items[:25]:
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
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"castforward.de item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"castforward.de error ({url}): {e}")
        return castings


# ── castingcallpro.com (UK) ───────────────────────────────────────────────────

class CastingCallProScraper(BaseScraper):
    """castingcallpro.com — major UK casting platform with public listings."""
    name = 'castingcallpro.com'
    BASE_URL = 'https://www.castingcallpro.com'
    country = 'UK'
    URLS = [
        'https://www.castingcallpro.com/castings',
        'https://www.castingcallpro.com/uk/castings',
        'https://www.castingcallpro.com/castings?type=unpaid',
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
                    soup.find_all('div', class_=re.compile(r'casting|card|item|role|job', re.I)) or
                    soup.find_all('article') or
                    soup.find_all('li', class_=re.compile(r'casting|item|role', re.I))
                )
                for item in items[:25]:
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
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"castingcallpro item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"castingcallpro.com error ({url}): {e}")
        return castings


# ── mandy.com (UK) ────────────────────────────────────────────────────────────

class MandyScraper(BaseScraper):
    """mandy.com — UK/international film & TV casting board."""
    name = 'mandy.com'
    BASE_URL = 'https://www.mandy.com'
    country = 'UK'
    URLS = [
        'https://www.mandy.com/uk/actor/job-list',
        'https://www.mandy.com/uk/casting',
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
                    soup.find_all('div', class_=re.compile(r'job|casting|card|listing|item', re.I)) or
                    soup.find_all('article')
                )
                for item in items[:25]:
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
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"mandy.com item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"mandy.com error ({url}): {e}")
        return castings


# ── streetcasting.ch ─────────────────────────────────────────────────────────

class StreetCastingScraper(BaseScraper):
    """streetcasting.ch — Swiss street casting platform."""
    name = 'streetcasting.ch'
    BASE_URL = 'https://www.streetcasting.ch'
    country = 'CH'
    URLS = [
        'https://www.streetcasting.ch/castings/',
        'https://www.streetcasting.ch/casting/',
        'https://www.streetcasting.ch/jobs/',
        'https://www.streetcasting.ch/',
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
                    soup.find_all('article') or
                    soup.find_all('div', class_=re.compile(r'casting|post|item|job|card', re.I)) or
                    soup.find_all('li', class_=re.compile(r'casting|item|job', re.I))
                )
                for item in items[:25]:
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
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"streetcasting.ch item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"streetcasting.ch error ({url}): {e}")
        return castings


# ── 451.ch ────────────────────────────────────────────────────────────────────

class Casting451Scraper(BaseScraper):
    """451.ch — Swiss casting and film production platform."""
    name = '451.ch'
    BASE_URL = 'https://www.451.ch'
    country = 'CH'
    URLS = [
        'https://www.451.ch/casting/',
        'https://www.451.ch/castings/',
        'https://www.451.ch/jobs/',
        'https://www.451.ch/stellenangebote/',
        'https://www.451.ch/',
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
                    soup.find_all('article') or
                    soup.find_all('div', class_=re.compile(r'casting|post|item|job|card|entry', re.I)) or
                    soup.find_all('li', class_=re.compile(r'casting|item|job', re.I))
                )
                for item in items[:25]:
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
                        if casting:
                            castings.append(casting)
                    except Exception as e:
                        logger.debug(f"451.ch item error: {e}")
                if castings:
                    break
            except Exception as e:
                logger.warning(f"451.ch error ({url}): {e}")
        return castings


# ── backstage.com ────────────────────────────────────────────────────────────

class BackstageScraper(BaseScraper):
    """
    backstage.com — major US/UK/international casting platform.

    Three independent discovery strategies (each is tried; results are merged):

    1. XML Sitemap  — Backstage publishes sitemaps listing all /casting/ URLs.
                      Parse to find recently-added casting pages.
    2. Magazine articles — Backstage publishes "Now Casting" roundup articles at
                      /magazine/article/...  These ARE server-side rendered (no JS
                      needed). Fetch them and extract /casting/slug-ID links.
    3. Search engines — DuckDuckGo HTML endpoint, then Bing, then Google as
                      successive fallbacks.

    Individual /casting/slug-ID/ pages use Next.js SSR and return full HTML.
    """
    name = 'backstage.com'
    BASE_URL = 'https://www.backstage.com'
    country = 'UK'

    # Magazine roundup articles list current castings and ARE server-side rendered
    ARTICLE_SEARCH_QUERIES = [
        'site:backstage.com/magazine now casting UK Europe 2026',
        'site:backstage.com/magazine animation voiceover teen child UK 2026',
        'site:backstage.com/magazine casting calls UK Europe 2026',
    ]

    # Direct casting page queries (fallback)
    CASTING_SEARCH_QUERIES = [
        'backstage.com/casting teen youth child actor UK Europe 2026',
        'backstage.com/casting voiceover animation youth 2026',
        'backstage.com/casting 13 14 15 16 years UK Europe 2026',
    ]

    # Sitemaps to try
    SITEMAP_URLS = [
        'https://www.backstage.com/sitemap.xml',
        'https://www.backstage.com/sitemap_index.xml',
        'https://www.backstage.com/sitemap-0.xml',
    ]

    _HDRS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/121.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    # Regex that matches individual casting detail pages
    _DETAIL_RE = re.compile(r'backstage\.com/casting/[\w%-]+-\d+/?$')

    # ── main entry point ──────────────────────────────────────────────────────

    def scrape(self) -> list:
        casting_urls: dict = {}   # url → snippet (snippet may be empty string)

        # ── Strategy 1: XML sitemaps ─────────────────────────────────────────
        logger.info("backstage.com: Strategy 1 — checking XML sitemaps")
        sm_urls = self._sitemap_urls()
        logger.info(f"backstage.com: sitemap yielded {len(sm_urls)} casting URLs")
        for u in sm_urls:
            casting_urls.setdefault(u, '')

        # ── Strategy 2: Magazine roundup articles ────────────────────────────
        logger.info("backstage.com: Strategy 2 — magazine article search")
        article_urls = set()
        for query in self.ARTICLE_SEARCH_QUERIES:
            found = self._search_urls(query, pattern=r'backstage\.com/magazine/article/')
            logger.info(f"backstage.com: article query '{query[:50]}' → {len(found)} articles")
            article_urls.update(found)

        logger.info(f"backstage.com: fetching {len(article_urls)} article pages for casting links")
        for art_url in list(article_urls)[:10]:
            links = self._extract_casting_links_from_article(art_url)
            logger.info(f"backstage.com: article → {len(links)} casting links: {art_url}")
            for u in links:
                casting_urls.setdefault(u, '')

        # ── Strategy 3: Search engines for casting pages directly ────────────
        logger.info("backstage.com: Strategy 3 — direct casting search")
        for query in self.CASTING_SEARCH_QUERIES:
            found = self._search_urls_with_snippets(query, pattern=self._DETAIL_RE)
            logger.info(f"backstage.com: casting query '{query[:50]}' → {len(found)} URLs")
            for u, snip in found:
                casting_urls.setdefault(u, snip or '')

        logger.info(f"backstage.com: total unique casting URLs to process: {len(casting_urls)}")

        # ── Process every discovered URL ─────────────────────────────────────
        castings = []
        for url, snippet in casting_urls.items():
            title = self._slug_to_title(url) + ' Casting'
            casting = {
                'id': make_id(url),
                'title': title,
                'source_url': url,
                'source_site': self.name,
                'country': self.country,
                'description': snippet,
            }
            logger.info(f"backstage.com: processing {url}")
            enriched = self._enrich_backstage(casting)
            if enriched:
                age_str = (f"{enriched['age_min']}-{enriched['age_max']}"
                           if enriched.get('age_min') is not None else "age unknown")
                logger.info(f"backstage.com ✅ ADDED: {enriched['title']} ({age_str})")
                castings.append(enriched)
            else:
                logger.info(f"backstage.com ❌ filtered: {title}")

            if len(castings) >= 30:
                break

        logger.info(f"backstage.com: done — {len(castings)} castings passed all filters")
        return castings

    # ── Strategy 1: sitemaps ──────────────────────────────────────────────────

    def _sitemap_urls(self) -> set:
        """Parse Backstage XML sitemaps and return /casting/slug-ID/ URLs."""
        import urllib.parse as up
        found = set()
        for sm_url in self.SITEMAP_URLS:
            try:
                resp = requests.get(sm_url, headers=self._HDRS, timeout=20)
                logger.info(f"backstage.com: sitemap {sm_url} → HTTP {resp.status_code}")
                if resp.status_code != 200:
                    continue
                # Parse XML — find all <loc> tags
                soup = BeautifulSoup(resp.text, 'lxml')
                locs = soup.find_all('loc')
                logger.info(f"backstage.com: sitemap has {len(locs)} <loc> entries")
                for loc in locs:
                    url = loc.get_text(strip=True)
                    if self._DETAIL_RE.search(url):
                        found.add(url)
                    elif 'sitemap' in url.lower():
                        # Sub-sitemap index — fetch it too
                        try:
                            r2 = requests.get(url, headers=self._HDRS, timeout=15)
                            if r2.status_code == 200:
                                s2 = BeautifulSoup(r2.text, 'lxml')
                                for loc2 in s2.find_all('loc'):
                                    u2 = loc2.get_text(strip=True)
                                    if self._DETAIL_RE.search(u2):
                                        found.add(u2)
                        except Exception:
                            pass
                if found:
                    break   # got something from this sitemap, skip the others
            except Exception as e:
                logger.info(f"backstage.com: sitemap error {sm_url} — {e}")
        return found

    # ── Strategy 2: magazine articles ────────────────────────────────────────

    def _extract_casting_links_from_article(self, article_url: str) -> set:
        """Fetch a Backstage magazine article page and extract /casting/ links."""
        found = set()
        try:
            resp = requests.get(article_url, headers=self._HDRS, timeout=20)
            logger.info(f"backstage.com: article HTTP {resp.status_code} — {article_url}")
            if resp.status_code != 200:
                return found
            soup = BeautifulSoup(resp.text, 'lxml')
            for a in soup.find_all('a', href=True):
                href = a['href']
                if href.startswith('/'):
                    href = self.BASE_URL + href
                if self._DETAIL_RE.search(href):
                    found.add(href)
        except Exception as e:
            logger.info(f"backstage.com: article fetch error — {e}")
        return found

    # ── Strategy 3 helpers: search engines ───────────────────────────────────

    def _search_urls(self, query: str, pattern: str) -> set:
        """Search DDG/Bing/Google and return URLs matching pattern (no snippets)."""
        results = self._search_with_snippets(query)
        pat = re.compile(pattern)
        return {url for url, _ in results if pat.search(url)}

    def _search_urls_with_snippets(self, query: str, pattern) -> list:
        """Search and return [(url, snippet)] matching the given compiled pattern."""
        results = self._search_with_snippets(query)
        return [(url, snip) for url, snip in results if pattern.search(url)]

    def _search_with_snippets(self, query: str) -> list:
        """Try DDG → Bing → Google, return first non-empty [(url, snippet)]."""
        for engine_fn, name in [
            (self._ddg,    'DuckDuckGo'),
            (self._bing,   'Bing'),
            (self._google, 'Google'),
        ]:
            logger.info(f"backstage.com: searching {name} for: {query[:60]}")
            try:
                results = engine_fn(query)
                logger.info(f"backstage.com: {name} returned {len(results)} results")
                if results:
                    return results
            except Exception as e:
                logger.info(f"backstage.com: {name} error — {e}")
        return []

    def _ddg(self, query: str) -> list:
        import urllib.parse as up
        url = 'https://html.duckduckgo.com/html/?q=' + up.quote(query)
        resp = requests.get(url, headers=self._HDRS, timeout=20)
        logger.info(f"backstage.com: DDG HTTP {resp.status_code}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'lxml')
        results = []
        for div in soup.find_all('div', class_='result'):
            a = div.find('a', class_='result__a')
            if not a:
                continue
            href = a.get('href', '')
            parsed = up.urlparse(href)
            qs = up.parse_qs(parsed.query)
            actual = qs.get('uddg', [''])[0] or href
            actual = up.unquote(actual)
            if 'backstage.com' not in actual:
                continue
            snip_el = div.find('a', class_='result__snippet')
            snippet = snip_el.get_text(strip=True) if snip_el else ''
            results.append((actual, snippet))
        return results

    def _bing(self, query: str) -> list:
        import urllib.parse as up
        url = 'https://www.bing.com/search?q=' + up.quote(query) + '&count=20'
        resp = requests.get(url, headers=self._HDRS, timeout=20)
        logger.info(f"backstage.com: Bing HTTP {resp.status_code}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'lxml')
        results = []
        for li in soup.find_all('li', class_='b_algo'):
            a = li.find('a', href=True)
            if not a:
                continue
            href = a['href']
            if 'backstage.com' not in href:
                continue
            snip_el = li.find('p') or li.find('div', class_='b_caption')
            snippet = snip_el.get_text(strip=True) if snip_el else ''
            results.append((href, snippet))
        return results

    def _google(self, query: str) -> list:
        import urllib.parse as up
        url = 'https://www.google.com/search?q=' + up.quote(query) + '&num=20&hl=en'
        resp = requests.get(url, headers=self._HDRS, timeout=20)
        logger.info(f"backstage.com: Google HTTP {resp.status_code}")
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'lxml')
        results = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if not href.startswith('/url?q='):
                continue
            actual = up.unquote(href[7:].split('&')[0])
            if 'backstage.com' not in actual:
                continue
            parent = a.find_parent(['div', 'li'])
            snippet = parent.get_text(separator=' ', strip=True)[:400] if parent else ''
            results.append((actual, snippet))
        return results

    # ── enrich individual casting pages ───────────────────────────────────────

    def _enrich_backstage(self, casting: dict):
        """Fetch a Backstage casting detail page and extract role data."""
        url     = casting['source_url']
        title   = casting['title']
        snippet = casting.get('description', '')

        if not self._url_ok(url):
            return None
        if not self._title_ok(title):
            return None

        page_text   = ''
        pdf_urls    = []
        pdf_content = ''

        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            logger.info(f"backstage.com: detail HTTP {resp.status_code} — {url}")
            if resp.status_code in (404, 410):
                return None
            if resp.status_code == 200:
                detail = BeautifulSoup(resp.text, 'lxml')

                # Real title from h1
                h1 = detail.find('h1')
                if h1:
                    rt = h1.get_text(strip=True)
                    if len(rt) > 4:
                        casting['title'] = rt + ' Casting'
                        logger.info(f"backstage.com: title = {rt!r}")

                content_el = (
                    detail.find('div', class_=re.compile(
                        r'content|entry|article|post-body|description|role|casting', re.I
                    )) or detail.find('article') or detail.find('main')
                )
                page_text = (content_el or detail).get_text(separator=' ', strip=True)[:4000]
                logger.info(f"backstage.com: page_text={len(page_text)} chars, snippet={len(snippet)} chars")

                pdf_urls = find_pdf_links(detail, url)
                for pu in pdf_urls[:3]:
                    txt = fetch_pdf_text(pu, url)
                    if txt:
                        pdf_content += f'\n--- PDF: {pu} ---\n{txt}'
        except Exception as e:
            logger.info(f"backstage.com: detail fetch error — {e}")

        full_text = f"{casting['title']} {snippet} {page_text} {pdf_content}"

        if not full_text.strip():
            return None

        if not _is_open_casting(full_text):
            if not any(kw in full_text.lower() for kw in ('apply', 'audition', 'seeking', 'sought')):
                logger.info(f"backstage.com: failed open-casting check: {casting['title']!r}")
                return None

        age_min, age_max = extract_age_range(full_text)
        logger.info(f"backstage.com: age={age_min}-{age_max} gender={extract_gender(full_text)}")

        casting['description'] = page_text or snippet
        casting['pdf_urls']    = pdf_urls
        casting['pdf_content'] = pdf_content
        casting['age_min'], casting['age_max'] = age_min, age_max
        casting['gender']   = extract_gender(full_text)
        casting['deadline'] = extract_deadline(full_text)
        return casting

    @staticmethod
    def _slug_to_title(url: str) -> str:
        slug = url.rstrip('/').split('/')[-1]
        slug = re.sub(r'-\d+$', '', slug)
        return slug.replace('-', ' ').title()


# ── Main runner ───────────────────────────────────────────────────────────────

# Keywords that indicate a casting is open to remote/e-casting applicants
ECAST_KEYWORDS = [
    'e-casting', 'ecasting', 'self-tape', 'selftape', 'self tape',
    'online casting', 'remote', 'work-from-home', 'work from home',
    'worldwide', 'international applicants',
    'nicht vor ort', 'aus dem ausland', 'videoauftritt', 'videobewerbung',
    'open to all', 'apply from anywhere',
]

def _allows_remote(text: str) -> bool:
    """Return True if the casting text suggests remote/e-casting is accepted."""
    t = text.lower()
    return any(kw in t for kw in ECAST_KEYWORDS)


SCRAPER_REGISTRY = {
    'filmkidsplus.ch':    FilmKidsPlusScraper,
    'studentfilm.ch':     StudentFilmScraper,
    'ronorp.net':         RonorpScraper,
    'encast.pro':         EnCastScraper,
    'swisscasting.ch':    SwissCastingScraper,
    'streetcasting.ch':   StreetCastingScraper,
    '451.ch':             Casting451Scraper,
    'casting-network.de': CastingNetworkDEScraper,
    'backstage.com':      BackstageScraper,
    'castforward.de':     CastForwardDEScraper,
    'castingcallpro.com': CastingCallProScraper,
    'mandy.com':          MandyScraper,
}


def run_scrapers(settings: dict) -> tuple:
    """
    Run all enabled scrapers.
    Returns (list_of_castings, stats_dict)
    """
    enabled_sites = settings.get('enabled_sites', list(SCRAPER_REGISTRY.keys()))
    enabled_countries = settings.get('enabled_countries', ['CH'])
    de_ecast_only = settings.get('de_ecast_only', 'true') == 'true'
    uk_ecast_only = settings.get('uk_ecast_only', 'true') == 'true'

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

                # E-casting filter: for DE/UK, only include if casting allows remote
                country = casting.get('country', '')
                full_text = f"{casting.get('title','')} {casting.get('description','')}"
                if country == 'DE' and de_ecast_only and not _allows_remote(full_text):
                    stats['filtered'] += 1
                    logger.debug(f"  Filtered (DE, no e-casting): {casting.get('title','?')}")
                    continue
                if country == 'UK' and uk_ecast_only and not _allows_remote(full_text):
                    stats['filtered'] += 1
                    logger.debug(f"  Filtered (UK, no e-casting): {casting.get('title','?')}")
                    continue

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
