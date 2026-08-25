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
import swisseph as swe
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

IST = timezone(timedelta(hours=5, minutes=30))
GEONAME_ID = os.environ.get("GEONAME_ID", "1254360")

# Tirupati, Andhra Pradesh - 13 deg 38' 07" N, 79 deg 25' 11" E (matches the
# location Drik Panchang itself resolves geoname-id 1254360 to). Used by the
# self-contained ephemeris-based Tithi engine below, so Tithi no longer
# depends on scraping drikpanchang.com's rendered HTML for this field.
TIRUPATI_LAT = 13.635278
TIRUPATI_LON = 79.419722
swe.set_sid_mode(swe.SIDM_LAHIRI)  # Lahiri ayanamsha - same standard Drik Panchang uses
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

# Rahu Kalam / Yamagandam / Gulika Kalam - previously a table of NOMINAL
# fixed clock times (the day treated as a flat 6:00 AM-6:00 PM, divided
# into eight 90-minute blocks), chosen in an earlier session to match a
# different reference app rather than Drik Panchang's own sunrise-
# adjusted numbers. The user has since provided today's actual Drik
# Panchang page for Tirupati as ground truth and asked for it to match -
# checked segment-by-segment, our weekday->segment assignment below was
# already correct (matches the standard classical rotation for all three
# kalams, all 7 weekdays), but the fixed 90-minute-block clock times were
# off from Drik's real sunrise-adjusted ones by up to ~20-30 minutes.
# Switched to computing the actual daylight window (sunrise to sunset,
# whatever the day's real values are) split into eight equal segments -
# verified this reproduces the reference exactly (to the minute) for
# Monday, Aug 24 2026, Tirupati: Rahu 7:36-9:09 AM, Yamaganda
# 10:42 AM-12:14 PM, Gulika 1:47-3:20 PM, all matching to within rounding.
KALAM_SEGMENT = {
    "rahu":   {"Sunday": 8, "Monday": 2, "Tuesday": 7, "Wednesday": 5, "Thursday": 6, "Friday": 4, "Saturday": 3},
    "yama":   {"Sunday": 5, "Monday": 4, "Tuesday": 3, "Wednesday": 2, "Thursday": 1, "Friday": 7, "Saturday": 6},
    "gulika": {"Sunday": 7, "Monday": 6, "Tuesday": 5, "Wednesday": 4, "Thursday": 3, "Friday": 2, "Saturday": 1},
}


def compute_kalam(sunrise_str, sunset_str, weekday_full):
    """Splits the day's actual sunrise-to-sunset daylight into 8 equal
    segments and returns {'rahu': (start,end), 'yama': (...), 'gulika':
    (...)} as 'HH:MM AM/PM' string pairs, using the classical weekday-to-
    segment assignment in KALAM_SEGMENT above. Returns None if sunrise/
    sunset can't be parsed, so the caller can fall back gracefully."""
    try:
        sr = datetime.strptime(sunrise_str.strip(), "%I:%M %p")
        ss = datetime.strptime(sunset_str.strip(), "%I:%M %p")
    except (ValueError, AttributeError):
        return None
    seg_len = (ss - sr) / 8
    out = {}
    for kalam, per_weekday in KALAM_SEGMENT.items():
        n = per_weekday.get(weekday_full)
        if n is None:
            return None
        start = sr + seg_len * (n - 1)
        end = sr + seg_len * n
        out[kalam] = (start.strftime("%I:%M %p"), end.strftime("%I:%M %p"))
    return out


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
    "Vishkambha": "విష్కంభ", "Priti": "ప్రీతి", "Ayushman": "ఆయుష్మాన్", "Ayushmana": "ఆయుష్మాన్", "Saubhagya": "సౌభాగ్య",
    "Shobhana": "శోభన", "Atiganda": "అతిగండ", "Sukarma": "సుకర్మ", "Dhriti": "ధృతి",
    "Shula": "శూల", "Ganda": "గండ", "Vriddhi": "వృద్ధి", "Dhruva": "ధ్రువ",
    "Vyaghata": "వ్యాఘాత", "Harshana": "హర్షణ", "Vajra": "వజ్ర", "Siddhi": "సిద్ధి",
    "Vyatipata": "వ్యతీపాత", "Variyana": "వరీయాన్", "Parigha": "పరిఘ", "Shiva": "శివ",
    "Siddha": "సిద్ధ", "Sadhya": "సాధ్య", "Shubha": "శుభ", "Shukla": "శుక్ల",
    "Brahma": "బ్రహ్మ", "Indra": "ఇంద్ర", "Vaidhriti": "వైధృతి",
}
YOGA_TA = {
    "Vishkambha": "விஷ்கம்பம்", "Priti": "பிரீதி", "Ayushman": "ஆயுஷ்மான்", "Ayushmana": "ஆயுஷ்மான்", "Saubhagya": "சௌபாக்கியம்",
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
    "Chatushpada": "చతుష్పాద", "Naga": "నాగ", "Nagava": "నాగ", "Kimstughna": "కింస్తుఘ్న",
}
KARANA_TA = {
    "Bava": "பவம்", "Balava": "பாலவம்", "Kaulava": "கௌலவம்", "Taitila": "தைதிலம்",
    "Garaja": "கரஜம்", "Vanija": "வணிஜம்", "Vishti": "பத்திரை", "Shakuni": "சகுனி",
    "Chatushpada": "சதுஷ்பாதம்", "Naga": "நாகவம்", "Nagava": "நாகவம்", "Kimstughna": "கிம்ஸ்துக்னம்",
}

# --------------------------------------------------------------------------
# Year / Month / Season info (Samvatsara, Masa, Ritu, Ayana) - shown as an
# extra "day info" box, matching the info card the temple's own manual
# template includes at the top. Best-effort: these are translated for
# display but not part of the strict validate_data() fail-safe below,
# since their source markup on Drik Panchang is a bit less uniform than
# the core Tithi/Nakshatra/Rahu-Kalam fields.
# --------------------------------------------------------------------------

MASA_TE = {
    "Chaitra": "చైత్ర", "Vaishakha": "వైశాఖ", "Jyeshtha": "జ్యేష్ఠ", "Ashadha": "ఆషాఢ",
    "Shravana": "శ్రావణ", "Bhadrapada": "భాద్రపద", "Ashwin": "ఆశ్వయుజ", "Ashwina": "ఆశ్వయుజ",
    "Kartika": "కార్తీక", "Margashirsha": "మార్గశిర", "Margashira": "మార్గశిర",
    "Pausha": "పుష్య", "Magha": "మాఘ", "Phalguna": "ఫాల్గుణ",
}
MASA_TA = {
    "Chaitra": "சைத்திர", "Vaishakha": "வைசாக", "Jyeshtha": "ஜேஷ்ட", "Ashadha": "ஆஷாட",
    "Shravana": "ஸ்ராவண", "Bhadrapada": "பாத்ரபத", "Ashwin": "ஆஸ்வயுஜ", "Ashwina": "ஆஸ்வயுஜ",
    "Kartika": "கார்த்திக", "Margashirsha": "மார்கசீர்ஷ", "Margashira": "மார்கசீர்ஷ",
    "Pausha": "புஷ்ய", "Magha": "மாக", "Phalguna": "பால்குன",
}

RITU_TE = {
    "Vasanta": "వసంత ఋతువు", "Grishma": "గ్రీష్మ ఋతువు", "Varsha": "వర్ష ఋతువు",
    "Sharad": "శరద్ ఋతువు", "Hemant": "హేమంత ఋతువు", "Hemanta": "హేమంత ఋతువు",
    "Shishira": "శిశిర ఋతువు",
}
RITU_TA = {
    "Vasanta": "வசந்த ருது", "Grishma": "கிரீஷ்ம ருது", "Varsha": "வர்ஷ ருது",
    "Sharad": "சரத் ருது", "Hemant": "ஹேமந்த ருது", "Hemanta": "ஹேமந்த ருது",
    "Shishira": "சிசிர ருது",
}

AYANA_TE = {"Uttarayana": "ఉత్తరాయణం", "Dakshinayana": "దక్షిణాయనం"}
AYANA_TA = {"Uttarayana": "உத்தராயணம்", "Dakshinayana": "தட்சிணாயனம்"}

