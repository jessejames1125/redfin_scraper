#!/usr/bin/env python3
"""
redfin_spokane_to_scout.py
• Scrape Redfin active Spokane listings with COMPREHENSIVE field extraction (25+ property details)
• Resolve each street to a PID with SCOUT/PropertyLookup
• Pull the short plat / lot-block legal description from the SCOUT summary page
• Count keywords (incl. every "L0…L99") and export to Excel with enhanced visualizations
• Email results with Excel and PDF attachments

NEW COMPREHENSIVE FIELD EXTRACTION:
• Basic: price, sqft, beds/baths, property type, year built, days on market
• Financial: HOA fees, property taxes, monthly payment estimates, price per sqft
• Features: fireplace, pool/spa, basement, stories, heating/cooling, flooring
• Location: neighborhood, school district, utilities, walk score, view details
• Marketing: listing agent, MLS number, listing status, photo count, open house
• Additional: appliances, fence, garage details, previous price, and more!

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

import argparse, datetime as dt, logging, re, time, os, warnings
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
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

# ───── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,  # Normal logging level
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ───── constants ──────────────────────────────────────────────────────────────
HDRS          = {"User-Agent": "Mozilla/5.0"}

# Multiple Redfin URLs for different jurisdictions
# Note: Spokane Valley will be filtered out later
# Generate URLs for multiple pages of Spokane County results
SPOKANE_COUNTY_BASE = "https://www.redfin.com/county/3100/WA/Spokane-County/filter/sort=lo-days"
REDFIN_SOURCES = {
    "Spokane County Page 1": SPOKANE_COUNTY_BASE,
    "Spokane County Page 2": f"{SPOKANE_COUNTY_BASE}/page-2",
    "Spokane County Page 3": f"{SPOKANE_COUNTY_BASE}/page-3",
    "Spokane County Page 4": f"{SPOKANE_COUNTY_BASE}/page-4",
    "Spokane County Page 5": f"{SPOKANE_COUNTY_BASE}/page-5",
    "Spokane County Page 6": f"{SPOKANE_COUNTY_BASE}/page-6",
    "Spokane County Page 7": f"{SPOKANE_COUNTY_BASE}/page-7"
}

SLUG_RE       = re.compile(r"/WA/Spokane/([^/]+)/home")

SCOUT_LAYER   = ("https://gismo.spokanecounty.org/arcgis/rest/services/"
                 "SCOUT/PropertyLookup/MapServer/0/query")
SCOUT_SUMMARY = ("https://cp.spokanecounty.org/SCOUT/propertyinformation/"
                 "Summary.aspx?PID={} ")

# Updated keywords per Aaron's requirements
KEYWORDS_BASE = [
    " LT","LTS"," L ","LOTS","THRU"," TO ",
    # "AND","ALL",  # Commented out - too dominant in results
    "THROUGH","&",
    # ">=1500","RANCH",">=1500&RANCH"  # Commented out - not needed for keyword analysis
]
KEYWORDS      = KEYWORDS_BASE + [f"L{i}" for i in range(100)]   # L0 … L99

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

def extract_post_date_from_card(card, show_raw_text=False) -> str:
    """Extract post/listing date from Redfin property card with comprehensive debugging."""
    card_text = card.get_text()
    
    # Show full card text when debug flag is enabled
    if show_raw_text:
        print(f"\n{'='*50}")
        print(f"PROPERTY CARD RAW TEXT:")
        print(f"{'='*50}")
        print(card_text)
        print(f"{'='*50}")
        print(f"TEXT LENGTH: {len(card_text)} characters")
        print(f"{'='*50}\n")
    
    # Look for "days on Redfin" or "days on market" first - this is most reliable
    days_patterns = [
        r'(\d+)\s+days?\s+on\s+Redfin',        # "5 days on Redfin"
        r'(\d+)\s+days?\s+on\s+market',        # "5 days on market"  
        r'(\d+)\s+day\s+on\s+Redfin',          # "1 day on Redfin"
        r'(\d+)\s+day\s+on\s+market',          # "1 day on market"
        r'On\s+Redfin\s+(\d+)\s+days?',        # "On Redfin 5 days"
        r'On\s+market\s+(\d+)\s+days?',        # "On market 5 days"
    ]
    
    for pattern in days_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                days_ago = int(match.group(1))
                if 0 <= days_ago <= 365:  # Reasonable range
                    post_date = dt.datetime.now() - dt.timedelta(days=days_ago)
                    result = post_date.strftime('%m/%d/%Y')
                    logging.info("Found days pattern: %s -> %d days ago -> %s", match.group(0), days_ago, result)
                    return result
            except (ValueError, TypeError):
                continue
    
    # Look for status badges like "NEW TODAY", "NEW 2 HOURS AGO" etc. 
    # These are common on Redfin - be aggressive to catch edge cases
    status_patterns = [
        r'NEW\s+TODAY',                         # "NEW TODAY"
        r'NEW\s+(\d+)\s+HOURS?\s+AGO',         # "NEW 2 HOURS AGO"
        r'NEW\s+(\d+)\s+HRS?\s+AGO',           # "NEW 2 HRS AGO"  
        r'NEW\s+(\d+)\s+HOUR\s+AGO',           # "NEW 1 HOUR AGO"
        r'NEW\s+(\d+)\s+MINUTES?\s+AGO',       # "NEW 30 MINUTES AGO"
        r'NEW\s+(\d+)\s+MINS?\s+AGO',          # "NEW 30 MINS AGO"
        r'NEW\s+(\d+)\s+MIN\s+AGO',            # "NEW 6 MIN AGO"
        r'NEW\s+(\d+)\s+DAYS?\s+AGO',          # "NEW 3 DAYS AGO"
        r'NEW\s+(\d+)\s+DAY\s+AGO',            # "NEW 1 DAY AGO"
        r'NEW\s+YESTERDAY',                     # "NEW YESTERDAY"
        r'NEW\s+A\s+FEW\s+MINUTES?\s+AGO',     # "NEW A FEW MINUTES AGO"
        r'LISTED\s+TODAY',                      # "LISTED TODAY"
        r'LISTED\s+(\d+)\s+DAYS?\s+AGO',       # "LISTED 2 DAYS AGO"
        r'LISTED\s+YESTERDAY',                  # "LISTED YESTERDAY"
        r'JUST\s+LISTED',                       # "JUST LISTED" 
        r'RECENTLY\s+LISTED',                   # "RECENTLY LISTED"
    ]
    
    for pattern in status_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                now = dt.datetime.now()
                matched_text = match.group(0)
                
                if 'TODAY' in matched_text.upper():
                    result = now.strftime('%m/%d/%Y')
                    logging.info("Found status pattern: %s -> today -> %s", matched_text, result)
                    return result
                elif 'YESTERDAY' in matched_text.upper():
                    result = (now - dt.timedelta(days=1)).strftime('%m/%d/%Y')
                    logging.debug("Found status pattern: %s -> yesterday -> %s", matched_text, result)
                    return result
                elif 'JUST LISTED' in matched_text.upper() or 'RECENTLY LISTED' in matched_text.upper() or 'A FEW MINUTES' in matched_text.upper():
                    result = now.strftime('%m/%d/%Y')
                    logging.debug("Found status pattern: %s -> today -> %s", matched_text, result)
                    return result
                elif match.groups():
                    time_value = int(match.group(1))
                    if 'HOUR' in matched_text.upper() or 'HR' in matched_text.upper():
                        if time_value <= 24:  # Same day
                            result = now.strftime('%m/%d/%Y')
                            logging.debug("Found status pattern: %s -> %d hours ago (same day) -> %s", matched_text, time_value, result)
                            return result
                    elif 'MIN' in matched_text.upper():
                        result = now.strftime('%m/%d/%Y')  # Same day
                        logging.debug("Found status pattern: %s -> %d minutes ago (same day) -> %s", matched_text, time_value, result)
                        return result
                    elif 'DAY' in matched_text.upper():
                        if time_value <= 30:  # Reasonable range
                            post_date = now - dt.timedelta(days=time_value)
                            result = post_date.strftime('%m/%d/%Y')
                            logging.debug("Found status pattern: %s -> %d days ago -> %s", matched_text, time_value, result)
                            return result
            except (ValueError, TypeError, IndexError):
                continue
    
    # Look for explicit dates in various formats
    explicit_date_patterns = [
        r'Listed\s+(\d{1,2}/\d{1,2}/\d{4})',         # "Listed 12/25/2024"
        r'Posted\s+(\d{1,2}/\d{1,2}/\d{4})',         # "Posted 12/25/2024"
        r'Added\s+(\d{1,2}/\d{1,2}/\d{4})',          # "Added 12/25/2024"
        r'(\d{1,2}/\d{1,2}/\d{4})',                  # Just "12/25/2024"
        r'(\d{1,2}-\d{1,2}-\d{4})',                  # "12-25-2024"
        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})',  # "Dec 25, 2024"
    ]
    
    for pattern in explicit_date_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                if len(match.groups()) == 1:
                    date_str = match.group(1)
                    # Try different date formats
                    for date_format in ['%m/%d/%Y', '%m-%d-%Y', '%Y-%m-%d']:
                        try:
                            parsed_date = dt.datetime.strptime(date_str, date_format)
                            # Only accept dates from the past year
                            if (dt.datetime.now() - parsed_date).days <= 365 and parsed_date <= dt.datetime.now():
                                result = parsed_date.strftime('%m/%d/%Y')
                                logging.debug("Found explicit date: %s -> %s", date_str, result)
                                return result
                        except ValueError:
                            continue
                elif len(match.groups()) == 3:
                    # Month name format
                    month_name, day, year = match.groups()
                    month_names = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                                 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
                    try:
                        month_num = month_names.index(month_name.upper()) + 1
                        parsed_date = dt.datetime(int(year), month_num, int(day))
                        if (dt.datetime.now() - parsed_date).days <= 365 and parsed_date <= dt.datetime.now():
                            result = parsed_date.strftime('%m/%d/%Y')
                            logging.debug("Found month name date: %s %s %s -> %s", month_name, day, year, result)
                            return result
                    except (ValueError, IndexError):
                        continue
            except (ValueError, TypeError, IndexError):
                continue
    
    # Last resort: Look for any time indicators that suggest recency
    recency_indicators = [
        r'\bNEW\b',
        r'\bJUST\s+LISTED\b',
        r'\bRECENTLY\s+LISTED\b',
        r'\bFRESH\s+LISTING\b',
        r'\bPRICE\s+IMPROVEMENT\b',
        r'\bPRICE\s+REDUCED\b',
        r'\bBACK\s+ON\s+MARKET\b'
    ]
    
    for pattern in recency_indicators:
        if re.search(pattern, card_text, re.IGNORECASE):
            result = dt.datetime.now().strftime('%m/%d/%Y')
            logging.debug("Found recency indicator: %s -> today -> %s", pattern, result)
            return result
    
    # DEBUGGING: Log when we fall back to unknown  
    logging.debug("No date pattern matched for property. This is normal for older listings.")
    logging.debug("Card text length: %d characters", len(card_text))
    return "Unknown"  # Don't assume today's date if we can't find anything

def clean_date_string(date_str: str) -> str:
    """Clean up date string by removing unwanted characters."""
    if not date_str or date_str == "Unknown":
        return date_str
    
    # Remove common unwanted characters
    cleaned = date_str.strip()
    cleaned = re.sub(r'[^\d/\-]', '', cleaned)  # Keep only digits, /, and -
    
    # Validate the format
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', cleaned):
        return cleaned
    elif re.match(r'^\d{1,2}-\d{1,2}-\d{4}$', cleaned):
        # Convert to MM/DD/YYYY format
        try:
            parsed = dt.datetime.strptime(cleaned, '%m-%d-%Y')
            return parsed.strftime('%m/%d/%Y')
        except ValueError:
            pass
    
    return "Unknown"

def extract_bedrooms_from_card(card) -> int:
    """Extract number of bedrooms from Redfin property card."""
    card_text = card.get_text()
    
    # Look for bedroom patterns
    bedroom_patterns = [
        r'(\d+)\s*beds?\b',           # "3 beds" or "3 bed"
        r'(\d+)\s*BD\b',              # "3 BD"
        r'(\d+)\s*BR\b',              # "3 BR"
        r'(\d+)\s*BDRM\b',            # "3 BDRM"
        r'(\d+)\s*bedroom\b',         # "3 bedroom"
        r'(\d+)\s*bedrooms?\b',       # "3 bedrooms"
        r'Beds:?\s*(\d+)',            # "Beds: 3"
        r'Bedrooms:?\s*(\d+)',        # "Bedrooms: 3"
    ]
    
    for pattern in bedroom_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                beds = int(match.group(1))
                # Sanity check - reasonable bedroom count
                if 0 <= beds <= 20:
                    return beds
            except (ValueError, TypeError):
                continue
    
    return 0

def extract_bathrooms_from_card(card) -> float:
    """Extract number of bathrooms from Redfin property card."""
    card_text = card.get_text()
    
    # Look for bathroom patterns
    bathroom_patterns = [
        r'(\d+\.?\d*)\s*baths?\b',       # "2.5 baths" or "2 bath"
        r'(\d+\.?\d*)\s*BA\b',           # "2.5 BA"
        r'(\d+\.?\d*)\s*bathroom\b',     # "2 bathroom"
        r'(\d+\.?\d*)\s*bathrooms?\b',   # "2.5 bathrooms"
        r'Baths:?\s*(\d+\.?\d*)',        # "Baths: 2.5"
        r'Bathrooms:?\s*(\d+\.?\d*)',    # "Bathrooms: 2.5"
    ]
    
    for pattern in bathroom_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                baths = float(match.group(1))
                # Sanity check - reasonable bathroom count
                if 0 <= baths <= 20:
                    return baths
            except (ValueError, TypeError):
                continue
    
    return 0.0

def extract_property_type_from_card(card) -> str:
    """Extract property type from Redfin property card."""
    card_text = card.get_text()
    
    # Look for property type patterns
    property_types = [
        'Single Family',
        'Single-Family',
        'Townhouse',
        'Townhome',
        'Condo',
        'Condominium',
        'Multi-Family',
        'Duplex',
        'Triplex',
        'Fourplex',
        'Apartment',
        'Mobile Home',
        'Manufactured Home',
        'Vacant Land',
        'Land',
        'Commercial'
    ]
    
    # Check for each property type
    for prop_type in property_types:
        if re.search(rf'\b{re.escape(prop_type)}\b', card_text, re.IGNORECASE):
            return prop_type
    
    # Look for generic patterns
    generic_patterns = [
        r'House\b',
        r'Home\b',
        r'Residence\b'
    ]
    
    for pattern in generic_patterns:
        if re.search(pattern, card_text, re.IGNORECASE):
            return 'Single Family'  # Default assumption
    
    return 'Unknown'

def extract_year_built_from_card(card) -> int:
    """Extract year built from Redfin property card."""
    card_text = card.get_text()
    
    # Look for year built patterns
    year_patterns = [
        r'Built in (\d{4})',          # "Built in 1995"
        r'Built:?\s*(\d{4})',         # "Built: 1995"
        r'Year Built:?\s*(\d{4})',    # "Year Built: 1995"
        r'(\d{4})\s*Built',           # "1995 Built"
        r'Yr Built:?\s*(\d{4})',      # "Yr Built: 1995"
    ]
    
    for pattern in year_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                year = int(match.group(1))
                # Sanity check - reasonable year range
                current_year = dt.datetime.now().year
                if 1800 <= year <= current_year + 5:  # Allow for new construction
                    return year
            except (ValueError, TypeError):
                continue
    
    return 0

def extract_days_on_market_from_card(card) -> int:
    """Extract days on market from Redfin property card."""
    card_text = card.get_text()
    
    # Look for days on market patterns
    dom_patterns = [
        r'(\d+)\s*days?\s*on\s*Redfin',    # "5 days on Redfin"
        r'(\d+)\s*days?\s*on\s*market',    # "5 days on market"
        r'(\d+)\s*DOM\b',                  # "5 DOM"
        r'On market:?\s*(\d+)\s*days?',    # "On market: 5 days"
        r'Days on market:?\s*(\d+)',       # "Days on market: 5"
    ]
    
    for pattern in dom_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                days = int(match.group(1))
                # Sanity check - reasonable days on market
                if 0 <= days <= 3650:  # Max 10 years
                    return days
            except (ValueError, TypeError):
                continue
    
    return 0

def extract_garage_parking_from_card(card) -> str:
    """Extract garage/parking information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for garage/parking patterns
    garage_patterns = [
        r'(\d+)\s*-?\s*car\s*garage',      # "2-car garage" or "2 car garage"
        r'(\d+)\s*garage',                 # "2 garage"
        r'garage:?\s*(\d+)',               # "Garage: 2"
        r'(\d+)\s*bay\s*garage',           # "2 bay garage"
        r'(\d+)\s*stall\s*garage',         # "2 stall garage"
        r'parking:?\s*(\d+)',              # "Parking: 2"
        r'(\d+)\s*parking\s*spaces?',      # "2 parking spaces"
        r'(\d+)\s*spaces?',                # "2 spaces"
    ]
    
    for pattern in garage_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                spaces = int(match.group(1))
                # Sanity check - reasonable parking count
                if 0 <= spaces <= 20:
                    if 'garage' in pattern:
                        return f"{spaces}-car garage"
                    else:
                        return f"{spaces} parking spaces"
            except (ValueError, TypeError):
                continue
    
    # Look for text indicators
    parking_indicators = [
        'Attached Garage',
        'Detached Garage',
        'Carport',
        'Covered Parking',
        'No Garage',
        'Garage Available',
        'Parking Available'
    ]
    
    for indicator in parking_indicators:
        if re.search(rf'\b{re.escape(indicator)}\b', card_text, re.IGNORECASE):
            return indicator
    
    return 'Unknown'

