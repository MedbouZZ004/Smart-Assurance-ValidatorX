from fpdf import FPDF
from faker import Faker
import os

fake = Faker()
os.makedirs("test_set", exist_ok=True)

def create_doc(filename, content, include_table=True):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, "CAPGEMINI ASSURANCE SERVICES", ln=True, align='C')
    pdf.ln(10)
    
    pdf.set_font("Arial", size=12)
    for line in content:
        pdf.multi_cell(0, 10, line)
    
    if include_table:
        pdf.ln(5)
        pdf.cell(100, 10, "Policy Detail: " + fake.bothify(text='POL-#####'), border=1)
        pdf.cell(80, 10, "Status: ACTIVE", border=1)

    pdf.output(f"test_set/{filename}")

# 1. VALID DOC
create_doc("valid_policy.pdf", [
    f"Policy Holder: {fake.name()}",
    f"Address: {fake.address()}",
    f"Effective Date: 2024-01-01",
    f"Expiry Date: 2026-01-01",
    "Coverage: Total Vehicle Protection"
])

# 2. INVALID DOC (Missing Date & Structure)
create_doc("invalid_no_date.pdf", [
    f"Customer: {fake.name()}",
    "This is a note about a car accident but it has no policy number or dates."
], include_table=False)

# 3. GIBBERISH DOC
create_doc("spam_doc.pdf", [fake.text(max_nb_chars=500)], include_table=False)

print("Test set created in /test_set folder!")