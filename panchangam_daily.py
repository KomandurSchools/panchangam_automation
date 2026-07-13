# -*- coding: utf-8 -*-
"""
Daily Panchangam fetcher + image generator + WhatsApp sender.

Fetches today's Panchangam for Tirupati, Andhra Pradesh, India from
drikpanchang.com, renders a clean summary card in English, Telugu, and
Tamil, and sends each as a WhatsApp image via the TextMeBot API.

Configuration is via environment variables (set as GitHub Actions secrets):
    TEXTMEBOT_APIKEY   - your TextMeBot API key
    RECIPIENT_NUMBERS  - comma-separated WhatsApp numbers with country code,
                          e.g. "+919246998931,+919494403789"
    GEONAME_ID         - drikpanchang.com geoname-id for the location
                          (defaults to 1254360 = Tirupati, Andhra Pradesh)
"""
import os
import re
import sys
import time
import base64
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

IST = timezone(timedelta(hours=5, minutes=30))
GEONAME_ID = os.environ.get("GEONAME_ID", "1254360")
APIKEY = os.environ.get("TEXTMEBOT_APIKEY", "")

def _parse_recipients(raw):
    # tolerate commas, newlines, semicolons, and stray whitespace between numbers
    parts = re.split(r'[,\n\r;]+', raw)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if not p.startswith('+'):
            p = '+' + p.lstrip('+')
        out.append(p)
    return out

RECIPIENTS = _parse_recipients(os.environ.get("RECIPIENT_NUMBERS", ""))
CITY_LABEL_EN = os.environ.get("CITY_LABEL_EN", "Tirupati, AP")
CITY_LABEL_TE = os.environ.get("CITY_LABEL_TE", "తిరుపతి, ఆం.ప్ర.")
CITY_LABEL_TA = os.environ.get("CITY_LABEL_TA", "திருப்பதி, ஆ.பி.")

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

# --------------------------------------------------------------------------
# Translation tables (standard, fixed Panchangam vocabulary - these never
# change, only which entry is "today's" value changes day to day)
# --------------------------------------------------------------------------

WEEKDAY_TE = {
    "Sunday": "ఆదివారం", "Monday": "సోమవారం", "Tuesday": "మంగళవారం",
    "Wednesday": "బుధవారం", "Thursday": "గురువారం", "Friday": "శుక్రవారం",
    "Saturday": "శనివారం",
}
WEEKDAY_TA = {
    "Sunday": "ஞாயிற்றுக்கிழமை", "Monday": "திங்கள்கிழமை", "Tuesday": "செவ்வாய்கிழமை",
    "Wednesday": "புதன்கிழமை", "Thursday": "வியாழக்கிழமை", "Friday": "வெள்ளிக்கிழமை",
    "Saturday": "சனிக்கிழமை",
}

TITHI_TE = {
    "Pratipada": "పాడ్యమి", "Dwitiya": "విదియ", "Tritiya": "తదియ", "Chaturthi": "చవితి",
    "Panchami": "పంచమి", "Shashthi": "షష్ఠి", "Saptami": "సప్తమి", "Ashtami": "అష్టమి",
    "Navami": "నవమి", "Dashami": "దశమి", "Ekadashi": "ఏకాదశి", "Dwadashi": "ద్వాదశి",
    "Trayodashi": "త్రయోదశి", "Chaturdashi": "చతుర్దశి", "Purnima": "పౌర్ణమి", "Amavasya": "అమావాస్య",
}
TITHI_TA = {
    "Pratipada": "பிரதமை", "Dwitiya": "துவிதியை", "Tritiya": "திரிதியை", "Chaturthi": "சதுர்த்தி",
    "Panchami": "பஞ்சமி", "Shashthi": "சஷ்டி", "Saptami": "சப்தமி", "Ashtami": "அஷ்டமி",
    "Navami": "நவமி", "Dashami": "தசமி", "Ekadashi": "ஏகாதசி", "Dwadashi": "துவாதசி",
    "Trayodashi": "திரயோதசி", "Chaturdashi": "சதுர்த்தசி", "Purnima": "பௌர்ணமி", "Amavasya": "அமாவாசை",
}
PAKSHA_TE = {"Krishna Paksha": "కృష్ణ పక్షం", "Shukla Paksha": "శుక్ల పక్షం"}
PAKSHA_TA = {"Krishna Paksha": "கிருஷ்ண பக்ஷம்", "Shukla Paksha": "சுக்ல பக்ஷம்"}

