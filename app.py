import streamlit as st
import os, shutil, json, hashlib, logging, re
from datetime import datetime
import sqlite3

from validator import InsuranceValidator
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

VALID_DIR = "validated_docs"
REVIEW_DIR = "review_needed"
INVALID_DIR = "invalid_docs"
TMP_DIR = "uploads_tmp"

for d in [VALID_DIR, REVIEW_DIR, INVALID_DIR, TMP_DIR]:
    os.makedirs(d, exist_ok=True)

sec = initialize_security()
audit_logger = sec["audit"]
fingerprints = sec["fingerprints"]

# -----------------------------
# Audit DB (with migration)
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
            reason_short TEXT
        )
    """)
    conn.commit()

    
    c.execute("PRAGMA table_info(dossiers)")
    cols = [row[1] for row in c.fetchall()]
    if "expected_type" not in cols:
        c.execute("ALTER TABLE dossiers ADD COLUMN expected_type TEXT")
        conn.commit()

    conn.close()

init_audit_db()

def fuzzy_name_match(name1, name2):
    if not name1 or not name2 or name1 == "—" or name2 == "—":
        return False

    def norm(n: str) -> list[str]:
        n = (n or "").upper().replace("-", " ")  
        n = re.sub(r"[^A-Z\s]", " ", n)          
        n = re.sub(r"\s+", " ", n).strip()
        return sorted(n.split())

    return norm(name1) == norm(name2)


def compute_file_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()

def to_safe_reason(reason: str, max_len: int = 220) -> str:
    r = (reason or "").replace("\n", " ").strip()
    if len(r) > max_len:
        r = r[:max_len] + "..."
    return r

def save_to_audit_db(case_id, expected_type, file_name, file_hash, score, decision, fraud_suspected, reason_short):
    conn = sqlite3.connect("audit_trail.db")
    c = conn.cursor()
    timestamp = datetime.now().isoformat()
    c.execute("""
        INSERT INTO dossiers (case_id, expected_type, file_name, file_hash, timestamp, score, decision, fraud_suspected, reason_short)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (case_id, expected_type, file_name, file_hash, timestamp, int(score), decision, int(bool(fraud_suspected)), reason_short))
    conn.commit()
    conn.close()

# -----------------------------
# Matching helpers 
# -----------------------------
def normalize_simple(s: str) -> str:
    s = (s or "").lower()

   
    s = s.replace("-", " ")

    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-zàâçéèêëîïôùûüÿñ\s']", " ", s)  
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_overlap(a: str, b: str) -> float:
    a, b = normalize_simple(a), normalize_simple(b)
    if not a or not b:
        return 0.0
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, len(sa | sb))

