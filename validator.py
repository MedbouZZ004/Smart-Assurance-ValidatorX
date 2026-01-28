import os
import re
import json
import fitz  # PyMuPDF
import easyocr
import groq
import streamlit as st
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
from utils import (
    validate_iban,
    validate_date_format,
    validate_rib_morocco,
    build_iban_ma,
)


load_dotenv()


# ----------------------------
# Helpers
# ----------------------------
def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _clean_name(s: str) -> str:
    s = (s or "").strip()
    # 1. Remove any digits found in the name
    s = re.sub(r"\d+", " ", s)

    # 2. REMOVE THE HYPHEN HERE:
    # Before: s = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s\-']", " ", s)
    # After:
    s = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s']", " ", s)

    # 3. Collapse the resulting double spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s



def _normalize_cne(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def _is_cne_strict(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{1,2}\d{6}", _normalize_cne(s)))


def _parse_date_any(s: str) -> date | None:
    s = _norm_spaces(s)
    if not s:
        return None

    s2 = re.sub(r"[.\-]", "/", s)
    s2 = re.sub(r"\s+", "/", s2)

    for fmt in ("%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s2, fmt).date()
        except Exception:
            pass

    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_duration_to_timedelta(s: str) -> timedelta | None:
    s = (s or "").lower().strip()
    if not s:
        return None

    years = sum(int(x) for x in re.findall(r"(\d+)\s*(?:ans?|années?|annees?|year|years)", s))
    months = sum(int(x) for x in re.findall(r"(\d+)\s*(?:mois|month|months)", s))
    days = sum(int(x) for x in re.findall(r"(\d+)\s*(?:jours?|day|days)", s))

    if years == 0 and months == 0 and days == 0:
        return None
    return timedelta(days=years * 365 + months * 30 + days)


def _extract_cne_by_context(text: str, keywords: list[str]) -> str:
    """
    Find strict CNE near keywords. If nothing, return first strict CNE.
    """
    t = (text or "").upper()
    raw_matches = list(re.finditer(r"\b[A-Z]{2}\s*[-]?\s*\d{6}\b", t))
    if not raw_matches:
        return ""

    strict = []
    for m in raw_matches:
        c = _normalize_cne(m.group(0))
        if _is_cne_strict(c):
            strict.append((c, m.start()))

    if not strict:
        return ""

    if not keywords:
        return strict[0][0]

    for c, pos in strict:
        left = t[max(0, pos - 120):pos]
        if any(k.upper() in left for k in keywords):
            return c

    return strict[0][0]


# ----------------------------
# Cached EasyOCR Reader
# ----------------------------
@st.cache_resource
def get_ocr_reader():
    """
    Cache the EasyOCR reader to avoid reloading on every run.
    French + English only (NO Arabic to avoid errors).
    """
    return easyocr.Reader(["fr", "en"], gpu=False)


# ----------------------------
# Main class
# ----------------------------
class InsuranceValidator:
    """
    POLICY:
    - NEVER auto-reject
    - ONLY: ACCEPT or REVIEW
    """

    def __init__(self):
        # Use cached reader (French + English only)
        self.reader = get_ocr_reader()

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY non trouvée ! Vérifiez votre fichier .env.")
        self.client = Groq(api_key=api_key)
        self.groq_timeout = 30

    def analyze_technical_integrity(self, doc, file_path: str) -> dict:
        metadata = doc.metadata or {}
        fraud_tools = ["canva", "photoshop", "illustrator", "gimp", "inkscape", "adobe acrobat pro"]
        creator = (metadata.get("creator") or "").lower()
        producer = (metadata.get("producer") or "").lower()
        is_suspicious_tool = any(tool in creator or tool in producer for tool in fraud_tools)

        fonts = []
        for page in doc:
            fonts.extend([f[3] for f in page.get_fonts()])
        font_count = len(set(fonts))

        potential_tampering = bool(is_suspicious_tool or font_count > 8)

        return {
            "suspicious_metadata": bool(is_suspicious_tool),
            "editor_detected": creator if creator else producer,
            "font_count": font_count,
            "potential_tampering": potential_tampering,
            "file_path": file_path,
        }

    def extract_all(self, file_path: str, file_bytes: bytes | None = None, fileName=None):
        """
        OCR:
        - PDF via PyMuPDF pages -> pixmap -> bytes png (LOWER ZOOM = 0.8 for speed)
        - IMAGE via bytes (jpg/png/webp) passed from app.py
        """
        # Add this line at the start
        # This creates a visual progress box in the Streamlit UI
        file_name = os.path.basename(file_path)
        with st.status(f"Analyse de {file_name}...", expanded=False) as status:
            st.write("🔍 [Etape 1/2] Extraction du texte (OCR)...")
            print(f"🔍 OCR: {file_name}")
        ext = os.path.splitext(file_path)[1].lower()


        # IMAGE mode
        if ext in [".png", ".jpg", ".jpeg", ".webp"] and file_bytes is not None:
            structure = {"has_images": True, "page_count": 1, "has_tables": False}
            tech_report = {
                "suspicious_metadata": False,
                "editor_detected": "image_upload",
                "font_count": 0,
                "potential_tampering": False,
                "file_path": file_path,
            }

            # EasyOCR accepts bytes for readtext
            text_results = self.reader.readtext(file_bytes, detail=0)
            # validator.py

            # ... after the existing text_results.extend(...) ...
            text_results.extend(self.reader.readtext(file_bytes, detail=0))

            # --- ADD THIS FOR CONSOLE DEBUGGING ---
            print(f"\n--- DEBUG: RAW OCR FOR {file_path} ---")
            print(" ".join(text_results))
            print("-" * 40 + "\n")
            # --------------------------------------
            return " ".join(text_results), structure, tech_report

        # PDF mode
        text_results = []
        structure = {"has_images": False, "page_count": 0, "has_tables": False}

        doc = fitz.open(file_path)
        structure["page_count"] = len(doc)
        tech_report = self.analyze_technical_integrity(doc, file_path)

        for page in doc:
            if len(page.get_images()) > 0:
                structure["has_images"] = True
            if len(page.get_drawings()) > 10:
                structure["has_tables"] = True

            # REDUCED DPI (0.8 instead of 1.2) => MUCH FASTER, still readable
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            img_bytes = pix.tobytes("png")
            text_results.extend(self.reader.readtext(img_bytes, detail=0))
        raw_text = " ".join(text_results)
        print(f"DEBUG FULL OCR: {raw_text}")
        st.write("📝 Texte extrait avec succès.")
        status.update(label=f"OCR terminé pour {file_name}", state="complete")
        return " ".join(text_results), structure, tech_report

    def validate_with_groq(self, text: str, structure: dict, tech_report: dict, forced_doc_type: str):
        # Show a small notification at the bottom of the screen
        st.toast(f"🧠 Intelligence Artificielle : Analyse du document {forced_doc_type}...")

        forced_doc_type = (forced_doc_type or "").strip().upper()
        if forced_doc_type not in {"ID", "BANK", "DEATH", "LIFE_CONTRACT"}:
            forced_doc_type = "UNKNOWN"

        prompt = f"""
RÔLE : Auditeur Expert en Assurance (MAROC).
MISSION : Extraire les données du texte OCR pour un dossier de succession.
RÈGLE D'OR : Analyse UNIQUEMENT le texte fourni. Ne réutilise JAMAIS des noms ou CNE vus dans d'autres documents.

TYPE DE DOCUMENT ATTENDU : {forced_doc_type}

---
DIRECTIVES PAR TYPE :

1. SI TYPE = ID :
   - 'cni_full_name' : Concatène le 'Nom' et le 'Prénom' (ex: "DOHA EL IDRISSI...").
   - 'cni_cne' : Extrais le numéro CNIE/CIN exact (ex: CD936873).

2. SI TYPE = BANK :
   - 'bank_account_holder' : Capture l'intitulé complet du compte.
   - Ignore tout CNE ou date de naissance sur ce document.

3. SI TYPE = DEATH :
   - 'deceased_full_name' : Nom de la personne décédée.
   - 'deceased_cne' : Son numéro de CIN/CNIE.
   - 'death_date' : Extrais UNIQUEMENT la date (DD/MM/YYYY). Ignore l'heure (ex: si le texte dit '17.07 12/01/2026', extrais '12/01/2026').

4. SI TYPE = LIFE_CONTRACT :
   - 'insured_full_name/cne' : Concerne l'ASSURÉ (souvent le défunt).
   - 'beneficiary_full_name/cne' : Concerne le BÉNÉFICIAIRE (celui qui reçoit le capital).
   - ATTENTION : Ne confonds pas les deux. Lis attentivement les sections "ASSURÉ" et "BÉNÉFICIAIRE".

DIRECTIVES CRITIQUES:
1. Analyse UNIQUEMENT le texte OCR suivant. Oublie les fichiers précédents.
2. Ne réutilise JAMAIS un CNE ou un Nom d'un autre document.
3. Si l'OCR dit 'CD936873', n'utilise pas 'CD112323'.
---

TEXTE OCR:
{text[:6000]}

STRUCTURE:
{json.dumps(structure, ensure_ascii=False)}

TECH REPORT:
{json.dumps(tech_report, ensure_ascii=False)}

TU DOIS GÉNÉRER UN JSON CONFORME AU FORMAT CI-DESSOUS. Ne produit AUCUN texte explicatif.

Champs:
- "decision": "ACCEPT" OU "REVIEW" uniquement. Jamais REJECT.
- "score": 0-100
- "country": "MAROC"
- "doc_type": "{forced_doc_type}"
- "fraud_suspected": true/false
- "fraud_signals": ["signal1", "signal2"]
- "extracted_data":
  * Si {forced_doc_type} = ID: cni_full_name, cni_cne, cni_birth_date, cni_expiry_date
  * Si {forced_doc_type} = BANK: bank_account_holder, bank_code_banque, bank_code_ville, bank_numero_compte, bank_cle_rib, bank_iban
  * Si {forced_doc_type} = DEATH: deceased_full_name, deceased_cne, deceased_birth_date, death_date
  * Si {forced_doc_type} = LIFE_CONTRACT: insured_full_name, insured_cne, insured_birth_date, beneficiary_full_name, beneficiary_cne, beneficiary_birth_date, contract_effective_date, contract_duration, contract_end_date
- "format_validation":
  * dates_format_valid: true/false
  * rib_format_valid: true/false
  * iban_format_valid: true/false
  * cne_format_valid: true/false
- "reason": texte descriptif

CONTRAINTES:
1. CNE format STRICT: 2 lettres + 6 chiffres. Si invalide ou absent => laisser vide ("").
2. Dates format: DD/MM/YYYY ou similaire.
3. Pour RIB:
   - bank_code_banque (3 chiffres)
   - bank_code_ville (3 chiffres)
   - bank_numero_compte (16 chiffres)
   - bank_cle_rib (2 chiffres)
   Total RIB = 24 chiffres.
4. Si données manquantes/illisibles => mettre "".
5. Si texte introuvable => decision="REVIEW", reason="Champ manquant".

EXEMPLES:

TYPE: ID
{{
  "decision": "ACCEPT",
  "score": 95,
  "country": "MAROC",
  "doc_type": "ID",
  "fraud_suspected": false,
  "fraud_signals": [],
  "extracted_data": {{
    "cni_full_name": "BENALI MOHAMED",
    "cni_cne": "AB123456",
    "cni_birth_date": "15/03/1985",
    "cni_expiry_date": "20/08/2020"
  }},
  "format_validation": {{
    "dates_format_valid": true,
    "rib_format_valid": true,
    "iban_format_valid": true,
    "cne_format_valid": true
  }},
  "reason": "CNI bien extraite, CNE valide, date expiration correcte."
}}

TYPE: BANK
{{
  "decision": "REVIEW",
  "score": 70,
  "country": "MAROC",
  "doc_type": "BANK",
  "fraud_suspected": false,
  "fraud_signals": [],
  "extracted_data": {{
    "bank_account_holder": "BENALI MOHAMED",
    "bank_code_banque": "011",
    "bank_code_ville": "640",
    "bank_numero_compte": "1234567890123456",
    "bank_cle_rib": "78",
    "bank_iban": "MA64230270457496521100710060"
  }},
  "format_validation": {{
    "dates_format_valid": true,
    "rib_format_valid": true,
    "iban_format_valid": true,
    "cne_format_valid": true
  }},
  "reason": "RIB présent, IBAN correct, clé valide."
}}

TYPE: DEATH
{{
  "decision": "REVIEW",
  "score": 97,
  "country": "MAROC",
  "doc_type": "DEATH",
  "fraud_suspected": false,
  "fraud_signals": [],
  "extracted_data": {{
    "deceased_full_name": "BENALI MOHAMED",
    "deceased_cne": "AB123456",
    "deceased_birth_date": "15/03/1985",
    "death_date": "10/12/2023"
  }},
  "format_validation": {{
    "dates_format_valid": true,
    "rib_format_valid": true,
    "iban_format_valid": true,
    "cne_format_valid": true
  }},
  
}}

TYPE: LIFE_CONTRACT
{{
  "decision": "ACCEPT",
  "score": 90,
  "country": "MAROC",
  "doc_type": "LIFE_CONTRACT",
  "fraud_suspected": false,
  "fraud_signals": [],
  "extracted_data": {{
    "insured_full_name": "BENALI MOHAMED",
    "insured_cne": "AB123456",
    "insured_birth_date": "15/03/1985",
    "beneficiary_full_name": "ALAMI FATIMA",
    "beneficiary_cne": "CD789012",
    "beneficiary_birth_date": "22/07/1990",
    "contract_effective_date": "01/01/2010",
    "contract_duration": "15 ans",
    "contract_end_date": ""
  }},
  "format_validation": {{
    "dates_format_valid": true,
    "rib_format_valid": true,
    "iban_format_valid": true,
    "cne_format_valid": true
  }},
  "reason": ""
}}
""".strip()

        try:
            chat = self.client.chat.completions.create(
                # Change "llama-3.3-70b-versatile" to "llama3-8b-8192"
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=self.groq_timeout,
                response_format={"type": "json_object"},
            )
            result = json.loads(chat.choices[0].message.content)
            result["doc_type"] = forced_doc_type
            st.success(f"✅ Analyse {forced_doc_type} terminée.")

            return self._validate_extracted_data(result, tech_report, text)

        except groq.AuthenticationError:
            raise ValueError("Clé API GROQ invalide.")
        except Exception as e:
            return {
                "decision": "REVIEW",
                "is_valid": False,
                "score": 0,
                "country": "MAROC",
                "doc_type": forced_doc_type,
                "fraud_suspected": False,
                "fraud_signals": [],
                "extracted_data": {},
                "format_validation": {},
                "reason": f"Erreur API/système : {str(e)}",
            }



    def _validate_extracted_data(self, groq_result: dict, tech_report: dict, raw_ocr_text: str) -> dict:
        tech_report = tech_report or {}
        format_errors: list[str] = []
        fraud_signals: list[str] = []

        groq_result = groq_result or {}
        groq_result.setdefault("format_validation", {})
        groq_result.setdefault("extracted_data", {})
        groq_result.setdefault("fraud_signals", [])
        groq_result.setdefault("country", "MAROC")

        extracted = groq_result.get("extracted_data", {}) or {}
        dt = (groq_result.get("doc_type") or "UNKNOWN").strip().upper()

        # Clean names
        for k in ["cni_full_name", "bank_account_holder", "deceased_full_name",
                  "insured_full_name", "beneficiary_full_name"]:
            if k in extracted:
                extracted[k] = _clean_name(extracted.get(k, ""))

        # Normalize CNE fields
        for k in ["cni_cne", "deceased_cne", "insured_cne", "beneficiary_cne"]:
            if extracted.get(k):
                extracted[k] = _normalize_cne(extracted[k])

        fv = groq_result["format_validation"]
        fv.setdefault("cne_format_valid", True)
        fv.setdefault("iban_format_valid", True)
        fv.setdefault("rib_format_valid", True)
        fv.setdefault("dates_format_valid", True)

        today = date.today()

        def _check_date_field(key: str, label: str) -> date | None:
            v = _norm_spaces(extracted.get(key, ""))
            if not v:
                return None

            # --- FIX: Pre-clean dots into slashes for unified format ---
            v_clean = re.sub(r"[.\-]", "/", v)
            v_clean = re.sub(r"\s+", "/", v_clean)
            # -----------------------------------------------------------

            ok, formatted_or_msg = validate_date_format(v_clean)
            if not ok:
                fv["dates_format_valid"] = False
                format_errors.append(f"{label} invalide: {v}")
                return None

            # Update the extracted data with the unified DD/MM/YYYY string
            extracted[key] = formatted_or_msg

            d = _parse_date_any(formatted_or_msg)
            if not d:
                fv["dates_format_valid"] = False
                format_errors.append(f"{label} illisible: {v}")
                return None
            return d

        def _check_cne_field(key: str, label: str, fallback_keywords: list[str]):
            v = extracted.get(key, "")
            if not v:
                fb = _extract_cne_by_context(raw_ocr_text, fallback_keywords)
                if fb:
                    extracted[key] = fb
                    v = fb
            if not v:
                fv["cne_format_valid"] = False
                format_errors.append(f"{label} manquant.")
                return
            if not _is_cne_strict(v):
                fv["cne_format_valid"] = False
                format_errors.append(f"{label} invalide (2 lettres + 6 chiffres): {v}")

        # ID rules
        if dt == "ID":
            _check_cne_field("cni_cne", "CNE (CNI)", ["CNIE", "CIN", "NUM", "N°"])
            _check_date_field("cni_birth_date", "Date naissance (CNI)")
            exp = _check_date_field("cni_expiry_date", "Date expiration (CNI)")
            # your rule: expiry must be > today
            if exp and exp <= today:
                format_errors.append("CNI invalide selon règle projet: date expiration doit être > date du jour.")
            # Use .get() with a default to avoid 'missing' errors if the AI is slightly off
            cne = extracted.get("cni_cne") or extracted.get("cne") or ""
            if not cne:
                format_errors.append("CNE manquant")
            else:
                extracted["cni_cne"] = _normalize_cne(cne)


        # BANK rules
        elif dt == "BANK":
            ex = extracted  # on travaille sur le dict final

            holder = _norm_spaces(ex.get("bank_account_holder", ""))

            # --- 0) Lire IBAN OCR (si présent), le nettoyer, et vérifier checksum ---
            iban_ocr_raw = _norm_spaces(ex.get("bank_iban", "")).upper()
            iban_ocr_clean = re.sub(r"[^A-Z0-9]", "", iban_ocr_raw)

            # si "MA" apparaît au milieu, on découpe à partir de MA sur 28 chars
            if "MA" in iban_ocr_clean:
                i = iban_ocr_clean.find("MA")
                iban_ocr_clean = iban_ocr_clean[i:i + 28]

            use_iban_as_source = False
            if len(iban_ocr_clean) == 28:
                ok_ocr_iban, _msg = validate_iban(iban_ocr_clean)
                if ok_ocr_iban:
                    use_iban_as_source = True

            if use_iban_as_source:
                # --- 1) SOURCE DE VÉRITÉ: IBAN OCR valide -> on parse le RIB depuis l’IBAN ---
                rib24 = iban_ocr_clean[4:]  # après MAxx

                cb = rib24[0:3]
                cv = rib24[3:6]
                nc16 = rib24[6:22]
                kr = rib24[22:24]

                ex["bank_code_banque"] = cb
                ex["bank_code_ville"] = cv
                ex["bank_numero_compte"] = nc16
                ex["bank_cle_rib"] = kr
                ex["bank_rib_code"] = rib24

                ok_rib, msg_rib = validate_rib_morocco(rib24)
                fv["rib_format_valid"] = bool(ok_rib)
                if not ok_rib:
                    format_errors.append(msg_rib)

                fv["iban_format_valid"] = True
                ex["bank_iban"] = iban_ocr_clean
                ex["bank_iban_reconstructed"] = iban_ocr_clean
                ex["bank_iban_source"] = "ocr_iban_valid_parsed"

            else:
                # --- 2) FALLBACK: champs OCR -> normalisation -> reconstruction IBAN ---
                cb = re.sub(r"\D", "", ex.get("bank_code_banque", "") or "").zfill(3)
                cv = re.sub(r"\D", "", ex.get("bank_code_ville", "") or "").zfill(3)
                nc = re.sub(r"\D", "", ex.get("bank_numero_compte", "") or "")
                kr = re.sub(r"\D", "", ex.get("bank_cle_rib", "") or "").zfill(2)

                # compte = 16 chiffres (OCR peut coller banque+ville+compte)
                if len(nc) > 16:
                    format_errors.append(
                        f"Numéro de compte OCR trop long, tronqué à 16 chiffres. Original: {nc}"
                    )
                    nc16 = nc[-16:]
                else:
                    nc16 = nc.zfill(16)

                rib24 = cb + cv + nc16 + kr
                ex["bank_rib_code"] = rib24

                ok_rib, msg_rib = validate_rib_morocco(rib24)
                fv["rib_format_valid"] = bool(ok_rib)
                if not ok_rib:
                    format_errors.append(msg_rib)

                iban_rebuilt = build_iban_ma(cb, cv, nc16, kr)
                ok_iban, msg_iban = validate_iban(iban_rebuilt)
                fv["iban_format_valid"] = bool(ok_iban)
                if not ok_iban:
                    format_errors.append(f"IBAN reconstruit invalide: {msg_iban}")

                # Audit seulement (si IBAN OCR existe mais n’est pas valide/fiable)
                if iban_ocr_raw and iban_ocr_clean and iban_ocr_clean != iban_rebuilt:
                    format_errors.append(f"IBAN OCR différent du reconstruit. OCR: {iban_ocr_clean}")

                ex["bank_iban"] = iban_rebuilt
                ex["bank_iban_reconstructed"] = iban_rebuilt
                ex["bank_iban_source"] = "reconstructed_from_rib"

            # --- 3) Holder check ---
            if not holder:
                format_errors.append("Titulaire du compte (bank_account_holder) manquant.")

        # DEATH rules
        elif dt == "DEATH":
            _check_cne_field("deceased_cne", "CNE (décès)", ["DECE", "DECED", "CIN", "CNIE", "ID"])
            _check_date_field("deceased_birth_date", "Date naissance (décès)")
            dth = _check_date_field("death_date", "Date décès")
            # your rule: death date must be < today
            # FIXED: Error ONLY if death date is in the future
            if dth and dth > today:
                format_errors.append("Date décès invalide: la date ne peut pas être dans le futur.")

        # LIFE_CONTRACT rules
        elif dt == "LIFE_CONTRACT":
            _check_cne_field("insured_cne", "CNE (assuré)", ["ASSURE", "ADHERENT", "SOUSCRIPTEUR", "CIN", "CNIE"])
            _check_cne_field("beneficiary_cne", "CNE (bénéficiaire)", ["BENEFICIAIRE", "AYANT", "DROIT", "CIN", "CNIE"])

            _check_date_field("insured_birth_date", "Naissance (assuré)")
            _check_date_field("beneficiary_birth_date", "Naissance (bénéficiaire)")

            eff = _check_date_field("contract_effective_date", "Date effectuation")
            end = _check_date_field("contract_end_date", "Date fin contrat")
            duration = _norm_spaces(extracted.get("contract_duration", ""))

            # your rule: (effective + duration) < today OR end_date < today
            if end:
                if end >= today:
                    format_errors.append("Contrat invalide selon règle projet: date fin doit être < date du jour.")
            else:
                if eff:
                    td = _parse_duration_to_timedelta(duration)
                    if td is None:
                        format_errors.append("Durée manquante/illisible (si pas de date fin).")
                    else:
                        computed_end = eff + td
                        if computed_end <= today:
                            format_errors.append("Contrat invalide selon règle projet: date effet + durée doit être < date du jour.")

        # -------------------------------
        # BUSINESS OVERRIDE (BANK CLEAN)
        # -------------------------------
        fv = groq_result.get("format_validation", {})

        is_clean_bank = (
            fv.get("rib_format_valid") is True and
            fv.get("iban_format_valid") is True and
            not tech_report.get("potential_tampering", False) and
            not tech_report.get("suspicious_metadata", False)
        )

        if dt == "BANK" and is_clean_bank:
            groq_result["score"] = max(groq_result.get("score", 0), 90)
            groq_result["decision"] = "ACCEPT"
            groq_result["fraud_suspected"] = False


        # scoring / decision
        base_score = int(groq_result.get("score", 60) or 60)
        penalty = (len(format_errors) * 6) + (len(fraud_signals) * 10)
        final_score = max(0, base_score - penalty)
        groq_result["score"] = final_score

        groq_result["fraud_suspected"] = len(fraud_signals) > 0
        groq_result["fraud_signals"] = list(set(groq_result.get("fraud_signals", []) + fraud_signals))

        if tech_report.get("potential_tampering"):
            groq_result["decision"] = "REVIEW"
        elif final_score >= 85 and not groq_result["fraud_suspected"] and len(format_errors) == 0:
            groq_result["decision"] = "ACCEPT"
        else:
            groq_result["decision"] = "REVIEW"

        if format_errors:
            existing = (groq_result.get("reason") or "").strip()
            extra = "À vérifier: " + "; ".join(format_errors)
            groq_result["reason"] = (existing + " | " + extra).strip(" |")

        groq_result["is_valid"] = (groq_result["decision"] == "ACCEPT")
        groq_result["extracted_data"] = extracted
        return groq_result