NAKSHATRA_TE = {
    "Ashwini": "అశ్విని", "Bharani": "భరణి", "Krittika": "కృత్తిక", "Rohini": "రోహిణి",
    "Mrigashira": "మృగశిర", "Ardra": "ఆరుద్ర", "Punarvasu": "పునర్వసు", "Pushya": "పుష్యమి",
    "Ashlesha": "ఆశ్లేష", "Magha": "మఖ", "Purva Phalguni": "పుబ్బ", "Uttara Phalguni": "ఉత్తర",
    "Hasta": "హస్త", "Chitra": "చిత్త", "Swati": "స్వాతి", "Vishakha": "విశాఖ",
    "Anuradha": "అనూరాధ", "Jyeshtha": "జ్యేష్ఠ", "Mula": "మూల", "Purva Ashadha": "పూర్వాషాఢ",
    "Uttara Ashadha": "ఉత్తరాషాఢ", "Shravana": "శ్రవణం", "Dhanishtha": "ధనిష్ఠ",
    "Shatabhisha": "శతభిషం", "Purva Bhadrapada": "పూర్వాభాద్ర", "Uttara Bhadrapada": "ఉత్తరాభాద్ర",
    "Revati": "రేవతి",
}
NAKSHATRA_TA = {
    "Ashwini": "அஸ்வினி", "Bharani": "பரணி", "Krittika": "கார்த்திகை", "Rohini": "ரோகிணி",
    "Mrigashira": "மிருகசீரிடம்", "Ardra": "திருவாதிரை", "Punarvasu": "புனர்பூசம்", "Pushya": "பூசம்",
    "Ashlesha": "ஆயில்யம்", "Magha": "மகம்", "Purva Phalguni": "பூரம்", "Uttara Phalguni": "உத்திரம்",
    "Hasta": "அஸ்தம்", "Chitra": "சித்திரை", "Swati": "சுவாதி", "Vishakha": "விசாகம்",
    "Anuradha": "அனுஷம்", "Jyeshtha": "கேட்டை", "Mula": "மூலம்", "Purva Ashadha": "பூராடம்",
    "Uttara Ashadha": "உத்திராடம்", "Shravana": "திருவோணம்", "Dhanishtha": "அவிட்டம்",
    "Shatabhisha": "சதயம்", "Purva Bhadrapada": "பூரட்டாதி", "Uttara Bhadrapada": "உத்திரட்டாதி",
    "Revati": "ரேவதி",
}

YOGA_TE = {
    "Vishkambha": "విష్కంభ", "Priti": "ప్రీతి", "Ayushman": "ఆయుష్మాన్", "Saubhagya": "సౌభాగ్య",
    "Shobhana": "శోభన", "Atiganda": "అతిగండ", "Sukarma": "సుకర్మ", "Dhriti": "ధృతి",
    "Shula": "శూల", "Ganda": "గండ", "Vriddhi": "వృద్ధి", "Dhruva": "ధ్రువ",
    "Vyaghata": "వ్యాఘాత", "Harshana": "హర్షణ", "Vajra": "వజ్ర", "Siddhi": "సిద్ధి",
    "Vyatipata": "వ్యతీపాత", "Variyana": "వరీయాన్", "Parigha": "పరిఘ", "Shiva": "శివ",
    "Siddha": "సిద్ధ", "Sadhya": "సాధ్య", "Shubha": "శుభ", "Shukla": "శుక్ల",
    "Brahma": "బ్రహ్మ", "Indra": "ఇంద్ర", "Vaidhriti": "వైధృతి",
}
YOGA_TA = {
    "Vishkambha": "விஷ்கம்பம்", "Priti": "பிரீதி", "Ayushman": "ஆயுஷ்மான்", "Saubhagya": "சௌபாக்கியம்",
    "Shobhana": "சோபனம்", "Atiganda": "அதிகண்டம்", "Sukarma": "சுகர்மா", "Dhriti": "திருதி",
    "Shula": "சூலம்", "Ganda": "கண்டம்", "Vriddhi": "விருத்தி", "Dhruva": "துருவம்",
    "Vyaghata": "வியாகாதம்", "Harshana": "ஹர்ஷணம்", "Vajra": "வஜ்ரம்", "Siddhi": "சித்தி",
    "Vyatipata": "வியதீபாதம்", "Variyana": "வரியான்", "Parigha": "பரிகம்", "Shiva": "சிவம்",
    "Siddha": "சித்தம்", "Sadhya": "சாத்யம்", "Shubha": "சுபம்", "Shukla": "சுக்லம்",
    "Brahma": "பிரம்மம்", "Indra": "இந்திரம்", "Vaidhriti": "வைதிருதி",
}

