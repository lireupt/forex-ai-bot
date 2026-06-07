"""Weekend Mode e preparação semanal do mercado Forex.

Weekend Mode:
- Activo quando o mercado Forex está fechado (sábado ou domingo antes da abertura).
- Actualiza notícias e calendário mas NÃO executa análise técnica, IA fundamental,
  nem cria paper trades. NÃO grava decisões duplicadas de market_closed_weekend.

Weekly Market Prep:
- Corre uma única vez por semana no domingo próximo de 1h antes da abertura do mercado.
- Recolhe notícias do fim-de-semana e calendário da próxima semana.
- Chama a IA em modo preparação semanal (NÃO abre trades, NÃO altera sinais técnicos).
- Grava o resultado em SQLite e exporta para data.json como contexto para a IA agregadora.

Variáveis .env relevantes:
    WEEKEND_MODE_ENABLED, WEEKEND_MODE_UPDATE_NEWS,
    WEEKEND_MODE_UPDATE_CALENDAR, WEEKEND_MODE_EXPORT_LOGS,
    WEEKLY_MARKET_PREP_ENABLED, WEEKLY_MARKET_PREP_WEEKDAY,
    WEEKLY_MARKET_PREP_HOUR_UTC, WEEKLY_MARKET_PREP_LOOKBACK_HOURS,
    WEEKLY_MARKET_PREP_CALENDAR_DAYS, WEEKLY_MARKET_PREP_USE_AI.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from modules.ai_analyst import (
    GROQ_BASE_URL,
    GROQ_MODEL,
    CLAUDE_MODEL,
    model_version_for_provider,
)

load_dotenv()


WEEKLY_PREP_SYSTEM_PROMPT = """És um analista de preparação semanal para um bot de forex (EUR/USD) em paper trading.

Recebes notícias do fim-de-semana e o calendário económico da semana seguinte.
A tua função é preparar o CONTEXTO para a semana — NÃO abres trades, NÃO alteras sinais
técnicos e NÃO ignoras gates. Esta análise é exclusivamente informação de contexto.

REGRAS OBRIGATÓRIAS:
1. Respondes SEMPRE com um único objeto JSON válido, sem texto antes ou depois, sem markdown.
2. O JSON deve conter EXACTAMENTE estes campos:
   - "pair": string, o par analisado (ex: "EUR/USD")
   - "macro_bias": uma de "bullish_eur", "bearish_eur", "neutral", "mixed"
   - "preferred_direction": uma de "BUY", "SELL", "NEUTRAL"
   - "confidence": inteiro 0-100
   - "risk_level": uma de "low", "medium", "high"
   - "summary": string em português (2-4 frases) com o panorama macro/fundamental
   - "key_weekend_news": lista de strings com notícias relevantes do fim-de-semana
   - "key_events_next_week": lista de strings com eventos importantes da próxima semana
   - "market_opening_risks": lista de strings com riscos na abertura de domingo
   - "recommendation": uma de "trade_normally", "reduce_risk", "avoid_first_hour", "avoid_until_events_pass"
   - "reasoning_summary": string em português (1-3 frases) com o raciocínio central
   - "warnings": lista de strings com avisos operacionais relevantes
