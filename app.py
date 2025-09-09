# app.py  (consolidado com RAG local via SQLite FTS5 + BM25)

import os
import re
import json
import sqlite3
from datetime import datetime, date
from dateutil import parser as dateparser
from flask import Flask, request, Response, render_template
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# [NOVO - RAG]
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------
load_dotenv()
app = Flask(__name__, template_folder="templates")

DB_PATH = os.getenv("DB_PATH", "prenatal.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "changeme")  # defina no Render

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    # --- tabelas existentes ---
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

    # --- [NOVO] base de conhecimento (mini-RAG local) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kb_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT,
            added_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            chunk_ix INTEGER NOT NULL,
            content TEXT NOT NULL,
            FOREIGN KEY(doc_id) REFERENCES kb_docs(id)
        )
    """)

    # índice FTS5 para BM25; pode não existir em alguns builds de SQLite
    global RAG_AVAILABLE
    RAG_AVAILABLE = True
    try:
        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts
            USING fts5(content, content='kb_chunks', content_rowid='id');
        """)
        cur.executescript("""
            CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON kb_chunks
            BEGIN
                INSERT INTO kb_chunks_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON kb_chunks
            BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS kb_chunks_au AFTER UPDATE ON kb_chunks
            BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content) VALUES('delete', old.id, old.content);
                INSERT INTO kb_chunks_fts(rowid, content) VALUES (new.id, new.content);
            END;
        """)
    except Exception:
        # Fallback: sem RAG se FTS5 indisponível
        RAG_AVAILABLE = False

    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------
# Educational/FAQ (resumos disparados por ?palavra)
# ---------------------------------------------------------------------
def _t(s):  # minify multiline text
    return re.sub(r"[ \t]+\n", "\n", s.strip())

FAQ_TOPICS = {
    ("primeira consulta", "primeira vez", "começar", "iniciar"): _t("""
        *Primeira consulta de pré-natal* — anamnese, PA, peso/altura (IMC),
        exame físico, exames iniciais (hemograma, tipagem/Rh, glicemia, sorologias,
        urina/urocultura). Ácido fólico, nutrição, calendário e vacinas.
    """),
    ("consultas", "calendário", "frequência", "quantas consultas"): _t("""
        *Calendário* — até 34s: mensais; 34–36s: quinzenais; >36s: semanais.
        Mínimo 6 consultas. Em cada visita: PA, peso, AU, BCF, edemas/queixas;
        exames por trimestre conforme protocolo.
    """),
    ("alimentação", "dieta", "nutrição", "comida", "peso"): _t("""
        *Alimentação* — dieta variada, 5–6 refeições/dia, hidratação adequada.
        Evitar carnes/ovos crus, álcool e excesso de cafeína. Ganho ponderal = IMC prévio.
    """),
    ("sintomas", "enjoo", "azia", "constipação", "dor nas costas", "inchaço"): _t("""
        *Sintomas comuns* — náuseas, azia, constipação, dor lombar, cãibras, edema.
        Medidas: fracionar refeições, hidratação, alongamentos, elevar pernas.
        Procure serviço se dor intensa, sangramento, febre, cefaleia forte.
    """),
    ("sinais de alerta", "emergência", "perigo"): _t("""
        *Sinais de alerta* — sangramento, dor abdominal forte, PA muito alta,
        febre, perda de líquido, ↓ movimentos fetais, cefaleia intensa com visão turva.
        Procure atendimento imediato / SAMU 192.
    """),
    ("vacina", "vacinação", "imunização"): _t("""
        *Vacinas* — dTpa (20–36s), Influenza, Hepatite B e COVID-19 conforme indicação.
        Contraindicadas: tríplice viral, varicela. Informe sempre que está grávida.
    """),
    ("exames", "ultrassom", "laboratório", "sangue", "urina"): _t("""
        *Exames* — 1º tri: hemograma, tipagem/Rh, glicemia, sorologias, urina/urocultura, US obstétrico.
        2º tri: TOTG 24–28s, US morfológico; 3º tri: hemograma, sorologias de controle, cultura EGB 35–37s.
    """),
    ("diabetes", "glicose", "totg", "açúcar"): _t("""
        *Diabetes gestacional* — rastreio TOTG 75g (24–28s); tratamento: dieta/exercício,
        monitorização e insulina se necessário.
    """),
    ("pressão alta", "hipertensão", "pré-eclâmpsia", "eclâmpsia"): _t("""
        *Síndromes hipertensivas* — PA ≥140/90 após 20s requer avaliação; sinais graves:
        cefaleia intensa, escotomas, dor epigástrica, edema súbito. Procure atendimento.
    """),
    ("parto prematuro", "contrações", "antes da hora"): _t("""
        *Trabalho de parto prematuro* — contrações regulares <37s, dor lombar,
        pressão pélvica, sangramento/perda de líquido. Procure serviço.
    """),
}

