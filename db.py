import copy
import streamlit as st
from supabase import create_client


TIER_COLUMNS = {
    "Small": "small_price",
    "Standard": "standard_price",
    "Large": "large_price",
    "Complex": "complex_price",
    "Estate": "estate_price",
}


def _get_secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


@st.cache_resource
def get_supabase_client():
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")

    return create_client(url, key)


def get_company_id():
    company_id = _get_secret("COMPANY_ID")
    if not company_id:
        raise RuntimeError("Missing COMPANY_ID in Streamlit secrets.")
    return company_id


def _num(value):
    if value in [None, ""]:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_price(value, fallback):
    try:
        if value is None or value == "":
            return float(fallback)
        return float(value)
    except Exception:
        return float(fallback)


def get_pricing_settings_supabase(default_settings):
    """Load company name and tier pricing from Supabase.

    The app still passes DEFAULT_PRICING_SETTINGS so we can safely fall back for
    missing services/tiers. Supabase is the source of truth for tier prices.
    """
    supabase = get_supabase_client()
    company_id = get_company_id()
    settings = copy.deepcopy(default_settings)

    company_resp = (
        supabase.table("companies")
        .select("company_name")
        .eq("id", company_id)
        .limit(1)
        .execute()
    )
    if company_resp.data:
        settings["company_name"] = company_resp.data[0].get("company_name") or settings.get("company_name", "Lawn Company")

    pricing_resp = (
        supabase.table("pricing_tiers")
        .select("service_name, small_price, standard_price, large_price, complex_price, estate_price")
        .eq("company_id", company_id)
        .execute()
    )

    if "tier_prices" not in settings or not isinstance(settings.get("tier_prices"), dict):
        settings["tier_prices"] = {}

    for row in pricing_resp.data or []:
        service_name = row.get("service_name")
        if not service_name:
            continue

        current = settings["tier_prices"].get(service_name, {})
        settings["tier_prices"][service_name] = {
            tier_name: _safe_price(row.get(column_name), current.get(tier_name, 0))
            for tier_name, column_name in TIER_COLUMNS.items()
        }

    return settings


def save_pricing_settings_supabase(settings):
    """Save editable tier prices back to Supabase.

    This updates existing rows when they exist and inserts a row if a service has
    not been created yet. It avoids requiring a unique database constraint for
    company_id + service_name.
    """
    supabase = get_supabase_client()
    company_id = get_company_id()

    company_name = str(settings.get("company_name", "Lawn Company")).strip() or "Lawn Company"
    supabase.table("companies").update({"company_name": company_name}).eq("id", company_id).execute()

    tier_prices = settings.get("tier_prices", {})
    for service_name, tiers in tier_prices.items():
        row = {
            "company_id": company_id,
            "service_name": service_name,
            "small_price": _num(tiers.get("Small")) or 0,
            "standard_price": _num(tiers.get("Standard")) or 0,
            "large_price": _num(tiers.get("Large")) or 0,
            "complex_price": _num(tiers.get("Complex")) or 0,
            "estate_price": _num(tiers.get("Estate")) or 0,
        }

        existing = (
            supabase.table("pricing_tiers")
            .select("id")
            .eq("company_id", company_id)
            .eq("service_name", service_name)
            .limit(1)
            .execute()
        )

        if existing.data:
            supabase.table("pricing_tiers").update(row).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("pricing_tiers").insert(row).execute()

    return True


def save_quote_supabase(result):
    supabase = get_supabase_client()
    company_id = get_company_id()

    row = {
        "company_id": company_id,
        "quote_id": result.get("quote_id"),
        "address": result.get("address"),
        "property_type": result.get("property_type"),
        "service_type": result.get("service_type"),
        "parcel_sqft": _num(result.get("parcel_sqft")),
        "building_sqft": _num(result.get("building_sqft")),
        "estimated_lawn_sqft": _num(result.get("lawn_sqft")),
        "hardscape_sqft": _num(result.get("hardscape_sqft")),
        "ai_property_class": result.get("ai_property_class"),
        "ai_risk_level": result.get("ai_risk_level"),
        "base_pricing_tier": result.get("base_pricing_tier"),
        "pricing_tier": result.get("pricing_tier"),
        "price": _num(result.get("price")),
    }

    return supabase.table("quotes").insert(row).execute()


def save_lead_supabase(customer_name, customer_phone, customer_email, customer_notes, result):
    supabase = get_supabase_client()
    company_id = get_company_id()

    row = {
        "company_id": company_id,
        "quote_id": result.get("quote_id"),
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "customer_email": customer_email,
        "customer_notes": customer_notes,
        "address": result.get("address"),
        "service_type": result.get("service_type"),
        "price": _num(result.get("price")),
        "status": "New",
    }

    return supabase.table("leads").insert(row).execute()



def get_quotes_supabase(limit=250):
    """Return recent company quotes from Supabase."""
    supabase = get_supabase_client()
    company_id = get_company_id()

    resp = (
        supabase.table("quotes")
        .select("created_at, quote_id, address, property_type, service_type, parcel_sqft, building_sqft, estimated_lawn_sqft, hardscape_sqft, ai_property_class, ai_risk_level, base_pricing_tier, pricing_tier, price")
        .eq("company_id", company_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def get_leads_supabase(limit=250):
    """Return recent company leads from Supabase."""
    supabase = get_supabase_client()
    company_id = get_company_id()

    resp = (
        supabase.table("leads")
        .select("id, created_at, quote_id, customer_name, customer_phone, customer_email, customer_notes, address, service_type, price, status")
        .eq("company_id", company_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def update_lead_status_supabase(lead_id, status):
    """Update a lead status for the current company."""
    supabase = get_supabase_client()
    company_id = get_company_id()

    return (
        supabase.table("leads")
        .update({"status": status})
        .eq("id", lead_id)
        .eq("company_id", company_id)
        .execute()
    )
