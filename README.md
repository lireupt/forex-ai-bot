# Forex AI Bot

Bot experimental de análise Forex para o par **EUR/USD**, com foco em decisão assistida por IA, análise técnica, cache local e simulação em modo **DRY RUN**.

> Este projeto não executa ordens reais. O objetivo atual é recolher dados, analisar sinais, simular decisões e validar a qualidade da estratégia antes de qualquer integração com broker.

## Funcionalidades

- Recolha de notícias financeiras via RSS/APIs.
- Recolha de eventos económicos relevantes.
- Análise fundamental com IA via Groq.
- Price feed com candles EUR/USD.
- Análise técnica com:
  - RSI 14
  - EMA 20 / EMA 50
  - MACD
  - ATR 14
- Classificação de volatilidade com base no ATR.
- Combinação entre sinal IA e sinal técnico.
- Modo simulado `DRY_RUN`.
- Gestão de risco simulada com SL/TP baseado em ATR.
- Bloqueio de trades perto de eventos económicos relevantes.
- Cache local em SQLite para reduzir chamadas externas.
- Logs em JSONL e SQLite.
- Dashboard estático simples servido por Nginx.
- Cron para execução automática.

## Estado atual

O bot encontra-se em fase de **observação e validação**.

Atualmente:
- não há execução real de ordens;
- não há integração ativa com OANDA;
- `DRY_RUN=True`;
- o sinal técnico estrito é conservador;
- o sinal shadow é usado apenas para comparação;
- os outcomes são avaliados após candles futuras.

## Estrutura principal

```text
forex-ai-bot/
├── main.py
├── modules/
│   ├── database.py
│   ├── technical.py
│   ├── risk.py
│   └── ...
├── scripts/
│   └── export_logs.py
├── web/
│   ├── index.html
│   └── data.json
├── logs/
│   ├── decisions.jsonl
│   └── cron.log
├── data/
│   └── forex_bot.db
├── docs/
│   └── deployment_server.md
├── requirements.txt
└── .env

```
Alterar dados do ficheiro .env para aceder API's exteriores para obtençao de dados.

## Instalação local
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

## Executar
venv/bin/python main.py
