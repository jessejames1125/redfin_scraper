#!/usr/bin/env python3
"""
redfin_spokane_to_scout.py
• Scrape Redfin active Spokane listings
• Resolve each street to a PID with SCOUT/PropertyLookup
• Pull the short plat / lot-block legal description from the SCOUT summary page
• Count keywords (incl. every "L0…L99") and export to Excel with enhanced visualizations
• Email results with Excel and PDF attachments

USAGE EXAMPLES:
  python redfin_scraper.py                     # Creates HTML email preview (safe test mode)
  python redfin_scraper.py --send-email        # Actually sends email (requires setup)
  python redfin_scraper.py --no-email          # Just creates files, no email
  python redfin_scraper.py --send-email --provider outlook  # Use different email provider
  python redfin_scraper.py --schedule          # Run daily at 10am PST (24-hour automation)
  python redfin_scraper.py --limit 10          # Process only first 10 properties (testing)

EMAIL SETUP:
  For Outlook/Hotmail (easiest): set EMAIL_ADDRESS=you@outlook.com & EMAIL_PASSWORD=yourpassword
  For Gmail (harder): Requires app password setup
"""

import argparse, datetime as dt, logging, re, sys, time, os, warnings
import smtplib
import schedule
import pytz
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# Suppress CSS selector warnings from BeautifulSoup
warnings.filterwarnings("ignore", message=".*pseudo class.*deprecated.*", category=FutureWarning)
from reportlab.lib.pagesizes import letter, A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

# ───── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ───── constants ──────────────────────────────────────────────────────────────
HDRS          = {"User-Agent": "Mozilla/5.0"}

# Multiple Redfin URLs for different jurisdictions
REDFIN_SOURCES = {
    "Spokane City": "https://www.redfin.com/city/17154/WA/Spokane/filter/status=active",
    "Spokane County": "https://www.redfin.com/county/1736/WA/Spokane-County/filter/status=active"
}

SLUG_RE       = re.compile(r"/WA/Spokane/([^/]+)/home")

SCOUT_LAYER   = ("https://gismo.spokanecounty.org/arcgis/rest/services/"
                 "SCOUT/PropertyLookup/MapServer/0/query")
SCOUT_SUMMARY = ("https://cp.spokanecounty.org/SCOUT/propertyinformation/"
                 "Summary.aspx?PID={} ")

# capture strings like "FAIRWOOD CREST NO 4 L23 B2"
LEGAL_RE_HTML = re.compile(r">\s*([A-Z0-9 ]+ L\d{1,2} B\d+)\s*<")

# Updated keywords per Aaron's requirements
KEYWORDS_BASE = [
    " LT","LTS"," L ","LOTS","THRU"," TO ",
    # "AND","ALL",  # Commented out - too dominant in results
    "THROUGH","&",
    # ">=1500","RANCH",">=1500&RANCH"  # Commented out - not needed for keyword analysis
]
KEYWORDS      = KEYWORDS_BASE + [f"L{i}" for i in range(100)]   # L0 … L99

# Additional Scout search functionality
SCOUT_SEARCH_URL = ("https://gismo.spokanecounty.org/arcgis/rest/services/"
                   "SCOUT/PropertyLookup/MapServer/0/query")

# Email configuration
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Alternative email providers (easier setup)
EMAIL_PROVIDERS = {
    'gmail': {'smtp': 'smtp.gmail.com', 'port': 587, 'requires_app_password': True},
    'outlook': {'smtp': 'smtp-mail.outlook.com', 'port': 587, 'requires_app_password': False},
    'yahoo': {'smtp': 'smtp.mail.yahoo.com', 'port': 587, 'requires_app_password': True},
    'aol': {'smtp': 'smtp.aol.com', 'port': 587, 'requires_app_password': False}
}

# ───── helpers ────────────────────────────────────────────────────────────────

def create_robust_session():
    """Create a requests session with retry logic and timeout handling."""
    session = requests.Session()
    
    # Define retry strategy
    retry_strategy = Retry(
        total=3,                # Total number of retries
        backoff_factor=1,       # Wait time between retries (1s, 2s, 4s)
        status_forcelist=[429, 500, 502, 503, 504],  # HTTP status codes to retry on
        raise_on_status=False   # Don't raise exception on HTTP errors
    )
    
    # Mount adapter with retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

# Global session for reuse
ROBUST_SESSION = create_robust_session()
def extract_street(card_addr: str | None, url_href: str) -> str:
    """Return street line without city/ZIP, e.g. '11628 N GALAHAD DR'."""
    if card_addr:
        return card_addr.split(",")[0].upper().strip()

    m = SLUG_RE.search(url_href)
    if not m:
        return ""
    raw = m.group(1).replace("-", " ").upper()
    raw = re.split(r"\sSPOKANE|\sWA\s", raw)[0]
    raw = re.sub(r"\s\d{5}$", "", raw)
    return raw.strip()

def extract_price_from_card(card) -> int:
    """Extract price from Redfin property card."""
    card_text = card.get_text()
    
    # Look for price patterns in the entire card text
    price_patterns = [
        r'\$([0-9,]+)\s*M',      # $1.5M format
        r'\$([0-9,]+)\s*K',      # $450K format  
        r'\$([0-9,]+(?:\.[0-9]+)?)\s*M',  # $1.25M format
        r'\$([0-9,]+)',          # $450,000 format
        r'([0-9,]+)\s*K',        # 450K format (no $)
        r'([0-9,]+)'             # Raw numbers as last resort
    ]
    
    for pattern in price_patterns:
        matches = re.findall(pattern, card_text)
        for match in matches:
            try:
                # Clean the match
                price_str = match.replace(',', '').replace('$', '')
                price_num = float(price_str)
                
                # Apply multipliers based on pattern
                if 'M' in pattern:
                    price_num *= 1000000
                elif 'K' in pattern:
                    price_num *= 1000
                
                # Only accept reasonable house prices (between $50K and $50M)
                if 50000 <= price_num <= 50000000:
                    price_int = int(price_num)
                    # Trim rightmost digit as suggested - prices seem to have extra digit
                    price_str = str(price_int)
                    if len(price_str) > 5:  # Only trim if more than 5 digits
                        price_int = int(price_str[:-1])  # Remove rightmost digit
                    
                    return price_int
                    
            except (ValueError, TypeError):
                continue
    
    return 0

