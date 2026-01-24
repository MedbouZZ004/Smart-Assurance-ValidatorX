import streamlit as st
import os, shutil
from validator import InsuranceValidator

VALID_DIR = "validated_docs"
os.makedirs(VALID_DIR, exist_ok=True)

st.set_page_config(page_title="Capgemini AI Auditor", layout="wide")
st.title("🛡️ Smart Assurance Validator")

validator = InsuranceValidator()
files = st.file_uploader("Upload Docs", accept_multiple_files=True)

if st.button("Run AI Audit"):
    for f in files:
        path = f"temp_{f.name}"
        with open(path, "wb") as tmp: tmp.write(f.getbuffer())
        
        text, struct = validator.extract_all(path)
        result = validator.validate_with_groq(text, struct)
        
        if result["is_valid"] and result["score"] > 70:
            st.success(f"✅ {f.name} - Score: {result['score']}%")
            shutil.move(path, os.path.join(VALID_DIR, f.name))
        else:
            st.error(f"❌ {f.name} - REJECTED: {result['reason']}")
            os.remove(path)