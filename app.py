# app.py — webhook mínimo e 100% funcional para Twilio WhatsApp
import os
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

GREETINGS = {"oi", "olá", "ola", "hello", "hi", "bom dia", "boa tarde", "boa noite"}

def twiml(texto: str) -> Response:
    r = MessagingResponse()
    r.message(texto)
    return Response(str(r), mimetype="application/xml")

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/whatsapp")
def whatsapp():
    body = (request.form.get("Body") or "").strip()
    from_raw = request.form.get("From") or ""
    phone = from_raw.replace("whatsapp:", "")

    txt = body.lower()

    # comandos básicos
    if txt in {"sair", "fim"}:
        return twiml("Conversa encerrada. Obrigado! (Emergência: 192 SAMU)")

    # respostas simples (demonstração)
    if txt in GREETINGS:
        return twiml("👋 Webhook ativo! Envie *ACEITO* para a demonstração.")
    if txt == "aceito":
        return twiml("✅ Consentimento registrado.\nPergunta 1) Informe apenas as *iniciais* do seu nome.")
    if len(txt) <= 8:
        # eco simples
        return twiml(f"Recebi: *{body}*.\nDiga *ACEITO* ou um 'Oi' para testar.")
    return twiml("Tudo certo por aqui! 😉

- Este é um webhook de *demonstração*.
- Para fluxo completo, aponte o Twilio para a rota de produção do seu bot.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
