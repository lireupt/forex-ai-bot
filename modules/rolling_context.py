"""Rolling Market Context — memória contextual contínua do mercado.

Mantém e atualiza um resumo evolutivo do contexto macro/técnico ao longo
do dia. Em cada ciclo horário, a IA recebe o contexto anterior + snapshot
atual e produz uma atualização: o que mudou, se o viés se mantém, riscos
atuais e intenção provável do mercado.

IMPORTANTE: Este módulo NÃO abre trades, NÃO bloqueia trades e NÃO altera
o gating. É exclusivamente memória/contexto para a IA agregadora.
"""

import json
import os

from dotenv import load_dotenv

from modules.ai_analyst import (
    GROQ_BASE_URL,
    GROQ_MODEL,
    CLAUDE_MODEL,
)

load_dotenv()

SYSTEM_PROMPT = """És um analista de contexto de mercado forex (EUR/USD).

A tua função é manter uma MEMÓRIA CONTEXTUAL CONTÍNUA do mercado. Recebes:
- O contexto anterior (o que a IA sabia na última hora).
- O snapshot atual: indicadores técnicos, notícias recentes, eventos económicos,
  dados de volatilidade (ATR), sinal combinado, resultado da IA fundamental,
  resultado da IA agregadora e performance recente.

A tua tarefa é analisar a EVOLUÇÃO, não apenas o estado atual, e produzir uma
atualização contextual que responda:
1. De onde vem o mercado.
2. O que mudou desde a última análise.
3. Se o viés anterior se mantém ou foi invalidado.
4. Quais os riscos atuais.
5. Qual a intenção provável do mercado.
6. Qual a postura recomendada para o sistema de trading.

REGRAS OBRIGATÓRIAS:
1. Respondes SEMPRE com um único objeto JSON válido, sem texto antes ou depois, sem markdown, sem ```json.
2. O JSON deve conter EXACTAMENTE estes campos:
   - "market_phase": uma de "trend", "range", "transition", "uncertain"
   - "macro_bias": uma de "bullish_eur", "bearish_eur", "neutral", "mixed"
   - "technical_bias": uma de "BUY", "SELL", "NEUTRAL"
   - "combined_bias": uma de "BUY", "SELL", "NEUTRAL"
   - "confidence": inteiro 0-100
   - "risk_level": uma de "low", "medium", "high"
   - "short_summary": string curta em português (1-2 frases) do estado atual
   - "what_changed": string descrevendo o que mudou desde o contexto anterior
   - "persistent_factors": lista de strings com fatores que continuam válidos
   - "new_factors": lista de strings com novos fatores relevantes
   - "invalidated_factors": lista de strings com fatores anteriores que deixaram de ser válidos
   - "key_risks": lista de strings com principais riscos atuais
   - "likely_market_intent": string descrevendo a intenção provável do mercado
   - "recommended_stance": uma de "trade_normally", "reduce_risk", "avoid_new_trades", "wait_for_confirmation"
   - "should_trade_bias": booleano — true se o contexto suporta trading alinhado com o viés
   - "should_reduce_risk": booleano — true se há motivos para reduzir exposição
   - "warnings": lista de strings com alertas operacionais
3. Se não há contexto anterior, analisa apenas o snapshot atual como ponto de partida.
4. Sê conservador e honesto. Se a evidência for ambígua, reflecte isso na confiança baixa.
5. NÃO devolvas ordens, preços, stop-loss nem take-profit. És memória/contexto, não executor."""

_VALID_MARKET_PHASES = {"trend", "range", "transition", "uncertain"}
_VALID_MACRO_BIAS = {"bullish_eur", "bearish_eur", "neutral", "mixed"}
_VALID_TECHNICAL_BIAS = {"BUY", "SELL", "NEUTRAL"}
_VALID_COMBINED_BIAS = {"BUY", "SELL", "NEUTRAL"}
_VALID_RISK_LEVELS = {"low", "medium", "high"}
_VALID_STANCE = {
    "trade_normally", "reduce_risk", "avoid_new_trades", "wait_for_confirmation"
}