def extract_mls_number_from_card(card) -> str:
    """Extract MLS number from Redfin property card."""
    card_text = card.get_text()
    
    # Look for MLS patterns
    mls_patterns = [
        r'MLS\s*#?\s*[:\-]?\s*([A-Z0-9]+)',      # "MLS #123456" or "MLS: 123456"
        r'MLS\s*ID\s*[:\-]?\s*([A-Z0-9]+)',      # "MLS ID: 123456"
        r'List\s*#\s*([A-Z0-9]+)',               # "List #123456"
        r'Listing\s*#\s*([A-Z0-9]+)',            # "Listing #123456"
        r'ID\s*[:\-]?\s*([A-Z0-9]{6,})',         # "ID: 123456"
    ]
    
    for pattern in mls_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            mls_id = match.group(1)
            if len(mls_id) >= 4:  # Reasonable MLS number length
                return mls_id
    
    return 'Unknown'

def extract_hoa_fee_from_card(card) -> str:
    """Extract HOA fee from Redfin property card."""
    card_text = card.get_text()
    
    # Look for HOA patterns
    hoa_patterns = [
        r'HOA\s*[:\-]?\s*\$([0-9,]+)(?:/mo|/month)?',     # "HOA: $150/mo"
        r'HOA\s*Fee\s*[:\-]?\s*\$([0-9,]+)',             # "HOA Fee: $150"
        r'Association\s*Fee\s*[:\-]?\s*\$([0-9,]+)',      # "Association Fee: $150"
        r'\$([0-9,]+)\s*HOA',                             # "$150 HOA"
        r'HOA\s*[:\-]?\s*([0-9,]+)',                      # "HOA: 150"
    ]
    
    for pattern in hoa_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                fee = match.group(1).replace(',', '')
                hoa_amount = int(fee)
                if 0 <= hoa_amount <= 10000:  # Reasonable HOA range
                    return f"${hoa_amount}"
            except (ValueError, TypeError):
                continue
    
    # Look for "No HOA" indicators
    no_hoa_patterns = [
        r'No\s*HOA',
        r'HOA\s*None',
        r'No\s*Association',
        r'HOA\s*N/A'
    ]
    
    for pattern in no_hoa_patterns:
        if re.search(pattern, card_text, re.IGNORECASE):
            return 'No HOA'
    
    return 'Unknown'

