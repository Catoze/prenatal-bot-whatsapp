import os
import re
import json
import sqlite3
from datetime import datetime, date
from dateutil import parser as dateparser
from flask import Flask, request, Response, render_template
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from flask import render_template, render_template_string

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
load_dotenv()
app = Flask(__name__, template_folder="templates")

DB_PATH = os.getenv("DB_PATH", "prenatal.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            phone TEXT PRIMARY KEY,
            state INTEGER NOT NULL,
            data TEXT NOT NULL,
            consented INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            data TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            ga_weeks INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------
# Educational/FAQ (resumos dos tópicos; usuário envia "? termo")
# ---------------------------------------------------------------------
def _t(s):  # minify multiline text
    return re.sub(r"[ \t]+\n", "\n", s.strip())

FAQ_TOPICS = {
    ("primeira consulta", "primeira vez", "começar", "iniciar"): _t("""
        *Primeira consulta de pré-natal*
        • Anamnese, PA, peso/altura (IMC), exame físico
        • Exames iniciais: hemograma, tipagem/Rh, glicemia, sorologias, urina/urocultura
        • Orientações: ácido fólico, vacinas, calendário e sinais de alerta
    """),
    ("consultas", "calendário", "frequência", "quantas consultas"): _t("""
        *Calendário de consultas*
        • Até 34s: mensais | 34–36s: quinzenais | >36s: semanais
        • Mínimo recomendado: 6 consultas
    """),
    ("alimentação", "dieta", "nutrição", "comida", "peso"): _t("""
        *Alimentação na gestação*
        • Refeições fracionadas, hidratação adequada
        • Evitar carnes/ovos crus, álcool e excesso de cafeína
    """),
    ("sintomas", "enjoo", "azia", "constipação", "dor nas costas", "inchaço"): _t("""
        *Sintomas comuns e alívio*
        • Náuseas/azia/constipação/dor lombar/edema: medidas não farmacológicas
        • Procure serviço se dor intensa, sangramento, febre, cefaleia forte
    """),
    ("sinais de alerta", "emergência", "perigo"): _t("""
        *Sinais de alerta (procure serviço imediatamente / 192 SAMU)*
        • Sangramento, dor abdominal forte, febre, perda de líquido
        • Diminuição dos movimentos fetais, cefaleia intensa com visão turva
    """),
    ("vacina", "vacinação", "imunização"): _t("""
        *Vacinas*
        • dTpa (20–36s), Influenza (anual), Hepatite B e COVID-19 conforme indicação
        • Contraindicadas: tríplice viral, varicela
    """),
    ("exames", "ultrassom", "laboratório", "sangue", "urina"): _t("""
        *Exames por trimestre (resumo)*
        • 1º: hemograma, tipagem/Rh, glicemia, sorologias, urina/urocultura, US obstétrico
        • 2º: TOTG 24–28s, US morfológico
        • 3º: hemograma, sorologias de controle, cultura EGB 35–37s
    """),
    ("diabetes", "glicose", "totg", "açúcar"): _t("""
        *Diabetes gestacional*
        • Rastreamento com TOTG 75g (24–28s); dieta, exercícios e, se preciso, insulina
    """),
    ("pressão alta", "hipertensão", "pré-eclâmpsia", "eclâmpsia"): _t("""
        *Pressão na gravidez*
        • PA ≥140/90 após 20s pede avaliação
        • Sinais graves: cefaleia forte, escotomas, dor epigástrica, edema súbito
    """),
    ("parto prematuro", "contrações", "antes da hora"): _t("""
        *Trabalho de parto prematuro*
        • Contrações regulares <37s, dor lombar, pressão pélvica, sangramento/perda de líquido
    """),
    ("faixa etária", "idade materna", "adolescente", "gravidez após 35"): _t("""
        *Faixa etária e riscos*
        • <18 anos ou ≥35 anos podem ter maior chance de alguns eventos obstétricos
        • Não é diagnóstico; significa acompanhamento mais próximo e atento
    """),
}

FAQ_MENU = _t("""
*Ajuda/Informações* — envie:
• `? primeira consulta` • `? consultas` • `? alimentação`
• `? sintomas` • `? sinais de alerta` • `? vacinação`
• `? exames` • `? diabetes` • `? pressão alta`
• `? parto prematuro` • `? faixa etária`
(Use `MENU` para ver esta lista; `CONTINUAR` volta ao questionário.)
""")

def answer_faq(text: str) -> str | None:
    t = text.lower().strip()
    if t.startswith("?"):
        t = t[1:].strip()
    for keys, msg in FAQ_TOPICS.items():
        if any(k in t for k in keys):
            return msg
    return None

# ---------------------------------------------------------------------
# Questionnaire & helpers
# ---------------------------------------------------------------------
SEVERE_SYMPTOM_IDS = {"1","2","3","4","6"}  # flags for urgent care

WELCOME = (
   "Oi! Sou o assistente Pré-Natal. Digite ACEITO para começar."
)

CONSENT_CONFIRMED = "Obrigado. Consentimento registrado. Vamos começar com algumas perguntas rápidas."

QUESTIONS = {
    1: "1) Para preservar a privacidade, informe apenas *iniciais* do seu nome (ex.: A.R.M.).",
    2: "2) Qual sua *idade* em anos? (ex.: 28)",
    3: (
        "3) Informe a *data da última menstruação (DUM)* em *DD/MM/AAAA*\n"
        "   *ou* digite apenas as *semanas de gestação* (ex.: 22)."
    ),
    4: (
        "4) Você apresenta algum(s) *sintoma(s) agora*? Responda com os números (ex.: 1,3):\n"
        "1 Sangramento vaginal\n"
        "2 Dor abdominal intensa\n"
        "3 Febre (≥ 38°C)\n"
        "4 Dor de cabeça forte / visão turva / inchaço súbito\n"
        "5 Náusea/vômito persistente\n"
        "6 Ausência de movimentos fetais (> 28s)\n"
        "7 Nenhum dos anteriores"
    ),
    5: (
        "5) Possui alguma *condição de saúde*? (números, ex.: 1,4)\n"
        "1 Hipertensão\n"
        "2 Diabetes\n"
        "3 Infecção urinária atual\n"
        "4 Nenhuma"
    ),
    6: "6) Quantas *consultas de pré-natal* você já realizou nesta gestação? (ex.: 3)",
    7: (
        "7) Você consegue informar sua *pressão arterial* agora?\n"
        "   Envie como *12x8*, *12/8*, *12 8* ou *120/80* (ou digite *PULAR*)."
    ),
    8: "8) Informe seu *peso em kg* (ex.: 70). Se não souber, digite *PULAR*.",
    9: "9) Informe sua *altura em metros* (ex.: 1.60). Se não souber, digite *PULAR*.",
    10: "10) Você usa *tabaco* ou *álcool* atualmente? Responda *1* Sim ou *2* Não.",
}

FINAL_MSG = "Obrigado. Avaliando suas respostas…"

EDU_MSG = (
    "Deseja receber *material educativo* (dicas de sinais de alerta e calendário de consultas)?\n"
    "Responda 1 para *Sim* ou 2 para *Não*."
)

EDU_CONTENT = (
    "*Sinais de alerta* (procure serviço imediatamente): sangramento, dor forte, febre ≥38°C, "
    "dor de cabeça intensa/visão turva/inchaço súbito, ausência de movimentos fetais após 28s.\n\n"
    "*Rotina*: mantenha o calendário de consultas do pré-natal e exames recomendados.\n"
    "Em dúvida, procure sua unidade de referência. Emergência: 192."
)

def parse_dum_or_weeks(text):
    text = text.strip()
    # Try weeks as number
    try:
        w = int(text)
        if 0 <= w <= 45:
            return w
    except ValueError:
        pass
    # Try date dd/mm/yyyy
    try:
        dt = datetime.strptime(text, "%d/%m/%Y").date()
        days = (date.today() - dt).days
        weeks = days // 7
        if 0 <= weeks <= 45:
            return weeks
    except Exception:
        try:
            dt = dateparser.parse(text, dayfirst=True).date()
            days = (date.today() - dt).days
            weeks = days // 7
            if 0 <= weeks <= 45:
                return weeks
        except Exception:
            return None
    return None

def parse_bp(text):
    """
    Aceita: "12x8", "12/8", "12 8", "120/80".
    Se valores forem estilo '12' e '8', converte para 120/80.
    Retorna (sistólica, diastólica) ou (None, None).
    """
    t = text.lower().replace(",", ".").strip()
    # normaliza separadores
    t = t.replace("x", "/").replace(" ", "/")
    m = re.search(r"(\d{2,3})\s*/\s*(\d{1,3})", t)
    if not m:
        return None, None
    try:
        s = int(m.group(1))
        d = int(m.group(2))
        # Se veio "12/8", multiplica por 10 -> 120/80
        if s < 30 and d < 30:
            s *= 10
            d *= 10
        if 60 <= s <= 260 and 30 <= d <= 180:
            return s, d
    except Exception:
        pass
    return None, None

def parse_kg(text):
    """retorna peso em kg (float) se válido; aceita '70', '70,5', '70kg'."""
    t = text.lower().replace(",", ".")
    m = re.search(r"(\d+(\.\d+)?)", t)
    if not m:
        return None
    try:
        w = float(m.group(1))
        if 30 <= w <= 250:
            return round(w, 1)
    except Exception:
        pass
    return None

def parse_meters(text):
    """retorna altura em metros (float) se válido; aceita '1.60', '1,60', '1.60m'."""
    t = text.lower().replace(",", ".")
    m = re.search(r"(\d+(\.\d+)?)", t)
    if not m:
        return None
    try:
        h = float(m.group(1))
        if 1.3 <= h <= 2.2:
            return round(h, 2)
    except Exception:
        pass
    return None

def classify_risk(record):
    """Return (risk_level, rationale)"""
    age = record.get("idade")
    weeks = record.get("ga_weeks")
    sintomas = set(record.get("sintomas_ids", []))
    comorb = set(record.get("comorb_ids", []))
    sys = record.get("pa_sys")
    dia = record.get("pa_dia")
    imc = record.get("imc")
    habitos = record.get("habitos")  # 'sim'/'nao'

    # Emergente
    if sintomas & SEVERE_SYMPTOM_IDS:
        return ("EMERGENTE", "Sintoma(s) de alerta reportado(s). Orientar ida IMEDIATA ao serviço / 192.")
    if sys and dia and (sys >= 160 or dia >= 110):
        return ("EMERGENTE", "Pressão arterial muito elevada (≥160/110). Procurar emergência.")

    # Priority
    priority_reasons = []
    try:
        if age is not None and (age < 18 or age >= 35):
            priority_reasons.append("Faixa etária (<18 ou ≥35) pode elevar riscos obstétricos; acompanhamento mais próximo é recomendado.")
    except Exception:
        pass

    if {"1","2","3"} & comorb:
        priority_reasons.append("Comorbidade (hipertensão/diabetes/ITU).")
    if weeks is not None and weeks >= 28 and "6" in sintomas:
        priority_reasons.append("Queixa sobre movimentos fetais no 3º trimestre.")
    if sys and dia and (sys >= 140 or dia >= 90):
        priority_reasons.append("Pressão arterial elevada (≥140/90).")
    if imc and imc >= 30:
        priority_reasons.append("IMC elevado (≥30).")
    if habitos == "sim":
        priority_reasons.append("Uso de tabaco/álcool (risco gestacional).")

    if priority_reasons:
        return ("PRIORITÁRIO", "; ".join(priority_reasons) + " Para saber mais, envie `? faixa etária` ou `? pressão alta`. Orientar avaliação em breve (hoje/amanhã).")

    return ("ROTINA", "Sem sinais de alerta no momento. Manter acompanhamento de pré-natal e orientações gerais.")

def get_session(phone):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.close()
    return row

def save_session(phone, state, data, consented):
    now = datetime.utcnow().isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions(phone, state, data, consented, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(phone) DO UPDATE SET state=excluded.state, data=excluded.data,
        consented=excluded.consented, updated_at=excluded.updated_at
    """, (phone, state, json.dumps(data, ensure_ascii=False), consented, now, now))
    conn.commit()
    conn.close()

def end_session(phone):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()

def store_response(phone, data, risk_level, ga_weeks):
    now = datetime.utcnow().isoformat()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO responses(phone, data, risk_level, ga_weeks, created_at)
        VALUES(?,?,?,?,?)
    """, (phone, json.dumps(data, ensure_ascii=False), risk_level, ga_weeks, now))
    conn.commit()
    conn.close()

def twiml(message):
    r = MessagingResponse()
    r.message(message)
    return Response(str(r), mimetype="application/xml")

# ---------------------------------------------------------------------
# Home & Errors
# ---------------------------------------------------------------------
@app.get("/")
def index():
    return (
        "Chatbot Pré-Natal online. Endpoints: "
        "/health (GET), /whatsapp (POST, Twilio webhook), /export.csv (GET)."
    )

# SUBSTITUA seu handler 404 por este:
@app.errorhandler(404)
def not_found(e):
    try:
        return render_template("404.html"), 404
    except Exception:
        # fallback se o template não existir
        return Response(
            "404 – Página não encontrada. Endpoints: /health (GET), /whatsapp (POST), /whatsapp-test (GET/POST), /export.csv (GET)",
            mimetype="text/plain"
        ), 404

# ---------------------------------------------------------------------
# Webhook (Twilio -> WhatsApp)
# ---------------------------------------------------------------------
@app.post("/whatsapp")
def whatsapp_webhook():
    incoming = request.form
    body = (incoming.get("Body") or "").strip()
    from_raw = incoming.get("From") or ""
    phone = from_raw.replace("whatsapp:", "")

    up = body.upper()

    # Commands
    if up == "SAIR":
        end_session(phone)
        return twiml("Ok, conversa encerrada. Seus dados não serão mais coletados.")
    if up == "REINICIAR":
        end_session(phone)
        save_session(phone, 0, {}, 0)
        return twiml("Sessão reiniciada. Para iniciar, digite *ACEITO*.")
    if up == "MENU":
        return twiml(FAQ_MENU)

    sess = get_session(phone)
    if not sess:
        save_session(phone, 0, {}, 0)
        return twiml(WELCOME)

    state = sess["state"]
    data = json.loads(sess["data"] or "{}")
    consented = sess["consented"] == 1

    # Atalhos de FAQ (não mudam o estado)
    if up.startswith("?") or any(k in body.lower() for k in [
        "alimentação","vacina","sinais de alerta","consultas","exames",
        "diabetes","pressão","prematuro","primeira consulta","sintomas","faixa etária"
    ]):
        ans = answer_faq(body)
        if ans:
            return twiml(ans + "\n\nDigite *CONTINUAR* para voltar ao questionário, ou *MENU* para ver mais tópicos.")
        if up.startswith("?"):
            return twiml("Não encontrei esse tópico. Digite *MENU* para ver as opções ou *CONTINUAR* para seguir o questionário.")

    if not consented:
        if up == "ACEITO":
            save_session(phone, 1, data, 1)
            return twiml(CONSENT_CONFIRMED + "\n\n" + QUESTIONS[1])
        else:
            return twiml("Para iniciar, digite *ACEITO*. Para sair, digite SAIR.")

    # Estado especial: repetir a pergunta corrente
    if up == "CONTINUAR":
        return twiml(QUESTIONS.get(state, "Vamos continuar."))

    # state machine
    try:
        if state == 1:
            data["iniciais"] = body[:20].strip()
            save_session(phone, 2, data, 1)
            return twiml(QUESTIONS[2])

        elif state == 2:
            try:
                idade = int(body)
                if idade < 10 or idade > 60:
                    return twiml("Informe uma *idade válida* (ex.: 28).")
                data["idade"] = idade
            except Exception:
                return twiml("Informe a idade em *número* (ex.: 28).")
            save_session(phone, 3, data, 1)
            return twiml(QUESTIONS[3])

        elif state == 3:
            weeks = parse_dum_or_weeks(body)
            if weeks is None:
                return twiml("Não entendi. Envie *DD/MM/AAAA* (DUM) ou apenas *semanas* (ex.: 22).")
            data["ga_weeks"] = weeks
            save_session(phone, 4, data, 1)
            return twiml(QUESTIONS[4])

        elif state == 4:
            ids = {s.strip() for s in body.replace(";",",").split(",") if s.strip()}
            valid = {"1","2","3","4","5","6","7"}
            if not ids or not ids.issubset(valid):
                return twiml("Responda com os *números* dos sintomas (ex.: 1,3).")
            data["sintomas_ids"] = sorted(list(ids))
            save_session(phone, 5, data, 1)
            return twiml(QUESTIONS[5])

        elif state == 5:
            ids = {s.strip() for s in body.replace(";",",").split(",") if s.strip()}
            valid = {"1","2","3","4"}
            if not ids or not ids.issubset(valid):
                return twiml("Responda com os *números* das condições (ex.: 1,4).")
            data["comorb_ids"] = sorted(list(ids))
            save_session(phone, 6, data, 1)
            return twiml(QUESTIONS[6])

        elif state == 6:
            try:
                consultas = int(body)
                if consultas < 0 or consultas > 50:
                    return twiml("Informe um número *válido* de consultas (ex.: 3).")
                data["consultas_qtd"] = consultas
            except Exception:
                return twiml("Responda com *número* (ex.: 3).")
            save_session(phone, 7, data, 1)
            return twiml(QUESTIONS[7])

        elif state == 7:
            if up == "PULAR":
                data["pa_sys"] = None
                data["pa_dia"] = None
            else:
                s, d = parse_bp(body)
                if not s or not d:
                    return twiml("Formato inválido. Envie como *12x8*, *12/8*, *12 8* ou *120/80* (ou digite *PULAR*).")
                data["pa_sys"] = s
                data["pa_dia"] = d
            save_session(phone, 8, data, 1)
            return twiml(QUESTIONS[8])

        elif state == 8:
            if up == "PULAR":
                data["peso"] = None
            else:
                w = parse_kg(body)
                if w is None:
                    return twiml("Informe apenas o *peso em kg* (ex.: 70) ou digite *PULAR*.")
                data["peso"] = w
            # recalcula IMC se já houver altura
            if data.get("peso") and data.get("altura"):
                data["imc"] = round(data["peso"] / (data["altura"]**2), 1)
            else:
                data["imc"] = None
            save_session(phone, 9, data, 1)
            return twiml(QUESTIONS[9])

        elif state == 9:
            if up == "PULAR":
                data["altura"] = None
            else:
                h = parse_meters(body)
                if h is None:
                    return twiml("Informe apenas a *altura em metros* (ex.: 1.60) ou digite *PULAR*.")
                data["altura"] = h
            # recalcula IMC se já houver peso
            if data.get("peso") and data.get("altura"):
                data["imc"] = round(data["peso"] / (data["altura"]**2), 1)
            else:
                data["imc"] = None
            save_session(phone, 10, data, 1)
            return twiml(QUESTIONS[10])

        elif state == 10:
            if body.strip() not in ("1","2"):
                return twiml("Responda *1* para Sim ou *2* para Não.")
            data["habitos"] = "sim" if body.strip() == "1" else "nao"

            # Classificar
            risk_level, rationale = classify_risk(data)
            store_response(phone, data, risk_level, data.get("ga_weeks"))
            save_session(phone, 11, data, 1)

            msg = (
                f"{FINAL_MSG}\n\n"
                f"*Classificação:* {risk_level}\n"
                f"*Justificativa:* {rationale}\n"
            )
            if risk_level == "EMERGENTE":
                msg += "➡️ Procure um serviço de *emergência agora* ou ligue *192 (SAMU)*.\n"
            elif risk_level == "PRIORITÁRIO":
                msg += "➡️ Procure *avaliação na sua unidade* ainda hoje/amanhã.\n"
            else:
                msg += "➡️ Mantenha seu *acompanhamento de rotina*.\n"
            msg += "\n" + EDU_MSG
            return twiml(msg)

        elif state == 11:
            if body.strip() == "1":
                end_session(phone)
                return twiml(EDU_CONTENT + "\n\nConversa finalizada. Obrigado por participar!")
            elif body.strip() == "2":
                end_session(phone)
                return twiml("Ok, sem material adicional. Conversa finalizada. Obrigado por participar!")
            else:
                return twiml("Responda 1 para *Sim* ou 2 para *Não*.")

        else:
            end_session(phone)
            return twiml("Sessão reiniciada. Digite *ACEITO* para iniciar.")

    except Exception:
        end_session(phone)
        return twiml("Ocorreu um erro inesperado. Tente novamente mais tarde.")

# ---------------------------------------------------------------------
# Admin utilities
# ---------------------------------------------------------------------
@app.get("/export.csv")
def export_csv():
    import csv, io
    sep = (request.args.get("sep") or ";").strip()
    if sep.lower() == "tab":
        delimiter = "\t"
    elif sep in [",", ";", "|"]:
        delimiter = sep
    else:
        delimiter = ";"

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, phone, data, risk_level, ga_weeks, created_at FROM responses ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow([
        "id","phone","risk_level","ga_weeks","created_at",
        "iniciais","idade","sintomas_ids","comorb_ids","consultas_qtd",
        "pa_sys","pa_dia","peso","altura","imc","habitos"
    ])
    for r in rows:
        payload = json.loads(r["data"])
        writer.writerow([
            r["id"], r["phone"], r["risk_level"], r["ga_weeks"], r["created_at"],
            payload.get("iniciais",""),
            payload.get("idade",""),
            "|".join(payload.get("sintomas_ids", [])),
            "|".join(payload.get("comorb_ids", [])),
            payload.get("consultas_qtd",""),
            payload.get("pa_sys",""),
            payload.get("pa_dia",""),
            payload.get("peso",""),
            payload.get("altura",""),
            payload.get("imc",""),
            payload.get("habitos",""),
        ])
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(csv_bytes, mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=prenatal_export.csv"})

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

@app.route("/whatsapp-test", methods=["GET", "POST"])
def whatsapp_test():
    body = (request.values.get("Body") or "ping").strip()
    frm  = (request.values.get("From") or "whatsapp:+000").replace("whatsapp:", "")
    return twiml(f"✅ Webhook OK. Echo: '{body}' de {frm}")

