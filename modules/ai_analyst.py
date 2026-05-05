import json
import os

from dotenv import load_dotenv

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """És um analista financeiro especializado em forex (mercado cambial).

Recebes notícias e eventos económicos relevantes para um par de moedas. A tua tarefa é avaliar o sentimento e a direção provável do par, e devolver uma decisão estruturada.

REGRAS OBRIGATÓRIAS:
1. Respondes SEMPRE com um único objeto JSON válido, sem texto antes ou depois, sem markdown, sem ```json.
2. O JSON deve conter EXACTAMENTE estes campos:
   - "signal": uma de "BUY", "SELL", "NEUTRAL"
   - "confidence": número inteiro de 0 a 100
   - "reasoning": string em português explicando a tua análise (2-4 frases)
   - "risk_level": uma de "LOW", "MEDIUM", "HIGH"
   - "hold_off": booleano (true se for prudente não abrir posição agora)
3. Se houver eventos de alto impacto iminentes ou notícias contraditórias fortes, define "hold_off": true.
4. "confidence" deve refletir a tua certeza real — não inflaciones."""


def _format_news(news):
    if not news:
        return "(sem notícias relevantes)"
    lines = []
    for i, art in enumerate(news[:15], 1):
        title = art.get("title", "").strip()
        source = art.get("source", "")
        lines.append(f"{i}. [{source}] {title}")
    return "\n".join(lines)


def _format_events(events):
    if not events:
        return "(sem eventos de alto impacto)"
    lines = []
    for ev in events[:20]:
        time = ev.get("time", "")
        currency = ev.get("currency", "")
        event = ev.get("event", "")
        extras = []
        for key in ("previous", "forecast", "actual"):
            val = ev.get(key)
            if val:
                extras.append(f"{key}: {val}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"- {time} {currency} {event}{suffix}".strip())
    return "\n".join(lines)


def _build_user_message(news, events, pair):
    return f"""Par: {pair}

NOTÍCIAS RELEVANTES:
{_format_news(news)}

EVENTOS ECONÓMICOS DE ALTO IMPACTO:
{_format_events(events)}

Analisa e devolve o JSON com a tua decisão."""


def build_analysis_input(news, events, pair="EUR/USD"):
    return _build_user_message(news, events, pair)


def _fallback(provider, error_msg):
    return {
        "signal": "NEUTRAL",
        "confidence": 0,
        "reasoning": f"Erro na análise: {error_msg}",
        "risk_level": "HIGH",
        "hold_off": True,
        "provider": provider,
    }


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _validate(result):
    required = ("signal", "confidence", "reasoning", "risk_level", "hold_off")
    for field in required:
        if field not in result:
            raise ValueError(f"campo '{field}' em falta na resposta")
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


def analyse(news, events, pair="EUR/USD"):
    provider = (os.getenv("AI_PROVIDER") or "groq").strip().lower()
    user_message = _build_user_message(news, events, pair)

    try:
        if provider == "groq":
            raw = _analyse_groq(user_message)
        elif provider == "claude":
            raw = _analyse_claude(user_message)
        else:
            return _fallback(
                provider,
                f"AI_PROVIDER='{provider}' inválido (usa 'groq' ou 'claude')",
            )

        result = json.loads(_strip_json_fences(raw))
        _validate(result)
        result["provider"] = provider
        return result

    except json.JSONDecodeError as e:
        return _fallback(provider, f"resposta não é JSON válido ({e})")
    except Exception as e:
        return _fallback(provider, f"{type(e).__name__}: {e}")


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