FAQ_MENU = _t("""
*Ajuda/Informações* — envie:
• `? primeira consulta`
• `? consultas`
• `? alimentação`
• `? sintomas`
• `? sinais de alerta`
• `? vacinação`
• `? exames`
• `? diabetes`
• `? pressão alta`
• `? parto prematuro`
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
    "Olá! Sou o assistente *Pré-Natal* da pesquisa.\n\n"
    "*Aviso*: este serviço NÃO substitui atendimento médico. "
    "Em emergência, ligue 192 (SAMU).\n\n"
    "Se você *concorda em participar* e autoriza o uso dos dados para fins acadêmicos "
    "conforme a LGPD, responda: *ACEITO*.\n\n"
    "Comandos: *MENU*, *REINICIAR*, *SAIR*."
)

CONSENT_CONFIRMED = (
    "Obrigado. Consentimento registrado. Vamos começar com algumas perguntas rápidas."
)

QUESTIONS = {
    1: "1) Para preservar a privacidade, informe apenas *iniciais* do seu nome (ex.: A.R.M.).",
    2: "2) Qual sua *idade* em anos? (ex.: 28)",
    3: "3) Informe a *data da última menstruação (DUM)* em DD/MM/AAAA *ou* digite as *semanas de gestação* (ex.: 22).",
    4: (
        "4) Você apresenta algum(s) *sintoma(s) agora*? Responda com os números, separados por vírgula:\n"
        "1 Sangramento vaginal | 2 Dor abdominal intensa | 3 Febre (≥ 38°C)\n"
        "4 Dor de cabeça forte/visão turva/inchaço súbito | 5 Náusea/vômito persistente\n"
        "6 Ausência de movimentos fetais (> 28s) | 7 Nenhum dos anteriores"
    ),
    5: (
        "5) Possui alguma condição de saúde? (números, separados por vírgula)\n"
        "1 Hipertensão | 2 Diabetes | 3 Infecção urinária atual | 4 Nenhuma"
    ),
    6: "6) Quantas *consultas de pré-natal* você já realizou nesta gestação? (ex.: 3)",
    7: "7) Você consegue informar sua *pressão arterial* hoje? Envie como *120/80* (ou digite *PULAR*).",
    8: "8) Informe seu *peso (kg)* e *altura (m)* no formato: *70 1.60* (ou *PULAR*).",
    9: "9) Você usa *tabaco* ou *álcool* atualmente? Responda *1* Sim ou *2* Não.",
}

FINAL_MSG = "Obrigado. Avaliando suas respostas…"

EDU_MSG = (
    "Deseja receber *material educativo* (dicas de sinais de alerta e calendário de consultas)?\n"
    "Responda 1 para *Sim* ou 2 para *Não*."
)

EDU_CONTENT = (
    "*Sinais de alerta* (procurar serviço imediatamente): sangramento, dor forte, febre ≥38°C, "
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
    """Return (systolic, diastolic) or (None, None). Accept '120/80' with spaces."""
    m = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)
    if not m:
        return None, None
    try:
        s = int(m.group(1))
        d = int(m.group(2))
        if 60 <= s <= 260 and 30 <= d <= 180:
            return s, d
    except Exception:
        pass
    return None, None

def parse_weight_height(text):
    """Parse '70 1.60' or '70, 1,60' → (70.0, 1.60)"""
    t = text.replace(",", ".").strip()
    nums = [x for x in re.findall(r"[0-9.]+", t)]
    if len(nums) >= 2:
        try:
            w = float(nums[0]); h = float(nums[1])
            if 30 <= w <= 250 and 1.3 <= h <= 2.2:
                return w, h
        except Exception:
            pass
    return None, None

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
        return ("EMERGENTE", "Sintoma(s) de alerta reportado(s). Orientar ida IMEDIATA ao serviço de saúde / 192.")
    if sys and dia and (sys >= 160 or dia >= 110):
        return ("EMERGENTE", "Pressão arterial muito elevada (≥160/110). Procurar emergência.")

    # Priority
    priority_reasons = []
    try:
        if age is not None and (age < 18 or age >= 35):
            priority_reasons.append("Faixa etária <18 ou ≥35.")
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
        return ("PRIORITÁRIO", "; ".join(priority_reasons) + " Orientar avaliação em breve (hoje/amanhã).")

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
# [NOVO] Mini-RAG: chunking, ingestão e busca BM25
# ---------------------------------------------------------------------
def _chunk_text(txt: str, size=700, overlap=120):
    txt = re.sub(r'\s+', ' ', txt).strip()
    chunks = []
    i = 0
    while i < len(txt):
        j = min(len(txt), i + size)
        cut = j
        m = re.search(r'[.!?]\s', txt[i:j][::-1])
        if m and m.start() > 40:
            cut = i + (j - m.start())
        chunks.append(txt[i:cut].strip())
        i = max(cut - overlap, i + size)
    return [c for c in chunks if c]

def kb_add_text(title: str, text: str, source: str = None):
    if not RAG_AVAILABLE:
        return (-1, 0)
    now = datetime.utcnow().isoformat()
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO kb_docs(title, source, added_at) VALUES (?,?,?)", (title, source, now))
    doc_id = cur.lastrowid
    for ix, c in enumerate(_chunk_text(text)):
        cur.execute("INSERT INTO kb_chunks(doc_id, chunk_ix, content) VALUES (?,?,?)", (doc_id, ix, c))
    conn.commit(); conn.close()
    return (doc_id, ix + 1 if 'ix' in locals() else 0)

def kb_search(query: str, k: int = 5):
    if not RAG_AVAILABLE:
        return []
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.doc_id, c.chunk_ix,
               snippet(kb_chunks_fts, 0, '[', ']', ' … ', 12) AS snippet,
               d.title, d.source
        FROM kb_chunks_fts
        JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid
        JOIN kb_docs d    ON d.id = c.doc_id
        WHERE kb_chunks_fts MATCH ?
        ORDER BY rank LIMIT ?
    """, (query, k))
    rows = cur.fetchall()
    conn.close()
    return rows

