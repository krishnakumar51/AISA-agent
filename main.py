import asyncio
import platform
import re
import uuid
import json
import time
import csv
from pathlib import Path
from urllib.parse import urljoin
import traceback
from typing import List, TypedDict, Dict, Any
import aiohttp
import logger
import subprocess
from core import force_stop_chrome, forward_port, get_devtools_port, start_chrome_incognito, start_chrome_normal, wait_for_devtools
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page, Browser
from PIL import Image
from langgraph.graph import StateGraph, END
from bs4 import BeautifulSoup

from llm import LLMProvider, get_refined_prompt, get_agent_action
from config import SCREENSHOTS_DIR, ANTHROPIC_MODEL, GROQ_MODEL, OPENAI_MODEL
# CAPTCHA Integration - Universal CAPTCHA solver for all types
from captcha import UniversalCaptchaSolver

# --- FastAPI App Initialization ---
app = FastAPI(title="LangGraph Web Agent with Memory")

# --- CAPTCHA Solver Integration ---
# Global CAPTCHA solver instance - handles all CAPTCHA types universally
# Supports: Cloudflare Turnstile, reCAPTCHA v2/v3, hCAPTCHA, Image CAPTCHAs
captcha_solver = UniversalCaptchaSolver()

# --- NEW: Helper functions for enhanced HITL ---

def detect_login_failure(page_content: str, page_url: str) -> bool:
    """Detect if a login attempt has failed based on page content and URL."""
    failure_indicators = [
        "invalid credentials", "login failed", "incorrect password", 
        "incorrect username", "authentication failed", "login error",
        "wrong password", "invalid login", "access denied", "login unsuccessful",
        "incorrect email", "invalid email", "user not found", "account not found",
        "too many attempts", "account locked", "temporarily locked"
    ]
    
    url_indicators = [
        "/login", "/signin", "/auth", "/error", "/failure"
    ]
    
    content_lower = page_content.lower()
    url_lower = page_url.lower()
    
    # Check for failure text in content
    content_has_failure = any(indicator in content_lower for indicator in failure_indicators)
    
    # Check if still on login/auth page (might indicate failure)
    still_on_auth_page = any(indicator in url_lower for indicator in url_indicators)
    
    return content_has_failure or still_on_auth_page


# --- In-Memory Job Storage ---
JOB_QUEUES = {}
JOB_RESULTS = {}
# NEW: Human-in-the-loop storage
USER_INPUT_REQUESTS = {}  # job_id -> UserInputRequest
USER_INPUT_RESPONSES = {}  # job_id -> user_provided_value
PENDING_JOBS = {}  # job_id -> asyncio.Event for resuming
# NEW: Track jobs that are in user input flow to prevent interference
JOBS_IN_INPUT_FLOW = set()  # job_ids currently in user input flow

# --- NEW: Token Cost Analysis Configuration ---
ANALYSIS_DIR = Path("analysis")
REPORT_CSV_FILE = Path("report.csv")


# Prices per 1 Million tokens
TOKEN_COSTS = {
    "anthropic": {
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "claude-3-5-haiku-20241022": {"input": 0.8, "output": 4.0},
        "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
        "claude-3-5-sonnet-20240620": {"input": 3.0, "output": 15.0}
    },
    "openai": {
        "gpt-4o": {"input": 5.0, "output": 15.0}
    },
    "groq": {
        "llama3-8b-8192": {"input": 0.05, "output": 0.10}
    }
}

MODEL_MAPPING = {
    LLMProvider.ANTHROPIC: ANTHROPIC_MODEL,
    LLMProvider.GROQ: GROQ_MODEL,
    LLMProvider.OPENAI: OPENAI_MODEL
}

# --- Helper Functions ---
def get_current_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def push_status(job_id: str, msg: str, details: dict = None):
    q = JOB_QUEUES.get(job_id)
    if q:
        entry = {"ts": get_current_timestamp(), "msg": msg}
        if details: entry["details"] = details
        q.put_nowait(entry)

# NEW: Helper function for cleaning up stuck human-in-the-loop jobs
def cleanup_stuck_jobs():
    """Clean up jobs that might be stuck waiting for user input"""
    current_time = time.time()
    stuck_jobs = []
    
    for job_id, request in list(USER_INPUT_REQUESTS.items()):
        # Check if request is older than 10 minutes (600 seconds)
        request_time = request.get('timestamp', '')
        if request_time:
            try:
                request_timestamp = time.mktime(time.strptime(request_time, "%Y-%m-%dT%H:%M:%SZ"))
                if current_time - request_timestamp > 600:  # 10 minutes
                    stuck_jobs.append(job_id)
            except:
                stuck_jobs.append(job_id)
    
    for job_id in stuck_jobs:
        print(f"Cleaning up stuck job: {job_id}")
        USER_INPUT_REQUESTS.pop(job_id, None)
        USER_INPUT_RESPONSES.pop(job_id, None)
        JOBS_IN_INPUT_FLOW.discard(job_id)  # Remove from global protection
        if job_id in PENDING_JOBS:
            PENDING_JOBS[job_id].set()  # Release the waiting job
            PENDING_JOBS.pop(job_id, None)
    
    return len(stuck_jobs)

def resize_image_if_needed(image_path: Path, max_dimension: int = 2000):
    try:
        with Image.open(image_path) as img:
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
                img.save(image_path)
    except Exception as e:
        print(f"Warning: Could not resize image {image_path}. Error: {e}")

def find_elements_with_attribute_text_detailed(html: str, text: str) -> List[Dict[str, Any]]:
    
    if not html or not text:
        return []
        
    soup = BeautifulSoup(html, 'html.parser')
    matching_elements = []
    text_lower = text.lower()

    for element in soup.find_all(True):
        if not hasattr(element, 'attrs') or not element.attrs:
            continue
            
        matched_attributes = []
        
        for attr_name, attr_value in element.attrs.items():
            try:
                if attr_value is None:
                    continue
                    
                # Convert list attributes to string
                if isinstance(attr_value, list):
                    attr_value_str = ' '.join(str(v) for v in attr_value)
                else:
                    attr_value_str = str(attr_value)
                
                # Check for matches
                name_match = text_lower in attr_name.lower()
                value_match = text_lower in attr_value_str.lower()
                
                if name_match or value_match:
                    matched_attributes.append({
                        'name': attr_name,
                        'value': attr_value_str,
                        'name_match': name_match,
                        'value_match': value_match
                    })
                    
            except (AttributeError, TypeError):
                continue
        
        if matched_attributes:
            # Generate useful selectors
            selectors = []
            
            # ID selector
            if element.get('id'):
                selectors.append(f"#{element['id']}")
            
            # Class selector
            if element.get('class'):
                classes = element['class'] if isinstance(element['class'], list) else [element['class']]
                # Convert all class values to strings
                class_strings = [str(cls) for cls in classes]
                selectors.append(f".{'.'.join(class_strings)}")
            
            # Tag selector
            selectors.append(element.name)
            
            # Attribute selectors for matched attributes
            for attr in matched_attributes:
                if attr['name'] in ['id', 'class']:
                    continue  # Already handled above
                selectors.append(f"{element.name}[{attr['name']}*='{attr['value'][:20]}']")
            
            matching_elements.append({
                'element_html': str(element),
                'tag_name': element.name,
                'matched_attributes': matched_attributes,
                'suggested_selectors': selectors[:3],  # Top 3 most useful selectors
                'all_attributes': dict(element.attrs) if element.attrs else {}
            })

    return matching_elements

async def find_elements_with_text_live(page, text: str) -> List[Dict[str, Any]]:
    """
    Finds all elements on the LIVE page where any attribute name, value, or text content contains the given text.
    This function works with dynamically rendered elements and conditional content.
    
    Parameters:
        page: Playwright page object
        text (str): The text to search for (case-insensitive).

    Returns:
        List[Dict]: A list of dictionaries containing element info, selectors, and interaction capabilities.
    """
    if not text:
        return []
    
    # Escape the search text for JavaScript
    escaped_text = text.replace('"', '\\"')
    
    # JavaScript function to search for elements comprehensively with fuzzy matching
    js_search_script = f"""
    (function() {{
        const searchText = "{escaped_text}".toLowerCase();
        const results = [];
        
        function normalizeText(text) {{
            if (!text) return '';
            return text.toLowerCase()
                .replace(/[\\s_-]+/g, '')
                .replace(/[^a-z0-9]/g, '');
        }}
        
        function calculateMatchScore(searchNorm, targetNorm, originalTarget) {{
            let score = 0;
            
            if (targetNorm === searchNorm) {{
                score = 100;
            }} else if (targetNorm.startsWith(searchNorm)) {{
                score = 80;
            }} else if (targetNorm.includes(searchNorm)) {{
                score = 60;
            }} else if (targetNorm.endsWith(searchNorm)) {{
                score = 40;
            }} else {{
                return 0;
            }}
            
            if (targetNorm.length === searchNorm.length) {{
                score += 20;
            }}
            
            if (originalTarget.includes(' ') && searchText.includes(' ')) {{
                score += 10;
            }}
            
            return Math.min(score, 100);
        }}
        
        function generateSelector(element) {{
            const selectors = [];
            
            // ID selector (highest priority)
            if (element.id) {{
                selectors.push('#' + element.id);
            }}
            
            // Class selector
            if (element.className && typeof element.className === 'string') {{
                const classes = element.className.trim().split(/\\s+/).filter(c => c.length > 0);
                if (classes.length > 0) {{
                    selectors.push('.' + classes.join('.'));
                }}
            }}
            
            // Data attributes
            for (let attr of element.attributes) {{
                if (attr.name.startsWith('data-') && attr.value) {{
                    selectors.push(`[${{attr.name}}="${{attr.value}}"]`);
                }}
            }}
            
            // Specific attribute selectors
            ['name', 'type', 'role', 'aria-label'].forEach(attrName => {{
                const value = element.getAttribute(attrName);
                if (value) {{
                    selectors.push(`[${{attrName}}="${{value}}"]`);
                }}
            }});
            
            // Text-based selector (for unique text)
            const textContent = element.textContent?.trim();
            if (textContent && textContent.length > 0 && textContent.length < 50) {{
                selectors.push(`text="${{textContent}}"`);
                selectors.push(`:has-text("${{textContent}}")`);
            }}
            
            // Tag-based selector (lowest priority)
            selectors.push(element.tagName.toLowerCase());
            
            return selectors;
        }}
        
        function checkElement(element) {{
            const matches = [];
            const searchNormalized = normalizeText(searchText);
            
            // Check all attributes with fuzzy matching
            for (let attr of element.attributes) {{
                const attrNameNorm = normalizeText(attr.name);
                const attrValueNorm = normalizeText(attr.value);
                
                const nameScore = calculateMatchScore(searchNormalized, attrNameNorm, attr.name);
                const valueScore = calculateMatchScore(searchNormalized, attrValueNorm, attr.value);
                
                if (nameScore > 0 || valueScore > 0) {{
                    matches.push({{
                        type: 'attribute',
                        name: attr.name,
                        value: attr.value,
                        nameMatch: nameScore > 0,
                        valueMatch: valueScore > 0,
                        nameScore: nameScore,
                        valueScore: valueScore,
                        maxScore: Math.max(nameScore, valueScore)
                    }});
                }}
            }}
            
            // Check text content with fuzzy matching
            const textContent = element.textContent?.trim() || '';
            const innerText = element.innerText?.trim() || '';
            
            const textContentNorm = normalizeText(textContent);
            const textContentScore = calculateMatchScore(searchNormalized, textContentNorm, textContent);
            
            if (textContentScore > 0) {{
                matches.push({{
                    type: 'textContent',
                    value: textContent,
                    match: true,
                    score: textContentScore
                }});
            }}
            
            if (innerText !== textContent) {{
                const innerTextNorm = normalizeText(innerText);
                const innerTextScore = calculateMatchScore(searchNormalized, innerTextNorm, innerText);
                
                if (innerTextScore > 0) {{
                    matches.push({{
                        type: 'innerText', 
                        value: innerText,
                        match: true,
                        score: innerTextScore
                    }});
                }}
            }}
            
            // Check placeholder, value, and other common text properties with fuzzy matching
            ['placeholder', 'value', 'title', 'alt', 'aria-label'].forEach(prop => {{
                const value = element[prop] || element.getAttribute(prop);
                if (value) {{
                    const valueNorm = normalizeText(value);
                    const propScore = calculateMatchScore(searchNormalized, valueNorm, value);
                    
                    if (propScore > 0) {{
                        matches.push({{
                            type: 'property',
                            name: prop,
                            value: value,
                            match: true,
                            score: propScore
                        }});
                    }}
                }}
            }});
            
            return matches;
        }}
        
        // Get all elements in the document (including dynamically added ones)
        const allElements = document.querySelectorAll('*');
        
        allElements.forEach((element, index) => {{
            const matches = checkElement(element);
            
            if (matches.length > 0) {{
                const rect = element.getBoundingClientRect();
                const computedStyle = window.getComputedStyle(element);
                
                // Check visibility and interaction capabilities
                const isVisible = (
                    rect.width > 0 && 
                    rect.height > 0 && 
                    computedStyle.visibility !== 'hidden' && 
                    computedStyle.display !== 'none' &&
                    element.offsetParent !== null
                );
                
                const isInteractive = (
                    element.tagName.toLowerCase() in {{'button': 1, 'a': 1, 'input': 1, 'select': 1, 'textarea': 1}} ||
                    element.onclick !== null ||
                    element.getAttribute('onclick') ||
                    element.getAttribute('href') ||
                    computedStyle.cursor === 'pointer' ||
                    element.hasAttribute('tabindex')
                );
                
                const isClickable = (
                    isInteractive ||
                    element.addEventListener ||
                    computedStyle.pointerEvents !== 'none'
                );
                
                results.push({{
                    index: index,
                    tagName: element.tagName.toLowerCase(),
                    matches: matches,
                    selectors: generateSelector(element),
                    isVisible: isVisible,
                    isInteractive: isInteractive,
                    isClickable: isClickable,
                    position: {{
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height)
                    }},
                    styles: {{
                        display: computedStyle.display,
                        visibility: computedStyle.visibility,
                        cursor: computedStyle.cursor,
                        pointerEvents: computedStyle.pointerEvents
                    }},
                    textContent: element.textContent?.trim()?.substring(0, 100) || '',
                    innerHTML: element.innerHTML?.substring(0, 200) || '',
                    outerHTML: element.outerHTML?.substring(0, 300) || ''
                }});
            }}
        }});
        
        // Sort by relevance (visible and interactive elements first, then by match quality)
        results.sort((a, b) => {{
            const maxMatchScoreA = Math.max(...a.matches.map(m => m.score || 0), 0);
            const maxMatchScoreB = Math.max(...b.matches.map(m => m.score || 0), 0);
            
            const scoreA = (a.isVisible ? 10 : 0) + (a.isInteractive ? 5 : 0) + (a.isClickable ? 3 : 0) + (maxMatchScoreA / 10);
            const scoreB = (b.isVisible ? 10 : 0) + (b.isInteractive ? 5 : 0) + (b.isClickable ? 3 : 0) + (maxMatchScoreB / 10);
            return scoreB - scoreA;
        }});
        
        return results;
    }})();
    """
    
    try:
        # Execute the JavaScript and get results
        results = await page.evaluate(js_search_script)
        
        # Process and enhance results
        processed_results = []
        for result in results:
            # Calculate priority score with match scores
            priority_score = 0
            if result['isVisible']:
                priority_score += 10
            if result['isInteractive']:
                priority_score += 5
            if result['isClickable']:
                priority_score += 3
            
            # Add match quality score (scale down from 0-100 to 0-10 range)
            match_scores = [match.get('score', 0) for match in result['matches']]
            max_match_score = max(match_scores) if match_scores else 0
            priority_score += max_match_score / 10  # 100 -> 10, 80 -> 8, 60 -> 6
            
            # Determine interaction capabilities
            interaction_methods = []
            if result['isClickable']:
                interaction_methods.append('click')
            if result['tagName'] in ['input', 'textarea']:
                interaction_methods.append('fill')
                interaction_methods.append('press')
            if result['tagName'] == 'select':
                interaction_methods.append('selectOption')
            
            processed_result = {
                'element_index': result['index'],
                'tag_name': result['tagName'],
                'matches': result['matches'],
                'suggested_selectors': result['selectors'][:5],  # Top 5 selectors
                'is_visible': result['isVisible'],
                'is_interactive': result['isInteractive'],
                'is_clickable': result['isClickable'],
                'position': result['position'],
                'styles': result['styles'],
                'interaction_methods': interaction_methods,
                'text_content': result['textContent'],
                'inner_html': result['innerHTML'],
                'outer_html': result['outerHTML'],
                'priority_score': priority_score,
                'element_summary': f"{result['tagName']} ({'visible' if result['isVisible'] else 'hidden'}, {'interactive' if result['isInteractive'] else 'static'}) - {len(result['matches'])} matches",
                'all_attributes': {}  # Keep compatibility with existing code
            }
            processed_results.append(processed_result)
        
        return processed_results
        
    except Exception as e:
        print(f"Error in live element search: {e}")
        return []

