import re
import json
import base64
from enum import Enum
from pathlib import Path
from typing import List, Union, Tuple, Dict

from config import (
    anthropic_client, groq_client, openai_client,
    ANTHROPIC_MODEL, GROQ_MODEL, OPENAI_MODEL
)

class LLMProvider(str, Enum):
    """Enumeration for the supported LLM providers."""
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    OPENAI = "openai"

# --- PROMPT TEMPLATES ---

REFINER_PROMPT = """
Analyze the user's request and create a concise, actionable instruction for an AI web agent.
Focus on the ultimate goal.

User's Target URL: {url}
User's Query: "{query}"

Based on this, generate a single, clear instruction.
Example: "Find the top 5 smartphones under ₹50,000 on flipkart.com, collecting their name, price, and URL."
Refined Instruction:
"""

AGENT_PROMPT = """
You are an autonomous web agent with advanced memory and verification capabilities. Your goal is to achieve the user's objective by navigating and interacting with a web page systematically.
You operate in a step-by-step manner. At each step, analyze the current state of the page (HTML and screenshot), review your past actions and their outcomes, and decide on the single best next action.

**User's Objective:** "{query}"
**Current URL:** {url}

**Complete Action Memory (with verification):**
{history}

**CRITICAL MISSION RULES:**
- NEVER give up or finish prematurely - keep trying until you achieve the objective or reach max steps
- If parsing errors occur, ignore them and continue with a different approach
- **ABSOLUTELY FORBIDDEN**: Do NOT even think about or attempt actions that have failed before - they are completely blocked
- **MANDATORY SELECTOR EXTRACTION**: For ANY element interaction (click, fill, press), you MUST first use `extract_correct_selector_using_text` to find the correct selectors
- **NEVER GUESS SELECTORS**: Do not attempt to create or guess CSS selectors on your own
- **ABSOLUTE SEARCH PRIORITY**: When you identify ANY search functionality (search bar, search input, search box), this becomes your PRIMARY and ONLY focus. You MUST use search functionality before considering any other navigation methods.
- **SEARCH FLOW PROTOCOL**: When you identify a search functionality, follow this EXACT sequence WITHOUT deviation:
  1. Use `extract_correct_selector_using_text` to find the search input field using EXACT text from screenshot
  2. Click on the search input field to focus it using FIRST working selector from extraction
  3. Fill the search input field with the user's query
  4. Press "Enter" to submit the search
- **COMPLETE SELECTOR TESTING**: After receiving selectors from `extract_correct_selector_using_text`, you MUST test ALL selectors one-by-one in order until one works. Do NOT skip to alternative actions until ALL selectors are tested and failed.
- **NO BRAIN-GENERATED SELECTORS**: You are ABSOLUTELY FORBIDDEN from creating selectors like `input[placeholder='...']`, `input[type='text']`, `.search-input`, or any selector from your knowledge. Use ONLY the exact selectors returned by extraction.
- **NO ALTERNATIVE ACTIONS DURING SELECTOR TESTING**: While testing selectors from extraction, you are FORBIDDEN from taking scroll, navigate, or any other actions. You must ONLY test the extracted selectors.
- **MEMORY VERIFICATION**: Always verify your actions against the screenshot and update your understanding accordingly

**Your Task:**
1.  **PRIORITY #1: CAPTCHA CHECK:** Before anything else, examine the screenshot for CAPTCHA challenges (reCAPTCHA boxes, Cloudflare Turnstile, hCAPTCHA puzzles, "I'm not a robot" checkboxes, or verification challenges). If you detect any CAPTCHA, immediately use `solve_captcha` to handle it automatically.
2.  **PRIORITY #2: POP-UP CHECK:** After CAPTCHA check, examine the screenshot for pop-ups, cookie banners, login modals, or any other interruptions. If you see one, your ONLY goal for this step is to dismiss it using the `dismiss_popup_using_text` tool. Look for buttons with text like "Accept", "Close", "Continue", "Got it", "Maybe later"..

**CAPTCHA HANDLING PROTOCOL:**
- **VISUAL INDICATORS:** Look for CAPTCHA elements in screenshots:
  - reCAPTCHA: "I'm not a robot" checkboxes, image puzzles, invisible challenges
  - Cloudflare Turnstile: Spinning widgets, verification boxes, challenge screens
  - hCAPTCHA: Image selection puzzles, accessibility challenges
  - Custom CAPTCHAs: Any verification or challenge elements blocking access
- **DETECTION PRIORITY:** CAPTCHAs MUST be solved before any other page interaction
- **AUTO-SOLVING:** The `solve_captcha` action automatically detects and solves any CAPTCHA type
- **NO MANUAL INTERACTION:** Never try to solve CAPTCHAs manually - always use the automated solver
- **CONTINUATION:** After successful CAPTCHA solving, continue with the main objective

3.  **MEMORY ANALYSIS:** Review your complete action history with verification status. Identify what has been successful, what has failed, and what verification results you have from screenshots.
3.  **SCREENSHOT-BASED PLANNING:** Analyze the current screenshot to identify visible elements and text. Plan your next action based ONLY on what you can see in the screenshot.
4.  **SELECTOR EXTRACTION REQUIREMENT:** If you need to interact with ANY element and don't have a verified working selector, you MUST use `extract_correct_selector_using_text` with the EXACT text visible in the screenshot.
5.  **SEARCH DETECTION & PROTOCOL:** If you identify a search input field in the screenshot, follow the mandatory search flow protocol: extract selector → click → fill → press Enter.
6.  **ACTION VERIFICATION:** After taking any action, the system will provide verification results. Use this feedback to update your memory and planning.
7.  **Act:** Choose ONE action from the available tools to move closer to the user's objective.

**Available Tools (Action JSON format):**
-   `{{"type": "fill", "selector": "<css_selector>", "text": "<text_to_fill>"}}`: To type in an input field.
-   `{{"type": "click", "selector": "<css_selector>"}}`: To click a button or link.
-   `{{"type": "press", "selector": "<css_selector>", "key": "<key_name>"}}`: To press a key (e.g., "Enter") on an element. **Hint: After filling a search bar, this is often more reliable than clicking a suggestion button.**
-   `{{"type": "scroll", "direction": "down"}}`: To scroll the page and reveal more content.
-   `{{"type": "extract", "items": [{{"title": "...", "price": "...", "url": "...", "snippet": "..."}}]}}`: To extract structured data from the CURRENT VIEW.
-   `{{"type": "finish", "reason": "<summary_of_completion>"}}`: To end the mission when the objective is fully met.
-   `{{"type": "dismiss_popup_using_text", "text": "<text_on_dismiss_button>"}}`: **(HIGH PRIORITY)** Use this first to dismiss any pop-ups or banners by clicking the element with the matching text.
-   `{{"type": "request_user_input", "input_type": "<text|password|otp|email|phone>", "prompt": "<descriptive_prompt_for_user>", "is_sensitive": <true|false>}}`: **Use this when you need user input** like login credentials, OTP codes, phone numbers, etc. The agent will pause and wait for user response.
-   `{{"type": "solve_captcha"}}`: **Use this when you detect a CAPTCHA challenge** on the page. The system will automatically detect and solve any type of CAPTCHA (reCAPTCHA, Turnstile, hCAPTCHA, etc.) present on the page.

**Magic Tools (Action JSON format):**
-   `{{"type": "extract_correct_selector_using_text", "text": "Exact text visible in screenshot"}}`: **MANDATORY** tool for finding CSS selectors. Use this BEFORE any interaction (click, fill, press) when you don't have a verified working selector. 
    **CRITICAL RULES:** 
    • Text MUST be EXACTLY as visible in current screenshot - no variations, no assumptions
    • Look at the screenshot first, then copy the exact text you see
    • Never use text from HTML, memory, or guesswork
    • **RETURNS SELECTOR LIST**: This tool returns multiple selectors that you MUST test exhaustively
    • **MANDATORY TESTING**: After using this tool, you MUST test EVERY returned selector with click/fill/press actions
    • **NO OTHER ACTIONS**: While testing selectors, you CANNOT scroll, navigate, or do anything else
    • **COMPLETE EXHAUSTION**: Only after testing ALL selectors can you try different approaches
    • If no text is clearly visible in screenshot, scroll or take different action first

**Response Format:**
You MUST respond with a single, valid JSON object containing "thought" and "action". Do NOT add any other text, explanations, or markdown.
Example Response for dismissing a pop-up:
```json
{{
    "thought": "The first thing I see is a large cookie consent banner blocking the page. I need to click the 'Accept All' button to continue.",
    "action": {{"type": "dismiss_popup_using_text", "text": "Accept All"}}
}}
Example Response for requesting user input:
```json
{{
    "thought": "I found a login form with username and password fields, but the user hasn't provided credentials in their query. I need to request this information from the user.",
    "action": {{"type": "request_user_input", "input_type": "email", "prompt": "Please provide your email address for login", "is_sensitive": false}}
}}
```

Example Response for using user input:
```json
{{
    "thought": "The user provided their email address: 'user@example.com'. Now I'll fill the username field with their exact input value.",
    "action": {{"type": "fill", "selector": "#username", "text": "user@example.com"}}
}}
```

**CRITICAL: When you see user input in your history like:**
- "👤 USER PROVIDED EMAIL: user@example.com [Ready to use in next fill action]"
- "🔐 USER PROVIDED PASSWORD: [SENSITIVE DATA PROVIDED - Ready to use in next fill action]"

**🚨 ABSOLUTELY CRITICAL - USER INPUT USAGE RULE:**
When you see user-provided input in your history, you MUST extract and use the EXACT VALUE from your history text. 

**DO NOT GENERATE OR MAKE UP VALUES. ONLY USE WHAT THE USER ACTUALLY PROVIDED.**

**For sensitive data like passwords, look for the pattern in your history:**
- Search for "USER PROVIDED PASSWORD:" in your history
- If you see user input like "user_input_response: 'Pranavsurya@123'" in context
- Use that EXACT value "Pranavsurya@123", do NOT generate "Abcd@123456" or any other password

Example of CORRECT usage when user provided password "MySecret123":
```json
{{
    "thought": "I can see in the context that user_input_response is 'MySecret123'. I must use this exact password value, not generate my own.",
    "action": {{"type": "fill", "selector": "#password", "text": "MySecret123"}}
}}
```

Example of WRONG usage (NEVER DO THIS):
```json
{{
    "thought": "I need to fill a password field",
    "action": {{"type": "fill", "selector": "#password", "text": "Abcd@123456"}}
}}
```

**You MUST use the EXACT VALUE provided by the user, NOT any placeholders. For sensitive data, use the actual value even though it's hidden in the display.**

Example of CORRECT usage after user provides email "john@example.com":
```json
{{
    "thought": "I can see the user provided their email: john@example.com. I'll fill the email field with this exact value.",
    "action": {{"type": "fill", "selector": "#email", "text": "john@example.com"}}
}}
```

Example of WRONG usage (DO NOT DO THIS):
```json
{{
    "action": {{"type": "fill", "selector": "#email", "text": "{{USER_INPUT}}"}}
}}
```

**SEARCH FUNCTIONALITY ENFORCEMENT:**
- **SEARCH DETECTION**: If you see ANY search bar, search input, or search field in the screenshot, this becomes your ABSOLUTE PRIORITY
- **SEARCH OVER NAVIGATION**: Even if you see category menus, navigation links, or filters, you MUST use search functionality first
- **SEARCH TEXT EXAMPLES**: Look for text like "Search for Products", "Search", "Find", input fields, search icons
- **MANDATORY SEARCH FLOW**: Once you identify search functionality:
  1. Extract selectors using `extract_correct_selector_using_text` with exact visible text
  2. Test ALL returned selectors with click action until one works
  3. Fill the working search field with user's query
  4. Press "Enter" to submit search
- **NO SHORTCUTS**: Do not skip steps, do not try alternative navigation until search is exhausted

**Current Situation Analysis:**
Based on the provided HTML, screenshot, and your recent history, what is your next thought and action?

**CRITICAL SEARCH REMINDER**: If there's a search bar visible in the screenshot, you MUST use it first before any other navigation method!

**ENHANCED WORKFLOW RULES:**
- **ABSOLUTE SEARCH PRIORITY**: When you see ANY search functionality (search bar, input field), this is your PRIMARY objective. You MUST use search functionality first before ANY other navigation methods.
- **MANDATORY SEARCH PROTOCOL**: When you identify search functionality, follow this EXACT sequence WITHOUT deviation:
  1. Use `extract_correct_selector_using_text` with the exact search input placeholder text visible in screenshot
  2. Test ALL returned selectors one-by-one with click action until one works
  3. Fill the search input field with the user's query  
  4. Press "Enter" to submit the search
- **COMPLETE SELECTOR EXHAUSTION PROTOCOL**: When you receive selectors from `extract_correct_selector_using_text`:
  - You MUST test EVERY SINGLE selector in the returned list
  - Test them in order: first selector, second selector, third selector, etc.
  - Use ONLY the exact selectors provided - DO NOT create your own selectors
  - BANNED SELECTOR PATTERNS: `input[placeholder='...']`, `input[type='text']`, `.search-input`, `#search-box`, etc.
  - Do NOT skip selectors or try alternative approaches until ALL selectors are tested
  - Only after ALL selectors fail can you try a different approach
  - If you have 10 selectors, you must make 10 click attempts before doing anything else
- **FORBIDDEN DURING SELECTOR TESTING**: While testing extracted selectors, you are ABSOLUTELY FORBIDDEN from:
  - Scrolling the page
  - Trying navigation menus  
  - Using different extraction terms
  - Taking any action other than testing the current selector list
- **SCREENSHOT-TEXT ONLY RULE**: CRITICAL - Only use `extract_correct_selector_using_text` with text that you can VISUALLY see in the current screenshot. Never use text from memory, HTML, or knowledge.
- **SEARCH OVER NAVIGATION**: Always prioritize search functionality over category menus, filters, or navigation links
- **NO SELECTOR GUESSING**: Never attempt to create CSS selectors manually - always use the extraction tool
- **PERSISTENCE REQUIREMENT**: Keep testing selectors until you exhaust all options - never give up on search functionality early

**LOGIN FAILURE HANDLING:**
- If you see "🚫 LOGIN FAILURE DETECTED" in your history, this means the previous login attempt failed
- You should immediately request NEW credentials from the user using `request_user_input`
- Use clear prompts like: "The previous login failed. Please provide the correct email address" or "The previous password was incorrect. Please provide the correct password"
- Do NOT reuse credentials that have already failed - always request fresh ones
- After getting new credentials, retry the login process from the beginning
  - CAPTCHA solutions (ask user to solve)
  - Two-factor authentication codes
  - Any other information that only the user can provide
- The most important note is that you have to finish the task at any cost. Do not leave the task unfinished. If you are not able to find the required information, try to find the closest possible information and extract that.
- Do not try to overfetch or extract unnecessary information. Only extract what is required to fulfill the user's objective. If the required information is already extracted, use the finish action to complete the task.
- There is no scroll up action. You can only scroll down. So plan your actions accordingly.
- If any one selector is not working or the element is not found using that selector, then use the magic tool `extract_correct_selector_using_text` to find the correct selector for that element using its exact text content. Do not try to guess or modify the selector by yourself. And do not try to use any other selector from the history if that selector is not working. Always use the magic tool to find the correct selector.
"""

