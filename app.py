# app.py
import os
import re
import json
import sqlite3
from functools import lru_cache
from datetime import datetime, date
from dateutil import parser as dateparser
from flask import Flask, request, Response, render_template
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

# =========================
# Setup
# =========================
load_dotenv()
app = Flask(__name__, template_folder="templates")
app.config["JSON_AS_ASCII"] = False

DB_PATH = os.getenv("DB_PATH", "prenatal.db")
PORT = int(os.getenv("PORT", "5000"))
TWILIO_DEBUG = os.getenv("TWILIO_DEBUG", "1") == "1"
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "").lower()  # deixe vazio p/ máxima velocidade
USE_LLM_ONLY_WITH_QMARK = True

def log(*args):
    if TWILIO_DEBUG:
        print("[WHATSAPP]", *args, flush=True)

# =========================
# SQLite rápido
# =========================
def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=3.0,
        check_same_thread=False,
        isolation_level=None,  # autocommit
    )
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("PRAGMA temp_store=MEMORY;")
    cur.execute("PRAGMA cache_size=-16000;")
    cur.execute("PRAGMA busy_timeout=3000;")
    cur.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
          phone TEXT PRIMARY KEY,
          state INTEGER NOT NULL,
          data  TEXT NOT NULL,
          consented INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS responses(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone TEXT NOT NULL,
          data  TEXT NOT NULL,
          risk_level TEXT NOT NULL,
          ga_weeks INTEGER,
          created_at TEXT NOT NULL
        );
    """)
    try:
        cur.execute("""
          CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts
          USING fts5(
            title, body, tags,
            tokenize='unicode61 remove_diacritics 2'
          );
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit(); conn.close()

init_db()

