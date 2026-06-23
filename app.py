import csv
import json
import base64
import smtplib
from email.message import EmailMessage
import math
import re
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import streamlit as st
import urllib3

try:
    from db import (
        save_quote_supabase,
        save_lead_supabase,
        get_pricing_settings_supabase,
        save_pricing_settings_supabase,
        get_quotes_supabase,
        get_leads_supabase,
        update_lead_status_supabase,
    )
except Exception:
    save_quote_supabase = None
    save_lead_supabase = None
    get_pricing_settings_supabase = None
    save_pricing_settings_supabase = None
    get_quotes_supabase = None
    get_leads_supabase = None
    update_lead_status_supabase = None

from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

try:
    from streamlit_searchbox import st_searchbox
except Exception:
    st_searchbox = None


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

st.set_page_config(
    page_title="Instant Quote",
    page_icon="🌱",
    layout="wide",
)

# ----------------------------
# File paths
# ----------------------------
BUILDINGS_GEOJSON = Path("buildings_simple.geojson")
LEADS_CSV = Path("leads.csv")
QUOTES_CSV = Path("quotes.csv")
PRICING_SETTINGS_JSON = Path("pricing_settings.json")

# ----------------------------
# Lambton GIS public ArcGIS REST endpoints
# ----------------------------
ADDRESS_URL = "https://www.lambtongis.ca/arcgis_adaptor/rest/services/LCGIS_VertiGIS/MapServer/25/query"
ADDRESS_FALLBACK_URL = "https://www.lambtongis.ca/arcgis_adaptor/rest/services/OpenData/AddressPoints/MapServer/0/query"
PARCEL_URL = "https://www.lambtongis.ca/arcgis_adaptor/rest/services/LCGIS_VertiGIS/MapServer/28/query"

# Esri World Imagery tiles
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"

TO_UTM17 = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)

# ----------------------------
# Default pricing settings
# ----------------------------
DEFAULT_PRICING_SETTINGS = {
    "company_name": "Lawn Company",
    "lead_notification_email": "",
    "enable_ai_review": True,

    # Tier pricing matches how many lawn companies actually quote.
    # Customers see a clean price like $45, $55, $65, $75, etc.
    "tier_prices": {
        "Grass cutting": {
            "Small": 45.0,
            "Standard": 55.0,
            "Large": 65.0,
            "Complex": 75.0,
            "Estate": 95.0,
        },
        "Seasonal cleanup": {
            "Small": 125.0,
            "Standard": 175.0,
            "Large": 225.0,
            "Complex": 275.0,
            "Estate": 400.0,
        },
        "Fertilization and weed control": {
            "Small": 75.0,
            "Standard": 95.0,
            "Large": 125.0,
            "Complex": 145.0,
            "Estate": 195.0,
        },
    },

    # Kept for compatibility with older saved pricing_settings.json files.
    "Grass cutting": {
        "base_fee": 30.0,
        "rate_per_1000_sqft": 5.0,
        "minimum_price": 45.0,
    },
    "Seasonal cleanup": {
        "base_fee": 85.0,
        "rate_per_1000_sqft": 12.0,
        "minimum_price": 125.0,
    },
    "Fertilization and weed control": {
        "base_fee": 45.0,
        "rate_per_1000_sqft": 8.0,
        "minimum_price": 75.0,
    },
    "Residential": {
        "multiplier": 1.0,
    },
    "Commercial": {
        "multiplier": 1.25,
    },
}


