import streamlit as st
import os, shutil
from validator import InsuranceValidator

# Configuration des dossiers
VALID_DIR = "validated_docs"
os.makedirs(VALID_DIR, exist_ok=True)

st.set_page_config(page_title="Capgemini AI Auditor", layout="wide", page_icon="🛡️")

# --- Interface Utilisateur ---
st.title("🛡️ Capgemini Smart Assurance Validator")
st.markdown("### Système d'Audit Intelligent et Détection de Fraude")
st.info("Ce système vérifie la conformité technique, temporelle et légale des documents d'assurance.")

# Initialisation du validateur
@st.cache_resource # Pour éviter de recharger le modèle OCR à chaque clic
def get_validator():
    return InsuranceValidator()

validator = get_validator()

# Upload des documents
uploaded_files = st.file_uploader("Déposez vos documents ici (PDF, PNG, JPG)", accept_multiple_files=True)

if st.button("Lancer l'Audit IA", type="primary"):
    if not uploaded_files:
        st.warning("Veuillez uploader au moins un document.")
    else:
        for f in uploaded_files:
            # Sauvegarde temporaire
            path = f"temp_{f.name}"
            with open(path, "wb") as tmp:
                tmp.write(f.getbuffer())
            
            try:
                with st.spinner(f"Analyse en cours : {f.name}..."):
                    # 1. Extraction et Analyse technique
                    text, struct, tech_report = validator.extract_all(path)
                    
                    # 2. Validation Groq (IA)
                    result = validator.validate_with_groq(text, struct, tech_report)
                    
                    # --- Affichage des Résultats ---
                    st.divider()
                    col_status, col_details = st.columns([1, 2])
                    
                    # CORRECTION LOGIQUE : On vérifie si is_valid est VRAIMENT True
                    # (Gestion des cas où l'IA renvoie un string "true" au lieu d'un booléen)
                    is_valid_bool = str(result.get("is_valid")).lower() == "true"
                    score = result.get("score", 0)

                    with col_status:
                        # On accepte si is_valid est vrai ET que le score est suffisant
                        if is_valid_bool and score > 70:
                            st.success(f"### ✅ ACCEPTÉ\n**Score : {score}%**")
                            dest_path = os.path.join(VALID_DIR, f.name)
                            shutil.copy(path, dest_path) # Utilise copy puis remove pour éviter les erreurs de permission
                        else:
                            st.error(f"### ❌ REJETÉ\n**Score : {score}%**")
                        
                        # Alerte Fraude
                        if tech_report.get("potential_tampering"):
                            st.warning(f"🚩 **ALERTE FRAUDE**\nOutil détecté : {tech_report.get('editor_detected', 'Inconnu')}")

                    with col_details:
                        st.subheader(f"📄 Analyse : {f.name}")
                        st.write(f"**Verdict :** `{result.get('verdict_technique', 'N/A')}`")
                        
                        # Affichage des infos pays et type
                        country = result.get('country', 'Non détecté')
                        doc_type = result.get('doc_type', 'Non identifié')
                        if country != 'Non détecté':
                            st.caption(f"📍 **Pays détecté :** {country.upper()} | 📑 **Type :** {doc_type}")

                        # Affichage des données métier extraites
                        extracted = result.get("extracted_data", {})
                        dates = result.get("dates_extracted", {})
                        
                        col_a, col_b = st.columns(2)
                        with col_a:
                            st.info(f"**Assureur :** {extracted.get('insurer', 'N/A')}")
                            st.write(f"**Police :** {extracted.get('policy_number', 'N/A')}")
                        with col_b:
                            st.warning(f"📅 **Validité :** {dates.get('start_date', '?')}  ➜  {dates.get('end_date', '?')}")
                            st.write(f"**Client/Véhicule :** {extracted.get('client_or_vehicle', 'N/A')}")

                        with st.expander("🔍 Détails & Preuves Technique", expanded=True):
                            st.write(f"**Raison :** {result.get('reason', 'Aucune explication logicielle.')}")
                            
                            st.json({
                                "metadata_suspicious": tech_report.get("suspicious_metadata"),
                                "fonts_detected_count": tech_report.get("font_count"),
                                "ocr_tables_found": struct.get("has_tables"),
                                "page_count": struct.get("page_count")
                            })
            
            finally:
                # Nettoyage systématique du fichier temporaire
                if os.path.exists(path):
                    os.remove(path)

# Barre latérale pour le suivi
st.sidebar.title("Tableau de bord")
st.sidebar.write(f"📁 **Documents validés :** {len(os.listdir(VALID_DIR))}")
if st.sidebar.button("Vider le dossier validé"):
    for filename in os.listdir(VALID_DIR):
        os.remove(os.path.join(VALID_DIR, filename))
    st.rerun()