# The 60-year Samvatsara (Hindu year name) cycle - a fixed, unchanging list
# used in every printed Telugu/Tamil panchangam. Flagging this as the
# single largest new translation table added here - if any name looks off
# once you see it rendered, let me know and I'll correct that one entry.
SAMVATSARA_TE = {
    "Prabhava": "ప్రభవ", "Vibhava": "విభవ", "Shukla": "శుక్ల", "Pramoda": "ప్రమోద",
    "Prajapati": "ప్రజాపతి", "Angirasa": "అంగీరస", "Shrimukha": "శ్రీముఖ", "Bhava": "భవ",
    "Yuva": "యువ", "Dhata": "ధాత", "Ishvara": "ఈశ్వర", "Bahudhanya": "బహుధాన్య",
    "Pramathi": "ప్రమాథి", "Vikrama": "విక్రమ", "Vrisha": "వృష", "Vishu": "వృష",
    "Chitrabhanu": "చిత్రభాను", "Subhanu": "సుభాను", "Tarana": "తారణ", "Parthiva": "పార్థివ",
    "Vyaya": "వ్యయ", "Sarvajit": "సర్వజిత్", "Sarvadhari": "సర్వధారి", "Virodhi": "విరోధి",
    "Vikriti": "వికృతి", "Khara": "ఖర", "Nandana": "నందన", "Vijaya": "విజయ", "Jaya": "జయ",
    "Manmatha": "మన్మథ", "Durmukhi": "దుర్ముఖి", "Hevilambi": "హేవిళంబి", "Vilambi": "విళంబి",
    "Vikari": "వికారి", "Sharvari": "శార్వరి", "Plava": "ప్లవ", "Shubhakrit": "శుభకృత్",
    "Shobhakrit": "శోభకృత్", "Krodhi": "క్రోధి", "Vishvavasu": "విశ్వావసు", "Parabhava": "పరాభవ",
    "Plavanga": "ప్లవంగ", "Kilaka": "కీలక", "Saumya": "సౌమ్య", "Sadharana": "సాధారణ",
    "Virodhikrit": "విరోధికృత్", "Paridhavi": "పరీధావి", "Pramadi": "ప్రమాది", "Pramadicha": "ప్రమాది",
    "Ananda": "ఆనంద", "Rakshasa": "రాక్షస", "Nala": "నల", "Anala": "నల", "Pingala": "పింగళ",
    "Kalayukti": "కాళయుక్తి", "Kalayukta": "కాళయుక్తి", "Siddharthi": "సిద్ధార్థి", "Raudra": "రౌద్రి",
    "Durmati": "దుర్మతి", "Dundubhi": "దుందుభి", "Rudhirodgari": "రుధిరోద్గారి",
    "Raktakshi": "రక్తాక్షి", "Krodhana": "క్రోధన", "Kshaya": "క్షయ", "Akshaya": "క్షయ",
}
SAMVATSARA_TA = {
    "Prabhava": "பிரபவ", "Vibhava": "விபவ", "Shukla": "சுக்ல", "Pramoda": "பிரமோதூத",
    "Prajapati": "பிரஜோத்பத்தி", "Angirasa": "ஆங்கீரச", "Shrimukha": "ஸ்ரீமுக", "Bhava": "பவ",
    "Yuva": "யுவ", "Dhata": "தாது", "Ishvara": "ஈஸ்வர", "Bahudhanya": "வெகுதானிய",
    "Pramathi": "பிரமாதி", "Vikrama": "விக்கிரம", "Vrisha": "விஷு", "Vishu": "விஷு",
    "Chitrabhanu": "சித்திரபானு", "Subhanu": "சுபானு", "Tarana": "தாரண", "Parthiva": "பார்த்திப",
    "Vyaya": "வியய", "Sarvajit": "சர்வசித்து", "Sarvadhari": "சர்வதாரி", "Virodhi": "விரோதி",
    "Vikriti": "விக்ருதி", "Khara": "கர", "Nandana": "நந்தன", "Vijaya": "விஜய", "Jaya": "ஜய",
    "Manmatha": "மன்மத", "Durmukhi": "துன்முகி", "Hevilambi": "ஹேவிளம்பி", "Vilambi": "விளம்பி",
    "Vikari": "விகாரி", "Sharvari": "சார்வரி", "Plava": "பிலவ", "Shubhakrit": "சுபகிருது",
    "Shobhakrit": "சோபகிருது", "Krodhi": "குரோதி", "Vishvavasu": "விசுவாசு", "Parabhava": "பராபவ",
    "Plavanga": "பிலவங்க", "Kilaka": "கீலக", "Saumya": "சௌமிய", "Sadharana": "சாதாரண",
    "Virodhikrit": "விரோதிகிருது", "Paridhavi": "பரிதாபி", "Pramadi": "பிரமாதீச", "Pramadicha": "பிரமாதீச",
    "Ananda": "ஆனந்த", "Rakshasa": "ராட்சச", "Nala": "நள", "Anala": "நள", "Pingala": "பிங்கள",
    "Kalayukti": "காளயுக்தி", "Kalayukta": "காளயுக்தி", "Siddharthi": "சித்தார்த்தி", "Raudra": "ரௌத்திரி",
    "Durmati": "துன்மதி", "Dundubhi": "துந்துபி", "Rudhirodgari": "ருதிரோத்காரி",
    "Raktakshi": "ரக்தாட்சி", "Krodhana": "குரோதன", "Kshaya": "அட்சய", "Akshaya": "அட்சய",
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


def fmt_range_display(s):
    """'12:39 PM to 01:28 PM, then 03:07 PM to 03:57 PM' -> '12:39 PM –
    01:28 PM, 03:07 PM – 03:57 PM' - the compact en-dash notation the
    reference layout uses, which also reads correctly in the Telugu/Tamil
    cards (unlike the bare English words 'to'/'then' this replaces)."""
    if not s:
        return s
    s = re.sub(r'\s+to\s+', ' \u2013 ', s, flags=re.IGNORECASE)
    s = re.sub(r',?\s*then\s+', ', ', s, flags=re.IGNORECASE)
    return s


def fmt_compact_chain(value, lang):
    """'Dwadashi upto 06:21 AM, Aug 25, then Amavasya' -> 'Dwadashi
    (06:21 AM)' - the reference layout's compact single-line convention
    for Tithi/Nakshatra/Yoga/Karana: name plus when it ends, dropping the
    'then next name' continuation and any cross-day date tag. If the
    value has no transition (spans the whole Panchang day), returns it
    unchanged. Handles the already-translated Telugu/Tamil chain text too
    (which uses the localized 'upto' word after the time, per
    translate_value's word-order swap) as well as the raw English form."""
    if not value:
        return value
    if lang == "en":
        m = re.match(r'^(.*?)\s+upto\s+(\d{1,2}:\d{2}\s*(?:AM|PM))', value, re.IGNORECASE)
    else:
        upto_word = re.escape(LABELS[lang]["upto"])
        m = re.match(r'^(.*?)\s+(\d{1,2}:\d{2}\s*(?:AM|PM))\s*' + upto_word, value)
    if m:
        return f"{m.group(1).strip()} ({m.group(2)})"
    return value.strip()


LABELS = {
    "en": {
        "title": "Today's Panchangam", "core": "Panchang Core", "sunmoon": "Sun & Moon",
        "auspicious": "Auspicious Timings", "inauspicious": "Inauspicious Timings",
        "tithi": "Tithi", "nakshatra": "Nakshatra", "yoga": "Yoga", "karana": "Karana",
        "paksha": "Paksha", "sunrise": "Sunrise", "sunset": "Sunset", "moonrise": "Moonrise",
        "moonset": "Moonset", "brahma": "Brahma Muhurat", "abhijit": "Abhijit Muhurat",
        "amrit": "Amrit Kaal", "rahu": "Rahu", "yama": "Yamaganda",
        "gulika": "Gulika", "durmuhurtam": "Dur Muhurat", "varjyam": "Varjyam",
        "upto": "upto", "then": "then", "none_today": "None Today",
        "yearinfo": "Year & Season", "samvatsara": "Samvatsara", "masa": "Masa",
        "ritu": "Ritu", "ayana": "Ayana", "vara": "Vara", "soorya_rasi": "Soorya Rasi",
        "sun_in": "Sun in", "shaka_samvat": "Shaka Samvat", "gata_kali": "Gata Kali",
        "date": "Date", "day": "Day", "godhuli": "Godhuli",
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
        "yearinfo": "సంవత్సర వివరాలు", "samvatsara": "సంవత్సరం", "masa": "మాసం",
        "ritu": "ఋతువు", "ayana": "అయనం", "vara": "వారం", "soorya_rasi": "సూర్య రాశి",
        "sun_in": "సూర్యుడు", "shaka_samvat": "శాలివాహన శకం", "gata_kali": "గత కలి",
        "date": "తేదీ", "day": "వారం", "godhuli": "గోధూళి ముహూర్తం",
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
        "yearinfo": "ஆண்டு விவரங்கள்", "samvatsara": "வருடம்", "masa": "மாதம்",
        "ritu": "ருது", "ayana": "அயனம்", "vara": "வாரம்", "soorya_rasi": "சூரிய ராசி",
        "sun_in": "சூரியன்", "shaka_samvat": "சாலிவாஹன சகம்", "gata_kali": "கத கலி",
        "date": "தேதி", "day": "வாரம்", "godhuli": "கோதூளி முகூர்த்தம்",
    },
}

MONTH_TE = {1:"జనవరి",2:"ఫిబ్రవరి",3:"మార్చి",4:"ఏప్రిల్",5:"మే",6:"జూన్",7:"జూలై",
            8:"ఆగస్టు",9:"సెప్టెంబర్",10:"అక్టోబర్",11:"నవంబర్",12:"డిసెంబర్"}
MONTH_TA = {1:"ஜனவரி",2:"பிப்ரவரி",3:"மார்ச்",4:"ஏப்ரல்",5:"மே",6:"ஜூன்",7:"ஜூலை",
            8:"ஆகஸ்ட்",9:"செப்டம்பர்",10:"அக்டோபர்",11:"நவம்பர்",12:"டிசம்பர்"}
MONTH_ABBR_TO_NUM = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,
                      "Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}


def translate_value(raw, te_map, ta_map):
    """Translate an English panchang value like 'Krishna Ashtami' or
    'Revati upto 04:00 PM, then Ashwini' into Telugu/Tamil, keeping any
    time fragments in numerals as-is."""
    def sub_all(text, mapping, upto_word, then_word):
        # sort by length desc so multi-word names match before substrings
        for en, native in sorted(mapping.items(), key=lambda x: -len(x[0])):
            text = re.sub(r'\b' + re.escape(en) + r'\b', native, text)
        # Telugu/Tamil are SOV languages: "upto TIME" has to become
        # "TIME <upto_word>" (e.g. "06:28 PM వరకు", not "వరకు 06:28 PM")
        # to read grammatically - a straight word-for-word substitution in
        # the English word order is wrong. Swap the token order here.
        text = re.sub(
            r'\bupto\b\s*(\d{1,2}:\d{2}\s*(?:AM|PM))',
            lambda m: f"{m.group(1)} {upto_word}",
            text, flags=re.IGNORECASE,
        )
        text = re.sub(r'\bthen\b', then_word, text)
        return text

    def sub_months(text, month_map):
        # Cross-day date tags (e.g. "Jul 21") should read in the native
        # script too, not drop into raw English mid-sentence.
        def repl(m):
            num = MONTH_ABBR_TO_NUM.get(m.group(1))
            return f"{month_map[num]} {m.group(2)}" if num else m.group(0)
        return re.sub(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})\b', repl, text)

    te = sub_all(raw, te_map, LABELS["te"]["upto"], LABELS["te"]["then"])
    ta = sub_all(raw, ta_map, LABELS["ta"]["upto"], LABELS["ta"]["then"])
    te = sub_months(te, MONTH_TE)
    ta = sub_months(ta, MONTH_TA)
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
    # a chained value like "Chaturdashi upto 06:28 PM, then Amavasya" can
    # be split into many fragments ("Chaturdashi", "upto", "06:28", "PM",
    # "then", "Amavasya", each its own line). find_value() below handles
    # the simple single-fragment case; find_chain_value() and
    # find_window_value() below it handle the two ways a value can span
    # multiple fragments, each with its own stopping rule so one field's
    # leftover fragments can never bleed into the next field's value.
    _BARE_CONTINUE_RE = re.compile(r'^(AM|PM|to|\d{1,2}:\d{2})$', re.IGNORECASE)
    _CHAIN_CONTINUE_RE = re.compile(r'^(AM|PM|to|upto|then|\d{1,2}:\d{2})$', re.IGNORECASE)
    _RANGE_COMPLETE_RE = re.compile(
        r'\d{1,2}:\d{2}\s*(AM|PM)\s*to\s*\d{1,2}:\d{2}\s*(AM|PM)', re.IGNORECASE
    )

    def _join_frags(frags):
        """Join fragments with a comma before "then" for readability
        ("upto 06:28 PM, then Amavasya" instead of a run-on sentence)."""
        out = frags[0]
        for f in frags[1:]:
            if f.lower() == "then":
                out += ","
            out += " " + f
        return re.sub(r'\s+', ' ', out).strip()

    def _first_fragment(label, lookahead):
        """Locate `label` in the bounded region and return the index of the
        next real (non-empty, non-icon, non-label-repeat) line after it, or
        None if not found."""
        for i, l in enumerate(region):
            if l == label:
                for j in range(i + 1, min(i + 1 + lookahead, len(region))):
                    cand = region[j]
                    if cand and cand != label and not cand.startswith("ⓘ"):
                        return j
        return None

    def find_value(label, lookahead=6):
        """Simple lookup for fields that are always a single fragment or a
        single AM/PM-glued time (Weekday, Paksha, Sunrise/Sunset/Moonrise/
        Moonset, and the best-effort year-info fields)."""
        j = _first_fragment(label, lookahead)
        if j is None:
            return None
        frags = [region[j]]
        k = j + 1
        while k < len(region) and k < j + 8 and _BARE_CONTINUE_RE.match(region[k] or ""):
            frags.append(region[k])
            k += 1
        return re.sub(r'\s+', ' ', " ".join(frags)).strip()

    def find_window_value(label, max_windows=1, lookahead=6):
        """For time-range fields (Rahu Kalam, Yamaganda, Gulikai Kalam,
        Brahma Muhurta, Abhijit, Amrit Kalam, Dur Muhurtam, Varjyam). Stops
        as soon as `max_windows` complete "H:MM AM/PM to H:MM AM/PM" ranges
        have been formed, so it can never keep gluing on fragments that
        actually belong to the NEXT field (the bug that produced a garbled
        "01:55 PM to 03:31 PM 03:18 PM to" Gulikai Kalam value in
        production). max_windows=1 for fields that are always a single
        window; =2 for Dur Muhurtam/Varjyam, which can have two."""
        j = _first_fragment(label, lookahead)
        if j is None:
            return None
        frags = [region[j]]
        k = j + 1
        windows_seen = 0
        while k < len(region) and (k - j) < 20:
            cand = region[k]
            if not cand:
                k += 1
                continue
            if not _BARE_CONTINUE_RE.match(cand) and cand.lower() != "then":
                break
            frags.append(cand)
            k += 1
            joined = re.sub(r'\s+', ' ', " ".join(frags))
            windows_seen = len(_RANGE_COMPLETE_RE.findall(joined))
            if windows_seen >= max_windows and cand.lower() != "then":
                break
        return _join_frags(frags)

    def find_chain_value(label, lookahead=6):
        """For fields that name something and can optionally transition to
        a second (or third) name partway through the day - Tithi,
        Nakshatra, Yoga, Karana - rendered as e.g. "Chaturdashi", "upto",
        "06:28", "PM", "then", "Amavasya" each on their own line. Glues
        together the name plus any "upto TIME, then NAME" continuations;
        stops at the first line that doesn't fit that grammar (the next
        field's label)."""
        j = _first_fragment(label, lookahead)
        if j is None:
            return None
        frags = [region[j]]
        k = j + 1
        while k < len(region) and (k - j) < 30:
            cand = region[k]
            if not cand:
                k += 1
                continue
            if _CHAIN_CONTINUE_RE.match(cand):
                frags.append(cand)
                k += 1
                continue
            # A bare word immediately after "then" is the next segment's
            # name (e.g. "...then", "Amavasya") - anything else means we've
            # hit unrelated content (the next field's label) and should stop.
            if frags[-1].lower() == "then" and re.match(r'^[A-Za-z ]+$', cand):
                frags.append(cand)
                k += 1
                continue
            break
        return _join_frags(frags)

    data = {}
    data["tithi"] = find_chain_value("Tithi")
    data["nakshatra"] = find_chain_value("Nakshatra")
    data["yoga"] = find_chain_value("Yoga")
    data["karana"] = find_chain_value("Karana")
    data["weekday"] = find_value("Weekday")
    data["paksha"] = find_value("Paksha")
    data["sunrise"] = find_value("Sunrise")
    data["sunset"] = find_value("Sunset")
    data["moonrise"] = find_value("Moonrise")
    data["moonset"] = find_value("Moonset")
    data["brahma_muhurta"] = find_window_value("Brahma Muhurta", max_windows=1)
    data["abhijit"] = find_window_value("Abhijit", max_windows=1)
    data["amrit_kalam"] = find_window_value("Amrit Kalam", max_windows=1)
    data["rahu_kalam"] = find_window_value("Rahu Kalam", max_windows=1)
    data["yamaganda"] = find_window_value("Yamaganda", max_windows=1)
    data["gulikai_kalam"] = find_window_value("Gulikai Kalam", max_windows=1)
    data["durmuhurtam"] = find_window_value("Dur Muhurtam", max_windows=2)
    data["varjyam"] = find_window_value("Varjyam", max_windows=2)

    # --- Best-effort extras: Samvatsara (year name), lunar month, season,
    # and ayana. These live further down the same page, further from the
    # nav-menu collision risk that affected Tithi/Nakshatra, but the
    # Amanta month name in particular needs a bespoke scan (Drik Panchang
    # lists the Purnimanta name first, then an unrelated "Pravishte/Gate"
    # field, THEN the Amanta name we actually want) rather than the simple
    # next-line lookup used above. If anything here can't be found, we
    # degrade gracefully to "-" rather than failing the whole run - these
    # aren't covered by validate_data()'s hard fail-safe.
    # IMPORTANT: the samvatsara (year) NAME must come from Shaka Samvat, not
    # the generic "Samvatsara" field (which Drik Panchang ties to Vikram
    # Samvat, a North Indian lunar-calendar system). Telugu Ugadi tradition
    # follows the Shaka Samvat, and the two systems' 60-year Jupiter cycles
    # can be several names apart in any given year - e.g. this field once
    # showed "Siddharthi" (Vikram-based) when the correct Telugu year name
    # was "Parabhava" (Shaka-based). The Shaka Samvat line looks like
    # "1948 Parabhava" - a number then the name.
    shaka_raw = find_value("Shaka Samvat")
    data["samvatsara"] = None
    data["shaka_year"] = None
    if shaka_raw:
        m = re.match(r'^(\d+)\s+([A-Za-z]+)', shaka_raw.strip())
        if m:
            data["shaka_year"] = m.group(1)
            data["samvatsara"] = m.group(2)
        else:
            data["samvatsara"] = shaka_raw.strip()

    data["masa"] = None
    masa_idx = next((i for i, l in enumerate(region) if l == "Chandramasa"), None)
    if masa_idx is not None:
        window = region[masa_idx + 1: masa_idx + 25]
        # Try the single-line form first: "Jyeshtha - Amanta"
        amanta_re = re.compile(r'^([A-Za-z]+)\s*-\s*Amanta$', re.IGNORECASE)
        for l in window:
            m = amanta_re.match(l or "")
            if m:
                data["masa"] = m.group(1)
                break
        # Fall back to a fragmented form where "Amanta" is its own line and
        # the month name is one of the few non-empty lines just before it.
        if not data["masa"]:
            for idx, l in enumerate(window):
                if (l or "").strip().lower() == "amanta":
                    back = [x for x in window[max(0, idx - 3):idx] if x and x != "-"]
                    if back:
                        data["masa"] = back[-1]
                    break
        # Last resort: whatever name follows "Chandramasa" directly (the
        # Purnimanta name), stripping a trailing "- Purnimanta" suffix if
        # present - not the Amanta name we prefer, but better than "-".
        if not data["masa"] and window and window[0]:
            data["masa"] = re.sub(r'\s*-\s*Purnimanta$', '', window[0], flags=re.IGNORECASE).strip() or None

    ritu_raw = find_value("Vedic Ritu")
    data["ritu"] = ritu_raw.split(" (")[0].strip() if ritu_raw else None

    data["ayana"] = find_value("Vedic Ayana")

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
    r'(\d{1,2}:\d{2}\s*(AM|PM))|(\bNone\b)|(\bWhole Day\b)|(\bNo Moon(rise|set)\b)|(\bNo Sun(rise|set)\b)',
    re.IGNORECASE
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
                   "rahu_kalam", "yamaganda", "gulikai_kalam", "durmuhurtam", "varjyam"]
    for field in time_fields:
        val = data.get(field)
        if not val:
            problems.append(f"{field} is missing")
        elif not TIME_RE.search(val):
            problems.append(f"{field}='{val}' does not look like a valid time")

    # Abhijit Muhurta and Amrit Kalam are genuinely absent from the source
    # page on some days (Abhijit doesn't occur on certain weekdays; Amrit
    # Kalam depends on the Moon's position and some days it simply doesn't
    # fall during a usable window) - that's real panchang behavior, not a
    # scraping failure, so a missing value here should not block sending
    # the rest of the day's card. Only flag it if something WAS scraped but
    # doesn't look like a time (that would be an actual parsing bug).
    for field in ("abhijit", "amrit_kalam"):
        val = data.get(field)
        if val and val.lower() != "none" and not TIME_RE.search(val):
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