FALLBACK = {
    "market_phase": "uncertain",
    "macro_bias": "neutral",
    "technical_bias": "NEUTRAL",
    "combined_bias": "NEUTRAL",
    "confidence": 0,
    "risk_level": "medium",
    "short_summary": "Contexto não disponível — falha da IA.",
    "what_changed": "",
    "persistent_factors": [],
    "new_factors": [],
    "invalidated_factors": [],
    "key_risks": [],
    "likely_market_intent": "Desconhecido.",
    "recommended_stance": "wait_for_confirmation",
    "should_trade_bias": False,
    "should_reduce_risk": False,
    "warnings": ["rolling_context_failed"],
}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _clamp_int(value, lo, hi, default=0):
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def normalise(raw):
    """Normaliza a resposta da IA para o formato esperado."""
    if not raw or not isinstance(raw, dict):
        return dict(FALLBACK)

    market_phase = str(raw.get("market_phase") or "uncertain").lower()
    if market_phase not in _VALID_MARKET_PHASES:
        market_phase = "uncertain"

    macro_bias = str(raw.get("macro_bias") or "neutral").lower()
    if macro_bias not in _VALID_MACRO_BIAS:
        macro_bias = "neutral"

    technical_bias = str(raw.get("technical_bias") or "NEUTRAL").upper()
    if technical_bias not in _VALID_TECHNICAL_BIAS:
        technical_bias = "NEUTRAL"

    combined_bias = str(raw.get("combined_bias") or "NEUTRAL").upper()
    if combined_bias not in _VALID_COMBINED_BIAS:
        combined_bias = "NEUTRAL"

    confidence = _clamp_int(raw.get("confidence"), 0, 100, 0)

    risk_level = str(raw.get("risk_level") or "medium").lower()
    if risk_level not in _VALID_RISK_LEVELS:
        risk_level = "medium"

    recommended_stance = str(raw.get("recommended_stance") or "wait_for_confirmation").lower()
    if recommended_stance not in _VALID_STANCE:
        recommended_stance = "wait_for_confirmation"

    return {
        "market_phase": market_phase,
        "macro_bias": macro_bias,
        "technical_bias": technical_bias,
        "combined_bias": combined_bias,
        "confidence": confidence,
        "risk_level": risk_level,
        "short_summary": str(raw.get("short_summary") or "").strip() or "Sem resumo.",
        "what_changed": str(raw.get("what_changed") or "").strip(),
        "persistent_factors": _as_list(raw.get("persistent_factors")),
        "new_factors": _as_list(raw.get("new_factors")),
        "invalidated_factors": _as_list(raw.get("invalidated_factors")),
        "key_risks": _as_list(raw.get("key_risks")),
        "likely_market_intent": str(raw.get("likely_market_intent") or "").strip(),
        "recommended_stance": recommended_stance,
        "should_trade_bias": _coerce_bool(raw.get("should_trade_bias")),
        "should_reduce_risk": _coerce_bool(raw.get("should_reduce_risk")),
        "warnings": _as_list(raw.get("warnings")),
    }


def _build_user_message(previous_context, snapshot, lookback_hours, max_prev_chars):
    """Constrói a mensagem de input para a IA."""
    parts = []

    if previous_context:
        prev_risks = previous_context.get("key_risks_json") or previous_context.get("key_risks") or []
        if isinstance(prev_risks, str):
            try:
                prev_risks = json.loads(prev_risks)
            except Exception:
                prev_risks = [prev_risks]

        prev_text = (
            f"CONTEXTO ANTERIOR (em {previous_context.get('created_at', 'desconhecido')}):\n"
            f"  market_phase: {previous_context.get('market_phase', '-')}\n"
            f"  combined_bias: {previous_context.get('combined_bias', '-')}\n"
            f"  recommended_stance: {previous_context.get('recommended_stance', '-')}\n"
            f"  likely_market_intent: {previous_context.get('likely_market_intent', '-')}\n"
            f"  short_summary: {previous_context.get('short_summary', '-')}\n"
            f"  key_risks: {json.dumps(prev_risks, ensure_ascii=False)}\n"
        )
        if len(prev_text) > max_prev_chars:
            prev_text = prev_text[:max_prev_chars] + "...[truncado]"
        parts.append(prev_text)
    else:
        parts.append("CONTEXTO ANTERIOR: Nenhum — este é o primeiro ciclo de análise.")

    parts.append(f"\nSNAPSHOT ATUAL (lookback {lookback_hours}h):")

    tech = snapshot.get("technical") or {}
    parts.append(
        f"  Preço atual: {tech.get('current_price')}\n"
        f"  RSI: {tech.get('rsi')} ({tech.get('rsi_signal')})\n"
        f"  EMA trend: {tech.get('ema_trend')}\n"
        f"  MACD: {tech.get('macd_signal')}\n"
        f"  ATR: {tech.get('atr_pips')} pips\n"
        f"  ADX: {tech.get('adx')}\n"
        f"  Sinal técnico: {tech.get('technical_signal')}\n"
        f"  Multi-TF score: {tech.get('multi_timeframe_score')}\n"
        f"  Timeframe alignment: {tech.get('timeframe_alignment')}"
    )

    fund = snapshot.get("fundamental") or {}
    parts.append(
        f"  IA fundamental: bias={fund.get('ai_bias')} conf={fund.get('ai_confidence')}%\n"
        f"  Macro context: {fund.get('macro_context')}\n"
        f"  News sentiment: {fund.get('news_sentiment')}\n"
        f"  Volatility context: {fund.get('volatility_context')}\n"
        f"  Evento perigoso: {fund.get('dangerous_event_nearby')} "
        f"— {fund.get('dangerous_event_reason')}"
    )

    prelim = snapshot.get("preliminary_recommendation") or {}
    parts.append(
        f"  Sinal combinado: {prelim.get('combined_signal')} "
        f"({prelim.get('combined_confidence')}%)\n"
        f"  Score combinado: {prelim.get('combined_score')}\n"
        f"  Hold off: {prelim.get('hold_off')}"
    )

    agg = snapshot.get("ai_aggregator") or {}
    if agg:
        parts.append(
            f"  IA agregadora: signal={agg.get('signal')} conf={agg.get('confidence')}% "
            f"should_trade={agg.get('should_trade')} risk={agg.get('risk_level')}"
        )

    perf = snapshot.get("performance") or {}
    if perf:
        parts.append(
            f"  Performance ({perf.get('window_days', 7)}d): "
            f"winrate={perf.get('winrate')}% net_pips={perf.get('net_pips')} "
            f"loss_streak={perf.get('loss_streak')}"
        )

    weekly = snapshot.get("weekly_market_prep") or {}
    if weekly:
        parts.append(
            f"  Weekly prep: macro_bias={weekly.get('macro_bias')} "
            f"preferred_direction={weekly.get('preferred_direction')} "
            f"risk_level={weekly.get('risk_level')}"
        )

    return "\n".join(parts)


