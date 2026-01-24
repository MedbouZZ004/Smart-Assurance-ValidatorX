import os
import easyocr
import fitz
import json
import groq
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

class InsuranceValidator:
    def __init__(self):
        self.reader = easyocr.Reader(['en', 'fr'])

        api_key = os.getenv("GROQ_API_KEY")
        # 2. Add a safety check to see if the key actually exists
        if not api_key:
            raise ValueError("GROQ_API_KEY not found! Check your .env file.")
        self.client = Groq(api_key=api_key)

    def extract_all(self, file_path):
        """Extracts both Text and Structural metadata."""
        text_results = []
        structure = {"has_images": False, "page_count": 0, "has_tables": False}
        
        doc = fitz.open(file_path)
        structure["page_count"] = len(doc)
        
        for page in doc:
            # Structure check
            if len(page.get_images()) > 0: structure["has_images"] = True
            if len(page.get_drawings()) > 10: structure["has_tables"] = True
            
            # OCR Text extraction
            pix = page.get_pixmap()
            text_results.extend(self.reader.readtext(pix.tobytes("png"), detail=0))
        
        return " ".join(text_results), structure

    def validate_with_groq(self, text, structure):
        prompt = f"""
        Audit this document:
        STRUCTURE: {structure}
        TEXT: {text[:3000]}
        
        RULES:
        1. Must be insurance-related.
        2. Must have a Policy Number and Expiry Date.
        3. Reject if text is gibberish or non-insurance.
        
        Return JSON: {{"is_valid": bool, "score": int, "reason": "str"}}
        """
        try:
            chat = self.client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
        except groq.AuthenticationError as e:
            raise ValueError("Invalid GROQ API key. Set a valid `GROQ_API_KEY` in your environment or .env and restart the app.") from e
        except Exception:
            raise

        return json.loads(chat.choices[0].message.content)