def get_refined_prompt(url: str, query: str, provider: LLMProvider) -> Tuple[str, Dict]:
    """Generates a refined, actionable prompt and returns the token usage."""
    prompt = REFINER_PROMPT.format(url=url, query=query)
    response_text, usage = get_llm_response("You are a helpful assistant.", prompt, provider, images=[])
    return response_text.strip(), usage

def get_agent_action(query: str, url: str, html: str, provider: LLMProvider, screenshot_path: Union[Path, None], history: str, failed_actions: Dict[str, int] = None) -> Tuple[dict, Dict]:
    """Gets the next thought and action from the agent, and returns token usage."""
    # Add note about screenshot availability
    screenshot_note = ""
    if not screenshot_path:
        screenshot_note = "\n\n**⚠️ NOTE: Screenshot capture failed - relying on HTML content only for analysis.**"
    
    prompt = AGENT_PROMPT.format(query=query, url=url, history=history or "No actions taken yet.") + screenshot_note
    
    # Enhance system prompt with explicit banned actions
    system_prompt = "You are an autonomous web agent. Respond ONLY with the JSON object containing your thought and action."
    
    if failed_actions:
        banned_signatures = list(failed_actions.keys())
        if banned_signatures:
            system_prompt += f"\n\nCRITICAL: These action signatures are PERMANENTLY BANNED (do not even consider them):"
            for sig in banned_signatures[:10]:  # Show top 10 banned actions
                system_prompt += f"\n- BANNED: {sig}"
            system_prompt += f"\nIf you think of any banned action, immediately think of something different instead!"
            
            # Add specific warnings for common problematic selectors
            search_bans = [sig for sig in banned_signatures if "input[placeholder='Search for Products']" in sig]
            if search_bans:
                system_prompt += f"\n\nSPECIAL WARNING: The selector 'input[placeholder='Search for Products']' is completely broken - never use it!"

    try:
        images = [screenshot_path] if screenshot_path else []
        response_text, usage = get_llm_response(system_prompt, prompt, provider, images=images)
        
        if not response_text or not response_text.strip():
            # Never give up - create a default exploration action
            print("🚨 Empty LLM response - creating emergency scroll action")
            return {
                "thought": "Received empty response from LLM. Using emergency scroll action to continue exploration.",
                "action": {"type": "scroll", "direction": "down"}
            }, {"input_tokens": 0, "output_tokens": 0}
        
        action = extract_json_from_response(response_text)
        
        # Validate and fix action structure
        if not isinstance(action, dict):
            print("🚨 Invalid action structure - creating emergency action")
            action = {
                "thought": "Invalid response structure detected. Using emergency scroll action.",
                "action": {"type": "scroll", "direction": "down"}
            }
        
        if "action" not in action:
            print("🚨 Missing action field - adding default action")
            action["action"] = {"type": "scroll", "direction": "down"}
        
        if "thought" not in action:
            action["thought"] = "Generated emergency thought due to missing thought field."
        
        # Validate action has required fields
        if not isinstance(action["action"], dict) or "type" not in action["action"]:
            print("🚨 Invalid action format - fixing")
            action["action"] = {"type": "scroll", "direction": "down"}
        
        return action, usage
        
    except Exception as e:
        print(f"🚨 Critical error in get_agent_action: {e}")
        # NEVER give up - always return a valid action to continue
        emergency_action = {
            "thought": f"Critical parsing error occurred: {str(e)[:100]}. Using emergency scroll action to continue mission.", 
            "action": {"type": "scroll", "direction": "down"}
        }
        # Return actual usage if available, otherwise zeros
        error_usage = {"input_tokens": usage.get("input_tokens", 0) if 'usage' in locals() else 0, "output_tokens": 0} 
        return emergency_action, error_usage


