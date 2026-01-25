import streamlit as st
import os, shutil, json, hashlib, logging, re
from datetime import datetime
from validator import InsuranceValidator
import sqlite3

from security import initialize_security, sanitize_dict, mask_value

# -----------------------------
# Logging
# -----------------------------
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename="logs/audit.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------
# Dirs
# -----------------------------
VALID_DIR = "validated_docs"
REVIEW_DIR = "review_needed"
INVALID_DIR = "invalid_docs"  # manual only
TMP_DIR = "uploads_tmp"

for d in [VALID_DIR, REVIEW_DIR, INVALID_DIR, TMP_DIR]:
    os.makedirs(d, exist_ok=True)

# -----------------------------
# Security
# -----------------------------
sec = initialize_security()
audit_logger = sec["audit"]
fingerprints = sec["fingerprints"]

# -----------------------------
# Audit DB (NO sensitive fields)
# -----------------------------
def init_audit_db():
    conn = sqlite3.connect("audit_trail.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS dossiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT,
            file_name TEXT,
            file_hash TEXT,
            timestamp TEXT,
            score INTEGER,
            decision TEXT,
            fraud_suspected BOOLEAN,
            doc_type TEXT,
            reason_short TEXT
        )
    """)
    conn.commit()
    conn.close()

init_audit_db()

def compute_file_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    return sha256.hexdigest()

def to_safe_reason(reason: str, max_len: int = 180) -> str:
    r = (reason or "").replace("\n", " ").strip()
    if len(r) > max_len:
        r = r[:max_len] + "..."
    return r

def save_to_audit_db(case_id, file_name, file_hash, score, decision, fraud_suspected, doc_type, reason_short):
    conn = sqlite3.connect("audit_trail.db")
    c = conn.cursor()
    timestamp = datetime.now().isoformat()
    c.execute("""
        INSERT INTO dossiers (case_id, file_name, file_hash, timestamp, score, decision, fraud_suspected, doc_type, reason_short)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (case_id, file_name, file_hash, timestamp, int(score), decision, int(bool(fraud_suspected)), doc_type, reason_short))
    conn.commit()
    conn.close()

# -----------------------------
# Helpers
# -----------------------------
def safe_get_bank_fields(extracted: dict) -> tuple[str, str]:
    rib = (extracted.get("bank_rib") or "").strip()
    iban = (extracted.get("bank_iban") or "").strip()

    # backward compatibility (old key)
    if not rib and not iban:
        legacy = (extracted.get("beneficiary_rib") or "").strip()
        if legacy.upper().startswith("MA"):
            iban = legacy
        else:
            rib = legacy

    return rib or "N/A", iban or "N/A"

def normalize_doc_type(s: str) -> str:
    return (s or "").strip().lower()

def name_norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def fuzzy_ratio(a: str, b: str) -> float:
    a = name_norm(a)
    b = name_norm(b)
    if not a or not b:
        return 0.0
    ta = set(re.findall(r"[a-zàâçéèêëîïôùûüÿñ0-9]+", a))
    tb = set(re.findall(r"[a-zàâçéèêëîïôùûüÿñ0-9]+", b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))

def classify_doc_category(doc_type: str, extracted: dict, ocr_text: str) -> set:
    """
    Classification par OCR (robuste) + fallback doc_type.
    On veut 4 catégories obligatoires: ID, BANK, DEATH, LIFE_CONTRACT
    """
    dt = normalize_doc_type(doc_type)
    t = (ocr_text or "").lower()
    ex = extracted or {}
    cats = set()

    # BANK
    rib, iban = safe_get_bank_fields(ex)
    bank_kw = [
        "relevé d'identité bancaire", "releve d'identite bancaire", "rib", "iban",
        "bic", "swift", "banque", "bank", "agence", "titulaire", "account holder"
    ]
    if (rib != "N/A" or iban != "N/A") or any(k in t for k in bank_kw) or any(k in dt for k in bank_kw):
        cats.add("BANK")

    # DEATH
    death_kw = [
        "certificat de décès", "certificat de deces", "acte de décès", "acte de deces",
        "certificate of death", "date of death", "place where death occurred",
        "registration no", "registrar"
    ]
    if any(k in t for k in death_kw) or any(k in dt for k in ["deces", "décès", "death"]):
        cats.add("DEATH")

    # LIFE_CONTRACT
    contract_kw = [
        "assurance épargne-vie", "assurance epargne-vie", "assurance vie", "life insurance",
        "numéro de police", "numero de police", "policy number", "policy no",
        "capital assuré", "capital assure", "clause bénéficiaire", "beneficiaire",
        "date d’effet", "date d'effet", "contract effective"
    ]
    if any(k in t for k in contract_kw) or any(k in dt for k in ["assurance", "contrat", "police", "epargne", "épargne", "life"]):
        cats.add("LIFE_CONTRACT")

    # ID
    id_kw = [
        "carte nationale", "cnie", "cni", "cin", "passport", "passeport", "identity",
        "date d'expiration", "date expiration", "numéro", "numero", "document"
    ]
    if any(k in t for k in id_kw) or any(k in dt for k in ["cni", "cnie", "cin", "passport", "passeport", "identity", "id"]):
        cats.add("ID")

    return cats