# Reverted back to the original, known-safe values - a prior attempt to
# reclaim a few extra pixels here (measuring only the flat template color
# band, not the actual drawn header/footer graphics on top of it) caused
# real overflow in production.
# Measured directly from the real "DAILY Panchangam" template (header logo +
# temple timings baked into the top band, deity photo + temple name/address/
# phone baked into a band lower down). The template image itself has been
# cropped to end shortly after the footer band - the original export had a
# large blank margin below the footer that just wasted space and made the
# card look bottom-heavy/unbalanced, so it's gone from the asset now.
HEADER_FRAC = 0.1242
FOOTER_FRAC = 0.8071
# The actual purple header/footer bands in the template are noticeably
# shorter than the safety margins above (measured directly from the
# image: header ends ~y=226/2245, footer starts ~y=1922/2245) - used only
# for the cream background fill below, so that fill reaches the real
# edges of the purple bands instead of stopping short and leaving a
# visible white gap between the card and the header/footer.
HEADER_VISUAL_BOTTOM_FRAC = 0.1007
FOOTER_VISUAL_TOP_FRAC = 0.8561
LEFT_FRAC = 0.045
RIGHT_FRAC = 0.955

# Minimal palette: plain white background throughout, no filled color boxes.
# A single purple accent (matches the temple's own header/footer bars) is
# used only for section-title text; every data value is bold near-black for
# max readability; thin grey lines divide sections instead of color fills.
ACCENT_COL = (95, 40, 130)      # kept for reference; no longer used for the content-area boxes
TEXT_COL = (18, 14, 10)         # near-black, max contrast on white
LINE_COL = (205, 200, 210)      # thin divider / border lines
SUBTITLE_COL = (95, 40, 130)
# Content-area palette: matches the reference's warm parchment card look
# (cream background, gold-bronze borders, dark maroon-brown text) instead
# of white/purple - the header/footer purple banner is left untouched,
# but a plain white card sitting right below a purple banner read as a
# mismatched, thin, under-scaled design, not the "big, elegant" reference.
CARD_BG_COL = (247, 237, 213)
BORDER_COL = (185, 138, 52)
LABEL_COL = (109, 27, 27)
VALUE_COL = (46, 27, 14)
# Functional pill-header colors for the two timing boxes - muted versions
# of the reference's maroon/green (the one place a second/third color is
# used, since it signals "avoid this" vs "good time for this" rather than
# just being decorative theme).
INAUSPICIOUS_PILL_COL = (133, 51, 51)
AUSPICIOUS_PILL_COL = (44, 105, 71)