# ----------------------------
# Styling
# ----------------------------
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 760px;
        }

        .simple-header {
            margin-bottom: 1.75rem;
            text-align: center;
        }

        .simple-header h1 {
            font-size: 44px;
            margin-bottom: 0.35rem;
            letter-spacing: -0.04em;
        }

        .simple-header p {
            color: #666;
            font-size: 17px;
            margin-top: 0;
        }

        .input-card {
            padding: 28px;
            border-radius: 24px;
            border: 1px solid #e7e7e7;
            background: #ffffff;
            box-shadow: 0 8px 28px rgba(0,0,0,0.06);
            margin-bottom: 22px;
        }

        .quote-card {
            padding: 36px;
            border-radius: 28px;
            border: 1px solid #dbe8df;
            background: linear-gradient(180deg, #ffffff 0%, #f6fbf7 100%);
            box-shadow: 0 12px 36px rgba(0,0,0,0.08);
            text-align: center;
            margin-top: 20px;
            margin-bottom: 18px;
        }

        .quote-service {
            font-size: 22px;
            font-weight: 750;
            color: #14532d;
            margin-bottom: 8px;
        }

        .quote-price {
            font-size: 72px;
            font-weight: 850;
            line-height: 1.0;
            color: #111827;
            margin-bottom: 10px;
        }

        .quote-label {
            font-size: 16px;
            color: #666;
        }

        .fine-print {
            color: #777;
            font-size: 13px;
            text-align: center;
            margin-top: -4px;
        }

        .stButton > button {
            width: 100%;
            border-radius: 999px;
            padding: 0.8rem 1.2rem;
            font-weight: 800;
            font-size: 16px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------
# Pricing settings helpers
# ----------------------------
def deep_merge_pricing(defaults, loaded):
    settings = json.loads(json.dumps(defaults))

    if not isinstance(loaded, dict):
        return settings

    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key].update(value)
        else:
            settings[key] = value

    return settings


def load_pricing_settings():
    if not PRICING_SETTINGS_JSON.exists():
        return json.loads(json.dumps(DEFAULT_PRICING_SETTINGS))

    try:
        with PRICING_SETTINGS_JSON.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        return deep_merge_pricing(DEFAULT_PRICING_SETTINGS, loaded)
    except Exception:
        return json.loads(json.dumps(DEFAULT_PRICING_SETTINGS))


def save_pricing_settings(settings):
    with PRICING_SETTINGS_JSON.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def get_service_pricing(pricing_settings, service_type):
    return pricing_settings.get(service_type, DEFAULT_PRICING_SETTINGS["Grass cutting"])


def get_property_multiplier(pricing_settings, property_type):
    return float(pricing_settings.get(property_type, {}).get("multiplier", 1.0))



# ----------------------------
# Email + AI review helpers
# ----------------------------
def get_secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def send_lead_email(pricing_settings, customer_name, customer_phone, customer_email, customer_notes, result):
    """Send a lead notification email using SMTP settings from Streamlit Secrets."""
    recipient = pricing_settings.get("lead_notification_email", "").strip()
    if not recipient:
        return False, "No lead notification email set."

    email_host = get_secret("EMAIL_HOST")
    email_port = int(get_secret("EMAIL_PORT", 587))
    email_user = get_secret("EMAIL_USER") or get_secret("EMAIL_USERNAME")
    email_password = get_secret("EMAIL_PASSWORD")
    email_from = get_secret("EMAIL_FROM", email_user)

    if not all([email_host, email_user, email_password, email_from]):
        return False, "Email secrets are missing."

    company_name = pricing_settings.get("company_name", "Lawn Company")
    quote_status = result.get("quote_status", "Auto quote")
    price_text = f"${result['price']}" if quote_status != "Manual review" else "Manual review required"

    subject = f"New LawnQuote AI Lead - {result['service_type']}"
    body = f"""New LawnQuote AI Lead

Company: {company_name}
Quote ID: {result['quote_id']}

Customer
Name: {customer_name}
Phone: {customer_phone}
Email: {customer_email}
Notes: {customer_notes}

Property
Address: {result['address']}
Property Type: {result['property_type']}
Service: {result['service_type']}
Quote Status: {quote_status}
Estimated Price: {price_text}

Internal Estimate
Estimated Lawn Sq Ft: {result.get('lawn_sqft')}
Parcel Sq Ft: {result.get('parcel_sqft')}
Building Sq Ft: {result.get('building_sqft')}
Hardscape Sq Ft: {result.get('hardscape_sqft')}
Hardscape Method: {result.get('hardscape_method')}
Review Reasons: {result.get('review_reasons', '')}
Base Pricing Tier: {result.get('base_pricing_tier', '')}\nFinal Pricing Tier: {result.get('pricing_tier', '')}
Pricing Lawn Sq Ft: {result.get('pricing_lawn_sqft', '')}
AI Property Class: {result.get('ai_property_class', '')}
AI Risk Level: {result.get('ai_risk_level', '')}
AI Adjustment Factor: {result.get('ai_adjustment_factor', '')}
AI Review Notes: {result.get('ai_review_notes', '')}
Quote Confidence Score: {result.get('quote_confidence_score', '')}%
"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP(email_host, email_port) as server:
            server.starttls()
            server.login(email_user, email_password)
            server.send_message(msg)
        return True, "Email sent."
    except Exception as e:
        return False, str(e)


def quote_confidence(result):
    """Decide whether this property is safe for instant pricing or should be reviewed."""
    parcel_sqft = float(result.get("parcel_sqft", 0) or 0)
    lawn_sqft = float(result.get("lawn_sqft", 0) or 0)
    hardscape_sqft = float(result.get("hardscape_sqft", 0) or 0)
    building_sqft = float(result.get("building_sqft", 0) or 0)
    property_type = result.get("property_type", "Residential")

    usable_yard = max(parcel_sqft - building_sqft, 1)
    hardscape_ratio = hardscape_sqft / usable_yard

    reasons = []

    if property_type == "Commercial":
        reasons.append("Commercial property")
    if parcel_sqft > 20000:
        reasons.append("Large or estate-size parcel")
    if lawn_sqft > 12000:
        reasons.append("Large estimated maintained lawn area")
    if hardscape_ratio > 0.35:
        reasons.append("High hardscape ratio")
    if building_sqft <= 0:
        reasons.append("Building footprint unavailable")

    if reasons:
        return "Manual review", reasons

    return "Auto quote", []


def image_to_data_url(image):
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def ai_review_property(parcel_geometry, result, pricing_settings):
    """OpenAI vision classifier for every quote.

    Commercial-safe approach:
    - AI does NOT provide the final sqft measurement.
    - AI classifies the property type/risk from the aerial image.
    - The app uses that classification to make the pricing tier more conservative.
    - Customer only sees service + quote, not lawn sqft.
    """
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "Math estimate",
            "confidence": "none",
            "notes": "OpenAI API key is not configured, so the original math estimate was used.",
            "property_class": "unknown",
            "risk_level": "unknown",
            "adjustment_factor": 1.0,
        }

    try:
        lat, lon = parcel_centroid(parcel_geometry)
        image, top_left, zoom = fetch_mosaic(lat, lon)
        if image is None:
            raise RuntimeError("Could not load aerial imagery for AI classification.")

        data_url = image_to_data_url(image)

        prompt = f"""
You are reviewing an aerial image for a lawn-care instant quote system.

Your job is NOT to measure exact square footage.
Your job is to classify the property so the quote can be made safer.

The app already has GIS/math estimates:
- parcel area
- building footprint
- satellite hardscape estimate
- math lawn estimate

You must decide whether the property looks like a standard lawn or a risky/high-hardscape property.

Property classes:
- "standard_residential": normal house with normal grass areas.
- "pool_or_concrete_backyard": pool, pool deck, concrete backyard, pavers, patio-dominant yard.
- "high_hardscape": unusually large driveway/patio/paving/concrete/gravel.
- "tree_covered_or_unclear": tree cover/shadows make mowable lawn hard to see.
- "estate_or_large_lot": large lot where not all land may be maintained lawn.
- "commercial_or_nonstandard": commercial or unusual property.

Return ONLY valid JSON with this exact shape:
{{
  "confidence": "high" or "medium" or "low",
  "property_class": "standard_residential" or "pool_or_concrete_backyard" or "high_hardscape" or "tree_covered_or_unclear" or "estate_or_large_lot" or "commercial_or_nonstandard",
  "risk_level": "low" or "medium" or "high",
  "adjustment_factor": number,
  "notes": "brief internal explanation"
}}

Adjustment factor rules:
- 1.00 = no adjustment; normal property.
- 0.85 = slightly reduce effective lawn/pricing size.
- 0.70 = meaningfully reduce effective lawn/pricing size.
- 0.55 = strongly reduce effective lawn/pricing size for pool/concrete/high-hardscape yards.
- 0.40 = extreme case where very little visible mowable grass exists.

Important:
- When the backyard contains a pool and mostly concrete/pavers, use "pool_or_concrete_backyard" and an adjustment_factor between 0.40 and 0.70.
- When unsure, be conservative for the lawn company and reduce the effective lawn size.
- Do not count pool decks, concrete, pavers, patios, driveways, gravel, beds, shrubs, decorative areas, or tree-covered uncertain areas as reliable mowable lawn.
- Ask yourself: "Would a lawn crew actually mow this area?" If no, treat it as a risk that should reduce the pricing size.

GIS/math context:
Address: {result.get('address')}
Property type: {result.get('property_type')}
Service: {result.get('service_type')}
Parcel sqft: {result.get('parcel_sqft')}
GIS building sqft: {result.get('building_sqft')}
Math hardscape sqft: {result.get('hardscape_sqft')}
Math lawn sqft: {result.get('lawn_sqft')}
Current price: ${result.get('price')}
Hardscape method: {result.get('hardscape_method')}
"""

        payload = {
            "model": "gpt-4o-mini",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url, "detail": "high"},
                    ],
                }
            ],
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()

        text_parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text_parts.append(content.get("text", ""))
        text = "\n".join(text_parts).strip()

        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()

        review = json.loads(text)
        confidence = str(review.get("confidence", "low")).lower()
        property_class = str(review.get("property_class", "standard_residential"))
        risk_level = str(review.get("risk_level", "medium")).lower()
        notes = review.get("notes", "")

        try:
            adjustment_factor = float(review.get("adjustment_factor", 1.0))
        except Exception:
            adjustment_factor = 1.0

        # Safety clamps: AI can classify risk, but cannot destroy pricing.
        adjustment_factor = max(0.40, min(adjustment_factor, 1.0))

        # Extra deterministic protection for known risky classes.
        if property_class in ["pool_or_concrete_backyard", "high_hardscape"]:
            adjustment_factor = min(adjustment_factor, 0.70)
        if property_class in ["tree_covered_or_unclear", "estate_or_large_lot", "commercial_or_nonstandard"]:
            adjustment_factor = min(adjustment_factor, 0.85)

        return {
            "status": "AI classified",
            "confidence": confidence,
            "notes": notes,
            "property_class": property_class,
            "risk_level": risk_level,
            "adjustment_factor": adjustment_factor,
        }
    except Exception as e:
        return {
            "status": "Math estimate",
            "confidence": "error",
            "notes": f"AI classification failed, so the original math estimate was used: {e}",
            "property_class": "unknown",
            "risk_level": "unknown",
            "adjustment_factor": 1.0,
        }


# ----------------------------
# GIS + estimate helpers
# ----------------------------
def clean_address(text):
    text = text.upper().strip()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    for word in ["SARNIA", "ONTARIO", "CANADA"]:
        text = text.replace(word, "")
    return text.strip()


def get_json(url, params, timeout=20):
    r = requests.get(url, params=params, timeout=timeout, verify=False)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data


@st.cache_data(ttl=3600)
def search_addresses(query):
    q = clean_address(query)
    if not q:
        return []

    params = {
        "f": "json",
        "where": f"UPPER(ADDRESS) LIKE '%{q}%'",
        "outFields": "ADDRESS",
        "returnGeometry": "false",
        "resultRecordCount": 10,
        "orderByFields": "ADDRESS ASC",
    }

    try:
        data = get_json(ADDRESS_URL, params)
    except Exception:
        data = get_json(ADDRESS_FALLBACK_URL, params)

    addresses = []
    seen = set()

    for f in data.get("features", []):
        attrs = f.get("attributes", {})
        addr = attrs.get("ADDRESS")
        if addr and addr not in seen:
            seen.add(addr)
            addresses.append(addr)

    return addresses


@st.cache_data(ttl=3600)
def find_address_point(address):
    params = {
        "f": "json",
        "where": f"ADDRESS='{address}'",
        "outFields": "ADDRESS",
        "returnGeometry": "true",
        "outSR": "26917",
        "resultRecordCount": 1,
    }

    try:
        data = get_json(ADDRESS_URL, params)
    except Exception:
        data = get_json(ADDRESS_FALLBACK_URL, params)

    features = data.get("features", [])
    if not features:
        return None

    geom = features[0].get("geometry", {})
    return geom.get("x"), geom.get("y")


@st.cache_data(ttl=3600)
def find_parcel_from_point(x, y):
    params = {
        "f": "geojson",
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "inSR": "26917",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,ARN,PRIMARY_ARN,Shape.STArea(),Shape.STLength()",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": 1,
    }

    data = get_json(PARCEL_URL, params)
    features = data.get("features", [])
    if not features:
        return None

    return features[0]


def area_sqft_wgs84(geometry):
    geom = shape(geometry)
    geom_utm = transform(lambda x, y: TO_UTM17.transform(x, y), geom)
    return geom_utm.area * 10.7639


@st.cache_resource
def load_buildings():
    """Load simplified building footprints from buildings_simple.geojson.

    This avoids GeoPandas/Fiona/GDAL so the app can run on Streamlit Cloud.
    The GeoJSON was exported in EPSG:26917, so coordinates are already UTM metres.
    Returns a list of (bounds, geometry) tuples for fast bounding-box filtering.
    """
    if not BUILDINGS_GEOJSON.exists():
        return []

    try:
        with BUILDINGS_GEOJSON.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    buildings = []
    for feature in data.get("features", []):
        geom_data = feature.get("geometry")
        if not geom_data:
            continue
        try:
            geom = shape(geom_data).buffer(0)
            if not geom.is_empty:
                buildings.append((geom.bounds, geom))
        except Exception:
            continue

    return buildings


def building_area_inside_parcel(parcel_geometry):
    buildings = load_buildings()

    if not buildings:
        return 0, False

    parcel_utm = transform(lambda x, y: TO_UTM17.transform(x, y), shape(parcel_geometry))
    minx, miny, maxx, maxy = parcel_utm.bounds

    total_area_m2 = 0.0
    found_candidate = False

    for bounds, geom in buildings:
        bx1, by1, bx2, by2 = bounds

        # Fast bounding-box reject before doing expensive intersection.
        if bx2 < minx or bx1 > maxx or by2 < miny or by1 > maxy:
            continue

        found_candidate = True
        try:
            inter = geom.intersection(parcel_utm)
            if not inter.is_empty:
                total_area_m2 += inter.area
        except Exception:
            pass

    return total_area_m2 * 10.7639, True


def parcel_centroid(parcel_geometry):
    geom = shape(parcel_geometry)
    c = geom.centroid
    return c.y, c.x


def latlon_to_tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def lonlat_to_global_pixel(lon, lat, zoom):
    world_px = 256 * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * world_px
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * world_px
    return x, y


def meters_per_pixel(lat, zoom):
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


@st.cache_data(ttl=86400)
def fetch_tile_bytes(x, y, z):
    url = TILE_URL.format(z=z, y=y, x=x)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.content


def fetch_tile(x, y, z):
    return Image.open(BytesIO(fetch_tile_bytes(x, y, z))).convert("RGB")


def fetch_mosaic(lat, lon):
    for zoom in [19, 18, 17]:
        cx, cy = latlon_to_tile(lat, lon, zoom)
        radius_tiles = 1
        size = 256 * (radius_tiles * 2 + 1)
        mosaic = Image.new("RGB", (size, size))

        for dx in range(-radius_tiles, radius_tiles + 1):
            for dy in range(-radius_tiles, radius_tiles + 1):
                tile = fetch_tile(cx + dx, cy + dy, zoom)
                mosaic.paste(tile, ((dx + radius_tiles) * 256, (dy + radius_tiles) * 256))

        top_left_global = ((cx - radius_tiles) * 256, (cy - radius_tiles) * 256)
        return mosaic, top_left_global, zoom

    return None, None, None


def draw_ring(draw, ring, top_left_global, zoom, fill):
    points = []
    tlx, tly = top_left_global

    for lon, lat in ring:
        gx, gy = lonlat_to_global_pixel(lon, lat, zoom)
        points.append((gx - tlx, gy - tly))

    if len(points) >= 3:
        draw.polygon(points, fill=fill)


def parcel_pixel_mask(parcel_geometry, image_size, top_left_global, zoom):
    mask = Image.new("L", image_size, 0)
    draw = ImageDraw.Draw(mask)

    geom_type = parcel_geometry.get("type")
    coords = parcel_geometry.get("coordinates", [])

    if geom_type == "Polygon":
        if coords:
            draw_ring(draw, coords[0], top_left_global, zoom, 255)
            for hole in coords[1:]:
                draw_ring(draw, hole, top_left_global, zoom, 0)

    elif geom_type == "MultiPolygon":
        for poly in coords:
            if poly:
                draw_ring(draw, poly[0], top_left_global, zoom, 255)
                for hole in poly[1:]:
                    draw_ring(draw, hole, top_left_global, zoom, 0)

    return np.array(mask) > 0


def satellite_hardscape_area(parcel_geometry, parcel_sqft, building_sqft):
    lat, lon = parcel_centroid(parcel_geometry)
    image, top_left, zoom = fetch_mosaic(lat, lon)

    if image is None:
        return None

    arr = np.array(image).astype(np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    maxc = np.maximum.reduce([r, g, b])
    minc = np.minimum.reduce([r, g, b])
    sat = (maxc - minc) / np.maximum(maxc, 1)
    brightness = (r + g + b) / 3

    green = (
        (g > r * 1.05)
        & (g > b * 1.03)
        & (g > 45)
    )

    grey_or_tan = (
        (sat < 0.20)
        & (brightness > 70)
        & (brightness < 230)
    )

    dark_asphalt = (
        (sat < 0.16)
        & (brightness >= 35)
        & (brightness <= 105)
    )

    hardscape = (grey_or_tan | dark_asphalt) & (~green)

    parcel_mask = parcel_pixel_mask(parcel_geometry, image.size, top_left, zoom)
    final = hardscape & parcel_mask

    mpp = meters_per_pixel(lat, zoom)
    raw_sqft = final.sum() * (mpp ** 2) * 10.7639
    remaining_land = max(parcel_sqft - building_sqft, 0)

    detected = min(raw_sqft, remaining_land * 0.55)

    if detected < 250:
        return None

    return int(detected)


def fixed_hardscape(parcel_sqft, building_sqft):
    remaining = max(parcel_sqft - building_sqft, 0)

    if parcel_sqft <= 4500:
        allowance = 550
    elif parcel_sqft <= 8000:
        allowance = 1000
    elif parcel_sqft <= 12000:
        allowance = 1250
    elif parcel_sqft <= 20000:
        allowance = 1700
    else:
        allowance = 2300

    return int(min(allowance, remaining * 0.40))


def estimate_lawn(parcel_sqft, building_sqft, satellite_hardscape_sqft):
    yard_after_buildings = max(parcel_sqft - building_sqft, 0)

    fixed = fixed_hardscape(parcel_sqft, building_sqft)
    sat = satellite_hardscape_sqft if satellite_hardscape_sqft is not None else 0

    hardscape_sqft = max(fixed, sat)

    if sat > fixed:
        method = "Satellite hardscape"
    else:
        method = "Fixed hardscape"

    lawn_sqft = yard_after_buildings - hardscape_sqft

    lawn_sqft = max(lawn_sqft, 350)
    lawn_sqft = min(lawn_sqft, parcel_sqft * 0.72)

    return int(lawn_sqft), int(hardscape_sqft), int(fixed), int(sat), method


def quote_price_for_service(lawn_sqft, property_type, service_type, pricing_settings):
    service_pricing = get_service_pricing(pricing_settings, service_type)
    property_multiplier = get_property_multiplier(pricing_settings, property_type)

    base_fee = float(service_pricing.get("base_fee", 0))
    rate_per_1000 = float(service_pricing.get("rate_per_1000_sqft", 0))
    minimum_price = float(service_pricing.get("minimum_price", 0))

    price = base_fee + (lawn_sqft / 1000) * rate_per_1000
    price = price * property_multiplier

    return int(max(price, minimum_price))



def get_tier_prices(pricing_settings, service_type):
    """Get editable tier prices for a service, with safe defaults."""
    default_tiers = DEFAULT_PRICING_SETTINGS["tier_prices"].get(
        service_type,
        DEFAULT_PRICING_SETTINGS["tier_prices"]["Grass cutting"],
    )
    saved = pricing_settings.get("tier_prices", {}).get(service_type, {})

    tiers = dict(default_tiers)
    if isinstance(saved, dict):
        tiers.update(saved)

    return tiers


def round_to_nearest_5(value):
    return int(round(float(value) / 5.0) * 5)


def tier_rank(tier_name):
    order = ["Small", "Standard", "Large", "Complex", "Estate"]
    try:
        return order.index(tier_name)
    except ValueError:
        return 1


def max_tier(tier_a, tier_b):
    order = ["Small", "Standard", "Large", "Complex", "Estate"]
    return order[max(tier_rank(tier_a), tier_rank(tier_b))]


def base_tier_from_property(parcel_sqft, building_sqft, lawn_sqft, property_type):
    """Initial deterministic tier before AI classification.

    The estimated sqft is kept as an admin/internal signal, but the public quote is
    driven by pricing tiers. This avoids pretending the app is an exact measuring
    tape while still giving the company useful property intelligence.
    """
    parcel_sqft = float(parcel_sqft or 0)
    property_type = str(property_type or "Residential")

    if property_type == "Commercial":
        return "Complex"

    # Base tier is mostly driven by overall parcel size, because that is the most
    # reliable GIS signal. Pool/high-hardscape adjustments happen after AI review.
    if parcel_sqft <= 4500:
        return "Small"
    elif parcel_sqft <= 8500:
        return "Standard"
    elif parcel_sqft <= 13000:
        return "Large"
    elif parcel_sqft <= 20000:
        return "Complex"
    else:
        return "Estate"


def quote_confidence_score(result):
    """Internal confidence score for the admin/company view.

    This is not a promise of exact square footage. It is a quick indicator of how
    much supporting data the quote engine had available.
    """
    score = 95

    if not result.get("building_data_used"):
        score -= 15

    if not result.get("satellite_hardscape_sqft"):
        score -= 5

    confidence = str(result.get("ai_review_confidence", "")).lower()
    if confidence in ["low", "error", "none", ""]:
        score -= 15
    elif confidence == "medium":
        score -= 7

    risk = str(result.get("ai_risk_level", "")).lower()
    if risk == "high":
        score -= 15
    elif risk == "medium":
        score -= 8

    property_class = str(result.get("ai_property_class", "")).lower()
    if property_class == "tree_covered_or_unclear":
        score -= 15
    elif property_class in ["commercial_or_nonstandard", "estate_or_large_lot"]:
        score -= 10

    if result.get("review_reasons"):
        score -= 8

    return max(50, min(98, int(score)))


def tier_from_ai_class(property_class, risk_level, current_tier):
    order = ["Small", "Standard", "Large", "Complex", "Estate"]

    try:
        idx = order.index(current_tier)
    except ValueError:
        idx = 1

    property_class = str(property_class or "").lower()

    # Pool/concrete properties usually have LESS grass
    if property_class == "pool_or_concrete_backyard":
        idx -= 1

    # High hardscape usually means LESS mowing
    elif property_class == "high_hardscape":
        idx -= 1

    # Estate lots are genuinely larger
    elif property_class == "estate_or_large_lot":
        idx = max(idx, order.index("Estate"))

    # Commercial is usually more complex
    elif property_class == "commercial_or_nonstandard":
        idx = max(idx, order.index("Complex"))

    # Tree cover / uncertainty shouldn't increase price
    elif property_class == "tree_covered_or_unclear":
        pass

    idx = max(0, min(idx, len(order) - 1))

    return order[idx]


def tier_price_for_service(pricing_settings, service_type, property_type, tier_name):
    tiers = get_tier_prices(pricing_settings, service_type)
    price = float(tiers.get(tier_name, tiers.get("Standard", 55.0)))
    multiplier = get_property_multiplier(pricing_settings, property_type)
    return round_to_nearest_5(price * multiplier)


def address_autocomplete(searchterm: str):
    if not searchterm or len(searchterm.strip()) < 3:
        return []
    return search_addresses(searchterm)


def append_csv(path, row, fieldnames):
    file_exists = path.exists()

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)


def save_quote(result):
    append_csv(
        QUOTES_CSV,
        {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "quote_id": result["quote_id"],
            "address": result["address"],
            "property_type": result["property_type"],
            "service_type": result["service_type"],
            "estimated_price": result["price"],
            "estimated_lawn_sqft": result["lawn_sqft"],
            "parcel_sqft": result["parcel_sqft"],
            "building_sqft": result["building_sqft"],
            "hardscape_sqft": result["hardscape_sqft"],
            "fixed_hardscape_sqft": result["fixed_hardscape_sqft"],
            "satellite_hardscape_sqft": result["satellite_hardscape_sqft"],
            "hardscape_method": result["hardscape_method"],
            "quote_status": result.get("quote_status", ""),
            "review_reasons": result.get("review_reasons", ""),
            "ai_review_confidence": result.get("ai_review_confidence", ""),
            "original_lawn_sqft": result.get("original_lawn_sqft", ""),
            "original_price": result.get("original_price", ""),
            "ai_review_notes": result.get("ai_review_notes", ""),
            "ai_property_class": result.get("ai_property_class", ""),
            "ai_risk_level": result.get("ai_risk_level", ""),
            "ai_adjustment_factor": result.get("ai_adjustment_factor", ""),
            "base_pricing_tier": result.get("base_pricing_tier", ""),
            "pricing_lawn_sqft": result.get("pricing_lawn_sqft", ""),
            "pricing_tier": result.get("pricing_tier", ""),
            "tier_pricing_sqft": result.get("tier_pricing_sqft", ""),
            "quote_confidence_score": result.get("quote_confidence_score", ""),
        },
        [
            "timestamp",
            "quote_id",
            "address",
            "property_type",
            "service_type",
            "estimated_price",
            "estimated_lawn_sqft",
            "parcel_sqft",
            "building_sqft",
            "hardscape_sqft",
            "fixed_hardscape_sqft",
            "satellite_hardscape_sqft",
            "hardscape_method",
            "quote_status",
            "review_reasons",
            "ai_review_confidence",
            "original_lawn_sqft",
            "original_price",
            "ai_review_notes",
            "ai_property_class",
            "ai_risk_level",
            "ai_adjustment_factor",
            "base_pricing_tier",
            "pricing_lawn_sqft",
            "pricing_tier",
            "tier_pricing_sqft",
            "quote_confidence_score",
        ],
    )


def save_lead(name, phone, email, notes, result):
    append_csv(
        LEADS_CSV,
        {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "quote_id": result["quote_id"],
            "name": name,
            "phone": phone,
            "email": email,
            "notes": notes,
            "address": result["address"],
            "property_type": result["property_type"],
            "service_type": result["service_type"],
            "estimated_price": result["price"],
            "estimated_lawn_sqft": result["lawn_sqft"],
            "quote_status": result.get("quote_status", ""),
            "pricing_tier": result.get("pricing_tier", ""),
            "quote_confidence_score": result.get("quote_confidence_score", ""),
            "status": "New",
        },
        [
            "timestamp",
            "quote_id",
            "name",
            "phone",
            "email",
            "notes",
            "address",
            "property_type",
            "service_type",
            "estimated_price",
            "estimated_lawn_sqft",
            "quote_status",
            "pricing_tier",
            "quote_confidence_score",
            "status",
        ],
    )


def read_csv_if_exists(path):
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


# ----------------------------
# Session state
# ----------------------------
if "result" not in st.session_state:
    st.session_state.result = None

if "lead_submitted" not in st.session_state:
    st.session_state.lead_submitted = False

local_pricing_settings = load_pricing_settings()
pricing_settings = local_pricing_settings
pricing_source = "Local JSON fallback"

# Phase 2B: pricing now loads from Supabase when configured.
# The local JSON remains as a backup so the app still runs if Supabase is unavailable.
if get_pricing_settings_supabase:
    try:
        pricing_settings = get_pricing_settings_supabase(DEFAULT_PRICING_SETTINGS)
        pricing_source = "Supabase"
    except Exception as e:
        pricing_settings = local_pricing_settings
        pricing_source = f"Local JSON fallback - Supabase pricing load failed: {e}"



# ----------------------------
# Main quote calculation wrapper
# ----------------------------
def calculate_quote(selected_address, property_type, service_type, pricing_settings):
    point = find_address_point(selected_address)

    if not point or not point[0] or not point[1]:
        raise ValueError("Could not find coordinates for that address.")

    parcel = find_parcel_from_point(point[0], point[1])

    if not parcel:
        raise ValueError("Could not find parcel boundary for that address.")

    parcel_geom = parcel.get("geometry")
    parcel_sqft = int(area_sqft_wgs84(parcel_geom))
    building_sqft, building_data_used = building_area_inside_parcel(parcel_geom)

    try:
        satellite_hardscape_sqft = satellite_hardscape_area(parcel_geom, parcel_sqft, building_sqft)
    except Exception:
        satellite_hardscape_sqft = None

    (
        lawn_sqft,
        hardscape_sqft,
        fixed_hardscape_sqft,
        satellite_hardscape_used,
        hardscape_method,
    ) = estimate_lawn(parcel_sqft, building_sqft, satellite_hardscape_sqft)

    price = quote_price_for_service(
        lawn_sqft,
        property_type,
        service_type,
        pricing_settings,
    )

    result = {
        "quote_id": str(uuid.uuid4())[:8].upper(),
        "address": selected_address,
        "property_type": property_type,
        "service_type": service_type,
        "parcel_sqft": parcel_sqft,
        "building_sqft": int(building_sqft),
        "hardscape_sqft": hardscape_sqft,
        "fixed_hardscape_sqft": fixed_hardscape_sqft,
        "satellite_hardscape_sqft": satellite_hardscape_used,
        "hardscape_method": hardscape_method,
        "lawn_sqft": int(lawn_sqft),
        "price": price,
        "building_data_used": building_data_used,
    }

    quote_status, review_reasons = quote_confidence(result)

    result["quote_status"] = "Math estimate"
    result["review_reasons"] = ", ".join(review_reasons)
    result["original_lawn_sqft"] = int(lawn_sqft)
    result["original_price"] = price

    # AI is now used as an internal classifier, not as the measuring tape.
    # It can reduce the effective pricing size for high-risk/high-hardscape properties.
    result["ai_review_confidence"] = ""
    result["ai_review_notes"] = ""
    result["ai_property_class"] = ""
    result["ai_risk_level"] = ""
    result["ai_adjustment_factor"] = 1.0

    ai_review = ai_review_property(parcel_geom, result, pricing_settings)
    result["quote_status"] = ai_review.get("status", "Math estimate")
    result["ai_review_confidence"] = ai_review.get("confidence", "")
    result["ai_review_notes"] = ai_review.get("notes", "")
    result["ai_property_class"] = ai_review.get("property_class", "")
    result["ai_risk_level"] = ai_review.get("risk_level", "")
    result["ai_adjustment_factor"] = float(ai_review.get("adjustment_factor", 1.0) or 1.0)

    # New commercial quoting model:
    # Address -> Parcel/Building/Imagery -> Service difficulty -> Pricing tier.
    # Sqft is only an internal signal; the customer receives only the tier-based quote.
    base_tier = base_tier_from_property(
        parcel_sqft=result["parcel_sqft"],
        building_sqft=result["building_sqft"],
        lawn_sqft=result["lawn_sqft"],
        property_type=property_type,
    )

    final_tier = tier_from_ai_class(
        property_class=result["ai_property_class"],
        risk_level=result["ai_risk_level"],
        current_tier=base_tier,
    )

    final_price = tier_price_for_service(
        pricing_settings=pricing_settings,
        service_type=service_type,
        property_type=property_type,
        tier_name=final_tier,
    )

    result["base_pricing_tier"] = base_tier
    result["pricing_lawn_sqft"] = ""
    result["pricing_tier"] = final_tier
    result["tier_pricing_sqft"] = ""
    result["price"] = final_price
    result["quote_status"] = "Tier quote"

    # If the property is risky, keep that internal only.
    if result["ai_risk_level"] in ["medium", "high"] or result["review_reasons"]:
        result["quote_status"] = "Tier quote - confirmation recommended"

    result["quote_confidence_score"] = quote_confidence_score(result)

    return result


# ----------------------------
# Sidebar: hidden admin controls
# ----------------------------
with st.sidebar:
    show_admin = st.checkbox("Show admin/export")

    if show_admin:
        st.subheader("Company settings")
        st.caption("Phase 2B: tier pricing loads from Supabase. The local JSON file is kept as a backup.")
        st.caption(f"Pricing source: {pricing_source}")

        edited_pricing = json.loads(json.dumps(pricing_settings))

        edited_pricing["company_name"] = st.text_input(
            "Company name",
            value=str(edited_pricing.get("company_name", "Lawn Company")),
            key="company_name_setting",
        )
        edited_pricing["lead_notification_email"] = st.text_input(
            "Lead notification email",
            value=str(edited_pricing.get("lead_notification_email", "")),
            key="lead_notification_email_setting",
        )
        edited_pricing["enable_ai_review"] = True
        st.info("AI classification runs internally on every quote. Customer pricing is based on tiers, not exact sqft. If the API key is missing or fails, the app uses the base tier from GIS/math signals.")

        st.subheader("Pricing tiers")
        st.caption("Set the prices customers actually see. Most lawn companies quote in clean tiers like $45, $55, $65, $75.")

        if "tier_prices" not in edited_pricing or not isinstance(edited_pricing.get("tier_prices"), dict):
            edited_pricing["tier_prices"] = json.loads(json.dumps(DEFAULT_PRICING_SETTINGS["tier_prices"]))

        for service_name in [
            "Grass cutting",
            "Seasonal cleanup",
            "Fertilization and weed control",
        ]:
            if service_name not in edited_pricing["tier_prices"]:
                edited_pricing["tier_prices"][service_name] = json.loads(
                    json.dumps(DEFAULT_PRICING_SETTINGS["tier_prices"][service_name])
                )

            with st.expander(f"{service_name} tier prices", expanded=False):
                for tier_name in ["Small", "Standard", "Large", "Complex", "Estate"]:
                    edited_pricing["tier_prices"][service_name][tier_name] = st.number_input(
                        f"{tier_name} price ($)",
                        min_value=0.0,
                        value=float(edited_pricing["tier_prices"][service_name].get(tier_name, 0)),
                        step=5.0,
                        key=f"{service_name}_{tier_name}_tier_price",
                    )

        with st.expander("Property multipliers", expanded=False):
            edited_pricing["Residential"]["multiplier"] = st.number_input(
                "Residential multiplier",
                min_value=0.0,
                value=float(edited_pricing["Residential"].get("multiplier", 1.0)),
                step=0.05,
                key="residential_multiplier",
            )
            edited_pricing["Commercial"]["multiplier"] = st.number_input(
                "Commercial multiplier",
                min_value=0.0,
                value=float(edited_pricing["Commercial"].get("multiplier", 1.25)),
                step=0.05,
                key="commercial_multiplier",
            )

        col_save, col_reset = st.columns(2)
        with col_save:
            if st.button("Save pricing"):
                save_pricing_settings(edited_pricing)  # local backup
                if save_pricing_settings_supabase:
                    try:
                        save_pricing_settings_supabase(edited_pricing)
                        st.success("Pricing saved to Supabase.")
                    except Exception as e:
                        st.warning(f"Pricing saved locally, but Supabase update failed: {e}")
                else:
                    st.success("Pricing saved locally. Supabase pricing helper is unavailable.")
                st.rerun()

        with col_reset:
            if st.button("Reset defaults"):
                save_pricing_settings(DEFAULT_PRICING_SETTINGS)  # local backup
                if save_pricing_settings_supabase:
                    try:
                        save_pricing_settings_supabase(DEFAULT_PRICING_SETTINGS)
                        st.success("Defaults restored in Supabase.")
                    except Exception as e:
                        st.warning(f"Defaults restored locally, but Supabase update failed: {e}")
                else:
                    st.success("Defaults restored locally. Supabase pricing helper is unavailable.")
                st.rerun()


# ----------------------------
# Customer UI
# ----------------------------
st.markdown(
    """
    <div class="simple-header">
        <h1>Instant Quote</h1>
        <p>Select a service, enter your address, and get an estimated price.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

service_type = st.selectbox(
    "Service",
    ["Grass cutting", "Seasonal cleanup", "Fertilization and weed control"],
)

property_type = st.selectbox("Property type", ["Residential", "Commercial"])

if st_searchbox:
    selected_address = st_searchbox(
        address_autocomplete,
        key="address_searchbox",
        placeholder="Start typing your address...",
        label="Address",
        default_options=[],
        clear_on_submit=False,
    )
else:
    st.warning("For Google-style autocomplete, install: pip install streamlit-searchbox")
    address_input = st.text_input("Address")
    suggestions = search_addresses(address_input) if address_input else []
    selected_address = st.selectbox("Select your address", suggestions) if suggestions else None

submitted = st.button("Get Quote", type="primary")

if submitted:
    st.session_state.result = None
    st.session_state.lead_submitted = False

    if not selected_address:
        st.error("Please select an address.")
        st.stop()

    with st.spinner("Calculating quote..."):
        try:
            result = calculate_quote(
                selected_address,
                property_type,
                service_type,
                pricing_settings,
            )
        except Exception as e:
            st.error(str(e))
            st.stop()

    st.session_state.result = result
    save_quote(result)

    # Phase 2A: also save to Supabase, while keeping CSV as a safe fallback.
    if save_quote_supabase:
        try:
            save_quote_supabase(result)
        except Exception as e:
            if show_admin:
                st.warning(f"Quote saved locally, but Supabase quote save failed: {e}")


if st.session_state.result:
    r = st.session_state.result

    status_label = "estimated price"
    st.markdown(
        f"""
        <div class="quote-card">
            <div class="quote-service">{r['service_type']}</div>
            <div class="quote-price">${r['price']}</div>
            <div class="quote-label">{status_label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="fine-print">Final price may be confirmed after first visit if site conditions differ.</div>', unsafe_allow_html=True)

    with st.expander("Request this service"):
        customer_name = st.text_input("Name")
        customer_phone = st.text_input("Phone")
        customer_email = st.text_input("Email")
        customer_notes = st.text_area("Notes", placeholder="Gate code, pets, preferred day, etc.")

        if st.button("Submit request"):
            if not customer_name.strip() or not customer_phone.strip():
                st.error("Please enter your name and phone number.")
            else:
                save_lead(customer_name, customer_phone, customer_email, customer_notes, r)

                # Phase 2A: also save to Supabase, while keeping CSV as a safe fallback.
                if save_lead_supabase:
                    try:
                        save_lead_supabase(customer_name, customer_phone, customer_email, customer_notes, r)
                    except Exception as e:
                        if show_admin:
                            st.warning(f"Lead saved locally, but Supabase lead save failed: {e}")

                email_sent, email_message = send_lead_email(
                    pricing_settings,
                    customer_name,
                    customer_phone,
                    customer_email,
                    customer_notes,
                    r,
                )
                st.session_state.lead_submitted = True
                if email_sent:
                    st.success("Request submitted. The company will follow up with you.")
                else:
                    st.success("Request submitted. The company will follow up with you.")
                    if show_admin:
                        st.warning(f"Lead saved, but email was not sent: {email_message}")


# ----------------------------
# Admin/export area
# ----------------------------
if show_admin:
    st.divider()
    st.header("Admin / Export")

    if st.session_state.result:
        r_admin = st.session_state.result
        st.markdown("### Current quote property report")
        a, b, c = st.columns(3)
        a.metric("Estimated Mowable Area", f"{int(r_admin.get('lawn_sqft', 0)):,} sqft")
        b.metric("Final Tier", str(r_admin.get("pricing_tier", "")))
        c.metric("Quote Confidence", f"{int(r_admin.get('quote_confidence_score', 0))}%")

        d, e, f = st.columns(3)
        d.metric("Parcel Size", f"{int(r_admin.get('parcel_sqft', 0)):,} sqft")
        e.metric("Building Footprint", f"{int(r_admin.get('building_sqft', 0)):,} sqft")
        f.metric("Hardscape Estimate", f"{int(r_admin.get('hardscape_sqft', 0)):,} sqft")

        with st.expander("Internal tier reasoning", expanded=True):
            st.write(f"**Address:** {r_admin.get('address', '')}")
            st.write(f"**AI Class:** {r_admin.get('ai_property_class', '')}")
            st.write(f"**AI Risk Level:** {r_admin.get('ai_risk_level', '')}")
            st.write(f"**AI Notes:** {r_admin.get('ai_review_notes', '')}")
            st.write(f"**Base Tier:** {r_admin.get('base_pricing_tier', '')}")
            st.write(f"**Final Tier:** {r_admin.get('pricing_tier', '')}")
            st.write(f"**Quote Status:** {r_admin.get('quote_status', '')}")
            st.caption("Estimated mowable area is admin-only. Customer pricing is tier-based, not exact square-foot pricing.")

    # Phase 2C: admin dashboard reads from Supabase first, with CSV fallback.
    admin_data_source = "Supabase"
    try:
        if get_leads_supabase and get_quotes_supabase:
            leads_df = pd.DataFrame(get_leads_supabase())
            quotes_df = pd.DataFrame(get_quotes_supabase())
        else:
            raise RuntimeError("Supabase admin helpers unavailable.")
    except Exception as e:
        admin_data_source = "CSV fallback"
        leads_df = read_csv_if_exists(LEADS_CSV)
        quotes_df = read_csv_if_exists(QUOTES_CSV)
        st.warning(f"Using CSV fallback for admin data: {e}")

    st.caption(f"Admin data source: {admin_data_source}")

    col1, col2, col3 = st.columns(3)
    col1.metric("Quotes", len(quotes_df))
    col2.metric("Leads", len(leads_df))
    conversion = (len(leads_df) / len(quotes_df) * 100) if len(quotes_df) else 0
    col3.metric("Lead conversion", f"{conversion:.0f}%")

    tabs = st.tabs(["Leads", "Quotes", "Pricing"])

    with tabs[0]:
        st.markdown("### Leads")
        if leads_df.empty:
            st.info("No leads submitted yet.")
        else:
            display_leads = leads_df.copy()
            if "id" in display_leads.columns:
                display_leads = display_leads.drop(columns=["id"])
            st.dataframe(display_leads, use_container_width=True)

            if admin_data_source == "Supabase" and "id" in leads_df.columns and update_lead_status_supabase:
                with st.expander("Update lead status", expanded=False):
                    lead_options = {}
                    for _, row in leads_df.iterrows():
                        label = f"{row.get('created_at', '')} | {row.get('customer_name', '')} | {row.get('address', '')} | {row.get('status', 'New')}"
                        lead_options[label] = row.get("id")

                    selected_label = st.selectbox("Lead", list(lead_options.keys()), key="lead_status_select")
                    new_status = st.selectbox("New status", ["New", "Contacted", "Quoted", "Won", "Lost", "Archived"], key="lead_status_value")

                    if st.button("Update selected lead"):
                        try:
                            update_lead_status_supabase(lead_options[selected_label], new_status)
                            st.success("Lead status updated.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not update lead status: {e}")

            st.download_button(
                "Download leads CSV",
                leads_df.to_csv(index=False),
                file_name="leads.csv",
                mime="text/csv",
            )

    with tabs[1]:
        st.markdown("### Quotes + internal estimate details")
        if quotes_df.empty:
            st.info("No quotes generated yet.")
        else:
            st.dataframe(quotes_df, use_container_width=True)
            st.download_button(
                "Download quotes CSV",
                quotes_df.to_csv(index=False),
                file_name="quotes.csv",
                mime="text/csv",
            )

    with tabs[2]:
        st.markdown("### Pricing")
        st.info("Pricing is edited in the sidebar under Show admin/export → Pricing tiers. Changes save to Supabase and the local JSON backup.")
        pricing_rows = []
        for service_name, tiers in pricing_settings.get("tier_prices", {}).items():
            pricing_rows.append({
                "service_name": service_name,
                "Small": tiers.get("Small"),
                "Standard": tiers.get("Standard"),
                "Large": tiers.get("Large"),
                "Complex": tiers.get("Complex"),
                "Estate": tiers.get("Estate"),
            })
        st.dataframe(pd.DataFrame(pricing_rows), use_container_width=True)
