import os
import re
import json
import fitz  # PyMuPDF
import easyocr
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime, date, timedelta

from utils import (
    validate_iban,
    validate_date_format,
    validate_rib_morocco,
)

from image_preprocess import preprocess_image_bytes

load_dotenv()

# ===============================
# Helpers
# ===============================

def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ\s\-']", " ", s)
    return _norm_spaces(s)

def _normalize_cne(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

def _is_cne_strict(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}\d{6}", _normalize_cne(s)))

def _parse_date_any(s: str):
    s = _norm_spaces(s)
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

def _parse_duration_to_timedelta(s: str):
    s = (s or "").lower()
    y = sum(map(int, re.findall(r"(\d+)\s*ans?", s)))
    m = sum(map(int, re.findall(r"(\d+)\s*mois", s)))
    d = sum(map(int, re.findall(r"(\d+)\s*jours?", s)))
    if not (y or m or d):
        return None
    return timedelta(days=y * 365 + m * 30 + d)

def _extract_cne_by_context(text: str, keywords: list[str]) -> str:
    t = (text or "").upper()
    matches = re.findall(r"\b[A-Z]{2}\s*[-]?\s*\d{6}\b", t)
    matches = [_normalize_cne(x) for x in matches if _is_cne_strict(x)]
    if not matches:
        return ""
    if not keywords:
        return matches[0]
    for m in re.finditer(r"\b[A-Z]{2}\s*[-]?\s*\d{6}\b", t):
        left = t[max(0, m.start() - 120):m.start()]
        if any(k.upper() in left for k in keywords):
            return _normalize_cne(m.group(0))
    return matches[0]

# ===============================
# Main class
# ===============================

class InsuranceValidator:
    """
    NEVER auto-reject
    ONLY: ACCEPT or REVIEW
    """

    def __init__(self):
        self.reader_fr = easyocr.Reader(["fr", "en"])
        self.reader_ar = easyocr.Reader(["en", "ar"])

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY manquant dans .env")
        self.client = Groq(api_key=api_key)

    # ===============================
    # OCR
    # ===============================

    def extract_all(self, file_path: str, file_bytes: bytes | None = None):
        ext = os.path.splitext(file_path)[1].lower()

        # ---------- IMAGE MODE ----------
        if ext in [".png", ".jpg", ".jpeg", ".webp"] and file_bytes:
            img = preprocess_image_bytes(file_bytes)
            text = []
            text.extend(self.reader_fr.readtext(img, detail=0))
            text.extend(self.reader_ar.readtext(img, detail=0))
            return " ".join(text), {
                "has_images": True,
                "page_count": 1,
                "has_tables": False
            }, {
                "potential_tampering": False,
                "editor_detected": "image_upload",
                "font_count": 0
            }

        # ---------- PDF MODE ----------
        text_results = []
        structure = {"has_images": False, "page_count": 0, "has_tables": False}

        doc = fitz.open(file_path)
        structure["page_count"] = len(doc)

        for page in doc:
            if page.get_images():
                structure["has_images"] = True
            if len(page.get_drawings()) > 10:
                structure["has_tables"] = True

            pix = page.get_pixmap()
            img_bytes = pix.tobytes("png")

            text_results.extend(self.reader_fr.readtext(img_bytes, detail=0))
            text_results.extend(self.reader_ar.readtext(img_bytes, detail=0))

        return " ".join(text_results), structure, {
            "potential_tampering": False,
            "editor_detected": doc.metadata.get("producer"),
            "font_count": sum(len(p.get_fonts()) for p in doc)
        }

    # ===============================
    # LLM
    # ===============================

    def validate_with_groq(self, text, structure, tech_report, forced_doc_type):
        forced_doc_type = forced_doc_type.upper()

        prompt = f"""
Tu es un auditeur assurance MAROC.
NE JAMAIS REJETER.
Décision = ACCEPT ou REVIEW.

TYPE FORCÉ: {forced_doc_type}

Règles clés:
- CNE strict: 2 lettres + 6 chiffres
- insured_* = assuré (souvent décédé)
- beneficiary_* = bénéficiaire
- Ne jamais inverser

TEXTE OCR:
{text[:6500]}

JSON STRICT uniquement.
"""

        res = self.client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )

        data = json.loads(res.choices[0].message.content)
        data["doc_type"] = forced_doc_type
        return self._post_validate(data, text)

    # ===============================
    # Post validation
    # ===============================

    def _post_validate(self, data, raw_text):
        extracted = data.get("extracted_data", {})
        errors = []
        today = date.today()

        # Clean names
        for k in extracted:
            if "name" in k:
                extracted[k] = _clean_name(extracted[k])

        # Normalize CNE
        for k in extracted:
            if "cne" in k and extracted[k]:
                extracted[k] = _normalize_cne(extracted[k])
                if not _is_cne_strict(extracted[k]):
                    errors.append(f"{k} invalide")

        # Date logic
        for k in extracted:
            if "date" in k and extracted[k]:
                d = _parse_date_any(extracted[k])
                if not d:
                    errors.append(f"{k} illisible")
                elif "death" in k and d > today:
                    errors.append("Date décès future")

        score = max(0, 60 - len(errors) * 6)
        data["score"] = score
        data["decision"] = "ACCEPT" if score >= 90 else "REVIEW"
        data["reason"] = "; ".join(errors)
        data["extracted_data"] = extracted
        return data