def font_for(lang, weight, size):
    if lang == "en":
        path = "Poppins-Bold.ttf" if weight == "bold" else "Poppins-Medium.ttf"
        return ImageFont.truetype(os.path.join(FONT_DIR, path), size)
    # Full, official Google Fonts variable-weight files (not the earlier
    # hand-merged subsets) - complete glyph coverage for the script, Latin,
    # and digits in one file. Dial in the weight axis so headers/values get
    # real bold vs regular, matching the English card's hierarchy.
    path = "NotoSansTelugu-Full.ttf" if lang == "te" else "NotoSansTamil-Full.ttf"
    font = ImageFont.truetype(os.path.join(FONT_DIR, path), size)
    try:
        font.set_variation_by_axes([700 if weight == "bold" else 400, 100])
    except Exception:
        pass
    return font


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


def _row_geom(draw, label, value, label_font, value_font, avail_w, bullet_d, gap):
    """Measures one 'bullet  LABEL : value' row, wrapping the value (with
    a hanging indent starting right after that row's own label) if it
    doesn't fit - matching the reference's inline 'LABEL : VALUE' style
    rather than a shared column across the whole box."""
    lbl_text = f"{label} :"
    bbox = draw.textbbox((0, 0), lbl_text, font=label_font)
    lbl_w = bbox[2] - bbox[0]
    x_off = bullet_d + gap + lbl_w + gap
    val_w = max(avail_w - x_off, int(avail_w * 0.25))
    lines = _wrap_lines(draw, value, value_font, val_w)
    return {"lbl_text": lbl_text, "lines": lines, "x_off": x_off}


def _measure_box_rows(draw, rows, label_font, value_font, box_w, row_h, pad, bullet_d, gap):
    inner_w = box_w - 2 * pad - bullet_d - gap
    rows_geom = []
    total_h = 0
    for lbl, val in rows:
        g = _row_geom(draw, lbl, val, label_font, value_font, inner_w, bullet_d, gap)
        total_h += max(len(g["lines"]), 1) * row_h
    for lbl, val in rows:
        g = _row_geom(draw, lbl, val, label_font, value_font, inner_w, bullet_d, gap)
        rows_geom.append(g)
    return rows_geom, total_h


def _draw_box_rows(d, rows_geom, x, y, row_h, label_font, value_font, bullet_d, gap,
                    bullet_col, label_col, value_col, extra_step=0):
    """extra_step adds breathing room between (single-line) rows without
    changing font size, so a box that naturally needs less height than
    its neighbor can be stretched to fill it evenly, rather than leaving
    one lump of blank space at the bottom."""
    ry = y
    for g in rows_geom:
        n = max(len(g["lines"]), 1)
        slot_h = n * row_h + extra_step
        ty = ry + (extra_step / 2 if n == 1 else 0)
        by = ry + slot_h / 2 if n == 1 else ty + row_h / 2
        r = bullet_d / 2
        d.ellipse([x, by - r, x + bullet_d, by + r], fill=bullet_col)
        d.text((x + bullet_d + gap, ty), g["lbl_text"], font=label_font, fill=label_col)
        vx, vy = x + g["x_off"], ty
        for line in g["lines"]:
            d.text((vx, vy), line, font=value_font, fill=value_col)
            vy += row_h
        ry += slot_h
    return ry