KARANA_TE = {
    "Bava": "బవ", "Balava": "బాలవ", "Kaulava": "కౌలవ", "Taitila": "తైతిల",
    "Garaja": "గరజ", "Vanija": "వణిజ", "Vishti": "భద్ర", "Shakuni": "శకుని",
    "Chatushpada": "చతుష్పాద", "Naga": "నాగ", "Kimstughna": "కింస్తుఘ్న",
}
KARANA_TA = {
    "Bava": "பவம்", "Balava": "பாலவம்", "Kaulava": "கௌலவம்", "Taitila": "தைதிலம்",
    "Garaja": "கரஜம்", "Vanija": "வணிஜம்", "Vishti": "பத்திரை", "Shakuni": "சகுனி",
    "Chatushpada": "சதுஷ்பாதம்", "Naga": "நாகவம்", "Kimstughna": "கிம்ஸ்துக்னம்",
}

# Drik Panchang's raw English spelling sometimes differs from the spelling
# South Indian temples/audiences actually use. This maps their spelling to
# the preferred one for the ENGLISH card only (the Telugu/Tamil cards are
# unaffected, since those go through their own translation dictionaries
# above). Add more entries here any time a spelling looks off - one line
# each, no other code changes needed.
EN_SPELLING_OVERRIDES = {
    "Ardra": "Arudra",
}

def apply_en_overrides(text):
    if not text:
        return text
    for raw, preferred in EN_SPELLING_OVERRIDES.items():
        text = re.sub(r'\b' + re.escape(raw) + r'\b', preferred, text)
    return text

LABELS = {
    "en": {
        "title": "Today's Panchangam", "core": "Panchang Core", "sunmoon": "Sun & Moon",
        "auspicious": "Auspicious Timings", "inauspicious": "Inauspicious Timings",
        "tithi": "Tithi", "nakshatra": "Nakshatra", "yoga": "Yoga", "karana": "Karana",
        "paksha": "Paksha", "sunrise": "Sunrise", "sunset": "Sunset", "moonrise": "Moonrise",
        "moonset": "Moonset", "brahma": "Brahma Muhurta", "abhijit": "Abhijit Muhurta",
        "amrit": "Amrit Kalam", "rahu": "Rahu Kalam", "yama": "Yamagandam",
        "gulika": "Gulikai Kalam", "durmuhurtam": "Durmuhurtam", "varjyam": "Varjyam",
        "upto": "upto", "then": "then", "none_today": "None Today",
    },
    "te": {
        "title": "నేటి పంచాంగం", "core": "పంచాంగ వివరాలు", "sunmoon": "సూర్య చంద్ర సమయాలు",
        "auspicious": "శుభ సమయాలు", "inauspicious": "అశుభ సమయాలు",
        "tithi": "తిథి", "nakshatra": "నక్షత్రం", "yoga": "యోగం", "karana": "కరణం",
        "paksha": "పక్షం", "sunrise": "సూర్యోదయం", "sunset": "సూర్యాస్తమయం", "moonrise": "చంద్రోదయం",
        "moonset": "చంద్రాస్తమయం", "brahma": "బ్రహ్మ ముహూర్తం", "abhijit": "అభిజిత్ ముహూర్తం",
        "amrit": "అమృత కాలం (ఘడియలు)", "rahu": "రాహు కాలం", "yama": "యమగండం",
        "gulika": "గుళిక కాలం", "durmuhurtam": "దుర్ముహూర్తం", "varjyam": "వర్జ్యము",
        "upto": "వరకు", "then": "తర్వాత", "none_today": "ఈరోజు లేదు",
    },
    "ta": {
        "title": "இன்றைய பஞ்சாங்கம்", "core": "பஞ்சாங்க விவரங்கள்", "sunmoon": "சூரிய சந்திர நேரங்கள்",
        "auspicious": "சுப நேரங்கள்", "inauspicious": "அசுப நேரங்கள்",
        "tithi": "திதி", "nakshatra": "நட்சத்திரம்", "yoga": "யோகம்", "karana": "கரணம்",
        "paksha": "பக்ஷம்", "sunrise": "சூரிய உதயம்", "sunset": "சூரிய அஸ்தமனம்", "moonrise": "சந்திர உதயம்",
        "moonset": "சந்திர அஸ்தமனம்", "brahma": "பிரம்ம முகூர்த்தம்", "abhijit": "அபிஜித் முகூர்த்தம்",
        "amrit": "அமிர்த காலம்", "rahu": "ராகு காலம்", "yama": "எமகண்டம்",
        "gulika": "குளிகை காலம்", "durmuhurtam": "துர்முகூர்த்தம்", "varjyam": "வர்ஜ்யம்",
        "upto": "வரை", "then": "பின்", "none_today": "இன்று இல்லை",
    },
}

MONTH_TE = {1:"జనవరి",2:"ఫిబ్రవరి",3:"మార్చి",4:"ఏప్రిల్",5:"మే",6:"జూన్",7:"జూలై",
            8:"ఆగస్టు",9:"సెప్టెంబర్",10:"అక్టోబర్",11:"నవంబర్",12:"డిసెంబర్"}
