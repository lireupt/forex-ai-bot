"""IA agregadora (Camada 4) — voto/validação sobre o snapshot completo do mercado.

Ao contrário de `ai_analyst` (que contextualiza notícias/macro), esta camada recebe
um snapshot estruturado com TODOS os dados já apurados — técnico multi-timeframe,
fundamental/eventos, risco operacional, contexto de performance recente, estado dos
filtros e a recomendação técnica preliminar — e devolve um parecer agregado.

IMPORTANTE: esta IA NÃO executa trades e, na fase shadow, NÃO influencia a decisão
final. O resultado é apenas registado e comparado. Os gates fixos continuam soberanos.
"""

import json
import os
import time

from dotenv import load_dotenv

from modules.ai_analyst import (
    GROQ_BASE_URL,
    GROQ_MODEL,
    CLAUDE_MODEL,
    model_version_for_provider,
)

load_dotenv()


SYSTEM_PROMPT = """És um agregador de decisão para um bot de forex (EUR/USD) em paper trading.

Recebes um SNAPSHOT COMPLETO do estado do mercado: indicadores técnicos multi-timeframe,
contexto fundamental/notícias, eventos económicos, risco operacional, performance recente,
estado dos filtros e a recomendação técnica preliminar do sistema. A tua função é emitir um
VOTO AGREGADOR e uma camada de validação — NÃO és executor de trades.

REGRAS OBRIGATÓRIAS:
1. Respondes SEMPRE com um único objeto JSON válido, sem texto antes ou depois, sem markdown, sem ```json.
2. O JSON deve conter EXACTAMENTE estes campos:
   - "ai_aggregated_signal": uma de "BUY", "SELL", "NEUTRAL".
   - "ai_aggregated_confidence": inteiro 0-100.
   - "reasoning_summary": string curta em português (1-3 frases) a justificar o voto.
   - "risk_level": uma de "low", "medium", "high".
   - "supporting_factors": lista de strings curtas que apoiam o sinal.
   - "contradicting_factors": lista de strings curtas que contrariam o sinal.
   - "should_trade": booleano. true só quando o conjunto de evidências justifica abrir trade.
   - "should_reduce_risk": booleano. true quando há motivos para reduzir exposição (volatilidade, eventos, perdas recentes).
   - "warnings": lista de strings curtas com alertas operacionais relevantes.
3. NÃO devolvas ordens, preços, stop-loss nem take-profit. És um voto, não execução.
4. Pondera TODAS as camadas, não só as notícias. Se a técnica multi-timeframe e o contexto
   se contradizem, reflecte isso em "contradicting_factors" e baixa a confiança.
5. Se a evidência for fraca, contraditória ou houver risco operacional elevado, usa
   "NEUTRAL"/should_trade=false e explica em "warnings"."""


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _clamp_int(value, lo, hi, default=0):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _direction(signal):
    if signal == "BUY":
        return 1.0
    if signal == "SELL":
        return -1.0
    return 0.0


def aggregated_score(signal, confidence):
    """Converte (signal, confidence 0-100) num score contínuo em [-1, 1]."""
    return round(_direction(signal) * _clamp_int(confidence, 0, 100) / 100.0, 4)


def _format_block(title, lines):
    body = "\n".join(lines) if lines else "(sem dados)"
    return f"{title}:\n{body}"