def _measure_v2(draw, lang, blocks, content_w, font_scale, scale):
    """Compute the box-based layout's geometry at a given font_scale
    WITHOUT drawing anything, so render_card can auto-shrink the font
    until everything fits between the template's header and footer bars.

    blocks = {
        "info_bar": {"tl": str, "tr": str, "bl": str, "br": str} (2x2 grid -
                     Shaka Samvat/Date on top, Gata Kali/Day below - the
                     reference's prominent top info band),
        "left_box": [(label, value), ...] (Samvatsara through Karana, one
                     bordered box, each field its own bulleted row),
        "right_boxes": [{"pill": str, "pill_color": (r,g,b),
                          "rows": [(label, value), ...]}, ...] (Inauspicious
                         Timings / Auspicious Timings, each its own
                         bordered box with a solid-color pill header),
        "bottom_box": [(label, value), ...] (Sunrise/Sunset/Moonrise/
                       Moonset, one full-width bordered box).
    }
    """
    label_size = max(int(34 * scale * font_scale), 14)
    value_size = max(int(34 * scale * font_scale), 14)
    pill_size = max(int(33 * scale * font_scale), 14)

    fonts = {
        "label": font_for(lang, "bold", label_size),
        "value": font_for(lang, "medium", value_size),
        "pill": font_for(lang, "bold", pill_size),
    }

    pad = max(int(20 * scale * font_scale), 10)
    bullet_d = max(int(20 * scale * font_scale), 10)
    gap = max(int(10 * scale * font_scale), 5)
    col_gap = max(int(24 * scale * font_scale), 12)
    box_gap = max(int(18 * scale * font_scale), 9)
    row_h = value_size + int(18 * scale * font_scale)
    pill_h = pill_size + int(24 * scale * font_scale)
    border_w = max(int(3 * scale), 2)
    radius = max(int(16 * scale), 7)

    left_col_w = int((content_w - col_gap) * 0.47)
    right_col_w = content_w - col_gap - left_col_w

    left_rows_geom, left_rows_h = _measure_box_rows(
        draw, blocks["left_box"], fonts["label"], fonts["value"],
        left_col_w, row_h, pad, bullet_d, gap)
    left_box_h = left_rows_h + 2 * pad

    right_boxes_geom = []
    right_col_h = 0
    for grp in blocks["right_boxes"]:
        rows_geom, rows_h = _measure_box_rows(
            draw, grp["rows"], fonts["label"], fonts["value"],
            right_col_w, row_h, pad, bullet_d, gap)
        box_h = pill_h + rows_h + 2 * pad
        right_boxes_geom.append({"pill": grp["pill"], "pill_color": grp["pill_color"],
                                  "rows": rows_geom, "h": box_h})
        right_col_h += box_h + box_gap
    right_col_h -= box_gap if right_boxes_geom else 0

    main_row_h = max(left_box_h, right_col_h)

    bottom_rows_geom, bottom_rows_h = _measure_box_rows(
        draw, blocks["bottom_box"], fonts["label"], fonts["value"],
        content_w, row_h, pad, bullet_d, gap)
    bottom_box_h = bottom_rows_h + 2 * pad

    total_h = box_gap + main_row_h + box_gap + bottom_box_h

    return {
        "total_h": total_h, "main_row_h": main_row_h,
        "left_col_w": left_col_w, "right_col_w": right_col_w,
        "left_box_h": left_box_h, "right_col_h": right_col_h, "bottom_box_h": bottom_box_h,
        "left_rows_geom": left_rows_geom, "right_boxes_geom": right_boxes_geom,
        "bottom_rows_geom": bottom_rows_geom,
        "fonts": fonts, "pad": pad, "bullet_d": bullet_d, "gap": gap,
        "col_gap": col_gap, "box_gap": box_gap, "row_h": row_h, "pill_h": pill_h,
        "border_w": border_w, "radius": radius,
    }