def extract_property_taxes_from_card(card) -> str:
    """Extract property tax information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for property tax patterns
    tax_patterns = [
        r'Property\s*Tax\s*[:\-]?\s*\$([0-9,]+)(?:/yr|/year)?',    # "Property Tax: $3,500/yr"
        r'Tax\s*[:\-]?\s*\$([0-9,]+)(?:/yr|/year)?',              # "Tax: $3,500/yr"
        r'Annual\s*Tax\s*[:\-]?\s*\$([0-9,]+)',                   # "Annual Tax: $3,500"
        r'Taxes\s*[:\-]?\s*\$([0-9,]+)',                          # "Taxes: $3,500"
        r'\$([0-9,]+)\s*(?:property\s*)?tax',                     # "$3,500 property tax"
    ]
    
    for pattern in tax_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                tax_str = match.group(1).replace(',', '')
                tax_amount = int(tax_str)
                if 0 <= tax_amount <= 100000:  # Reasonable tax range
                    return f"${tax_amount:,}"
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_stories_from_card(card) -> str:
    """Extract number of stories from Redfin property card."""
    card_text = card.get_text()
    
    # Look for story patterns
    story_patterns = [
        r'(\d+)\s*Story',                    # "2 Story"
        r'(\d+)\s*Stories',                  # "2 Stories"
        r'(\d+)\s*Level',                    # "2 Level"
        r'(\d+)\s*Levels',                   # "2 Levels"
        r'Stories?\s*[:\-]?\s*(\d+)',        # "Stories: 2"
        r'Levels?\s*[:\-]?\s*(\d+)',         # "Levels: 2"
    ]
    
    for pattern in story_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                stories = int(match.group(1))
                if 1 <= stories <= 5:  # Reasonable story count
                    return str(stories)
            except (ValueError, TypeError):
                continue
    
    # Look for text indicators
    story_indicators = [
        'Single Story',
        'One Story',
        'Two Story',
        'Multi-Level',
        'Split Level',
        'Tri-Level'
    ]
    
    for indicator in story_indicators:
        if re.search(rf'\b{re.escape(indicator)}\b', card_text, re.IGNORECASE):
            return indicator
    
    return 'Unknown'

def extract_basement_from_card(card) -> str:
    """Extract basement information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for basement patterns
    basement_patterns = [
        'Finished Basement',
        'Unfinished Basement',
        'Partial Basement',
        'Full Basement',
        'Walkout Basement',
        'Daylight Basement',
        'Basement'
    ]
    
    for pattern in basement_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            return pattern
    
    # Look for "No Basement" indicators
    no_basement_patterns = [
        'No Basement',
        'Slab Foundation',
        'Crawl Space'
    ]
    
    for pattern in no_basement_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            return pattern
    
    return 'Unknown'