def pick_best_doc(doc_results: list[dict], category: str) -> dict | None:
    best, best_score = None, -1
    for d in doc_results:
        r = d.get("result", {}) or {}
        ex = r.get("extracted_data", {}) or {}
        cats = classify_doc_category(r.get("doc_type", ""), ex, d.get("ocr_text", ""))
        if category in cats:
            sc = int(r.get("score", 0) or 0)
            if sc > best_score:
                best_score = sc
                best = d
    return best

# -----------------------------
# Required documents + fields
# -----------------------------
REQUIRED_DOC_LABELS = {
    "ID": "Pièce d'identité (CNI/Passeport)",
    "BANK": "RIB/IBAN",
    "DEATH": "Certificat de décès",
    "LIFE_CONTRACT": "Contrat assurance épargne-vie",
}

def required_docs_missing(doc_results: list[dict]) -> list[str]:
    present = {k: False for k in REQUIRED_DOC_LABELS.keys()}
    for d in doc_results:
        r = d.get("result", {}) or {}
        ex = r.get("extracted_data", {}) or {}
        cats = classify_doc_category(r.get("doc_type", ""), ex, d.get("ocr_text", ""))
        for c in cats:
            if c in present:
                present[c] = True
    return [REQUIRED_DOC_LABELS[k] for k, v in present.items() if not v]

def missing_fields_for_doc(category: str, extracted: dict) -> list[str]:
    """
    IMPORTANT:
    - Ne jamais exiger CIN dans BANK
    - Ne jamais exiger IBAN/RIB dans LIFE_CONTRACT
    - Chaque doc a ses propres champs
    """
    ex = extracted or {}
    missing = []

    if category == "ID":
        required = ["beneficiary_name", "beneficiary_cin", "beneficiary_birth_date", "id_document_number", "id_expiry_date"]
        for k in required:
            if not (ex.get(k) or "").strip():
                missing.append(k)

    elif category == "BANK":
        rib, iban = safe_get_bank_fields(ex)
        if rib == "N/A" and iban == "N/A":
            missing.append("bank_rib_or_iban")
        for k in ["bank_account_holder", "bank_bic", "bank_name"]:
            if not (ex.get(k) or "").strip():
                missing.append(k)

    elif category == "DEATH":
        required = ["deceased_name", "death_date", "death_place", "death_act_number"]
        for k in required:
            if not (ex.get(k) or "").strip():
                missing.append(k)

    elif category == "LIFE_CONTRACT":
        required = ["policy_number", "subscriber_name", "beneficiary_name", "contract_effective_date"]
        for k in required:
            if not (ex.get(k) or "").strip():
                missing.append(k)
        capital = (ex.get("capital") or "").strip() or (ex.get("amount") or "").strip() or (ex.get("contract_capital") or "").strip()
        if not capital:
            missing.append("capital")

    return missing

def per_document_validation(doc_results: list[dict]) -> tuple[bool, list[str]]:
    """
    Vérifie 4 documents par catégorie:
    - On prend le meilleur doc de chaque catégorie
    - S'il manque un champ obligatoire dans un doc => dossier REVIEW
    """
    issues = []
    for cat in REQUIRED_DOC_LABELS.keys():
        d = pick_best_doc(doc_results, cat)
        if not d:
            continue
        ex = (d["result"].get("extracted_data") or {})
        miss = missing_fields_for_doc(cat, ex)
        if miss:
            issues.append(f"{REQUIRED_DOC_LABELS[cat]}: champs manquants -> {', '.join(miss)}")
    return (len(issues) == 0), issues