def rag_answer(query: str):
    hits = kb_search(query, k=5)
    if not hits:
        return ("Não encontrei conteúdo na biblioteca para essa dúvida. "
                "Digite *MENU* para ver tópicos ou reformule com `? sua dúvida`.")
    bullets = []
    refs = []
    for r in hits[:3]:
        bullets.append(f"• {r['snippet']}")
        refs.append(r['source'] or r['title'] or f"doc {r['doc_id']}")
    ans = ("📚 *Resumo com base na nossa biblioteca*\n"
           + "\n".join(bullets) +
           "\n\nFontes: " + "; ".join(sorted(set(refs))[:3]) +
           "\n\n*Aviso*: informações educativas; não substituem avaliação profissional.")
    return ans

# ---------------------------------------------------------------------
# Home & Errors
# ---------------------------------------------------------------------
@app.get("/")
def index():
    return (
        "Chatbot Pré-Natal online. Endpoints: "
        "/health (GET), /whatsapp (POST, Twilio webhook), /export.csv (GET), "
        "/kb/ingest (POST, admin)."
    )

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

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

    # [NOVO] Consultas educativas: FAQ → RAG (não altera estado)
    # Gatilho: começa com "?" ou contém palavras educativas
    if up.startswith("?") or any(k in body.lower() for k in [
        "ajuda","material","informação","informacoes","informações","sinais","consultas","vacina","exames","diabetes",
        "pressão","prematuro","primeira consulta","sintomas"
    ]):
        if not consented:
            return twiml("Para acessar materiais educativos e iniciar o atendimento, responda *ACEITO*.")
        # 1) tenta FAQ
        ans = answer_faq(body)
        if ans:
            return twiml(ans + "\n\nDigite *CONTINUAR* para voltar ao questionário, ou *MENU* para ver mais tópicos.")
        # 2) fallback: RAG
        q = body[1:].strip() if body.strip().startswith("?") else body.strip()
        if q:
            return twiml(rag_answer(q) + "\n\nDigite *CONTINUAR* para retomar o questionário.")
        # sem consulta válida → segue fluxo

    if not consented:
        if up == "ACEITO":
            save_session(phone, 1, data, 1)
            return twiml(CONSENT_CONFIRMED + "\n\n" + QUESTIONS[1])
        else:
            return twiml("Para iniciar, digite *ACEITO*. Para sair, digite SAIR.")

    # Estado especial para continuar após FAQ/RAG
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
            if body.upper() == "PULAR":
                data["pa_sys"] = None
                data["pa_dia"] = None
            else:
                s, d = parse_bp(body)
                if not s or not d:
                    return twiml("Formato inválido. Envie como *120/80* ou digite *PULAR*.")
                data["pa_sys"] = s
                data["pa_dia"] = d
            save_session(phone, 8, data, 1)
            return twiml(QUESTIONS[8])

        elif state == 8:
            if body.upper() == "PULAR":
                data["peso"] = None
                data["altura"] = None
                data["imc"] = None
            else:
                w, h = parse_weight_height(body)
                if not w or not h:
                    return twiml("Formato inválido. Envie como *70 1.60* (peso kg e altura m) ou digite *PULAR*.")
                data["peso"] = w
                data["altura"] = h
                data["imc"] = round(w / (h*h), 1)
            save_session(phone, 9, data, 1)
            return twiml(QUESTIONS[9])

        elif state == 9:
            if body.strip() not in ("1","2"):
                return twiml("Responda *1* para Sim ou *2* para Não.")
            data["habitos"] = "sim" if body.strip() == "1" else "nao"

            # Classificar
            risk_level, rationale = classify_risk(data)
            store_response(phone, data, risk_level, data.get("ga_weeks"))
            save_session(phone, 10, data, 1)

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

        elif state == 10:
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
            r["id"],
            r["phone"],
            r["risk_level"],
            r["ga_weeks"],
            r["created_at"],
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

# [NOVO] Ingestão de conhecimento (RAG)
@app.post("/kb/ingest")
def kb_ingest():
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return {"error": "unauthorized"}, 401
    if not RAG_AVAILABLE:
        return {"error": "fts5_unavailable"}, 503

    title = (request.form.get("title") or "").strip() or "Sem título"
    source = (request.form.get("source") or "").strip() or None
    text = (request.form.get("text") or "").strip()

    if not text and "file" in request.files:
        f = request.files["file"]
        name = secure_filename(f.filename or "upload.txt")
        text = f.read().decode("utf-8", errors="ignore")
        if not source:
            source = name

    if not text:
        return {"error": "forneça 'text' ou 'file'"}, 400

    doc_id, n = kb_add_text(title, text, source)
    return {"ok": True, "doc_id": doc_id, "chunks": n}

@app.get("/health")
def health():
    return {"ok": True, "rag": RAG_AVAILABLE, "time": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
