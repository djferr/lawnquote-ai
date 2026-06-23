import copy
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from db import (
    get_pricing_settings_supabase,
    save_pricing_settings_supabase,
    get_quotes_supabase,
    get_leads_supabase,
    update_lead_status_supabase,
)

st.set_page_config(
    page_title="LawnQuote Admin",
    page_icon="🌱",
    layout="wide",
)

PRICING_SETTINGS_JSON = Path("pricing_settings.json")

DEFAULT_PRICING_SETTINGS = {
    "company_name": "Lawn Company",
    "lead_notification_email": "",
    "enable_ai_review": True,
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
    "Residential": {"multiplier": 1.0},
    "Commercial": {"multiplier": 1.25},
}


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


def load_local_pricing_settings():
    if not PRICING_SETTINGS_JSON.exists():
        return copy.deepcopy(DEFAULT_PRICING_SETTINGS)
    try:
        with PRICING_SETTINGS_JSON.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        return deep_merge_pricing(DEFAULT_PRICING_SETTINGS, loaded)
    except Exception:
        return copy.deepcopy(DEFAULT_PRICING_SETTINGS)


def save_local_pricing_settings(settings):
    with PRICING_SETTINGS_JSON.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def get_admin_pricing_settings():
    local = load_local_pricing_settings()
    try:
        settings = get_pricing_settings_supabase(DEFAULT_PRICING_SETTINGS)
        # Keep local-only fields like lead_notification_email if your Supabase company table does not store them yet.
        settings["lead_notification_email"] = local.get("lead_notification_email", settings.get("lead_notification_email", ""))
        settings["Residential"] = local.get("Residential", settings.get("Residential", {"multiplier": 1.0}))
        settings["Commercial"] = local.get("Commercial", settings.get("Commercial", {"multiplier": 1.25}))
        return settings, "Supabase"
    except Exception as e:
        st.warning(f"Using local pricing fallback because Supabase pricing failed: {e}")
        return local, "Local JSON fallback"


st.title("LawnQuote Admin")
st.caption("Manage leads, quotes, pricing, and company settings.")

pricing_settings, pricing_source = get_admin_pricing_settings()

try:
    leads_df = pd.DataFrame(get_leads_supabase())
except Exception as e:
    leads_df = pd.DataFrame()
    st.warning(f"Could not load leads from Supabase: {e}")

try:
    quotes_df = pd.DataFrame(get_quotes_supabase())
except Exception as e:
    quotes_df = pd.DataFrame()
    st.warning(f"Could not load quotes from Supabase: {e}")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Quotes", len(quotes_df))
col2.metric("Leads", len(leads_df))
conversion = (len(leads_df) / len(quotes_df) * 100) if len(quotes_df) else 0
col3.metric("Lead Conversion", f"{conversion:.0f}%")
if not quotes_df.empty and "price" in quotes_df.columns:
    col4.metric("Avg Quote", f"${quotes_df['price'].dropna().astype(float).mean():.0f}")
else:
    col4.metric("Avg Quote", "$0")

st.divider()

tabs = st.tabs(["Leads", "Quotes", "Pricing", "Settings"])

with tabs[0]:
    st.subheader("Captured Leads")
    if leads_df.empty:
        st.info("No leads submitted yet.")
    else:
        status_filter = st.selectbox(
            "Filter by status",
            ["All"] + sorted([str(x) for x in leads_df.get("status", pd.Series(dtype=str)).dropna().unique()]),
        )
        display_leads = leads_df.copy()
        if status_filter != "All" and "status" in display_leads.columns:
            display_leads = display_leads[display_leads["status"] == status_filter]

        visible = display_leads.drop(columns=["id"], errors="ignore")
        st.dataframe(visible, use_container_width=True)

        if "id" in leads_df.columns:
            with st.expander("Update lead status"):
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
    st.subheader("Quote History")
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
    st.subheader("Pricing")
    st.caption(f"Pricing source: {pricing_source}")

    edited_pricing = copy.deepcopy(pricing_settings)
    if "tier_prices" not in edited_pricing or not isinstance(edited_pricing.get("tier_prices"), dict):
        edited_pricing["tier_prices"] = copy.deepcopy(DEFAULT_PRICING_SETTINGS["tier_prices"])

    for service_name in ["Grass cutting", "Seasonal cleanup", "Fertilization and weed control"]:
        if service_name not in edited_pricing["tier_prices"]:
            edited_pricing["tier_prices"][service_name] = copy.deepcopy(DEFAULT_PRICING_SETTINGS["tier_prices"][service_name])

        with st.expander(f"{service_name} tier prices", expanded=(service_name == "Grass cutting")):
            cols = st.columns(5)
            for idx, tier_name in enumerate(["Small", "Standard", "Large", "Complex", "Estate"]):
                with cols[idx]:
                    edited_pricing["tier_prices"][service_name][tier_name] = st.number_input(
                        tier_name,
                        min_value=0.0,
                        value=float(edited_pricing["tier_prices"][service_name].get(tier_name, 0)),
                        step=5.0,
                        key=f"admin_{service_name}_{tier_name}_tier_price",
                    )

    col_save, col_reset = st.columns(2)
    with col_save:
        if st.button("Save pricing to Supabase", type="primary"):
            try:
                save_pricing_settings_supabase(edited_pricing)
                save_local_pricing_settings(edited_pricing)
                st.success("Pricing saved.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save pricing: {e}")

    with col_reset:
        if st.button("Reset default pricing"):
            try:
                save_pricing_settings_supabase(DEFAULT_PRICING_SETTINGS)
                save_local_pricing_settings(DEFAULT_PRICING_SETTINGS)
                st.success("Defaults restored.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not reset pricing: {e}")

with tabs[3]:
    st.subheader("Company Settings")
    st.caption("Basic company settings. Pricing saves to Supabase; some notification settings remain local until the company table is expanded.")

    edited_settings = copy.deepcopy(pricing_settings)
    edited_settings["company_name"] = st.text_input(
        "Company name",
        value=str(edited_settings.get("company_name", "Lawn Company")),
    )
    edited_settings["lead_notification_email"] = st.text_input(
        "Lead notification email",
        value=str(edited_settings.get("lead_notification_email", "")),
    )

    with st.expander("Property multipliers"):
        edited_settings.setdefault("Residential", {"multiplier": 1.0})
        edited_settings.setdefault("Commercial", {"multiplier": 1.25})
        edited_settings["Residential"]["multiplier"] = st.number_input(
            "Residential multiplier",
            min_value=0.0,
            value=float(edited_settings["Residential"].get("multiplier", 1.0)),
            step=0.05,
            key="settings_residential_multiplier",
        )
        edited_settings["Commercial"]["multiplier"] = st.number_input(
            "Commercial multiplier",
            min_value=0.0,
            value=float(edited_settings["Commercial"].get("multiplier", 1.25)),
            step=0.05,
            key="settings_commercial_multiplier",
        )

    if st.button("Save company settings", type="primary"):
        # save_pricing_settings_supabase updates company_name and tier prices.
        # Local JSON backup preserves lead email and multipliers for the current Streamlit version.
        merged = copy.deepcopy(pricing_settings)
        merged.update({
            "company_name": edited_settings.get("company_name", "Lawn Company"),
            "lead_notification_email": edited_settings.get("lead_notification_email", ""),
            "Residential": edited_settings.get("Residential", {"multiplier": 1.0}),
            "Commercial": edited_settings.get("Commercial", {"multiplier": 1.25}),
        })
        try:
            save_pricing_settings_supabase(merged)
            save_local_pricing_settings(merged)
            st.success("Company settings saved.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not save company settings: {e}")
