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
)

load_dotenv()


# ----------------------------
# Helpers
# ----------------------------
def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_cne(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def _is_cne_strict(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}\d{6}", _normalize_cne(s)))


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

    def extract_all(self, file_path: str, file_bytes: bytes | None = None):
        """
        OCR:
        - PDF via PyMuPDF pages -> pixmap -> bytes png (LOWER ZOOM = 0.8 for speed)
        - IMAGE via bytes (jpg/png/webp) passed from app.py
        """
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
            pix = page.get_pixmap(matrix=fitz.Matrix(0.8, 0.8))
            img_bytes = pix.tobytes("png")
            text_results.extend(self.reader.readtext(img_bytes, detail=0))

        return " ".join(text_results), structure, tech_report

    def validate_with_groq(self, text: str, structure: dict, tech_report: dict, forced_doc_type: str):
        forced_doc_type = (forced_doc_type or "").strip().upper()
        if forced_doc_type not in {"ID", "BANK", "DEATH", "LIFE_CONTRACT"}:
            forced_doc_type = "UNKNOWN"

        prompt = f"""
RÔLE : Auditeur Assurance (MAROC) - Dossiers épargne-vie / succession.
RÈGLE : Tu ne rejettes JAMAIS. Décision = "ACCEPT" ou "REVIEW" uniquement.

DOCUMENT TYPE FORCÉ: {forced_doc_type}
IMPORTANT:
- Extraire UNIQUEMENT les champs du type forcé.
- CNE STRICT: 2 lettres + 6 chiffres (AB123456). Si doute => vide.
- Pour l'assurance:
  insured_* = ASSURÉ / ADHÉRENT / SOUSCRIPTEUR (souvent la personne décédée)
  beneficiary_* = BÉNÉFICIAIRE / AYANT-DROIT / BÉNÉFICIAIRE DÉSIGNÉ
  Ne les inverse jamais. Si ambigu => laisser vide.

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
    "bank_iban": "MA64011640000012345678901278"
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
  "score": 80,
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
  "reason": "Certificat décès bien rempli, date décès < aujourd'hui."
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
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=self.groq_timeout,
                response_format={"type": "json_object"},
            )
            result = json.loads(chat.choices[0].message.content)
            result["doc_type"] = forced_doc_type
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

        if tech_report.get("potential_tampering"):
            fraud_signals.append(f"Suspicious editor: {tech_report.get('editor_detected')}")
        if tech_report.get("font_count", 0) > 8:
            fraud_signals.append(f"High font variety: {tech_report.get('font_count')} fonts")

        today = date.today()

        def _check_date_field(key: str, label: str) -> date | None:
            v = _norm_spaces(extracted.get(key, ""))
            if not v:
                return None
            ok, _ = validate_date_format(v)
            if not ok:
                fv["dates_format_valid"] = False
                format_errors.append(f"{label} invalide: {v}")
                return None
            d = _parse_date_any(v)
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
            # your rule: expiry must be < today
            if exp and exp >= today:
                format_errors.append("CNI invalide selon règle projet: date expiration doit être < date du jour.")

        # BANK rules
        elif dt == "BANK":
            holder = _norm_spaces(extracted.get("bank_account_holder", ""))
            iban = _norm_spaces(extracted.get("bank_iban", "")).upper().replace(" ", "")

            cb = re.sub(r"\D", "", extracted.get("bank_code_banque", ""))
            cv = re.sub(r"\D", "", extracted.get("bank_code_ville", ""))
            nc = re.sub(r"\D", "", extracted.get("bank_numero_compte", ""))
            kr = re.sub(r"\D", "", extracted.get("bank_cle_rib", ""))

            # assemble
            rib_24 = re.sub(r"\D", "", (cb + cv + nc + kr))
            extracted["bank_rib_code"] = rib_24

            if not holder:
                format_errors.append("bank_account_holder manquant.")

            if iban:
                ok, msg = validate_iban(iban)
                fv["iban_format_valid"] = bool(ok)
                if not ok:
                    format_errors.append(msg)

            if rib_24:
                ok, msg = validate_rib_morocco(rib_24)
                fv["rib_format_valid"] = bool(ok)
                if not ok:
                    format_errors.append(msg)
            else:
                fv["rib_format_valid"] = False
                format_errors.append("RIB manquant (assembler code banque + code ville + n° compte + clé).")

        # DEATH rules
        elif dt == "DEATH":
            _check_cne_field("deceased_cne", "CNE (décès)", ["DECE", "DECED", "CIN", "CNIE", "ID"])
            _check_date_field("deceased_birth_date", "Date naissance (décès)")
            dth = _check_date_field("death_date", "Date décès")
            # your rule: death date must be < today
            if dth and dth >= today:
                format_errors.append("Date décès invalide selon règle projet: doit être < date du jour.")

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
                        if computed_end >= today:
                            format_errors.append("Contrat invalide selon règle projet: date effet + durée doit être < date du jour.")

        # scoring / decision
        base_score = int(groq_result.get("score", 60) or 60)
        penalty = (len(format_errors) * 6) + (len(fraud_signals) * 10)
        final_score = max(0, base_score - penalty)
        groq_result["score"] = final_score

        groq_result["fraud_suspected"] = len(fraud_signals) > 0
        groq_result["fraud_signals"] = list(set(groq_result.get("fraud_signals", []) + fraud_signals))

        if tech_report.get("potential_tampering"):
            groq_result["decision"] = "REVIEW"
        elif final_score >= 90 and not groq_result["fraud_suspected"] and len(format_errors) == 0:
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