def extract_heating_cooling_from_card(card) -> str:
    """Extract heating and cooling system information."""
    card_text = card.get_text()
    
    # Look for HVAC patterns
    hvac_patterns = [
        'Central Air',
        'Forced Air',
        'Heat Pump',
        'Radiant Heat',
        'Baseboard Heat',
        'Geothermal',
        'Electric Heat',
        'Gas Heat',
        'Oil Heat',
        'Solar Heat',
        'AC',
        'A/C',
        'Air Conditioning',
        'Heating',
        'Cooling'
    ]
    
    found_systems = []
    for pattern in hvac_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_systems.append(pattern)
    
    if found_systems:
        return ', '.join(found_systems[:3])  # Limit to first 3 to avoid clutter
    
    return 'Unknown'

def extract_flooring_from_card(card) -> str:
    """Extract flooring information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for flooring patterns
    flooring_patterns = [
        'Hardwood',
        'Laminate',
        'Vinyl',
        'Carpet',
        'Tile',
        'Stone',
        'Concrete',
        'Bamboo',
        'Cork',
        'Linoleum',
        'Marble',
        'Granite',
        'Engineered Wood'
    ]
    
    found_flooring = []
    for pattern in flooring_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_flooring.append(pattern)
    
    if found_flooring:
        return ', '.join(found_flooring[:3])  # Limit to first 3
    
    return 'Unknown'

def extract_appliances_from_card(card) -> str:
    """Extract appliances information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for appliance patterns
    appliance_patterns = [
        'Refrigerator',
        'Dishwasher',
        'Washer',
        'Dryer',
        'Microwave',
        'Oven',
        'Stove',
        'Range',
        'Disposal',
        'Freezer',
        'Wine Cooler',
        'All Appliances'
    ]
    
    found_appliances = []
    for pattern in appliance_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_appliances.append(pattern)
    
    if found_appliances:
        return ', '.join(found_appliances[:4])  # Limit to first 4
    
    return 'Unknown'

def extract_fireplace_from_card(card) -> str:
    """Extract fireplace information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for fireplace patterns
    fireplace_patterns = [
        r'(\d+)\s*Fireplace',                # "2 Fireplace"
        r'(\d+)\s*Fireplaces',               # "2 Fireplaces"
        r'Fireplace\s*[:\-]?\s*(\d+)',       # "Fireplace: 2"
        r'Fireplaces\s*[:\-]?\s*(\d+)',      # "Fireplaces: 2"
    ]
    
    for pattern in fireplace_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                count = int(match.group(1))
                if 1 <= count <= 10:  # Reasonable fireplace count
                    return f"{count} Fireplace{'s' if count > 1 else ''}"
            except (ValueError, TypeError):
                continue
    
    # Look for fireplace types
    fireplace_types = [
        'Wood Fireplace',
        'Gas Fireplace',
        'Electric Fireplace',
        'Fireplace',
        'Wood Burning',
        'Gas Burning'
    ]
    
    for ftype in fireplace_types:
        if re.search(rf'\b{re.escape(ftype)}\b', card_text, re.IGNORECASE):
            return ftype
    
    # Look for "No Fireplace"
    if re.search(r'No\s*Fireplace', card_text, re.IGNORECASE):
        return 'No Fireplace'
    
    return 'Unknown'

def extract_pool_spa_from_card(card) -> str:
    """Extract pool and spa information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for pool/spa patterns
    pool_spa_patterns = [
        'Swimming Pool',
        'Pool',
        'Spa',
        'Hot Tub',
        'Jacuzzi',
        'In-Ground Pool',
        'Above Ground Pool',
        'Heated Pool',
        'Saltwater Pool'
    ]
    
    found_features = []
    for pattern in pool_spa_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_features.append(pattern)
    
    if found_features:
        return ', '.join(found_features[:3])  # Limit to first 3
    
    return 'Unknown'