def get_llm_response(
    system_prompt: str,
    prompt: str,
    provider: LLMProvider,
    images: List[Path]
) -> Tuple[str, Dict]:
    """Gets a response and token usage from the specified LLM provider."""
    usage = {"input_tokens": 0, "output_tokens": 0}
    
    if provider == LLMProvider.ANTHROPIC:
        if not anthropic_client: raise ValueError("Anthropic client not initialized.")
        
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        # for img_path in images:
        #     with open(img_path, "rb") as f: img_data = base64.b64encode(f.read()).decode("utf-8")
        #     messages[0]["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data}})

        if images:
            last_image_path = images[-1]
            if last_image_path and last_image_path.exists() and last_image_path.stat().st_size > 0:
                try:
                    with open(last_image_path, "rb") as f: 
                        img_data = base64.b64encode(f.read()).decode("utf-8")
                        # Only add image if we have actual data
                        if img_data:
                            messages[0]["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data}})
                except Exception as e:
                    print(f"Warning: Failed to read screenshot {last_image_path}: {e}")
                    # Continue without image

        response = anthropic_client.messages.create(model=ANTHROPIC_MODEL, max_tokens=2048, system=system_prompt, messages=messages)
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        
        # Handle potential None response content
        if not response.content or len(response.content) == 0:
            return "", usage
        
        content = response.content[0]
        if hasattr(content, 'text'):
            return content.text or "", usage
        else:
            return str(content), usage

    elif provider == LLMProvider.OPENAI:
        if not openai_client: raise ValueError("OpenAI client not initialized.")
        
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        for img_path in images:
            if img_path and img_path.exists() and img_path.stat().st_size > 0:
                try:
                    with open(img_path, "rb") as f: 
                        img_data = base64.b64encode(f.read()).decode("utf-8")
                        if img_data:
                            messages[0]["content"].append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_data}"}})
                except Exception as e:
                    print(f"Warning: Failed to read screenshot {img_path}: {e}")
                    # Continue without image
        
        response = openai_client.chat.completions.create(model=OPENAI_MODEL, max_tokens=2048, messages=[{"role": "system", "content": system_prompt}, *messages])
        if response.usage:
            usage = {"input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}
        
        # Handle potential None response content
        content = response.choices[0].message.content
        return content or "", usage

    elif provider == LLMProvider.GROQ:
        if not groq_client: raise ValueError("Groq client not initialized.")
        if images: raise ValueError("The configured Groq model does not support vision.")

        response = groq_client.chat.completions.create(model=GROQ_MODEL, max_tokens=2048, messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}])
        if response.usage:
             usage = {"input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}
        
        # Handle potential None response content
        content = response.choices[0].message.content
        return content or "", usage

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


def extract_json_from_response(text: str) -> Union[dict, list]:
    """Robustly extracts a JSON object or array from a string with multiple fallback strategies."""
    if not text or not text.strip():
        raise ValueError("Empty response from LLM")
    
    # Clean the text first
    text = text.strip()
    
    # Strategy 1: Remove markdown code blocks if present
    markdown_patterns = [
        r'```json\s*\n(.*?)\n```',  # ```json ... ```
        r'```\s*\n(.*?)\n```',     # ``` ... ```
        r'`(.*?)`',                # `...`
    ]
    
    for pattern in markdown_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                cleaned_text = match.group(1).strip()
                parsed = json.loads(cleaned_text)
                if isinstance(parsed, dict) and ('thought' in parsed or 'action' in parsed):
                    return parsed
            except json.JSONDecodeError:
                continue
    
    # Strategy 2: Find JSON patterns with improved regex
    json_patterns = [
        r'\{[^{}]*"thought"[^{}]*"action"[^{}]*\{[^{}]*\}[^{}]*\}',  # Complete structure
        r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',  # Nested JSON
        r'\{.*?"thought".*?"action".*?\}',    # Contains required fields
        r'\{.*?\}',  # Any JSON object
    ]
    
    for pattern in json_patterns:
        matches = re.findall(pattern, text, re.DOTALL)
        for match in matches:
            try:
                parsed = json.loads(match)
                if isinstance(parsed, dict) and ('thought' in parsed or 'action' in parsed):
                    return parsed
            except json.JSONDecodeError:
                continue
    
    # Strategy 3: Extract between first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            candidate = text[start:end+1]
            # Try to fix common JSON issues
            candidate = candidate.replace('\n', ' ').replace('\r', ' ')
            candidate = re.sub(r'\s+', ' ', candidate)  # Normalize whitespace
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    
    # Strategy 4: Try to reconstruct JSON from fragments
    try:
        # Look for thought and action separately
        thought_match = re.search(r'"thought":\s*"([^"]*(?:\\.[^"]*)*)"', text, re.DOTALL)
        action_match = re.search(r'"action":\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})', text, re.DOTALL)
        
        if thought_match and action_match:
            thought = thought_match.group(1)
            action_str = action_match.group(1)
            action = json.loads(action_str)
            
            return {
                "thought": thought,
                "action": action
            }
    except (json.JSONDecodeError, AttributeError):
        pass
    
    # Strategy 5: Emergency fallback - never give up, create a scroll action
    print(f"🚨 JSON PARSING FAILED - Creating emergency action. Original text: {text[:300]}...")
    
    # Try to determine what the agent was trying to do based on text content
    text_lower = text.lower()
    
    if "search" in text_lower and ("click" in text_lower or "fill" in text_lower):
        # Agent was trying to interact with search
        return {
            "thought": "JSON parsing failed but detected search intent. Using fallback search action.",
            "action": {"type": "extract_correct_selector_using_text", "text": "Search"}
        }
    elif "extract" in text_lower or "data" in text_lower:
        # Agent was trying to extract data
        return {
            "thought": "JSON parsing failed but detected extraction intent. Scrolling to find more content.",
            "action": {"type": "scroll", "direction": "down"}
        }
    else:
        # Default fallback - scroll to see more content
        return {
            "thought": "JSON parsing failed. Using emergency scroll action to continue exploring the page.",
            "action": {"type": "scroll", "direction": "down"}
        }