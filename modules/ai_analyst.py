import json
import os
import time

from dotenv import load_dotenv

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


def model_version_for_provider(provider):
    provider = (provider or "").strip().lower()
    if provider == "groq":
        return f"groq:{GROQ_MODEL}"
    if provider == "claude":
        return f"claude:{CLAUDE_MODEL}"
    return provider or "unknown"

SYSTEM_PROMPT = """És um analista financeiro especializado em forex (mercado cambial).

Recebes notícias e eventos económicos relevantes para um par de moedas. Pode também ser-te dado um snapshot técnico multi-timeframe. A tua tarefa é contextualizar o mercado macro/fundamental. A direção operacional pertence aos indicadores técnicos; tu NÃO és executor de trades.

REGRAS OBRIGATÓRIAS:
1. Respondes SEMPRE com um único objeto JSON válido, sem texto antes ou depois, sem markdown, sem ```json.
2. O JSON deve conter EXACTAMENTE estes campos:
   - "bias": uma de "BUY", "SELL", "NEUTRAL"
   - "confidence_adjustment": número entre -0.25 e 0.25. Positivo reforça o bias; negativo enfraquece ou contraria o bias.
   - "risk_adjustment": número entre -0.50 e 0.50. Negativo reduz risco; positivo aumenta ligeiramente risco.
   - "macro_context": string curta, por exemplo "bullish_eur", "bullish_usd", "mixed", "neutral"
   - "volatility_context": uma de "low", "medium", "high", "event_risk"
   - "news_sentiment": uma de "positive", "negative", "mixed", "neutral"
   - "reason": string em português explicando a leitura macro/sentimento em 1-3 frases.
   - "hold_off": booleano. Define como true APENAS quando há evento high-impact iminente, notícia contraditória extrema, liquidez anormal ou risco macro perigoso.
3. NÃO devolvas uma ordem de trade. O campo "bias" é contexto, não execução.
4. Se o contexto for fraco ou contraditório, usa bias "NEUTRAL", confidence_adjustment perto de 0 e risk_adjustment negativo ou zero."""


def _format_news(news):
    if not news:
        return "(sem notícias relevantes)"
    # Últimas 5 notícias (lista ordenada por id ASC → tail = mais recentes)
    lines = []
    for i, art in enumerate(news[-5:], 1):
        title = art.get("title", "").strip()
        source = art.get("source", "")
        lines.append(f"{i}. [{source}] {title}")
    return "\n".join(lines)


def _format_events(events):
    if not events:
        return "(sem eventos de alto/médio impacto)"
    # Apenas high/medium impact, máx 10 eventos
    relevant = [
        e for e in events
        if (e.get("impact") or "").lower() in ("high", "medium")
    ]
    if not relevant:
        return "(sem eventos de alto/médio impacto)"
    lines = []
    for ev in relevant[:10]:
        time = ev.get("time", "")
        currency = ev.get("currency", "")
        event = ev.get("event", "")
        impact = ev.get("impact", "")
        extras = []
        for key in ("previous", "forecast", "actual"):
            val = ev.get(key)
            if val:
                extras.append(f"{key}: {val}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"- [{impact}] {time} {currency} {event}{suffix}".strip())
    return "\n".join(lines)


def _format_technical(technical):
    if not technical:
        return None

    indicators = technical.get("indicators") or {}
    if not indicators:
        return None

    def _f(value, suffix=""):
        if value is None:
            return "n/a"
        return f"{value}{suffix}"

    lines = [
        f"- Preço actual: {_f(indicators.get('current_price'))}",
        f"- RSI(14): {_f(indicators.get('rsi'))} ({indicators.get('rsi_signal', 'neutral')})",
        f"- EMA20 vs EMA50: {_f(indicators.get('ema20'))} vs {_f(indicators.get('ema50'))} -> {indicators.get('ema_trend', 'neutral')}",
        f"- MACD: {_f(indicators.get('macd'))} vs signal {_f(indicators.get('macd_signal_value'))} -> {indicators.get('macd_signal', 'neutral')}",
        f"- ATR(14): {_f(indicators.get('atr_pips'), ' pips')} ({indicators.get('volatility_reason', '')})",
    ]

    technical_signal = technical.get("signal")
    if technical_signal:
        lines.append(
            f"- Resumo técnico estrito: {technical_signal} "
            f"(confiança {technical.get('confidence', 0)}%) — {technical.get('technical_reason', '')}"
        )
    return "\n".join(lines)


def _format_macro_snapshot(snapshot):
    if not snapshot:
        return "(sem snapshot macro disponível)"
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _build_user_message(news, events, pair, technical=None, macro_context_snapshot=None):
    sections = [
        f"Par: {pair}",
        "",
        "NOTÍCIAS RELEVANTES:",
        _format_news(news),
        "",
        "EVENTOS ECONÓMICOS DE ALTO IMPACTO:",
        _format_events(events),
        "",
        "SNAPSHOT DO FILTRO MACRO PARA ESTA DECISÃO:",
        _format_macro_snapshot(macro_context_snapshot),
    ]

    technical_block = _format_technical(technical)
    if technical_block:
        sections += ["", "SNAPSHOT TÉCNICO ACTUAL:", technical_block]

    sections += ["", "Analisa e devolve o JSON com a tua decisão."]
    return "\n".join(sections)


def build_analysis_input(
    news,
    events,
    pair="EUR/USD",
    technical=None,
    macro_context_snapshot=None,
):
    return _build_user_message(
        news,
        events,
        pair,
        technical=technical,
        macro_context_snapshot=macro_context_snapshot,
    )


def _fallback(provider, error_msg):
    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "reasoning": f"Erro na análise: {error_msg}",
        "risk_level": "HIGH",
        "hold_off": True,
        "bias": "NEUTRAL",
        "confidence_adjustment": 0.0,
        "risk_adjustment": -0.25,
        "macro_context": "unknown",
        "volatility_context": "event_risk",
        "news_sentiment": "neutral",
        "reason": f"Erro na análise contextual: {error_msg}",
        "provider": provider,
        "model_version": model_version_for_provider(provider),
    }