def extract_view_from_card(card) -> str:
    """Extract view information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for view patterns
    view_patterns = [
        'Mountain View',
        'Water View',
        'City View',
        'Lake View',
        'River View',
        'Golf Course View',
        'Park View',
        'Greenbelt View',
        'Valley View',
        'Panoramic View',
        'Territorial View',
        'Partial View',
        'Peek View',
        'View'
    ]
    
    found_views = []
    for pattern in view_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_views.append(pattern)
    
    if found_views:
        return ', '.join(found_views[:3])  # Limit to first 3
    
    return 'Unknown'

def extract_listing_agent_from_card(card) -> str:
    """Extract listing agent information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for agent patterns
    agent_patterns = [
        r'Listed\s*by\s*([A-Za-z\s\.,]+)',       # "Listed by John Doe"
        r'Agent\s*[:\-]?\s*([A-Za-z\s\.,]+)',    # "Agent: John Doe"
        r'Listing\s*Agent\s*[:\-]?\s*([A-Za-z\s\.,]+)',  # "Listing Agent: John Doe"
        r'Contact\s*([A-Za-z\s\.,]+)',           # "Contact John Doe"
    ]
    
    for pattern in agent_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            agent = match.group(1).strip()
            # Clean up common suffixes
            agent = re.sub(r'\s*(Realty|Real Estate|Realtor|Agent).*$', '', agent, flags=re.IGNORECASE)
            if len(agent) > 3 and len(agent) < 50:  # Reasonable agent name length
                return agent
    
    return 'Unknown'

def extract_listing_status_from_card(card) -> str:
    """Extract listing status from Redfin property card."""
    card_text = card.get_text()
    
    # Look for status patterns
    status_patterns = [
        'Active',
        'Pending',
        'Under Contract',
        'Sold',
        'Off Market',
        'Withdrawn',
        'Expired',
        'Coming Soon',
        'New',
        'Price Reduced',
        'Back on Market',
        'Contingent'
    ]
    
    for pattern in status_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            return pattern
    
    return 'Active'  # Default assumption for Redfin listings

def extract_price_per_sqft_from_card(card) -> str:
    """Extract price per square foot from Redfin property card."""
    card_text = card.get_text()
    
    # Look for price per sqft patterns
    price_sqft_patterns = [
        r'\$([0-9,]+)\s*/?s?q?f?t?',           # "$150/sqft" or "$150 sqft"
        r'([0-9,]+)\s*/?s?q?f?t?',             # "150/sqft" or "150 sqft"
        r'Price\s*per\s*sq\s*ft\s*[:\-]?\s*\$?([0-9,]+)',  # "Price per sq ft: $150"
    ]
    
    for pattern in price_sqft_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                price_str = match.group(1).replace(',', '')
                price = int(price_str)
                if 50 <= price <= 1000:  # Reasonable price per sqft range
                    return f"${price}"
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_school_district_from_card(card) -> str:
    """Extract school district information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for school district patterns
    school_patterns = [
        r'School\s*District\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',     # "School District: ABC"
        r'District\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',             # "District: ABC"
        r'Schools?\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',             # "School: ABC"
        r'Elementary\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',           # "Elementary: ABC"
        r'Middle\s*School\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',      # "Middle School: ABC"
        r'High\s*School\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',        # "High School: ABC"
    ]
    
    for pattern in school_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            school = match.group(1).strip()
            if len(school) > 3 and len(school) < 100:  # Reasonable school name length
                return school
    
    return 'Unknown'

def extract_utilities_from_card(card) -> str:
    """Extract utilities information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for utility patterns
    utility_patterns = [
        'Public Water',
        'Well Water',
        'City Water',
        'Public Sewer',
        'Septic',
        'Private Sewer',
        'Electric',
        'Gas',
        'Propane',
        'Oil',
        'Solar',
        'Cable Ready',
        'Fiber Optic',
        'High Speed Internet'
    ]
    
    found_utilities = []
    for pattern in utility_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_utilities.append(pattern)
    
    if found_utilities:
        return ', '.join(found_utilities[:4])  # Limit to first 4
    
    return 'Unknown'

def extract_neighborhood_from_card(card) -> str:
    """Extract neighborhood/subdivision information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for neighborhood patterns
    neighborhood_patterns = [
        r'Neighborhood\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',         # "Neighborhood: ABC"
        r'Subdivision\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',          # "Subdivision: ABC"
        r'Community\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',            # "Community: ABC"
        r'Development\s*[:\-]?\s*([A-Za-z0-9\s\-]+)',          # "Development: ABC"
    ]
    
    for pattern in neighborhood_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            neighborhood = match.group(1).strip()
            if len(neighborhood) > 3 and len(neighborhood) < 100:  # Reasonable name length
                return neighborhood
    
    return 'Unknown'

def extract_open_house_from_card(card) -> str:
    """Extract open house information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for open house patterns
    open_house_patterns = [
        r'Open\s*House\s*[:\-]?\s*([A-Za-z0-9\s\-\/,:]+)',     # "Open House: Sat 1-3pm"
        r'Open\s*([A-Za-z0-9\s\-\/,:]+)',                      # "Open Sat 1-3pm"
        r'Tour\s*[:\-]?\s*([A-Za-z0-9\s\-\/,:]+)',             # "Tour: Available"
    ]
    
    for pattern in open_house_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            open_house = match.group(1).strip()
            if len(open_house) > 3 and len(open_house) < 100:  # Reasonable length
                return open_house
    
    # Look for simple indicators
    open_house_indicators = [
        'Virtual Tour',
        'Online Tour',
        '3D Tour',
        'Video Tour',
        'Open House',
        'Tour Available'
    ]
    
    for indicator in open_house_indicators:
        if re.search(rf'\b{re.escape(indicator)}\b', card_text, re.IGNORECASE):
            return indicator
    
    return 'Unknown'