def extract_lot_size_from_card(card) -> float:
    """Extract lot size in acres from Redfin property card."""
    # Look for lot size in various formats
    card_text = card.get_text()
    
    # Look for "X,XXX sq ft lot" or "X.X acres" patterns
    lot_patterns = [
        r'([\d,]+)\s*sq\s*ft\s*lot',
        r'([\d.]+)\s*acres?\s*lot',
        r'([\d.]+)\s*acres?(?:\s|$)',
        r'Lot.*?([\d,]+)\s*sq.*?ft',
        r'Lot.*?([\d.]+)\s*acres?'
    ]
    
    for pattern in lot_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                value_str = match.group(1).replace(',', '')
                value = float(value_str)
                
                # If it's square feet, convert to acres
                if 'sq' in pattern or 'ft' in pattern:
                    return round(value / 43560, 3)  # Convert sq ft to acres
                else:
                    return value  # Already in acres
            except ValueError:
                continue
    
    return 0.0

def extract_post_date_from_card(card) -> str:
    """Extract post/listing date from Redfin property card."""
    card_text = card.get_text()
    
    # Debug: log some card text to see what we're working with
    # logging.info("Card text sample: %s", card_text[:200])
    
    # Look for various Redfin date/time patterns
    time_patterns = [
        # Relative time patterns
        r'(\d+)\s*HR\s*AGO',           # "1 HR AGO"
        r'(\d+)\s*HRS\s*AGO',          # "17 HRS AGO" 
        r'(\d+)\s*HOUR\s*AGO',         # "1 HOUR AGO"
        r'(\d+)\s*HOURS\s*AGO',        # "17 HOURS AGO"
        r'(\d+)\s*MIN\s*AGO',          # "30 MIN AGO"
        r'(\d+)\s*MINS\s*AGO',         # "45 MINS AGO"
        r'(\d+)\s*MINUTE\s*AGO',       # "1 MINUTE AGO"
        r'(\d+)\s*MINUTES\s*AGO',      # "45 MINUTES AGO"
        r'(\d+)\s*DAY\s*AGO',          # "1 DAY AGO"
        r'(\d+)\s*DAYS\s*AGO',         # "3 DAYS AGO"
        r'(\d+)\s*WEEK\s*AGO',         # "1 WEEK AGO"
        r'(\d+)\s*WEEKS\s*AGO',        # "2 WEEKS AGO"
        r'(\d+)\s*MONTH\s*AGO',        # "1 MONTH AGO"
        r'(\d+)\s*MONTHS\s*AGO',       # "2 MONTHS AGO"
        
        # NEW prefix patterns
        r'NEW\s+(\d+)\s*HR\s*AGO',     # "NEW 1 HR AGO"
        r'NEW\s+(\d+)\s*HRS\s*AGO',    # "NEW 17 HRS AGO"
        r'NEW\s+(\d+)\s*HOUR\s*AGO',   # "NEW 1 HOUR AGO"
        r'NEW\s+(\d+)\s*HOURS\s*AGO',  # "NEW 17 HOURS AGO"
        r'NEW\s+(\d+)\s*MIN\s*AGO',    # "NEW 30 MIN AGO"
        r'NEW\s+(\d+)\s*MINS\s*AGO',   # "NEW 45 MINS AGO"
        r'NEW\s+(\d+)\s*MINUTE\s*AGO', # "NEW 1 MINUTE AGO"
        r'NEW\s+(\d+)\s*MINUTES\s*AGO',# "NEW 45 MINUTES AGO"
        r'NEW\s+(\d+)\s*DAY\s*AGO',    # "NEW 1 DAY AGO"
        r'NEW\s+(\d+)\s*DAYS\s*AGO',   # "NEW 3 DAYS AGO"
        r'NEW\s+(\d+)\s*WEEK\s*AGO',   # "NEW 1 WEEK AGO"
        r'NEW\s+(\d+)\s*WEEKS\s*AGO',  # "NEW 2 WEEKS AGO"
        r'NEW\s+(\d+)\s*MONTH\s*AGO',  # "NEW 1 MONTH AGO"
        r'NEW\s+(\d+)\s*MONTHS\s*AGO', # "NEW 2 MONTHS AGO"
        
        # Simple "NEW" indicators (treat as today)
        r'\bNEW\b',                    # Just "NEW" by itself
        r'NEW\s+LISTING',              # "NEW LISTING"
        r'JUST\s+LISTED',              # "JUST LISTED"
        r'PRICE\s+IMPROVEMENT',        # "PRICE IMPROVEMENT"
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                now = dt.datetime.now()
                
                # Handle simple "NEW" without numbers
                if pattern in [r'\bNEW\b', r'NEW\s+LISTING', r'JUST\s+LISTED', r'PRICE\s+IMPROVEMENT']:
                    return now.strftime('%m/%d/%Y')
                
                time_value = int(match.group(1))
                
                # Determine the time unit and calculate the post date
                if 'HR' in pattern or 'HOUR' in pattern:
                    post_datetime = now - dt.timedelta(hours=time_value)
                elif 'MIN' in pattern:
                    post_datetime = now - dt.timedelta(minutes=time_value)
                elif 'DAY' in pattern:
                    post_datetime = now - dt.timedelta(days=time_value)
                elif 'WEEK' in pattern:
                    post_datetime = now - dt.timedelta(weeks=time_value)
                elif 'MONTH' in pattern:
                    post_datetime = now - dt.timedelta(days=time_value * 30)  # Approximate
                else:
                    continue
                
                # Return just the calendar date (no time)
                return post_datetime.strftime('%m/%d/%Y')
                
            except (ValueError, TypeError, IndexError):
                continue
    
    # Look for explicit date formats
    date_patterns = [
        # Standard US date formats
        r'(\d{1,2}/\d{1,2}/\d{4})',              # "12/25/2024"
        r'(\d{1,2}-\d{1,2}-\d{4})',              # "12-25-2024"
        r'(\d{4}-\d{1,2}-\d{1,2})',              # "2024-12-25"
        
        # With context
        r'Posted:?\s*(\d{1,2}/\d{1,2}/\d{4})',   # "Posted: 12/25/2024"
        r'Listed:?\s*(\d{1,2}/\d{1,2}/\d{4})',   # "Listed: 12/25/2024"
        r'Added:?\s*(\d{1,2}/\d{1,2}/\d{4})',    # "Added: 12/25/2024"
        r'Date:?\s*(\d{1,2}/\d{1,2}/\d{4})',     # "Date: 12/25/2024"
        
        # Month names
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})',  # "Jan 15, 2024"
        r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})',     # "15 Jan 2024"
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                if len(match.groups()) == 1:
                    # Simple date format
                    date_str = match.group(1)
                    # Validate the date isn't in the future
                    try:
                        parsed_date = dt.datetime.strptime(date_str, '%m/%d/%Y')
                        if parsed_date <= dt.datetime.now():
                            return date_str
                    except ValueError:
                        try:
                            parsed_date = dt.datetime.strptime(date_str, '%m-%d-%Y')
                            if parsed_date <= dt.datetime.now():
                                return parsed_date.strftime('%m/%d/%Y')
                        except ValueError:
                            try:
                                parsed_date = dt.datetime.strptime(date_str, '%Y-%m-%d')
                                if parsed_date <= dt.datetime.now():
                                    return parsed_date.strftime('%m/%d/%Y')
                            except ValueError:
                                continue
                elif len(match.groups()) == 3:
                    # Month name format
                    month_name, day, year = match.groups()
                    if month_name.upper() in ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                                            'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']:
                        try:
                            month_num = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                                       'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'].index(month_name.upper()) + 1
                            parsed_date = dt.datetime(int(year), month_num, int(day))
                            if parsed_date <= dt.datetime.now():
                                return parsed_date.strftime('%m/%d/%Y')
                        except (ValueError, IndexError):
                            continue
            except (ValueError, TypeError, IndexError):
                continue
    
    # Look for "Today", "Yesterday" etc.
    relative_date_patterns = [
        r'\bTODAY\b',
        r'\bYESTERDAY\b',
        r'THIS\s+WEEK',
        r'LAST\s+WEEK'
    ]
    
    for pattern in relative_date_patterns:
        if re.search(pattern, card_text, re.IGNORECASE):
            now = dt.datetime.now()
            if 'TODAY' in pattern:
                return now.strftime('%m/%d/%Y')
            elif 'YESTERDAY' in pattern:
                return (now - dt.timedelta(days=1)).strftime('%m/%d/%Y')
            elif 'THIS WEEK' in pattern:
                return (now - dt.timedelta(days=3)).strftime('%m/%d/%Y')  # Rough estimate
            elif 'LAST WEEK' in pattern:
                return (now - dt.timedelta(days=7)).strftime('%m/%d/%Y')
    
    # Default fallback: assume listing posted today when no patterns matched
    return dt.datetime.now().strftime('%m/%d/%Y')