MONTH_TA = {1:"ஜனவரி",2:"பிப்ரவரி",3:"மார்ச்",4:"ஏப்ரல்",5:"மே",6:"ஜூன்",7:"ஜூலை",
            8:"ஆகஸ்ட்",9:"செப்டம்பர்",10:"அக்டோபர்",11:"நவம்பர்",12:"டிசம்பர்"}


def translate_value(raw, te_map, ta_map):
    """Translate an English panchang value like 'Krishna Ashtami' or
    'Revati upto 04:00 PM, then Ashwini' into Telugu/Tamil, keeping any
    time fragments in numerals as-is."""
    def sub_all(text, mapping, upto_word, then_word):
        # sort by length desc so multi-word names match before substrings
        for en, native in sorted(mapping.items(), key=lambda x: -len(x[0])):
            text = re.sub(r'\b' + re.escape(en) + r'\b', native, text)
        text = re.sub(r'\bupto\b', upto_word, text)
        text = re.sub(r'\bthen\b', then_word, text)
        return text
    te = sub_all(raw, te_map, LABELS["te"]["upto"], LABELS["te"]["then"])
    ta = sub_all(raw, ta_map, LABELS["ta"]["upto"], LABELS["ta"]["then"])
    return te, ta


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------

def fetch_panchang(date_str):
    """date_str: DD/MM/YYYY. Returns dict of parsed fields (best-effort)."""
    url = f"https://www.drikpanchang.com/panchang/day-panchang.html?geoname-id={GEONAME_ID}&date={date_str}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.drikpanchang.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    session = requests.Session()
    session.headers.update(headers)

    last_exc = None
    resp = None
    for attempt in range(5):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                break
            print(f"  attempt {attempt+1}: HTTP {resp.status_code}, retrying...", file=sys.stderr)
        except requests.RequestException as e:
            last_exc = e
            print(f"  attempt {attempt+1}: {e}, retrying...", file=sys.stderr)
        time.sleep(5 * (attempt + 1))  # backoff: 5s, 10s, 15s, 20s, 25s
    if resp is None:
        raise last_exc
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]

    # ------------------------------------------------------------------
    # The word "Nakshatra" (and a few other field labels) legitimately
    # appears THREE times on this page: once in the top navigation menu,
    # once in the real data table, and once in a footer resource-links
    # list. Blindly taking the first match risks grabbing nav/footer
    # junk (this is exactly how we once got "Calendars" instead of a
    # real nakshatra name). To avoid that, we first locate the real data
    # table by anchoring on "Sunrise and Moonrise" (a heading that only
    # appears once, right before the actual table) through "Notes:"
    # (which marks the end of the table, right before footer content),
    # and restrict all field lookups to that bounded region only.
    # ------------------------------------------------------------------
    start_idx = next((i for i, l in enumerate(lines) if l == "Sunrise and Moonrise"), 0)
    end_idx = next((i for i, l in enumerate(lines) if l.startswith("Notes:")), len(lines))
    if end_idx <= start_idx:
        end_idx = len(lines)
    region = lines[start_idx:end_idx]

    # Drik Panchang sometimes renders a time and its AM/PM marker as two
    # separate text nodes (e.g. "05:52" then "AM" on their own lines), and
    # time ranges like "12:17 PM to 01:54 PM" can likewise be split into
    # several fragments ("12:17", "PM", "to", "01:54", "PM"). After finding
    # the first fragment of a value, we glue on any immediately-following
    # short continuation fragments (a bare AM/PM marker, "to", or another
    # bare H:MM) so values never get truncated mid-time.
    _FRAG_CONTINUE_RE = re.compile(r'^(AM|PM|to|\d{1,2}:\d{2})$', re.IGNORECASE)

    def find_value(label, lookahead=6):
        """Find a line exactly equal to `label` within the bounded data
        region, return the next non-empty, non-icon line after it, with
        any split-off AM/PM/"to"/time continuation fragments reattached."""
        for i, l in enumerate(region):
            if l == label:
                for j in range(i + 1, min(i + 1 + lookahead, len(region))):
                    cand = region[j]
                    if cand and cand != label and not cand.startswith("ⓘ"):
                        frags = [cand]
                        k = j + 1
                        while k < len(region) and k < j + 8 and _FRAG_CONTINUE_RE.match(region[k] or ""):
                            frags.append(region[k])
                            k += 1
                        return re.sub(r'\s+', ' ', " ".join(frags)).strip()
        return None

    data = {}
    data["tithi"] = find_value("Tithi")
    data["nakshatra"] = find_value("Nakshatra")
    data["yoga"] = find_value("Yoga")
    data["karana"] = find_value("Karana")
    data["weekday"] = find_value("Weekday")
    data["paksha"] = find_value("Paksha")
    data["sunrise"] = find_value("Sunrise")
    data["sunset"] = find_value("Sunset")
    data["moonrise"] = find_value("Moonrise")
    data["moonset"] = find_value("Moonset")
    data["brahma_muhurta"] = find_value("Brahma Muhurta")
    data["abhijit"] = find_value("Abhijit")
    data["amrit_kalam"] = find_value("Amrit Kalam")
    data["rahu_kalam"] = find_value("Rahu Kalam")
    data["yamaganda"] = find_value("Yamaganda")
    data["gulikai_kalam"] = find_value("Gulikai Kalam")
    data["durmuhurtam"] = find_value("Dur Muhurtam")
    data["varjyam"] = find_value("Varjyam")

    missing = [k for k, v in data.items() if not v]
    if missing:
        print(f"WARNING: could not find fields: {missing}", file=sys.stderr)
    if start_idx == 0:
        print("WARNING: could not locate 'Sunrise and Moonrise' anchor - "
              "parsed from the WHOLE page, results may be unreliable.", file=sys.stderr)
    return data