def render_card(lang, blocks, outpath):
    """Draws a bordered-box Panchang card onto the temple's template
    image, modeled closely on the reference layout: a large 2x2 info grid
    (Shaka Samvat/Gata Kali left, Date/Day right - deliberately the
    biggest text on the card, readable at a glance), a left box listing
    Samvatsara through Karana as bulleted 'Label : Value' rows, a right
    column of two pill-headed boxes (Inauspicious/Auspicious Timings),
    and a bottom box for Sunrise/Sunset/Moonrise/Moonset - all on a warm
    parchment-cream background with gold-bronze borders (matching the
    reference's card look), composited onto this project's own header/
    footer template. Font size auto-shrinks as needed so nothing ever
    overlaps or runs past the footer bar."""
    base = Image.open(TEMPLATE_PATH).convert("RGB")
    W, H = base.size
    img = base.copy()
    d = ImageDraw.Draw(img)

    top = int(H * HEADER_FRAC)
    bottom = int(H * FOOTER_VISUAL_TOP_FRAC) - int(H * 0.014)
    left = int(W * LEFT_FRAC)
    right = int(W * RIGHT_FRAC)
    content_w = right - left
    scale = W / 1587.0
    content_top = int(H * HEADER_VISUAL_BOTTOM_FRAC) + int(H * 0.012)

    # Fill the whole content area with the warm parchment background
    # first, so the card reads as one cohesive piece rather than a thin
    # white strip wedged between the purple header and footer bands.
    # Uses the actual measured band edges (see the *_VISUAL_* constants),
    # not the more conservative content-safety top/bottom, so it reaches
    # all the way to the purple without a leftover white gap.
    d.rectangle([0, int(H * HEADER_VISUAL_BOTTOM_FRAC), W, int(H * FOOTER_VISUAL_TOP_FRAC)],
                fill=CARD_BG_COL)

    # Info grid gets its own sizing pass first, deliberately the biggest
    # text on the card (matching the reference's prominent date section,
    # readable at first glance) - a 2x2 grid: Shaka Samvat / Date on top,
    # Gata Kali / Day below.
    cells = [blocks["info_bar"]["tl"], blocks["info_bar"]["tr"],
             blocks["info_bar"]["bl"], blocks["info_bar"]["br"]]
    half_w = content_w / 2
    cell_inner_w = half_w - int(30 * scale)
    info_size = max(int(46 * scale), 16)
    while info_size > 15:
        f_info = font_for(lang, "bold", info_size)
        widths = [d.textbbox((0, 0), c, font=f_info)[2] - d.textbbox((0, 0), c, font=f_info)[0]
                  for c in cells]
        if max(widths) <= cell_inner_w:
            break
        info_size -= 1
    else:
        f_info = font_for(lang, "bold", info_size)
    info_line_h = info_size + int(16 * scale)
    info_pad = max(int(18 * scale), 9)
    info_h = 2 * info_line_h + 2 * info_pad
    box_gap0 = max(int(20 * scale), 10)
    available = bottom - content_top - info_h - box_gap0

    font_scale = 1.4
    m = None
    while True:
        m = _measure_v2(d, lang, blocks, content_w, font_scale, scale)
        if m["total_h"] <= available or font_scale <= 0.22:
            break
        font_scale -= 0.02

    squeeze = 1.0
    if m["total_h"] > available > 0:
        squeeze = max(available / m["total_h"], 0.5)

    row_h = m["row_h"] * squeeze
    pill_h = m["pill_h"] * squeeze
    box_gap = m["box_gap"] * squeeze
    pad = m["pad"]
    col_gap = m["col_gap"]
    left_col_w = m["left_col_w"]
    right_col_w = m["right_col_w"]
    border_w = m["border_w"]
    radius = m["radius"]
    bullet_d = m["bullet_d"]
    gap = m["gap"]
    fonts = m["fonts"]

    def _rows_h(rows_geom, row_h_):
        return sum(max(len(g["lines"]), 1) * row_h_ for g in rows_geom)

    left_box_h = _rows_h(m["left_rows_geom"], row_h) + 2 * pad
    right_col_h = sum(pill_h + _rows_h(g["rows"], row_h) + 2 * pad for g in m["right_boxes_geom"])
    right_col_h += box_gap * max(len(m["right_boxes_geom"]) - 1, 0)
    main_row_h = max(left_box_h, right_col_h)
    bottom_box_h = _rows_h(m["bottom_rows_geom"], row_h) + 2 * pad

    # Any leftover vertical space goes into stretching the bottom box and
    # the gaps between rows in whichever main-row box is shorter, so the
    # card fills the full available height instead of leaving dead white
    # space - matching the reference's fully-used real estate.
    boxes_total_h = box_gap + main_row_h + box_gap + bottom_box_h
    leftover = max(0, available - boxes_total_h)
    left_extra_step = 0
    if leftover > 0:
        extra = leftover / 2
        bottom_box_h += extra
        # Stretch the left box's rows to fill main_row_h if it's the
        # shorter side (right column's pill boxes are already at
        # main_row_h by construction when they're the taller one).
        if left_box_h < main_row_h:
            n_left = len(m["left_rows_geom"])
            left_extra_step = (main_row_h - left_box_h) / max(n_left, 1)

    y = content_top

    # Info grid: bordered box, 2x2 - Shaka Samvat/Date on top, Gata
    # Kali/Day below.
    d.rounded_rectangle([left, y, right, y + info_h], radius=radius,
                         outline=BORDER_COL, width=border_w)
    mid_x = left + content_w / 2
    ly1 = y + info_pad
    ly2 = y + info_pad + info_line_h
    d.text((left + info_pad, ly1), cells[0], font=f_info, fill=LABEL_COL, anchor="la")
    d.text((mid_x + info_pad / 2, ly1), cells[1], font=f_info, fill=LABEL_COL, anchor="la")
    d.text((left + info_pad, ly2), cells[2], font=f_info, fill=LABEL_COL, anchor="la")
    d.text((mid_x + info_pad / 2, ly2), cells[3], font=f_info, fill=LABEL_COL, anchor="la")
    d.line([(mid_x, y + info_pad / 2), (mid_x, y + info_h - info_pad / 2)],
           fill=BORDER_COL, width=max(1, border_w - 1))
    y += info_h + box_gap0

    # Left box: Samvatsara through Karana, stretched to fill main_row_h
    # if it's naturally shorter than the right column.
    d.rounded_rectangle([left, y, left + left_col_w, y + main_row_h], radius=radius,
                         outline=BORDER_COL, width=border_w)
    _draw_box_rows(d, m["left_rows_geom"], left + pad, y + pad, row_h,
                    fonts["label"], fonts["value"], bullet_d, gap,
                    LABEL_COL, LABEL_COL, VALUE_COL, extra_step=left_extra_step)

    # Right column: pill-headed Inauspicious/Auspicious boxes.
    rx = left + left_col_w + col_gap
    ry = y
    for grp in m["right_boxes_geom"]:
        rows_h = _rows_h(grp["rows"], row_h)
        box_h = rows_h + 2 * pad + pill_h
        d.rounded_rectangle([rx, ry, rx + right_col_w, ry + box_h], radius=radius,
                             outline=BORDER_COL, width=border_w)
        d.rounded_rectangle([rx, ry, rx + right_col_w, ry + pill_h],
                             radius=radius, fill=grp["pill_color"])
        # Square off the pill's bottom corners so it reads as a header
        # bar flush with the box below it, not a floating rounded pill.
        d.rectangle([rx, ry + pill_h - radius, rx + right_col_w, ry + pill_h],
                     fill=grp["pill_color"])
        d.text((rx + right_col_w / 2, ry + pill_h / 2), grp["pill"],
                font=fonts["pill"], fill=(255, 255, 255), anchor="mm")
        _draw_box_rows(d, grp["rows"], rx + pad, ry + pill_h + pad, row_h,
                        fonts["label"], fonts["value"], bullet_d, gap,
                        grp["pill_color"], LABEL_COL, VALUE_COL)
        ry += box_h + box_gap

    y += main_row_h + box_gap

    # Bottom box: Sunrise/Sunset/Moonrise/Moonset, full width.
    d.rounded_rectangle([left, y, right, y + bottom_box_h], radius=radius,
                         outline=BORDER_COL, width=border_w)
    bottom_rows_h = _rows_h(m["bottom_rows_geom"], row_h)
    n_bottom = len(m["bottom_rows_geom"])
    bottom_extra_step = max(0, (bottom_box_h - 2 * pad - bottom_rows_h) / max(n_bottom, 1))
    _draw_box_rows(d, m["bottom_rows_geom"], left + pad, y + pad, row_h,
                    fonts["label"], fonts["value"], bullet_d, gap,
                    LABEL_COL, LABEL_COL, VALUE_COL, extra_step=bottom_extra_step)

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

    # Best-effort extras - fall back to the raw (untranslated) value if a
    # name isn't in our table, rather than showing a blank.
    samv_raw = data.get("samvatsara")
    te_samv = SAMVATSARA_TE.get(samv_raw, samv_raw) if samv_raw else "-"
    ta_samv = SAMVATSARA_TA.get(samv_raw, samv_raw) if samv_raw else "-"

    masa_raw = data.get("masa")
    te_masa = f"{MASA_TE.get(masa_raw, masa_raw)} మాసం" if masa_raw else "-"
    ta_masa = f"{MASA_TA.get(masa_raw, masa_raw)} மாதம்" if masa_raw else "-"
    en_masa = masa_raw or "-"

    ritu_raw = data.get("ritu")
    te_ritu = RITU_TE.get(ritu_raw, f"{ritu_raw} Ritu" if ritu_raw else "-")
    ta_ritu = RITU_TA.get(ritu_raw, f"{ritu_raw} Ritu" if ritu_raw else "-")
    en_ritu = ritu_raw or "-"

    ayana_raw = data.get("ayana")
    te_ayana = AYANA_TE.get(ayana_raw, ayana_raw) if ayana_raw else "-"
    ta_ayana = AYANA_TA.get(ayana_raw, ayana_raw) if ayana_raw else "-"
    en_ayana = ayana_raw or "-"

    surya_raw = data.get("surya_rasi")
    en_surya = f"{surya_raw} ({RASHI_EN_WESTERN[surya_raw]})" if surya_raw else "-"
    te_surya = RASHI_TE.get(surya_raw, surya_raw) if surya_raw else "-"
    ta_surya = RASHI_TA.get(surya_raw, surya_raw) if surya_raw else "-"

    outputs = []
    for lang, tithi, nak, yoga, kar, paksha, weekday, month_name, samv, masa, ritu, ayana, surya in [
        ("en", apply_en_overrides(data["tithi"]), apply_en_overrides(data["nakshatra"]),
         apply_en_overrides(data["yoga"]), apply_en_overrides(data["karana"]),
         data["paksha"], data["weekday_full"], dt_ist.strftime("%B"),
         (samv_raw or "-"), en_masa, en_ritu, en_ayana, en_surya),
        ("te", te_tithi, te_nak, te_yoga, te_kar, te_paksha, te_weekday, MONTH_TE[dt_ist.month],
         te_samv, te_masa, te_ritu, te_ayana, te_surya),
        ("ta", ta_tithi, ta_nak, ta_yoga, ta_kar, ta_paksha, ta_weekday, MONTH_TA[dt_ist.month],
         ta_samv, ta_masa, ta_ritu, ta_ayana, ta_surya),
    ]:
        L = LABELS[lang]
        city = {"en": CITY_LABEL_EN, "te": CITY_LABEL_TE, "ta": CITY_LABEL_TA}[lang]
        if lang == "en":
            date_str = dt_ist.strftime("%B %d, %Y")
        else:
            date_str = f"{month_name} {dt_ist.day:02d}, {dt_ist.year}"

        abhijit_val = data["abhijit"] if data["abhijit"] and data["abhijit"].lower() != "none" else L["none_today"]
        amrit_val = data["amrit_kalam"] if data["amrit_kalam"] and data["amrit_kalam"].lower() != "none" else L["none_today"]

        def _na_if_absent(val):
            # Some days genuinely have no moonrise/moonset within the
            # calendar day window (common near Amavasya) and Drik Panchang
            # says so in plain English regardless of card language - show
            # the same "not applicable today" phrasing used elsewhere
            # instead of leaving untranslated English sitting in a TE/TA card.
            if val and re.match(r'^no (moon|sun)(rise|set)$', val.strip(), re.IGNORECASE):
                return L["none_today"]
            return val

        moonrise_val = _na_if_absent(data["moonrise"]) or "-"
        moonset_val = _na_if_absent(data["moonset"]) or "-"

        # Box layout matched closely to the reference design: a top info
        # strip (Shaka Samvat/Gata Kali left, Date/Day right), a left box
        # of Samvatsara through Karana as bulleted "Label : Value" rows
        # (Tithi/Nakshatra/Yoga/Karana shown in the reference's compact
        # "Name (end time)" form via fmt_compact_chain), a right column of
        # pill-headed Inauspicious/Auspicious Timings boxes, and a bottom
        # box for Sunrise/Sunset/Moonrise/Moonset - kept on this project's
        # own header/footer template as before.
        shaka_year = data.get("shaka_year") or "-"
        gata_kali = data.get("gata_kali")
        info_bar = {
            "tl": f"{L['shaka_samvat']}: {shaka_year} {samv}",
            "tr": f"{L['date']}: {date_str}",
            "bl": f"{L['gata_kali']}: {gata_kali}" if gata_kali else f"{L['gata_kali']}: -",
            "br": f"{L['day']}: {weekday}",
        }

        godhuli_val = fmt_range_display(data.get("godhuli")) or "-"

        blocks = {
            "info_bar": info_bar,
            "left_box": [
                (L["samvatsara"], samv),
                (L["ayana"], ayana),
                (L["ritu"], ritu),
                (L["masa"], masa),
                (L["paksha"], paksha or "-"),
                (L["tithi"], fmt_compact_chain(tithi, lang) or "-"),
                (L["vara"], weekday),
                (L["nakshatra"], fmt_compact_chain(nak, lang) or "-"),
                (L["yoga"], fmt_compact_chain(yoga, lang) or "-"),
                (L["karana"], fmt_compact_chain(kar, lang) or "-"),
            ],
            "right_boxes": [
                {"pill": L["inauspicious"], "pill_color": INAUSPICIOUS_PILL_COL, "rows": [
                    (L["rahu"], fmt_range_display(data["rahu_kalam"]) or "-"),
                    (L["gulika"], fmt_range_display(data["gulikai_kalam"]) or "-"),
                    (L["yama"], fmt_range_display(data["yamaganda"]) or "-"),
                    (L["durmuhurtam"], fmt_range_display(data["durmuhurtam"]) or "-"),
                    (L["varjyam"], fmt_range_display(data["varjyam"]) or "-"),
                ]},
                {"pill": L["auspicious"], "pill_color": AUSPICIOUS_PILL_COL, "rows": [
                    (L["brahma"], fmt_range_display(data["brahma_muhurta"]) or "-"),
                    (L["abhijit"], fmt_range_display(abhijit_val)),
                    (L["amrit"], fmt_range_display(amrit_val)),
                    (L["godhuli"], godhuli_val),
                ]},
            ],
            "bottom_box": [
                (L["sunrise"], data["sunrise"] or "-"),
                (L["sunset"], data["sunset"] or "-"),
                (L["moonrise"], moonrise_val),
                (L["moonset"], moonset_val),
            ],
        }

        outpath = os.path.join(HERE, f"panchangam_{lang}.jpg")
        render_card(lang, blocks, outpath)
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