def fetch_redfin_properties() -> list[dict]:
    """Fetch Redfin properties from both Spokane City and County with enhanced data."""
    all_properties = []
    
    for source_name, url in REDFIN_SOURCES.items():
        logging.info("Fetching properties from %s...", source_name)
        try:
            response = ROBUST_SESSION.get(url, headers=HDRS, timeout=45)
            response.raise_for_status()
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            
            for card in soup.select("div.HomeCardContainer"):
                a = card.find("a", href=True)
                disp = card.select_one("div.homeAddressV2")
                if not a:
                    continue
                
                street = extract_street(disp.text if disp else None, a["href"])
                if not street:
                    continue
                
                # Extract existing sqft data
                sqft = 0
                sqft_selectors = [
                    "div.stats span:contains('Sq Ft')",
                    "div.homeStatsV2 span:contains('Sq Ft')", 
                    "div.HomeStatsV2 span:contains('Sq Ft')",
                    "span.sqft-value",
                    "span.value:contains('Sq Ft')",
                    "[data-rf-test-id='abp-sqFt']",
                    ".sqft"
                ]
                
                for selector in sqft_selectors:
                    try:
                        sqft_elem = card.select_one(selector)
                        if sqft_elem:
                            sqft_text = sqft_elem.get_text()
                            sqft_match = re.search(r'([\d,]+)', sqft_text)
                            if sqft_match:
                                sqft = int(sqft_match.group(1).replace(',', ''))
                                break
                    except:
                        continue
                
                # Fallback sqft extraction
                if sqft == 0:
                    card_text = card.get_text()
                    sqft_patterns = [
                        r'([\d,]+)\s*[Ss]q\s*[Ff]t',
                        r'([\d,]+)\s*[Ss]quare\s*[Ff]eet',
                        r'([\d,]+)\s*SF\b'
                    ]
                    
                    for pattern in sqft_patterns:
                        match = re.search(pattern, card_text)
                        if match:
                            try:
                                sqft = int(match.group(1).replace(',', ''))
                                break
                            except ValueError:
                                continue
                
                # Extract new data fields
                price = extract_price_from_card(card)
                lot_size_acres = extract_lot_size_from_card(card)
                post_date = extract_post_date_from_card(card)
                
                all_properties.append({
                    'street': street,
                    'sqft': sqft,
                    'price': price,
                    'lot_size_acres': lot_size_acres,
                    'post_date': post_date,
                    'source': source_name  # Track which source this came from
                })
            
            logging.info("Found %d properties from %s", 
                        len([p for p in all_properties if p['source'] == source_name]), source_name)
                        
        except Exception as e:
            logging.error("Error fetching from %s: %s", source_name, str(e))
            continue
    
    logging.info("Total properties found: %d", len(all_properties))
    return all_properties

def fetch_redfin_streets() -> list[str]:
    """Legacy function - returns just street names for backwards compatibility."""
    properties = fetch_redfin_properties()
    return [prop['street'] for prop in properties]

def arcgis_pid(street: str) -> str | None:
    """Get PID from SCOUT with robust error handling and retries."""
    params = {
        "f":"json",
        "where": f"site_address LIKE '{street}%'",
        "outFields":"PID_NUM",
        "returnGeometry":"false"
    }
    
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            response = ROBUST_SESSION.get(SCOUT_LAYER, params=params, timeout=45)
            response.raise_for_status()  # Raise exception for HTTP errors
            js = response.json()
            
            feats = js.get("features") or []
            if not feats:
                logging.warning("→ No PID for %r", street)
                return None
            return feats[0]["attributes"]["PID_NUM"]
            
        except requests.exceptions.Timeout:
            logging.warning("→ Timeout attempt %d/%d for PID lookup: %s", attempt + 1, max_attempts, street)
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                continue
            else:
                logging.error("→ Final timeout for PID lookup: %s", street)
                return None
                
        except requests.exceptions.RequestException as e:
            logging.warning("→ Network error attempt %d/%d for PID lookup %s: %s", attempt + 1, max_attempts, street, str(e))
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                logging.error("→ Final network error for PID lookup: %s", street)
                return None
                
        except (KeyError, ValueError, TypeError) as e:
            logging.error("→ Data parsing error for PID lookup %s: %s", street, str(e))
            return None
    
    return None

def extract_lot_size_from_scout(text: str) -> float:
    """Extract lot size in acres from SCOUT data."""
    # Look for patterns like "6540 Square Feet" or "5 Acre(s)" or "1.3 Acre(s)"
    # These appear after the city name in the Site Address section
    
    # Pattern for acres
    acre_match = re.search(r'(\d+\.?\d*)\s+Acre\(s\)', text)
    if acre_match:
        try:
            return float(acre_match.group(1))
        except ValueError:
            pass
    
    # Pattern for square feet
    sqft_match = re.search(r'(\d+)\s+Square Feet', text)
    if sqft_match:
        try:
            sqft = int(sqft_match.group(1))
            return round(sqft / 43560, 3)  # Convert to acres
        except ValueError:
            pass
    
    return 0.0