# --------------------------------------------------------------------------
# Validation - fail-safe so we never send wrong/garbled data
# --------------------------------------------------------------------------

TIME_RE = re.compile(
    r'(\d{1,2}:\d{2}\s*(AM|PM))|(\bNone\b)|(\bWhole Day\b)', re.IGNORECASE
)

def _name_is_known(value, valid_names):
    if not value:
        return False
    for name in valid_names:
        if re.search(r'\b' + re.escape(name) + r'\b', value):
            return True
    return False

def validate_data(data):
    """Returns (ok: bool, problems: list[str]). Cross-checks every parsed
    value against known vocab / expected time-pattern so we never send
    garbage (like a stray nav-menu word) as if it were real panchang
    data."""
    problems = []

    name_checks = [
        ("tithi", TITHI_TE),
        ("nakshatra", NAKSHATRA_TE),
        ("yoga", YOGA_TE),
        ("karana", KARANA_TE),
    ]
    for field, valid_map in name_checks:
        val = data.get(field)
        if not val:
            problems.append(f"{field} is missing")
        elif not _name_is_known(val, valid_map.keys()):
            problems.append(f"{field}='{val}' does not match any known {field} name")

    if data.get("paksha") not in ("Krishna Paksha", "Shukla Paksha"):
        problems.append(f"paksha='{data.get('paksha')}' is not a recognized paksha")

    if data.get("weekday_full") not in WEEKDAY_TE:
        problems.append(f"weekday_full='{data.get('weekday_full')}' is not a recognized weekday")

    time_fields = ["sunrise", "sunset", "moonrise", "moonset", "brahma_muhurta",
                   "abhijit", "amrit_kalam", "rahu_kalam", "yamaganda",
                   "gulikai_kalam", "durmuhurtam", "varjyam"]
    for field in time_fields:
        val = data.get(field)
        if not val:
            problems.append(f"{field} is missing")
        elif not TIME_RE.search(val):
            problems.append(f"{field}='{val}' does not look like a valid time")

    return (len(problems) == 0, problems)


# --------------------------------------------------------------------------
# Image rendering - composites onto the temple's own template image
# --------------------------------------------------------------------------

ASSET_DIR = os.path.join(HERE, "assets")
TEMPLATE_PATH = os.path.join(ASSET_DIR, "panchangam_template.jpg")

# Fraction of the template's height/width reserved by the purple header /
# footer bars, based on the supplied template. Tweak these if content ever
# overlaps the bars or leaves too much empty space.
HEADER_FRAC = 0.11
FOOTER_FRAC = 0.865
LEFT_FRAC = 0.06
RIGHT_FRAC = 0.94

SECTION_COL = (95, 40, 130)     # purple, matches the template's own header/footer bars
TEXT_COL = (40, 30, 20)
LINE_COL = (185, 170, 195)
SUBTITLE_COL = (95, 40, 130)

def font_for(lang, weight, size):
    if lang == "en":
        path = "Poppins-Bold.ttf" if weight == "bold" else "Poppins-Medium.ttf"
    elif lang == "te":
        path = "NotoSansTelugu-Merged.ttf"
    else:
        path = "NotoSansTamil-Merged.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, path), size)


