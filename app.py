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
AI Review Notes: {result.get('ai_review_notes', '')}
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
    """OpenAI vision hardscape measurement layer for every quote.

    IMPORTANT:
    The AI does NOT directly control the final lawn sqft.

    The AI estimates visual hardscape.
    The app then calculates the final lawn area with:

        final_lawn_sqft = parcel_sqft - GIS_building_sqft - AI_hardscape_sqft

    This keeps the numbers physically consistent and prevents impossible outputs
    where parcel/building/hardscape/lawn do not reconcile.
    """
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "Math estimate",
            "confidence": "none",
            "notes": "OpenAI API key is not configured, so the original math estimate was used.",
            "ai_building_sqft": None,
            "ai_hardscape_sqft": None,
            "adjusted_lawn_sqft": int(result.get("lawn_sqft", 0) or 0),
            "adjusted_price": int(result.get("price", 0) or 0),
        }

    try:
        lat, lon = parcel_centroid(parcel_geometry)
        image, top_left, zoom = fetch_mosaic(lat, lon)
        if image is None:
            raise RuntimeError("Could not load aerial imagery for AI measurement.")

        data_url = image_to_data_url(image)

        prompt = f"""
You are measuring hardscape for a lawn-care instant quote system.

You are given:
1. An aerial/satellite image centered on the property.
2. GIS/math estimates from the app.

Your main job is to estimate HARDscape area visually.

Definitions:
- building_sqft = house, garage, sheds, accessory buildings.
- hardscape_sqft = driveway, patio, pool, concrete, pavers, gravel, decks, walkways, paved/concrete backyard, and other non-lawn maintained surfaces.
- visual_lawn_sqft = visible maintained grass that a lawn company would likely mow.

Critical instructions:
- Do NOT simply agree with the math estimate.
- Estimate the hardscape from the image as directly as possible.
- If the backyard is mostly concrete/pool, hardscape_sqft should be high and lawn should be low.
- Do NOT count neighbouring properties.
- Do NOT count roofs/buildings as lawn.
- Do NOT count pools, patios, concrete, or driveways as lawn.
- Use the parcel size as a reality check.
- Always return numeric estimates. Do not return manual_review.

Return ONLY valid JSON with this exact shape:
{{
  "confidence": "high" or "medium" or "low",
  "estimated_building_sqft": number,
  "estimated_hardscape_sqft": number,
  "visual_lawn_sqft": number,
  "notes": "brief explanation of visible pool/patio/driveway/lawn and why you chose the hardscape number"
}}

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

Important formula used by the app after your response:
final_lawn_sqft = parcel_sqft - GIS_building_sqft - estimated_hardscape_sqft

So your estimated_hardscape_sqft is the key number.
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

        # Some model responses may include ```json fences despite the instruction.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()

        review = json.loads(text)
        confidence = review.get("confidence", "low")
        notes = review.get("notes", "")

        parcel_sqft = int(float(result.get("parcel_sqft", 0) or 0))
        gis_building_sqft = int(float(result.get("building_sqft", 0) or 0))
        original_lawn_sqft = int(result.get("lawn_sqft", 0) or 0)
        original_price = int(result.get("price", 0) or 0)

        ai_building_sqft_raw = review.get("estimated_building_sqft")
        ai_hardscape_sqft_raw = review.get("estimated_hardscape_sqft")

        if ai_hardscape_sqft_raw is None:
            raise ValueError("AI did not return estimated_hardscape_sqft.")

        ai_building_sqft = int(max(float(ai_building_sqft_raw or 0), 0))
        ai_hardscape_sqft = int(max(float(ai_hardscape_sqft_raw), 0))

        # Keep GIS building as the source of truth when available.
        # If GIS building failed, fall back to AI's visual building estimate.
        final_building_sqft = gis_building_sqft if gis_building_sqft > 0 else ai_building_sqft

        if parcel_sqft > 0:
            final_building_sqft = int(min(final_building_sqft, parcel_sqft))
            max_possible_hardscape = max(parcel_sqft - final_building_sqft, 0)
            ai_hardscape_sqft = int(min(ai_hardscape_sqft, max_possible_hardscape))

        # This is the key corrected equation:
        # final lawn = parcel - building - AI hardscape
        final_lawn_sqft = int(parcel_sqft - final_building_sqft - ai_hardscape_sqft)

        # Keep a small floor so pricing does not break if AI overestimates hardscape.
        final_lawn_sqft = max(final_lawn_sqft, 350)

        adjusted_price = quote_price_for_service(
            final_lawn_sqft,
            result["property_type"],
            result["service_type"],
            pricing_settings,
        )

        notes = (
            f"{notes} | Final lawn calculated by formula: "
            f"{parcel_sqft} parcel - {final_building_sqft} building - "
            f"{ai_hardscape_sqft} AI hardscape = {final_lawn_sqft} sqft."
        )

        return {
            "status": "AI hardscape measured",
            "confidence": confidence,
            "notes": notes,
            "ai_building_sqft": ai_building_sqft,
            "ai_hardscape_sqft": ai_hardscape_sqft,
            "adjusted_lawn_sqft": final_lawn_sqft,
            "adjusted_price": adjusted_price,
        }
    except Exception as e:
        return {
            "status": "Math estimate",
            "confidence": "error",
            "notes": f"AI hardscape measurement failed, so original math estimate was used: {e}",
            "ai_building_sqft": None,
            "ai_hardscape_sqft": None,
            "adjusted_lawn_sqft": int(result.get("lawn_sqft", 0) or 0),
            "adjusted_price": int(result.get("price", 0) or 0),
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
            "ai_building_sqft": result.get("ai_building_sqft", ""),
            "ai_hardscape_sqft": result.get("ai_hardscape_sqft", ""),
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
            "ai_building_sqft",
            "ai_hardscape_sqft",
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

pricing_settings = load_pricing_settings()


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
    result["ai_review_confidence"] = ""
    result["ai_review_notes"] = ""
    result["ai_building_sqft"] = ""
    result["ai_hardscape_sqft"] = ""

    # Experimental mode: run AI review on every quote.
    # If OPENAI_API_KEY is missing or the API fails, ai_review_property falls back to the math estimate.
    ai_review = ai_review_property(parcel_geom, result, pricing_settings)
    result["quote_status"] = ai_review.get("status", "AI reviewed")
    result["ai_review_confidence"] = ai_review.get("confidence", "")
    result["ai_review_notes"] = ai_review.get("notes", "")
    result["ai_building_sqft"] = ai_review.get("ai_building_sqft", "")
    result["ai_hardscape_sqft"] = ai_review.get("ai_hardscape_sqft", "")

    # In this test version, AI hardscape measurement overrides the measured hardscape
    # used for the final quote/export; final lawn is parcel - building - AI hardscape. The original values are still preserved in
    # original_lawn_sqft and original_price for comparison.
    if ai_review.get("ai_hardscape_sqft") is not None:
        result["hardscape_sqft"] = int(ai_review.get("ai_hardscape_sqft"))
        result["hardscape_method"] = "AI visual measurement"

    if ai_review.get("adjusted_lawn_sqft") is not None:
        result["lawn_sqft"] = int(ai_review.get("adjusted_lawn_sqft"))
    if ai_review.get("adjusted_price") is not None:
        result["price"] = int(ai_review.get("adjusted_price"))

    return result


# ----------------------------
# Sidebar: hidden admin controls
# ----------------------------
with st.sidebar:
    show_admin = st.checkbox("Show admin/export")

    if show_admin:
        st.subheader("Company settings")
        st.caption("These settings save to pricing_settings.json and apply to future leads/quotes.")

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
        st.info("AI review runs on every quote in this test version. It requires OPENAI_API_KEY in Streamlit Secrets. If the key is missing or the API fails, the app falls back to the math estimate.")

        st.subheader("Pricing settings")

        for service_name in [
            "Grass cutting",
            "Seasonal cleanup",
            "Fertilization and weed control",
        ]:
            with st.expander(service_name, expanded=False):
                current = edited_pricing[service_name]
                current["base_fee"] = st.number_input(
                    "Base fee ($)",
                    min_value=0.0,
                    value=float(current.get("base_fee", 0)),
                    step=5.0,
                    key=f"{service_name}_base_fee",
                )
                current["rate_per_1000_sqft"] = st.number_input(
                    "Rate per 1,000 sq ft ($)",
                    min_value=0.0,
                    value=float(current.get("rate_per_1000_sqft", 0)),
                    step=0.5,
                    key=f"{service_name}_rate",
                )
                current["minimum_price"] = st.number_input(
                    "Minimum price ($)",
                    min_value=0.0,
                    value=float(current.get("minimum_price", 0)),
                    step=5.0,
                    key=f"{service_name}_minimum",
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
                save_pricing_settings(edited_pricing)
                st.success("Pricing saved.")
                st.rerun()

        with col_reset:
            if st.button("Reset defaults"):
                save_pricing_settings(DEFAULT_PRICING_SETTINGS)
                st.success("Defaults restored.")
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


if st.session_state.result:
    r = st.session_state.result

    status_label = "AI hardscape estimate" if r.get("quote_status") == "AI hardscape measured" else "estimated price"
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

    leads_df = read_csv_if_exists(LEADS_CSV)
    quotes_df = read_csv_if_exists(QUOTES_CSV)

    col1, col2, col3 = st.columns(3)
    col1.metric("Quotes", len(quotes_df))
    col2.metric("Leads", len(leads_df))
    conversion = (len(leads_df) / len(quotes_df) * 100) if len(quotes_df) else 0
    col3.metric("Lead conversion", f"{conversion:.0f}%")

    st.markdown("### Leads")
    if leads_df.empty:
        st.info("No leads submitted yet.")
    else:
        st.dataframe(leads_df, use_container_width=True)
        st.download_button(
            "Download leads CSV",
            leads_df.to_csv(index=False),
            file_name="leads.csv",
            mime="text/csv",
        )

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