def extract_jurisdiction_from_scout(text: str, html: str) -> str:
    """Extract jurisdiction (Valley/County/City) from SCOUT data."""
    # Look for the city in the Site Address section
    # Pattern: Site Address Parcel Type Site Address City Land Size...
    city_match = re.search(r'Site Address\s+([A-Z\s]+?)\s+(?:\d+\s+Square Feet|\d+\.?\d*\s+Acre)', text)
    if city_match:
        city = city_match.group(1).strip()
        if city == "SPOKANE":
            # Look at tax code to determine if it's City of Spokane vs Spokane County
            # Tax codes starting with 0xxx are typically City of Spokane
            # Tax codes like 1280, higher numbers might be county/valley
            tax_code_match = re.search(r'Tax Code Area Status.*?(\d{4})', text)
            if tax_code_match:
                tax_code = tax_code_match.group(1)
                if tax_code.startswith('0'):
                    return "City of Spokane"
                else:
                    return "Spokane County"
            return "City of Spokane"  # Default for SPOKANE
        else:
            return city.title()
    
    # Fallback patterns
    if 'SPOKANE VALLEY' in text.upper():
        return 'Spokane Valley'
    elif 'SPOKANE' in text.upper():
        return 'City of Spokane'
    
    return "Unknown"

def legal_for_pid(pid: str) -> tuple[str, str, str]:
    """Get legal description from SCOUT with robust error handling and retries."""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            response = ROBUST_SESSION.get(SCOUT_SUMMARY.format(pid), headers=HDRS, timeout=45)
            response.raise_for_status()  # Raise exception for HTTP errors
            html = response.text
            
            text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
            jurisdiction = extract_jurisdiction_from_scout(text, html)
            return text, html, jurisdiction
            
        except requests.exceptions.Timeout:
            logging.warning("→ Timeout attempt %d/%d for SCOUT summary PID %s", attempt + 1, max_attempts, pid)
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                continue
            else:
                logging.error("→ Final timeout for SCOUT summary PID %s", pid)
                # Return empty data to allow processing to continue
                return "", "", "Unknown"
                
        except requests.exceptions.RequestException as e:
            logging.warning("→ Network error attempt %d/%d for SCOUT summary PID %s: %s", attempt + 1, max_attempts, pid, str(e))
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                logging.error("→ Final network error for SCOUT summary PID %s", pid)
                return "", "", "Unknown"
                
        except Exception as e:
            logging.error("→ Parsing error for SCOUT summary PID %s: %s", pid, str(e))
            return "", "", "Unknown"
    
    return "", "", "Unknown"

def should_skip_property(legal_desc: str) -> bool:
    """Check if property should be skipped based on Aaron's filter criteria."""
    upper_desc = legal_desc.upper()
    return "SHORT PLAT" in upper_desc or "LONG PLAT" in upper_desc

def extract_square_footage(text: str) -> int:
    """Extract square footage from SCOUT full page text."""
    # Pattern to match "Dwelling YEAR SQFT NA SF" format
    # Example: "Dwelling 1959 1,920 NA SF" -> extracts 1920
    dwelling_pattern = re.compile(r'Dwelling\s+\d{4}\s+([\d,]+)\s+NA\s+SF', re.IGNORECASE)
    match = dwelling_pattern.search(text)
    
    if match:
        sqft_str = match.group(1).replace(',', '')  # Remove commas
        try:
            return int(sqft_str)
        except ValueError:
            pass
    
    # Fallback pattern for "Gross Living Area" if the above doesn't work
    # Look for patterns like "Gross Living Area 1,920"
    gross_pattern = re.compile(r'Gross\s+Living\s+Area\s+([\d,]+)', re.IGNORECASE)
    match = gross_pattern.search(text)
    
    if match:
        sqft_str = match.group(1).replace(',', '')
        try:
            return int(sqft_str)
        except ValueError:
            pass
    
    return 0  # Return 0 if no square footage found

def extract_unique_lot_numbers(text: str) -> set[str]:
    """Extract unique lot numbers from text, handling L-, L , and L& patterns."""
    upper_text = text.upper()
    lot_numbers = set()
    
    # Pattern to match lot numbers with various separators
    # Matches: L1, L-1, L 1, L&1, etc.
    lot_pattern = re.compile(r'\bL[-\s&]*(\d{1,2})\b')
    
    for match in lot_pattern.finditer(upper_text):
        lot_num = match.group(1)
        lot_numbers.add(f"L{lot_num}")
    
    return lot_numbers

def enhanced_kw_counts(text: str, sqft: int = 0) -> dict[str,int]:
    """Enhanced keyword counting with improved lot number handling per Aaron's requirements."""
    up = text.upper()
    counts = {}
    
    # Handle regular keywords from KEYWORDS_BASE (excluding commented out ones)
    for keyword in KEYWORDS_BASE:
        if keyword == " TO ":
            # Ensure "TO" has spaces on both sides
            counts["TO"] = up.count(keyword)
        elif keyword == "&":
            # Count & symbols, but be careful with lot contexts
            counts[keyword] = up.count(keyword)
        else:
            counts[keyword] = up.count(keyword)
    
    # Handle lot numbers with deduplication
    unique_lots = extract_unique_lot_numbers(text)
    
    # Initialize all lot counts to 0
    for i in range(100):
        lot_key = f"L{i}"
        counts[lot_key] = 0
    
    # Count each unique lot number only once
    for lot in unique_lots:
        if lot in counts:
            counts[lot] = 1
    
    # Handle dash context - only count when next to L
    dash_with_l_pattern = re.compile(r'L[-\s]*\d+')
    dash_matches = len(dash_with_l_pattern.findall(up))
    counts["-"] = dash_matches
    
    return counts

def search_scout_ranch_properties(min_sqft: int = 1500) -> list[dict]:
    """Stubbed: Ranch search is currently disabled."""
    # Disabled ranch search functionality
    # params = {
    #     "f": "json",
    #     "where": f"(legal_description LIKE '%RANCH%' OR site_address LIKE '%RANCH%' OR owner_name LIKE '%RANCH%') AND sqft > {min_sqft}",
    #     "outFields": "PID_NUM,site_address,legal_description,sqft,owner_name",
    #     "returnGeometry": "false",
    #     "resultRecordCount": 1000  # Limit results
    # }
    # try:
    #     response = requests.get(SCOUT_SEARCH_URL, params=params, timeout=30)
    #     js = response.json()
    #     features = js.get("features", [])
    #     results = []
    #     for feature in features:
    #         attrs = feature.get("attributes", {})
    #         results.append({
    #             "pid": attrs.get("PID_NUM"),
    #             "address": attrs.get("site_address"),
    #             "legal_description": attrs.get("legal_description"),
    #             "sqft": attrs.get("sqft"),
    #             "owner_name": attrs.get("owner_name"),
    #             "ranch_match_type": "Legal Desc" if "RANCH" in str(attrs.get("legal_description", "")).upper() else 
    #                                "Address" if "RANCH" in str(attrs.get("site_address", "")).upper() else
    #                                "Owner" if "RANCH" in str(attrs.get("owner_name", "")).upper() else "Other"
    #         })
    #     logging.info("Found %d Ranch properties >%d sqft", len(results), min_sqft)
    #     return results
    # except Exception as e:
    #     logging.error("Error searching Scout for Ranch properties: %s", str(e))
    return []

