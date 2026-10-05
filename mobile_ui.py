"""Small responsive style helpers for the Streamlit UI."""

import streamlit as st


def mobile_css():
    """Return mobile-only styles without changing desktop layout or app state."""
    return """
    <style>
    .st-key-mobile_auth {
        display: none;
    }

    @media (max-width: 640px) {
        [data-testid="stMainBlockContainer"] {
            max-width: 100%;
            padding: calc(3.25rem + env(safe-area-inset-top, 0px))
                     max(0.85rem, env(safe-area-inset-right, 0px))
                     calc(3rem + env(safe-area-inset-bottom, 0px))
                     max(0.85rem, env(safe-area-inset-left, 0px));
        }

        [data-testid="stMainBlockContainer"] h1 {
            font-size: 1.55rem;
            line-height: 1.35;
            max-width: 100%;
            white-space: normal;
            overflow: visible;
            overflow-wrap: anywhere;
            word-break: keep-all;
            margin-top: 0;
        }

        .st-key-mobile_auth {
            display: block;
        }

        [data-testid="stMainBlockContainer"] h2 {
            font-size: 1.3rem;
            line-height: 1.35;
        }

        [data-testid="stMainBlockContainer"] h3 {
            font-size: 1.12rem;
            line-height: 1.4;
        }

        [data-testid="stMainBlockContainer"] [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
            gap: 0.65rem;
        }

        [data-testid="stMainBlockContainer"] [data-testid="stColumn"] {
            flex: 1 1 100% !important;
            min-width: 100% !important;
            width: 100% !important;
        }

        [data-testid="stMainBlockContainer"] button {
            min-height: 2.75rem;
            white-space: normal;
        }

        [data-testid="stMainBlockContainer"] p,
        [data-testid="stMainBlockContainer"] label,
        [data-testid="stMainBlockContainer"] [data-testid="stMarkdownContainer"] {
            overflow-wrap: anywhere;
        }

        [data-testid="stMainBlockContainer"] [data-testid="stMetric"] {
            padding: 0.7rem 0.85rem;
            border: 1px solid rgba(128, 128, 128, 0.22);
            border-radius: 0.65rem;
        }

        .st-key-mobile_weekdays [data-testid="stHorizontalBlock"] {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.35rem;
        }

        .st-key-mobile_weekdays [data-testid="stColumn"] {
            min-width: 0 !important;
            width: auto !important;
        }

        .st-key-mobile_favorite_actions button {
            margin-top: 0;
        }
    }
    </style>
    """


def inject_mobile_styles():
    """Apply responsive styles once per Streamlit rerun."""
    st.markdown(mobile_css(), unsafe_allow_html=True)