# =========================
# FAQ / RAG local
# =========================
def _t(s):
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
*Ajuda/Informações* — você pode enviar `? tema` ou escrever em linguagem natural (ex.: "dor de cabeça", "movimentos do bebê").
• `? primeira consulta` • `? consultas` • `? alimentação`
• `? sintomas` • `? sinais de alerta` • `? vacinação`
• `? exames` • `? diabetes` • `? pressão alta`
• `? parto prematuro` • `? faixa etária`
(Use `MENU` para ver esta lista; `CONTINUAR` volta ao questionário.)
""")
FAQ_TRIGGER_WORDS = tuple(sorted({w for ks in FAQ_TOPICS for w in ks}, key=len, reverse=True))

TOKEN_RE = re.compile(r"\w{3,}", re.UNICODE)
BP_RE  = re.compile(r"(\d{2,3})\s*/\s*(\d{1,3})")
NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

def ensure_kb_seed():
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT 1 FROM kb_fts LIMIT 1")
        count = cur.execute("SELECT count(*) FROM kb_fts").fetchone()[0]
        if count == 0:
            rows = []
            for keys, body in FAQ_TOPICS.items():
                rows.append((list(keys)[0], body.strip(), ", ".join(keys)))
            cur.executemany("INSERT INTO kb_fts (title, body, tags) VALUES (?,?,?)", rows)
            conn.commit()
        conn.close()
    except Exception:
        pass

ensure_kb_seed()

def _fts_query_from_text(q: str) -> str:
    toks = TOKEN_RE.findall(q.lower())
    return " ".join(t + "*" for t in toks) if toks else ""

@lru_cache(maxsize=256)
def kb_search_cached(query_norm: str, k: int) -> tuple:
    try:
        conn = db(); cur = conn.cursor()
        sql = """
            SELECT title, snippet(kb_fts, 1, '*', '*', '…', 10) AS snip, body, bm25(kb_fts) AS score
            FROM kb_fts WHERE kb_fts MATCH ? ORDER BY score LIMIT ?
        """
        rows = list(cur.execute(sql, (query_norm, k)))
        conn.close()
        return tuple(rows)
    except Exception:
        return tuple()

def kb_search_raw(query: str, k: int = 3):
    q = _fts_query_from_text(query)
    if not q:
        return []
    rows = kb_search_cached(q, k)
    return [{"title": r[0], "snippet": r[1] or r[2], "body": r[2], "score": r[3]} for r in rows]

def llm_summarize(question: str, passages: list[str]) -> str | None:
    if not passages: return None
    try:
        if LLM_PROVIDER == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            prompt = (
                "Você é um assistente educativo em saúde materna. Responda em português, "
                "curto e claro, sem diagnosticar nem prescrever. Em sinais de alerta, "
                "oriente procurar serviço de saúde/SAMU 192.\n\n"
                f"PERGUNTA:\n{question}\n\n"
                f"FONTES:\n- " + "\n- ".join(passages) + "\n\nResponda objetivamente (bullets quando útil)."
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=300,
                timeout=8.0,
            )
            return (resp.choices[0].message.content or "").strip()
        elif LLM_PROVIDER == "gemini":
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
            model = genai.GenerativeModel("gemini-1.5-flash")
            prompt = (
                "Você é um assistente educativo em saúde materna. Responda em português, "
                "curto e claro, sem diagnosticar nem prescrever. Em sinais de alerta, "
                "oriente procurar serviço de saúde/SAMU 192.\n\n"
                f"PERGUNTA:\n{question}\n\n"
                f"FONTES:\n- " + "\n- ".join(passages) + "\n\nResponda objetivamente (bullets quando útil)."
            )
            resp = model.generate_content(prompt)
            return (getattr(resp, "text", "") or "").strip()
    except Exception:
        return None
    return None

def answer_faq(text: str, data: dict | None = None) -> str | None:
    t = text.lower().strip()
    starts_with_q = t.startswith("?")
    if starts_with_q: t = t[1:].strip()

    for keys, msg in FAQ_TOPICS.items():
        if any(k in t for k in keys):
            return msg

    hits = kb_search_raw(t, k=3)
    if not hits: return None

    if LLM_PROVIDER and (starts_with_q or not USE_LLM_ONLY_WITH_QMARK):
        summary = llm_summarize(text, [h["body"] for h in hits])
        if summary: return summary

    bullets = [f"• *{h['title'].capitalize()}*: {h['snippet']}" for h in hits]
    return "*Informações relacionadas:*\n" + "\n".join(bullets)

# =========================
# Fluxo / helpers
# =========================
SEVERE_SYMPTOM_IDS = {"1","2","3","4","6"}

WELCOME = (
    "Olá! Sou o assistente *Pré-Natal*.\n\n"
    "*Aviso*: este serviço NÃO substitui atendimento médico. Em emergência, ligue 192 (SAMU).\n\n"
    "Se você *concorda em participar* e autoriza o uso dos dados para fins acadêmicos "
    "conforme a LGPD, responda: *ACEITO*.\n\n"
    "Comandos: *MENU*, *CONTINUAR*, *REINICIAR*, *FIM*, *SAIR*."
)
CONSENT_CONFIRMED = "Obrigado. Consentimento registrado. Vamos começar com algumas perguntas rápidas."
QUESTIONS = {
    1: "1) Para preservar a privacidade, informe apenas *iniciais* do seu nome (ex.: A.R.M.).",
    2: "2) Qual sua *idade* em anos? (ex.: 28)",
    3: ("3) Informe a *data da última menstruação (DUM)* em *DD/MM/AAAA*\n"
        "   *ou* digite apenas as *semanas de gestação* (ex.: 22)."),
    4: ("4) Você apresenta algum(s) *sintoma(s) agora*? Responda com os números (ex.: 1,3):\n"
        "1 Sangramento vaginal\n"
        "2 Dor abdominal intensa\n"
        "3 Febre (≥ 38°C)\n"
        "4 Dor de cabeça forte / visão turva / inchaço súbito\n"
        "5 Náusea/vômito persistente\n"
        "6 Ausência de movimentos fetais (> 28s)\n"
        "7 Nenhum dos anteriores"),
    5: ("5) Possui alguma *condição de saúde*? (números, ex.: 1,4)\n"
        "1 Hipertensão\n"
        "2 Diabetes\n"
        "3 Infecção urinária atual\n"
        "4 Nenhuma"),
    6: "6) Quantas *consultas de pré-natal* você já realizou nesta gestação? (ex.: 3)",
    7: ("7) Você consegue informar sua *pressão arterial* agora?\n"
        "   Envie como *12x8*, *12/8*, *12 8* ou *120/80* (ou digite *PULAR*)."),
    8: "8) Informe seu *peso em kg* (ex.: 70). Se não souber, digite *PULAR*.",
    9: "9) Informe sua *altura em metros* (ex.: 1.60). Se não souber, digite *PULAR*.",
    10: "10) Você usa *tabaco* ou *álcool* atualmente? Responda *1* Sim ou *2* Não.",
}
FINAL_MSG = "Obrigado. Avaliando suas respostas…"
EDU_MSG = ("Deseja receber *material educativo* (dicas personalizadas, sinais de alerta e calendário de consultas)?\n"
           "Responda 1 para *Sim* ou 2 para *Não*.")
ALERTA_BASE = (
    "*Sinais de alerta* — procurar serviço imediatamente / *192 SAMU*:\n"
    "• Sangramento vaginal\n"
    "• Dor abdominal forte\n"
    "• Febre ≥38°C\n"
    "• Dor de cabeça intensa/visão turva/inchaço súbito\n"
    "• Ausência de movimentos fetais após 28s"
)
GREETINGS = {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "hello", "hi"}

def parse_dum_or_weeks(text):
    text = text.strip()
    try:
        w = int(text)
        if 0 <= w <= 45: return w
    except ValueError:
        pass
    try:
        dt = datetime.strptime(text, "%d/%m/%Y").date()
    except Exception:
        try:
            dt = dateparser.parse(text, dayfirst=True).date()
        except Exception:
            return None
    days = (date.today() - dt).days
    weeks = days // 7
    return weeks if 0 <= weeks <= 45 else None

def parse_bp(text):
    t = text.lower().replace(",", ".").strip().replace("x", "/").replace(" ", "/")
    m = BP_RE.search(t)
    if not m: return (None, None)
    try:
        s = int(m.group(1)); d = int(m.group(2))
        if s < 30 and d < 30: s *= 10; d *= 10
        if 60 <= s <= 260 and 30 <= d <= 180: return (s, d)
    except Exception:
        pass
    return (None, None)

def parse_kg(text):
    m = NUM_RE.search(text.lower().replace(",", "."))
    if not m: return None
    try:
        w = float(m.group(1))
        return round(w, 1) if 30 <= w <= 250 else None
    except Exception:
        return None

def parse_meters(text):
    m = NUM_RE.search(text.lower().replace(",", "."))
    if not m: return None
    try:
        h = float(m.group(1))
        return round(h, 2) if 1.3 <= h <= 2.2 else None
    except Exception:
        return None

def trimester_from_weeks(w):
    if w is None: return None
    if w < 14: return 1
    if w < 28: return 2
    return 3

def calendar_tip(weeks):
    if weeks is None:
        return "• Consultas: mensais até 34s; quinzenais 34–36s; semanais >36s."
    if weeks < 34: return "• Consultas: mensais até 34s; depois quinzenais."
    if weeks < 36: return "• Consultas: quinzenais até 36s; depois semanais."
    return "• Consultas: semanais a partir de 36s."

def trimester_exams(weeks):
    tri = trimester_from_weeks(weeks)
    if tri == 1: return "• 1º tri: hemograma, tipagem/Rh, glicemia, sorologias, urina/urocultura, US obstétrico."
    if tri == 2: return "• 2º tri: TOTG 24–28s, US morfológico."
    if tri == 3: return "• 3º tri: hemograma, sorologias de controle, cultura para EGB 35–37s."
    return "• Exames por trimestre variam; siga o pedido da sua unidade."

def vaccines_tip(weeks):
    tips = []
    if weeks is None:
        tips.append("• Vacinas: Influenza (anual), Hep. B e COVID-19 conforme indicação; dTpa entre 20–36s.")
    else:
        if 20 <= weeks <= 36: tips.append("• dTpa entre 20–36s.")
        tips.append("• Influenza (anual), Hep. B e COVID-19 conforme indicação.")
    return "\n".join(tips)

def educational_pack(data, risk_level):
    weeks = data.get("ga_weeks")
    imc   = data.get("imc")
    sys   = data.get("pa_sys")
    dia   = data.get("pa_dia")
    comorb = set(data.get("comorb_ids", []))
    habitos = data.get("habitos")

    block = []
    if risk_level == "EMERGENTE":
        block.append("*Prioridade:* sinais de gravidade detectados. Procure *emergência agora* / 192.")
    elif risk_level == "PRIORITÁRIO":
        block.append("*Prioridade:* avaliação em breve (hoje/amanhã) na sua unidade.")
    else:
        block.append("*Rotina:* manter acompanhamento e autocuidados.")
    block.append(calendar_tip(weeks))
    block.append(trimester_exams(weeks))
    block.append(vaccines_tip(weeks))
    if imc and imc >= 30:
        block.append("• IMC elevado (≥30): foco em alimentação equilibrada, atividade leve e metas de ganho de peso orientadas pela equipe.")
    if (sys and dia) and (sys >= 140 or dia >= 90):
        block.append("• Pressão arterial elevada: meça em horários regulares e leve os registros à sua unidade.")
    if "2" in comorb:
        block.append("• Diabetes/risco: siga orientações de dieta, atividade e metas glicêmicas; TOTG 24–28s se ainda não realizou.")
    if "1" in comorb:
        block.append("• Hipertensão: atenção a cefaleia forte, escotomas, dor em “boca do estômago” e inchaço súbito.")
    if habitos == "sim":
        block.append("• Tabaco/álcool: interromper traz benefício imediato; busque apoio na sua unidade.")
    block.append("\n" + ALERTA_BASE)
    block.append("\nTem dúvidas? Envie `? tema` (ex.: `? pressão alta`, `? alimentação`) ou `MENU` para a lista. Para encerrar, mande *FIM*.")
    return "\n".join(block)

def classify_risk(record):
    age  = record.get("idade")
    weeks = record.get("ga_weeks")
    sintomas = set(record.get("sintomas_ids", []))
    comorb   = set(record.get("comorb_ids", []))
    sys = record.get("pa_sys")
    dia = record.get("pa_dia")
    imc = record.get("imc")
    habitos = record.get("habitos")

    if sintomas & SEVERE_SYMPTOM_IDS:
        return ("EMERGENTE", "Sintoma(s) de alerta reportado(s). Orientar ida IMEDIATA ao serviço / 192.")
    if sys and dia and (sys >= 160 or dia >= 110):
        return ("EMERGENTE", "Pressão arterial muito elevada (≥160/110). Procurar emergência.")

    reasons = []
    try:
        if age is not None and (age < 18 or age >= 35):
            reasons.append("Faixa etária (<18 ou ≥35) pode elevar riscos obstétricos; acompanhamento mais próximo é recomendado.")
    except Exception:
        pass
    if {"1","2","3"} & comorb:
        reasons.append("Comorbidade (hipertensão/diabetes/ITU).")
    if weeks is not None and weeks >= 28 and "6" in sintomas:
        reasons.append("Queixa sobre movimentos fetais no 3º trimestre.")
    if sys and dia and (sys >= 140 or dia >= 90):
        reasons.append("Pressão arterial elevada (≥140/90).")
    if imc and imc >= 30:
        reasons.append("IMC elevado (≥30).")
    if habitos == "sim":
        reasons.append("Uso de tabaco/álcool (risco gestacional).")
    if reasons:
        return ("PRIORITÁRIO", "; ".join(reasons) + " Para saber mais, envie `? faixa etária` ou `? pressão alta`. Orientar avaliação em breve (hoje/amanhã).")
    return ("ROTINA", "Sem sinais de alerta no momento. Manter acompanhamento de pré-natal e orientações gerais.")

def get_session(phone):
    conn = db(); cur = conn.cursor()
    row = cur.execute("SELECT * FROM sessions WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return row

def save_session(phone, state, data, consented):
    now = datetime.utcnow().isoformat()
    conn = db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions(phone, state, data, consented, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(phone) DO UPDATE SET
            state=excluded.state,
            data=excluded.data,
            consented=excluded.consented,
            updated_at=excluded.updated_at;
    """, (phone, state, json.dumps(data, ensure_ascii=False), consented, now, now))
    conn.commit(); conn.close()