def _wrap_lines(draw, text, font, max_width):
    """Word-wrap text to fit max_width, measured with the real font via
    textbbox (a pure measurement call - it doesn't draw anything)."""
    text = text or "-"
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _measure_blocks(draw, lang, blocks, content_w, font_scale, scale):
    """Compute every box's size at a given font_scale WITHOUT drawing
    anything, so render_card can auto-shrink the font until everything
    fits between the template's header and footer bars."""
    header_size = max(int(34 * scale * font_scale), 14)
    value_size = max(int(32 * scale * font_scale), 14)
    list_header_size = max(int(36 * scale * font_scale), 14)
    list_row_size = max(int(30 * scale * font_scale), 14)
    pad = max(int(16 * scale * font_scale), 6)
    col_gap = max(int(18 * scale * font_scale), 6)

    fonts = {
        "header": font_for(lang, "bold", header_size),
        "value": font_for(lang, "bold", value_size),
        "list_header": font_for(lang, "bold", list_header_size),
        "list_row_lbl": font_for(lang, "medium", list_row_size),
        "list_row_val": font_for(lang, "bold", list_row_size),
    }
    value_line_h = value_size + int(10 * scale * font_scale)
    list_row_h = list_row_size + int(16 * scale * font_scale)

    geoms = []
    total_h = 0
    for blk in blocks:
        if blk["type"] == "pair":
            box_w = (content_w - col_gap) // 2
            usable_w = box_w - 2 * pad
            left_lines = _wrap_lines(draw, blk["left"][1], fonts["value"], usable_w)
            right_lines = _wrap_lines(draw, blk["right"][1], fonts["value"], usable_w)
            n_lines = max(len(left_lines), len(right_lines), 1)
            header_h = header_size + 2 * pad
            value_h = n_lines * value_line_h + 2 * pad
            geoms.append({"type": "pair", "box_w": box_w, "header_h": header_h,
                          "value_h": value_h, "left_lines": left_lines, "right_lines": right_lines})
            total_h += header_h + value_h
        else:  # "list"
            header_h = list_header_size + 2 * pad
            rows_h = len(blk["rows"]) * list_row_h + 2 * pad
            geoms.append({"type": "list", "header_h": header_h, "rows_h": rows_h})
            total_h += header_h + rows_h

    row_gap = max(int(16 * scale * font_scale), 6)
    total_h += row_gap * max(len(blocks) - 1, 0)
    return total_h, geoms, fonts, pad, col_gap, value_line_h, list_row_h, row_gap


def render_card(lang, subtitle, blocks, outpath):
    """Draws a 2-column grid of bordered boxes (one box per field, e.g.
    Tithi | Nakshatra side by side) onto the temple's template image,
    styled after the layout the temple already uses in its manual
    Panchangam posts. Font size auto-shrinks as needed so nothing ever
    overlaps or runs past the footer bar."""
    base = Image.open(TEMPLATE_PATH).convert("RGB")
    W, H = base.size
    img = base.copy()
    d = ImageDraw.Draw(img)

    top = int(H * HEADER_FRAC)
    bottom = int(H * FOOTER_FRAC)
    left = int(W * LEFT_FRAC)
    right = int(W * RIGHT_FRAC)
    content_w = right - left
    scale = W / 1587.0

    subtitle_size = max(int(32 * scale), 14)
    f_sub = font_for(lang, "medium", subtitle_size)
    subtitle_h = subtitle_size + int(30 * scale)
    content_top = top + int(24 * scale)
    available = bottom - content_top - subtitle_h

    # Auto-fit: try full size first, shrink ~8% at a time until the whole
    # grid fits in the available space (floor at 40% of base size).
    font_scale = 1.0
    for _ in range(14):
        total_h, geoms, fonts, pad, col_gap, value_line_h, list_row_h, row_gap = _measure_blocks(
            d, lang, blocks, content_w, font_scale, scale)
        if total_h <= available or font_scale <= 0.4:
            break
        font_scale *= 0.92

    y = content_top
    d.text((W / 2, y), subtitle, font=f_sub, fill=SUBTITLE_COL, anchor="ma")
    y += subtitle_h

    border_w = max(2, int(2 * scale))
    for blk, geom in zip(blocks, geoms):
        if geom["type"] == "pair":
            box_w = geom["box_w"]
            header_h = geom["header_h"]
            box_h = header_h + geom["value_h"]
            for i, side in enumerate(("left", "right")):
                bx = left + i * (box_w + col_gap)
                d.rectangle([bx, y, bx + box_w, y + box_h], outline=LINE_COL, width=border_w, fill=(255, 255, 255))
                d.rectangle([bx, y, bx + box_w, y + header_h], fill=SECTION_COL)
                d.text((bx + box_w / 2, y + header_h / 2), blk[side][0], font=fonts["header"],
                       fill=(255, 255, 255), anchor="mm")
                ty = y + header_h + pad
                for line in geom[f"{side}_lines"]:
                    d.text((bx + box_w / 2, ty), line, font=fonts["value"], fill=TEXT_COL, anchor="ma")
                    ty += value_line_h
            y += box_h + row_gap
        else:  # "list"
            header_h = geom["header_h"]
            box_h = header_h + geom["rows_h"]
            d.rectangle([left, y, right, y + box_h], outline=LINE_COL, width=border_w, fill=(255, 255, 255))
            d.rectangle([left, y, right, y + header_h], fill=SECTION_COL)
            d.text((left + content_w / 2, y + header_h / 2), blk["header"], font=fonts["list_header"],
                   fill=(255, 255, 255), anchor="mm")
            ry = y + header_h + pad
            for lbl, val in blk["rows"]:
                d.text((left + pad, ry), lbl, font=fonts["list_row_lbl"], fill=TEXT_COL)
                d.text((right - pad, ry), val, font=fonts["list_row_val"], fill=TEXT_COL, anchor="ra")
                ry += list_row_h
            y += box_h + row_gap

    img.save(outpath, quality=92)
    return outpath


