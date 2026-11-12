"""Streamlit entry point. Run locally with: streamlit run streamlit_app.py

On Streamlit Community Cloud, set this file as the app's main file and put the keys from
.streamlit/secrets.toml.example in the app's Secrets.
"""

from app.ui import main

main()