def _build_user_message(snapshot):
    snapshot = snapshot or {}
    technical = snapshot.get("technical") or {}
    fundamental = snapshot.get("fundamental") or {}
    operational = snapshot.get("operational_risk") or {}
    performance = snapshot.get("performance") or {}
    filters = snapshot.get("filters") or {}
    preliminary = snapshot.get("preliminary_recommendation") or {}

    technical_lines = [
        f"- Preço actual: {technical.get('current_price')}",
        f"- RSI(14): {technical.get('rsi')} ({technical.get('rsi_signal')})",
        f"- EMA20 vs EMA50: {technical.get('ema20')} vs {technical.get('ema50')} -> {technical.get('ema_trend')}",
        f"- MACD: {technical.get('macd')} vs signal {technical.get('macd_signal_value')} -> {technical.get('macd_signal')}",
        f"- ATR(14): {technical.get('atr_pips')} pips ({technical.get('volatility_reason')})",
        f"- ADX(14): {technical.get('adx')}",
        f"- Multi-TF score: {technical.get('multi_timeframe_score')} "
        f"(M15={technical.get('technical_score_m15')}, H1={technical.get('technical_score_h1')}, "
        f"H4={technical.get('technical_score_h4')}, D1={technical.get('technical_score_d1')})",
        f"- Alinhamento timeframes: {technical.get('timeframe_alignment')}",
        f"- Sinal técnico: {technical.get('technical_signal')} (score {technical.get('technical_score')})",
    ]

    fundamental_lines = [
        f"- Sinal/bias IA contextual: {fundamental.get('ai_bias')} (conf {fundamental.get('ai_confidence')}%)",
        f"- Contexto macro: {fundamental.get('macro_context')}",
        f"- Sentimento notícias: {fundamental.get('news_sentiment')}",
        f"- Contexto volatilidade: {fundamental.get('volatility_context')}",
        f"- Evento high-impact próximo: {fundamental.get('dangerous_event_nearby')} "
        f"({fundamental.get('dangerous_event_reason') or 'n/a'})",
        f"- Leitura IA: {fundamental.get('ai_reason') or 'n/a'}",
    ]

    operational_lines = [
        f"- Mercado aberto: {operational.get('market_open')} (sessão {operational.get('session')})",
        f"- Janela operacional pode abrir trade: {operational.get('can_open_trade')} "
        f"({operational.get('operational_block_reason') or 'ok'})",
        f"- Cooldown activo: {operational.get('cooldown_active')}",
        f"- Persistência do sinal: {operational.get('signal_persistence')}",
        f"- Spread (pips): {operational.get('spread_pips')}",
    ]

    performance_lines = [
        f"- Winrate recente: {performance.get('winrate')}",
        f"- Expectancy: {performance.get('expectancy')}",
        f"- Net pips: {performance.get('net_pips')}",
        f"- Perdas consecutivas: {performance.get('loss_streak')}",
        f"- Max drawdown: {performance.get('max_drawdown')}",
        f"- BUY vs SELL: {performance.get('buy_vs_sell')}",
        f"- Bloqueios por razão: {performance.get('blocked_by_reason')}",
    ]

    filters_lines = [
        f"- DRY_RUN: {filters.get('dry_run')}",
        f"- allow_buy/allow_sell: {filters.get('allow_buy')}/{filters.get('allow_sell')}",
        f"- Bloqueio evento high-impact activo: {filters.get('block_near_high_impact_events')}",
        f"- Gate técnico (momentum/ATR) notas: {filters.get('gate_reasons')}",
        f"- Block reason actual: {filters.get('block_reason') or 'n/a'}",
        f"- Trade permitido pelo sistema: {filters.get('trade_allowed')}",
    ]

    preliminary_lines = [
        f"- Recomendação combinada: {preliminary.get('combined_signal')} (conf {preliminary.get('combined_confidence')}%)",
        f"- Sinal de gating efectivo: {preliminary.get('gating_signal')} (modo {preliminary.get('gating_mode')})",
        f"- Combined score: {preliminary.get('combined_score')}",
        f"- hold_off: {preliminary.get('hold_off')}",
    ]

    sections = [
        f"Par: {snapshot.get('pair', 'EUR/USD')}",
        "",
        _format_block("CAMADA 1 — TÉCNICO", technical_lines),
        "",
        _format_block("CAMADA 2 — FUNDAMENTAL/EVENTOS", fundamental_lines),
        "",
        _format_block("CAMADA 3 — PERFORMANCE/CONTEXTO RECENTE", performance_lines),
        "",
        _format_block("RISCO OPERACIONAL", operational_lines),
        "",
        _format_block("ESTADO DOS FILTROS/LIMITAÇÕES", filters_lines),
        "",
        _format_block("RECOMENDAÇÃO TÉCNICA PRELIMINAR", preliminary_lines),
    ]

    weekly_prep = snapshot.get("weekly_market_prep") or {}
    if weekly_prep:
        weekly_lines = [
            f"- Bias macro semanal: {weekly_prep.get('macro_bias')}",
            f"- Direcção preferida: {weekly_prep.get('preferred_direction')} (conf {weekly_prep.get('confidence')}%)",
            f"- Nível de risco: {weekly_prep.get('risk_level')}",
            f"- Recomendação: {weekly_prep.get('recommendation')}",
            f"- Resumo: {weekly_prep.get('summary') or 'n/a'}",
            f"- Raciocínio: {weekly_prep.get('reasoning_summary') or 'n/a'}",
            f"- Avisos: {', '.join(weekly_prep.get('warnings') or []) or 'n/a'}",
            f"- Semana: {weekly_prep.get('week_start')} (gerado {weekly_prep.get('created_at', 'n/a')})",
        ]
        sections += ["", _format_block("CONTEXTO SEMANAL (Weekly Market Prep)", weekly_lines)]

    sections += ["", "Analisa o conjunto e devolve o JSON com o teu voto agregador."]
    return "\n".join(sections)