def parse_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    s2 = re.sub(r"[.\-]", "/", s)
    s2 = re.sub(r"\s+", "/", s2)
    for fmt in ("%d/%m/%Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s2 if fmt != "%Y-%m-%d" else s, fmt).date()
        except Exception:
            pass
    return None

def dates_equal(a: str, b: str) -> bool:
    da, db = parse_date(a), parse_date(b)
    if da and db:
        return da == db
    return (a or "").strip() == (b or "").strip()

def safe_get_bank_fields(extracted: dict):
    ex = extracted or {}
    rib = (ex.get("bank_rib_code") or "").strip()
    iban = (ex.get("bank_iban") or "").strip()
    holder = (ex.get("bank_account_holder") or "").strip()
    return holder, rib or "N/A", iban or "N/A"

def compute_cross_checks(docs: dict) -> list[str]:
    issues = []

    id_ex = docs["ID"]["result"].get("extracted_data", {}) or {}
    bank_ex = docs["BANK"]["result"].get("extracted_data", {}) or {}
    death_ex = docs["DEATH"]["result"].get("extracted_data", {}) or {}
    life_ex = docs["LIFE_CONTRACT"]["result"].get("extracted_data", {}) or {}

    id_name = (id_ex.get("cni_full_name") or "").strip()
    id_cne = (id_ex.get("cni_cne") or "").strip().upper()
    id_birth = (id_ex.get("cni_birth_date") or "").strip()

    bank_holder = (bank_ex.get("bank_account_holder") or "").strip()

    deceased_name = (death_ex.get("deceased_full_name") or "").strip()
    deceased_cne = (death_ex.get("deceased_cne") or "").strip().upper()
    deceased_birth = (death_ex.get("deceased_birth_date") or "").strip()

    insured_name = (life_ex.get("insured_full_name") or "").strip()
    insured_cne = (life_ex.get("insured_cne") or "").strip().upper()
    insured_birth = (life_ex.get("insured_birth_date") or "").strip()

    ben_name = (life_ex.get("beneficiary_full_name") or "").strip()
    ben_cne = (life_ex.get("beneficiary_cne") or "").strip().upper()
    ben_birth = (life_ex.get("beneficiary_birth_date") or "").strip()

    # CNI <-> RIB
    if name_overlap(id_name, bank_holder) < 0.55:
        issues.append("CNI vs RIB: nom complet ≠ intitulé de compte.")

    # CNI <-> Assurance (beneficiary)
    if name_overlap(id_name, ben_name) < 0.55:
        issues.append("CNI vs Assurance: nom CNI ≠ nom bénéficiaire.")
    if id_cne and ben_cne and id_cne != ben_cne:
        issues.append("CNI vs Assurance: CNE CNI ≠ CNE bénéficiaire.")
    if id_birth and ben_birth and not dates_equal(id_birth, ben_birth):
        issues.append("CNI vs Assurance: date naissance CNI ≠ date naissance bénéficiaire.")

    # RIB <-> Assurance
    if name_overlap(bank_holder, ben_name) < 0.55:
        issues.append("RIB vs Assurance: intitulé de compte ≠ bénéficiaire.")

    # Décès <-> Assurance (insured)
    if name_overlap(deceased_name, insured_name) < 0.55:
        issues.append("Décès vs Assurance: nom décédé ≠ nom assuré.")
    if deceased_cne and insured_cne and deceased_cne != insured_cne:
        issues.append("Décès vs Assurance: CNE décédé ≠ CNE assuré.")
    if deceased_birth and insured_birth and not dates_equal(deceased_birth, insured_birth):
        issues.append("Décès vs Assurance: naissance décédé ≠ naissance assuré.")

    # sanity: insured != beneficiary
    if insured_name and ben_name and name_overlap(insured_name, ben_name) > 0.85:
        issues.append("Assurance: assuré et bénéficiaire semblent identiques (possible inversion).")

    return issues

def compute_case_decision(doc_results):
    docs = {d["expected_type"]: d for d in doc_results}
    missing = [k for k in ["ID", "BANK", "DEATH", "LIFE_CONTRACT"] if k not in docs]
    if missing:
        return "REVIEW", "Documents manquants: " + ", ".join(missing), ["Missing docs"]

    per_doc_issues = []
    for k in ["ID", "BANK", "DEATH", "LIFE_CONTRACT"]:
        r = docs[k]["result"]
        if r.get("decision") != "ACCEPT":
            per_doc_issues.append(f"{k}: {to_safe_reason(r.get('reason','')) or 'REVIEW'}")

    cross_issues = compute_cross_checks(docs)

    if not per_doc_issues and not cross_issues:
        return "ACCEPT", "OK: 4 docs + validations + cohérences inter-documents.", []

    issues = per_doc_issues + cross_issues
    return "REVIEW", " | ".join(issues)[:500], issues


st.set_page_config(page_title="Insurance Validator", layout="wide", initial_sidebar_state="expanded")

# Custom CSS styling
custom_css = """
<style>
    /* Main styling */
    .main {
        background: linear-gradient(135deg, #0f1419 0%, #1a1f2e 100%);
    }
    
    /* Header styling */
    h1, h2, h3 {
        color: #e0e8f0;
        font-weight: 700;
        letter-spacing: -0.5px;
    }
    
    h1 {
        font-size: 2.5rem !important;
        margin-bottom: 0.5rem !important;
    }
    
    /* Divider enhancement */
    hr {
        margin: 2rem 0 !important;
        border: none;
        height: 2px;
        background: linear-gradient(90deg, #667eea 0%, transparent 50%, #667eea 100%);
    }
    
    /* Button styling */
    .stButton > button {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white !important;
        border: none;
        border-radius: 8px;
        padding: 0.75rem 2rem !important;
        font-weight: 600;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
    }
    
    .stButton > button:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6);
    }
    
    /* File uploader styling */
    .stFileUploader {
        background: linear-gradient(135deg, rgba(102, 126, 234, 0.15) 0%, rgba(118, 75, 162, 0.15) 100%);
        border: 2px solid rgba(102, 126, 234, 0.3);
        border-radius: 12px;
        padding: 1.5rem;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        margin-bottom: 1rem;
    }
    
    /* Success/Warning/Error messages */
    .stAlert {
        border-radius: 10px;
        border-left: 4px solid;
        padding: 1rem;
        margin: 1rem 0;
        background: rgba(255, 255, 255, 0.05) !important;
    }
    
    /* Dataframe styling */
    .stDataFrame {
        border-radius: 10px;
        overflow: hidden;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
        background: rgba(30, 41, 59, 0.8) !important;
    }
    
    /* Expander styling */
    .streamlit-expanderHeader {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 8px;
        color: white !important;
    }
    
    /* Metric styling */
    .stMetric {
        background: linear-gradient(135deg, rgba(102, 126, 234, 0.2) 0%, rgba(118, 75, 162, 0.2) 100%);
        border: 1px solid rgba(102, 126, 234, 0.3);
        border-radius: 10px;
        padding: 1rem;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
    }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

# Main title with description
col1, col2 = st.columns([3, 1])
with col1:
    st.title("🏢 Insurance Document Validator")
    st.markdown("**Smart succession document analysis system** • Automated verification & fraud detection")

# Sidebar
st.sidebar.title("⚙️ Settings & Maintenance")

# Check if we're in cache clear mode
if "clear_cache_mode" not in st.session_state:
    st.session_state.clear_cache_mode = False

if st.sidebar.button("🧹 Clear Cache", use_container_width=True):
    st.session_state.clear_cache_mode = True

# Display cache clear confirmation page
if st.session_state.clear_cache_mode:
    st.divider()
    st.subheader("Clear Cache & Reset System")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.warning("This will delete all cached results, databases, and temporary files.")
        st.write("**Affected items:**")
        st.markdown("""
        - Audit database (audit_trail.db)
        - Fingerprint cache (fingerprints.json)
        - Temporary uploads folder
        - Validated documents folder
        - Review needed folder
        """)
    
    with col2:
        st.info("This action cannot be undone. Make sure you have backed up any important data.")
    
    st.divider()
    
    col_btn1, col_btn2, col_btn3 = st.columns([2, 2, 1])
    
    with col_btn1:
        if st.button("Confirm Clear", use_container_width=True, key="confirm_clear"):
            # nuke db + fingerprints + folders
            for f in ["audit_trail.db", "fingerprints.json"]:
                if os.path.exists(f):
                    os.remove(f)

            for d in [VALID_DIR, REVIEW_DIR, TMP_DIR]:
                if os.path.exists(d):
                    shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)

            st.success("Cache cleared successfully! System reset complete.")
            st.session_state.clear_cache_mode = False
            st.balloons()
            st.stop()
    
    with col_btn2:
        if st.button("Cancel", use_container_width=True, key="cancel_clear"):
            st.session_state.clear_cache_mode = False
            st.rerun()
    
    st.stop()

show_ocr_debug = st.checkbox("Show OCR Debug (technical details)", value=False)


st.divider()
st.subheader("Step 1: Upload Required Documents")
st.markdown("Please upload **all 4 documents** in the appropriate categories below. Accepted formats: PDF, PNG, JPG, JPEG, WebP")

types = ["pdf", "png", "jpg", "jpeg", "webp"]

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### Identity Documents")
    cni_file = st.file_uploader("1. National ID (CNI/CNIE)", type=types, accept_multiple_files=False, key="cni")
    if cni_file:
        st.caption(f"{cni_file.name} ({cni_file.size} bytes)")
    
    st.markdown("#### Insurance Documents")
    life_file = st.file_uploader("4. Life Savings Policy (épargne-vie)", type=types, accept_multiple_files=False, key="life")
    if life_file:
        st.caption(f"{life_file.name} ({life_file.size} bytes)")

with col2:
    st.markdown("#### Administrative Documents")
    death_file = st.file_uploader("3. Death Certificate", type=types, accept_multiple_files=False, key="death")
    if death_file:
        st.caption(f"{death_file.name} ({death_file.size} bytes)")
    
    st.markdown("#### Banking Documents")
    rib_file = st.file_uploader("2. Bank Account Details (RIB/IBAN)", type=types, accept_multiple_files=False, key="rib")
    if rib_file:
        st.caption(f"{rib_file.name} ({rib_file.size} bytes)")

if not (cni_file and rib_file and death_file and life_file):
    st.info("Waiting for all 4 documents to be uploaded...")
    st.stop()

# Progress indicator
progress_placeholder = st.empty()
st.divider()

col_center = st.columns([1, 2, 1])[1]
with col_center:
    if st.button("Start Analysis", use_container_width=True, key="start_analysis"):
        st.session_state.analysis_started = True

if "analysis_started" not in st.session_state or not st.session_state.analysis_started:
    st.stop()

inputs = [
    ("ID", cni_file),
    ("BANK", rib_file),
    ("DEATH", death_file),
    ("LIFE_CONTRACT", life_file),
]

validator = InsuranceValidator()

case_id = hashlib.sha256(
    ("|".join([f.name for _, f in inputs]) + str(datetime.now().timestamp())).encode("utf-8")
).hexdigest()[:10]

temp_dir = os.path.join(TMP_DIR, case_id)
os.makedirs(temp_dir, exist_ok=True)


st.subheader("Step 2: Processing Documents")
st.caption(f"Case ID: `{case_id}`")

progress_bar = st.progress(0)
status_text = st.empty()

doc_results = []
errors = []

for expected_type, uf in inputs:
    file_bytes = uf.getbuffer().tobytes()
    file_hash = compute_file_hash(file_bytes)

    local_path = os.path.join(temp_dir, uf.name)
    with open(local_path, "wb") as f:
        f.write(file_bytes)

    
    is_dup, prev_decision = fingerprints.is_duplicate(local_path)
    if is_dup:
        st.warning(f"{expected_type}: File already analyzed before (previous: {prev_decision}). Re-analyzing...")

    try:
       
        idx = [x[0] for x in inputs].index(expected_type)
        progress = (idx) / len(inputs)
        progress_bar.progress(progress)
        status_text.markdown(f"**Processing:** {expected_type} ({uf.name})...")
        
        ocr_text, structure, tech_report = validator.extract_all(local_path, file_bytes=file_bytes)
        result = validator.validate_with_groq(
            ocr_text,
            structure,
            tech_report,
            forced_doc_type=expected_type,
        )

        doc_results.append({
            "expected_type": expected_type,
            "file_name": uf.name,
            "local_path": local_path,
            "file_hash": file_hash,
            "ocr_text": ocr_text,
            "structure": structure,
            "tech_report": tech_report,
            "result": result
        })

        fingerprints.register_fingerprint(local_path, result.get("decision", "REVIEW"), int(result.get("score", 0)))

        save_to_audit_db(
            case_id=case_id,
            expected_type=expected_type,
            file_name=uf.name,
            file_hash=file_hash,
            score=result.get("score", 0),
            decision=result.get("decision", "REVIEW"),
            fraud_suspected=result.get("fraud_suspected", False),
            reason_short=to_safe_reason(result.get("reason", "")),
        )

    except Exception as e:
        errors.append((uf.name, str(e)))
        doc_results.append({
            "expected_type": expected_type,
            "file_name": uf.name,
            "local_path": local_path,
            "file_hash": file_hash,
            "ocr_text": "",
            "structure": {},
            "tech_report": {},
            "result": {
                "decision": "REVIEW",
                "score": 0,
                "doc_type": expected_type,
                "fraud_suspected": False,
                "reason": f"Error: {str(e)}",
                "extracted_data": {}
            }
        })


progress_bar.progress(1.0)
status_text.success("All documents processed successfully!")

st.divider()

case_decision, case_reason, case_issues = compute_case_decision(doc_results)


id_data = next((d for d in doc_results if d["expected_type"] == "ID"), None)
bank_data = next((d for d in doc_results if d["expected_type"] == "BANK"), None)
death_data = next((d for d in doc_results if d["expected_type"] == "DEATH"), None)
life_data = next((d for d in doc_results if d["expected_type"] == "LIFE_CONTRACT"), None)


if id_data and bank_data:
    name_id = id_data["result"]["extracted_data"].get("cni_full_name")
    name_bank = bank_data["result"]["extracted_data"].get("bank_account_holder")
    if not fuzzy_name_match(name_id, name_bank):
        case_issues.append(f"CNI vs BANK: Nom CNI ({name_id}) ≠ Titulaire RIB ({name_bank})")


if death_data and life_data:
    cne_death = death_data["result"]["extracted_data"].get("deceased_cne")
    cne_insured = life_data["result"]["extracted_data"].get("insured_cne")
    if cne_death and cne_insured and cne_death != cne_insured:
        case_issues.append(f"Décès vs Assurance: CNE décédé ({cne_death}) ≠ CNE assuré ({cne_insured})")


if id_data and life_data:
    cne_id = id_data["result"]["extracted_data"].get("cni_cne")
    cne_benef = life_data["result"]["extracted_data"].get("beneficiary_cne")
    if cne_id and cne_benef and cne_id != cne_benef:
        case_issues.append(f"CNI vs Assurance: CNE bénéficiaire ({cne_benef}) ≠ CNE CNI ({cne_id})")
dest_root = VALID_DIR if case_decision == "ACCEPT" else REVIEW_DIR
case_dir = os.path.join(dest_root, case_id)
os.makedirs(case_dir, exist_ok=True)

for d in doc_results:
    if os.path.exists(d["local_path"]):
        shutil.copy(d["local_path"], os.path.join(case_dir, d["file_name"]))

report = {
    "case_id": case_id,
    "case_decision": case_decision,
    "case_reason": case_reason,
    "timestamp": datetime.now().isoformat(),
    "errors": [{"file": n, "error": m} for n, m in errors],
    "issues": case_issues,
    "documents": []
}

for d in doc_results:
    r = d["result"]
    ex = r.get("extracted_data", {}) or {}
    holder, rib, iban = safe_get_bank_fields(ex)
    report["documents"].append({
        "expected_type": d["expected_type"],
        "file_name": d["file_name"],
        "decision": r.get("decision", "REVIEW"),
        "score": r.get("score", 0),
        "fraud_suspected": bool(r.get("fraud_suspected", False)),
        "reason": r.get("reason", ""),
        "extracted_data": ex,
        "bank_account_holder": holder,
        "bank_rib_code": rib,
        "bank_iban": iban
    })

with open(os.path.join(case_dir, "report.json"), "w", encoding="utf-8") as fp:
    json.dump(report, fp, ensure_ascii=False, indent=2)

st.divider()


st.subheader("Step 3: Validation Results")


decision_color = "[PASS]" if case_decision == "ACCEPT" else "[WARN]"
st.markdown(f"### Decision: {case_decision}")


col1, col2, col3, col4 = st.columns(4)

valid_docs = sum(1 for d in doc_results if d["result"].get("decision") == "ACCEPT")
avg_score = sum(d["result"].get("score", 0) for d in doc_results) / len(doc_results) if doc_results else 0
fraud_count = sum(1 for d in doc_results if d["result"].get("fraud_suspected", False))

with col1:
    st.metric("Valid Documents", f"{valid_docs}/4")
with col2:
    st.metric("Avg. Score", f"{int(avg_score)}/100")
with col3:
    st.metric("Fraud Alerts", fraud_count)
with col4:
    st.metric("Issues Found", len(case_issues))

st.markdown(f"**Analysis Summary:** {case_reason}")

st.divider()
st.subheader("Document Summary Table")

rows = []
for d in doc_results:
    r = d["result"]
    ex = r.get("extracted_data", {}) or {}
    holder, rib, iban = safe_get_bank_fields(ex)

   
    cni_nom, cni_cne, cni_naiss, cni_exp = "—", "—", "—", "—"
    deces_nom, deces_cne, deces_date = "—", "—", "—"

  
    if d["expected_type"] == "ID":
        cni_nom = ex.get("cni_full_name", "—")
        cni_cne = ex.get("cni_cne", "—")
        cni_naiss = ex.get("cni_birth_date", "—")
        cni_exp = ex.get("cni_expiry_date", "—")

    elif d["expected_type"] == "DEATH":
        deces_nom = ex.get("deceased_full_name", "—")
        deces_cne = ex.get("deceased_cne", "—")
        deces_date = ex.get("death_date", "—")
        cni_naiss = ex.get("deceased_birth_date", "—")

    elif d["expected_type"] == "LIFE_CONTRACT":
       
        cni_nom = ex.get("beneficiary_full_name", "—")
        cni_cne = ex.get("beneficiary_cne", "—")
        cni_naiss = ex.get("beneficiary_birth_date", "—")

        
        deces_nom = ex.get("insured_full_name", "—")
        deces_cne = ex.get("insured_cne", "—")
        deces_date = ex.get("insured_birth_date", "—") 

  
    rows.append({
        "Doc attendu": d["expected_type"],
        "Fichier": d["file_name"],
        "Décision": r.get("decision", "REVIEW"),
        "Score": r.get("score", 0),

        # CNI Columns (Mapped based on logic above)
        "CNI (nom)": cni_nom,
        "CNI (CNE)": mask_value(cni_cne, keep_last=3) if cni_cne != "—" else "—",
        "CNI (naiss.)": cni_naiss,
        "CNI (exp.)": cni_exp,

        # BANK Section
        "RIB (titulaire)": holder or "—",
        "RIB": mask_value(re.sub(r"\D", "", rib), keep_last=4) if rib != "N/A" else "—",
        "IBAN": mask_value(iban.replace(" ", ""), keep_last=4) if iban != "N/A" else "—",

        # DEATH Columns (Mapped based on logic above)
        "Décès (nom)": deces_nom,
        "Décès (CNE)": mask_value(deces_cne, keep_last=3) if deces_cne != "—" else "—",
        "Date décès": deces_date,

        # LIFE_CONTRACT Section (Keeping raw keys for clarity in those specific columns)
        "Assuré (nom)": ex.get("insured_full_name", "—"),
        "Assuré (CNE)": mask_value((ex.get("insured_cne", "—") or ""), keep_last=3),
        "Benef (nom)": ex.get("beneficiary_full_name", "—"),
        "Benef (CNE)": mask_value((ex.get("beneficiary_cne", "—") or ""), keep_last=3),
    })


st.dataframe(rows, use_container_width=True)

st.divider()
st.subheader("Cross-Document Consistency Check")
if case_issues:
    for i in case_issues:
        st.warning(f"{i}")
else:
    st.success("All documents are consistent and coherent!")

st.divider()
st.subheader("Detailed Document Analysis")
for d in doc_results:
    r = d["result"]
    ex = r.get("extracted_data", {}) or {}
    
    decision_status = "[PASS]" if r.get('decision') == "ACCEPT" else "[WARN]"
    
    with st.expander(f"{decision_status} {d['expected_type']} — {d['file_name']} — Score: {r.get('score',0)}/100"):
        st.write(f"**Analysis Result:** {to_safe_reason(r.get('reason',''))}")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Decision", r.get('decision', 'REVIEW'))
        with col2:
            st.metric("Confidence", f"{r.get('score', 0)}%")
        with col3:
            fraud_status = "YES" if r.get('fraud_suspected', False) else "No"
            st.metric("Fraud Suspected", fraud_status)
        
        st.json(sanitize_dict({
            "expected_type": d["expected_type"],
            "extracted_data": ex,
            "format_validation": r.get("format_validation", {}),
            "tech_report": d.get("tech_report", {}),
            "structure": d.get("structure", {}),
        }))
        if show_ocr_debug:
            st.text((d.get("ocr_text") or "")[:2000])

try:
    shutil.rmtree(temp_dir, ignore_errors=True)
except Exception:
    pass

st.divider()
st.markdown("---")
st.markdown("<div align='center'><small>Smart Assurance Validator — Hackathon Project</small></div>", unsafe_allow_html=True)
