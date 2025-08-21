
# Chatbot de Pré-Natal (WhatsApp via Twilio Sandbox + Flask)

> **Atenção:** Para usar **seu próprio número** (ex.: +55 62 98663-727), é necessário cadastrar o número no **WhatsApp Business Platform** (Meta) ou contratar um BSP (ex.: Twilio) e concluir a verificação do WhatsApp Business. Para testes imediatos, use o **Twilio WhatsApp Sandbox**.

## ⚙️ Passo a passo (teste rápido com Sandbox)

1) Crie uma conta em https://www.twilio.com/ e ative o **WhatsApp Sandbox**.
2) No painel do Sandbox, siga a instrução "Join" (envie a palavra fornecida para o número do Sandbox no seu WhatsApp).
3) Configure o **Webhook** de mensagens do Sandbox para apontar para a sua aplicação pública, por exemplo usando o **ngrok**:
   ```bash
   ngrok http 5000
   ```
   Pegue a URL gerada (ex.: `https://abcd-1234.ngrok.app`) e cole no campo **WHEN A MESSAGE COMES IN** do Sandbox, com o sufixo `/whatsapp`.
4) Rode o bot localmente:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env
   python app.py
   ```
5) No seu WhatsApp (já conectado ao Sandbox), envie qualquer mensagem para iniciar. Responda `ACEITO` para registrar o consentimento.
6) Para exportar os dados: acesse `GET /export.csv` (ex.: `https://abcd-1234.ngrok.app/export.csv`).

## 📦 Estrutura do projeto

- `app.py` — servidor Flask, lógica do questionário, classificação de risco, persistência (SQLite).
- `requirements.txt` — dependências Python.
- `.env.example` — exemplo de configuração.
- `export.csv` — endpoint para exportar respostas.

## 🔒 Privacidade e Ética (LGPD)

- Este chatbot coleta **dados sensíveis** (saúde). Use *apenas* com consentimento explícito.
- Mantenha o banco de dados protegido e com acesso restrito.
- Evite coletar dados pessoais desnecessários; no exemplo, pedimos somente **iniciais**.
- Inclua termo de consentimento claro e opção de **SAIR** a qualquer momento.
- O bot **não** substitui atendimento médico. Em emergência, ligar 192 (SAMU).

## 🚀 Produção com número próprio

Para usar o seu número:
- **Meta WhatsApp Cloud API**: verificar o seu Facebook Business, cadastrar o número e gerar o token do WABA. Configurar um webhook HTTPS público e templates de mensagem aprovados.
- **BSP (ex.: Twilio)**: contratar um número WhatsApp e concluir a verificação. Reapontar o webhook do número para `POST /whatsapp` da sua aplicação.

A lógica do `app.py` é independente do provedor; a diferença está no provisionamento do **número** e do **webhook**.
