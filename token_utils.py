import os
import time
import json
import sqlite3
import traceback
from datetime import datetime

# Load environment variables from .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optional dependencies
try:
    from google import genai
    from google.genai import types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "sim_state.db")

# FRUGAL FALLBACKS (STABLE FLASH MODELS)
VERTEX_FLASH_VARIANTS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-001",
    "gemini-1.5-flash-002",
    "gemini-1.5-flash-001",
    "gemini-1.5-flash"
]

def get_db_conn(timeout=120):
    """
    Returns a thread-safe SQLite connection with WAL mode enabled.
    This facilitates high-concurrency writes across parallel agent threads.
    """
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    # Enable Write-Ahead Logging for better concurrency
    conn.execute("PRAGMA journal_mode=WAL")
    # Improve write performance while remaining safe
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def load_model_config():
    config_path = os.path.join(PROJECT_DIR, "model_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return {"default_model": "gemini-2.5-flash", "pricing": {}, "eur_per_usd": 1.0}

MODEL_CONFIG = load_model_config()

def get_pricing(model_name: str):
    pricing = MODEL_CONFIG.get("pricing", {}).get(model_name, {"input_per_m_usd": 0, "output_per_m_usd": 0})
    eur_rate = MODEL_CONFIG.get("eur_per_usd", 1.0)
    return pricing, eur_rate

def log_tokens(tick: int, agent_name: str, model_name: str, in_tok: int, out_tok: int):
    pricing, eur_rate = get_pricing(model_name)
    cost_usd = (in_tok / 1_000_000 * pricing["input_per_m_usd"]) + (out_tok / 1_000_000 * pricing["output_per_m_usd"])
    cost_eur = cost_usd * eur_rate

    conn = get_db_conn() # Use the safe connection factory
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO token_log (tick, agent_name, model_name, input_tokens, output_tokens, cost_eur)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (tick, agent_name, model_name, in_tok, out_tok, cost_eur))
    conn.commit()
    conn.close()

def call_llm(prompt: str, agent_name: str, tick: int = 0, max_tokens: int = 1024, thinking_budget: int = 0):
    """Indestructible LLM Caller with Frugal-Only Fallbacks."""
    model_name = MODEL_CONFIG.get("agents", {}).get(agent_name, {}).get("model") or MODEL_CONFIG.get("default_model")
    
    try:
        if "gemini" in model_name.lower():
            text, in_tok, out_tok, reasoning = _call_gemini(prompt, model_name, max_tokens, thinking_budget)
        elif "claude" in model_name.lower():
            text, in_tok, out_tok, reasoning = _call_claude(prompt, model_name, max_tokens)
        else:
            raise ValueError(f"Unknown model provider in: {model_name}")

        log_tokens(tick, agent_name, model_name, in_tok, out_tok)
        return text, in_tok, out_tok, model_name, reasoning

    except Exception as e:
        print(f"  [CRITICAL] LLM Call failed for {agent_name}: {str(e)[:250]}")
        # Final emergency fallback: pause to allow internet/quota to recover
        time.sleep(2)
        return "ERROR: LLM Connectivity Issue", 0, 0, model_name, f"Emergency recovery from: {str(e)[:100]}"

def _call_gemini(prompt: str, model: str, max_tokens: int, thinking_budget: int):
    """Call Google Gemini API with Frugal Fallback Chain (2.5-First)."""
    if not _GEMINI_AVAILABLE:
        raise ImportError("google-genai package not installed.")

    # Get credentials (handle empty/missing strings)
    project_id = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1").strip()
    api_key = (os.environ.get("GOOGLE_API_KEY") or "").strip()

    def get_client(use_p_id: str, use_a_key: str):
        if use_p_id:
            return genai.Client(vertexai=True, project=use_p_id, location=location)
        if use_a_key:
            return genai.Client(api_key=use_a_key)
        raise ValueError("No credentials found. Ensure GOOGLE_CLOUD_PROJECT or GOOGLE_API_KEY is in your .env file.")

    is_vertex = bool(project_id)
    try:
        client = get_client(project_id, api_key)
    except Exception as e:
        if api_key:
            client = get_client("", api_key)
            is_vertex = False
        else:
            raise e

    # Start the fallback sequence
    trial_models = [model]
    if is_vertex:
        for v in VERTEX_FLASH_VARIANTS:
            if v not in trial_models:
                trial_models.append(v)
    
    for current_model in trial_models:
        mapped_model = current_model
        if is_vertex:
            if mapped_model == "gemini-2.0-flash": mapped_model = "gemini-2.0-flash-001"
            if mapped_model == "gemini-1.5-flash": mapped_model = "gemini-1.5-flash-002"

        config = {"max_output_tokens": max_tokens}
        if ("2.0" in mapped_model or "2.5" in mapped_model) and thinking_budget > 0:
            config["thinking_config"] = {"thinking_budget": thinking_budget}

        for attempt in range(2): # Short internal retries
            try:
                response = client.models.generate_content(
                    model=mapped_model,
                    contents=prompt,
                    config=config
                )
                
                text = response.text or ""
                reasoning = ""
                if response.candidates and response.candidates[0].content:
                    for part in response.candidates[0].content.parts:
                        if hasattr(part, 'thought') and part.thought:
                            reasoning = part.thought
                
                in_tok = response.usage_metadata.prompt_token_count or 0
                out_tok = response.usage_metadata.candidates_token_count or 0
                return text, in_tok, out_tok, reasoning

            except Exception as e:
                err_msg = str(e)
                if "404" in err_msg or "NOT_FOUND" in err_msg:
                    print(f"  [LLM] Vertex 404 for {mapped_model}. Hunting next Flash...")
                    break
                
                if is_vertex and ("PERMISSION_DENIED" in err_msg or "SERVICE_DISABLED" in err_msg) and api_key:
                    print(f"  [LLM] Vertex restricted. Falling back to Developer API Key...")
                    client = get_client("", api_key)
                    is_vertex = False
                    break 

                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    time.sleep(5)
                    continue
                
                if attempt == 1:
                    if current_model == trial_models[-1]:
                        raise e
                    else:
                        break 
    
    raise RuntimeError("Exhausted all Gemini Flash fallback options.")

def extract_json(text: str) -> dict:
    """Utility to pull JSON from markdown backticks or raw text."""
    if "```json" in text:
        content = text.split("```json")[1].split("```")[0].strip()
        try: return json.loads(content)
        except: pass
    elif "```" in text:
        content = text.split("```")[1].split("```")[0].strip()
        try: return json.loads(content)
        except: pass
    try:
        return json.loads(text.strip())
    except:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try: return json.loads(text[start:end+1])
            except: pass
    return {}

def _call_claude(prompt: str, model: str, max_tokens: int):
    """Call Anthropic Claude API."""
    if not _ANTHROPIC_AVAILABLE:
        raise ImportError("anthropic package not installed.")
    
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is missing.")
    
    client = anthropic.Anthropic(api_key=api_key)
    
    for attempt in range(2):
        try:
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            text = message.content[0].text
            in_tok = message.usage.input_tokens
            out_tok = message.usage.output_tokens
            return text, in_tok, out_tok, ""
        except Exception as e:
            if "429" in str(e) and attempt < 1:
                time.sleep(5)
                continue
            raise e