def build_images(data, dt_ist):
    te_tithi, ta_tithi = translate_value(data["tithi"] or "", TITHI_TE, TITHI_TA)
    te_nak, ta_nak = translate_value(data["nakshatra"] or "", NAKSHATRA_TE, NAKSHATRA_TA)
    te_yoga, ta_yoga = translate_value(data["yoga"] or "", YOGA_TE, YOGA_TA)
    te_kar, ta_kar = translate_value(data["karana"] or "", KARANA_TE, KARANA_TA)
    te_paksha = PAKSHA_TE.get(data["paksha"], data["paksha"] or "")
    ta_paksha = PAKSHA_TA.get(data["paksha"], data["paksha"] or "")
    te_weekday = WEEKDAY_TE.get(data["weekday_full"], data["weekday_full"])
    ta_weekday = WEEKDAY_TA.get(data["weekday_full"], data["weekday_full"])

    outputs = []
    for lang, tithi, nak, yoga, kar, paksha, weekday, month_name in [
        ("en", apply_en_overrides(data["tithi"]), apply_en_overrides(data["nakshatra"]),
         apply_en_overrides(data["yoga"]), apply_en_overrides(data["karana"]),
         data["paksha"], data["weekday_full"], dt_ist.strftime("%B")),
        ("te", te_tithi, te_nak, te_yoga, te_kar, te_paksha, te_weekday, MONTH_TE[dt_ist.month]),
        ("ta", ta_tithi, ta_nak, ta_yoga, ta_kar, ta_paksha, ta_weekday, MONTH_TA[dt_ist.month]),
    ]:
        L = LABELS[lang]
        city = {"en": CITY_LABEL_EN, "te": CITY_LABEL_TE, "ta": CITY_LABEL_TA}[lang]
        if lang == "en":
            date_str = dt_ist.strftime("%B %d, %Y")
        else:
            date_str = f"{month_name} {dt_ist.day:02d}, {dt_ist.year}"
        subtitle = f"{date_str}  |  {weekday}  |  {city}"

        abhijit_val = data["abhijit"] if data["abhijit"] and data["abhijit"].lower() != "none" else L["none_today"]

        # 2-column boxed grid, styled after the temple's own manual layout:
        # one bordered box per field, paired up two-to-a-row, plus a single
        # full-width box for the auspicious muhurtas.
        blocks = [
            {"type": "list", "header": L["auspicious"], "rows": [
                (L["brahma"], data["brahma_muhurta"] or "-"),
                (L["abhijit"], abhijit_val),
            ]},
            {"type": "pair", "left": (L["tithi"], tithi or "-"), "right": (L["nakshatra"], nak or "-")},
            {"type": "pair", "left": (L["yoga"], yoga or "-"), "right": (L["karana"], kar or "-")},
            {"type": "pair", "left": (L["sunrise"], data["sunrise"] or "-"), "right": (L["sunset"], data["sunset"] or "-")},
            {"type": "pair", "left": (L["moonrise"], data["moonrise"] or "-"), "right": (L["moonset"], data["moonset"] or "-")},
            {"type": "pair", "left": (L["rahu"], data["rahu_kalam"] or "-"), "right": (L["yama"], data["yamaganda"] or "-")},
            {"type": "pair", "left": (L["gulika"], data["gulikai_kalam"] or "-"), "right": (L["durmuhurtam"], data["durmuhurtam"] or "-")},
            {"type": "pair", "left": (L["varjyam"], data["varjyam"] or "-"), "right": (L["amrit"], data["amrit_kalam"] or "-")},
        ]

        outpath = os.path.join(HERE, f"panchangam_{lang}.jpg")
        render_card(lang, subtitle, blocks, outpath)
        outputs.append((lang, outpath))
    return outputs


