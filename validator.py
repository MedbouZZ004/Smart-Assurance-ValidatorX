import os, fitz, easyocr, json, groq
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

class InsuranceValidator:
    def __init__(self):
        # Initializing the OCR engine
        self.reader = easyocr.Reader(['fr', 'en'])

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY non trouvée !")
        self.client = Groq(api_key=api_key)

    def analyze_technical_integrity(self, doc):
        """Checks for metadata tampering (Canva, Photoshop, etc.) """
        metadata = doc.metadata
        fraud_tools = ['canva', 'photoshop', 'illustrator', 'gimp', 'adobe acrobat pro']
        creator = (metadata.get('creator') or "").lower()
        producer = (metadata.get('producer') or "").lower()
        is_suspicious = any(tool in creator or tool in producer for tool in fraud_tools)

        fonts = []
        for page in doc:
            fonts.extend([f[3] for f in page.get_fonts()])

        return {
            "potential_tampering": is_suspicious or len(set(fonts)) > 6,
            "editor_detected": creator if creator else producer
        }

    def extract_all(self, file_path):
        """Extracts raw text and technical metadata """
        doc = fitz.open(file_path)
        text_results = []

        tech_report = self.analyze_technical_integrity(doc)

        for page in doc:
            pix = page.get_pixmap()
            text_results.extend(self.reader.readtext(pix.tobytes("png"), detail=0))
        print(f"--- DEBUG OCR {file_path} ---\n{text_results}")

        return " ".join(text_results), tech_report

    def cross_validate_claim(self, data_bundle):
            """The core audit logic with strict classification and name matching."""
            prompt = f"""
            ROLE : Auditeur de Fraude en Assurance (NIVEAU DE RIGUEUR : MAXIMUM).

            VÉRIFIEZ CES 4 DOCUMENTS :
            1. CONTRAT : {data_bundle['contract']['text']}
            2. CERTIFICAT DE DÉCÈS : {data_bundle['death_cert']['text']}
            3. ID : {data_bundle['id_card']['text']}
            4. RIB : {data_bundle['rib']['text']}

            PROTOCOLE DE VÉRIFICATION MAROC (STRICT) :
                    1. CLASSIFICATION : REJETTE immédiatement si l'ID est une 'CARTE ETUDIANT'.
                    2. RÉSOLUTION D'IDENTITÉ (NOMS) :
                       - SOYEZ EXTRÊMEMENT STRICT : 'Sara El Idrissi' et 'Sara Idrissi' sont des personnes DIFFÉRENTES. Toute absence ou présence du 'El' est une anomalie majeure.
                       - Ne jamais inventer de noms (ex: pas de 'Doha').
                    3. LOGIQUE TEMPORELLE (CONTRAT À DURÉE INDÉTERMINÉE) :
                       - Si aucune 'Date de Fin' n'est trouvée dans le contrat, considérez-le comme un contrat à durée indéterminée (Vie entière).
                       - Une anomalie n'existe QUE si la Date de Décès est AVANT la Date de Début.
                       - Si $Date\_Deces \ge Date\_Debut$, marquez comme 'LOGIQUE RESPECTÉE'.


            RÉPONDEZ UNIQUEMENT EN JSON :
            {{
                "is_valid": bool,
                "score": int,
                "verdict": "APPROUVÉ" | "REJETÉ",
                "reason": "Explication détaillée (ex: 'Document ID non conforme : Carte Etudiant refusée')",
                "mismatches": ["Liste précise des anomalies trouvées"]
            }}
            """

            try:
                chat = self.client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0, # On reste à 0 pour éviter toute créativité de l'IA
                    response_format={"type": "json_object"}
                )
                return json.loads(chat.choices[0].message.content)
            except Exception as e:
                return {"is_valid": False, "score": 0, "reason": f"Erreur système : {str(e)}"}