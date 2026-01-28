"""
utils.py - Validateurs et utilitaires (MAROC focus) pour Life Savings / Succession Document Validator

Objectifs:
- Validation formats (CIN, dates, RIB, IBAN)
- Extraction candidats depuis texte OCR
- Fuzzy matching noms / identités entre documents
- Scoring de risque (IMPORTANT: l'IA ne rejette jamais, score = aide à décision)
"""

import re
from difflib import SequenceMatcher
from datetime import datetime


# =========================
# Helpers (normalisation)
# =========================

def _strip_spaces(s: str) -> str:
    return re.sub(r"\s+", "", (s or ""))

# In utils.py
def normalize_name(s: str) -> str:
    if not s: return ""
    s = s.lower().strip()

    replacements = {
        "é": "e", "è": "e", "ê": "e", "à": "a", "â": "a",
        "ù": "u", "û": "u", "ô": "o", "ö": "o", "ç": "c",
        "î": "i", "ï": "i"
    }

    # Force hyphens to become spaces
    s = s.replace("-", " ")

    # Remove all remaining non-alphanumeric characters
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# =========================
# VALIDATEURS DE FORMAT
# =========================

def validate_iban(iban_str: str) -> tuple:
    """
    Vérifie la validité d'un IBAN (checksum + format).
    Support : IBAN internationaux (FR, MA, etc.)
    Retourne : (is_valid, message)
    """
    if not iban_str:
        return False, "IBAN vide"

    iban = _strip_spaces(iban_str).upper()

    # Format IBAN: 2 lettres pays + 2 chiffres clé + jusqu'à 30 caractères
    if not re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$", iban):
        return False, f"Format IBAN invalide : {iban}"

    # Checksum IBAN (ISO 7064 mod 97)
    try:
        rearranged = iban[4:] + iban[:4]
        numeric = ""
        for char in rearranged:
            if char.isdigit():
                numeric += char
            else:
                numeric += str(ord(char) - ord("A") + 10)

        if int(numeric) % 97 == 1:
            return True, "IBAN valide"
        return False, "Checksum IBAN invalide"
    except Exception as e:
        return False, f"Erreur vérification IBAN : {str(e)}"


def validate_rib_morocco(rib_str: str) -> tuple:
    digits = re.sub(r"\D", "", rib_str)
    if len(digits) != 24:
        return True, "Un RIB marocain doit comporter 24 chiffres."

    # Checksum: (97 - (((97 + (bank_code_and_account % 97)) * 100) % 97))
    # Or more simply: (RIB_22_digits * 100 + key) % 97 == 0
    try:
        base = int(digits[:22])
        key = int(digits[22:])
        if (base * 100 + key) % 97 == 0:
            return True, "RIB valide"
        return False, "Clé RIB incorrecte"
    except ValueError:
        return False, "Format numérique invalide"

def validate_cin_morocco(cin_str: str) -> tuple:
    """
    CIN Maroc (pragmatique):
    Formats courants:
    - 1 à 2 lettres + 5 à 8 chiffres  (ex: CD936873, AB123456)
    Tolérance espaces.
    """
    if not cin_str:
        return False, "CIN vide"

    cin = _strip_spaces(cin_str).upper()

    # 1-2 lettres + 5-8 chiffres
    if re.match(r"^[A-Z]{1,2}[0-9]{5,8}$", cin):
        return True, "CIN Maroc valide"

    # fallback (ancien pattern)
    if re.match(r"^[0-9]{7,8}[A-Z]{0,2}$", cin):
        return True, "CIN valide (pattern alternatif)"

    return False, f"Format CIN invalide : {cin}"


