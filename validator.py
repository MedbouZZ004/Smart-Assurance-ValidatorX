import os
import fitz  # PyMuPDF
import easyocr
import json
import groq
from groq import Groq
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

class InsuranceValidator:
    def __init__(self):
        # Support du français et de l'anglais
        self.reader = easyocr.Reader(['fr', 'en'])
        
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY non trouvée ! Vérifiez votre fichier .env.")
        self.client = Groq(api_key=api_key)

    def analyze_technical_integrity(self, doc, file_path):
        """Couche C : Détection de fraude visuelle et métadonnées."""
        metadata = doc.metadata
        
        # Détection des outils de modification suspects
        fraud_tools = ['canva', 'photoshop', 'illustrator', 'gimp', 'inkscape', 'adobe acrobat pro']
        creator = (metadata.get('creator') or "").lower()
        producer = (metadata.get('producer') or "").lower()
        is_suspicious_tool = any(tool in creator or tool in producer for tool in fraud_tools)
        
        # Analyse des polices (un excès de polices différentes indique souvent un montage)
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
        """Extraction du texte, de la structure et de l'intégrité technique."""
        text_results = []
        structure = {"has_images": False, "page_count": 0, "has_tables": False}
        
        doc = fitz.open(file_path)
        structure["page_count"] = len(doc)
        
        # 1. Analyse Technique (Fraude)
        tech_report = self.analyze_technical_integrity(doc, file_path)
        
        # 2. Analyse Structurelle et OCR
        for page in doc:
            if len(page.get_images()) > 0: structure["has_images"] = True
            if len(page.get_drawings()) > 10: structure["has_tables"] = True
            
            pix = page.get_pixmap()
            text_results.extend(self.reader.readtext(pix.tobytes("png"), detail=0))
        
        return " ".join(text_results), structure, tech_report

    def validate_with_groq(self, text, structure, tech_report):
        """Analyse de conformité et validation métier (Mode Souple - Format Date uniquement)."""
        
        # On n'impose plus la date du jour pour le test
        
        prompt = f"""
        RÔLE : Auditeur Expert Assurance (Marché Français et Marocain).
        
        DONNÉES TECHNIQUES :
        - Structure : {structure}
        - Alertes Fraude : {tech_report['potential_tampering']} (Outil détecté : {tech_report['editor_detected']})
        - Nombre de polices distinctes : {tech_report['font_count']}
        
        TEXTE EXTRAIT DU DOCUMENT :
        {text[:4000]}
        
        MISSION : Analyser ce document d'assurance avec focus sur la STRUCTURE, la SYNTAXE des dates et la VALIDITÉ MÉTIER pour la France et le Maroc.
        
        CONTEXTE DOCUMENTAIRE :
        - France : Carte Verte, Attestation d'Assurance, Constat Amiable, Avis d'échéance.
        - Maroc : Attestation d'Assurance (souvent jaune/blanche), Carte Grise, Permis de Conduire, Constat Amiable. 
        - Assureurs fréquents (Maroc) : Wafa Assurance, RMA, Sanlam, AtlantaSanad, AXA Assurance Maroc, MAMDA/MCMA.
        - Assureurs fréquents (France) : AXA, Allianz, MAAF, MMA, GMF, Groupama.

        CRITÈRES DE VALIDATION :
        1. TYPE DE DOCUMENT : Doit être un document officiel d'assurance ou lié (Attestation, Carte Verte/Grise, Permis).
        2. VALIDITÉ TEMPORELLE (FORMAT UNIQUEMENT) : 
           - Extraire les dates de début et de fin.
           - Vérifier qu'elles respectent un format de date valide (JJ/MM/AAAA ou similaire).
           - Vérifier la logique interne : Date de début < Date de fin.
           - IMPORTANT : NE PAS COMPARER avec la date d'aujourd'hui. Même si la date est passée (2020, 2024...), le document est VALIDE pour ce test si les dates sont cohérentes entre elles.
        3. INTÉGRITÉ : 
           - Rejeter si 'potential_tampering' est True (Metadata suspectes).
           - Rejeter si incohérences manifestes dans le texte.
        4. MENTIONS LÉGALES : 
           - Présence d'un Assureur reconnu (FR ou MA).
           - Numéro de Police ou Immatriculation visible.
        
        Réponds UNIQUEMENT en JSON formaté ainsi :
        {{
            "is_valid": bool,
            "score": int (0-100),
            "country": "FRANCE / MAROC / INCONNU",
            "doc_type": "Nom du type de document identifié",
            "dates_extracted": {{
                "start_date": "dd/mm/yyyy",
                "end_date": "dd/mm/yyyy"
            }},
            "extracted_data": {{
                "insurer": "Nom Assureur",
                "policy_number": "Numéro Police",
                "client_or_vehicle": "Nom ou Immatriculation"
            }},
            "verdict_technique": "VALIDE / FORMAT_DATE_INVALIDE / FRAUDE / NON_CONFORME",
            "reason": "Explication claire en français incluant le pays détecté"
        }}
        
        RÈGLES DE SCORE :
        - Valide (Format dates OK + Cohérence + Assureur identifié) : > 85
        - Erreur de format date ou Incohérence (Fin < Début) : < 40
        - Fraude suspectée : < 20
        """
        
        try:
            chat = self.client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                # model="llama-3.2-11b-vision-preview",
                messages=[{"role": "user", "content": prompt}],
                temperature=0, 
                response_format={"type": "json_object"}
            )
            return json.loads(chat.choices[0].message.content)
        except groq.AuthenticationError:
            raise ValueError("Clé API GROQ invalide.")
        except Exception as e:
            return {"is_valid": False, "score": 0, "reason": f"Erreur système : {str(e)}"}