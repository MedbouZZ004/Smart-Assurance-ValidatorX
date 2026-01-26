"""
Streamlit app for Multi-Document Insurance Claim Validation
Validates individual documents and cross-validates across a batch
"""

import streamlit as st
import os
import shutil
from multi_doc_validator import MultiDocValidator

# Configuration
VALID_DIR = "validated_docs"
REJECTED_DIR = "rejected_docs"
TEMP_DIR = "temp_uploads"

os.makedirs(VALID_DIR, exist_ok=True)
os.makedirs(REJECTED_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

st.set_page_config(
    page_title="Smart Assurance Multi-Doc Validator",
    layout="wide",
    page_icon="🛡️"
)

st.title("🛡️ Smart Assurance Multi-Document Validator")
st.markdown("### Validation Intelligente Multi-Documents et Détection de Fraude")
st.info(
    "🔍 Ce système valide chaque document individuellement, puis croise les informations "
    "pour vérifier leur cohérence. Score > 50: Accepté. Score ≤ 50: Rejeté."
)

# Initialize validator
@st.cache_resource
def get_validator():
    return MultiDocValidator()

validator = get_validator()

# Main interface
st.subheader("📤 Étape 1: Téléchargez vos documents")
uploaded_files = st.file_uploader(
    "Déposez vos documents (PDF, PNG, JPG)",
    accept_multiple_files=True,
    type=["pdf", "png", "jpg", "jpeg"]
)

if st.button("🔍 Lancer la Validation Croisée", type="primary"):
    if not uploaded_files:
        st.warning("⚠️ Veuillez télécharger au moins un document.")
    else:
        # Save files temporarily
        temp_paths = []
        for f in uploaded_files:
            temp_path = os.path.join(TEMP_DIR, f.name)
            with open(temp_path, "wb") as tmp:
                tmp.write(f.getbuffer())
            temp_paths.append((temp_path, f.name))

        try:
            with st.spinner("⏳ Analyse en cours..."):
                # Process all documents
                individual_results, cross_validation = validator.process_document_batch(
                    [path for path, _ in temp_paths]
                )

            # Display Individual Validation Results
            st.divider()
            st.subheader("📋 Résultats Individuels par Document")
            
            for file_name, result in individual_results.items():
                with st.expander(f"📄 {file_name}", expanded=False):
                    col1, col2, col3 = st.columns(3)
                    
                    with col1:
                        st.metric("Type", result.get("document_type", "unknown"))
                    with col2:
                        st.metric("Confiance", f"{result.get('confidence_score', 0)}%")
                    with col3:
                        st.metric(
                            "Fraude",
                            "🚩 OUI" if result.get("fraud_detected") else "✅ NON"
                        )
                    
                    st.write("**Données Extraites:**")
                    extracted = result.get("extracted_data", {})
                    
                    # Display extracted fields in two columns
                    col_left, col_right = st.columns(2)
                    
                    with col_left:
                        st.write(f"**Nom:** {extracted.get('name', 'N/A')}")
                        st.write(f"**Prénom:** {extracted.get('first_name', 'N/A')}")
                        st.write(f"**Numéro Police:** {extracted.get('policy_number', 'N/A')}")
                        st.write(f"**Souscripteur:** {extracted.get('subscriber_name', 'N/A')}")
                    
                    with col_right:
                        st.write(f"**Date Naissance:** {extracted.get('birth_date', 'N/A')}")
                        st.write(f"**Date Effet:** {extracted.get('effective_date', 'N/A')}")
                        st.write(f"**Date Fin:** {extracted.get('end_date', 'N/A')}")
                        st.write(f"**IBAN:** {extracted.get('iban', 'N/A')}")
                    
                    st.write(f"**Complétude:** {result.get('data_completeness', 'N/A')}")
                    st.write(f"**Raison:** {result.get('reason', 'N/A')}")

            # Display Cross-Validation Results
            st.divider()
            st.subheader("🔗 Résultat de Validation Croisée")
            
            overall_score = cross_validation.get("overall_score", 0)
            is_valid = cross_validation.get("is_valid", False)
            status = cross_validation.get("cross_validation_status", "UNKNOWN")
            
            col_score, col_status, col_recommendation = st.columns(3)
            
            with col_score:
                st.metric("Score Global", overall_score)
            
            with col_status:
                status_color = "🟢" if is_valid else "🔴"
                st.metric("Statut", f"{status_color} {status}")
            
            with col_recommendation:
                rec = cross_validation.get("recommendation", "UNKNOWN")
                rec_color = "✅" if rec == "ACCEPT" else "❌" if rec == "REJECT" else "⚠️"
                st.metric("Recommandation", f"{rec_color} {rec}")
            
            # Score Breakdown with Logical Rules
            st.write("**📊 Détail du Calcul du Score:**")
            score_breakdown = cross_validation.get("score_breakdown", {})
            if score_breakdown:
                col_breakdown1, col_breakdown2 = st.columns(2)
                with col_breakdown1:
                    st.metric("Score de Base", score_breakdown.get("base_score", 100))
                with col_breakdown2:
                    st.metric("Score Final", score_breakdown.get("final_score", 0))
                
                deductions = score_breakdown.get("deductions", [])
                if deductions:
                    st.write("**Déductions appliquées:**")
                    for deduction in deductions:
                        st.write(f"- {deduction}")
            
            # Detailed Analysis
            st.write("**Analyse Détaillée:**")
            
            col_names, col_dates = st.columns(2)
            
            with col_names:
                st.write("**Correspondance des Noms:**")
                name_matches = cross_validation.get("name_matches", {})
                if name_matches.get("deceased_vs_subscriber") is not None:
                    st.write(f"- Décédé ↔️ Souscripteur: {'✅' if name_matches.get('deceased_vs_subscriber') else '❌'}")
                if name_matches.get("beneficiary_vs_account_holder") is not None:
                    st.write(f"- Bénéficiaire ↔️ Titulaire: {'✅' if name_matches.get('beneficiary_vs_account_holder') else '❌'}")
                if name_matches.get("mismatches_found"):
                    st.write("**Incohérences détectées:**")
                    for mismatch in name_matches.get("mismatches_found", []):
                        st.write(f"- {mismatch}")
            
            with col_dates:
                st.write("**Logique des Dates:**")
                date_logic = cross_validation.get("date_logic_valid", False)
                st.write(f"Dates cohérentes: {'✅ OUI' if date_logic else '❌ NON'}")
                if cross_validation.get("date_issues"):
                    for issue in cross_validation.get("date_issues", []):
                        st.write(f"- {issue}")
            
            # Critical Documents and Fields
            col_docs, col_fields = st.columns(2)
            with col_docs:
                st.write("**Documents Critiques:**")
                if cross_validation.get("critical_documents_present"):
                    st.success("✅ Tous les documents critiques présents")
                else:
                    missing = cross_validation.get("missing_documents", [])
                    st.error(f"❌ Documents manquants: {', '.join(missing) if missing else 'Unknown'}")
            
            with col_fields:
                st.write("**Champs Critiques:**")
                missing_fields = cross_validation.get("missing_fields", [])
                if missing_fields:
                    st.error(f"❌ Champs manquants: {len(missing_fields)}")
                    for field in missing_fields:
                        st.write(f"- {field}")
                else:
                    st.success("✅ Tous les champs critiques présents")
            
            # Fraud and Discrepancies
            if cross_validation.get("fraud_indicators"):
                st.warning("🚩 **Indicateurs de Fraude Détectés:**")
                for fraud in cross_validation.get("fraud_indicators", []):
                    st.write(f"- {fraud}")
            
            if cross_validation.get("low_confidence_documents"):
                st.warning("⚠️ **Documents avec Faible Confiance:**")
                for doc in cross_validation.get("low_confidence_documents", []):
                    st.write(f"- {doc}")
            
            if cross_validation.get("discrepancies"):
                st.error("⚠️ **Incohérences Détectées:**")
                for disc in cross_validation.get("discrepancies", []):
                    st.write(f"- {disc}")
            
            st.write("**Explication Détaillée:**")
            st.info(cross_validation.get("detailed_reason", "No explanation provided"))

            # File Storage Decision
            st.divider()
            st.subheader("💾 Stockage des Documents")
            
            if overall_score > 50:
                st.success(f"✅ **ACCEPTÉ** (Score: {overall_score}%)")
                st.write("Les documents vont être archivés dans `validated_docs/`")
                
                for temp_path, file_name in temp_paths:
                    dest_path = os.path.join(VALID_DIR, file_name)
                    shutil.copy(temp_path, dest_path)
                
                st.balloons()
            else:
                st.error(f"❌ **REJETÉ** (Score: {overall_score}%)")
                st.write("Les documents vont être archivés dans `rejected_docs/` pour révision.")
                
                for temp_path, file_name in temp_paths:
                    dest_path = os.path.join(REJECTED_DIR, file_name)
                    shutil.copy(temp_path, dest_path)
            
            st.write(f"📁 Documents validés: **{len(os.listdir(VALID_DIR))}**")
            st.write(f"📁 Documents rejetés: **{len(os.listdir(REJECTED_DIR))}**")

        finally:
            # Cleanup temp files
            for temp_path, _ in temp_paths:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

# Sidebar Dashboard
st.sidebar.title("📊 Tableau de Bord")
st.sidebar.markdown("---")

valid_count = len(os.listdir(VALID_DIR)) if os.path.exists(VALID_DIR) else 0
rejected_count = len(os.listdir(REJECTED_DIR)) if os.path.exists(REJECTED_DIR) else 0

col1, col2 = st.sidebar.columns(2)
with col1:
    st.metric("✅ Acceptés", valid_count)
with col2:
    st.metric("❌ Rejetés", rejected_count)

st.sidebar.markdown("---")
st.sidebar.subheader("🗂️ Gestion des Fichiers")

if st.sidebar.button("🗑️ Vider dossier Validé"):
    for filename in os.listdir(VALID_DIR):
        file_path = os.path.join(VALID_DIR, filename)
        if os.path.isfile(file_path):
            os.remove(file_path)
    st.rerun()

if st.sidebar.button("🗑️ Vider dossier Rejeté"):
    for filename in os.listdir(REJECTED_DIR):
        file_path = os.path.join(REJECTED_DIR, filename)
        if os.path.isfile(file_path):
            os.remove(file_path)
    st.rerun()

# File browser
st.sidebar.markdown("---")
st.sidebar.subheader("📂 Fichiers Validés")
if valid_count > 0:
    for file in os.listdir(VALID_DIR):
        st.sidebar.write(f"✅ {file}")

st.sidebar.subheader("📂 Fichiers Rejetés")
if rejected_count > 0:
    for file in os.listdir(REJECTED_DIR):
        st.sidebar.write(f"❌ {file}")
