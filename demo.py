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
        pdf.multi_cell(0, 10, line)
    
    # Ajout d'un faux tableau pour la structure (SIREN valide pour le test)
    pdf.ln(10)
    pdf.cell(50, 10, "POL-998877", border=1)
    pdf.cell(50, 10, "SIREN: 123456782", border=1) 
    
    # Sauvegarde sur le Bureau
    pdf.output(full_path)
    print(f"✅ Fichier généré sur le Bureau : {full_path}")

# --- GÉNÉRATION DES SCÉNARIOS ---

# 1. SCÉNARIO : SUCCÈS (Document propre)
create_demo_pdf("attestation_valide.pdf", [
    "ATTESTATION D'ASSURANCE HABITATION",
    "Assureur : Capgemini Assurance France",
    "Assuré : Jean Dupont",
    "Validité : du 01/01/2025 au 01/01/2027",
    "Mention légale : Conforme RGPD et Loi Hamon."
])

# 2. SCÉNARIO : ÉCHEC LOGIQUE (Date expirée)
create_demo_pdf("attestation_expiree.pdf", [
    "ATTESTATION D'ASSURANCE AUTO",
    "Assuré : Marc Durand",
    "Période de garantie : du 01/01/2020 au 01/01/2021",
    "Statut : Contrat Résilié"
])

# 3. SCÉNARIO : ÉCHEC SÉCURITÉ (Fraude Canva)
create_demo_pdf("fraude_canva.pdf", [
    "ATTESTATION D'ASSURANCE SCOLAIRE",
    "Assuré : Petit Paul",
    "Date de modification : 24/01/2026",
    "Ce texte semble parfait, mais l'analyse technique verra l'outil d'édition."
], creator="Canva Design Tool")