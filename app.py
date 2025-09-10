@app.post("/whatsapp")
def whatsapp_webhook():
    incoming = request.form
    body = (incoming.get("Body") or "").strip()
    from_raw = incoming.get("From") or ""
    phone = from_raw.replace("whatsapp:", "")

    up = body.upper()
    low = body.lower().strip()

    # Commands
    if up in ("SAIR", "FIM"):
        end_session(phone)
        return twiml("Conversa encerrada. Obrigado por participar! Em emergência, 192 (SAMU).")
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
        else:
            return twiml("Para iniciar, digite *ACEITO*. Para sair, digite SAIR.")

    # Saudações comuns → repete a pergunta atual
    GREETINGS = {"oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "hello", "hi"}
    if low in GREETINGS:
        return twiml(QUESTIONS.get(state, "Vamos continuar."))

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
                return twiml("Não entendi.\n\n" + QUESTIONS[3])
            data["ga_weeks"] = weeks
            save_session(phone, 4, data, 1)
            return twiml(QUESTIONS[4])

        elif state == 4:
            ids = {s.strip() for s in body.replace(";",",").split(",") if s.strip()}
            valid = {"1","2","3","4","5","6","7"}
            if not ids or not ids.issubset(valid):
                return twiml("Não entendi.\n\n" + QUESTIONS[4])
            data["sintomas_ids"] = sorted(list(ids))
            save_session(phone, 5, data, 1)
            return twiml(QUESTIONS[5])

        elif state == 5:
            ids = {s.strip() for s in body.replace(";",",").split(",") if s.strip()}
            valid = {"1","2","3","4"}
            if not ids or not ids.issubset(valid):
                return twiml("Não entendi.\n\n" + QUESTIONS[5])
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
                return twiml("Não entendi.\n\n" + QUESTIONS[6])
            save_session(phone, 7, data, 1)
            return twiml(QUESTIONS[7])

        elif state == 7:
            if up == "PULAR":
                data["pa_sys"] = None
                data["pa_dia"] = None
            else:
                s, d = parse_bp(body)
                if not s or not d:
                    return twiml("Não entendi.\n\n" + QUESTIONS[7])
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
                    return twiml("Não entendi.\n\n" + QUESTIONS[8])
                data["peso"] = w
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
                    return twiml("Não entendi.\n\n" + QUESTIONS[9])
                data["altura"] = h
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

    except Exception:
        end_session(phone)
        return twiml("Ocorreu um erro inesperado. Tente novamente mais tarde.")
