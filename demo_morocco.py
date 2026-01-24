from fpdf import FPDF
import os

def get_desktop_path():
    """Détermine le chemin du Bureau de l'utilisateur actuel."""
    return os.path.join(os.path.expanduser("~"), "Desktop")

def create_demo_pdf(filename, text_lines, creator="Adobe PDF Library"):
    # Définition du chemin complet vers le Bureau
    desktop = get_desktop_path()
    full_path = os.path.join(desktop, filename)
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    
    # Simulation de métadonnées pour la détection de fraude
    pdf.set_creator(creator) 
    
    for line in text_lines:
        try:
             # encode('latin-1', 'replace') handles characters not in latin-1 but FPDF basic only supports latin1. 
             # Ideally we use a unicode font but for a simple demo this is okay.
             # We will stick to basic characters or remove tricky ones.
             safe_line = line.encode('latin-1', 'replace').decode('latin-1')
             pdf.multi_cell(0, 10, safe_line)
        except Exception:
             pdf.multi_cell(0, 10, line)
    
    # Ajout d'un faux tableau pour la structure
    pdf.ln(10)
    pdf.cell(50, 10, "POL-MA-998877", border=1)
    pdf.cell(50, 10, "MAD 1250.00", border=1) 
    
    # Sauvegarde sur le Bureau
    pdf.output(full_path)
    print(f"✅ Fichier généré sur le Bureau : {full_path}")

# --- GÉNÉRATION DES SCÉNARIOS MAROC ---

# 1. SCÉNARIO : Attestation Marocaine Valide
create_demo_pdf("maroc_attestation_valide.pdf", [
    "ROYAUME DU MAROC",
    "ATTESTATION D'ASSURANCE AUTOMOBILE",
    "Assureur : Wafa Assurance",
    "Assuré : Ahmed Benali",
    "Immatriculation : 12345-A-10",
    "Période de garantie : du 15/01/2026 au 14/01/2027",
    "Usage : Tourisme",
    "Prime Totale : 2500.00 MAD"
])

# 2. SCÉNARIO : Carte Grise (Simulation Textuelle)
create_demo_pdf("maroc_carte_grise.pdf", [
    "ROYAUME DU MAROC",
    "CERTIFICAT D'IMMATRICULATION",
    "Numéro d'immatriculation : 55667-B-26",
    "Propriétaire : Société Transport Express",
    "Date de mise en circulation : 10/06/2020",
    "Validité de l'assurance : Non applicable ici (document administratif)"
])

# 3. SCÉNARIO : Fraude Détectée (Photoshop)
create_demo_pdf("maroc_fraude_photoshop.pdf", [
    "ATTESTATION D'ASSURANCE SCOLAIRE",
    "Assureur : RMA Watanya",
    "Assuré : Ecole Privée les Iris",
    "Validité : 01/09/2025 au 30/06/2026",
    "Note: Ce document a des métadonnées suspectes."
], creator="Adobe Photoshop CS6")
