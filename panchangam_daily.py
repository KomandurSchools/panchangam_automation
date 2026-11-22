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

# Rahu Kalam / Yamagandam / Gulika Kalam - the standard fixed per-weekday
# table (the daylight period divided into eight ~90-minute segments,
# assigned to weekdays in a fixed rotation). This is what most familiar
# calendar apps show, including the user's trusted reference app - which
# was confirmed side-by-side to match this table exactly (Tuesday: Rahu
# Kalam 3:00-4:30 PM, Yamagandam 9:00-10:30 AM), and is DIFFERENT from Drik
# Panchang's own sunrise-adjusted numbers for these three fields
# specifically. Computing these ourselves instead of scraping them also
# removes a layer of parsing risk for three of the most safety-relevant
# fields on the card.
FIXED_KALAM_TABLE = {
    "Sunday":    {"rahu": ("04:30 PM", "06:00 PM"), "yama": ("12:00 PM", "01:30 PM"), "gulika": ("03:00 PM", "04:30 PM")},
    "Monday":    {"rahu": ("07:30 AM", "09:00 AM"), "yama": ("10:30 AM", "12:00 PM"), "gulika": ("09:00 AM", "10:30 AM")},
    "Tuesday":   {"rahu": ("03:00 PM", "04:30 PM"), "yama": ("09:00 AM", "10:30 AM"), "gulika": ("12:00 PM", "01:30 PM")},
    "Wednesday": {"rahu": ("12:00 PM", "01:30 PM"), "yama": ("07:30 AM", "09:00 AM"), "gulika": ("10:30 AM", "12:00 PM")},
    "Thursday":  {"rahu": ("01:30 PM", "03:00 PM"), "yama": ("06:00 AM", "07:30 AM"), "gulika": ("09:00 AM", "10:30 AM")},
    "Friday":    {"rahu": ("10:30 AM", "12:00 PM"), "yama": ("03:00 PM", "04:30 PM"), "gulika": ("07:30 AM", "09:00 AM")},
    "Saturday":  {"rahu": ("09:00 AM", "10:30 AM"), "yama": ("01:30 PM", "03:00 PM"), "gulika": ("06:00 AM", "07:30 AM")},
}

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