def _call_groq(prompt):
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("GROQ_API_KEY") or "",
        base_url=GROQ_BASE_URL,
    )
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    if response.usage:
        u = response.usage
        print(
            f"[ai-tokens] module=rolling_context provider=groq "
            f"prompt={u.prompt_tokens} completion={u.completion_tokens} "
            f"total={u.total_tokens}"
        )
    return (response.choices[0].message.content or "").strip()


def _call_claude(prompt):
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY") or "")
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if message.usage:
        u = message.usage
        print(
            f"[ai-tokens] module=rolling_context provider=claude "
            f"prompt={u.input_tokens} completion={u.output_tokens} "
            f"total={u.input_tokens + u.output_tokens}"
        )
    content = message.content[0].text if message.content else ""
    return content.strip()


def _parse_response(text):
    """Faz parse do JSON da resposta da IA."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None


def update(
    conn,
    pair,
    snapshot,
    aggregator_result=None,
    provider=None,
    lookback_hours=24,
    max_prev_chars=2500,
):
    """Atualiza o rolling context para o par dado.

    Sempre não-fatal: em caso de falha retorna o fallback e grava warning.
    NÃO abre trades, NÃO bloqueia trades, NÃO altera gating.

    Args:
        conn: conexão SQLite
        pair: par de moedas (e.g. "EUR/USD")
        snapshot: snapshot de mercado do ciclo atual (de context_snapshot)
        aggregator_result: resultado da IA agregadora (opcional)
        provider: "groq" | "claude" | None (usa ROLLING_CONTEXT_PROVIDER ou AI_PROVIDER)
        lookback_hours: horas de lookback para o contexto
        max_prev_chars: limite de caracteres do contexto anterior

    Returns:
        dict: contexto normalizado (nunca None)
    """
    from modules import database

    provider = (
        provider
        or os.getenv("ROLLING_CONTEXT_PROVIDER")
        or os.getenv("AI_PROVIDER")
        or "groq"
    ).strip().lower()

    try:
        previous_context = database.get_latest_rolling_market_context(conn, pair)
    except Exception as e:
        print(f"[rolling-context] leitura anterior falhou (não-fatal): {type(e).__name__}: {e}")
        previous_context = None

    enriched_snapshot = dict(snapshot) if snapshot else {}
    if aggregator_result:
        enriched_snapshot["ai_aggregator"] = {
            "signal": aggregator_result.get("ai_aggregated_signal"),
            "confidence": aggregator_result.get("ai_aggregated_confidence"),
            "should_trade": aggregator_result.get("should_trade"),
            "risk_level": aggregator_result.get("risk_level"),
        }

    try:
        user_message = _build_user_message(
            previous_context, enriched_snapshot, lookback_hours, max_prev_chars
        )

        if provider in ("claude", "anthropic"):
            raw_text = _call_claude(user_message)
        else:
            raw_text = _call_groq(user_message)

        raw_parsed = _parse_response(raw_text)
        if raw_parsed is None:
            raise ValueError(f"resposta da IA não é JSON válido: {raw_text[:200]}")

        result = normalise(raw_parsed)

        try:
            saved_id = database.save_rolling_market_context(
                conn,
                pair=pair,
                data=result,
                previous_context_id=(
                    previous_context.get("id") if previous_context else None
                ),
                raw_response=raw_parsed,
            )
            result["id"] = saved_id
        except Exception as e:
            print(f"[rolling-context] gravação falhou (não-fatal): {type(e).__name__}: {e}")

        return result

    except Exception as e:
        print(f"[rolling-context] IA falhou (não-fatal): {type(e).__name__}: {e}")
        fallback = dict(FALLBACK)
        if previous_context:
            prev_summary = previous_context.get("short_summary", "")
            fallback["short_summary"] = f"[fallback] {prev_summary}"
        return fallback