# TextMeBot returns HTTP 200 even when a message was NOT actually delivered
# (e.g. an expired trial/subscription, or a bad API key) - the real failure
# only shows up in the response body text, not the status code. Without this
# check, the script (and the GitHub Actions job) reports "success" while
# nothing actually reaches WhatsApp, which is worse than an honest failure -
# this is exactly what happened when the TextMeBot trial expired.
_SEND_FAILURE_SIGNATURES = (
    "trial period is over",
    "click here to subscribe",
    "invalid apikey",
    "invalid api key",
)


def _send_ok(resp):
    """True only if TextMeBot's response actually indicates delivery, not
    just an HTTP 200."""
    if resp is None or resp.status_code != 200:
        return False
    body = resp.text.lower()
    return not any(sig in body for sig in _SEND_FAILURE_SIGNATURES)


# --------------------------------------------------------------------------
# Ephemeris-based Tithi engine
# --------------------------------------------------------------------------
# Tithi used to come from scraping drikpanchang.com's rendered HTML, which
# was fragile (see the chain-parsing bugs fixed earlier) and gave us no way
# to independently verify correctness. This computes Tithi directly from the
# Sun and Moon's true sidereal positions (Swiss Ephemeris, Lahiri ayanamsha -
# the same standard Drik Panchang itself uses), verified to match Drik
# Panchang's own published Tithi transition times to within about a minute
# across multiple test dates. It follows the same sunrise-to-sunrise day
# convention Drik Panchang uses: the Tithi named for a given date is the one
# active AT that date's sunrise, plus any transition(s) before the next
# sunrise.

TITHI_NAMES = [
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi", "Saptami",
    "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi", "Purnima",
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi", "Saptami",
    "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi", "Amavasya",
]


def _jd_from_ist(dt_ist):
    dt_utc = dt_ist.astimezone(timezone.utc)
    return swe.julday(dt_utc.year, dt_utc.month, dt_utc.day,
                       dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600)


def _jd_to_ist(jd):
    y, m, d, h = swe.revjul(jd)
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = round((((h - hh) * 60) - mm) * 60)
    if ss == 60:
        ss = 0
        mm += 1
    if mm == 60:
        mm = 0
        hh += 1
    dt_utc = datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)
    return dt_utc.astimezone(IST)


def _sunrise_ist(local_midnight_ist):
    jd = _jd_from_ist(local_midnight_ist)
    _, tret = swe.rise_trans(jd, swe.SUN, swe.CALC_RISE, (TIRUPATI_LON, TIRUPATI_LAT, 0))
    return _jd_to_ist(tret[0])