def extract_previous_price_from_card(card) -> str:
    """Extract previous/original price information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for previous price patterns
    price_patterns = [
        r'Was\s*\$([0-9,]+)',                    # "Was $450,000"
        r'Originally\s*\$([0-9,]+)',             # "Originally $450,000"
        r'Previous\s*Price\s*[:\-]?\s*\$([0-9,]+)',  # "Previous Price: $450,000"
        r'Reduced\s*from\s*\$([0-9,]+)',         # "Reduced from $450,000"
        r'Price\s*Drop\s*[:\-]?\s*\$([0-9,]+)',  # "Price Drop: $450,000"
    ]
    
    for pattern in price_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                price_str = match.group(1).replace(',', '')
                price = int(price_str)
                if 50000 <= price <= 50000000:  # Reasonable price range
                    return f"${price:,}"
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_walk_score_from_card(card) -> str:
    """Extract walk score information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for walk score patterns
    walk_score_patterns = [
        r'Walk\s*Score\s*[:\-]?\s*(\d+)',        # "Walk Score: 75"
        r'Walkability\s*[:\-]?\s*(\d+)',         # "Walkability: 75"
        r'(\d+)\s*Walk\s*Score',                 # "75 Walk Score"
    ]
    
    for pattern in walk_score_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                score = int(match.group(1))
                if 0 <= score <= 100:  # Walk score range
                    return str(score)
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_monthly_payment_from_card(card) -> str:
    """Extract estimated monthly payment from Redfin property card."""
    card_text = card.get_text()
    
    # Look for monthly payment patterns
    payment_patterns = [
        r'Monthly\s*Payment\s*[:\-]?\s*\$([0-9,]+)',     # "Monthly Payment: $2,500"
        r'Est\s*Payment\s*[:\-]?\s*\$([0-9,]+)',         # "Est Payment: $2,500"
        r'Payment\s*[:\-]?\s*\$([0-9,]+)/mo',            # "Payment: $2,500/mo"
        r'\$([0-9,]+)/mo',                               # "$2,500/mo"
    ]
    
    for pattern in payment_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                payment_str = match.group(1).replace(',', '')
                payment = int(payment_str)
                if 500 <= payment <= 50000:  # Reasonable payment range
                    return f"${payment:,}"
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_photo_count_from_card(card) -> str:
    """Extract photo count from Redfin property card."""
    card_text = card.get_text()
    
    # Look for photo count patterns
    photo_patterns = [
        r'(\d+)\s*Photo',                     # "25 Photo"
        r'(\d+)\s*Photos',                    # "25 Photos"
        r'(\d+)\s*Image',                     # "25 Image"
        r'(\d+)\s*Images',                    # "25 Images"
        r'Photos?\s*[:\-]?\s*(\d+)',          # "Photos: 25"
    ]
    
    for pattern in photo_patterns:
        match = re.search(pattern, card_text, re.IGNORECASE)
        if match:
            try:
                count = int(match.group(1))
                if 0 <= count <= 200:  # Reasonable photo count
                    return str(count)
            except (ValueError, TypeError):
                continue
    
    return 'Unknown'

