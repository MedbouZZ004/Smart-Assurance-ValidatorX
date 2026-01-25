import streamlit as st
import os
from validator import InsuranceValidator

# Configuration des dossiers
VALID_DIR = "validated_docs"
os.makedirs(VALID_DIR, exist_ok=True)

st.set_page_config(page_title="Capgemini AI Auditor", layout="wide", page_icon="🛡️")

# --- Interface Utilisateur ---
st.title("🛡️ Système d'Audit de Capital Décès")

validator = InsuranceValidator()

# Mapping the 4 required files
doc_types = {
    "contract": "📜 Contrat d'Assurance",
    "death_cert": "⚰️ Certificat de Décès",
    "id_card": "🆔 Pièce d'Identité du Bénéficiaire",
    "rib": "🏦 RIB du Bénéficiaire"
}

uploads = {}
col1, col2 = st.columns(2)

for i, (key, label) in enumerate(doc_types.items()):
    with col1 if i < 2 else col2:
        uploads[key] = st.file_uploader(f"Upload {label}", type=["pdf", "png", "jpg"], key=key)

if st.button("🚀 Lancer l'Audit Complet", type="primary"):
    if not all(uploads.values()):
        st.error("Veuillez uploader les 4 documents requis.")
    else:
        all_extracted_data = {}
        with st.spinner("Analyse croisée en cours..."):
            for key, file in uploads.items():
                path = f"temp_{file.name}"
                with open(path, "wb") as f:
                    f.write(file.getbuffer())

                text, tech = validator.extract_all(path)
                all_extracted_data[key] = {"text": text, "tech": tech}
                os.remove(path)

            # This is where the error was happening! Now it's fixed.
            final_report = validator.cross_validate_claim(all_extracted_data)

            # Display Results
            if final_report.get("is_valid"):
                st.success(f"## ✅ {final_report['verdict']} ({final_report['score']}%)")
            else:
                st.error(f"## ❌ {final_report['verdict']} ({final_report['score']}%)")

            st.info(f"**Analyse :** {final_report.get('reason')}")
            with st.expander("🔍 Détails des anomalies"):
                st.json(final_report.get("mismatches", []))