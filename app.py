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

    # migration: add expected_type if missing
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
        n = (n or "").upper().replace("-", " ")  # <-- IMPORTANT
        n = re.sub(r"[^A-Z\s]", " ", n)          # <-- pas "" (sinon ça colle)
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
# Matching helpers (less dumb than token ratio)
# -----------------------------
def normalize_simple(s: str) -> str:
    s = (s or "").lower()

    # IMPORTANT: casser les tirets en espaces (CNI met des tirets)
    s = s.replace("-", " ")

    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-zàâçéèêëîïôùûüÿñ\s']", " ", s)  # <-- retire le \- ici
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

# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="Insurance Validator", layout="wide")
st.title("Insurance Document Validator — Forced 4-doc flow")

st.sidebar.title("Maintenance")
if st.sidebar.button("🧹 Vider cache résultats (DB + fingerprints + dossiers)"):
    # nuke db + fingerprints + folders
    for f in ["audit_trail.db", "fingerprints.json"]:
        if os.path.exists(f):
            os.remove(f)

    for d in [VALID_DIR, REVIEW_DIR, TMP_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)

    st.sidebar.success("Cache vidé. Relance la page.")
    st.stop()

show_ocr_debug = st.checkbox("Afficher OCR brut (debug)", value=False)

st.subheader("1) Upload (PDF / Image) — 4 documents")
types = ["pdf", "png", "jpg", "jpeg", "webp"]

cni_file = st.file_uploader("1) CNI / CNIE", type=types, accept_multiple_files=False)
rib_file = st.file_uploader("2) RIB / IBAN", type=types, accept_multiple_files=False)
death_file = st.file_uploader("3) Certificat de décès", type=types, accept_multiple_files=False)
life_file = st.file_uploader("4) Assurance épargne-vie", type=types, accept_multiple_files=False)

if not (cni_file and rib_file and death_file and life_file):
    st.stop()

if not st.button("🚀 Démarrer l’analyse du dossier"):
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

st.subheader("2) Traitement")
doc_results = []
errors = []

for expected_type, uf in inputs:
    file_bytes = uf.getbuffer().tobytes()
    file_hash = compute_file_hash(file_bytes)

    local_path = os.path.join(temp_dir, uf.name)
    with open(local_path, "wb") as f:
        f.write(file_bytes)

    # duplicates: warn but still process (NO extra fake rows)
    is_dup, prev_decision = fingerprints.is_duplicate(local_path)
    if is_dup:
        st.warning(f"{expected_type}: fichier déjà analysé avant (prev: {prev_decision}). Je relance l’extraction quand même.")

    try:
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
                "reason": f"Erreur: {str(e)}",
                "extracted_data": {}
            }
        })

case_decision, case_reason, case_issues = compute_case_decision(doc_results)

# Get names/CNEs from our main variables
id_data = next((d for d in doc_results if d["expected_type"] == "ID"), None)
bank_data = next((d for d in doc_results if d["expected_type"] == "BANK"), None)
death_data = next((d for d in doc_results if d["expected_type"] == "DEATH"), None)
life_data = next((d for d in doc_results if d["expected_type"] == "LIFE_CONTRACT"), None)

# 1. CNI vs BANK (Fuzzy name match)
if id_data and bank_data:
    name_id = id_data["result"]["extracted_data"].get("cni_full_name")
    name_bank = bank_data["result"]["extracted_data"].get("bank_account_holder")
    if not fuzzy_name_match(name_id, name_bank):
        case_issues.append(f"CNI vs BANK: Nom CNI ({name_id}) ≠ Titulaire RIB ({name_bank})")

# 2. DEATH vs LIFE_CONTRACT (Match Insured to Deceased)
if death_data and life_data:
    cne_death = death_data["result"]["extracted_data"].get("deceased_cne")
    cne_insured = life_data["result"]["extracted_data"].get("insured_cne")
    if cne_death and cne_insured and cne_death != cne_insured:
        case_issues.append(f"Décès vs Assurance: CNE décédé ({cne_death}) ≠ CNE assuré ({cne_insured})")

# 3. CNI vs LIFE_CONTRACT (Match Beneficiary to CNI)
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
st.subheader("3) Résultat Case")
st.write(f"**Case ID:** {case_id}")
st.write(f"**Décision:** {case_decision}")
st.caption(case_reason)

st.divider()
st.subheader("Résumé (exactement 4 lignes)")

rows = []
for d in doc_results:
    r = d["result"]
    ex = r.get("extracted_data", {}) or {}
    holder, rib, iban = safe_get_bank_fields(ex)

    # ... inside your loop where you define 'ex' (extracted_data) ...

    # 1. Initialize empty values for our target columns
    cni_nom, cni_cne, cni_naiss, cni_exp = "—", "—", "—", "—"
    deces_nom, deces_cne, deces_date = "—", "—", "—"

    # 2. Apply Conditional Mapping based on expected_type
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
        # As requested: BÉNÉFICIAIRE goes to CNI cells
        cni_nom = ex.get("beneficiary_full_name", "—")
        cni_cne = ex.get("beneficiary_cne", "—")
        cni_naiss = ex.get("beneficiary_birth_date", "—")

        # As requested: ASSURÉ goes to DÉCÈS cells
        deces_nom = ex.get("insured_full_name", "—")
        deces_cne = ex.get("insured_cne", "—")
        deces_date = ex.get("insured_birth_date", "—") # Using birth date since death date doesn't exist for insured here

    # 3. Now append the row using these variables
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
st.subheader("Cross-check issues")
if case_issues:
    for i in case_issues:
        st.write(f"- {i}")
else:
    st.success("Aucune incohérence détectée.")

st.divider()
st.subheader("Détails par document")
for d in doc_results:
    r = d["result"]
    ex = r.get("extracted_data", {}) or {}
    with st.expander(f"{d['expected_type']} — {d['file_name']} — {r.get('decision')} — {r.get('score',0)}/100"):
        st.write(f"**Raison:** {to_safe_reason(r.get('reason',''))}")
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