3. Esta análise NÃO executa trades. Foca-se em contexto macro/fundamental.
4. Se a informação for escassa ou inconclusiva, usa "neutral"/"NEUTRAL" e explica em "warnings"."""


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def weekend_mode_config():
    return {
        "enabled": _env_bool("WEEKEND_MODE_ENABLED", True),
        "update_news": _env_bool("WEEKEND_MODE_UPDATE_NEWS", True),
        "update_calendar": _env_bool("WEEKEND_MODE_UPDATE_CALENDAR", True),
        "export_logs": _env_bool("WEEKEND_MODE_EXPORT_LOGS", True),
    }


def weekly_prep_config():
    return {
        "enabled": _env_bool("WEEKLY_MARKET_PREP_ENABLED", True),
        "weekday": _env_int("WEEKLY_MARKET_PREP_WEEKDAY", 6),
        "hour_utc": _env_int("WEEKLY_MARKET_PREP_HOUR_UTC", 21),
        "lookback_hours": _env_int("WEEKLY_MARKET_PREP_LOOKBACK_HOURS", 72),
        "calendar_days": _env_int("WEEKLY_MARKET_PREP_CALENDAR_DAYS", 7),
        "use_ai": _env_bool("WEEKLY_MARKET_PREP_USE_AI", True),
    }


def is_weekend_mode_active(now_utc=None):
    """True quando o mercado Forex está fechado (fim-de-semana)."""
    from modules.market import forex_market_state

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    state = forex_market_state(now_utc=now_utc)
    return not state["is_open"]


def is_weekly_prep_due(now_utc=None, conn=None, pair="EUR/USD"):
    """True se estiver no horário de preparação semanal e ainda não correu hoje."""
    config = weekly_prep_config()
    if not config["enabled"]:
        return False

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Deve ser o weekday configurado (default: domingo = 6)
    if now_utc.weekday() != config["weekday"]:
        return False

    # Deve estar na hora configurada ou depois
    if now_utc.hour < config["hour_utc"]:
        return False

    # Não correr depois da abertura do mercado
    from modules.market import market_guard_config

    mg = market_guard_config()
    if now_utc.weekday() == mg["open_weekday"] and now_utc.hour >= mg["open_hour_utc"]:
        return False

    # Verificar se já correu hoje (mesmo dia da semana)
    if conn is not None:
        from modules import database

        latest = database.get_latest_weekly_market_prep(conn, pair)
        if latest:
            created = latest.get("created_at") or ""
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if created_dt.date() == now_utc.date():
                    return False
            except (ValueError, TypeError):
                pass

    return True


# ---------------------------------------------------------------------------
# Construção do prompt para a IA
# ---------------------------------------------------------------------------

def _build_weekly_prep_message(pair, news, events, config):
    """Monta o user message para a IA de preparação semanal."""
    lookback = config.get("lookback_hours", 72)
    cal_days = config.get("calendar_days", 7)
    now_iso = datetime.now(timezone.utc).isoformat()

    news_lines = []
    for item in (news or [])[:20]:
        title = item.get("title", "")
        source = item.get("source", "")
        published = item.get("published", "") or item.get("published_at", "")
        news_lines.append(f"  - [{source}] {title} ({published})")

    event_lines = []
    for event in (events or [])[:20]:
        title = event.get("event", "") or event.get("title", "")
        country = event.get("currency", "") or event.get("country", "")
        impact = event.get("impact", "")
        event_time = event.get("time", "") or event.get("event_time", "")
        event_lines.append(f"  - [{country} | {impact}] {title} ({event_time})")

    sections = [
        f"Par: {pair}",
        f"Data/hora UTC: {now_iso}",
        "",
        f"NOTÍCIAS DO FIM-DE-SEMANA (últimas {lookback}h):",
        "\n".join(news_lines) if news_lines else "  (sem notícias recolhidas)",
        "",
        f"CALENDÁRIO ECONÓMICO (próximos {cal_days} dias):",
        "\n".join(event_lines) if event_lines else "  (sem eventos registados)",
        "",
        "Analisa o contexto macro/fundamental e devolve o JSON de preparação semanal.",
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Chamadas à IA
# ---------------------------------------------------------------------------

def _strip_json_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _as_str_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _validate_prep_result(result, pair):
    required = (
        "macro_bias", "preferred_direction", "confidence", "risk_level",
        "summary", "key_weekend_news", "key_events_next_week",
        "market_opening_risks", "recommendation", "reasoning_summary", "warnings",
    )
    for field in required:
        if field not in result:
            raise ValueError(f"campo '{field}' em falta na resposta da IA")

    result["pair"] = pair

    macro_bias = str(result.get("macro_bias") or "neutral").lower()
    if macro_bias not in {"bullish_eur", "bearish_eur", "neutral", "mixed"}:
        macro_bias = "neutral"
    result["macro_bias"] = macro_bias

    direction = str(result.get("preferred_direction") or "NEUTRAL").upper()
    if direction not in {"BUY", "SELL", "NEUTRAL"}:
        direction = "NEUTRAL"
    result["preferred_direction"] = direction

    try:
        conf = int(round(float(result.get("confidence") or 0)))
        result["confidence"] = max(0, min(100, conf))
    except (TypeError, ValueError):
        result["confidence"] = 0

    risk = str(result.get("risk_level") or "medium").lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    result["risk_level"] = risk

    rec = str(result.get("recommendation") or "trade_normally").lower()
    if rec not in {"trade_normally", "reduce_risk", "avoid_first_hour", "avoid_until_events_pass"}:
        rec = "trade_normally"
    result["recommendation"] = rec

    for list_field in ("key_weekend_news", "key_events_next_week", "market_opening_risks", "warnings"):
        result[list_field] = _as_str_list(result.get(list_field))

    result["summary"] = str(result.get("summary") or "").strip()
    result["reasoning_summary"] = str(result.get("reasoning_summary") or "").strip()

    return result


def _call_groq(user_message):
    from openai import OpenAI

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key == "PLACEHOLDER":
        raise RuntimeError("GROQ_API_KEY não configurada")
    client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": WEEKLY_PREP_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
    )
    return response.choices[0].message.content


def _call_claude(user_message):
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("sk-ant-PLACEHOLDER"):
        raise RuntimeError("ANTHROPIC_API_KEY não configurada")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=WEEKLY_PREP_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    return text


def _fallback_prep(pair, reason):
    return {
        "pair": pair,
        "macro_bias": "neutral",
        "preferred_direction": "NEUTRAL",
        "confidence": 0,
        "risk_level": "high",
        "summary": f"Preparação semanal indisponível: {reason}",
        "key_weekend_news": [],
        "key_events_next_week": [],
        "market_opening_risks": [f"IA indisponível: {reason}"],
        "recommendation": "reduce_risk",
        "reasoning_summary": f"Falha na análise semanal: {reason}",
        "warnings": [f"weekly_prep_falhou: {reason}"],
        "status": "failed",
        "error": reason,
    }


def run_weekly_ai_analysis(pair, news, events, provider=None):
    """Chama a IA para análise de preparação semanal. Devolve dict normalizado."""
    config = weekly_prep_config()
    provider = (
        provider or os.getenv("AI_PROVIDER") or "groq"
    ).strip().lower()

    if provider not in {"groq", "claude"}:
        return _fallback_prep(pair, f"provider '{provider}' inválido")

    user_message = _build_weekly_prep_message(pair, news, events, config)
    max_retries = int(float(os.getenv("AI_MAX_RETRIES") or 3))
    backoff_seconds = float(os.getenv("AI_RETRY_BACKOFF_SECONDS") or 5)
    attempts = max(1, max_retries)
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            raw = _call_groq(user_message) if provider == "groq" else _call_claude(user_message)
            result = json.loads(_strip_json_fences(raw))
            _validate_prep_result(result, pair)
            result["provider"] = provider
            result["model_version"] = model_version_for_provider(provider)
            result["status"] = "ok"
            return result
        except json.JSONDecodeError as e:
            return _fallback_prep(pair, f"resposta não é JSON válido ({e})")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)

    return _fallback_prep(pair, last_error or "falha desconhecida")


# ---------------------------------------------------------------------------
# Orquestração principal (chamada pelo ciclo weekend)
# ---------------------------------------------------------------------------

def run_weekly_prep(conn, news, events, pair="EUR/USD", provider=None):
    """Executa a preparação semanal: chama IA (se activada) e grava no SQLite.

    Não abre trades. Não altera sinais. Devolve o dict gravado.
    """
    from modules import database

    config = weekly_prep_config()

    if not config["use_ai"]:
        result = {
            "pair": pair,
            "macro_bias": "neutral",
            "preferred_direction": "NEUTRAL",
            "confidence": 0,
            "risk_level": "medium",
            "summary": "IA de preparação semanal desactivada (WEEKLY_MARKET_PREP_USE_AI=False).",
            "key_weekend_news": [item.get("title", "") for item in (news or [])[:5]],
            "key_events_next_week": [e.get("event", "") for e in (events or [])[:5]],
            "market_opening_risks": [],
            "recommendation": "trade_normally",
            "reasoning_summary": "Análise IA desactivada — sem contexto gerado.",
            "warnings": ["IA desactivada"],
            "status": "ai_disabled",
        }
    else:
        result = run_weekly_ai_analysis(pair, news, events, provider=provider)

    # Calcular week_start: segunda-feira da semana que começa
    now = datetime.now(timezone.utc)
    days_to_monday = (7 - now.weekday()) % 7 or 7
    week_start = (now + timedelta(days=days_to_monday)).date().isoformat()
    result["week_start"] = week_start

    database.save_weekly_market_prep(conn, result)
    return result