def build_aggregation_input(snapshot):
    return _build_user_message(snapshot)


def _fallback(provider, error_msg):
    return {
        "ai_aggregated_signal": "NEUTRAL",
        "ai_aggregated_confidence": 0,
        "reasoning_summary": f"Agregador indisponível: {error_msg}",
        "risk_level": "high",
        "supporting_factors": [],
        "contradicting_factors": [],
        "should_trade": False,
        "should_reduce_risk": True,
        "warnings": [f"agregador_falhou: {error_msg}"],
        "ai_aggregated_score": 0.0,
        "provider": provider,
        "model_version": model_version_for_provider(provider),
    }


def _failed(provider, error_msg):
    result = _fallback(provider, error_msg)
    result["status"] = "failed"
    result["error"] = error_msg
    return result


def _strip_json_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _validate(result):
    required = (
        "ai_aggregated_signal",
        "ai_aggregated_confidence",
        "reasoning_summary",
        "risk_level",
        "supporting_factors",
        "contradicting_factors",
        "should_trade",
        "should_reduce_risk",
        "warnings",
    )
    for field in required:
        if field not in result:
            raise ValueError(f"campo '{field}' em falta na resposta")

    signal = str(result.get("ai_aggregated_signal") or "NEUTRAL").upper()
    if signal not in {"BUY", "SELL", "NEUTRAL"}:
        signal = "NEUTRAL"
    result["ai_aggregated_signal"] = signal
    result["ai_aggregated_confidence"] = _clamp_int(result.get("ai_aggregated_confidence"), 0, 100, 0)

    risk_level = str(result.get("risk_level") or "medium").lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "medium"
    result["risk_level"] = risk_level

    result["reasoning_summary"] = str(result.get("reasoning_summary") or "").strip()
    result["supporting_factors"] = _as_list(result.get("supporting_factors"))
    result["contradicting_factors"] = _as_list(result.get("contradicting_factors"))
    result["warnings"] = _as_list(result.get("warnings"))
    result["should_trade"] = bool(result.get("should_trade"))
    result["should_reduce_risk"] = bool(result.get("should_reduce_risk"))
    result["ai_aggregated_score"] = aggregated_score(signal, result["ai_aggregated_confidence"])
    return result


def _analyse_groq(user_message):
    from openai import OpenAI

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key == "PLACEHOLDER":
        raise RuntimeError("GROQ_API_KEY não configurada")

    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        max_tokens=1024,
    )
    return response.choices[0].message.content


def _analyse_claude(user_message):
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        raise RuntimeError("ANTHROPIC_API_KEY não configurada")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    return text


def analyse(snapshot, provider=None):
    provider = (provider or os.getenv("AI_AGGREGATOR_PROVIDER") or os.getenv("AI_PROVIDER") or "groq").strip().lower()
    user_message = _build_user_message(snapshot)
    max_retries = int(float(os.getenv("AI_MAX_RETRIES") or 3))
    backoff_seconds = float(os.getenv("AI_RETRY_BACKOFF_SECONDS") or 5)

    if provider not in {"groq", "claude"}:
        return _failed(provider, f"AI_AGGREGATOR_PROVIDER='{provider}' inválido (usa 'groq' ou 'claude')")

    attempts = max(1, max_retries)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _analyse_groq(user_message) if provider == "groq" else _analyse_claude(user_message)
            result = json.loads(_strip_json_fences(raw))
            _validate(result)
            result["provider"] = provider
            result["model_version"] = model_version_for_provider(provider)
            result["status"] = "ok"
            return result
        except json.JSONDecodeError as e:
            return _failed(provider, f"resposta não é JSON válido ({e})")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)

    return _failed(provider, last_error or "falha desconhecida")