# --- NEW: Cost Analysis Function ---
def save_analysis_report(analysis_data: dict):
    """Calculates final costs, saves a detailed JSON report, and appends to a summary CSV."""
    job_id = analysis_data["job_id"]
    provider = analysis_data["provider"]
    model = analysis_data["model"]
    
    total_input = 0
    total_output = 0
    
    for step in analysis_data["steps"]:
        total_input += step.get("input_tokens", 0)
        total_output += step.get("output_tokens", 0)

    analysis_data["total_input_tokens"] = total_input
    analysis_data["total_output_tokens"] = total_output

    cost_info = TOKEN_COSTS.get(provider, {}).get(model)
    # --- MODIFIED: Add a more robust fallback for different Anthropic model names ---
    if not cost_info and provider == "anthropic":
        model_name_lower = model.lower()
        if "sonnet" in model_name_lower:
            # Default to the latest Sonnet pricing if a specific version isn't matched
            cost_info = TOKEN_COSTS.get("anthropic", {}).get("claude-3.5-sonnet-20240620")
        elif "haiku" in model_name_lower:
            cost_info = TOKEN_COSTS.get("anthropic", {}).get("claude-3-haiku-20240307")


    total_cost = 0.0
    if cost_info:
        input_cost = (total_input / 1_000_000) * cost_info["input"]
        output_cost = (total_output / 1_000_000) * cost_info["output"]
        total_cost = input_cost + output_cost
    
    # Format the cost to a string with 5 decimal places to ensure precision in output files.
    total_cost_usd_str = f"{total_cost:.5f}"
    analysis_data["total_cost_usd"] = total_cost_usd_str

    # 1. Save detailed JSON report in analysis/ directory
    try:
        ANALYSIS_DIR.mkdir(exist_ok=True)
        json_report_path = ANALYSIS_DIR / f"{job_id}.json"
        with open(json_report_path, 'w') as f:
            json.dump(analysis_data, f, indent=2)
    except Exception as e:
        print(f"Error saving JSON analysis report for job {job_id}: {e}")

    # 2. Append summary to report.csv
    try:
        file_exists = REPORT_CSV_FILE.is_file()
        with open(REPORT_CSV_FILE, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            header = ['job_id', 'total_input_tokens', 'total_output_tokens', 'total_cost_usd']
            if not file_exists:
                writer.writerow(header)
            
            row = [job_id, total_input, total_output, total_cost_usd_str]
            writer.writerow(row)
    except Exception as e:
        print(f"Error updating CSV report: {e}")





async def install_popup_killer(page):
    """
    🛡️ PROACTIVE POPUP KILLER - MutationObserver-based instant removal
    Runs in browser context with zero Python overhead
    """
    popup_killer_script = """
    (function() {
        const DISMISS_TEXTS = [
            "accept", "accept all", "accept cookies", "agree", "agree and continue",
            "i accept", "i agree", "ok", "okay", "yes", "allow", "allow all",
            "got it", "understood", "sounds good", "close", "dismiss", "no thanks",
            "not now", "maybe later", "later", "skip", "skip for now", "remind me later",
            "not interested", "continue", "proceed", "next", "go ahead", "let's go",
            "decline", "reject", "refuse", "no", "cancel", "don't show again",
            "do not show", "only necessary", "necessary only", "essential only",
            "reject all", "decline all", "manage preferences", "continue without",
            "skip sign in", "skip login", "browse as guest", "continue as guest",
            "no account", "no thank you", "unsubscribe", "don't subscribe",
            "×", "✕", "✖", "⨯", "close dialog", "close modal", "close popup",
            "dismiss notification", "close banner", "close alert"
        ];
        
        const POPUP_SELECTORS = [
            "[role='dialog']", "[role='alertdialog']", ".modal", ".popup", 
            ".overlay", ".lightbox", ".dialog", "#cookie-banner", ".cookie-banner",
            "[class*='cookie']", "#cookieConsent", ".cookie-consent", "[id*='cookie']",
            ".overlay-wrapper", ".modal-backdrop", ".popup-overlay", "[class*='overlay']",
            "[class*='backdrop']", ".newsletter-popup", ".subscription-modal",
            "[class*='newsletter']", ".close-btn", ".close-button", "[aria-label*='close']",
            "[aria-label*='dismiss']", "button.close", ".modal-close"
        ];
        
        let processedElements = new WeakSet();
        let killCount = 0;
        
        function normalizeText(text) {
            return text.toLowerCase().trim().replace(/\\s+/g, ' ');
        }
        
        function tryKillPopup(element) {
            if (!element || processedElements.has(element)) return false;
            processedElements.add(element);
            
            const style = window.getComputedStyle(element);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            
            const clickables = element.querySelectorAll('button, a, [role="button"], [onclick]');
            for (const btn of clickables) {
                const text = normalizeText(btn.textContent || btn.innerText || '');
                const ariaLabel = normalizeText(btn.getAttribute('aria-label') || '');
                
                for (const dismissText of DISMISS_TEXTS) {
                    if (text.includes(dismissText) || ariaLabel.includes(dismissText)) {
                        try {
                            btn.click();
                            killCount++;
                            console.log(`🎯 Popup killed #${killCount}: clicked "${btn.textContent}" in`, element);
                            return true;
                        } catch (e) {
                            continue;
                        }
                    }
                }
            }
            
            if (element.tagName === 'BUTTON' || element.getAttribute('role') === 'button') {
                const text = normalizeText(element.textContent || '');
                for (const dismissText of DISMISS_TEXTS) {
                    if (text.includes(dismissText)) {
                        try {
                            element.click();
                            killCount++;
                            console.log(`🎯 Popup killed #${killCount}: direct click`, element);
                            return true;
                        } catch (e) {}
                    }
                }
            }
            
            return false;
        }
        
        function scanAndKill() {
            for (const selector of POPUP_SELECTORS) {
                try {
                    const popups = document.querySelectorAll(selector);
                    for (const popup of popups) {
                        if (tryKillPopup(popup)) return;
                    }
                } catch (e) {}
            }
        }
        
        scanAndKill();
        
        const observer = new MutationObserver((mutations) => {
            let hasRelevantMutation = false;
            
            for (const mutation of mutations) {
                if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                    for (const node of mutation.addedNodes) {
                        if (node.nodeType === 1) {
                            const tag = node.tagName?.toLowerCase();
                            const classes = node.className?.toLowerCase() || '';
                            const id = node.id?.toLowerCase() || '';
                            
                            if (tag === 'dialog' || 
                                classes.includes('modal') || 
                                classes.includes('popup') || 
                                classes.includes('overlay') ||
                                classes.includes('cookie') ||
                                id.includes('cookie') ||
                                id.includes('modal')) {
                                hasRelevantMutation = true;
                                if (tryKillPopup(node)) return;
                            }
                        }
                    }
                }
            }
            
            if (hasRelevantMutation) {
                setTimeout(scanAndKill, 50);
            }
        });
        
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: false,
            characterData: false
        });
        
        setInterval(scanAndKill, 2000);
        
        console.log('🛡️ Popup killer installed and monitoring...');
        
        window.__popupKillCount = () => killCount;
    })();
    """
    
    try:
        await page.evaluate(popup_killer_script)
        logger.info("🛡️ Proactive popup killer installed")
    except Exception as e:
        print(f"⚠️ Failed to install popup killer: {e}")



# --- API Models ---
class SearchRequest(BaseModel):
    url: str
    query: str
    top_k: int
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC

class UserInputRequest(BaseModel):
    job_id: str
    input_type: str  # "text", "password", "otp", "email", "phone"
    prompt: str
    is_sensitive: bool = False

class UserInputResponse(BaseModel):
    job_id: str
    input_value: str

# --- LangGraph Agent State with Memory ---
class AgentState(TypedDict):
    job_id: str
    browser: Browser
    page: Page
    query: str
    url: str  # Current page URL
    top_k: int
    provider: LLMProvider
    refined_query: str
    results: List[dict]
    screenshots: List[str]
    job_artifacts_dir: Path
    step: int
    max_steps: int
    last_action: dict
    history: List[str] 
    token_usage: List[dict] # To store token usage per step
    found_element_context: dict # To store context about found elements
    failed_actions: Dict[str, int] # signature -> failure count
    attempted_action_signatures: List[str] # chronological list of attempted signatures
    # Enhanced memory system
    recent_extracts: List[str] # recent extract texts to prevent repeated calls
    selector_attempts: Dict[str, List[str]] # track which selectors were tried for each text
    successful_selectors: Dict[str, str] # text -> working selector mapping
    action_verification: List[Dict[str, Any]] # verification results from screenshots
    screenshot_analysis: Dict[str, Any] # current screenshot analysis
    element_interaction_log: List[Dict[str, Any]] # detailed interaction history with outcomes
    # Search flow tracking
    search_flow_state: Dict[str, Any] # track search flow progress (detected, clicked, filled, submitted)
    # Human-in-the-loop state
    waiting_for_user_input: bool
    user_input_request: dict  # Stores the current input request
    user_input_response: str  # Stores the user's response
    user_input_flow_active: bool  # Tracks if we're in a user input flow to prevent interference
    # CAPTCHA handling state
    captcha_detected: Dict[str, Any]  # CAPTCHA detection results (type, sitekey, confidence)
    captcha_solved: bool              # Whether detected CAPTCHA was successfully solved
    captcha_attempts: List[Dict[str, Any]]  # History of CAPTCHA solving attempts
    captcha_service_used: str         # Which service successfully solved the CAPTCHA

# --- NEW: Enhanced memory context builder ---
def build_enhanced_memory_context(state: AgentState) -> str:
    """
    Build comprehensive memory context for the agent including action verification,
    selector tracking, and search flow state.
    """
    context = ""
    
    # Action verification summary
    action_verification = state.get('action_verification', [])
    if action_verification:
        recent_verifications = action_verification[-5:]  # Last 5 verifications
        context += f"\n\n📊 RECENT ACTION VERIFICATION RESULTS:"
        for i, verification in enumerate(recent_verifications):
            step = verification.get('step', 'N/A')
            success = "✅" if verification.get('success') or verification.get('changes_detected') else "❌"
            action_type = verification.get('action', {}).get('type', 'unknown')
            notes = "; ".join(verification.get('verification_notes', []))
            context += f"\n  {success} Step {step} ({action_type}): {notes}"
    
    # Successful selectors memory
    successful_selectors = state.get('successful_selectors', {})
    if successful_selectors:
        context += f"\n\n✅ VERIFIED WORKING SELECTORS (reusable):"
        for text, selector in list(successful_selectors.items())[-5:]:  # Last 5 successful
            context += f"\n  • Text '{text}' → {selector}"
        context += f"\n💡 TIP: You can reuse these selectors if you need to interact with the same elements"
    
    # Element interaction log summary
    element_log = state.get('element_interaction_log', [])
    if element_log:
        recent_interactions = element_log[-3:]  # Last 3 interactions
        context += f"\n\n🎯 RECENT ELEMENT INTERACTIONS:"
        for interaction in recent_interactions:
            step = interaction.get('step', 'N/A')
            text = interaction.get('search_text', 'N/A')
            tested = interaction.get('tested_selectors', 0)
            working = interaction.get('working_selectors', 0)
            context += f"\n  • Step {step}: '{text}' → {working}/{tested} selectors working"
    
    # Search flow tracking
    search_flow = state.get('search_flow_state', {})
    if search_flow:
        context += f"\n\n🔍 SEARCH FLOW PROGRESS:"
        for phase, status in search_flow.items():
            status_icon = "✅" if status else "⏳"
            context += f"\n  {status_icon} {phase.capitalize()}: {'Complete' if status else 'Pending'}"
        
        # Add search flow guidance
        if not search_flow.get('detected', False):
            context += f"\n💡 NEXT: Detect search input using extract_correct_selector_using_text"
        elif not search_flow.get('clicked', False):
            context += f"\n💡 NEXT: Click on the search input field to focus it"
        elif not search_flow.get('filled', False):
            context += f"\n💡 NEXT: Fill the search input with user's query"
        elif not search_flow.get('submitted', False):
            context += f"\n💡 NEXT: Press Enter to submit the search"
    
    # Failed actions analysis
    failed_actions = state.get('failed_actions', {})
    if failed_actions:
        total_failures = sum(failed_actions.values())
        context += f"\n\n🚨 FAILURE ANALYSIS ({total_failures} total failures):"
        
        # Categorize failures
        click_failures = [k for k in failed_actions.keys() if k.startswith('click|')]
        fill_failures = [k for k in failed_actions.keys() if k.startswith('fill|')]
        extract_failures = [k for k in failed_actions.keys() if 'extract_correct_selector' in k]
        
        if click_failures:
            context += f"\n  ❌ Click failures: {len(click_failures)}"
        if fill_failures:
            context += f"\n  ❌ Fill failures: {len(fill_failures)}"
        if extract_failures:
            context += f"\n  ❌ Extract failures: {len(extract_failures)}"
        
        # Pattern analysis
        if total_failures > 5:
            context += f"\n⚠️  HIGH FAILURE RATE: Consider completely different approach"
            context += f"\n💡 SUGGESTIONS: Try scrolling, different search terms, or navigation menus"
    
    # Selector attempt tracking
    selector_attempts = state.get('selector_attempts', {})
    if selector_attempts:
        context += f"\n\n📋 SELECTOR ATTEMPT TRACKING:"
        for text, selectors in list(selector_attempts.items())[-3:]:  # Last 3 texts
            context += f"\n  • '{text}': {len(selectors)} selectors tested"
        context += f"\n💡 TIP: Use different text if current selectors aren't working"
    
    # 🤖 CAPTCHA STATUS AND HISTORY
    captcha_detected = state.get('captcha_detected', {})
    captcha_solved = state.get('captcha_solved', False)
    captcha_attempts = state.get('captcha_attempts', [])
    
    if captcha_detected.get('type'):
        context += f"\n\n🤖 CAPTCHA STATUS:"
        context += f"\n  Type: {captcha_detected['type'].upper()}"
        context += f"\n  Confidence: {captcha_detected.get('confidence', 0)}%"
        context += f"\n  Status: {'✅ SOLVED' if captcha_solved else '⏳ PENDING'}"
        
        if captcha_solved:
            service_used = state.get('captcha_service_used', 'Unknown')
            context += f"\n  Service: {service_used}"
        else:
            context += f"\n💡 REQUIRED ACTION: Use 'solve_captcha' to handle this challenge"
    
    if captcha_attempts:
        context += f"\n\n🔄 CAPTCHA ATTEMPT HISTORY ({len(captcha_attempts)} attempts):"
        for i, attempt in enumerate(captcha_attempts[-3:], 1):  # Last 3 attempts
            status = "✅" if attempt.get('solved', False) else "❌"
            service = attempt.get('service', 'Unknown')
            context += f"\n  {status} Attempt {i}: {service} - {attempt.get('type', 'Unknown')}"
    
    return context

# --- NEW: Enhanced selector validation system ---
async def validate_selectors_systematically(page, all_elements_context: List[Dict], search_text: str, state: AgentState) -> Dict[str, Any]:
    """
    Systematically test selectors one-by-one to find working ones.
    Returns validation results with working and failed selectors.
    """
    working_selectors = []
    failed_selectors = []
    best_selector = None
    best_element = None
    
    # Get previously tested selectors to avoid retesting
    selector_attempts = state.get('selector_attempts', {})
    previously_tested = selector_attempts.get(search_text, [])
    
    # Priority order: interactive visible > visible > interactive > any
    sorted_elements = sorted(all_elements_context, key=lambda x: (
        x.get('is_visible', False) and x.get('is_interactive', False),  # Interactive + visible first
        x.get('is_visible', False),  # Then visible
        x.get('is_interactive', False),  # Then interactive
    ), reverse=True)
    
    print(f"🔧 SYSTEMATIC SELECTOR VALIDATION for '{search_text}':")
    print(f"   Elements to test: {len(sorted_elements)}")
    print(f"   Previously tested: {len(previously_tested)}")
    
    tested_count = 0
    max_tests = 10  # Limit testing to prevent endless loops
    
    for element_idx, element in enumerate(sorted_elements):
        if tested_count >= max_tests:
            break
            
        selectors = element.get('suggested_selectors', [])
        element_type = f"{element.get('tag_name', 'unknown')}"
        visibility = "visible" if element.get('is_visible', False) else "hidden"
        interactivity = "interactive" if element.get('is_interactive', False) else "static"
        
        print(f"   Testing element {element_idx + 1}: {element_type} ({visibility}, {interactivity})")
        
        for selector_idx, selector in enumerate(selectors):
            if tested_count >= max_tests:
                break
                
            # Skip if already tested recently
            if selector in previously_tested:
                print(f"     Selector {selector_idx + 1}: {selector} - SKIPPED (tested before)")
                continue
                
            tested_count += 1
            
            try:
                # Test if selector exists and is actionable
                locator = page.locator(selector)
                element_count = await locator.count()
                
                if element_count > 0:
                    # Additional checks for actionability
                    is_visible = await locator.first.is_visible() if element_count > 0 else False
                    is_enabled = await locator.first.is_enabled() if element_count > 0 else False
                    
                    if is_visible and is_enabled:
                        working_selectors.append(selector)
                        if not best_selector:  # Use first working selector as best
                            best_selector = selector
                            best_element = element
                        print(f"     ✅ Selector {selector_idx + 1}: {selector} - WORKING (visible & enabled)")
                    else:
                        failed_selectors.append(selector)
                        print(f"     ❌ Selector {selector_idx + 1}: {selector} - FAILED (not actionable)")
                else:
                    failed_selectors.append(selector)
                    print(f"     ❌ Selector {selector_idx + 1}: {selector} - FAILED (not found)")
                    
            except Exception as e:
                failed_selectors.append(selector)
                print(f"     ❌ Selector {selector_idx + 1}: {selector} - FAILED (error: {str(e)[:50]})")
    
    # Update element interaction log
    interaction_log = state.get('element_interaction_log', [])
    interaction_log.append({
        'step': state.get('step', 0),
        'search_text': search_text,
        'total_elements': len(all_elements_context),
        'tested_selectors': tested_count,
        'working_selectors': len(working_selectors),
        'failed_selectors': len(failed_selectors),
        'best_selector': best_selector,
        'timestamp': get_current_timestamp()
    })
    state['element_interaction_log'] = interaction_log[-20:]  # Keep last 20 interactions
    
    print(f"   📊 VALIDATION RESULTS:")
    print(f"     Tested: {tested_count}")
    print(f"     Working: {len(working_selectors)}")
    print(f"     Failed: {len(failed_selectors)}")
    print(f"     Best: {best_selector}")
    
    return {
        'working_selectors': working_selectors,
        'failed_selectors': failed_selectors,
        'best_selector': best_selector,
        'best_element': best_element,
        'total_tested': tested_count
    }

async def verify_action_from_screenshot(page, action: dict, state: AgentState) -> Dict[str, Any]:
    """
    Verify if an action was successful by analyzing the page state and taking a new screenshot.
    """
    verification_result = {
        'action': action,
        'step': state.get('step', 0),
        'timestamp': get_current_timestamp(),
        'success': False,
        'changes_detected': False,
        'screenshot_path': None,
        'verification_notes': []
    }
    
    try:
        # Take a screenshot for verification
        screenshot_path = state['job_artifacts_dir'] / f"verification_step_{state['step']}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        verification_result['screenshot_path'] = str(screenshot_path)
        
        # Basic verification checks based on action type
        action_type = action.get('type')
        
        if action_type == 'click':
            # Check if page changed after click (URL, new elements, etc.)
            current_url = page.url
            prev_url = state.get('url', '')
            if current_url != prev_url:
                verification_result['changes_detected'] = True
                verification_result['verification_notes'].append(f"URL changed: {prev_url} → {current_url}")
                
        elif action_type == 'fill':
            # Check if the input field contains the filled text
            selector = action.get('selector')
            if selector:
                try:
                    field_value = await page.locator(selector).input_value()
                    expected_text = action.get('text', '')
                    if expected_text in field_value:
                        verification_result['success'] = True
                        verification_result['verification_notes'].append(f"Input field contains expected text: '{expected_text}'")
                    else:
                        verification_result['verification_notes'].append(f"Input field value '{field_value}' doesn't match expected '{expected_text}'")
                except:
                    verification_result['verification_notes'].append("Could not verify input field value")
                    
        elif action_type == 'scroll':
            # For scroll, just mark as successful if no errors occurred
            verification_result['success'] = True
            verification_result['changes_detected'] = True
            verification_result['verification_notes'].append("Scroll action completed")
            
        elif action_type == 'press':
            # Check if pressing Enter caused navigation or form submission
            current_url = page.url
            prev_url = state.get('url', '')
            if current_url != prev_url:
                verification_result['changes_detected'] = True
                verification_result['success'] = True
                verification_result['verification_notes'].append(f"Key press triggered navigation: {prev_url} → {current_url}")
                
        # Update action verification history
        action_verification = state.get('action_verification', [])
        action_verification.append(verification_result)
        state['action_verification'] = action_verification[-20:]  # Keep last 20 verifications
        
    except Exception as e:
        verification_result['verification_notes'].append(f"Verification error: {str(e)}")
    
    return verification_result

# --- NEW: Stable action signature builder ---
def make_action_signature(action: dict) -> str:
    """Create a normalized signature for an agent action to detect repeats.

    Includes the action type plus distinguishing fields if present.
    Falls back to 'invalid' if the structure is unexpected.
    """
    if not isinstance(action, dict) or not action:
        return "invalid"
    parts = [action.get("type", "")]
    for key in ("selector", "text", "key"):
        val = action.get(key)
        if isinstance(val, str) and val.strip():
            # Truncate very long values to keep signature compact
            truncated = val.strip()
            if len(truncated) > 80:
                truncated = truncated[:77] + "..."
            parts.append(f"{key}={truncated}")
    return "|".join(parts) or "invalid"

# --- LangGraph Nodes ---
async def navigate_to_page(state: AgentState) -> AgentState:
    try:
        await state['page'].goto(state['query'], wait_until='domcontentloaded', timeout=60000)
        push_status(state['job_id'], "navigation_complete", {"url": state['query']})
    except Exception as e:
        push_status(state['job_id'], "navigation_failed", {"url": state['query'], "error": str(e)})
        print(f"Navigation failed: {e}")
        # Still continue with the process even if navigation partially fails
    try:
            await install_popup_killer(state['page'])
    except Exception as e:
        print(f"⚠️ Popup killer installation failed (non-critical): {e}")
    
    # 🤖 AUTOMATIC CAPTCHA DETECTION ON PAGE LOAD
    # Proactively scan for CAPTCHAs immediately after navigation
    # This helps the agent identify CAPTCHA challenges early in the process
    try:
        print(f"🔍 PROACTIVE CAPTCHA SCAN: Checking for CAPTCHA challenges on page load...")
        captcha_detection = await captcha_solver.detect_captcha_universal(state['page'])
        
        if captcha_detection['type']:
            # CAPTCHA detected - log for agent awareness
            detection_msg = f"🚨 CAPTCHA DETECTED ON PAGE LOAD: {captcha_detection['type'].upper()} found (confidence: {captcha_detection['confidence']}%)"
            state['history'].append(f"Navigation: {detection_msg}")
            print(detection_msg)
            
            # Store detection info for agent to use
            state['captcha_detected'] = captcha_detection
            push_status(state['job_id'], "captcha_detected", {
                "type": captcha_detection['type'],
                "confidence": captcha_detection['confidence'],
                "sitekey": captcha_detection.get('sitekey', 'unknown')
            })
        else:
            print(f"✅ CAPTCHA SCAN CLEAR: No CAPTCHA challenges detected on initial page load")
            
    except Exception as e:
        print(f"⚠️ CAPTCHA detection during navigation failed (non-critical): {e}")
    
    # Only clear input fields once during initial navigation (step 1)
    # Don't clear if we're waiting for or have received user input
    should_clear_inputs = (
        state['step'] == 1 and 
        not state.get('waiting_for_user_input', False) and 
        not state.get('user_input_response') and
        not state.get('user_input_flow_active', False) and
        state['job_id'] not in JOBS_IN_INPUT_FLOW  # Global protection
    )
    
    # DEBUG: Add comprehensive logging for input clearing decision
    print(f"🧹 INPUT CLEARING DEBUG - Job {state['job_id']}")
    print(f"   step: {state['step']}")
    print(f"   waiting_for_user_input: {state.get('waiting_for_user_input', False)}")
    print(f"   user_input_response: '{state.get('user_input_response', 'None')}'")
    print(f"   user_input_flow_active: {state.get('user_input_flow_active', False)}")
    print(f"   job_id in JOBS_IN_INPUT_FLOW: {state['job_id'] in JOBS_IN_INPUT_FLOW}")
    print(f"   should_clear_inputs: {should_clear_inputs}")
    
    if should_clear_inputs:
        try:
            inputs = await state['page'].query_selector_all('input')
            clear_count = 0
            for inp in inputs:
                try:
                    if await inp.is_enabled() and await inp.is_visible():
                        await inp.fill("")
                        clear_count += 1
                except Exception as e:
                    print(f"Failed to clear input field: {e}")
            print(f"   ✅ Cleared {clear_count} input fields during initial navigation")
        except Exception as e:
            print(f"   ❌ Failed to clear input fields: {e}")
    else:
        print(f"   ⏭️ Skipping input clearing (protection active)")

    return state

async def agent_reasoning_node(state: AgentState) -> AgentState:
    job_id = state['job_id']
    push_status(job_id, "agent_step", {"step": state['step'], "max_steps": state['max_steps']})
    
    screenshot_path = state['job_artifacts_dir'] / f"{state['step']:02d}_step.png"
    screenshot_success = False
    
    try:
        # Multiple attempts to ensure page is ready for screenshot
        try:
            # First try: wait for network to be idle (basic loading complete)
            await state['page'].wait_for_load_state('networkidle', timeout=3000)
        except:
            try:
                # Second try: wait for DOM content to be loaded
                await state['page'].wait_for_load_state('domcontentloaded', timeout=2000)
            except:
                # Third try: just wait a bit for any pending operations
                await asyncio.sleep(1)
        
        # Take screenshot with reasonable timeout
        await state['page'].screenshot(path=screenshot_path, timeout=20000, full_page=False)  # 20 second timeout, not full page
        resize_image_if_needed(screenshot_path)
        screenshot_success = True
        state['screenshots'].append(f"screenshots/{job_id}/{state['step']:02d}_step.png")
        print(f"Screenshot saved: {screenshot_path}")
    except Exception as e:
        # If screenshot fails, do not create placeholder file to avoid empty image errors
        push_status(job_id, "screenshot_failed", {"error": str(e), "step": state['step']})
        print(f"Screenshot failed at step {state['step']}: {e}")
        # Don't add to screenshots list if it failed - this prevents sending empty images to API
        screenshot_path = None

    # Enhanced history formatting with comprehensive memory context
    history_text = "\n".join(state['history'])
    
    # Add enhanced memory context
    history_text += build_enhanced_memory_context(state)

    # --- NEW: Add user input context if available ---
    if state.get('user_input_response'):
        input_type = state.get('user_input_request', {}).get('input_type', 'input')
        is_sensitive = state.get('user_input_request', {}).get('is_sensitive', False)
        
        if is_sensitive:
            # For sensitive data, provide clear instruction with the actual value
            # The LLM needs to see the actual value to use it correctly
            history_text += f"\n\n🔐 USER PROVIDED {input_type.upper()}: {state['user_input_response']} [SENSITIVE DATA - USE THIS EXACT VALUE]"
            history_text += f"\n💡 CRITICAL: Use this exact value '{state['user_input_response']}' in your next fill action."
            history_text += f"\n🚨 DO NOT GENERATE YOUR OWN {input_type.upper()}! Use '{state['user_input_response']}' exactly as provided."
            history_text += f"\n❌ DO NOT use placeholders like {{{{USER_INPUT}}}} - use the actual value '{state['user_input_response']}' directly."
        else:
            # Show non-sensitive data and clear instruction
            history_text += f"\n\n👤 USER PROVIDED {input_type.upper()}: {state['user_input_response']} [Ready to use in next fill action]"
            history_text += f"\n💡 IMPORTANT: Use this exact value '{state['user_input_response']}' in your next fill action."
            history_text += f"\n❌ DO NOT use placeholders like {{{{USER_INPUT}}}} - use the actual value '{state['user_input_response']}' directly."
        
        # DON'T reset user input here - it will be cleared after being used in fill action

    # --- NEW: Add extract loop prevention context ---
    recent_extracts = state.get('recent_extracts', [])
    if recent_extracts:
        history_text += f"\n\n🚫 RECENT EXTRACTS (DO NOT REPEAT): {recent_extracts[-3:]}"
        history_text += "\n⚡ ACTION REQUIRED: You already have selectors - USE THEM NOW with click/fill/press!"
        history_text += "\n🔴 FORBIDDEN: Do NOT call extract_correct_selector_using_text again for the same text!"

    # --- NEW: Inject anti-repeat guidance if we have failed actions ---
    if state.get('failed_actions'):
        failed_list = sorted(state['failed_actions'].items(), key=lambda x: -x[1])
        total_failures = sum(count for _, count in failed_list)
        
        history_text += f"\n\n🚨 BANNED ACTIONS - DO NOT EVEN THINK ABOUT THESE ({total_failures} failures):"
        
        # Show most critical failed actions prominently
        for sig, count in failed_list[:8]:  # show top 8 failures
            history_text += f"\n  🚫 BANNED: {sig} (failed {count}x) - NEVER attempt this again!"
        
        # Add specific warnings for the most common failure patterns
        banned_selectors = [sig for sig, _ in failed_list if "input[placeholder='Search for Products']" in sig]
        if banned_selectors:
            history_text += f"\n\n� CRITICAL WARNING: The selector \"input[placeholder='Search for Products']\" is COMPLETELY BANNED!"
            history_text += f"\n  This selector has failed multiple times - DO NOT use it in any action!"
            history_text += f"\n  ❌ Don't click it, don't fill it, don't press keys on it - IT DOESN'T WORK!"
            
        # Extract banned patterns for specific guidance
        banned_clicks = [sig for sig, _ in failed_list if sig.startswith("click|")]
        banned_fills = [sig for sig, _ in failed_list if sig.startswith("fill|")]
        
        if banned_clicks:
            history_text += f"\n\n🚫 BANNED CLICK PATTERNS ({len(banned_clicks)} patterns):"
            for sig in banned_clicks[:3]:
                selector = sig.split("|selector=")[1] if "|selector=" in sig else "unknown"
                history_text += f"\n  • DO NOT click on: {selector}"
        
        if banned_fills:
            history_text += f"\n\n🚫 BANNED FILL PATTERNS ({len(banned_fills)} patterns):"
            for sig in banned_fills[:3]:
                selector = sig.split("|selector=")[1].split("|")[0] if "|selector=" in sig else "unknown"
                history_text += f"\n  • DO NOT fill: {selector}"
        
        # Provide specific guidance based on failure patterns
        if total_failures > 10:
            history_text += f"\n\n💡 ALTERNATIVE STRATEGIES (since {total_failures} actions failed):"
            history_text += "\n  ✅ Try scroll action to explore page content"
            history_text += "\n  ✅ Use extract_correct_selector_using_text with different text (e.g., 'button', 'search icon', 'input')"
            history_text += "\n  ✅ Look for alternative elements (menu items, category links, different search boxes)"
            history_text += "\n  ✅ Try press actions with Tab/Enter keys to navigate"
            history_text += "\n  ✅ Change approach entirely - maybe navigate through categories instead of search"
        
        if len(failed_list) > 8:
            history_text += f"\n  ... {len(failed_list) - 8} more banned patterns"
            
        history_text += f"\n\n🎯 THINKING RULE: If you catch yourself considering any banned action above, STOP and think of something else!"
    
    # Add found element context with MANDATORY testing protocol
    if state.get('found_element_context'):
        element_ctx = state['found_element_context']
        history_text += f"\n\n🎯 MANDATORY SELECTOR TESTING REQUIRED!"
        history_text += f"\n📋 Search Text: '{element_ctx['text']}' - Total Matches: {element_ctx.get('total_matches', 0)}"
        
        # Show untested selectors that MUST be tested
        untested_selectors = element_ctx.get('untested_selectors', [])
        current_index = element_ctx.get('current_test_index', 0)
        is_search_related = element_ctx.get('is_search_related', False)
        
        if untested_selectors and current_index < len(untested_selectors):
            next_selector = untested_selectors[current_index]
            history_text += f"\n\n🚨🚨🚨 MANDATORY SELECTOR TESTING IN PROGRESS 🚨🚨🚨"
            history_text += f"\n📋 EXTRACTED TEXT: '{element_ctx['text']}'"
            history_text += f"\n🎯 CURRENT PROGRESS: Selector #{current_index + 1} of {len(untested_selectors)}"
            history_text += f"\n\n📍 EXACT SELECTOR TO TEST NOW: '{next_selector}'"
            history_text += f"\n🔥 MANDATORY ACTION: {{'\"type\": \"click\", \"selector\": \"{next_selector}\"}}"
            history_text += f"\n\n⚠️  CRITICAL RULES:"
            history_text += f"\n   • You MUST use the EXACT selector above"
            history_text += f"\n   • Do NOT create your own selectors"
            history_text += f"\n   • Do NOT use input[placeholder='...'] or similar"
            history_text += f"\n   • Do NOT use input[type='text'] or similar"
            history_text += f"\n   • Do NOT try fill, press, or other actions yet"
            history_text += f"\n   • ONLY use click action with the exact selector provided"
            
            if is_search_related:
                history_text += f"\n\n🔍 SEARCH PRIORITY: This is search-related! You MUST test ALL {len(untested_selectors)} selectors before doing anything else!"
            
            history_text += f"\n\n❌ ABSOLUTELY FORBIDDEN UNTIL ALL SELECTORS TESTED:"
            history_text += f"\n   • Scrolling the page"
            history_text += f"\n   • Navigating to categories"
            history_text += f"\n   • Creating your own selectors"
            history_text += f"\n   • Using extract_correct_selector_using_text again"
            history_text += f"\n   • Any other action except testing the provided selector"
            history_text += f"\n\n📊 REMAINING: {len(untested_selectors) - current_index} selectors still need testing"
            
        elif untested_selectors and current_index >= len(untested_selectors):
            history_text += f"\n✅ ALL SELECTORS TESTED: {len(untested_selectors)} selectors tested, now can try alternative approach"
        
        # Show the recommended action prominently
        if element_ctx.get('best_selector') and element_ctx.get('best_element'):
            best_elem = element_ctx['best_element']
            extract_count = recent_extracts.count(element_ctx['text'])
            rotation_note = f" (Selector #{extract_count + 1})" if extract_count > 0 else ""
            
            history_text += f"\n\n💡 NEXT ACTION SUGGESTION: Use selector '{element_ctx['best_selector']}'{rotation_note}"
            history_text += f"\n   ✅ Ready-to-use Selector: '{element_ctx['best_selector']}'"
            history_text += f"\n   🎯 Element: {best_elem['tag_name']} ({'visible' if best_elem.get('is_visible') else 'hidden'}, {'interactive' if best_elem.get('is_interactive') else 'static'})"
            
            if extract_count > 0:
                history_text += f"\n   🔄 Auto-rotated to different selector (attempt #{extract_count + 1})"
            
            history_text += f"\n\n🔥 ACTION REQUIRED: Use this selector immediately with click, fill, or press action!"
        
        # Simplified list of top 3 alternatives
        if element_ctx.get('all_elements'):
            interactive_elements = [e for e in element_ctx['all_elements'][:3] if e.get('is_interactive')]
            if interactive_elements:
                history_text += f"\n\n⚡ Alternative Interactive Elements:"
                for i, elem in enumerate(interactive_elements[:3]):
                    selectors = elem['suggested_selectors'][:2]  # Show only first 2 selectors
                    history_text += f"\n  {i+1}. {elem['tag_name']}: {', '.join(selectors)}"
        
        # Debug output
        print(f"🤖 ELEMENT CONTEXT FOR AGENT:")
        print(f"   Search Text: '{element_ctx['text']}'")
        print(f"   Total Matches: {element_ctx.get('total_matches', 0)}")
        print(f"   Available Selectors: {len(element_ctx.get('all_suggested_selectors', []))}")
    
    # MODIFIED: Capture token usage from agent action with error handling
    try:
        # Only pass screenshot if it was successfully taken
        images_to_send = [screenshot_path] if screenshot_path and screenshot_success else []
        
        action_response, usage = get_agent_action(
            query=state['refined_query'],
            url= state['page'].url,
            html= await state['page'].content(),
            provider=state['provider'],
            screenshot_path=screenshot_path if screenshot_success else None,
            history=history_text,
            failed_actions=state.get('failed_actions', {})  # Pass banned actions to LLM
        )
        
        # DEBUG: Log the raw LLM response to understand what's being returned
        print(f"🤖 LLM RESPONSE DEBUG - Job {job_id}:")
        print(f"   action_response: {action_response}")
        if action_response and isinstance(action_response, dict):
            action = action_response.get("action")
            if action and action.get("type") == "fill":
                print(f"   🔍 FILL ACTION DETECTED:")
                print(f"      text: '{action.get('text', 'None')}'")
                print(f"      selector: '{action.get('selector', 'None')}'")
        
        # NEW: Store usage for this step
        state['token_usage'].append({
            "task": f"agent_step_{state['step']}",
            **usage
        })

        push_status(job_id, "agent_thought", {
            "thought": action_response.get("thought", "No thought provided."),
            "usage": usage
        })
        
        # Validate that we have a proper action
        if not action_response or not isinstance(action_response, dict):
            raise ValueError("Invalid action response format")
            
        action = action_response.get("action")
        if not action or not isinstance(action, dict) or not action.get("type"):
            raise ValueError("Missing or invalid action in response")
        
        # PRE-EXECUTION CHECK: Analyze if the action would be banned before setting it
        proposed_signature = make_action_signature(action)  # Function is defined in same file
        failed_actions = state.get('failed_actions', {})
        
        if proposed_signature in failed_actions:
            # The LLM ignored our warnings - generate an alternative action
            print(f"🚨 PRE-EXECUTION BLOCK: LLM proposed banned action {proposed_signature}")
            
            # Generate a corrective action based on the intent and context
            if "search" in proposed_signature.lower() or "input[placeholder" in proposed_signature:
                # Extract meaningful keywords from user's actual query
                refined_query = state.get('refined_query', '')
                fallback_text = "button"  # Safe universal fallback
                
                if refined_query:
                    # Use first meaningful word from the user's actual query
                    query_words = [word for word in refined_query.split() if len(word) > 3]
                    if query_words:
                        fallback_text = query_words[0]
                
                corrective_action = {"type": "extract_correct_selector_using_text", "text": fallback_text}
                state['history'].append(f"Step {state['step']}: 🔧 AUTO-CORRECTED banned search action to extract approach using '{fallback_text}' from user query")
            elif "extract_correct_selector_using_text" in proposed_signature:
                # Agent wants to extract - suggest scroll instead
                corrective_action = {"type": "scroll", "direction": "down"}
                state['history'].append(f"Step {state['step']}: 🔧 AUTO-CORRECTED repeated extract to scroll action")
            else:
                # Generic correction - scroll to see more content
                corrective_action = {"type": "scroll", "direction": "down"}
                state['history'].append(f"Step {state['step']}: 🔧 AUTO-CORRECTED banned action to exploration")
            
            action = corrective_action
            print(f"   Corrected to: {action}")
            
        state['last_action'] = action
        
    except Exception as e:
        # Handle LLM parsing errors gracefully
        error_msg = f"Failed to get agent action: {str(e)}"
        push_status(job_id, "agent_error", {"error": error_msg, "step": state['step']})
        print(f"Agent reasoning error at step {state['step']}: {error_msg}")
        
        # Provide emergency action to continue mission - NEVER give up!
        failed_count = len(state.get('failed_actions', {}))
        recent_extracts = state.get('recent_extracts', [])
        
        # Analyze failed actions to choose smarter emergency action
        failed_actions = state.get('failed_actions', {})
        search_related_failures = [sig for sig in failed_actions.keys() if 'search' in sig.lower() or 'input[placeholder' in sig]
        
        # Choose emergency action based on failure patterns
        if len(search_related_failures) > 5:
            # Many search failures - try navigation approach
            emergency_action = {
                "type": "extract_correct_selector_using_text", 
                "text": "Mobiles"
            }
            state['history'].append(f"Step {state['step']}: 🚨 PATTERN DETECTED: Search failures = {len(search_related_failures)}, switching to navigation approach")
        elif failed_count < 3 and not recent_extracts:
            # Try to extract elements for interaction
            emergency_action = {
                "type": "extract_correct_selector_using_text", 
                "text": "button"
            }
        elif state['step'] % 3 == 0:
            # Every 3rd emergency, try scrolling
            emergency_action = {
                "type": "scroll", 
                "direction": "down"
            }
        elif state['step'] < state['max_steps'] - 5:
            # Try extracting common elements, avoiding known failures
            search_terms = ["menu", "category", "link", "button", "navigation", "home"]
            term_index = (state['step'] - 1) % len(search_terms)
            emergency_action = {
                "type": "extract_correct_selector_using_text", 
                "text": search_terms[term_index]
            }
        else:
            # Only finish as absolute last resort
            emergency_action = {
                "type": "finish", 
                "reason": f"Emergency finish after exhausting all options at step {state['step']} with {len(failed_actions)} different failures"
            }
        
        state['last_action'] = emergency_action
        state['history'].append(f"Step {state['step']}: 🚨 EMERGENCY ACTION due to reasoning failure: {emergency_action}")
        print(f"🚨 Emergency action at step {state['step']}: {emergency_action}")
        
        # Still record some usage info if available
        state['token_usage'].append({
            "task": f"agent_step_{state['step']}_failed",
            "input_tokens": 0,
            "output_tokens": 0,
            "error": error_msg
        })
    
    # Clear found element context after agent has processed it
    if state.get('found_element_context'):
        state['found_element_context'] = {}
    
    return state

async def execute_action_node(state: AgentState) -> AgentState:

    """
    Open any website on Android device using Chrome.
    
    Args:
        device_id (str): Android device ID
        url (str): Website URL to open
        incognito (bool): Use incognito mode (default: True)
        wait_time (int): Time to wait after opening URL (seconds)
        take_screenshot (bool): Take screenshot after loading
        custom_actions (function): Optional custom function to perform actions on the page
    
    Returns:
        dict: Result containing status, page info, and any extracted data
    """

    job_id = state['job_id']
    action = state['last_action']
    page = state['page']
    
    # --- NEW: Build signature & skip if previously failed ---
    action_signature = make_action_signature(action)
    state['attempted_action_signatures'].append(action_signature)

    if action_signature in state.get('failed_actions', {}):
        failure_count = state['failed_actions'][action_signature]
        
        # Generate specific alternative suggestions based on the failed action
        alternatives = []
        if "click|selector=input[placeholder='Search for Products']" in action_signature:
            alternatives = [
                "Try: scroll down to find different elements",
                "Try: extract_correct_selector_using_text with text 'search icon' or 'search button'", 
                "Try: press Tab key to navigate to search field",
                "Try: look for menu categories like 'Mobiles' to navigate instead of searching"
            ]
        elif "fill|selector=" in action_signature:
            alternatives = [
                "Try: click on the element first, then fill",
                "Try: extract_correct_selector_using_text to find a different selector", 
                "Try: scroll to find alternative input fields",
                "Try: press Tab to navigate to input fields"
            ]
        elif "extract_correct_selector_using_text" in action_signature:
            alternatives = [
                "Try: scroll to see different page content",
                "Try: use different search text (single words like 'button', 'input', 'menu')",
                "Try: navigate through visible links or categories instead"
            ]
        else:
            alternatives = [
                "Try: scroll action to explore the page",
                "Try: extract_correct_selector_using_text with different text",
                "Try: different interaction approach (press keys, navigate menus)"
            ]
        
        educational_message = f"Step {state['step']}: 🚫 BLOCKED DUPLICATE ACTION `{action_signature}` (failed {failure_count}x before)"
        educational_message += f"\n💡 The LLM should NOT have considered this action - it was warned about banned patterns!"
        educational_message += f"\n🔄 AUTO-SUGGESTING ALTERNATIVES:"
        for alt in alternatives[:2]:  # Show top 2 alternatives
            educational_message += f"\n  • {alt}"
        
        state['history'].append(educational_message)
        push_status(job_id, "duplicate_action_blocked_with_education", {
            "signature": action_signature, 
            "failure_count": failure_count,
            "suggested_alternatives": alternatives[:2]
        })
        
        # Record token usage with educational context
        state['token_usage'].append({
            "task": f"action_blocked_education_{state['step']}",
            "input_tokens": 0,
            "output_tokens": 0,
            "blocked_signature": action_signature,
            "lesson": "LLM ignored banned action warnings"
        })
        state['step'] += 1
        return state

    push_status(job_id, "executing_action", {"action": action, "signature": action_signature})
    
    # Get action type for enforcement
    action_type = action.get("type")
    
    # NEW: Validate that agent is not using brain-generated selectors
    if action_type in ['click', 'fill', 'press']:
        selector = action.get('selector', '')
        brain_generated_patterns = [
            'input[placeholder=',
            'input[type="text"]',
            'input[type=\'text\']', 
            'button[type="submit"]',
            '.search-input',
            '#search-box',
            '.search-field',
            'input.search',
            '[placeholder*="search"]'
        ]
        
        # Check if selector looks like agent created it from knowledge
        is_brain_generated = any(pattern in selector.lower() for pattern in brain_generated_patterns)
        
        if is_brain_generated:
            found_context = state.get('found_element_context', {})
            if found_context.get('testing_required', False):
                untested_selectors = found_context.get('untested_selectors', [])
                current_index = found_context.get('current_test_index', 0)
                
                if untested_selectors and current_index < len(untested_selectors):
                    expected_selector = untested_selectors[current_index]
                    error_msg = f"\n🚨 BRAIN-GENERATED SELECTOR DETECTED 🚨"
                    error_msg += f"\n\nYou used: '{selector}'"
                    error_msg += f"\nThis looks like a selector created from your knowledge, not from extraction results."
                    error_msg += f"\n\n✅ You MUST use the extracted selector: '{expected_selector}'"
                    error_msg += f"\n\nRemember: Use ONLY selectors from extract_correct_selector_using_text results!"
                    raise ValueError(error_msg)
    
    # NEW: Enforce mandatory selector testing protocol
    found_context = state.get('found_element_context', {})
    if found_context.get('testing_required', False):
        untested_selectors = found_context.get('untested_selectors', [])
        current_index = found_context.get('current_test_index', 0)
        
        # If we have untested selectors, enforce testing them
        if untested_selectors and current_index < len(untested_selectors):
            expected_selector = untested_selectors[current_index]
            
            # Check if the current action is testing the expected selector
            if action_type in ['click', 'fill', 'press']:
                current_selector = action.get('selector', '')
                if current_selector == expected_selector:
                    # This is the expected selector test - update the index
                    found_context['current_test_index'] = current_index + 1
                    state['found_element_context'] = found_context
                    print(f"🧪 TESTING SELECTOR {current_index + 1}/{len(untested_selectors)}: {expected_selector}")
                else:
                    # Wrong selector - provide detailed error with expected vs actual
                    error_msg = f"\n🚨 MANDATORY TESTING VIOLATION 🚨"
                    error_msg += f"\n\nYou are REQUIRED to test selector #{current_index + 1} from your extraction results."
                    error_msg += f"\n\nEXPECTED SELECTOR: '{expected_selector}'"
                    error_msg += f"\nYOUR SELECTOR:     '{current_selector}'"
                    error_msg += f"\n\nYou CANNOT use your own selectors like:"
                    error_msg += f"\n❌ input[placeholder='...']"
                    error_msg += f"\n❌ input[type='text']" 
                    error_msg += f"\n❌ Any selector you create yourself"
                    error_msg += f"\n\n✅ You MUST use: '{expected_selector}'"
                    error_msg += f"\n\nCORRECT ACTION: {{\"type\": \"click\", \"selector\": \"{expected_selector}\"}}"
                    error_msg += f"\n\nRemaining selectors to test: {len(untested_selectors) - current_index}"
                    raise ValueError(error_msg)
            elif action_type not in ['extract_correct_selector_using_text']:
                # Non-extraction action while testing is required
                error_msg = f"\n🚨 MANDATORY TESTING VIOLATION 🚨"
                error_msg += f"\n\nYou have {len(untested_selectors) - current_index} untested selectors for '{found_context['text']}'."
                error_msg += f"\n\nNEXT REQUIRED ACTION: {{\"type\": \"click\", \"selector\": \"{expected_selector}\"}}"
                error_msg += f"\n\nYou CANNOT:"
                error_msg += f"\n❌ Scroll the page"
                error_msg += f"\n❌ Navigate to categories"  
                error_msg += f"\n❌ Use extract_correct_selector_using_text again"
                error_msg += f"\n❌ Take any other action"
                error_msg += f"\n\n✅ You MUST test selector #{current_index + 1}: '{expected_selector}'"
                raise ValueError(error_msg)
        elif untested_selectors and current_index >= len(untested_selectors):
            # All selectors tested, clear the requirement
            found_context['testing_required'] = False
            state['found_element_context'] = found_context
            print(f"✅ SELECTOR TESTING COMPLETE: All {len(untested_selectors)} selectors tested for '{found_context['text']}'")
    
    try:
        if action_type == "click":
            await page.locator(action["selector"]).click(timeout=5000)
            
            # Track search flow progress
            selector = action.get("selector", "")
            if any(search_term in selector.lower() for search_term in ["search", "input", "query"]):
                search_flow = state.get('search_flow_state', {})
                search_flow['clicked'] = True
                state['search_flow_state'] = search_flow
        elif action_type == "fill":
            # NEW: Enhanced fill action that can use user-provided input
            fill_text = action["text"]
            used_user_input = False
            
            # DEBUG: Add comprehensive logging for fill actions
            print(f"🔍 FILL DEBUG - Job {job_id}")
            print(f"   Original fill_text: '{fill_text}'")
            print(f"   user_input_response: '{state.get('user_input_response', 'None')}'")
            print(f"   user_input_flow_active: {state.get('user_input_flow_active', False)}")
            print(f"   selector: '{action.get('selector', 'None')}'")
            
            # Check if this is a placeholder that should use user input
            if fill_text in ["{{USER_INPUT}}", "{{PASSWORD}}", "{{EMAIL}}", "{{PHONE}}", "{{OTP}}"]:
                if state.get('user_input_response'):
                    fill_text = state['user_input_response']
                    used_user_input = True
                    state['history'].append(f"Step {state['step']}: 🔄 Using user-provided input via placeholder")
                    print(f"   🔄 Replaced placeholder with user input: '{fill_text}'")
                else:
                    # No user input available, this shouldn't happen but handle gracefully
                    raise ValueError(f"Placeholder {fill_text} requires user input but none available")
            
            # Check if the agent is directly using the user input value
            elif state.get('user_input_response') and fill_text == state['user_input_response']:
                used_user_input = True
                state['history'].append(f"Step {state['step']}: 🔄 Using user-provided input directly")
                print(f"   ✅ Direct match with user input")
            
            # FORCE USER INPUT FOR PASSWORD FIELDS: If we have user input and this is a password field, use it
            elif (state.get('user_input_response') and 
                  ('password' in action.get('selector', '').lower() or 
                   'pass' in action.get('selector', '').lower() or
                   action.get('selector', '') in ['#password', '[type="password"]'] or
                   '[type="password"]' in action.get('selector', '')) and
                  state.get('user_input_request', {}).get('input_type') == 'password'):
                print(f"   🔒 FORCING USER PASSWORD: LLM tried to use '{fill_text}' but overriding with user input")
                fill_text = state['user_input_response']
                used_user_input = True
                state['history'].append(f"Step {state['step']}: 🔒 FORCED user password instead of LLM-generated value")
                print(f"   🔄 OVERRODE LLM password with user input: '{fill_text}'")
                
            # ADDITIONAL CHECK: If user provided password recently and this looks like a password field
            elif (state.get('user_input_response') and 
                  state.get('user_input_request', {}).get('input_type') == 'password' and
                  (len(fill_text) > 6 and any(c.isdigit() for c in fill_text) and any(c.isupper() for c in fill_text))):
                # This looks like a generated password pattern, override it
                print(f"   🔒 SUSPICIOUS PASSWORD PATTERN: Overriding '{fill_text}' with user input")
                fill_text = state['user_input_response']
                used_user_input = True
                state['history'].append(f"Step {state['step']}: 🔒 OVERRODE suspicious password pattern with user input")
                print(f"   🔄 PATTERN OVERRIDE: '{fill_text}'")
            
            print(f"   Final fill_text: '{fill_text}'")
            
            # Add longer delay before filling to ensure page is ready, especially for password fields
            if 'password' in action.get('selector', '').lower() or used_user_input:
                await page.wait_for_timeout(2000)  # Extra time for password fields
            else:
                await page.wait_for_timeout(1000)
            
            await page.locator(action["selector"]).fill(fill_text, timeout=10000)  # Increased timeout
            
            # Track search flow progress
            selector = action.get("selector", "")
            if any(search_term in selector.lower() for search_term in ["search", "input", "query"]):
                search_flow = state.get('search_flow_state', {})
                search_flow['filled'] = True
                state['search_flow_state'] = search_flow
            
            # Add a small delay after filling to prevent immediate clearing
            await page.wait_for_timeout(500)
            
            # If we used user input, clean up the state
            if used_user_input:
                state['user_input_response'] = ""
                state['user_input_request'] = {}
                state['user_input_flow_active'] = False
                JOBS_IN_INPUT_FLOW.discard(job_id)  # Remove from global protection
                state['history'].append(f"Step {state['step']}: ✅ User input successfully used in form field, flow complete")
        elif action_type == "press":
            await page.locator(action["selector"]).press(action["key"],timeout=5000)
            
            # Track search flow progress
            if action.get("key") == "Enter":
                selector = action.get("selector", "")
                if any(search_term in selector.lower() for search_term in ["search", "input", "query"]):
                    search_flow = state.get('search_flow_state', {})
                    search_flow['submitted'] = True
                    state['search_flow_state'] = search_flow
        elif action_type == "scroll":
            # Enhanced scroll implementation with multiple strategies
            direction = action.get("direction", "down")
            distance = action.get("distance", None)
            
            try:
                # Strategy 1: Try modern smooth scrolling with different distances
                if direction == "down":
                    if distance:
                        scroll_script = f"window.scrollBy({{top: {distance}, behavior: 'smooth'}})"
                    else:
                        # Try multiple scroll distances to handle different page layouts
                        scroll_script = """
                        const scrollDistance = Math.max(window.innerHeight * 0.8, 400);
                        window.scrollBy({top: scrollDistance, behavior: 'smooth'});
                        """
                elif direction == "up":
                    if distance:
                        scroll_script = f"window.scrollBy({{top: -{distance}, behavior: 'smooth'}})"
                    else:
                        scroll_script = """
                        const scrollDistance = Math.max(window.innerHeight * 0.8, 400);
                        window.scrollBy({top: -scrollDistance, behavior: 'smooth'});
                        """
                else:
                    # Default to down
                    scroll_script = """
                    const scrollDistance = Math.max(window.innerHeight * 0.8, 400);
                    window.scrollBy({top: scrollDistance, behavior: 'smooth'});
                    """
                
                # Get current scroll position before scrolling
                before_scroll = await page.evaluate("window.pageYOffset")
                
                # Execute the scroll
                await page.evaluate(scroll_script)
                
                # Wait for scroll to complete
                await page.wait_for_timeout(1000)
                
                # Verify scroll happened
                after_scroll = await page.evaluate("window.pageYOffset")
                
                if abs(after_scroll - before_scroll) < 50:
                    # Scroll didn't work well, try alternative methods
                    print(f"🔄 Primary scroll failed, trying alternative methods...")
                    
                    # Strategy 2: Try keyboard scrolling
                    try:
                        await page.keyboard.press("Page Down" if direction == "down" else "Page Up")
                        await page.wait_for_timeout(500)
                        fallback_scroll = await page.evaluate("window.pageYOffset")
                        
                        if abs(fallback_scroll - before_scroll) < 50:
                            # Strategy 3: Force scroll with different approach
                            if direction == "down":
                                force_scroll = """
                                document.documentElement.scrollTop += Math.max(window.innerHeight, 600);
                                document.body.scrollTop += Math.max(window.innerHeight, 600);
                                """
                            else:
                                force_scroll = """
                                document.documentElement.scrollTop -= Math.max(window.innerHeight, 600);
                                document.body.scrollTop -= Math.max(window.innerHeight, 600);
                                """
                            await page.evaluate(force_scroll)
                            await page.wait_for_timeout(500)
                            
                            # Strategy 4: Try scrolling specific scrollable elements
                            scrollable_elements_script = """
                            const scrollableElements = Array.from(document.querySelectorAll('*')).filter(el => {
                                const style = window.getComputedStyle(el);
                                return style.overflowY === 'scroll' || style.overflowY === 'auto' || 
                                       (el.scrollHeight > el.clientHeight && style.overflowY !== 'hidden');
                            });
                            
                            for (const el of scrollableElements) {
                                if (el.scrollHeight > el.clientHeight) {
                                    el.scrollTop += """ + ("400" if direction == "down" else "-400") + """;
                                    break;
                                }
                            }
                            """
                            await page.evaluate(scrollable_elements_script)
                            await page.wait_for_timeout(500)
                        
                    except Exception as e:
                        print(f"Keyboard scroll fallback failed: {e}")
                
                # Final verification
                final_scroll = await page.evaluate("window.pageYOffset")
                scroll_distance = abs(final_scroll - before_scroll)
                
                state['history'].append(f"Step {state['step']}: ✅ Scroll {direction} completed - moved {scroll_distance}px (from {before_scroll} to {final_scroll})")
                print(f"📜 SCROLL DEBUG: {direction} - Before: {before_scroll}, After: {final_scroll}, Distance: {scroll_distance}")
                
            except Exception as scroll_error:
                print(f"❌ All scroll strategies failed: {scroll_error}")
                state['history'].append(f"Step {state['step']}: ❌ Scroll {direction} failed - {str(scroll_error)}")
                raise scroll_error
        elif action_type == "extract":
            items = action.get("items", [])
            for item in items:
                if 'url' in item and isinstance(item.get('url'), str):
                    item['url'] = urljoin(page.url, item['url'])
            state['results'].extend(items)
            push_status(job_id, "partial_result", {"new_items_found": len(items), "total_items": len(state['results'])})
        elif action_type == "dismiss_popup_using_text":
            search_text = action.get("text", "")
            if not search_text: 
                raise ValueError("No text provided for dismiss_popup_using_text action")
            
            print(f"🛡️ ENHANCED POPUP DISMISSAL for text: '{search_text}'")
            
            # Strategy 1: Use the existing popup killer for common patterns
            common_dismiss_texts = [
                "accept", "accept all", "accept cookies", "agree", "ok", "okay", "yes", 
                "close", "dismiss", "no thanks", "not now", "×", "✕", "got it"
            ]
            
            if any(dismiss_text in search_text.lower() for dismiss_text in common_dismiss_texts):
                print("🎯 Using proactive popup killer for common dismiss text")
                try:
                    # Trigger the popup killer specifically for this text
                    popup_killer_trigger = f"""
                    (function() {{
                        const searchText = "{search_text.lower()}";
                        const buttons = document.querySelectorAll('button, a, [role="button"], [onclick], .close, .dismiss');
                        
                        for (const btn of buttons) {{
                            const text = (btn.textContent || btn.innerText || '').toLowerCase().trim();
                            const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                            
                            if (text.includes(searchText) || ariaLabel.includes(searchText)) {{
                                try {{
                                    btn.click();
                                    console.log('🎯 Popup killer clicked:', btn);
                                    return true;
                                }} catch (e) {{
                                    continue;
                                }}
                            }}
                        }}
                        return false;
                    }})();
                    """
                    
                    killed = await page.evaluate(popup_killer_trigger)
                    if killed:
                        state['history'].append(f"Step {state['step']}: ✅ Popup dismissed using popup killer for text '{search_text}'")
                        await page.wait_for_timeout(1000)  # Wait for popup to disappear
                        return state
                        
                except Exception as e:
                    print(f"Popup killer approach failed: {e}")
            
            # Strategy 2: Enhanced element search with popup-specific selectors
            try:
                # First, look for elements using our enhanced search
                elements = await find_elements_with_text_live(page, search_text)
                
                # Filter for popup-like elements and clickable dismiss buttons
                popup_elements = []
                for element in elements:
                    # Prioritize elements that are likely dismiss buttons
                    is_dismiss_button = (
                        element.get('is_clickable', False) and
                        element.get('is_visible', False) and
                        (element.get('tag_name') in ['button', 'a'] or
                         any(selector for selector in element.get('suggested_selectors', []) 
                             if 'close' in selector or 'dismiss' in selector or 'modal' in selector))
                    )
                    
                    if is_dismiss_button:
                        popup_elements.append(element)
                
                # Sort by priority (visible + interactive first)
                popup_elements.sort(key=lambda x: (
                    x.get('is_visible', False),
                    x.get('is_interactive', False),
                    x.get('is_clickable', False)
                ), reverse=True)
                
                if popup_elements:
                    target_element = popup_elements[0]
                    selectors = target_element.get('suggested_selectors', [])
                    
                    # Try each selector until one works
                    for selector in selectors:
                        try:
                            locator = page.locator(selector)
                            if await locator.count() > 0 and await locator.first.is_visible():
                                await locator.first.click(timeout=5000)
                                state['history'].append(f"Step {state['step']}: ✅ Popup dismissed by clicking element with text '{search_text}' using selector '{selector}'")
                                await page.wait_for_timeout(1000)
                                return state
                        except Exception as e:
                            print(f"Selector {selector} failed: {e}")
                            continue
                
            except Exception as e:
                print(f"Enhanced element search failed: {e}")
            
            # Strategy 3: Fallback popup detection and removal
            try:
                print("🔄 Trying fallback popup detection...")
                
                fallback_popup_script = f"""
                (function() {{
                    const searchText = "{search_text}".toLowerCase();
                    let clickedElement = null;
                    
                    // Enhanced popup selectors
                    const popupSelectors = [
                        '[role="dialog"]', '[role="alertdialog"]', '.modal', '.popup', 
                        '.overlay', '.lightbox', '.dialog', '#cookie-banner', '.cookie-banner',
                        '[class*="cookie"]', '#cookieConsent', '.cookie-consent', '[id*="cookie"]',
                        '.overlay-wrapper', '.modal-backdrop', '.popup-overlay', '[class*="overlay"]',
                        '[class*="backdrop"]', '.newsletter-popup', '.subscription-modal',
                        '[class*="newsletter"]', '[aria-modal="true"]', '[data-modal]'
                    ];
                    
                    // Look for popup containers first
                    for (const selector of popupSelectors) {{
                        const popups = document.querySelectorAll(selector);
                        for (const popup of popups) {{
                            if (popup.offsetParent !== null) {{ // visible
                                // Look for dismiss elements within this popup
                                const dismissElements = popup.querySelectorAll(
                                    'button, a, [role="button"], [onclick], .close, .dismiss, ' +
                                    '[aria-label*="close"], [aria-label*="dismiss"], ' +
                                    '[class*="close"], [class*="dismiss"], [data-dismiss]'
                                );
                                
                                for (const element of dismissElements) {{
                                    const text = (element.textContent || element.innerText || '').toLowerCase().trim();
                                    const ariaLabel = (element.getAttribute('aria-label') || '').toLowerCase();
                                    const className = (element.className || '').toLowerCase();
                                    
                                    if (text.includes(searchText) || ariaLabel.includes(searchText) || 
                                        className.includes(searchText.replace(' ', ''))) {{
                                        try {{
                                            element.click();
                                            clickedElement = element;
                                            console.log('🎯 Fallback popup dismiss clicked:', element);
                                            return element.tagName + '.' + element.className + '#' + element.id;
                                        }} catch (e) {{
                                            continue;
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                    
                    // If no popup container found, search entire document
                    const allElements = document.querySelectorAll('*');
                    for (const element of allElements) {{
                        const style = window.getComputedStyle(element);
                        const text = (element.textContent || element.innerText || '').toLowerCase().trim();
                        const ariaLabel = (element.getAttribute('aria-label') || '').toLowerCase();
                        
                        // Check if element contains our search text and looks like a dismiss button
                        if ((text.includes(searchText) || ariaLabel.includes(searchText)) &&
                            (element.tagName === 'BUTTON' || element.tagName === 'A' || 
                             element.getAttribute('role') === 'button' || element.onclick ||
                             style.cursor === 'pointer') &&
                            style.display !== 'none' && style.visibility !== 'hidden' &&
                            element.offsetParent !== null) {{
                            
                            try {{
                                element.click();
                                clickedElement = element;
                                console.log('🎯 Global search dismiss clicked:', element);
                                return element.tagName + '.' + element.className + '#' + element.id;
                            }} catch (e) {{
                                continue;
                            }}
                        }}
                    }}
                    
                    return null;
                }})();
                """
                
                clicked_info = await page.evaluate(fallback_popup_script)
                
                if clicked_info:
                    state['history'].append(f"Step {state['step']}: ✅ Popup dismissed using fallback detection for text '{search_text}' - clicked {clicked_info}")
                    await page.wait_for_timeout(1000)
                    return state
                
            except Exception as e:
                print(f"Fallback popup script failed: {e}")
            
            # Strategy 4: Force dismiss common popup patterns
            try:
                print("🔄 Trying force dismiss of common popup patterns...")
                
                force_dismiss_script = """
                (function() {
                    let dismissed = false;
                    
                    // Common popup dismiss patterns
                    const dismissPatterns = [
                        'button[aria-label*="close"]',
                        'button[aria-label*="dismiss"]', 
                        '.modal-close',
                        '.popup-close',
                        '.overlay-close',
                        '[data-dismiss="modal"]',
                        '[data-dismiss="popup"]',
                        'button.close',
                        '.close-btn',
                        '.close-button',
                        '[role="dialog"] button:last-child',
                        '[aria-modal="true"] button:first-child'
                    ];
                    
                    for (const pattern of dismissPatterns) {
                        try {
                            const elements = document.querySelectorAll(pattern);
                            for (const el of elements) {
                                if (el.offsetParent !== null) {
                                    el.click();
                                    dismissed = true;
                                    console.log('🎯 Force dismissed using pattern:', pattern);
                                    return pattern;
                                }
                            }
                        } catch (e) {
                            continue;
                        }
                    }
                    
                    return dismissed;
                })();
                """
                
                force_result = await page.evaluate(force_dismiss_script)
                
                if force_result:
                    state['history'].append(f"Step {state['step']}: ✅ Popup force dismissed using pattern '{force_result}'")
                    await page.wait_for_timeout(1000)
                    return state
                    
            except Exception as e:
                print(f"Force dismiss failed: {e}")
            
            # If all strategies failed
            raise ValueError(f"Could not find or dismiss popup with text '{search_text}' using any strategy")
        
        elif action_type == "solve_captcha":
            # 🤖 UNIVERSAL CAPTCHA SOLVER INTEGRATION
            # This action automatically detects and solves any CAPTCHA type present on the page:
            # - Cloudflare Turnstile (0x, 3x sitekeys)
            # - reCAPTCHA v2/v3 (6L sitekeys) 
            # - hCAPTCHA (all variants)
            # - Image CAPTCHAs and custom implementations
            print(f"🤖 CAPTCHA SOLVER: Scanning page for CAPTCHA challenges...")
            
            try:
                # Call the universal CAPTCHA solver - it handles everything automatically
                captcha_result = await captcha_solver.solve_captcha_if_present(page, page.url)
                
                # Process the results and update agent state
                if captcha_result['found']:
                    if captcha_result['solved']:
                        # SUCCESS: CAPTCHA was found and solved successfully
                        success_msg = f"✅ CAPTCHA SOLVED: {captcha_result['type'].upper()} solved using {captcha_result['service']} service"
                        state['history'].append(f"Step {state['step']}: {success_msg}")
                        print(success_msg)
                        
                        # Optional: Add brief wait for page to process the solution
                        await page.wait_for_timeout(2000)
                        
                    else:
                        # CAPTCHA found but solving failed
                        error_msg = f"❌ CAPTCHA SOLVING FAILED: {captcha_result['type'].upper()} - {captcha_result.get('error', 'Unknown error')}"
                        state['history'].append(f"Step {state['step']}: {error_msg}")
                        print(error_msg)
                        
                        # Don't raise exception - let agent continue with other strategies
                        
                else:
                    # No CAPTCHA detected on the page
                    no_captcha_msg = "ℹ️ NO CAPTCHA DETECTED: Page scan complete - no CAPTCHA challenges found"
                    state['history'].append(f"Step {state['step']}: {no_captcha_msg}")
                    print(no_captcha_msg)
                
            except Exception as captcha_error:
                # Handle any unexpected errors in CAPTCHA processing
                error_msg = f"❌ CAPTCHA SOLVER ERROR: Unexpected error occurred - {str(captcha_error)}"
                state['history'].append(f"Step {state['step']}: {error_msg}")
                print(error_msg)
                
                # Log the full error for debugging
                import traceback
                print(f"🔍 CAPTCHA ERROR DETAILS:")
                print(traceback.format_exc())
                
                # Don't raise exception - allow agent to continue
        
        elif action_type == "extract_correct_selector_using_text":
            search_text = action.get("text", "")
            if not search_text:
                state['history'].append(f"Step {state['step']}: ❌ FAILED - No text provided for element search")
                raise ValueError("No text provided for extract_correct_selector_using_text action")
            
            # Enhanced validation: Check if text is reasonable for screenshot extraction
            if len(search_text) > 100:
                state['history'].append(f"Step {state['step']}: ⚠️ WARNING - Text too long for screenshot extraction: '{search_text[:50]}...'")
                state['history'].append("💡 TIP: Use shorter, specific text visible in screenshot (e.g., 'Search', 'Login', 'Submit')")
            
            # Check for suspicious patterns that suggest non-screenshot text
            suspicious_patterns = ['<', '>', '{', '}', 'class=', 'id=', 'xpath', 'css', 'selector']
            if any(pattern in search_text.lower() for pattern in suspicious_patterns):
                state['history'].append(f"Step {state['step']}: 🚫 BLOCKED - Text appears to be HTML/CSS code, not screenshot text: '{search_text}'")
                raise ValueError(f"Invalid text for screenshot extraction: '{search_text}' appears to be code, not visible text from screenshot")
            
            # Enhanced loop prevention - allow re-extraction only if all previous selectors were tested
            recent_extracts = state.get('recent_extracts', [])
            if search_text in recent_extracts[-3:]:  # Check last 3 extracts
                # Check if we have completed testing all selectors from previous extraction
                found_context = state.get('found_element_context', {})
                if (found_context.get('text') == search_text and 
                    found_context.get('testing_required', False) and
                    found_context.get('untested_selectors', [])):
                    
                    untested_count = len(found_context.get('untested_selectors', [])) - found_context.get('current_test_index', 0)
                    state['history'].append(f"Step {state['step']}: 🚫 MANDATORY TESTING INCOMPLETE - You have {untested_count} untested selectors for '{search_text}'. Complete testing first!")
                    push_status(job_id, "testing_incomplete", {"untested_selectors": untested_count, "text": search_text})
                    raise ValueError(f"TESTING INCOMPLETE: You must test the remaining {untested_count} selectors for '{search_text}' before extracting again!")
                else:
                    state['history'].append(f"Step {state['step']}: 🚫 LOOP PREVENTION - Already extracted '{search_text}' recently. Use existing selectors!")
                    push_status(job_id, "loop_prevented", {"reason": f"Repeated extract for '{search_text}'"})
                    raise ValueError(f"Loop detected: Already extracted selectors for '{search_text}' in recent steps. Use existing selectors instead of extracting again!")
            
            # Use live search to find elements in the current DOM (including dynamic content)
            result = await find_elements_with_text_live(page, search_text)

            if result:
                # Store only the TOP 10 matched elements context for the agent to use
                all_elements_context = []
                all_selectors = []
                
                # Limit to first 10 results only
                limited_result = result[:10]
                
                for i, match in enumerate(limited_result):
                    suggested_selectors = match.get('suggested_selectors', [])
                    tag_name = match.get('tag_name', 'unknown')
                    is_visible = match.get('is_visible', False)
                    is_interactive = match.get('is_interactive', False)
                    is_clickable = match.get('is_clickable', False)
                    
                    # Collect selectors from the 10 matches only
                    all_selectors.extend(suggested_selectors)
                    
                    # Create simplified context for each element
                    element_context = {
                        "index": i + 1,
                        "tag_name": tag_name,
                        "suggested_selectors": suggested_selectors,
                        "is_visible": is_visible,
                        "is_interactive": is_interactive,
                        "is_clickable": is_clickable
                    }
                    all_elements_context.append(element_context)
                
                # Enhanced selector validation and management system
                validated_selectors = await validate_selectors_systematically(page, all_elements_context, search_text, state)
                best_selector = validated_selectors.get('best_selector')
                best_element = validated_selectors.get('best_element')
                working_selectors = validated_selectors.get('working_selectors', [])
                failed_selectors = validated_selectors.get('failed_selectors', [])
                
                # Update selector attempts tracking
                selector_attempts = state.get('selector_attempts', {})
                if search_text not in selector_attempts:
                    selector_attempts[search_text] = []
                
                # Add all tested selectors to attempts tracking
                for selector in failed_selectors + working_selectors:
                    if selector not in selector_attempts[search_text]:
                        selector_attempts[search_text].append(selector)
                
                state['selector_attempts'] = selector_attempts
                
                # Update successful selectors tracking
                successful_selectors = state.get('successful_selectors', {})
                if working_selectors:
                    successful_selectors[search_text] = working_selectors[0]  # Store the best working selector
                    state['successful_selectors'] = successful_selectors
                
                # Track search flow detection
                if any(search_term in search_text.lower() for search_term in ["search", "input", "query", "find"]):
                    search_flow = state.get('search_flow_state', {})
                    search_flow['detected'] = True
                    state['search_flow_state'] = search_flow
                
                print(f"🎯 ENHANCED SELECTOR VALIDATION for '{search_text}':")
                print(f"   Working selectors: {len(working_selectors)}")
                print(f"   Failed selectors: {len(failed_selectors)}")
                print(f"   Best selector: {best_selector}")
                print(f"   Total tested: {len(working_selectors) + len(failed_selectors)}")
                
                # Create the complete list of selectors to test (prioritize working ones, then all others)
                all_unique_selectors = []
                seen_selectors = set()
                
                # First add working selectors (most likely to succeed)
                for selector in working_selectors:
                    if selector not in seen_selectors:
                        all_unique_selectors.append(selector)
                        seen_selectors.add(selector)
                
                # Then add all other selectors from the extraction
                for selector in all_selectors:
                    if selector not in seen_selectors:
                        all_unique_selectors.append(selector)
                        seen_selectors.add(selector)
                
                # Limit to top 15 selectors for efficiency but ensure comprehensive testing
                selectors_to_test = all_unique_selectors[:15]
                
                # Store COMPLETE selector context for MANDATORY exhaustive testing
                state['found_element_context'] = {
                    "text": search_text,
                    "total_matches": len(limited_result),
                    "all_elements": all_elements_context,
                    "all_suggested_selectors": all_selectors,
                    "summary": f"Found {len(limited_result)} elements containing '{search_text}'",
                    "best_selector": best_selector,
                    "best_element": best_element,
                    "action_suggestion": f"MANDATORY: Test all {len(selectors_to_test)} selectors systematically",
                    # Mandatory testing protocol
                    "untested_selectors": selectors_to_test,
                    "current_test_index": 0,
                    "testing_required": True,
                    "is_search_related": any(term in search_text.lower() for term in ["search", "input", "query", "find"]),
                    "total_working_selectors": len(working_selectors),
                    "total_failed_selectors": len(failed_selectors)
                }
                
                print(f"🎯 MANDATORY TESTING SETUP:")
                print(f"   Text: '{search_text}'")
                print(f"   Total selectors to test: {len(selectors_to_test)}")
                print(f"   Working selectors found: {len(working_selectors)}")
                print(f"   Failed selectors found: {len(failed_selectors)}")
                print(f"   First selector to test: '{selectors_to_test[0] if selectors_to_test else 'None'}'")
                print(f"   Is search related: {any(term in search_text.lower() for term in ['search', 'input', 'query', 'find'])}")
                
                # Create comprehensive history entry showing MANDATORY testing requirements
                history_entry = f"Step {state['step']}: ✅ EXTRACTION COMPLETE - MANDATORY TESTING REQUIRED!"
                history_entry += f"\n📋 Extracted for text: '{search_text}'"
                history_entry += f"\n📊 Found {len(limited_result)} elements with {len(selectors_to_test)} selectors to test"
                history_entry += f"\n\n🚨 MANDATORY SELECTOR TESTING PROTOCOL ACTIVATED 🚨"
                history_entry += f"\nYou MUST test these {len(selectors_to_test)} selectors in order:"
                
                for i, selector in enumerate(selectors_to_test):
                    status = "✅ Working" if selector in working_selectors else "❓ Untested"
                    history_entry += f"\n  {i+1:2d}. {selector} ({status})"
                
                history_entry += f"\n\n🎯 NEXT ACTION REQUIRED:"
                history_entry += f"\n{{\"type\": \"click\", \"selector\": \"{selectors_to_test[0]}\"}}"
                history_entry += f"\n\n⚠️  RULES:"
                history_entry += f"\n• Test selectors 1-{len(selectors_to_test)} in exact order"
                history_entry += f"\n• Use ONLY the selectors listed above"
                history_entry += f"\n• Do NOT create your own selectors"
                history_entry += f"\n• Do NOT skip any selector"
                history_entry += f"\n• Do NOT scroll or navigate until ALL tested"
                
                if any(term in search_text.lower() for term in ["search", "input", "query", "find"]):
                    history_entry += f"\n\n🔍 SEARCH PRIORITY: This is search functionality - test ALL selectors!"
                
                state['history'].append(history_entry)

                # NEW: Add to recent extracts list to prevent loops
                recent_extracts = state.get('recent_extracts', [])
                recent_extracts.append(search_text)
                # Keep only last 5 extracts to prevent memory bloat
                state['recent_extracts'] = recent_extracts[-5:]

                # Simplified debug output
                visible_count = sum(1 for elem in all_elements_context if elem.get('is_visible', False))
                interactive_count = sum(1 for elem in all_elements_context if elem.get('is_interactive', False))
                
                print(f"🔍 LIVE ELEMENT SEARCH DEBUG:")
                print(f"   Search Text: '{search_text}'")
                print(f"   Total Matches (Limited to 10): {len(limited_result)}")
                print(f"   Visible Elements: {visible_count}")
                print(f"   Interactive Elements: {interactive_count}")
                print(f"   Total Selectors: {len(all_selectors)}")
                print(f"   Elements:")
                for i, elem in enumerate(all_elements_context):
                    visibility_icon = "👁️" if elem.get('is_visible') else "👻"
                    interactive_icon = "🖱️" if elem.get('is_interactive') else "📄"
                    
                    print(f"     {visibility_icon}{interactive_icon} {i+1}. {elem['tag_name']}")
                    print(f"        Selectors: {elem['suggested_selectors'][:2]}")
                print(f"   🤖 Agent Context: {len(all_elements_context)} elements with their selectors")
            else:
                # No elements found
                state['history'].append(f"Step {state['step']}: ❌ NO ELEMENTS FOUND! Text: '{search_text}' - No elements contain this text in their attributes")
                print(f"🔍 ELEMENT SEARCH DEBUG: No elements found containing '{search_text}'")

        elif action_type == "request_user_input":
            # NEW: Human-in-the-loop implementation
            input_type = action.get("input_type", "text")
            prompt = action.get("prompt", "Please provide input")
            is_sensitive = action.get("is_sensitive", False)
            
            # Create user input request
            user_input_request = {
                "input_type": input_type,
                "prompt": prompt,
                "is_sensitive": is_sensitive,
                "timestamp": get_current_timestamp(),
                "step": state['step']
            }
            
            # Store the request globally for API access
            USER_INPUT_REQUESTS[job_id] = user_input_request
            state['user_input_request'] = user_input_request
            state['waiting_for_user_input'] = True
            state['user_input_flow_active'] = True  # Mark that we're in a user input flow
            JOBS_IN_INPUT_FLOW.add(job_id)  # Global protection against field clearing
            
            # Create an event to wait for user input
            input_event = asyncio.Event()
            PENDING_JOBS[job_id] = input_event
            
            # Notify the user through status
            push_status(job_id, "user_input_required", {
                "input_type": input_type,
                "prompt": prompt,
                "is_sensitive": is_sensitive,
                "message": f"Agent needs user input: {prompt}"
            })
            
            state['history'].append(f"Step {state['step']}: 🔄 WAITING FOR USER INPUT - {prompt}")
            
            # Wait for user input with timeout
            try:
                await asyncio.wait_for(input_event.wait(), timeout=300)  # 5 minute timeout
                
                # Get the user's response
                user_response = USER_INPUT_RESPONSES.get(job_id, "")
                state['user_input_response'] = user_response
                state['waiting_for_user_input'] = False
                # Keep user_input_flow_active=True until the input is actually used
                
                # Clear the request from memory
                USER_INPUT_REQUESTS.pop(job_id, None)
                USER_INPUT_RESPONSES.pop(job_id, None)
                PENDING_JOBS.pop(job_id, None)
                
                state['history'].append(f"Step {state['step']}: ✅ USER INPUT RECEIVED - {input_type} provided, ready for next action")
                push_status(job_id, "user_input_received", {"input_type": input_type})
                
            except asyncio.TimeoutError:
                # Handle timeout
                state['waiting_for_user_input'] = False
                state['user_input_flow_active'] = False  # Reset flow on timeout
                JOBS_IN_INPUT_FLOW.discard(job_id)  # Remove from global protection
                USER_INPUT_REQUESTS.pop(job_id, None)
                PENDING_JOBS.pop(job_id, None)
                
                state['history'].append(f"Step {state['step']}: ⏰ USER INPUT TIMEOUT - Continuing without input")
                push_status(job_id, "user_input_timeout", {"message": "User input request timed out after 5 minutes"})
                raise ValueError(f"User input request timed out after 5 minutes: {prompt}")

        elif action_type == "close_popup":
            soup = BeautifulSoup(await page.content(), 'html.parser')

            elements = soup.find_all(class_=re.compile(r'overlay'))
            for el in elements:
                classname = el.get('class')
                print(classname)
                if classname:
                    for cls in classname:
                        try:
                            await page.evaluate(f"document.querySelector('.{cls}')?.click()")
                        except Exception as e:
                            print(f"Failed to click on .{cls}: {e}")

            await asyncio.sleep(5)

            inputs = await page.query_selector_all('input')

            for inp in inputs:
                try:
                    if inp.is_enabled() and inp.is_visible():
                        await inp.fill("", timeout=0, force=True)
                except Exception as e:
                    print(f"Failed to clear input fields: {e}")

            raise ValueError(f"No parent element found for text '{action.get('text')}'.")
        else:
            raise ValueError(f"No element found with text '{action.get('text')}'.")
        # Add action verification for all successful actions
        verification_result = await verify_action_from_screenshot(page, action, state)
        
        # Update state URL if it changed
        current_url = page.url
        if current_url != state.get('url', ''):
            state['url'] = current_url
        
        # Enhance history with verification details
        success_indicator = "✅" if verification_result.get('success') or verification_result.get('changes_detected') else "⚠️"
        verification_notes = "; ".join(verification_result.get('verification_notes', []))
        verification_summary = f" | Verification: {verification_notes}" if verification_notes else ""
        
        await page.wait_for_timeout(2000)
        state['history'].append(f"Step {state['step']}: {success_indicator} Executed `{action_signature}` successfully.{verification_summary}")

    except Exception as e:
        error_message = str(e).splitlines()[0]
        push_status(job_id, "action_failed", {"action": action, "error": error_message, "signature": action_signature})
        state['history'].append(f"Step {state['step']}: ❌ FAILED `{action_signature}` error='{error_message}' (will avoid repeating).")
        # Record failure
        state['failed_actions'][action_signature] = state['failed_actions'].get(action_signature, 0) + 1
        
    # NEW: Check for login failures after actions that might be login-related
    page = state['page']
    if action.get("type") in ["click", "press"] and any(keyword in action_signature.lower() for keyword in ["login", "submit", "sign", "enter"]):
        try:
            # Wait a moment for the page to respond
            await page.wait_for_timeout(2000)
            page_content = await page.content()
            page_url = page.url
            
            if detect_login_failure(page_content, page_url):
                print(f"🚫 LOGIN FAILURE DETECTED - Job {job_id}")
                print(f"   URL: {page_url}")
                
                # Add failure info to history for LLM context
                state['history'].append(f"Step {state['step']}: 🚫 LOGIN FAILURE DETECTED - The login attempt appears to have failed. The page still shows login form or error messages.")
                
                # Mark that we should request new credentials
                failure_context = {
                    "login_failed": True,
                    "failure_url": page_url,
                    "step": state['step']
                }
                push_status(job_id, "login_failure_detected", failure_context)
        except Exception as e:
            print(f"Error checking for login failure: {e}")
        
    state['step'] += 1
    state['history'] = state['history']
    return state

# --- LangGraph Supervisor Logic ---
def supervisor_node(state: AgentState) -> str:
    # Only finish if explicitly requested AND we have results OR we've truly exhausted all options
    if state['last_action'].get("type") == "finish":
        finish_reason = state['last_action'].get("reason", "")
        
        # Never finish on parsing failures - keep going
        if any(term in finish_reason.lower() for term in ["parsing failed", "json", "error:", "failed to parse"]):
            print("🚫 Preventing finish due to parsing/technical error - continuing mission")
            state['history'].append(f"Step {state['step']}: 🔄 SUPERVISOR OVERRIDE - Preventing premature finish due to technical error. Mission continues!")
            return "continue"
        
        # Only allow finish if we have results or truly completed the objective
        if len(state['results']) > 0:
            push_status(state['job_id'], "agent_finished", {"reason": f"Successfully collected {len(state['results'])} items. {finish_reason}"})
            return END
        elif "completed" in finish_reason.lower() or "success" in finish_reason.lower():
            push_status(state['job_id'], "agent_finished", {"reason": finish_reason})
            return END
        
        # Don't finish without results unless we've really exhausted options
        if state['step'] >= state['max_steps']:
            push_status(state['job_id'], "agent_stopped", {"reason": f"Reached max steps ({state['max_steps']}) with incomplete results."})
            return END
        
        print(f"🔄 Preventing premature finish at step {state['step']} - no results yet. Mission continues!")
        state['history'].append(f"Step {state['step']}: 🔄 SUPERVISOR OVERRIDE - Cannot finish without results. Keep trying!")
        return "continue"
    
    # Finish if we have enough results
    if len(state['results']) >= state['top_k']:
        push_status(state['job_id'], "agent_finished", {"reason": f"Successfully collected {len(state['results'])}/{state['top_k']} items."})
        return END
    
    # Only finish at max steps as absolute last resort
    if state['step'] > state['max_steps']:
        push_status(state['job_id'], "agent_stopped", {"reason": f"Max steps ({state['max_steps']}) reached. Collected {len(state['results'])} items."})
        return END
    
    # NEW: Handle human-in-the-loop scenario
    if state.get('waiting_for_user_input', False):
        # This shouldn't happen as we handle input in execute_action_node
        # But if it does, continue reasoning to process the received input
        return "continue"
    return "continue"

# --- Build the Graph ---
builder = StateGraph(AgentState)
builder.add_node("navigate", navigate_to_page)
builder.add_node("reason", agent_reasoning_node)
builder.add_node("execute", execute_action_node)
builder.set_entry_point("navigate")
builder.add_edge("navigate", "reason")
builder.add_conditional_edges("execute", supervisor_node, {END: END, "continue": "reason"})
builder.add_edge("reason", "execute")
graph_app = builder.compile()

# --- The Core Job Orchestrator ---
# async def run_job(job_id: str, payload: dict, device_id: str = "ZD222GXYPV", ):
async def run_job(job_id: str, payload: dict, device_id: str = "emulator-5554", ):

    device_id = payload.get("device_id", device_id)
    url = payload.get('query', '')
    incognito = True

    # Check if we're using ngrok URL
    is_ngrok = device_id.startswith("https://") or device_id.startswith("http://")
    
    if is_ngrok:
        # For ngrok connections, skip all Android device setup
        print(f"Using ngrok connection: {device_id}")
        
        # Construct proper CDP endpoint
        if not device_id.endswith('/'):
            device_id += '/'
        cdp_endpoint = f"{device_id}devtools/browser"
        
        print(f"CDP endpoint: {cdp_endpoint}")
        
    else:
        # Original Android device setup for local devices
        print(f"Using local Android device: {device_id}")
        
        # Setup
        port = get_devtools_port(device_id)
        
        # Launch Chrome
        force_stop_chrome(device_id)
        await asyncio.sleep(2)
        
        if incognito:
            start_chrome_incognito(device_id)
        else:
            start_chrome_normal(device_id)
        
        await asyncio.sleep(3)
        forward_port(device_id, port)
        await asyncio.sleep(2)
        
        # Wait for DevTools
        if not await wait_for_devtools(port):
            print(f"[{device_id}] Error: DevTools not available on port {port}")
            push_status(job_id, "job_failed", {"error": "DevTools not available"})
            JOB_RESULTS[job_id] = {"status": "failed", "error": "DevTools not available"}
            return
        
        cdp_endpoint = f"http://localhost:{port}"
    
    
    result = {
        "status": "success", 
        "url": url, 
        "device_id": device_id,
        "incognito": incognito,
        "data": {}
    }

    provider = payload["llm_provider"]
    job_analysis = {
        "job_id": job_id,
        "timestamp": get_current_timestamp(),
        "provider": provider,
        "model": MODEL_MAPPING.get(provider, "unknown"),
        "query": payload["query"],
        "url": payload["url"],
        "steps": []
    }

    ngrok_base_url = "https://726c88b92d78.ngrok.app"

    # Step 1: Get actual debugger URL
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{ngrok_base_url}/json/version") as resp:
            data = await resp.json()
            websocket_path = data["webSocketDebuggerUrl"].split("/devtools/")[1]
            cdp_endpoint = f"wss://726c88b92d78.ngrok.app/devtools/{websocket_path}"
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        print("hello")
        final_result = {}
        final_state = {}
        try:
            push_status(job_id, "job_started", {"provider": provider, "query": payload["query"]})
            
            # MODIFIED: Capture entoken usage from prompt refinement
            refined_query, usage = get_refined_prompt(payload["url"], payload["query"], provider)
            job_analysis["steps"].append({"task": "refine_prompt", **usage})
            push_status(job_id, "prompt_refined", {"refined_query": refined_query, "usage": usage})

            initial_state = AgentState(
                job_id=job_id, browser=browser, page=page, query=payload["url"],
                url=payload["url"],  # Initialize with target URL
                top_k=payload["top_k"], provider=provider,
                refined_query=refined_query, results=[], screenshots=[],
                job_artifacts_dir=SCREENSHOTS_DIR / job_id,
                step=1, max_steps=100, last_action={},
                history=[],
                token_usage=[], # Initialize empty token usage list
                found_element_context={}, # Initialize empty element context
                failed_actions={}, # track failed action signatures
                attempted_action_signatures=[], # chronological list
                # Enhanced memory system initialization
                recent_extracts=[], # track recent extract calls to prevent loops
                selector_attempts={}, # track selector attempts per text
                successful_selectors={}, # track working selectors
                action_verification=[], # verification results from screenshots
                screenshot_analysis={}, # current screenshot analysis
                element_interaction_log=[], # detailed interaction history with outcomes
                # Search flow tracking
                search_flow_state={}, # track search flow progress
                # Human-in-the-loop state
                waiting_for_user_input=False,
                user_input_request={},
                user_input_response="",
                user_input_flow_active=False
            )
            initial_state['job_artifacts_dir'].mkdir(exist_ok=True)
            
            # graph_app.get_graph().draw_png()
            final_state = await graph_app.ainvoke(initial_state, {"recursion_limit": 200})

            final_result = {"job_id": job_id, "results": final_state['results'], "screenshots": final_state['screenshots']}
        except Exception as e:
            push_status(job_id, "job_failed", {"error": str(e), "trace": traceback.format_exc()})
            final_result["error"] = str(e)
        finally:
            JOB_RESULTS[job_id] = final_result
            push_status(job_id, "job_done")
            
            # NEW: Aggregate and save analysis report
            if final_state:
                job_analysis["steps"].extend(final_state.get('token_usage', []))
            save_analysis_report(job_analysis)
            
            await page.close()
            await browser.close()

# --- FastAPI Endpoints ---
@app.post("/search")
async def start_search(req: SearchRequest):
    job_id = str(uuid.uuid4())
    JOB_QUEUES[job_id] = asyncio.Queue()
    # loop = asyncio.get_event_loop()
    # loop.run_in_executor(None, run_job, job_id, req.dict())
    # asyncio.create_task(run_job(job_id, {**req.model_dump(), "device_id": "emulator-5554"}))
    # asyncio.create_task(run_job(job_id, {**req.model_dump(), "device_id": "https://2b93471dc9cf.ngrok-free.app"}))
    asyncio.create_task(run_job(job_id, {**req.model_dump(), "device_id": "https://726c88b92d78.ngrok.app"}))
    return {"job_id": job_id, "stream_url": f"/stream/{job_id}", "result_url": f"/result/{job_id}"}

@app.get("/stream/{job_id}")
async def stream_status(job_id: str):
    q = JOB_QUEUES.get(job_id)
    if not q: raise HTTPException(status_code=404, detail="Job not found")
    async def event_generator():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=60)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["msg"] in ("job_done", "job_failed"): break
            except asyncio.TimeoutError: yield ": keep-alive\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/result/{job_id}")