def create_keyword_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Create a summary of only properties with non-zero keyword counts."""
    keyword_cols = [col for col in df.columns if col in KEYWORDS]
    
    # Create rows for properties that have at least one keyword match
    summary_rows = []
    for _, row in df.iterrows():
        found_keywords = {}
        for kw in keyword_cols:
            if row[kw] > 0:
                found_keywords[kw] = row[kw]
        
        if found_keywords:  # Only include if there are non-zero keywords
            summary_row = {
                'street': row['street'],
                'pid': row['pid'],
                'legal_description': row['legal_description'][:100] + '...' if len(row['legal_description']) > 100 else row['legal_description'],
                'total_keyword_matches': sum(found_keywords.values()),
                'keywords_found': ', '.join([f"{k}({v})" for k, v in found_keywords.items()])
            }
            summary_rows.append(summary_row)
    
    return pd.DataFrame(summary_rows) if summary_rows else pd.DataFrame()

def create_keyword_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Create aggregate statistics for all keywords."""
    keyword_cols = [col for col in df.columns if col in KEYWORDS]
    
    stats = []
    for kw in keyword_cols:
        total_count = df[kw].sum()
        properties_with_kw = (df[kw] > 0).sum()
        if total_count > 0:  # Only include keywords that appear
            stats.append({
                'keyword': kw,
                'total_occurrences': total_count,
                'properties_with_keyword': properties_with_kw,
                'avg_per_property': round(total_count / len(df), 2),
                'max_in_single_property': df[kw].max()
            })
    
    # Sort by total occurrences descending
    stats_df = pd.DataFrame(stats)
    if not stats_df.empty:
        stats_df = stats_df.sort_values('total_occurrences', ascending=False)
    
    return stats_df

