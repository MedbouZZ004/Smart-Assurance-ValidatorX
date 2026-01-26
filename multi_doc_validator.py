"""
Multi-Document Cross-Validation System for Insurance Claims
Validates individual documents and cross-validates data across documents
"""

import os
import fitz  # PyMuPDF
import easyocr
import json
import groq
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, List, Tuple, Any

load_dotenv()


class MultiDocValidator:
    """
    Validates multiple insurance documents individually and cross-validates their data.
    Document types: CNI/Passport, Death Certificate, Contract, RIB/IBAN, Proof of Residence
    """

    def __init__(self):
        self.reader = easyocr.Reader(['fr', 'en'])
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not found. Check your .env file.")
        self.client = Groq(api_key=api_key)

        # Define document type patterns and expected fields
        self.document_types = {
            "cni_passport": {
                "keywords": ["cni", "passport", "carte nationale", "identité", "nationalité"],
                "fields": ["nom", "prenom", "date_naissance", "numero_document", "date_expiration", "photo"]
            },
            "death_certificate": {
                "keywords": ["certificat", "décès", "death", "mort", "acte de décès"],
                "fields": ["nom_defunt", "date_deces", "lieu", "numero_acte"]
            },
            "insurance_contract": {
                "keywords": ["contrat", "assurance", "police", "souscripteur", "bénéficiaire"],
                "fields": ["numero_police", "souscripteur", "beneficiaires", "capital", "date_effet", "date_fin"]
            },
            "rib_iban": {
                "keywords": ["rib", "iban", "bic", "banque", "titulaire", "compte"],
                "fields": ["titulaire", "iban", "bic", "banque"]
            },
            "proof_residence": {
                "keywords": ["justificatif", "domicile", "residence", "adresse", "facture", "bail"],
                "fields": ["nom", "adresse", "date_justificatif"]
            }
        }

    def analyze_technical_integrity(self, doc, file_path):
        """Analyze document for fraud indicators: metadata and font consistency."""
        metadata = doc.metadata
        
        fraud_tools = ['canva', 'photoshop', 'illustrator', 'gimp', 'inkscape', 'adobe acrobat pro']
        creator = (metadata.get('creator') or "").lower()
        producer = (metadata.get('producer') or "").lower()
        is_suspicious_tool = any(tool in creator or tool in producer for tool in fraud_tools)
        
        fonts = []
        for page in doc:
            fonts.extend([f[3] for f in page.get_fonts()])
        unique_fonts = set(fonts)
        
        return {
            "suspicious_metadata": is_suspicious_tool,
            "editor_detected": creator if creator else producer,
            "font_count": len(unique_fonts),
            "potential_tampering": is_suspicious_tool or len(unique_fonts) > 6
        }

    def extract_all(self, file_path):
        """Extract text, structure, and technical integrity from document."""
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

    def detect_document_type(self, text: str) -> str:
        """Detect the type of document based on content."""
        text_lower = text.lower()
        
        for doc_type, config in self.document_types.items():
            if any(keyword in text_lower for keyword in config["keywords"]):
                return doc_type
        
        return "unknown"

    def compute_individual_confidence_score(self, doc_type: str, extracted_data: Dict, 
                                            tech_report: Dict, fraud_detected: bool) -> int:
        """
        Compute confidence score for a single document based on logical rules.
        
        Scoring Rules (0-100):
        - Base: 100 points
        - Fraud Detected: -50 points
        - Missing Critical Fields: -10 per missing field (max -40)
        - Poor Metadata: -10 if suspicious tools detected
        - Low Font Consistency: -5 if too many fonts (>6)
        
        Score interpretation:
        - 80-100: HIGH confidence
        - 60-79: MEDIUM confidence
        - 40-59: LOW confidence
        - 0-39: VERY LOW confidence
        """
        
        score = 100
        deductions = []
        
        # RULE 1: Fraud Detection (-50)
        if fraud_detected or tech_report.get("potential_tampering"):
            score -= 50
            deductions.append("Fraud detected (-50)")
        
        # RULE 2: Check for suspicious metadata (-10)
        if tech_report.get("suspicious_metadata"):
            score -= 10
            deductions.append("Suspicious metadata detected (-10)")
        
        # RULE 3: Font consistency (-5)
        font_count = tech_report.get("font_count", 0)
        if font_count > 6:
            score -= 5
            deductions.append(f"Too many fonts: {font_count} (-5)")
        
        # RULE 4: Critical fields presence by document type
        missing_fields = []
        critical_fields_map = {
            "cni_passport": ["name", "birth_date", "numero_document"],
            "death_certificate": ["deceased_name", "death_date", "lieu"],
            "insurance_contract": ["policy_number", "subscriber_name", "effective_date", "end_date"],
            "rib_iban": ["titulaire", "iban", "bic"],
            "proof_residence": ["name", "address"],
            "unknown": []
        }
        
        critical_fields = critical_fields_map.get(doc_type, [])
        for field in critical_fields:
            if not extracted_data.get(field) or not str(extracted_data.get(field)).strip():
                missing_fields.append(field)
        
        # Deduct 10 points per missing critical field (max -40)
        field_deduction = min(len(missing_fields) * 10, 40)
        if missing_fields:
            score -= field_deduction
            deductions.append(f"Missing critical fields: {', '.join(missing_fields)} (-{field_deduction})")
        
        # Ensure score doesn't go below 0
        score = max(score, 0)
        
        return score

    def validate_single_document(self, file_path: str) -> Dict[str, Any]:
        """
        Validate a single document and extract structured data.
        Returns detailed validation result with extracted fields.
        """
        
        text, structure, tech_report = self.extract_all(file_path)
        doc_type = self.detect_document_type(text)
        
        prompt = f"""
ROLE: Expert Insurance Auditor specialized in cross-claim validation.

TASK: Analyze this single document and extract key data fields.

DOCUMENT TYPE DETECTED: {doc_type}

TECHNICAL ANALYSIS:
- Fraud Alerts: {tech_report['potential_tampering']} (Tool: {tech_report['editor_detected']})
- Font Count: {tech_report['font_count']}
- Structure: {structure}

EXTRACTED TEXT (First 4000 chars):
{text[:4000]}

EXTRACTION INSTRUCTIONS based on document type:

1. **CNI/Passport**: Extract Name, First Name, Birth Date, Document Number, Expiration Date
2. **Death Certificate**: Extract Deceased Name, Death Date, Location, Act Number
3. **Insurance Contract**: Extract Policy Number, Subscriber Name, Beneficiary Names, Capital, Effective Date, End Date
4. **RIB/IBAN**: Extract Account Holder Name (titulaire), IBAN, BIC, Bank Name
5. **Proof of Residence**: Extract Name, Full Address, Document Date

VALIDATION RULES:
- All dates must be in DD/MM/YYYY format or extracted as-is
- Names should be standardized (remove extra spaces, uppercase first letters)
- Reject if fraud detected
- Extract all visible data, mark as N/A if not present

RESPOND IN JSON FORMAT ONLY:
{{
    "document_type": "detected_type",
    "is_valid": bool,
    "extracted_data": {{
        "name": "Full Name",
        "first_name": "First Name",
        "birth_date": "DD/MM/YYYY or N/A",
        "numero_document": "ID number",
        "deceased_name": "Name if death cert",
        "death_date": "DD/MM/YYYY or N/A",
        "lieu": "Location if death cert",
        "numero_acte": "Act number",
        "policy_number": "Policy # if contract",
        "subscriber_name": "Subscriber name",
        "beneficiary_names": ["Name1", "Name2"],
        "capital": "Amount if present",
        "effective_date": "DD/MM/YYYY",
        "end_date": "DD/MM/YYYY",
        "titulaire": "Account holder if RIB",
        "iban": "IBAN if present",
        "bic": "BIC if present",
        "bank_name": "Bank name",
        "address": "Full address if present"
    }},
    "fraud_detected": {tech_report['potential_tampering']},
    "data_completeness": "HIGH / MEDIUM / LOW",
    "reason": "Brief explanation in French"
}}
        """
        
        try:
            chat = self.client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"}
            )
            result = json.loads(chat.choices[0].message.content)
            
            # Compute confidence score using logical rules
            fraud_detected = result.get("fraud_detected", False) or tech_report.get("potential_tampering")
            confidence_score = self.compute_individual_confidence_score(
                doc_type, 
                result.get("extracted_data", {}),
                tech_report,
                fraud_detected
            )
            
            result["confidence_score"] = confidence_score
            return result
        
        except Exception as e:
            return {
                "document_type": "unknown",
                "is_valid": False,
                "confidence_score": 0,
                "extracted_data": {},
                "fraud_detected": True,
                "data_completeness": "LOW",
                "reason": f"Extraction error: {str(e)}"
            }

    def compute_cross_validation_score(self, validation_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compute score based on logical rules instead of arbitrary deductions.
        
        Scoring Rules:
        - Base Score: 100 points
        - Name Consistency: -20 per mismatch (max -60 for 3 mismatches)
        - Date Logic: -25 if death date outside contract period
        - Critical Fields: -15 if essential documents or fields missing
        - Fraud Detection: -50 if fraud indicators found
        - Confidence Level: -10 if overall confidence < 60%
        
        Final Score:
        - > 70: VALID (Accept)
        - 50-70: QUESTIONABLE (Investigate)
        - < 50: INVALID (Reject)
        """
        
        score = 100
        deductions = []
        
        # Extract key data from all documents
        deceased_name = None
        subscriber_name = None
        beneficiary_names = []
        account_holder = None
        death_date = None
        contract_start = None
        contract_end = None
        fraud_found = False
        low_confidence_docs = []
        missing_critical_docs = []
        missing_critical_fields = []
        
        for file_name, doc_result in validation_data.items():
            doc_type = doc_result.get("document_type", "unknown")
            extracted = doc_result.get("extracted_data", {})
            fraud_detected = doc_result.get("fraud_detected", False)
            confidence = doc_result.get("confidence_score", 0)
            
            # Track fraud
            if fraud_detected:
                fraud_found = True
            
            # Track low confidence
            if confidence < 60:
                low_confidence_docs.append(file_name)
            
            # Extract critical data by document type
            if doc_type == "death_certificate":
                deceased_name = extracted.get("deceased_name", "").strip()
                death_date = extracted.get("death_date")
                if not deceased_name:
                    missing_critical_fields.append(f"{file_name}: Missing deceased name")
                if not death_date:
                    missing_critical_fields.append(f"{file_name}: Missing death date")
            
            elif doc_type == "insurance_contract":
                subscriber_name = extracted.get("subscriber_name", "").strip()
                beneficiary_names = extracted.get("beneficiary_names", [])
                contract_start = extracted.get("effective_date")
                contract_end = extracted.get("end_date")
                policy_num = extracted.get("policy_number", "").strip()
                
                if not subscriber_name:
                    missing_critical_fields.append(f"{file_name}: Missing subscriber name")
                if not policy_num:
                    missing_critical_fields.append(f"{file_name}: Missing policy number")
                if not contract_start or not contract_end:
                    missing_critical_fields.append(f"{file_name}: Missing contract dates")
            
            elif doc_type == "rib_iban":
                account_holder = extracted.get("titulaire", "").strip()
                iban = extracted.get("iban", "").strip()
                
                if not account_holder:
                    missing_critical_fields.append(f"{file_name}: Missing account holder name")
                if not iban:
                    missing_critical_fields.append(f"{file_name}: Missing IBAN")
            
            elif doc_type == "cni_passport":
                if not extracted.get("name", "").strip():
                    missing_critical_fields.append(f"{file_name}: Missing name on ID")
            
            elif doc_type == "proof_residence":
                if not extracted.get("address", "").strip():
                    missing_critical_fields.append(f"{file_name}: Missing address")
        
        # Check critical documents presence
        doc_types_present = set()
        for file_name, doc_result in validation_data.items():
            doc_types_present.add(doc_result.get("document_type", "unknown"))
        
        # Critical documents for death claims: Death Cert + Contract + RIB
        critical_docs = {"death_certificate", "insurance_contract", "rib_iban"}
        missing_docs = critical_docs - doc_types_present
        
        if missing_docs:
            missing_critical_docs = list(missing_docs)
            deductions.append(f"Missing critical documents: {', '.join(missing_docs)}")
        
        # RULE 1: Fraud Detection (-50 points)
        if fraud_found:
            score -= 50
            deductions.append("Fraud detected in one or more documents (-50)")
        
        # RULE 2: Missing Critical Documents (-15 points)
        if missing_critical_docs:
            score -= 15
            deductions.append(f"Missing critical documents: {', '.join(missing_critical_docs)} (-15)")
        
        # RULE 3: Missing Critical Fields (-15 points)
        if missing_critical_fields:
            score -= 15
            deductions.append(f"Missing critical fields: {len(missing_critical_field)} fields (-15)")
        
        # RULE 4: Low Confidence Documents (-10 points)
        if low_confidence_docs:
            score -= 10
            deductions.append(f"Low confidence in: {', '.join(low_confidence_docs)} (-10)")
        
        # RULE 5: Name Consistency (-20 per mismatch, max -60)
        name_mismatches = []
        if deceased_name and subscriber_name:
            if deceased_name.lower() != subscriber_name.lower():
                name_mismatches.append("deceased_name ≠ subscriber_name")
        
        if beneficiary_names and account_holder:
            beneficiary_match = any(
                account_holder.lower() in b.lower() or b.lower() in account_holder.lower()
                for b in beneficiary_names
            )
            if not beneficiary_match:
                name_mismatches.append("beneficiary_names ≠ account_holder")
        
        # Apply name mismatch deductions (max -60)
        name_deduction = min(len(name_mismatches) * 20, 60)
        if name_mismatches:
            score -= name_deduction
            deductions.append(f"Name mismatches: {', '.join(name_mismatches)} (-{name_deduction})")
        
        # RULE 6: Date Logic Validity (-25 points)
        date_logic_valid = True
        date_issues = []
        
        if death_date and contract_start and contract_end:
            # Parse dates (simple format check: DD/MM/YYYY)
            try:
                from datetime import datetime
                death_dt = datetime.strptime(death_date.split()[0], "%d/%m/%Y") if death_date else None
                start_dt = datetime.strptime(contract_start.split()[0], "%d/%m/%Y") if contract_start else None
                end_dt = datetime.strptime(contract_end.split()[0], "%d/%m/%Y") if contract_end else None
                
                if death_dt and start_dt and end_dt:
                    if not (start_dt <= death_dt <= end_dt):
                        date_logic_valid = False
                        date_issues.append(f"Death date ({death_date}) outside contract period ({contract_start} to {contract_end})")
                        score -= 25
                        deductions.append(f"Death date outside contract validity (-25)")
            except Exception as e:
                date_logic_valid = False
                date_issues.append(f"Date parsing error: {str(e)}")
                score -= 25
                deductions.append(f"Invalid date format (-25)")
        
        # Ensure score doesn't go below 0
        score = max(score, 0)
        
        # Determine validation status
        if score >= 70:
            status = "VALID"
            recommendation = "ACCEPT"
        elif score >= 50:
            status = "QUESTIONABLE"
            recommendation = "INVESTIGATE"
        else:
            status = "INVALID"
            recommendation = "REJECT"
        
        # Build detailed reason
        if deductions:
            reason_text = "Calcul du score: " + " | ".join(deductions)
        else:
            reason_text = "Tous les critères de validation sont satisfaits."
        
        return {
            "is_valid": score > 50,
            "overall_score": score,
            "cross_validation_status": status,
            "score_breakdown": {
                "base_score": 100,
                "deductions": deductions,
                "final_score": score
            },
            "name_matches": {
                "deceased_vs_subscriber": deceased_name and subscriber_name and deceased_name.lower() == subscriber_name.lower(),
                "beneficiary_vs_account_holder": any(
                    account_holder.lower() in b.lower() or b.lower() in account_holder.lower()
                    for b in beneficiary_names
                ) if beneficiary_names and account_holder else None,
                "mismatches_found": name_mismatches
            },
            "date_logic_valid": date_logic_valid,
            "date_issues": date_issues,
            "critical_documents_present": len(missing_docs) == 0,
            "missing_documents": missing_critical_docs,
            "missing_fields": missing_critical_fields,
            "fraud_indicators": ["Fraud detected"] if fraud_found else [],
            "low_confidence_documents": low_confidence_docs,
            "discrepancies": deductions,
            "recommendation": recommendation,
            "detailed_reason": reason_text
        }

    def cross_validate_documents(self, documents: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Cross-validate multiple documents and compute score based on logical rules.
        
        Args:
            documents: Dict with file_name as key and validation result as value
        
        Returns:
            Cross-validation result with logical rule-based score
        """
        
        try:
            # Use logical rules to compute score
            return self.compute_cross_validation_score(documents)
        
        except Exception as e:
            return {
                "is_valid": False,
                "overall_score": 0,
                "cross_validation_status": "ERROR",
                "score_breakdown": {
                    "base_score": 100,
                    "deductions": [f"System error: {str(e)}"],
                    "final_score": 0
                },
                "name_matches": {},
                "date_logic_valid": False,
                "critical_documents_present": False,
                "missing_documents": [],
                "missing_fields": [],
                "fraud_indicators": ["System error during validation"],
                "low_confidence_documents": [],
                "discrepancies": [],
                "recommendation": "REJECT",
                "detailed_reason": f"Validation error: {str(e)}"
            }

    def process_document_batch(self, file_paths: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Process a batch of documents: validate individually, then cross-validate.
        
        Returns:
            Tuple of (individual_results, cross_validation_result)
        """
        individual_results = {}
        
        for file_path in file_paths:
            if not os.path.exists(file_path):
                continue
            
            file_name = os.path.basename(file_path)
            result = self.validate_single_document(file_path)
            individual_results[file_name] = result
        
        # Perform cross-validation
        cross_validation_result = self.cross_validate_documents(individual_results)
        
        return individual_results, cross_validation_result