def end_session(phone):
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
    conn.commit(); conn.close()

def store_response(phone, data, risk_level, ga_weeks):
    now = datetime.utcnow().isoformat()
    conn = db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO responses(phone, data, risk_level, ga_weeks, created_at)
        VALUES(?,?,?,?,?);
    """, (phone, json.dumps(data, ensure_ascii=False), risk_level, ga_weeks, now))
    conn.commit(); conn.close()

def twiml(message):
    r = MessagingResponse()
    r.message(message)
    return Response(str(r), mimetype="application/xml")

# =========================
# Rotas
# =========================
@app.get("/")
def index():
    return "Chatbot Pré-Natal: /health, /whatsapp (POST), /whatsapp-test (POST), /export.csv"

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

# --- webhook de TESTE (responde sempre) ---
@app.post("/whatsapp-test")
def whatsapp_test():
    incoming = request.form
    body = (incoming.get("Body") or "").strip()
    from_raw = incoming.get("From") or ""
    phone = from_raw.replace("whatsapp:", "")
    log("TEST IN:", {"from": phone, "body": body})
    return twiml("✅ Webhook OK. Use /whatsapp para o fluxo completo. Envie *ACEITO* para começar.")

# --- webhook principal ---
@app.post("/whatsapp")
def whatsapp_webhook():
    incoming = request.form
    body = (incoming.get("Body") or "").strip()
    from_raw = incoming.get("From") or ""
    phone = from_raw.replace("whatsapp:", "")
    up  = body.upper()
    low = body.lower()
    log("IN:", {"from": phone, "body": body})

    # Comandos
    if up in ("SAIR", "FIM"):
        end_session(phone)
        return twiml("Conversa encerrada. Obrigado por participar! Em emergência, 192 (SAMU).")
    if up == "REINICIAR":
        end_session(phone)
        save_session(phone, 0, {}, 0)
        return twiml("Sessão reiniciada. Para iniciar, digite *ACEITO*.")
    if up == "MENU":
        return twiml(FAQ_MENU)

    # Sessão
    sess = get_session(phone)
    if not sess:
        save_session(phone, 0, {}, 0)
        return twiml(WELCOME)

    state = int(sess["state"])
    data = json.loads(sess["data"] or "{}")
    consented = sess["consented"] == 1

    # FAQ rápido
    if up.startswith("?") or any(w in low for w in FAQ_TRIGGER_WORDS):
        ans = answer_faq(body, data)
        if ans:
            return twiml(ans + "\n\nDigite *CONTINUAR* para voltar ao questionário, *MENU* para ver mais tópicos ou *FIM* para encerrar.")
        if up.startswith("?"):
            return twiml("Não encontrei esse tópico. Digite *MENU* para ver as opções ou *CONTINUAR* para seguir o questionário.")

    # Consentimento
    if not consented:
        if up == "ACEITO":
            save_session(phone, 1, data, 1)
            return twiml(CONSENT_CONFIRMED + "\n\n" + QUESTIONS[1])
        return twiml("Para iniciar, digite *ACEITO*. Para sair, digite SAIR.")

    # Saudações/CONTINUAR → repete pergunta atual
    if low in GREETINGS or up == "CONTINUAR":
        return twiml(QUESTIONS.get(state, "Vamos continuar."))

    # State machine
    try:
        if state == 1:
            data["iniciais"] = body[:20].strip()
            save_session(phone, 2, data, 1)
            return twiml(QUESTIONS[2])

        elif state == 2:
            try:
                idade = int(body)
                if not (10 <= idade <= 60):
                    return twiml("Informe uma *idade válida* (ex.: 28).")
                data["idade"] = idade
            except Exception:
                return twiml("Informe a idade em *número* (ex.: 28).")
            save_session(phone, 3, data, 1)
            return twiml(QUESTIONS[3])

        elif state == 3:
            weeks = parse_dum_or_weeks(body)
            if weeks is None:
                return twiml("Não entendi.\n\n" + QUESTIONS[3])
            data["ga_weeks"] = weeks
            save_session(phone, 4, data, 1)
            return twiml(QUESTIONS[4])

        elif state == 4:
            ids = {s.strip() for s in body.replace(";", ",").split(",") if s.strip()}
            valid = {"1","2","3","4","5","6","7"}
            if not ids or not ids.issubset(valid):
                return twiml("Não entendi.\n\n" + QUESTIONS[4])
            data["sintomas_ids"] = sorted(list(ids))
            save_session(phone, 5, data, 1)
            return twiml(QUESTIONS[5])

        elif state == 5:
            ids = {s.strip() for s in body.replace(";", ",").split(",") if s.strip()}
            valid = {"1","2","3","4"}
            if not ids or not ids.issubset(valid):
                return twiml("Não entendi.\n\n" + QUESTIONS[5])
            data["comorb_ids"] = sorted(list(ids))
            save_session(phone, 6, data, 1)
            return twiml(QUESTIONS[6])

        elif state == 6:
            try:
                consultas = int(body)
                if not (0 <= consultas <= 50):
                    return twiml("Informe um número *válido* de consultas (ex.: 3).")
                data["consultas_qtd"] = consultas
            except Exception:
                return twiml("Não entendi.\n\n" + QUESTIONS[6])
            save_session(phone, 7, data, 1)
            return twiml(QUESTIONS[7])

        elif state == 7:
            if up == "PULAR":
                data["pa_sys"] = None; data["pa_dia"] = None
            else:
                s, d = parse_bp(body)
                if not s or not d:
                    return twiml("Não entendi.\n\n" + QUESTIONS[7])
                data["pa_sys"] = s; data["pa_dia"] = d
            save_session(phone, 8, data, 1)
            return twiml(QUESTIONS[8])

        elif state == 8:
            if up == "PULAR":
                data["peso"] = None
            else:
                w = parse_kg(body)
                if w is None: return twiml("Não entendi.\n\n" + QUESTIONS[8])
                data["peso"] = w
            data["imc"] = round(data["peso"] / (data["altura"]**2), 1) if (data.get("peso") and data.get("altura")) else None
            save_session(phone, 9, data, 1)
            return twiml(QUESTIONS[9])

        elif state == 9:
            if up == "PULAR":
                data["altura"] = None
            else:
                h = parse_meters(body)
                if h is None: return twiml("Não entendi.\n\n" + QUESTIONS[9])
                data["altura"] = h
            data["imc"] = round(data["peso"] / (data["altura"]**2), 1) if (data.get("peso") and data.get("altura")) else None
            save_session(phone, 10, data, 1)
            return twiml(QUESTIONS[10])

        elif state == 10:
            if body.strip() not in ("1","2"):
                return twiml("Responda *1* para Sim ou *2* para Não.")
            data["habitos"] = "sim" if body.strip() == "1" else "nao"

            risk_level, rationale = classify_risk(data)
            store_response(phone, data, risk_level, data.get("ga_weeks"))
            save_session(phone, 11, data, 1)

            msg = (f"{FINAL_MSG}\n\n"
                   f"*Classificação:* {risk_level}\n"
                   f"*Justificativa:* {rationale}\n")
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
                risk_level, _ = classify_risk(data)
                pack = educational_pack(data, risk_level)
                save_session(phone, 12, data, 1)
                return twiml(pack)
            elif body.strip() == "2":
                end_session(phone)
                return twiml("Ok, sem material adicional. Conversa finalizada. Obrigado por participar!")
            else:
                return twiml("Responda 1 para *Sim* ou 2 para *Não*.")

        elif state == 12:
            if up in ("FIM", "SAIR"):
                end_session(phone)
                return twiml("Conversa encerrada. Obrigado por participar! Em emergência, 192 (SAMU).")
            if up == "MENU":
                return twiml(FAQ_MENU)
            if up.startswith("?"):
                ans = answer_faq(body, data)
                if ans:
                    return twiml(ans + "\n\nDigite *MENU* para mais tópicos ou *FIM* para encerrar.")
                return twiml("Não encontrei esse tópico. Digite *MENU* para ver as opções ou *FIM* para encerrar.")
            if up == "CONTINUAR":
                return twiml("Podemos continuar pelo *MENU* (envie `MENU`) ou encerrar com *FIM*.")
            return twiml("Se quiser mais informações, envie `MENU` ou `? tema` (ex.: `? alimentação`). Para encerrar, mande *FIM*.")

        else:
            end_session(phone)
            return twiml("Sessão reiniciada. Digite *ACEITO* para iniciar.")
    except Exception as e:
        log("ERRO:", repr(e))
        end_session(phone)
        return twiml("Ocorreu um erro inesperado. Tente novamente mais tarde.")

# Export
@app.get("/export.csv")
def export_csv():
    import csv, io
    sep = (request.args.get("sep") or ";").strip()
    delimiter = "\t" if sep.lower() == "tab" else (sep if sep in [",", ";", "|"] else ";")

    conn = db(); cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, phone, data, risk_level, ga_weeks, created_at
        FROM responses
        ORDER BY id DESC
    """).fetchall()
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

# Boot
if __name__ == "__main__":
    # Em prod, prefira: gunicorn app:app --workers 3 --threads 2 --timeout 30
    app.run(host="0.0.0.0", port=PORT)
