
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from a .env file if present

# --- LLM API Keys ---
# Fetch API keys from environment variables.
# The application will gracefully handle missing keys by disabling the respective provider.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- LLM Model Defaults ---
# Specifies the default models to use for each provider.
# Make sure the selected models support the required capabilities (e.g., vision).
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o") # Vision-capable
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-70b-8192") # No Vision
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929") # Vision-capable
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp") # Vision-capable

# --- CAPTCHA Service API Keys ---
# Fetch CAPTCHA solving service API keys from environment variables.
# Multi-tier fallback system for maximum reliability.
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY")  # Tier 1
TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")  # Tier 2
ANTICAPTCHA_API_KEY = os.getenv("ANTICAPTCHA_API_KEY")  # Tier 3
DBC_USERNAME = os.getenv("DBC_USERNAME")  # Tier 4
DBC_PASSWORD = os.getenv("DBC_PASSWORD")  # Tier 4

# --- Global Directories ---
# Ensures a consistent directory structure for generated artifacts.
PROJECT_ROOT = Path(__file__).parent
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True) # Create the directory if it doesn't exist

# --- LLM Client Initialization ---
# Initialize API clients only if the corresponding API key is available.
anthropic_client = None
if ANTHROPIC_API_KEY:
    from anthropic import Anthropic
    try:
        anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
        print("[OK] Anthropic client initialized.")
    except Exception as e:
        print(f"[WARNING] Anthropic client failed to initialize: {e}")

groq_client = None
if GROQ_API_KEY:
    from groq import Groq
    try:
        groq_client = Groq(api_key=GROQ_API_KEY)
        print("[OK] Groq client initialized.")
    except Exception as e:
        print(f"[WARNING] Groq client failed to initialize: {e}")

openai_client = None
if OPENAI_API_KEY:
    from openai import OpenAI
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("[OK] OpenAI client initialized.")
    except Exception as e:
        print(f"[WARNING] OpenAI client failed to initialize: {e}")

gemini_client = None
if GEMINI_API_KEY:
    import google.generativeai as genai
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_client = genai.GenerativeModel(GEMINI_MODEL)
        print("[OK] Gemini client initialized.")
    except Exception as e:
        print(f"[WARNING] Gemini client failed to initialize: {e}")

# --- CAPTCHA Keys Status ---
# Display the status of CAPTCHA service API keys
if CAPSOLVER_API_KEY:
    print("[OK] CapSolver API key loaded.")
else:
    print("[WARNING] CapSolver API key not found in environment.")

if TWOCAPTCHA_API_KEY:
    print("[OK] 2Captcha API key loaded.")
else:
    print("[WARNING] 2Captcha API key not found in environment.")

if ANTICAPTCHA_API_KEY:
    print("[OK] AntiCaptcha API key loaded.")
else:
    print("[WARNING] AntiCaptcha API key not found in environment.")

if DBC_USERNAME and DBC_PASSWORD:
    print("[OK] DeathByCaptcha credentials loaded.")
else:
    print("[WARNING] DeathByCaptcha credentials not found in environment.")