def _tithi_angle(dt_ist):
    jd = _jd_from_ist(dt_ist)
    moon, _ = swe.calc_ut(jd, swe.MOON, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    sun, _ = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    return (moon[0] - sun[0]) % 360


def _unwrap_near(d, target):
    while d < target - 180:
        d += 360
    while d > target + 180:
        d -= 360
    return d


def _find_tithi_boundary(target_deg, lo, hi):
    def f(t):
        return _unwrap_near(_tithi_angle(t), target_deg) - target_deg
    flo, fhi = f(lo), f(hi)
    if (flo < 0) == (fhi < 0):
        return None  # no crossing in this window
    for _ in range(60):
        mid = lo + (hi - lo) / 2
        fm = f(mid)
        if (flo < 0) == (fm < 0):
            lo, flo = mid, fm
        else:
            hi, fhi = mid, fm
    return lo + (hi - lo) / 2


def compute_tithi_chain(y, m, d, sunrise_override=None):
    """Returns a string in the same 'Name upto TIME, then Name2' format the
    old scraper produced, e.g. 'Shashthi upto 03:29 AM, then Saptami', or
    just 'Amavasya' if there's no transition before the next sunrise.

    sunrise_override: an IST datetime for THIS date's actual sunrise (from
    the live Drik Panchang scrape), used in place of the offline-ephemeris
    estimate for determining which tithi is active at sunrise - the
    estimate has been measured a few minutes off from Drik Panchang's own
    figure (likely refraction-model or geoid differences), which could
    misidentify the tithi if a transition happens to fall in that gap.
    Tomorrow's sunrise (only used as a search-window bound, not to decide
    which tithi is "today's") still comes from the estimate, where a few
    minutes of imprecision doesn't affect correctness."""
    midnight = datetime(y, m, d, 0, 0, tzinfo=IST)
    sr0 = sunrise_override if sunrise_override else _sunrise_ist(midnight)
    sr1 = _sunrise_ist(midnight + timedelta(days=1))

    idx = int(_tithi_angle(sr0) // 12)
    segments = [[TITHI_NAMES[idx], None]]
    t = sr0
    for _ in range(5):  # a day can't have more than a couple of transitions
        boundary_deg = ((idx + 1) * 12) % 360
        b = _find_tithi_boundary(boundary_deg, t, sr1)
        if b is None:
            break
        idx = (idx + 1) % 30
        segments[-1][1] = b
        segments.append([TITHI_NAMES[idx], None])
        t = b

    # If a transition falls after midnight but before the NEXT sunrise, it's
    # still part of this card's Panchangam day (which runs sunrise-to-
    # sunrise, not midnight-to-midnight) - but its clock time alone
    # ("04:03 AM") would look like it happened earlier TODAY if shown
    # without a date tag, when it actually happens tomorrow morning. Drik
    # Panchang itself disambiguates this with a explicit date suffix (e.g.
    # "upto 03:29 AM, Jul 20"); do the same here rather than silently
    # dropping it, which would misrepresent how long the tithi actually
    # lasts (this was a real bug caught via a July 20 card showing an
    # apparent 33-minute Tithi that was actually ~24.5 hours).
    card_date = midnight.date()
    parts = []
    for name, end in segments:
        if end is None:
            parts.append(name)
        else:
            tag = "" if end.date() == card_date else f", {end.strftime('%b %d')}"
            parts.append(f"{name} upto {end.strftime('%I:%M %p')}{tag}, then")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Soorya Rasi (Sun's sidereal zodiac sign) - the same Lahiri-ayanamsha Sun
# position already computed for the Tithi engine above, just read off as a
# 30-degree zodiac slot instead of a Sun-Moon angle. Computed at sunrise,
# matching how a printed Panchangam names "today's" Rasi.
# --------------------------------------------------------------------------

RASHI_NAMES = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya",
               "Tula", "Vrischika", "Dhanu", "Makara", "Kumbha", "Meena"]

RASHI_EN_WESTERN = {
    "Mesha": "Aries", "Vrishabha": "Taurus", "Mithuna": "Gemini", "Karka": "Cancer",
    "Simha": "Leo", "Kanya": "Virgo", "Tula": "Libra", "Vrischika": "Scorpio",
    "Dhanu": "Sagittarius", "Makara": "Capricorn", "Kumbha": "Aquarius", "Meena": "Pisces",
}

RASHI_TE = {
    "Mesha": "మేషం", "Vrishabha": "వృషభం", "Mithuna": "మిథునం", "Karka": "కర్కాటకం",
    "Simha": "సింహం", "Kanya": "కన్య", "Tula": "తుల", "Vrischika": "వృశ్చికం",
    "Dhanu": "ధనుస్సు", "Makara": "మకరం", "Kumbha": "కుంభం", "Meena": "మీనం",
}

RASHI_TA = {
    "Mesha": "மேஷம்", "Vrishabha": "ரிஷபம்", "Mithuna": "மிதுனம்", "Karka": "கடகம்",
    "Simha": "சிம்மம்", "Kanya": "கன்னி", "Tula": "துலாம்", "Vrischika": "விருச்சிகம்",
    "Dhanu": "தனுசு", "Makara": "மகரம்", "Kumbha": "கும்பம்", "Meena": "மீனம்",
}


def compute_surya_rasi(y, m, d, sunrise_override=None):
    """Returns the Sanskrit rashi name (e.g. 'Simha') the Sun occupies at
    sunrise, sidereal/Lahiri - same convention as compute_tithi_chain.
    sunrise_override: see compute_tithi_chain - anchors to the live-
    scraped sunrise instead of the offline-ephemeris estimate."""
    midnight = datetime(y, m, d, 0, 0, tzinfo=IST)
    sr = sunrise_override if sunrise_override else _sunrise_ist(midnight)
    jd = _jd_from_ist(sr)
    sun, _ = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    idx = int(sun[0] // 30) % 12
    return RASHI_NAMES[idx]


def compute_gata_kali(shaka_year):
    """Elapsed Kali Yuga year count from the Shaka Samvat year - simple,
    fixed arithmetic (Kali Yuga began 3179 years before the Shaka era),
    verified against a real printed Panchangam: Shaka 1948 -> Gata Kali
    5127, exactly."""
    try:
        return int(shaka_year) + 3179
    except (TypeError, ValueError):
        return None


def compute_godhuli(sunset_str):
    """Godhuli Muhurta ('cow-dust hour'): the 24-minute auspicious window
    centered on sunset, 12 minutes before to 12 minutes after - the
    standard definition per Muhurta Ganapati. Verified against a real
    printed Panchangam: sunset 18:35:08 -> Godhuli 18:23-18:47, exactly."""
    if not sunset_str:
        return None
    try:
        t = datetime.strptime(sunset_str.strip(), "%I:%M %p")
    except ValueError:
        return None
    start = t - timedelta(minutes=12)
    end = t + timedelta(minutes=12)
    return f"{start.strftime('%I:%M %p')} to {end.strftime('%I:%M %p')}"


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

    # Override the scraped Rahu Kalam / Yamagandam / Gulika Kalam with the
    # sunrise-adjusted computation - see compute_kalam()/KALAM_SEGMENT
    # above for why. Uses this same day's scraped sunrise/sunset, so it
    # stays internally consistent with the rest of the card. Falls back
    # to leaving the scraped values in place if sunrise/sunset didn't
    # parse for some reason, rather than losing the fields entirely.
    kalam = compute_kalam(data.get("sunrise"), data.get("sunset"), weekday_full)
    if kalam:
        data["rahu_kalam"] = f"{kalam['rahu'][0]} to {kalam['rahu'][1]}"
        data["yamaganda"] = f"{kalam['yama'][0]} to {kalam['yama'][1]}"
        data["gulikai_kalam"] = f"{kalam['gulika'][0]} to {kalam['gulika'][1]}"
    else:
        print("  WARNING: computed Kalam failed (bad sunrise/sunset), "
              "falling back to scraped values", file=sys.stderr)

    # Override the scraped Tithi with the self-contained ephemeris
    # computation above - verified to match Drik Panchang's own Tithi
    # transition times to within about a minute, and no longer dependent on
    # scraping fragile HTML for this field. Fall back to the scraped value
    # if the computation fails for any reason (e.g. swisseph not available)
    # rather than losing the field entirely.
    # Anchor to the live-scraped sunrise (data["sunrise"], e.g. "06:04 AM")
    # rather than the offline-ephemeris sunrise estimate - the estimate has
    # been measured a few minutes off Drik Panchang's own figure, which
    # could misidentify the tithi/rasi if a transition falls in that gap.
    sunrise_dt = None
    try:
        dd, mm_, yyyy = date_str.split("/")
        if data.get("sunrise"):
            t = datetime.strptime(data["sunrise"].strip(), "%I:%M %p")
            sunrise_dt = datetime(int(yyyy), int(mm_), int(dd), t.hour, t.minute, tzinfo=IST)
    except Exception as e:
        print(f"  WARNING: could not parse scraped sunrise for tithi/rasi anchoring ({e})", file=sys.stderr)

    try:
        dd, mm_, yyyy = date_str.split("/")
        computed_tithi = compute_tithi_chain(int(yyyy), int(mm_), int(dd), sunrise_override=sunrise_dt)
        if computed_tithi:
            data["tithi"] = computed_tithi
    except Exception as e:
        print(f"  WARNING: computed Tithi failed ({e}), falling back to scraped value", file=sys.stderr)

    # Soorya Rasi - not scraped at all (Drik Panchang's day-panchang page
    # doesn't surface it directly), computed fresh from the same sidereal
    # Sun position as the Tithi engine above. Best-effort: if this fails,
    # the card just shows "-" for this one field rather than failing the
    # whole run, same fallback philosophy as the year-info extras.
    data["surya_rasi"] = None
    try:
        dd, mm_, yyyy = date_str.split("/")
        data["surya_rasi"] = compute_surya_rasi(int(yyyy), int(mm_), int(dd), sunrise_override=sunrise_dt)
    except Exception as e:
        print(f"  WARNING: computed Soorya Rasi failed ({e})", file=sys.stderr)

    # Gata Kali - fixed arithmetic from the Shaka year, and Godhuli
    # Muhurta - fixed offset from sunset. Both verified against a real
    # printed Panchangam above; best-effort, same fallback philosophy.
    data["gata_kali"] = compute_gata_kali(data.get("shaka_year"))
    data["godhuli"] = compute_godhuli(data.get("sunset"))

    print("Parsed fields:")
    for k, v in data.items():
        print(f"  {k}: {v}")

    ok, problems = validate_data(data)
    if not ok:
        return None, "validation failed: " + "; ".join(problems)

    print("Validation passed: all fields look correct.")
    return data, None


def notify_failure(target_dt, last_error):
    """Best-effort text (not image) to every recipient explaining that
    the automated Panchangam for `target_dt` could not be sent, so it's
    clear this is a known failure and not silence."""
    date_disp = target_dt.strftime("%B %d, %Y")
    message = (
        f"Hi, this is Vihari's automated Panchangam system.\n\n"
        f"The Panchangam for {date_disp} could not be sent after {MAX_ATTEMPTS} attempts "
        f"due to a technical error:\n{last_error}\n\n"
        f"Vihari has been notified and will look into it. Sorry for the inconvenience!\n\n"
        f"(నమస్తే, ఇది వీహారి యొక్క ఆటోమేటెడ్ పంచాంగం సిస్టమ్. సాంకేతిక సమస్య వలన పంచాంగం పంపడం సాధ్యం కాలేదు. క్షమించండి.)"
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

    # This job runs the NIGHT BEFORE (9:45 PM IST) so the family has
    # tomorrow's Panchangam in hand before they need it early the next
    # morning, rather than racing a 5 AM delivery window. So the
    # Panchangam we fetch/render/send is always for IST-tomorrow, not
    # today, relative to whenever this script actually executes.
    now_ist = datetime.now(IST)
    target_dt = now_ist + timedelta(days=1)
    date_str = target_dt.strftime("%d/%m/%Y")
    weekday_full = target_dt.strftime("%A")

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
              f"for {date_str}.", file=sys.stderr)
        notify_failure(target_dt, last_error)
        sys.exit(1)

    print("Rendering images...")
    images = build_images(data, target_dt)

    captions = {
        "en": f"Panchangam (English) - {target_dt.strftime('%B %d, %Y')}",
        "te": f"పంచాంగం (తెలుగు) - {target_dt.strftime('%d/%m/%Y')}",
        "ta": f"பஞ்சாங்கம் (தமிழ்) - {target_dt.strftime('%d/%m/%Y')}",
    }

    send_failures = []
    for recipient in RECIPIENTS:
        print(f"Sending to {recipient}...")
        for i, (lang, path) in enumerate(images):
            resp = send_image(recipient, APIKEY, path, captions[lang])
            if not _send_ok(resp):
                send_failures.append((recipient, lang, resp.text[:300] if resp is not None else "no response"))
            if i < len(images) - 1:
                time.sleep(6)
        time.sleep(6)

    if send_failures:
        print(f"\nERROR: {len(send_failures)} message(s) were NOT actually delivered "
              f"(TextMeBot returned HTTP 200 but the response body indicates failure):",
              file=sys.stderr)
        for recipient, lang, body in send_failures:
            print(f"  {recipient} ({lang}): {body}", file=sys.stderr)
        print("\nThis usually means the TextMeBot API key's trial/subscription has "
              "expired, or the key is invalid. Check https://textmebot.com and "
              "renew/subscribe if needed - this is not a bug in the script.",
              file=sys.stderr)
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