def create_lot_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Create analysis specifically for L0-L99 lot keywords."""
    lot_cols = [f"L{i}" for i in range(100)]
    existing_lot_cols = [col for col in lot_cols if col in df.columns]
    
    analysis = []
    for _, row in df.iterrows():
        lot_matches = []
        total_lots = 0
        for lot_col in existing_lot_cols:
            if row[lot_col] > 0:
                lot_matches.append(f"{lot_col}({row[lot_col]})")
                total_lots += row[lot_col]
        
        if lot_matches:  # Only include properties with lot matches
            analysis.append({
                'street': row['street'],
                'pid': row['pid'],
                'total_lot_references': total_lots,
                'lot_numbers_found': ', '.join(lot_matches),
                'unique_lots_count': len(lot_matches)
            })
    
    return pd.DataFrame(analysis) if analysis else pd.DataFrame()

def create_pdf_report(df: pd.DataFrame, summary_df: pd.DataFrame, stats_df: pd.DataFrame, 
                     lot_df: pd.DataFrame, overview_data: dict, pdf_path: Path):
    """Create a comprehensive landscape PDF report with full contents of each Excel sheet."""
    doc = SimpleDocTemplate(str(pdf_path), pagesize=landscape(letter))
    styles = getSampleStyleSheet()
    story = []
    
    # Define available width for landscape layout
    page_width = landscape(letter)[0] - 2*inch  # Account for margins
    
    # Title
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=28,
        spaceAfter=30,
        alignment=1  # Center
    )
    story.append(Paragraph("Spokane Real Estate Scout Report", title_style))
    story.append(Spacer(1, 20))
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # 1. EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════════
    story.append(Paragraph("Executive Summary", styles['Heading2']))
    overview_table_data = [['Metric', 'Value']] + [[k, str(v)] for k, v in overview_data.items()]
    overview_table = Table(overview_table_data, colWidths=[4*inch, 3*inch])
    overview_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 14),
        ('FONTSIZE', (0, 1), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
    ]))
    story.append(overview_table)
    story.append(PageBreak())
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # 2. KEYWORD SUMMARY - FULL SHEET CONTENTS
    # ═══════════════════════════════════════════════════════════════════════════════
    if not summary_df.empty:
        story.append(Paragraph("Keyword Summary - All Properties with Matches", styles['Heading2']))
        story.append(Paragraph(f"Total properties with keyword matches: {len(summary_df)}", styles['Normal']))
        story.append(Spacer(1, 15))
        
        # Full keyword summary table (excluding legal description for PDF clarity)
        summary_data = [['Street', 'PID', 'Total Matches', 'Keywords Found']]
        for _, row in summary_df.iterrows():
            # Truncate long text for PDF display
            street = row['street'][:40] + '...' if len(row['street']) > 40 else row['street']
            pid = str(row['pid'])[:15] + '...' if len(str(row['pid'])) > 15 else str(row['pid'])
            # Increased space for keywords since we removed legal description
            keywords = row['keywords_found'][:65] + '...' if len(row['keywords_found']) > 65 else row['keywords_found']
            
            summary_data.append([
                street,
                pid,
                str(row['total_keyword_matches']),
                keywords
            ])
        
        # Recalculated column widths for better use of space
        summary_table = Table(summary_data, colWidths=[3*inch, 1.5*inch, 1*inch, 4*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (3, 0), (3, -1), 'CENTER'),  # Center the total matches column
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'TOP')
        ]))
        story.append(summary_table)
        story.append(PageBreak())
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # 3. KEYWORD STATISTICS - FULL SHEET CONTENTS
    # ═══════════════════════════════════════════════════════════════════════════════
    if not stats_df.empty:
        story.append(Paragraph("Keyword Statistics - Complete Analysis", styles['Heading2']))
        story.append(Paragraph(f"All keywords found across {len(df)} properties, sorted by frequency", styles['Normal']))
        story.append(Spacer(1, 15))
        
        # Full keyword stats table
        stats_data = [['Keyword', 'Total Occurrences', 'Properties with Keyword', 'Avg per Property', 'Max in Single Property']]
        for _, row in stats_df.iterrows():
            stats_data.append([
                row['keyword'],
                str(row['total_occurrences']),
                str(row['properties_with_keyword']),
                str(row['avg_per_property']),
                str(row['max_in_single_property'])
            ])
        
        stats_table = Table(stats_data, colWidths=[2*inch, 1.5*inch, 1.8*inch, 1.5*inch, 1.7*inch])
        stats_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),  # Left align keyword names
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')
        ]))
        story.append(stats_table)
        story.append(PageBreak())
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # 4. LOT ANALYSIS - FULL SHEET CONTENTS
    # ═══════════════════════════════════════════════════════════════════════════════
    if not lot_df.empty:
        story.append(Paragraph("Lot Number Analysis - Complete Details", styles['Heading2']))
        story.append(Paragraph(f"All {len(lot_df)} properties with specific lot number references", styles['Normal']))
        story.append(Spacer(1, 15))
        
        # Full lot analysis table
        lot_data = [['Street', 'PID', 'Total Lot References', 'Unique Lots Count', 'Lot Numbers Found']]
        for _, row in lot_df.iterrows():
            street = row['street'][:40] + '...' if len(row['street']) > 40 else row['street']
            pid = str(row['pid'])[:15] + '...' if len(str(row['pid'])) > 15 else str(row['pid'])
            lot_numbers = row['lot_numbers_found'][:50] + '...' if len(row['lot_numbers_found']) > 50 else row['lot_numbers_found']
            
            lot_data.append([
                street,
                pid,
                str(row['total_lot_references']),
                str(row['unique_lots_count']),
                lot_numbers
            ])
        
        lot_table = Table(lot_data, colWidths=[2.5*inch, 1.3*inch, 1.3*inch, 1.2*inch, 3.2*inch])
        lot_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (2, 0), (3, -1), 'CENTER'),  # Center the numeric columns
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('VALIGN', (0, 0), (-1, -1), 'TOP')
        ]))
        story.append(lot_table)
    
    # Footer
    story.append(Spacer(1, 30))
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=10,
        alignment=1  # Center
    )
    story.append(Paragraph(f"Report generated on {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", footer_style))
    story.append(Paragraph("This report contains complete data from all Excel sheets (excluding raw data)", footer_style))
    
    doc.build(story)
    logging.info("Created comprehensive landscape PDF report: %s", pdf_path)

def create_test_email_file(excel_path: Path, pdf_path: Path, stats_summary: dict):
    """Create a local HTML file showing what the email would look like."""
    email_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Test Email Preview</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .email-container {{ border: 1px solid #ccc; padding: 20px; background: #f9f9f9; }}
            .header {{ background: #4CAF50; color: white; padding: 10px; text-align: center; }}
            .summary {{ background: #e8f5e9; padding: 15px; margin: 10px 0; }}
            .attachments {{ background: #fff3e0; padding: 15px; margin: 10px 0; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ margin: 5px 0; }}
            .file {{ color: #1976d2; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="email-container">
            <div class="header">
                <h2>📧 EMAIL PREVIEW - SPOKANE REAL ESTATE SCOUT</h2>
                <p>To: Email Recipients</p>
                <p>Subject: Spokane Real Estate Scout Results - {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
            </div>
            
            <h3>Email Body:</h3>
            <div style="border-left: 3px solid #4CAF50; padding-left: 15px; margin: 20px 0;">
                <p>Hello!</p>
                <p>Your Spokane real estate keyword analysis has completed successfully.</p>
                
                <div class="summary">
                    <h4>📊 SUMMARY:</h4>
                    <ul>
                        <li>• Total properties analyzed: {stats_summary.get('total_properties', 'N/A')}</li>
                        <li>• Properties with keywords: {stats_summary.get('properties_with_keywords', 'N/A')}</li>
                        <li>• Unique keywords found: {stats_summary.get('unique_keywords', 'N/A')}</li>
                        <li>• Properties with lot numbers: {stats_summary.get('properties_with_lots', 'N/A')}</li>
                    </ul>
                </div>
                
                <div class="attachments">
                    <h4>📎 Attachments:</h4>
                    <ul>
                        <li>📊 <span class="file">{excel_path.name}</span> - Excel file with 5 sheets: Raw Data, Keyword Summary, Keyword Stats, Lot Analysis, and Overview</li>
                        <li>📄 <span class="file">{pdf_path.name}</span> - PDF report with key findings and visualizations</li>
                    </ul>
                </div>

                <p><strong>Best regards,<br>Your Real Estate Bot Assistant 🏠</strong></p>
            </div>
            
            <div style="background: #ffebee; padding: 15px; margin: 20px 0; border-radius: 5px;">
                <h4>🧪 TEST MODE ACTIVE</h4>
                <p><strong>Files created locally:</strong></p>
                <ul>
                    <li>✅ <span class="file">{excel_path}</span></li>
                    <li>✅ <span class="file">{pdf_path}</span></li>
                    <li>📧 <span class="file">{excel_path.parent / f"test_email_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"}</span> (this preview)</li>
                </ul>
                <p><em>No actual email was sent. Use --send-email flag to send real emails when ready.</em></p>
            </div>
        </div>
    </body>
    </html>
    """
    
    test_email_path = excel_path.parent / f"test_email_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    with open(test_email_path, 'w', encoding='utf-8') as f:
        f.write(email_html)
    
    logging.info("Created email preview: %s", test_email_path)
    return test_email_path