async def get_result(job_id: str):
    result = JOB_RESULTS.get(job_id)
    if not result: return JSONResponse({"status": "pending"}, status_code=202)
    return JSONResponse(result)

@app.get("/screenshots/{job_id}/{filename}")
async def get_screenshot(job_id: str, filename: str):
    file_path = SCREENSHOTS_DIR / job_id / filename
    if not file_path.exists(): raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(file_path)

# NEW: Human-in-the-loop API endpoints
@app.get("/user-input-request/{job_id}")
async def get_user_input_request(job_id: str):
    """Get pending user input request for a job"""
    if job_id not in USER_INPUT_REQUESTS:
        raise HTTPException(status_code=404, detail="No pending user input request for this job")
    
    return {"job_id": job_id, **USER_INPUT_REQUESTS[job_id]}

@app.post("/user-input-response")
async def submit_user_input(response: UserInputResponse):
    """Submit user input response to resume job execution"""
    job_id = response.job_id
    
    if job_id not in USER_INPUT_REQUESTS:
        raise HTTPException(status_code=404, detail="No pending user input request for this job")
    
    if job_id not in PENDING_JOBS:
        raise HTTPException(status_code=400, detail="Job is not waiting for user input")
    
    # Store the user's response
    USER_INPUT_RESPONSES[job_id] = response.input_value
    
    # Signal the waiting job to continue
    event = PENDING_JOBS[job_id]
    event.set()
    
    return {"status": "success", "message": "User input received, job will resume"}