def validate_date_format(date_str: str) -> tuple:
    if not date_str:
        return False, "Date vide"

    # Replace dots and spaces with slashes immediately
    date_str = re.sub(r"[.\s\-]", "/", date_str.strip())

    for fmt in ("%d/%m/%Y", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(date_str, fmt)
            # This ensures every date returned is in dd/mm/yyyy format
            return True, parsed.strftime("%d/%m/%Y")
        except ValueError:
            continue

    return False, f"Format date invalide : {date_str} (attendu: JJ/MM/AAAA)"


def validate_dates_coherence(start_date_str: str, end_date_str: str) -> tuple:
    """
    Vérifie que date_début < date_fin.
    Retourne : (is_coherent, message)
    """
    is_valid_start, formatted_start = validate_date_format(start_date_str)
    is_valid_end, formatted_end = validate_date_format(end_date_str)

    if not is_valid_start:
        return False, f"Date début invalide : {start_date_str}"
    if not is_valid_end:
        return False, f"Date fin invalide : {end_date_str}"

    try:
        start = datetime.strptime(formatted_start, "%d/%m/%Y")
        end = datetime.strptime(formatted_end, "%d/%m/%Y")
        if start < end:
            return True, "Dates cohérentes"
        return False, f"Incohérence : date_début ({formatted_start}) >= date_fin ({formatted_end})"
    except Exception as e:
        return False, f"Erreur comparaison dates : {str(e)}"


# =================================
# MATCHING & FUZZY (identité)
# =================================

def fuzzy_match_name(name1: str, name2: str, threshold: float = 0.8) -> tuple:
    """
    Compare 2 noms avec tolérance (fuzzy matching).
    Retourne : (match, score_0_to_1)
    """
    if not name1 or not name2:
        return False, 0.0

    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    score = SequenceMatcher(None, n1, n2).ratio()
    return (score >= threshold), score


def match_identity_across_docs(doc1_name: str, doc1_cin: str,
                               doc2_name: str, doc2_cin: str) -> dict:
    """
    Matche identité entre 2 documents (ex: CIN vs Contrat).
    Stratégie:
    - CIN exact match => très fort
    - Sinon fuzzy match sur noms
    """
    name_match, name_score = fuzzy_match_name(doc1_name, doc2_name, threshold=0.75)

    c1 = _strip_spaces(doc1_cin).upper() if doc1_cin else ""
    c2 = _strip_spaces(doc2_cin).upper() if doc2_cin else ""
    cin_exact = bool(c1 and c2 and c1 == c2)

    overall = cin_exact or (name_match and name_score > 0.85)
    details = f"Nom: {name_score:.2%}, CIN: {'✓' if cin_exact else '✗'}"

    return {
        "name_match": bool(name_match),
        "name_score": float(name_score),
        "cin_exact_match": bool(cin_exact),
        "overall_match": bool(overall),
        "details": details
    }


# =========================
# EXTRACTION depuis texte
# =========================

def extract_iban_from_text(text: str) -> list:
    """
    Extrait des IBAN potentiels du texte.
    Pattern : 2 lettres + 2 chiffres + alphanumérique.
    """
    cleaned = _strip_spaces(text).upper()
    pattern = r"[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}"
    ibans = re.findall(pattern, cleaned)
    return list(set(ibans))


def extract_rib_from_text(text: str) -> list:
    """
    Extrait des RIB potentiels.
    - France: souvent 23 chiffres
    - Maroc: affichage variable -> on récupère des blocs de chiffres longs
    """
    raw = text or ""

    # France classic: 23 digits
    ribs_fr = re.findall(r"\b[0-9]{23}\b", raw)

    # Generic long digit blocks (Morocco or formatted)
    digits = re.sub(r"\D", " ", raw)
    candidates = re.findall(r"\b[0-9]{20,34}\b", digits)

    return list(set(ribs_fr + candidates))


def extract_cin_candidates(text: str, country: str = "MAROC") -> list:
    """
    Extrait candidats CIN/pièce d'identité du texte.
    """
    cleaned = (text or "").upper()

    if country.upper() == "MAROC":
        pattern = r"\b[0-9]{7,8}[A-Z]{0,2}\b"
        candidates = re.findall(pattern, cleaned)
        return list(set(candidates))

    # fallback generic: numbers 13-15 digits or passport like pattern
    candidates = re.findall(r"\b[0-9]{13,15}\b|[0-9]{9}[A-Z]{2}[0-9]{2}", cleaned)
    return list(set(candidates))


# =========================
# SCORE & RISK (aide décision)
# =========================

def calculate_document_risk_score(
    has_valid_format: bool,
    has_tampering: bool,
    ocr_confidence: float = 0.8,
    format_errors: int = 0
) -> dict:
    """
    Calcule score de risque détaillé pour 1 document.
    IMPORTANT: Ce score ne fait pas un rejet automatique, il guide le tri (ACCEPT vs REVIEW).
    Retourne : {
        'risk_score': 0-100 (100 = très sûr),
        'breakdown': dict,
        'recommendation': str
    }
    """
    score = 100
    breakdown = {}

    # Format
    if has_valid_format:
        breakdown["format_valid"] = 20
    else:
        score -= 30
        breakdown["format_invalid"] = -30

    # Tampering signal
    if has_tampering:
        score -= 40
        breakdown["tampering_detected"] = -40

    # OCR confidence adjustment
    ocr_adjustment = int((ocr_confidence - 0.7) * 50)  # [-10, +30]
    score += ocr_adjustment
    breakdown["ocr_confidence_adjustment"] = ocr_adjustment

    # Format errors penalty
    score -= (format_errors * 5)
    breakdown["format_errors"] = -format_errors * 5

    final_score = max(0, min(100, score))

    # Recommendation aligned with "no auto-reject"
    if final_score >= 85:
        rec = "AUTO-ACCEPT (confiance haute)"
    else:
        rec = "HUMAN REVIEW (à vérifier)"

    return {
        "risk_score": final_score,
        "breakdown": breakdown,
        "recommendation": rec
    }
# in app.py or utils.py


def advanced_name_match(name1: str, name2: str) -> float:
    n1 = normalize_name(name1).split()
    n2 = normalize_name(name2).split()

    if not n1 or not n2:
        return 0.0

    # Intersection of words
    set1, set2 = set(n1), set(n2)
    intersection = set1.intersection(set2)

    # If a particle like 'el' is in one but missing in the other,
    # the intersection score will naturally drop.
    score = len(intersection) / max(len(set1), len(set2))
    return score