def send_email(excel_path: Path, pdf_path: Path, stats_summary: dict, email_provider='gmail'):
    """Send email with Excel and PDF attachments."""
    # Get email credentials from environment variables
    sender_email = os.getenv('EMAIL_ADDRESS') or os.getenv('GMAIL_EMAIL')
    sender_password = os.getenv('EMAIL_PASSWORD') or os.getenv('GMAIL_APP_PASSWORD')
    
    # Build recipient list - send to both GMAIL_EMAIL and FORWARDING_EMAIL if both exist
    recipients = []
    gmail_email = os.getenv('GMAIL_EMAIL')
    forwarding_email = os.getenv('FORWARDING_EMAIL')
    
    if forwarding_email:
        recipients.append(forwarding_email)
    if gmail_email and gmail_email != forwarding_email:  # Avoid duplicates
        recipients.append(gmail_email)
    
    if not recipients:
        recipients = ['your@email.com']  # Fallback
    
    # Debug logging for recipients
    masked_recipients = []
    for email in recipients:
        masked = email[:3] + "***" + email[-10:] if len(email) > 13 else "***"
        masked_recipients.append(masked)
    
    logging.info("📧 Attempting to send email to: %s", ', '.join(masked_recipients))
    logging.info("📧 Using sender email: %s", sender_email[:3] + "***" + sender_email[-10:] if sender_email and len(sender_email) > 13 else "***")
    
    if not sender_email or not sender_password:
        logging.error("Email credentials not found.")
        logging.info("EASY SETUP OPTIONS:")
        logging.info("")
        logging.info("🔧 OPTION 1 - Use Outlook/Hotmail (easiest):")
        logging.info("   set EMAIL_ADDRESS=your-outlook@hotmail.com")
        logging.info("   set EMAIL_PASSWORD=your-regular-password")
        logging.info("")
        logging.info("🔧 OPTION 2 - Use Gmail (requires app password):")
        logging.info("   set GMAIL_EMAIL=your-gmail@gmail.com") 
        logging.info("   set GMAIL_APP_PASSWORD=16-char-app-password")
        logging.info("")
        logging.info("🧪 OPTION 3 - Test mode (no email needed):")
        logging.info("   python redfin_scraper2.py --test-email")
        return False
    
    provider_config = EMAIL_PROVIDERS.get(email_provider, EMAIL_PROVIDERS['gmail'])
    
    if not provider_config:
        logging.error("Unknown email provider: %s", email_provider)
        return False
    
    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = ', '.join(recipients)  # Multiple recipients
        msg['Subject'] = f"Spokane Real Estate Scout Results - {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        logging.info("📧 Email subject: %s", msg['Subject'])
        
        # Email body
        body = f"""
Hello!

Your Spokane real estate keyword analysis has completed successfully.

SUMMARY:
• Total properties analyzed: {stats_summary.get('total_properties', 'N/A')}
• Properties with keywords: {stats_summary.get('properties_with_keywords', 'N/A')}
• Unique keywords found: {stats_summary.get('unique_keywords', 'N/A')}
• Properties with lot numbers: {stats_summary.get('properties_with_lots', 'N/A')}

Attachments:
📊 Excel file with 5 sheets: Raw Data, Keyword Summary, Keyword Stats, Lot Analysis, and Overview
📄 PDF report with key findings and visualizations

Best regards,
Your Real Estate Bot Assistant 🏠
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        # Attach Excel file
        logging.info("📎 Attaching Excel file: %s", excel_path.name)
        with open(excel_path, "rb") as attachment:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename= {excel_path.name}')
            msg.attach(part)
        
        # Attach PDF file
        logging.info("📎 Attaching PDF file: %s", pdf_path.name)
        with open(pdf_path, "rb") as attachment:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename= {pdf_path.name}')
            msg.attach(part)
        
        # Send email using provider-specific settings
        logging.info("🔄 Connecting to %s (%s:%s)", email_provider, provider_config['smtp'], provider_config['port'])
        server = smtplib.SMTP(provider_config['smtp'], provider_config['port'])
        server.starttls()
        logging.info("🔐 Logging in to email server...")
        server.login(sender_email, sender_password)
        text = msg.as_string()
        logging.info("📤 Sending email...")
        server.sendmail(sender_email, recipients, text)  # Send to multiple recipients
        server.quit()
        
        logging.info("Email sent successfully to %s via %s", ', '.join(masked_recipients), email_provider)
        return True
        
    except Exception as e:
        logging.error("Failed to send email via %s: %s", email_provider, str(e))
        logging.info("💡 Try different provider with --provider flag (outlook, yahoo, aol)")
        return False

def run_daily_report():
    """Run the daily report generation and email sending."""
    logging.info("🕙 Running scheduled daily report at 10am PST...")
    
    # Create a mock args object for scheduled runs
    class MockArgs:
        limit = None
        no_email = False
        test_email = False
        send_email = True  # Always send real emails in scheduled mode
        provider = 'gmail'
        ranch_min_sqft = 1500
    
    args = MockArgs()
    
    try:
        # Run the main logic
        run_main_logic(args)
        logging.info("✅ Scheduled daily report completed successfully")
    except Exception as e:
        logging.error("❌ Scheduled daily report failed: %s", str(e))

def should_run_today():
    """Check if we should run today (skip weekends if desired)."""
    today = dt.datetime.now()
    # Run Monday through Friday (0=Monday, 6=Sunday)
    return today.weekday() < 5  # Skip weekends

def run_scheduler():
    """Run the scheduling system."""
    # Set up Pacific Time zone
    pst = pytz.timezone('US/Pacific')
    
    logging.info("🕐 Starting email automation scheduler...")
    logging.info("📅 Daily reports will be sent at 10:00 AM PST (Monday-Friday)")
    logging.info("⏸️  Press Ctrl+C to stop the scheduler")
    
    # Schedule daily at 10 AM PST
    schedule.every().day.at("10:00").do(run_daily_report)
    
    try:
        while True:
            # Check if we're in PST/PDT and adjust
            now_pst = dt.datetime.now(pst)
            schedule.run_pending()
            
            # Sleep for 1 minute between checks
            time.sleep(60)
            
            # Log status every hour on the hour
            if now_pst.minute == 0:
                logging.info("🕐 Scheduler active - Next run: %s PST", 
                           schedule.next_run().strftime('%Y-%m-%d %H:%M'))
                
    except KeyboardInterrupt:
        logging.info("⏹️  Scheduler stopped by user")

def run_main_logic(args):
    """Extract the main logic so it can be called by both CLI and scheduler."""
    # Fetch Redfin properties with enhanced data
    properties = fetch_redfin_properties()
    if args.limit:
        properties = properties[:args.limit]
        logging.info("Limiting to %d properties", len(properties))

    rows = []
    skipped_count = 0
    failed_count = 0
    
    for i, prop in enumerate(properties,1):
        street = prop['street']
        redfin_sqft = prop['sqft']
        price = prop['price']
        lot_size_acres = prop['lot_size_acres']
        post_date = prop['post_date']
        source = prop['source']
        
        logging.info("[%d/%d] %s (Source: %s | Price: $%s | Posted: %s)", 
                    i, len(properties), street, source, 
                    f"{price:,}" if price > 0 else "N/A",
                    post_date or "N/A")
        
        try:
            # Get PID with robust error handling
            pid = arcgis_pid(street)
            if not pid:
                failed_count += 1
                logging.warning("→ Skipping %s - no PID found", street)
                continue
                
            # Get SCOUT data with robust error handling  
            full_text, html, jurisdiction = legal_for_pid(pid)
            
            # If SCOUT data completely failed, use fallback values but continue processing
            if not full_text:
                logging.warning("→ No SCOUT data for %s (PID: %s) - using fallback values", street, pid)
                full_text = f"PROPERTY: {street}"  # Minimal text for keyword analysis
                jurisdiction = "Unknown"
            
            # Extract lot size and square footage from SCOUT data (more reliable than Redfin)
            scout_lot_size_acres = extract_lot_size_from_scout(full_text)
            scout_sqft = extract_square_footage(full_text)
            
            # Use Redfin data as fallback if SCOUT data is missing
            if scout_lot_size_acres == 0.0 and lot_size_acres > 0:
                scout_lot_size_acres = lot_size_acres
                logging.info("→ Using Redfin lot size as fallback: %.3f acres", scout_lot_size_acres)
                
            if scout_sqft == 0 and redfin_sqft > 0:
                scout_sqft = redfin_sqft
                logging.info("→ Using Redfin sqft as fallback: %d sqft", scout_sqft)
            
            logging.info("→ SCOUT data: %d sqft | %.3f acres | %s jurisdiction", 
                        scout_sqft, scout_lot_size_acres, jurisdiction)
            
            # Extract legal description between 'Active' and 'Appraisal'
            legal_desc = ""
            try:
                if full_text:
                    start = full_text.index("Active") + len("Active")
                    end = full_text.index("Appraisal", start)
                    legal_desc = full_text[start:end].strip()
            except ValueError:
                legal_desc = full_text.strip() if full_text else f"Property at {street}"
            
            # Apply Aaron's filter: skip short plat and long plat properties
            if should_skip_property(legal_desc):
                skipped_count += 1
                logging.info("→ Skipped (contains short/long plat): %s", street)
                continue
            
            # Create the row with all data
            rows.append({
                "street": street,
                "pid": pid,
                "legal_description": legal_desc,
                "sqft": scout_sqft,  # SCOUT data with Redfin fallback
                "price": price,
                "lot_size_acres": scout_lot_size_acres,  # SCOUT data with Redfin fallback
                "post_date": post_date,
                "source": source,
                "jurisdiction": jurisdiction,
                "full_page_text": full_text,
                **enhanced_kw_counts(full_text, scout_sqft)  # Use best available sqft for keyword analysis
            })
            
        except Exception as e:
            failed_count += 1
            logging.error("→ Unexpected error processing %s: %s", street, str(e))
            logging.info("→ Continuing with next property...")
            continue
            
        time.sleep(0.3)   # polite throttle
    
    # Summary logging
    total_processed = len(properties)
    successful = len(rows)
    
    logging.info("═══ PROCESSING SUMMARY ═══")
    logging.info("Total properties found: %d", total_processed)
    logging.info("Successfully processed: %d", successful)
    if failed_count > 0:
        logging.info("Failed (network/timeout): %d", failed_count)
    if skipped_count > 0:
        logging.info("Skipped (short/long plat): %d", skipped_count)
    logging.info("Success rate: %.1f%%", (successful / total_processed * 100) if total_processed > 0 else 0)

    if not rows:
        logging.error("No data collected; exiting.")
        return  # Don't sys.exit() in scheduler mode

    df = pd.DataFrame(rows)
    
    # Search for Ranch properties automatically
    logging.info("🏠 Searching for Ranch properties >%d sqft...", args.ranch_min_sqft)
    ranch_results = search_scout_ranch_properties(args.ranch_min_sqft)
    ranch_df = pd.DataFrame(ranch_results) if ranch_results else pd.DataFrame()
    
    batch_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(f"scout_results_{batch_id}.xlsx")
    pdf_out = Path(f"scout_results_{batch_id}.pdf")
    
    # Create multiple sheets with enhanced visualizations
    summary_df = create_keyword_summary(df)
    stats_df = create_keyword_stats(df)
    lot_df = create_lot_analysis(df)
    
    # Create overview data
    overview_data = {
        'Total Properties Scraped': len(df),
        'Properties with Keywords': len(summary_df) if not summary_df.empty else 0,
        'Total Unique Keywords Found': len(stats_df) if not stats_df.empty else 0,
        'Most Common Keyword': stats_df.iloc[0]['keyword'] if not stats_df.empty else 'None',
        'Properties with Lot Numbers': len(lot_df) if not lot_df.empty else 0,
        'Date Generated': batch_id
    }
    
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        # Original detailed data
        df.to_excel(writer, sheet_name='Raw Data', index=False)
        
        # Keyword Summary - only properties with matches
        if not summary_df.empty:
            summary_df.to_excel(writer, sheet_name='Keyword Summary', index=False)
            logging.info("Created Keyword Summary with %d properties", len(summary_df))
        
        # Keyword Statistics - aggregate analysis
        if not stats_df.empty:
            stats_df.to_excel(writer, sheet_name='Keyword Stats', index=False)
            logging.info("Created Keyword Stats with %d keywords", len(stats_df))
        
        # Lot Analysis - specific to L0-L99
        if not lot_df.empty:
            lot_df.to_excel(writer, sheet_name='Lot Analysis', index=False)
            logging.info("Created Lot Analysis with %d properties", len(lot_df))
        
        # Ranch Properties - stubbed out for now
        # if not ranch_df.empty:
        #     ranch_df.to_excel(writer, sheet_name='Ranch Properties', index=False)
        #     logging.info("Created Ranch Properties sheet with %d properties", len(ranch_df))
        
        # Overview sheet
        overview_df = pd.DataFrame(list(overview_data.items()), columns=['Metric', 'Value'])
        overview_df.to_excel(writer, sheet_name='Overview', index=False)

    logging.info("Wrote %s (%d rows) with enhanced visualizations", out, len(df))
    
    # Create PDF report
    create_pdf_report(df, summary_df, stats_df, lot_df, overview_data, pdf_out)
    
    # Handle email/preview generation
    if not args.no_email:
        stats_summary = {
            'total_properties': len(df),
            'properties_with_keywords': len(summary_df) if not summary_df.empty else 0,
            'unique_keywords': len(stats_df) if not stats_df.empty else 0,
            'properties_with_lots': len(lot_df) if not lot_df.empty else 0
        }
        
        # Test email mode (default) or real email mode
        if args.test_email or (not args.send_email and not args.no_email):
            # Create HTML preview by default (safest option)
            preview_path = create_test_email_file(out, pdf_out, stats_summary)
            logging.info("📧 Email preview created! Open in browser: %s", preview_path)
            logging.info("💡 To send real emails: use --send-email flag")
        elif args.send_email:
            # Actually send email
            email_sent = send_email(out, pdf_out, stats_summary, args.provider)
            if not email_sent:
                logging.info("Email not sent. Files saved locally: %s, %s", out, pdf_out)
                # Create preview as fallback
                preview_path = create_test_email_file(out, pdf_out, stats_summary)
                logging.info("📧 Email preview created as fallback: %s", preview_path)
    else:
        logging.info("Email sending skipped. Files saved locally: %s, %s", out, pdf_out)

# ───── main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n","--limit",type=int,help="max properties to process")
    ap.add_argument("--no-email", action="store_true", help="skip sending email")
    ap.add_argument("--test-email", action="store_true", help="create HTML preview instead of sending email")
    ap.add_argument("--send-email", action="store_true", help="force send real email (overrides test mode)")
    ap.add_argument("--provider", choices=['gmail', 'outlook', 'yahoo', 'aol'], default='gmail',
                    help="email provider to use (default: gmail)")
    ap.add_argument("--ranch-min-sqft", type=int, default=1500, help="minimum square footage for Ranch search (default: 1500)")
    ap.add_argument("--schedule", action="store_true", help="run in scheduling mode - sends daily emails at 10am PST")
    args = ap.parse_args()

    # Check if running in scheduler mode
    if args.schedule:
        run_scheduler()
        return

    # Run once normally
    run_main_logic(args)

if __name__ == "__main__":
    main()
