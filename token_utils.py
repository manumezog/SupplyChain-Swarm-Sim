"""
Shared Claude API client and token utilities.
Agents use Haiku by default; forecast uses Opus for accuracy.

API key: set ANTHROPIC_API_KEY environment variable, or create a .env file
in the project directory with:  ANTHROPIC_API_KEY=sk-ant-...
"""
import json
import os
import re

# Load .env file from the same directory as this module (project root)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

try:
    import anthropic as _anthropic
    _CLAUDE_AVAILABLE = True
except ImportError:
    _CLAUDE_AVAILABLE = False

# Approximate blended pricing (Haiku dominant, Opus for forecast)
USD_PER_M_INPUT  = 1.0
USD_PER_M_OUTPUT = 5.0
EUR_PER_USD      = 0.92

MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_OPUS  = "claude-opus-4-6"


def calc_cost_eur(input_tokens: int, output_tokens: int) -> float:
    cost_usd = (
        (input_tokens  / 1_000_000) * USD_PER_M_INPUT +
        (output_tokens / 1_000_000) * USD_PER_M_OUTPUT
    )
    return round(cost_usd * EUR_PER_USD, 6)


def call_claude(prompt: str, max_tokens: int = 512, model: str = MODEL_HAIKU) -> tuple[str, int, int]:
    """Call Claude. Returns (text_response, input_tokens, output_tokens)."""
    if not _CLAUDE_AVAILABLE:
        raise ImportError("anthropic package not installed. Run: pip install anthropic")
    client = _anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = ""
    for block in msg.content:
        if block.type == "text":
            text = block.text
            break
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def extract_json(text: str) -> dict:
    """Extract the first JSON object from Claude's response."""
    cleaned = re.sub(r'```(?:json)?\s*', '', text).strip('`\n ')
    start = cleaned.find('{')
    if start == -1:
        raise ValueError(f"No JSON object in: {cleaned[:300]}")
    depth, end = 0, start
    for i, c in enumerate(cleaned[start:], start):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    return json.loads(cleaned[start:end + 1])


def log_tokens(cur, tick: int, agent_name: str, input_tokens: int, output_tokens: int) -> None:
    """Insert actual token counts from API response into token_log."""
    cost = calc_cost_eur(input_tokens, output_tokens)
    cur.execute(
        "INSERT INTO token_log (tick, agent_name, input_tokens, output_tokens, cost_eur) "
        "VALUES (?, ?, ?, ?, ?)",
        (tick, agent_name, input_tokens, output_tokens, cost),
    )