LABELS = {
    "en": {
        "title": "Today's Panchangam", "core": "Panchang Core", "sunmoon": "Sun & Moon",
        "auspicious": "Auspicious Timings", "inauspicious": "Inauspicious Timings",
        "tithi": "Tithi", "nakshatra": "Nakshatra", "yoga": "Yoga", "karana": "Karana",
        "paksha": "Paksha", "sunrise": "Sunrise", "sunset": "Sunset", "moonrise": "Moonrise",
        "moonset": "Moonset", "brahma": "Brahma Muhurtham", "abhijit": "Abhijit Muhurtham",
        "amrit": "Amrit Kalam", "rahu": "Rahu Kalam", "yama": "Yamagandam",
        "gulika": "Gulikai Kalam", "durmuhurtam": "Durmuhurtam", "varjyam": "Varjyam",
        "upto": "upto", "then": "then", "none_today": "None Today",
        "daytime": "Good Time (Day)",
        "yearinfo": "Year & Season", "samvatsara": "Samvatsara", "masa": "Masa",
        "ritu": "Ritu", "ayana": "Ayana",
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
        "daytime": "పగటి శుభ సమయం",
        "yearinfo": "సంవత్సర వివరాలు", "samvatsara": "సంవత్సరం", "masa": "మాసం",
        "ritu": "ఋతువు", "ayana": "అయనం",
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
        "daytime": "பகல் சுப நேரம்",
        "yearinfo": "ஆண்டு விவரங்கள்", "samvatsara": "வருடம்", "masa": "மாதம்",
        "ritu": "ருது", "ayana": "அயனம்",
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
LEFT_FRAC = 0.045
RIGHT_FRAC = 0.955

# Minimal palette: plain white background throughout, no filled color boxes.
# A single purple accent (matches the temple's own header/footer bars) is
# used only for section-title text; every data value is bold near-black for
# max readability; thin grey lines divide sections instead of color fills.
ACCENT_COL = (95, 40, 130)      # the ONE accent color - section-title text only
TEXT_COL = (18, 14, 10)         # near-black, max contrast on white
LINE_COL = (205, 200, 210)      # thin divider / border lines
SUBTITLE_COL = (95, 40, 130)

def font_for(lang, weight, size):
    if lang == "en":
        path = "Poppins-Bold.ttf" if weight == "bold" else "Poppins-Medium.ttf"  # "reg" -> Medium
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
    # Keep a time range ("04:35 AM to 05:25 AM") together as one unit so it
    # moves to the next line whole instead of breaking mid-range.
    text = re.sub(r'(\d{1,2}:\d{2} [AP]M) to (\d{1,2}:\d{2} [AP]M)',
                  lambda m: m.group(0).replace(" ", "\x00"), text)
    words = text.split(" ")
    lines = []
    cur = ""
    for w in words:
        w = w.replace("\x00", " ")
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


def _split_chain_segments(chain_str, then_word):
    """Split a joined chain string (Tithi/Nakshatra/Yoga/Karana - already
    translated/formatted, e.g. "Saptami upto 04:03 AM, Jul 21, then
    Ashtami upto 11:47 PM, Jul 21, then Navami") into one display line per
    named segment: ["Saptami upto 04:03 AM, Jul 21", "Ashtami upto 11:47
    PM, Jul 21", "Navami"]. Each segment gets its own bullet-style line
    instead of being run together into one long sentence, matching the
    reference layout the user asked to match."""
    if not chain_str:
        return []
    parts = re.split(r',\s*' + re.escape(then_word) + r'\s+', chain_str)
    return [p.strip() for p in parts if p and p.strip()]


def _measure_column(draw, lang, groups, col_w, font_scale, scale):
    """Boxed layout: every group is its own rounded box (optional coloured
    title bar on top). Rows flagged kv=True are 'Label: value' lines drawn
    with a bullet and a coloured bold label. Returns the same tuple shape
    as before plus an `ex` dict of horizontal metrics."""
    # Text is a touch smaller than before, with noticeably more air around
    # it (padding inside boxes, gaps between lines and between boxes).
    header_size = max(int(32 * scale * font_scale), 14)
    row_size = max(int(38 * scale * font_scale), 14)
    pad = max(int(13 * scale * font_scale), 6)
    line_gap = max(int(9 * scale * font_scale), 4)
    group_gap = max(int(22 * scale * font_scale), 10)
    px = max(int(24 * scale * font_scale), 8)
    bullet_w = max(int(row_size * 0.6), 8)

    fonts = {
        "header": font_for(lang, "bold", header_size),
        "row": font_for(lang, "bold", row_size),
        "val": font_for(lang, "reg", row_size),
    }
    row_line_h = row_size + line_gap
    item_gap = max(int(row_size * 0.5), 6)   # extra space between separate items in a box
    ex = {"px": px, "bullet_w": bullet_w, "row_size": row_size, "item_gap": item_gap}

    geoms = []
    total_h = 0
    for gi, grp in enumerate(groups):
        kv = grp.get("kv", False)
        g = {"header": grp.get("header"), "row_lines": [], "kv": kv,
             "bar": grp.get("bar")}
        inner_w = col_w - 2 * px - (bullet_w if kv else 0)
        n_lines = 0
        for text in grp["rows"]:
            wrapped = _wrap_lines(draw, text, fonts["row"], inner_w)
            g["row_lines"].append(wrapped)
            n_lines += len(wrapped)
        gh = (((header_size + 2 * pad) if g["header"] else 0) + 2 * pad + n_lines * row_line_h
              + max(len(grp["rows"]) - 1, 0) * item_gap)
        geoms.append(g)
        total_h += gh
        if gi < len(groups) - 1:
            total_h += group_gap

    return total_h, geoms, fonts, header_size, row_line_h, pad, group_gap, ex


def _measure_column_legacy(draw, lang, groups, col_w, font_scale, scale):
    """(Superseded flat layout.) Compute the wrapped-line layout for one column (a list of groups,
    each an optional header + a list of already-formatted text rows)
    WITHOUT drawing anything, so render_card can auto-shrink the font
    until both columns fit between the template's header and footer bars."""
    header_size = max(int(38 * scale * font_scale), 14)
    row_size = max(int(42 * scale * font_scale), 14)
    pad = max(int(6 * scale * font_scale), 4)
    line_gap = max(int(4 * scale * font_scale), 2)
    group_gap = max(int(14 * scale * font_scale), 6)

    fonts = {
        "header": font_for(lang, "bold", header_size),
        "row": font_for(lang, "bold", row_size),
    }
    row_line_h = row_size + line_gap

    geoms = []
    total_h = 0
    for gi, grp in enumerate(groups):
        g = {"header": grp.get("header"), "row_lines": []}
        gh = (header_size + 2 * pad) if g["header"] else 0
        for text in grp["rows"]:
            wrapped = _wrap_lines(draw, text, fonts["row"], col_w)
            g["row_lines"].append(wrapped)
            gh += len(wrapped) * row_line_h
        geoms.append(g)
        total_h += gh
        if gi < len(groups) - 1:
            total_h += group_gap

    return total_h, geoms, fonts, header_size, row_line_h, pad, group_gap


# --------------------------------------------------------------------------
# Daily header/footer colour theme
# --------------------------------------------------------------------------
# Each palette: (band background, big-text colour [was yellow], small-text
# colour [was white], body accent for headings on the white page). None of
# them is red/black or a shade of either; every text colour is checked for
# strong contrast against its band at import time (see _contrast).
PALETTES = [
    ((103, 45, 145), (255, 255, 0), (255, 255, 255), (103, 45, 145)),    # original purple
    ((14, 45, 100), (255, 214, 10), (255, 255, 255), (14, 45, 100)),     # navy + gold
    ((25, 55, 130), (255, 200, 40), (235, 244, 255), (25, 55, 130)),     # royal blue + amber
    ((60, 40, 140), (255, 224, 102), (255, 255, 255), (60, 40, 140)),    # indigo + gold
    ((120, 40, 110), (255, 226, 120), (255, 255, 255), (120, 40, 110)),  # plum + gold
    ((28, 78, 140), (255, 236, 130), (255, 255, 255), (28, 78, 140)),    # steel blue + lemon
    ((70, 50, 120), (255, 205, 90), (250, 245, 255), (70, 50, 120)),     # violet + amber
    ((255, 232, 150), (20, 45, 120), (10, 80, 50), (20, 45, 120)),       # butter + navy / green
    ((190, 225, 245), (10, 50, 120), (20, 60, 70), (10, 50, 120)),       # sky + navy / teal
]


def _lum(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


PALETTES = [p for p in PALETTES
            if _contrast(p[0], p[1]) >= 4.5 and _contrast(p[0], p[2]) >= 4.5]


def _palette_index(dt):
    import random
    return random.Random(dt.toordinal() * 7919 + 13).randrange(len(PALETTES))


def pick_palette(dt):
    """Date-seeded, so all three language cards of a day share one theme,
    and never the same theme two days running."""
    idx = _palette_index(dt)
    prev = _palette_index(dt - timedelta(days=1))
    if idx == prev:
        idx = (idx + 1) % len(PALETTES)
    return PALETTES[idx]


_ORIG_BAND = (103, 45, 145)
_THEME_CACHE = {}
_GEO = {}


def _geometry():
    """Measure where the template's header band, footer band and deity photo
    actually are, from its pixels - so the script works with whatever
    template file is in assets/ (any height), instead of trusting fixed
    fractions. Falls back to the old fractions if detection looks wrong."""
    if _GEO:
        return _GEO
    img = Image.open(TEMPLATE_PATH).convert("RGB")
    W, H = img.size
    px = img.load()

    def purple(p):
        return abs(p[0] - 103) < 45 and abs(p[1] - 45) < 45 and abs(p[2] - 145) < 45

    hdr, top, bottom = int(H * HEADER_FRAC), int(H * FOOTER_FRAC), H
    xl, xr = int(W * 0.0145), int(W * 0.9895)
    try:
        y = 3
        while y < H and purple(px[3, y]):
            y += 1
        h2 = y
        xs = int(W * 0.02)
        yb = H - 1
        while yb > H // 2 and not purple(px[xs, yb]):
            yb -= 1
        b2 = yb + 1
        yt = yb
        while yt > H // 2 and purple(px[xs, yt]):
            yt -= 1
        t2 = yt + 1
        ym = (t2 + b2) // 2
        l2 = 0
        while l2 < W and not purple(px[l2, ym]):
            l2 += 1
        r2 = W - 1
        while r2 > 0 and not purple(px[r2, ym]):
            r2 -= 1
        if (0.05 * H < h2 < 0.3 * H and 0.5 * H < t2 < b2 <= H
                and b2 - t2 > 0.08 * H and l2 < 0.1 * W and r2 > 0.9 * W):
            hdr, top, bottom, xl, xr = h2, t2, b2, l2, r2
    except Exception:
        pass
    fh = bottom - top
    photo = (int(xl + 0.106 * fh), int(top + 0.047 * fh),
             int(xl + 0.106 * fh + 0.608 * fh), int(top + 0.949 * fh))
    _GEO.update(hdr=hdr, top=top, bottom=bottom, xl=xl, xr=xr, photo=photo, W=W, H=H)
    return _GEO


def _themed_base(palette):
    """Template with only the two coloured bands recoloured: purple ->
    band colour, yellow lettering -> big-text colour, white lettering ->
    small-text colour (edge pixels blended proportionally so the lettering
    stays smooth). The deity photo is left untouched."""
    if palette in _THEME_CACHE:
        return _THEME_CACHE[palette].copy()
    img = Image.open(TEMPLATE_PATH).convert("RGB")
    if palette[0] == _ORIG_BAND and palette[1] == (255, 255, 0) and palette[2] == (255, 255, 255):
        _THEME_CACHE[palette] = img
        return img.copy()
    W, H = img.size
    bg2, y2, w2 = palette[0], palette[1], palette[2]
    px = img.load()
    geo = _geometry()
    photo = geo["photo"]  # x0,y0,x1,y1 (the photo's own white box)

    def recolor(x0, y0, x1, y1):
        for y in range(y0, y1):
            in_photo_rows = photo[1] <= y < photo[3]
            for x in range(x0, x1):
                if in_photo_rows and photo[0] <= x < photo[2]:
                    continue
                r, g, b = px[x, y]
                if b < 138:           # purple <-> yellow axis
                    f = min((145 - b) / 145.0, 1.0)
                    t = y2
                elif b > 152:         # purple <-> white axis
                    f = min((b - 145) / 110.0, 1.0)
                    t = w2
                else:
                    f, t = 0.0, bg2
                px[x, y] = (int(bg2[0] + (t[0] - bg2[0]) * f),
                            int(bg2[1] + (t[1] - bg2[1]) * f),
                            int(bg2[2] + (t[2] - bg2[2]) * f))

    recolor(0, 0, W, geo["hdr"])
    recolor(geo["xl"], geo["top"], geo["xr"] + 1, geo["bottom"])
    _THEME_CACHE[palette] = img
    return img.copy()


def render_card(lang, subtitle, left_groups, right_groups, outpath, palette=None):
    """Draws a two-column sheet of labeled data (left: calendar identity +
    Tithi/Nakshatra/Yoga/Karana as one bullet line per segment; right:
    Sun/Moon times, Inauspicious Period, Auspicious Period) onto the
    temple's template image, styled after the reference layout the user
    provided - plain left-aligned "Label: Value" lines and section
    headers, no boxes. Font size auto-shrinks as needed so nothing ever
    overlaps or runs past the footer bar."""
    palette = palette or PALETTES[0]
    body_col = palette[3]
    img = _themed_base(palette)
    W, H = img.size
    d = ImageDraw.Draw(img)

    geo = _geometry()
    top = geo["hdr"]
    # Small safety margin above the footer band (measured from the actual
    # template pixels), so content never touches the temple's footer banner.
    bottom = geo["top"] - int(H * 0.03)
    left = int(W * LEFT_FRAC)
    right = int(W * RIGHT_FRAC)
    content_w = right - left
    scale = W / 1587.0

    # The date/day/location line - deliberately the single biggest, boldest
    # piece of text on the whole card, well above the size any grid data
    # ever reaches, so it's the first thing the eye lands on. Shrink it down
    # from that target size if needed so it never runs past the card's
    # edges - Tamil in particular renders noticeably wider than English at
    # the same point size for this string.
    subtitle_size = max(int(64 * scale), 14)
    subtitle_margin_w = int(content_w * 0.92)
    while subtitle_size > 14:
        f_sub = font_for(lang, "bold", subtitle_size)
        bbox = d.textbbox((0, 0), subtitle, font=f_sub)
        if (bbox[2] - bbox[0]) <= subtitle_margin_w:
            break
        subtitle_size -= 2
    else:
        f_sub = font_for(lang, "bold", subtitle_size)
    sub_box_h = subtitle_size + int(34 * scale)
    subtitle_h = sub_box_h + int(16 * scale)
    content_top = top + int(14 * scale)
    available = bottom - content_top - subtitle_h

    col_gap = max(int(28 * scale), 12)
    col_w = (content_w - col_gap) / 2

    # Auto-fit: search for the LARGEST font_scale that still fits both
    # columns (start big and shrink ~2% at a time for fine granularity),
    # floor at 22% of base size. The two columns usually hold different
    # amounts of content, so this sizes everything to whichever column is
    # taller - the shorter column just ends higher up, which is normal for
    # a two-column layout like this.
    font_scale = 1.45  # upper cap; shrinks until it fits with generous spacing
    while True:
        left_h, left_geoms, fonts, header_size, row_line_h, pad, group_gap, ex = _measure_column(
            d, lang, left_groups, col_w, font_scale, scale)
        right_h, right_geoms = _measure_column(
            d, lang, right_groups, col_w, font_scale, scale)[:2]
        total_h = max(left_h, right_h)
        if total_h <= available or font_scale <= 0.22:
            break
        font_scale -= 0.02

    # Step the text down ~12% from the largest size that fit, and use the
    # room that frees up as extra space between the items inside each box.
    # (Keep shrinking only if the added spacing makes it not fit.)
    font_scale = max(font_scale * 0.88, 0.22)
    while True:
        left_h, left_geoms, fonts, header_size, row_line_h, pad, group_gap, ex = _measure_column(
            d, lang, left_groups, col_w, font_scale, scale)
        right_h, right_geoms = _measure_column(
            d, lang, right_groups, col_w, font_scale, scale)[:2]
        total_h = max(left_h, right_h)
        if total_h <= available or font_scale <= 0.22:
            break
        font_scale -= 0.02

    # Hard safety valve: even at the smallest readable font, an extreme day
    # (many long chained transitions on both sides at once) could in theory
    # still not fit. Rather than let that overflow into the temple's footer
    # banner, compress the line/group spacing itself so the columns are
    # GUARANTEED to end at or above `bottom`, no matter what.
    if total_h > available > 0:
        squeeze = max(available / total_h, 0.5)
        row_line_h = max(int(row_line_h * squeeze), 1)
        group_gap = max(group_gap * squeeze, 1)
        pad = max(int(pad * squeeze), 1)
        ex["item_gap"] = max(int(ex["item_gap"] * squeeze), 1)
        total_h = available

    # Spare vertical room becomes breathing space: a little more under the
    # date box, and the rest split above/below the content.
    slack = max(available - total_h, 0)
    y0 = content_top + int(slack * 0.25)
    border_w = max(2, int(2.5 * scale))
    radius = max(int(14 * scale), 6)
    # Date / day / city line inside a rounded outline box
    d.rounded_rectangle([left, y0, right, y0 + sub_box_h], radius=radius,
                        outline=body_col, width=border_w, fill=(255, 255, 255))
    d.text((W / 2, y0 + sub_box_h / 2), subtitle, font=f_sub, fill=body_col, anchor="mm")
    y0 += subtitle_h

    row_size = ex["row_size"]
    px_in = ex["px"]
    bullet_w = ex["bullet_w"]
    item_gap = ex["item_gap"]

    def _draw_column(x0, groups, geoms):
        y = y0
        n = len(groups)
        for gi, (grp, geom) in enumerate(zip(groups, geoms)):
            col = geom["bar"] or body_col
            n_lines = sum(len(w) for w in geom["row_lines"])
            bar_h = (header_size + 2 * pad) if geom["header"] else 0
            box_h = (bar_h + 2 * pad + n_lines * row_line_h
                     + max(len(geom["row_lines"]) - 1, 0) * item_gap)
            d.rounded_rectangle([x0, y, x0 + col_w, y + box_h], radius=radius,
                                outline=col, width=border_w, fill=(255, 255, 255))
            if geom["header"]:
                try:
                    d.rounded_rectangle([x0, y, x0 + col_w, y + bar_h], radius=radius,
                                        fill=col, corners=(True, True, False, False))
                except TypeError:
                    d.rectangle([x0, y, x0 + col_w, y + bar_h], fill=col)
                d.text((x0 + col_w / 2, y + pad), geom["header"], font=fonts["header"],
                       fill=(255, 255, 255), anchor="ma")
            yy = y + bar_h + pad
            tx0 = x0 + px_in
            for wrapped in geom["row_lines"]:
                has_kv = geom["kv"] and (": " in wrapped[0] or wrapped[0].endswith(":"))
                text_x = tx0 + (bullet_w if geom["kv"] else 0)
                if has_kv:
                    r = max(int(row_size * 0.17), 3)
                    cy = yy + row_size * 0.55
                    d.ellipse([tx0, cy - r, tx0 + 2 * r, cy + r], fill=col)
                for li, line in enumerate(wrapped):
                    if has_kv and li == 0:
                        label, _sep, val = line.partition(": ")
                        label = label.rstrip(":") + ":"
                        d.text((text_x, yy), label, font=fonts["row"], fill=col)
                        lw = d.textlength(label + " ", font=fonts["row"])
                        d.text((text_x + lw, yy), val, font=fonts["val"], fill=TEXT_COL)
                    else:
                        f = fonts["val"] if geom["kv"] else fonts["row"]
                        d.text((text_x, yy), line, font=f, fill=TEXT_COL)
                    yy += row_line_h
                yy += item_gap
            y += box_h
            if gi < n - 1:
                y += group_gap

    mid_x = left + col_w + col_gap / 2
    _draw_column(left, left_groups, left_geoms)
    _draw_column(mid_x + col_gap / 2, right_groups, right_geoms)

    # quality=85 + optimize is a meaningfully smaller file than quality=92
    # (less data to upload from the Actions runner, less for recipients to
    # download over WhatsApp) with no visible loss on a flat-color/text
    # graphic like this - dimensions stay untouched, so none of the
    # readability work above is affected.
    img.save(outpath, format="JPEG", quality=85, optimize=True)
    return outpath


_RANGE_RE = re.compile(r'(\d{1,2}:\d{2}\s*[AP]M)\s*(?:to|-|–)\s*(\d{1,2}:\d{2}\s*[AP]M)', re.IGNORECASE)


def _to_min(s):
    t = datetime.strptime(re.sub(r'\s+', ' ', s.strip().upper()), "%I:%M %p")
    return t.hour * 60 + t.minute


def _fmt_min(m):
    return datetime(2000, 1, 1, m // 60, m % 60).strftime("%I:%M %p")


def compute_day_windows(data, min_len=20, max_windows=3):
    """Daytime 'good time' windows: sunrise-sunset with Rahu Kalam,
    Yamagandam, Gulika Kalam, Durmuhurtam and Varjyam removed. Returns up
    to `max_windows` longest remaining windows (each at least `min_len`
    minutes), in clock order, as 'HH:MM AM to HH:MM PM'. Purely derived -
    it does not alter any of the timings it is built from."""
    try:
        sr = _to_min(data["sunrise"])
        ss = _to_min(data["sunset"])
    except Exception:
        return []
    if ss <= sr:
        return []
    bad = []
    for key in ("rahu_kalam", "yamaganda", "gulikai_kalam", "durmuhurtam", "varjyam"):
        for a, b in _RANGE_RE.findall(data.get(key) or ""):
            try:
                s, e = _to_min(a), _to_min(b)
            except Exception:
                continue
            if e < s:
                e = 24 * 60
            bad.append((s, e))
    bad.sort()
    free, cur = [], sr
    for s, e in bad:
        if e <= cur:
            continue
        if s > cur:
            free.append((cur, min(s, ss)))
        cur = max(cur, e)
        if cur >= ss:
            break
    if cur < ss:
        free.append((cur, ss))
    free = [(a, b) for a, b in free if b - a >= min_len]
    free = sorted(sorted(free, key=lambda w: w[0] - w[1])[:max_windows])
    return [f"{_fmt_min(a)} to {_fmt_min(b)}" for a, b in free]


def build_images(data, dt_ist):
    palette = pick_palette(dt_ist.date() if hasattr(dt_ist, "date") else dt_ist)
    day_windows = compute_day_windows(data)
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

    outputs = []
    for lang, tithi, nak, yoga, kar, paksha, weekday, month_name, samv, masa, ritu, ayana in [
        ("en", apply_en_overrides(data["tithi"]), apply_en_overrides(data["nakshatra"]),
         apply_en_overrides(data["yoga"]), apply_en_overrides(data["karana"]),
         data["paksha"], data["weekday_full"], dt_ist.strftime("%B"),
         (samv_raw or "-"), en_masa, en_ritu, en_ayana),
        ("te", te_tithi, te_nak, te_yoga, te_kar, te_paksha, te_weekday, MONTH_TE[dt_ist.month],
         te_samv, te_masa, te_ritu, te_ayana),
        ("ta", ta_tithi, ta_nak, ta_yoga, ta_kar, ta_paksha, ta_weekday, MONTH_TA[dt_ist.month],
         ta_samv, ta_masa, ta_ritu, ta_ayana),
    ]:
        L = LABELS[lang]
        city = {"en": CITY_LABEL_EN, "te": CITY_LABEL_TE, "ta": CITY_LABEL_TA}[lang]
        if lang == "en":
            date_str = dt_ist.strftime("%B %d, %Y")
        else:
            date_str = f"{month_name} {dt_ist.day:02d}, {dt_ist.year}"
        subtitle = f"{date_str}  |  {weekday}  |  {city}"

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

        # Two-column layout, styled after the reference the user liked:
        # calendar identity + Tithi/Nakshatra/Yoga/Karana (each transition
        # segment on its own bullet line) on the left; Sun/Moon times,
        # Inauspicious Period, and Auspicious Period grouped on the right.
        # Plain left-aligned "Label: Value" lines under plain section
        # headers - no boxes, no per-field color coding.
        def _chain_rows(s):
            # One line per segment; every line after the first is led by
            # "Then:" (తర్వాత / பின்) so it reads as what comes next.
            segs = _split_chain_segments(s or "-", L["then"])
            rows = []
            for i, seg in enumerate(segs):
                if lang == "en":
                    seg = seg.replace(" upto ", " till ")
                rows.append(seg if i == 0 else f"{L['then'].capitalize()}: {seg}")
            return rows

        left_groups = [
            {"header": None, "kv": True, "rows": [
                f"{L['samvatsara']}: {samv}",
                f"{L['masa']}: {masa}",
                f"{L['ritu']}: {ritu}",
                f"{L['ayana']}: {ayana}",
                f"{L['paksha']}: {paksha}",
            ]},
            {"header": L["tithi"], "rows": _chain_rows(tithi)},
            {"header": L["nakshatra"], "rows": _chain_rows(nak)},
            {"header": L["yoga"], "rows": _chain_rows(yoga)},
            {"header": L["karana"], "rows": _chain_rows(kar)},
        ]
        right_groups = [
            {"header": None, "kv": True, "rows": [
                f"{L['sunrise']}: {data['sunrise'] or '-'}",
                f"{L['sunset']}: {data['sunset'] or '-'}",
                f"{L['moonrise']}: {moonrise_val}",
                f"{L['moonset']}: {moonset_val}",
            ]},
            {"header": L["inauspicious"], "kv": True, "bar": (140, 48, 52), "rows": [
                f"{L['rahu']}: {data['rahu_kalam'] or '-'}",
                f"{L['yama']}: {data['yamaganda'] or '-'}",
                f"{L['gulika']}: {data['gulikai_kalam'] or '-'}",
                f"{L['durmuhurtam']}: {data['durmuhurtam'] or '-'}",
                f"{L['varjyam']}: {data['varjyam'] or '-'}",
            ]},
            {"header": L["auspicious"], "kv": True, "bar": (170, 105, 10), "rows": [
                f"{L['brahma']}: {data['brahma_muhurta'] or '-'}",
                f"{L['abhijit']}: {abhijit_val}",
                f"{L['amrit']}: {amrit_val}",
            ] + ([f"{L['daytime']}: {day_windows[0]}"] + day_windows[1:] if day_windows else [])},
        ]

        outpath = os.path.join(HERE, f"panchangam_{lang}.jpg")
        render_card(lang, subtitle, left_groups, right_groups, outpath, palette)
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


def _legacy_tithi_chain(y, m, d):
    """(Superseded by the generic _angle_chain version defined below.)
    Returns a string in the same 'Name upto TIME, then Name2' format the
    old scraper produced, e.g. 'Shashthi upto 03:29 AM, then Saptami', or
    just 'Amavasya' if there's no transition before the next sunrise."""
    midnight = datetime(y, m, d, 0, 0, tzinfo=IST)
    sr0 = _sunrise_ist(midnight)
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


NAKSHATRA_NAMES = [
    "Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu",
    "Pushya", "Ashlesha", "Magha", "Purva Phalguni", "Uttara Phalguni", "Hasta",
    "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha", "Mula", "Purva Ashadha",
    "Uttara Ashadha", "Shravana", "Dhanishtha", "Shatabhisha", "Purva Bhadrapada",
    "Uttara Bhadrapada", "Revati",
]


def _moon_sidereal(dt_ist):
    moon, _ = swe.calc_ut(_jd_from_ist(dt_ist), swe.MOON, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    return moon[0] % 360


YOGA_NAMES = [
    "Vishkambha", "Priti", "Ayushman", "Saubhagya", "Shobhana", "Atiganda", "Sukarma",
    "Dhriti", "Shula", "Ganda", "Vriddhi", "Dhruva", "Vyaghata", "Harshana", "Vajra",
    "Siddhi", "Vyatipata", "Variyana", "Parigha", "Shiva", "Siddha", "Sadhya", "Shubha",
    "Shukla", "Brahma", "Indra", "Vaidhriti",
]
_KARANA_REPEAT = ["Bava", "Balava", "Kaulava", "Taitila", "Garaja", "Vanija", "Vishti"]


def _karana_name(i):
    i %= 60
    if i == 0:
        return "Kimstughna"
    if i == 57:
        return "Shakuni"
    if i == 58:
        return "Chatushpada"
    if i == 59:
        return "Naga"
    return _KARANA_REPEAT[(i - 1) % 7]


def _sun_moon(dt_ist):
    jd = _jd_from_ist(dt_ist)
    moon, _ = swe.calc_ut(jd, swe.MOON, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    sun, _ = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH | swe.FLG_SIDEREAL)
    return sun[0] % 360, moon[0] % 360


def _angle_chain(y, m, d, angle_fn, n, name_fn):
    """Generic sunrise-to-sunrise chain for any panchang limb that is an
    angle divided into `n` equal parts (Tithi 30, Nakshatra 27, Yoga 27,
    Karana 60). Returns 'A upto 11:42 PM, Nov 23, then B upto 08:23 PM,
    Nov 24' - EVERY segment carries its end time AND date, and the last
    segment (the one running at the next sunrise) is followed through to
    its own end, so the card can say what comes next and until when."""
    step = 360.0 / n
    midnight = datetime(y, m, d, 0, 0, tzinfo=IST)
    sr0 = _sunrise_ist(midnight)
    sr1 = _sunrise_ist(midnight + timedelta(days=1))

    def boundary(idx, lo, hi):
        target = ((idx + 1) * step) % 360

        def f(t):
            return _unwrap_near(angle_fn(t), target) - target
        flo, fhi = f(lo), f(hi)
        if (flo < 0) == (fhi < 0):
            return None
        for _ in range(60):
            mid = lo + (hi - lo) / 2
            fm = f(mid)
            if (flo < 0) == (fm < 0):
                lo, flo = mid, fm
            else:
                hi, fhi = mid, fm
        return lo + (hi - lo) / 2

    idx = int(angle_fn(sr0) // step) % n
    segs = []
    t = sr0
    for _ in range(8):
        b = boundary(idx, t, sr1)
        if b is None:
            break
        segs.append((idx, b))
        idx = (idx + 1) % n
        t = b
    last_end = boundary(idx, t, t + timedelta(days=2))
    segs.append((idx, last_end))

    parts = []
    for i, (k, end) in enumerate(segs):
        txt = name_fn(k)
        if end is not None:
            txt += f" upto {end.strftime('%I:%M %p')}, {end.strftime('%b %d')}"
        parts.append(txt)
    return ", then ".join(parts)


def compute_tithi_chain(y, m, d):
    return _angle_chain(y, m, d, _tithi_angle, 30, lambda k: TITHI_NAMES[k % 30])


def compute_nakshatra_chain(y, m, d):
    return _angle_chain(y, m, d, _moon_sidereal, 27, lambda k: NAKSHATRA_NAMES[k % 27])


def compute_yoga_chain(y, m, d):
    def ang(t):
        s, mo = _sun_moon(t)
        return (s + mo) % 360
    return _angle_chain(y, m, d, ang, 27, lambda k: YOGA_NAMES[k % 27])


def compute_karana_chain(y, m, d):
    return _angle_chain(y, m, d, _tithi_angle, 60, _karana_name)


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
    # fixed per-weekday table - see FIXED_KALAM_TABLE above for why. This
    # also means these three fields can no longer fail validation due to a
    # scraping/parsing issue, since they're now computed, not scraped.
    fixed = FIXED_KALAM_TABLE.get(weekday_full)
    if fixed:
        data["rahu_kalam"] = f"{fixed['rahu'][0]} to {fixed['rahu'][1]}"
        data["yamaganda"] = f"{fixed['yama'][0]} to {fixed['yama'][1]}"
        data["gulikai_kalam"] = f"{fixed['gulika'][0]} to {fixed['gulika'][1]}"

    # Override the scraped Tithi with the self-contained ephemeris
    # computation above - verified to match Drik Panchang's own Tithi
    # transition times to within about a minute, and no longer dependent on
    # scraping fragile HTML for this field. Fall back to the scraped value
    # if the computation fails for any reason (e.g. swisseph not available)
    # rather than losing the field entirely.
    try:
        dd, mm_, yyyy = date_str.split("/")
        computed_tithi = compute_tithi_chain(int(yyyy), int(mm_), int(dd))
        if computed_tithi:
            data["tithi"] = computed_tithi
    except Exception as e:
        print(f"  WARNING: computed Tithi failed ({e}), falling back to scraped value", file=sys.stderr)

    # Nakshatra: same treatment, so it always reads "X upto TIME, DATE,
    # then Y" with the following nakshatra named.
    for key, fn in (("nakshatra", compute_nakshatra_chain),
                    ("yoga", compute_yoga_chain),
                    ("karana", compute_karana_chain)):
        try:
            val = fn(int(yyyy), int(mm_), int(dd))
            if val:
                data[key] = val
        except Exception as e:
            print(f"  WARNING: computed {key} failed ({e}), falling back to scraped value", file=sys.stderr)

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