def _failed(provider, error_msg):
    result = _fallback(provider, error_msg)
    result["status"] = "failed"
    result["error"] = error_msg
    return result


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _clamp_float(value, lo, hi, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _normalise_contextual(result):
    if "bias" not in result and "signal" in result:
        signal = result.get("signal", "NEUTRAL")
        confidence = _clamp_float(result.get("confidence"), 0, 100, 0) / 100
        result["bias"] = signal if signal in {"BUY", "SELL", "NEUTRAL"} else "NEUTRAL"
        result["confidence_adjustment"] = round(confidence * 0.20, 4)
        result["risk_adjustment"] = -0.15 if result.get("risk_level") == "HIGH" else 0.0
        result["macro_context"] = "legacy_signal"
        result["volatility_context"] = "medium"
        result["news_sentiment"] = "neutral"
        result["reason"] = result.get("reasoning", "")

    required = (
        "bias",
        "confidence_adjustment",
        "risk_adjustment",
        "macro_context",
        "volatility_context",
        "news_sentiment",
        "reason",
        "hold_off",
    )
    for field in required:
        if field not in result:
            raise ValueError(f"campo '{field}' em falta na resposta")

    bias = str(result.get("bias") or "NEUTRAL").upper()
    if bias not in {"BUY", "SELL", "NEUTRAL"}:
        bias = "NEUTRAL"
    result["bias"] = bias
    result["confidence_adjustment"] = round(
        _clamp_float(result.get("confidence_adjustment"), -0.25, 0.25), 4
    )
    result["risk_adjustment"] = round(
        _clamp_float(result.get("risk_adjustment"), -0.50, 0.50), 4
    )
    result["hold_off"] = bool(result.get("hold_off"))
    result["reason"] = str(result.get("reason") or "").strip()

    # Campos legados preservados para cache, dashboard e testes existentes.
    result.setdefault("signal", bias)
    # confidence_adjustment está em [-0.25, 0.25]. Normalizar para [0, 100]:
    # 0.25 → 100, 0.0 → 0. Sem esta divisão fica limitado a 25, sempre abaixo
    # de AI_VOTE_MIN_CONFIDENCE=35 → IA abstém-se em 100% dos ciclos.
    result.setdefault(
        "confidence",
        int(round(abs(result["confidence_adjustment"]) / 0.25 * 100)),
    )
    result.setdefault("reasoning", result["reason"])
    result.setdefault("risk_level", "HIGH" if result["risk_adjustment"] < -0.2 else "MEDIUM")
    return result


def _validate(result):
    return _normalise_contextual(result)


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
    if response.usage:
        u = response.usage
        print(
            f"[ai-tokens] module=ai_analyst provider=groq "
            f"prompt={u.prompt_tokens} completion={u.completion_tokens} "
            f"total={u.total_tokens}"
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
    if response.usage:
        u = response.usage
        print(
            f"[ai-tokens] module=ai_analyst provider=claude "
            f"prompt={u.input_tokens} completion={u.output_tokens} "
            f"total={u.input_tokens + u.output_tokens}"
        )
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    return text


def analyse(news, events, pair="EUR/USD", technical=None, macro_context_snapshot=None):
    provider = (os.getenv("AI_PROVIDER") or "groq").strip().lower()
    user_message = _build_user_message(
        news,
        events,
        pair,
        technical=technical,
        macro_context_snapshot=macro_context_snapshot,
    )
    max_retries = int(float(os.getenv("AI_MAX_RETRIES") or 3))
    backoff_seconds = float(os.getenv("AI_RETRY_BACKOFF_SECONDS") or 5)

    if provider not in {"groq", "claude"}:
        return _failed(
            provider,
            f"AI_PROVIDER='{provider}' inválido (usa 'groq' ou 'claude')",
        )

    attempts = max(1, max_retries)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if provider == "groq":
                raw = _analyse_groq(user_message)
            else:
                raw = _analyse_claude(user_message)

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


if __name__ == "__main__":
    sample_news = [
        {
            "source": "Reuters",
            "title": "ECB hints at further rate cuts as eurozone growth slows",
            "summary": "European Central Bank officials suggest dovish stance.",
        },
        {
            "source": "ForexLive",
            "title": "Fed signals no rush to cut rates, dollar strengthens",
            "summary": "US central bank holds firm on inflation outlook.",
        },
    ]
    sample_events = [
        {
            "time": "13:30",
            "currency": "USD",
            "event": "Non-Farm Payrolls",
            "previous": "180K",
            "forecast": "200K",
            "actual": "",
        }
    ]

    print(f"Provider activo: {(os.getenv('AI_PROVIDER') or 'groq').lower()}\n")
    result = analyse(sample_news, sample_events)
    print(json.dumps(result, indent=2, ensure_ascii=False))
