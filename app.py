import os
import re
import pandas as pd
import pdfplumber
from google import genais
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy_utils import EncryptedType
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
from datetime import datetime

app = Flask(__name__)
CORS(app)


client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

class AuditReport(db.Model):
    __tablename__ = "audit_report"
    id = db.Column(db.Integer, primary_key=True)
    industry = db.Column(db.String(50))
    revenue = db.Column(EncryptedType(db.Unicode, ENCRYPTION_KEY, AesEngine, 'pkcs5'))
    health_score = db.Column(db.Integer)
    loan_product = db.Column(db.String(100))
    report_en = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()


def mask_pii(text):
    if not text: return ""
    text = re.sub(r'(\+91|0)?[789]\d{9}', '[PHONE_HIDDEN]', text)
    text = re.sub(r'\b\d{9,18}\b', '[ACC_HIDDEN]', text)
    text = re.sub(r'[A-Z]{5}[0-9]{4}[A-Z]{1}', '[PAN_HIDDEN]', text)
    text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[EMAIL_HIDDEN]', text)
    return text


def run_financial_audit(income, expense, industry):
    margin = (income - expense) / income if income > 0 else 0
    benchmarks = {"Manufacturing": 0.25, "Retail": 0.12, "Services": 0.35, "Agri": 0.15}
    target = benchmarks.get(industry, 0.2)
    score = max(0, min(int((margin / target) * 100), 100)) 

    if score >= 85: loan = "SBI/HDFC - CGTMSE Collateral-Free"
    elif score >= 60: loan = "LendingKart - Working Capital (NBFC)"
    else: loan = "MUDRA Scheme - Govt Grant"
    
    runway = round((income / expense) * 30) if expense > 0 else 365
    return score, margin, loan, runway


def generate_ai_narrative(income, expense, margin, score, industry, runway, raw_data_sample):
    safe_data = mask_pii(raw_data_sample[:2000]) 
    
    prompt = (
        f"You are a Senior Financial SME Consultant. Write a high-value Strategic Audit Report for a business in the {industry} sector.\n\n"
        f"METRICS:\n"
        f"- Monthly Revenue: INR {income}\n"
        f"- Monthly Expenses: INR {expense}\n"
        f"- Profit Margin: {round(margin*100,2)}%\n"
        f"- Health Score: {score}/100\n"
        f"- Est. Cash Runway: {runway} Days\n\n"
        f"CONTEXTUAL DATA:\n{safe_data}\n\n"
        f"STRUCTURE YOUR RESPONSE EXACTLY LIKE THIS:\n"
        f"1. RISK ANALYSIS: Detailed breakdown of the score and burn rate.\n"
        f"2. 3-MONTH FORECAST: Specific predictions based on the runway.\n"
        f"3. GROWTH STRATEGY: One industry-specific cost saving and one expansion tip.\n"
        
    )

    try:
        
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        if response.text:
            return response.text.strip()
    except Exception as e:
        print(f"AI Connection Failed: {e}")

    
    return (
        f"EXECUTIVE AUDIT SUMMARY:\n"
        f"The business is operating in the {industry} sector with a health score of {score}/100. "
        f"With a monthly revenue of INR {income} and expenses of INR {expense}, the current "
        f"cash runway is approximately {runway} days. Strategy: Focus on reducing fixed costs."
    )

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files['file']
        industry = request.form.get('industry', 'Manufacturing')
        
        raw_sample = ""
        table_data = []

        if file.filename.endswith('.pdf'):
            with pdfplumber.open(file) as pdf:
                raw_sample = " ".join([page.extract_text() or "" for page in pdf.pages])
                for page in pdf.pages:
                    table = page.extract_table()
                    if table: table_data.extend(table)
                
                if not table_data:
                    return jsonify({"error": "No structured table data found in PDF"}), 400
                
                df = pd.DataFrame(table_data[1:], columns=table_data[0])
        else:
            df = pd.read_csv(file) if file.filename.endswith('.csv') else pd.read_excel(file)
            raw_sample = df.to_string()

        df.columns = [str(c).strip().lower() for c in df.columns]
        amt_col = next((c for c in df.columns if any(x in c for x in ['amount', 'amt', 'value', 'balance'])), None)
        
        if not amt_col: 
            return jsonify({"error": "Amount column not found"}), 400

        def clean_num(val):
            if val is None: return 0.0
            s = re.sub(r'[^\d.-]', '', str(val)) 
            try: return float(s)
            except: return 0.0

        df['clean_amt'] = df[amt_col].apply(clean_num)
        income = float(df[df['clean_amt'] > 0]['clean_amt'].sum())
        expense = float(abs(df[df['clean_amt'] < 0]['clean_amt'].sum()))

        if expense == 0 and income > 0: expense = income * 0.7

        score, margin, loan, runway = run_financial_audit(income, expense, industry)
        report_en = generate_ai_narrative(income, expense, margin, score, industry, runway, raw_sample)

        new_audit = AuditReport(
            industry=industry, revenue=str(income),
            health_score=score, loan_product=loan, report_en=report_en
        )
        db.session.add(new_audit)
        db.session.commit()

        return jsonify({
            "income": income, "expense": expense, "score": score, 
            "loan": loan, "report_en": report_en, "runway": runway
        })

    except Exception as e:
        db.session.rollback()
        print(f"Critical Backend Error: {e}") 
        return jsonify({"error": str(e)}), 500


    
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)