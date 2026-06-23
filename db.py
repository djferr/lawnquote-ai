from supabase import create_client
import streamlit as st


def _get_secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


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