def extract_fence_from_card(card) -> str:
    """Extract fence information from Redfin property card."""
    card_text = card.get_text()
    
    # Look for fence patterns
    fence_patterns = [
        'Fenced Yard',
        'Fenced',
        'Privacy Fence',
        'Chain Link Fence',
        'Wood Fence',
        'Vinyl Fence',
        'Partial Fence',
        'Fully Fenced',
        'Back Yard Fenced',
        'Front Yard Fenced'
    ]
    
    found_fencing = []
    for pattern in fence_patterns:
        if re.search(rf'\b{re.escape(pattern)}\b', card_text, re.IGNORECASE):
            found_fencing.append(pattern)
    
    if found_fencing:
        return ', '.join(found_fencing[:2])  # Limit to first 2
    
    return 'Unknown'

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
                post_date = clean_date_string(extract_post_date_from_card(card, args.show_raw_text))
                
                # In raw text mode, skip the rest of processing for this property
                if args.show_raw_text:
                    # Show only first 5 properties by default in raw text mode
                    if i >= 5:
                        print(f"\n✅ Shown first 5 properties. Use --limit to see more.")
                        return
                    continue
                
                # Extract additional property details
                bedrooms = extract_bedrooms_from_card(card)
                bathrooms = extract_bathrooms_from_card(card)
                property_type = extract_property_type_from_card(card)
                year_built = extract_year_built_from_card(card)
                days_on_market = extract_days_on_market_from_card(card)
                garage_parking = extract_garage_parking_from_card(card)
                
                # Extract ALL NEW FIELDS for comprehensive data
                mls_number = extract_mls_number_from_card(card)
                hoa_fee = extract_hoa_fee_from_card(card)
                property_taxes = extract_property_taxes_from_card(card)
                stories = extract_stories_from_card(card)
                basement = extract_basement_from_card(card)
                heating_cooling = extract_heating_cooling_from_card(card)
                flooring = extract_flooring_from_card(card)
                appliances = extract_appliances_from_card(card)
                fireplace = extract_fireplace_from_card(card)
                pool_spa = extract_pool_spa_from_card(card)
                view = extract_view_from_card(card)
                listing_agent = extract_listing_agent_from_card(card)
                listing_status = extract_listing_status_from_card(card)
                price_per_sqft = extract_price_per_sqft_from_card(card)
                school_district = extract_school_district_from_card(card)
                utilities = extract_utilities_from_card(card)
                neighborhood = extract_neighborhood_from_card(card)
                open_house = extract_open_house_from_card(card)
                previous_price = extract_previous_price_from_card(card)
                walk_score = extract_walk_score_from_card(card)
                monthly_payment = extract_monthly_payment_from_card(card)
                photo_count = extract_photo_count_from_card(card)
                fence = extract_fence_from_card(card)
                
                all_properties.append({
                    # Original fields
                    'street': street,
                    'sqft': sqft,
                    'price': price,
                    'lot_size_acres': lot_size_acres,
                    'post_date': post_date,
                    'bedrooms': bedrooms,
                    'bathrooms': bathrooms,
                    'property_type': property_type,
                    'year_built': year_built,
                    'days_on_market': days_on_market,
                    'garage_parking': garage_parking,
                    'source': source_name,
                    
                    # NEW COMPREHENSIVE FIELDS
                    'mls_number': mls_number,
                    'hoa_fee': hoa_fee,
                    'property_taxes': property_taxes,
                    'stories': stories,
                    'basement': basement,
                    'heating_cooling': heating_cooling,
                    'flooring': flooring,
                    'appliances': appliances,
                    'fireplace': fireplace,
                    'pool_spa': pool_spa,
                    'view': view,
                    'listing_agent': listing_agent,
                    'listing_status': listing_status,
                    'price_per_sqft': price_per_sqft,
                    'school_district': school_district,
                    'utilities': utilities,
                    'neighborhood': neighborhood,
                    'open_house': open_house,
                    'previous_price': previous_price,
                    'walk_score': walk_score,
                    'monthly_payment': monthly_payment,
                    'photo_count': photo_count,
                    'fence': fence
                })
            
            logging.info("Found %d properties from %s", 
                        len([p for p in all_properties if p['source'] == source_name]), source_name)
                        
        except Exception as e:
            logging.error("Error fetching from %s: %s", source_name, str(e))
            continue
    
    logging.info("Total properties found: %d", len(all_properties))
    return all_properties



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
    """Create analysis specifically for L0-L99 lot keywords with enhanced property details."""
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
            # Format price nicely
            price_str = f"${row['price']:,}" if row['price'] > 0 else "N/A"
            # Format acres with 3 decimal places
            acres_str = f"{row['lot_size_acres']:.3f}" if row['lot_size_acres'] > 0 else "N/A"
            
            analysis.append({
                'street': row['street'],
                'pid': row['pid'],
                'price': price_str,
                'acres': acres_str,
                'legal_description': row['legal_description'][:150] + '...' if len(row['legal_description']) > 150 else row['legal_description'],
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
                    
                    <h4>📖 FEATURE EXPLANATIONS:</h4>
                    <p><strong>📋 Keyword Summary:</strong> Shows only properties that contain specific keywords in their legal descriptions. Keywords include lot references (L1, L2, etc.), lot-related terms (LT, LTS, LOTS, THRU, TO), and subdivision indicators. This helps identify multi-lot properties or lots that can be subdivided.</p>
                    
                    <p><strong>📊 Keyword Stats:</strong> Provides aggregate statistics showing how frequently each keyword appears across all properties. Shows total occurrences, number of properties containing each keyword, and maximum occurrences in a single property. This helps identify the most common lot patterns in your market.</p>
                    
                    <p><strong>🏠 Lot Analysis:</strong> Focuses specifically on L0-L99 lot number references (like "L1", "L15", etc.). Shows which properties reference specific lot numbers and how many unique lots each property contains. This is crucial for identifying subdivision opportunities and understanding lot configurations.</p>
                </div>
                
                <div class="attachments">
                    <h4>📎 Attachments:</h4>
                    <ul>
                        <li>📊 <span class="file">{excel_path.name}</span> - Excel file with 6 sheets: Raw Data, All Redfin Fields (25+ property details), Keyword Summary, Keyword Stats, Lot Analysis, and Overview</li>
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

Your Spokane real estate keyword analysis has completed successfully with comprehensive property data extraction.

SUMMARY:
• Total properties analyzed: {stats_summary.get('total_properties', 'N/A')}
• Properties with keywords: {stats_summary.get('properties_with_keywords', 'N/A')}
• Unique keywords found: {stats_summary.get('unique_keywords', 'N/A')}
• Properties with lot numbers: {stats_summary.get('properties_with_lots', 'N/A')}

NEW BOSS REQUIREMENTS IMPLEMENTED:
✅ Added Spokane County listings (in addition to City of Spokane)
✅ Filtered OUT all Spokane Valley properties 
✅ Only includes properties with lots > 0.25 acres
✅ Sorted by date (newest listings first)
✅ Enhanced date extraction from Redfin

FEATURE EXPLANATIONS:

📋 KEYWORD SUMMARY: Shows only properties that contain specific keywords in their legal descriptions. Keywords include lot references (L1, L2, etc.), lot-related terms (LT, LTS, LOTS, THRU, TO), and subdivision indicators. This helps identify multi-lot properties or lots that can be subdivided.

📊 KEYWORD STATS: Provides aggregate statistics showing how frequently each keyword appears across all properties. Shows total occurrences, number of properties containing each keyword, and maximum occurrences in a single property. This helps identify the most common lot patterns in your market.

🏠 LOT ANALYSIS: Focuses specifically on L0-L99 lot number references (like "L1", "L15", etc.). Shows which properties reference specific lot numbers and how many unique lots each property contains. This is crucial for identifying subdivision opportunities and understanding lot configurations.

NEW FEATURE: All Redfin Fields extracted including:
• Basic info (price, sqft, beds/baths, year built)
• Financial (HOA fees, property taxes, monthly payment)
• Features (fireplace, pool, basement, stories)
• Location (neighborhood, school district, utilities)
• Marketing (listing agent, MLS number, photos)
• And 20+ additional property details!

Attachments:
📊 Excel file with 6 sheets: Raw Data, All Redfin Fields (25+ property details), Keyword Summary, Keyword Stats, Lot Analysis, and Overview
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
    """Run the report generation and email sending (scheduled every 3 days)."""
    logging.info("🕙 Running scheduled report at 10am PST...")
    
    # Create a mock args object for scheduled runs
    class MockArgs:
        limit = None
        no_email = False
        test_email = False
        send_email = True  # Always send real emails in scheduled mode
        provider = 'gmail'
    
    args = MockArgs()
    
    try:
        # Run the main logic
        run_main_logic(args)
        logging.info("✅ Scheduled daily report completed successfully")
    except Exception as e:
        logging.error("❌ Scheduled daily report failed: %s", str(e))



def run_scheduler():
    """Run the scheduling system."""
    # Set up Pacific Time zone
    pst = pytz.timezone('US/Pacific')
    
    logging.info("🕐 Starting email automation scheduler...")
    logging.info("📅 Reports will be sent every 3 days at 10:00 AM PST")
    logging.info("⏸️  Press Ctrl+C to stop the scheduler")
    
    # Schedule to run every 3 days at 10 AM PST
    schedule.every(3).days.at("10:00").do(run_daily_report)
    
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
    
    # Special mode: Just show raw text without other logging
    if args.show_raw_text:
        print("🔍 RAW TEXT DEBUG MODE - Showing full card text from each property")
        print("=" * 80)
        # Suppress most logging for clean output
        logging.getLogger().setLevel(logging.WARNING)
    
    # Fetch Redfin properties with enhanced data
    properties = fetch_redfin_properties()
    if args.limit:
        properties = properties[:args.limit]
        logging.info("Limiting to %d properties", len(properties))

    rows = []
    skipped_count = 0
    failed_count = 0
    
    for i, prop in enumerate(properties,1):
        if args.show_raw_text:
            print(f"\n🏠 PROPERTY #{i}: {prop['street']}")
            print(f"   Source: {prop['source']} | Price: ${prop['price']:,}" if prop['price'] > 0 else f"   Source: {prop['source']} | Price: N/A")
        street = prop['street']
        redfin_sqft = prop['sqft']
        price = prop['price']
        lot_size_acres = prop['lot_size_acres']
        post_date = prop['post_date']
        source = prop['source']
        bedrooms = prop['bedrooms']
        bathrooms = prop['bathrooms']
        property_type = prop['property_type']
        year_built = prop['year_built']
        days_on_market = prop['days_on_market']
        garage_parking = prop['garage_parking']
        
        logging.info("[%d/%d] %s (Source: %s | Price: $%s | %dBR/%sBA | %s | Posted: %s)", 
                    i, len(properties), street, source, 
                    f"{price:,}" if price > 0 else "N/A",
                    bedrooms, bathrooms, property_type,
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
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "property_type": property_type,
                "year_built": year_built,
                "days_on_market": days_on_market,
                "garage_parking": garage_parking,
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
    
    # APPLY BOSS'S FILTERS BEFORE FINALIZING
    logging.info("═══ APPLYING BOSS'S FILTERS ═══")
    pre_filter_count = len(rows)
    
    # Log jurisdictions found before filtering
    jurisdictions = {}
    for row in rows:
        jurisdiction = row['jurisdiction']
        jurisdictions[jurisdiction] = jurisdictions.get(jurisdiction, 0) + 1
    
    logging.info("Jurisdictions found before filtering:")
    for jurisdiction, count in sorted(jurisdictions.items()):
        logging.info("  %s: %d properties", jurisdiction, count)
    
    # Filter 1: Remove Spokane Valley properties (check jurisdiction, source, AND street address)
    def is_spokane_valley_property(row):
        """Check if property is in Spokane Valley based on multiple criteria."""
        # Check jurisdiction (from SCOUT data)
        if 'VALLEY' in row['jurisdiction'].upper():
            return True
        
        # Check source (which Redfin page it came from)
        if 'VALLEY' in row['source'].upper():
            return True
            
        # Check street address itself
        if 'SPOKANE VALLEY' in row['street'].upper():
            return True
            
        return False
    
    rows = [row for row in rows if not is_spokane_valley_property(row)]
    spokane_valley_removed = pre_filter_count - len(rows)
    if spokane_valley_removed > 0:
        logging.info("Removed %d Spokane Valley properties", spokane_valley_removed)
    
    # Filter 2: Only keep properties > 0.25 acres
    pre_acreage_count = len(rows)
    rows = [row for row in rows if row['lot_size_acres'] > 0.25]
    small_lots_removed = pre_acreage_count - len(rows)
    if small_lots_removed > 0:
        logging.info("Removed %d properties with lots <= 0.25 acres", small_lots_removed)
    
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
    if spokane_valley_removed > 0:
        logging.info("Filtered out (Spokane Valley): %d", spokane_valley_removed)
    if small_lots_removed > 0:
        logging.info("Filtered out (lots <= 0.25 acres): %d", small_lots_removed)
    logging.info("Final count after filters: %d", successful)
    logging.info("Success rate: %.1f%%", (successful / total_processed * 100) if total_processed > 0 else 0)

    if not rows:
        logging.error("No data collected; exiting.")
        return  # Don't sys.exit() in scheduler mode

    df = pd.DataFrame(rows)
    
    # SORT BY DATE - newest listings first
    logging.info("═══ SORTING BY DATE ═══")
    # Convert post_date to datetime for proper sorting
    def parse_date(date_str):
        if not date_str or date_str == '':
            return dt.datetime.min  # Put unknown dates at the end
        try:
            return dt.datetime.strptime(date_str, '%m/%d/%Y')
        except:
            return dt.datetime.min  # Fallback for invalid dates
    
    df['post_date_parsed'] = df['post_date'].apply(parse_date)
    df = df.sort_values(['post_date_parsed'], ascending=[False])  # Newest first
    df = df.drop('post_date_parsed', axis=1)  # Remove temp column
    
    logging.info("Properties sorted by date - newest listings first")
    if len(df) > 0:
        newest_date = df.iloc[0]['post_date'] if df.iloc[0]['post_date'] else 'Unknown'
        oldest_date = df.iloc[-1]['post_date'] if df.iloc[-1]['post_date'] else 'Unknown'
        logging.info("Date range: %s (newest) to %s (oldest)", newest_date, oldest_date)
    

    
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
        'Total Redfin Fields Extracted': len([col for col in df.columns if col not in ['full_page_text', 'legal_description', 'pid'] + [f"L{i}" for i in range(100)] + KEYWORDS_BASE]),
        'Properties with Keywords': len(summary_df) if not summary_df.empty else 0,
        'Total Unique Keywords Found': len(stats_df) if not stats_df.empty else 0,
        'Most Common Keyword': stats_df.iloc[0]['keyword'] if not stats_df.empty else 'None',
        'Properties with Lot Numbers': len(lot_df) if not lot_df.empty else 0,
        'Date Generated': batch_id
    }
    
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        # Original detailed data
        df.to_excel(writer, sheet_name='Raw Data', index=False)
        
        # Create All Redfin Fields sheet with comprehensive property data
        all_redfin_columns = [
            'street', 'price', 'sqft', 'lot_size_acres', 'bedrooms', 'bathrooms', 
            'property_type', 'year_built', 'days_on_market', 'post_date',
            'mls_number', 'hoa_fee', 'property_taxes', 'stories', 'basement',
            'heating_cooling', 'flooring', 'appliances', 'fireplace', 'pool_spa',
            'view', 'listing_agent', 'listing_status', 'price_per_sqft',
            'school_district', 'utilities', 'neighborhood', 'open_house',
            'previous_price', 'walk_score', 'monthly_payment', 'photo_count',
            'fence', 'garage_parking', 'source', 'jurisdiction'
        ]
        
        # Select only the columns that exist in the dataframe
        existing_columns = [col for col in all_redfin_columns if col in df.columns]
        all_redfin_df = df[existing_columns].copy()
        
        # Reorder columns to put most important ones first
        priority_columns = ['street', 'price', 'sqft', 'bedrooms', 'bathrooms', 'property_type', 'year_built', 'post_date']
        other_columns = [col for col in existing_columns if col not in priority_columns]
        ordered_columns = [col for col in priority_columns if col in existing_columns] + other_columns
        
        all_redfin_df = all_redfin_df[ordered_columns]
        all_redfin_df.to_excel(writer, sheet_name='All Redfin Fields', index=False)
        logging.info("Created All Redfin Fields sheet with %d properties and %d fields", len(all_redfin_df), len(ordered_columns))
        
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

    ap.add_argument("--schedule", action="store_true", help="run in scheduling mode - sends daily emails at 10am PST")
    ap.add_argument("--show-raw-text", action="store_true", help="debug mode: show full raw text that code sees from each property card")
    args = ap.parse_args()

    # Check if running in scheduler mode
    if args.schedule:
        run_scheduler()
        return

    # Run once normally
    run_main_logic(args)

if __name__ == "__main__":
    main()