# --------------------------------------------------------------------------
# Send via TextMeBot
# --------------------------------------------------------------------------

def send_image(recipient, apikey, image_path, caption):
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    resp = requests.post(
        "https://api.textmebot.com/send.php",
        data={"recipient": recipient, "apikey": apikey, "text": caption, "file": b64},
        timeout=60,
    )
    print(f"  -> {recipient}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp


def send_text(recipient, apikey, text):
    resp = requests.post(
        "https://api.textmebot.com/send.php",
        data={"recipient": recipient, "apikey": apikey, "text": text},
        timeout=30,
    )
    print(f"  -> {recipient} (text): HTTP {resp.status_code} {resp.text[:200]}")
    return resp


MAX_ATTEMPTS = 5


def fetch_and_validate(date_str, weekday_full):
    """One full attempt: fetch + parse + validate. Returns (data, None) on
    success, or (None, error_message) on any failure (network error, or
    validation failure)."""
    try:
        data = fetch_panchang(date_str)
        data["weekday_full"] = weekday_full
    except Exception as e:
        return None, f"fetch error: {e}"

    print("Parsed fields:")
    for k, v in data.items():
        print(f"  {k}: {v}")

    ok, problems = validate_data(data)
    if not ok:
        return None, "validation failed: " + "; ".join(problems)

    print("Validation passed: all fields look correct.")
    return data, None


def notify_failure(now_ist, last_error):
    """Best-effort text (not image) to every recipient explaining that
    today's automated Panchangam could not be sent, so it's clear this
    is a known failure and not silence."""
    date_disp = now_ist.strftime("%B %d, %Y")
    message = (
        f"Hi, this is Vihari's automated Panchangam system.\n\n"
        f"Today's Panchangam ({date_disp}) could not be sent after {MAX_ATTEMPTS} attempts "
        f"due to a technical error:\n{last_error}\n\n"
        f"Vihari has been notified and will look into it. Sorry for the inconvenience today!\n\n"
        f"(నమస్తే, ఇది వీహారి యొక్క ఆటోమేటెడ్ పంచాంగం సిస్టమ్. ఈరోజు సాంకేతిక సమస్య వలన పంచాంగం పంపడం సాధ్యం కాలేదు. క్షమించండి.)"
    )
    if not RECIPIENTS:
        print("No RECIPIENT_NUMBERS configured - cannot send failure notice either.", file=sys.stderr)
        return
    for recipient in RECIPIENTS:
        try:
            print(f"Sending failure notice to {recipient}...")
            send_text(recipient, APIKEY, message)
            time.sleep(6)
        except Exception as e:
            print(f"  could not even send failure notice to {recipient}: {e}", file=sys.stderr)


def main():
    if not APIKEY:
        print("ERROR: TEXTMEBOT_APIKEY is not set.", file=sys.stderr)
        sys.exit(1)
    if not RECIPIENTS:
        print("ERROR: RECIPIENT_NUMBERS is not set.", file=sys.stderr)
        sys.exit(1)

    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%d/%m/%Y")
    weekday_full = now_ist.strftime("%A")

    data = None
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"=== Attempt {attempt}/{MAX_ATTEMPTS}: fetching Panchangam for {date_str} "
              f"(geoname-id={GEONAME_ID})... ===")
        data, last_error = fetch_and_validate(date_str, weekday_full)
        if data is not None:
            break
        print(f"Attempt {attempt} failed: {last_error}", file=sys.stderr)
        if attempt < MAX_ATTEMPTS:
            wait = 20 * attempt
            print(f"Waiting {wait}s before retrying...", file=sys.stderr)
            time.sleep(wait)

    if data is None:
        print(f"All {MAX_ATTEMPTS} attempts failed. Notifying recipients and giving up "
              f"for today.", file=sys.stderr)
        notify_failure(now_ist, last_error)
        sys.exit(1)

    print("Rendering images...")
    images = build_images(data, now_ist)

    captions = {
        "en": f"Panchangam (English) - {now_ist.strftime('%B %d, %Y')}",
        "te": f"పంచాంగం (తెలుగు) - {now_ist.strftime('%d/%m/%Y')}",
        "ta": f"பஞ்சாங்கம் (தமிழ்) - {now_ist.strftime('%d/%m/%Y')}",
    }

    for recipient in RECIPIENTS:
        print(f"Sending to {recipient}...")
        for i, (lang, path) in enumerate(images):
            send_image(recipient, APIKEY, path, captions[lang])
            if i < len(images) - 1:
                time.sleep(6)
        time.sleep(6)

    print("Done.")


if __name__ == "__main__":
    main()