@app.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str):
    """Get comprehensive job status including user input requirements"""
    status = {
        "job_id": job_id,
        "has_result": job_id in JOB_RESULTS,
        "waiting_for_input": job_id in USER_INPUT_REQUESTS,
        "is_running": job_id in JOB_QUEUES
    }
    
    if job_id in USER_INPUT_REQUESTS:
        status["input_request"] = USER_INPUT_REQUESTS[job_id]
    
    if job_id in JOB_RESULTS:
        status["result"] = JOB_RESULTS[job_id]
    
    return status

@app.post("/admin/cleanup-stuck-jobs")
async def cleanup_stuck_jobs_endpoint():
    """Clean up jobs that are stuck waiting for user input (admin endpoint)"""
    cleaned_count = cleanup_stuck_jobs()
    return {
        "status": "success",
        "message": f"Cleaned up {cleaned_count} stuck job(s)",
        "cleaned_jobs": cleaned_count
    }

@app.get("/admin/system-status")
async def get_system_status():
    """Get overall system status including pending jobs and input requests"""
    return {
        "active_jobs": len(JOB_QUEUES),
        "completed_jobs": len(JOB_RESULTS),
        "pending_input_requests": len(USER_INPUT_REQUESTS),
        "pending_responses": len(USER_INPUT_RESPONSES),
        "jobs_in_input_flow": len(JOBS_IN_INPUT_FLOW),
        "input_flow_jobs": list(JOBS_IN_INPUT_FLOW),
        "stuck_jobs_cleaned": cleanup_stuck_jobs()
    }

@app.get("/")
async def client_ui():
    return FileResponse(Path(__file__).parent / "static/test_client.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)