def cross_document_validation(doc_results: list[dict]) -> tuple[bool, list[str]]:
    """
    Comparaisons autorisées:
    1) ID <-> LIFE_CONTRACT : beneficiary_name + CIN
    2) LIFE_CONTRACT <-> DEATH : subscriber_name <-> deceased_name
    3) ID <-> BANK : beneficiary_name <-> bank_account_holder (pas de CIN ici)
    """
    issues = []

    id_doc = pick_best_doc(doc_results, "ID")
    bank_doc = pick_best_doc(doc_results, "BANK")
    death_doc = pick_best_doc(doc_results, "DEATH")
    contract_doc = pick_best_doc(doc_results, "LIFE_CONTRACT")

    if not (id_doc and bank_doc and death_doc and contract_doc):
        return False, ["Documents insuffisants pour comparaison inter-documents."]

    id_ex = id_doc["result"].get("extracted_data", {}) or {}
    bank_ex = bank_doc["result"].get("extracted_data", {}) or {}
    death_ex = death_doc["result"].get("extracted_data", {}) or {}
    ct_ex = contract_doc["result"].get("extracted_data", {}) or {}

    # 1) ID <-> Contract (beneficiary)
    id_name = (id_ex.get("beneficiary_name") or "").strip()
    id_cin = (id_ex.get("beneficiary_cin") or "").strip().upper()

    ct_name = (ct_ex.get("beneficiary_name") or "").strip()
    ct_cin = (ct_ex.get("beneficiary_cin") or "").strip().upper()

    name_score = fuzzy_ratio(id_name, ct_name)
    cin_match = bool(id_cin and ct_cin and id_cin == ct_cin)

    if not cin_match:
        issues.append("CIN bénéficiaire incohérent entre CNI et contrat.")
    if name_score < 0.70:
        issues.append("Nom bénéficiaire incohérent entre CNI et contrat.")

    # 2) Contract <-> Death (deceased/subscriber)
    sub = (ct_ex.get("subscriber_name") or "").strip()
    dec = (death_ex.get("deceased_name") or "").strip()
    if fuzzy_ratio(sub, dec) < 0.70:
        issues.append("Assuré (contrat) ≠ Défunt (certificat de décès).")

    # 3) ID <-> Bank (holder vs beneficiary_name)
    holder = (bank_ex.get("bank_account_holder") or "").strip()
    if fuzzy_ratio(id_name, holder) < 0.70:
        issues.append("Titulaire bancaire (RIB) ≠ Bénéficiaire (CNI).")

    return (len(issues) == 0), issues

def compute_case_decision(doc_results: list[dict]) -> tuple[str, str]:
    if not doc_results:
        return "REVIEW", "Aucun document analysé."

    # Step A: required 4 docs
    missing_docs = required_docs_missing(doc_results)
    if missing_docs:
        return "REVIEW", "Document(s) manquant(s): " + ", ".join(missing_docs)

    # Step B: per-document required fields
    ok_docs, issues = per_document_validation(doc_results)
    if not ok_docs:
        return "REVIEW", "Erreur par document: " + " | ".join(issues)

    # Step C: inter-document comparisons
    ok_cross, cross_issues = cross_document_validation(doc_results)
    if not ok_cross:
        return "REVIEW", "Incohérences dossier: " + " | ".join(cross_issues)

    # Step D: any fraud/technical signal => review
    if any(bool(d["result"].get("fraud_suspected")) for d in doc_results):
        return "REVIEW", "Signaux techniques détectés (contrôle humain requis)."

    # Step E: strict rule -> if any doc decision REVIEW => dossier REVIEW
    if any((d["result"].get("decision") != "ACCEPT") for d in doc_results):
        return "REVIEW", "Au moins un document est en REVIEW."

    return "ACCEPT", "Dossier complet, documents conformes et cohérents."

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Smart Assurance Validator X", layout="wide", page_icon="🛡️")
st.title("🛡️ Smart Assurance Validator X")
st.caption("Pré-tri dossier épargne-vie / succession (Maroc). Jamais de rejet automatique.")

@st.cache_resource
def get_validator():
    return InsuranceValidator()

validator = get_validator()

st.subheader("1) Upload dossier (4 documents obligatoires)")
uploaded_files = st.file_uploader(
    "Ajoute: CNI/Passeport + RIB/IBAN + Certificat de décès + Contrat épargne-vie",
    accept_multiple_files=True,
    type=["pdf", "png", "jpg", "jpeg"]
)

colA, colB = st.columns([1, 1])
with colA:
    avoid_duplicates = st.checkbox("Éviter les doublons (cache)", value=True)
with colB:
    show_tech = st.checkbox("Afficher détails techniques", value=False)

st.divider()
st.subheader("2) Analyse dossier")

