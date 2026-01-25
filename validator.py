import os
import re
import fitz  # PyMuPDF
import easyocr
import json
import groq
from groq import Groq
from dotenv import load_dotenv

from utils import (
    validate_iban,
    validate_cin_morocco,
    validate_dates_coherence,
    validate_date_format,
    validate_rib_morocco,
)

load_dotenv()


class InsuranceValidator:
    """
    POLICY:
    - NEVER auto-reject
    - ONLY: ACCEPT or REVIEW
    """

    def __init__(self):
        self.reader = easyocr.Reader(["fr", "en"])
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY non trouvée ! Vérifiez votre fichier .env.")
        self.client = Groq(api_key=api_key)
        self.groq_timeout = 30

    # -----------------------------
    # Fraud / Integrity (PDF level)
    # -----------------------------
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

    # -----------------------------
    # OCR & Structure extraction
    # -----------------------------
    def extract_all(self, file_path: str):
        """
        PDF only (current). If you upload PNG/JPG, app should convert to PDF or you add support later.
        """
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

            pix = page.get_pixmap()
            text_results.extend(self.reader.readtext(pix.tobytes("png"), detail=0))

        return " ".join(text_results), structure, tech_report

# Penalize score if technical tampering is suspected
        final_score = int(analysis.get("score", 0))
        if tech["potential_tampering"]:
            final_score = max(0, final_score - 30)

        return {
            "category": analysis.get("category"),
            "nom": analysis.get("nom"),
            "score": final_score,
            "tech_report": tech
        }
    # -----------------------------
    # LLM extraction + validation
    # -----------------------------
    def validate_with_groq(self, text: str, structure: dict, tech_report: dict) -> dict:
        """
        OBJECTIF :
        1) Classifier le document...
        2) Extraire les noms complets SANS supprimer les particules (ex: garder 'El', 'Ait', 'Ben').
           Si le texte dit 'Sara El Idrissi', n'écris pas 'Sara Idrissi'.
        ...
        """

        """
        LLM extracts fields + proposes ACCEPT/REVIEW.
        Post-validation enforces policy and Morocco checks.
        """
        prompt = f"""
RÔLE : Auditeur Assurance (Contexte MAROC) - Dossiers épargne-vie / succession.
IMPORTANT : Tu ne rejettes JAMAIS. Décision = "ACCEPT" ou "REVIEW" seulement.

DONNÉES TECHNIQUES (SIGNAL UNIQUEMENT) :
- Structure : {structure}
- Suspicion technique : {tech_report.get('potential_tampering')} (éditeur : {tech_report.get('editor_detected')})
- Nombre de polices distinctes : {tech_report.get('font_count')}

TEXTE OCR (extrait) :
{text[:4500]}

OBJECTIF :
1) Classifier le document: ID / BANK / DEATH / LIFE_CONTRACT (ou UNKNOWN)
2) Extraire les champs requis
3) Proposer une décision "ACCEPT" seulement si champs critiques présents + formats OK + pas de signaux techniques

CHAMPS À EXTRAIRE :

[ID - CNI/PASSEPORT]
- beneficiary_name
- beneficiary_cin
- beneficiary_birth_date
- id_document_number
- id_expiry_date

[BANK - RIB/IBAN]
- bank_account_holder
- bank_iban (si présent)
- bank_rib (si présent)
- bank_bic
- bank_name

[DEATH - CERTIFICAT DE DÉCÈS]
- deceased_name
- death_date
- death_place
- death_act_number

[LIFE_CONTRACT - CONTRAT ÉPARGNE-VIE]
- policy_number
- subscriber_name
- beneficiary_name
- beneficiary_cin (si présent)
- contract_effective_date
- capital

Réponds UNIQUEMENT en JSON :
{{
  "decision": "ACCEPT" | "REVIEW",
  "score": 0,
  "country": "MAROC",
  "doc_type": "",

  "fraud_suspected": false,
  "fraud_signals": [],

  "dates_extracted": {{
    "start_date": "",
    "end_date": "",
    "dates_coherent": true
  }},

  "extracted_data": {{
    "insurer": "",
    "policy_number": "",
    "subscriber_name": "",
    "deceased_name": "",
    "beneficiary_name": "",
    "beneficiary_cin": "",
    "beneficiary_birth_date": "",
    "id_document_number": "",
    "id_expiry_date": "",
    "bank_account_holder": "",
    "bank_rib": "",
    "bank_iban": "",
    "bank_bic": "",
    "bank_name": "",
    "death_date": "",
    "death_place": "",
    "death_act_number": "",
    "contract_effective_date": "",
    "capital": ""
  }},

  "format_validation": {{
    "dates_format_valid": true,
    "rib_format_valid": true,
    "iban_format_valid": true,
    "cin_format_valid": true
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
            return self._validate_extracted_data(result, tech_report=tech_report)

        except groq.AuthenticationError:
            raise ValueError("Clé API GROQ invalide.")
        except Exception as e:
            # NEVER reject: errors => REVIEW
            return {
                "decision": "REVIEW",
                "is_valid": False,
                "score": 0,
                "country": "MAROC",
                "doc_type": "UNKNOWN",
                "fraud_suspected": False,
                "fraud_signals": [],
                "dates_extracted": {"start_date": "", "end_date": "", "dates_coherent": False},
                "extracted_data": {},
                "format_validation": {},
                "reason": f"Erreur API/système : {str(e)}",
            }

    # -----------------------------
    # Post validation (strict policy)
    # -----------------------------
    def _validate_extracted_data(self, groq_result: dict, tech_report: dict | None = None) -> dict:
        """
        Post-traitement (sans auto-reject):
        - Validations adaptées au type (ID/BANK/DEATH/LIFE_CONTRACT)
        - Tech signals => REVIEW
        """
        tech_report = tech_report or {}
        format_errors = []
        fraud_signals = []

        groq_result = groq_result or {}
        groq_result.setdefault("format_validation", {})
        groq_result.setdefault("extracted_data", {})
        groq_result.setdefault("dates_extracted", {})
        groq_result.setdefault("fraud_signals", [])
        groq_result.setdefault("country", "MAROC")
        groq_result.setdefault("doc_type", "UNKNOWN")

        extracted = groq_result.get("extracted_data", {}) or {}

        # normalize doc_type to one of 4
        raw_dt = (groq_result.get("doc_type") or "UNKNOWN").strip().upper()
        dt = raw_dt
        if "CNI" in dt or "CNIE" in dt or "PASSPORT" in dt or "PASSEPORT" in dt or "IDENT" in dt:
            dt = "ID"
        elif "RIB" in dt or "IBAN" in dt or "BANK" in dt or "BANQUE" in dt:
            dt = "BANK"
        elif "DECES" in dt or "DÉCÈS" in dt or "DEATH" in dt or "CERTIFICAT" in dt:
            dt = "DEATH"
        elif "ASSURANCE" in dt or "CONTRAT" in dt or "POLICE" in dt or "LIFE" in dt:
            dt = "LIFE_CONTRACT"
        elif dt not in ["ID", "BANK", "DEATH", "LIFE_CONTRACT"]:
            dt = "UNKNOWN"

        groq_result["doc_type"] = dt

        fv = groq_result["format_validation"]
        fv.setdefault("cin_format_valid", True)
        fv.setdefault("iban_format_valid", True)
        fv.setdefault("rib_format_valid", True)
        fv.setdefault("dates_format_valid", True)

        # tech fraud signals (signals only)
        if tech_report.get("potential_tampering"):
            fraud_signals.append(f"Suspicious editor: {tech_report.get('editor_detected')}")
        if tech_report.get("font_count", 0) > 8:
            fraud_signals.append(f"High font variety: {tech_report.get('font_count')} fonts")

        # dates coherence if both exist (optional)
        dates_ext = groq_result.get("dates_extracted", {}) or {}
        start = (dates_ext.get("start_date") or "").strip()
        end = (dates_ext.get("end_date") or "").strip()
        if start and end:
            is_coh, msg = validate_dates_coherence(start, end)
            groq_result["dates_extracted"]["dates_coherent"] = bool(is_coh)
            if not is_coh:
                format_errors.append(msg)

        # date format checks (only if present)
        def _check_date(field_key: str, label: str):
            v = (extracted.get(field_key) or "").strip()
            if not v:
                return
            ok, _ = validate_date_format(v)
            if not ok:
                fv["dates_format_valid"] = False
                format_errors.append(f"{label} invalide: {v}")

        _check_date("beneficiary_birth_date", "Date naissance")
        _check_date("id_expiry_date", "Date expiration ID")
        _check_date("death_date", "Date décès")
        _check_date("contract_effective_date", "Date effet contrat")

        # CIN validation: ONLY for ID/LIFE_CONTRACT
        cin = (extracted.get("beneficiary_cin") or "").strip()
        if dt in ["ID", "LIFE_CONTRACT"]:
            if cin:
                ok, msg = validate_cin_morocco(cin)
                fv["cin_format_valid"] = bool(ok)
                if not ok:
                    format_errors.append(msg)
            else:
                fv["cin_format_valid"] = False
                format_errors.append("CIN bénéficiaire manquant.")
        else:
            fv["cin_format_valid"] = True  # BANK/DEATH do not require CIN

        # IBAN/RIB validation: only if present (required at dossier-level by app)
        rib = (extracted.get("bank_rib") or "").strip()
        iban = (extracted.get("bank_iban") or "").strip()

        if iban:
            ok, msg = validate_iban(iban)
            fv["iban_format_valid"] = bool(ok)
            if not ok:
                format_errors.append(msg)
        else:
            fv["iban_format_valid"] = True

        if rib:
            ok, msg = validate_rib_morocco(rib)
            fv["rib_format_valid"] = bool(ok)
            if not ok:
                format_errors.append(msg)
        else:
            fv["rib_format_valid"] = True

        # Score adjustment
        original_score = int(groq_result.get("score", 60) or 60)
        penalty = (len(format_errors) * 6) + (len(fraud_signals) * 10)
        final_score = max(0, original_score - penalty)
        groq_result["score"] = final_score

        groq_result["fraud_suspected"] = len(fraud_signals) > 0
        groq_result["fraud_signals"] = list(set(groq_result.get("fraud_signals", []) + fraud_signals))

        # decision: never reject
        if tech_report.get("potential_tampering"):
            groq_result["decision"] = "REVIEW"

        if final_score >= 90 and not groq_result["fraud_suspected"] and len(format_errors) == 0:
            groq_result["decision"] = "ACCEPT"
        else:
            groq_result["decision"] = "REVIEW"

        # reason enrichment
        reasons = []
        if format_errors:
            reasons.append("Formats à vérifier: " + "; ".join(format_errors))
        if groq_result["fraud_suspected"]:
            reasons.append("Signaux techniques: " + "; ".join(groq_result["fraud_signals"]))

        if reasons:
            existing = (groq_result.get("reason") or "").strip()
            extra = " | ".join(reasons)
            groq_result["reason"] = (existing + " | " + extra).strip(" |")

        groq_result["is_valid"] = (groq_result["decision"] == "ACCEPT")
        return groq_result