if st.button("Lancer l'audit IA (dossier)", type="primary"):
    if not uploaded_files:
        st.warning("Upload au moins un document.")
        st.stop()

    case_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = os.path.join(TMP_DIR, case_id)
    os.makedirs(temp_dir, exist_ok=True)

    doc_results = []
    skipped, errors = [], []

    for f in uploaded_files:
        if f.size > 50 * 1024 * 1024:
            skipped.append((f.name, "Trop volumineux (>50MB)"))
            continue

        local_path = os.path.join(temp_dir, f.name)
        with open(local_path, "wb") as tmp:
            tmp.write(f.getbuffer())

        try:
            if avoid_duplicates:
                is_dup, prev = fingerprints.is_duplicate(local_path)
                if is_dup:
                    skipped.append((f.name, f"Doublon (déjà vu: {prev})"))
                    continue

            file_hash = compute_file_hash(local_path)

            with st.spinner(f"Analyse : {f.name}"):
                text, struct, tech_report = validator.extract_all(local_path)
                result = validator.validate_with_groq(text, struct, tech_report)

            ocr_snippet = (text or "")[:8000]

            decision = (result.get("decision") or "REVIEW").upper()
            if decision not in ["ACCEPT", "REVIEW"]:
                decision = "REVIEW"

            score = int(result.get("score", 0) or 0)
            doc_type = result.get("doc_type", "UNKNOWN")
            fraud_suspected = bool(result.get("fraud_suspected", False))
            reason = result.get("reason", "Pas d'explication.")

            # technical hard rule => REVIEW
            if tech_report.get("potential_tampering"):
                fraud_suspected = True
                decision = "REVIEW"
                reason = (reason + " | Signal technique: " + str(tech_report.get("editor_detected", ""))).strip(" |")

            fingerprints.register_fingerprint(local_path, decision=decision, score=score)

            result["decision"] = decision
            result["score"] = score
            result["doc_type"] = doc_type
            result["fraud_suspected"] = fraud_suspected
            result["reason"] = reason

            wrapped = {
                "file_name": f.name,
                "file_hash": file_hash,
                "local_path": local_path,
                "result": result,
                "structure": struct,
                "tech_report": tech_report,
                "ocr_text": ocr_snippet,
            }
            doc_results.append(wrapped)

            audit_logger.log_decision(
                case_id=case_id,
                file_name=f.name,
                file_hash=file_hash,
                score=score,
                decision=decision,
                fraud_suspected=fraud_suspected,
                doc_type=doc_type,
                extracted_fields=result.get("extracted_data", {}),
                reason=to_safe_reason(reason)
            )

            save_to_audit_db(
                case_id=case_id,
                file_name=f.name,
                file_hash=file_hash,
                score=score,
                decision=decision,
                fraud_suspected=fraud_suspected,
                doc_type=doc_type,
                reason_short=to_safe_reason(reason)
            )

        except Exception as e:
            errors.append((f.name, str(e)))
            logger.error(f"Erreur traitement {f.name}: {str(e)}")

    if not doc_results:
        st.warning("Aucun document analysé.")
        st.stop()

    # -----------------------------
    # DOSSIER DECISION
    # -----------------------------
    case_decision, case_reason = compute_case_decision(doc_results)

    dest_root = VALID_DIR if case_decision == "ACCEPT" else REVIEW_DIR
    case_dir = os.path.join(dest_root, case_id)
    os.makedirs(case_dir, exist_ok=True)

    for d in doc_results:
        shutil.copy(d["local_path"], os.path.join(case_dir, d["file_name"]))

    report = {
        "case_id": case_id,
        "case_decision": case_decision,
        "case_reason": case_reason,
        "timestamp": datetime.now().isoformat(),
        "skipped": [{"file": n, "why": w} for n, w in skipped],
        "errors": [{"file": n, "error": m} for n, m in errors],
        "documents": []
    }

    for d in doc_results:
        r = d["result"]
        ex = r.get("extracted_data", {}) or {}
        rib, iban = safe_get_bank_fields(ex)
        cats = classify_doc_category(r.get("doc_type", ""), ex, d.get("ocr_text", ""))

        report["documents"].append({
            "file_name": d["file_name"],
            "doc_type": r.get("doc_type", "UNKNOWN"),
            "categories": sorted(list(cats)),
            "decision": r.get("decision", "REVIEW"),
            "score": r.get("score", 0),
            "fraud_suspected": bool(r.get("fraud_suspected", False)),
            "reason_short": to_safe_reason(r.get("reason", "")),
            "extracted_preview": sanitize_dict({
                "beneficiary_name": ex.get("beneficiary_name", "N/A"),
                "beneficiary_cin": ex.get("beneficiary_cin", "N/A"),
                "subscriber_name": ex.get("subscriber_name", "N/A"),
                "deceased_name": ex.get("deceased_name", "N/A"),
                "policy_number": ex.get("policy_number", "N/A"),
                "bank_account_holder": ex.get("bank_account_holder", "N/A"),
                "bank_rib": rib,
                "bank_iban": iban,
                "bank_bic": ex.get("bank_bic", "N/A"),
                "bank_name": ex.get("bank_name", "N/A"),
            })
        })

    with open(os.path.join(case_dir, "report.json"), "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    st.divider()
    st.subheader("3) Résultat")

    if case_decision == "ACCEPT":
        st.success(f"✅ PRÉ-TRI: ACCEPT — Case ID: {case_id}")
    else:
        st.warning(f"⚠️ PRÉ-TRI: REVIEW — Case ID: {case_id}")

    st.caption(case_reason)


    st.divider()
    st.subheader("Résumé documents")

    rows = []
    for d in doc_results:
        r = d["result"]
        ex = r.get("extracted_data", {}) or {}
        rib, iban = safe_get_bank_fields(ex)
        cats = classify_doc_category(r.get("doc_type", ""), ex, d.get("ocr_text", ""))

        rows.append({
            "Fichier": d["file_name"],
            "Catégorie (OCR)": ", ".join(sorted(cats)) if cats else "—",
            "Décision": r.get("decision", "REVIEW"),
            "Score": r.get("score", 0),
            "Signaux": "Y" if r.get("fraud_suspected") else "—",
            "Benef (nom)": ex.get("beneficiary_name", "N/A"),
            "Benef (CIN)": mask_value(ex.get("beneficiary_cin", "N/A"), keep_last=3),
            "Assuré": ex.get("subscriber_name", "N/A"),
            "Défunt": ex.get("deceased_name", "N/A"),
            "RIB": mask_value(re.sub(r"\D", "", rib), keep_last=4) if rib != "N/A" else "N/A",
            "IBAN": mask_value(iban, keep_last=4) if iban != "N/A" else "N/A",
            "BIC": ex.get("bank_bic", "N/A"),
            "Banque": ex.get("bank_name", "N/A"),
        })

    st.dataframe(rows, use_container_width=True)

    st.divider()
    st.subheader("Détails")
    for d in doc_results:
        r = d["result"]
        ex = r.get("extracted_data", {}) or {}
        cats = classify_doc_category(r.get("doc_type", ""), ex, d.get("ocr_text", ""))

        with st.expander(f"📄 {d['file_name']} — {r.get('decision')} — {r.get('score', 0)}/100", expanded=False):
            st.write(f"**Type (LLM):** {r.get('doc_type', 'UNKNOWN')} | **Catégorie (OCR):** {', '.join(sorted(cats)) if cats else '—'}")
            st.write(f"**Signaux:** {'Oui' if r.get('fraud_suspected') else 'Non'}")
            st.write(f"**Raison:** {to_safe_reason(r.get('reason', ''))}")

            st.json(sanitize_dict({
                "extracted_data": ex,
                "format_validation": r.get("format_validation", {}),
                "dates_extracted": r.get("dates_extracted", {}),
                "tech_report": d.get("tech_report", {}),
                "structure": d.get("structure", {}),
            }))

            if show_tech:
                st.text((d.get("ocr_text") or "")[:1200])

    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass

# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.title("📊 Dashboard")

def count_cases(base_dir: str) -> int:
    if not os.path.exists(base_dir):
        return 0
    return len([d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))])

st.sidebar.write(f"✅ ACCEPT : {count_cases(VALID_DIR)}")
st.sidebar.write(f"⏳ REVIEW : {count_cases(REVIEW_DIR)}")

if st.sidebar.button("🗑️ Vider tous les dossiers"):
    for d in [VALID_DIR, REVIEW_DIR, INVALID_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
    st.rerun()

if st.sidebar.button("📋 Audit trail (20 derniers)"):
    st.sidebar.subheader("Historique")
    conn = sqlite3.connect("audit_trail.db")
    c = conn.cursor()
    c.execute("""
        SELECT case_id, file_name, timestamp, score, decision, fraud_suspected
        FROM dossiers
        ORDER BY timestamp DESC
        LIMIT 20
    """)
    rows = c.fetchall()
    conn.close()
    for row in rows:
        case_id, file_name, ts, score, decision, fraud = row
        st.sidebar.caption(f"{case_id} | {file_name} | {score}/100 | {decision} | signaux:{'Y' if fraud else 'N'}")
