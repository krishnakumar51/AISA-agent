#!/usr/bin/env python3
"""
Enhanced IMSS CAPTCHA solver with ngrok browser connection
Supports both page source download and automated CAPTCHA solving
Updated to use the FIXED ultimate_captcha_solver module
"""
import asyncio
import aiohttp
from playwright.async_api import async_playwright
import os
import sys
from dotenv import load_dotenv

# Add the current directory to path to import captcha module
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from ultimate_captcha_solver import UltimateCaptchaSolver

# Load environment variables
load_dotenv()

# Dummy test credentials for IMSS testing
DUMMY_CREDENTIALS = {
    'nss': '12345678901',  # 11-digit NSS (dummy)
    'curp': 'ABCD123456EFGHIJ01',  # 18-character CURP (dummy)
    'email': 'test.user@example.com'  # Email if needed
}

async def take_screenshot(page, filename):
    """Take viewport-sized screenshot (not full page)"""
    try:
        # Create screenshots directory if it doesn't exist
        os.makedirs('screenshots', exist_ok=True)
        screenshot_path = f"screenshots/{filename}.png"
        await page.screenshot(path=screenshot_path)  # Viewport size by default
        print(f"[SCREENSHOT] Saved: {screenshot_path}")
    except Exception as e:
        print(f"[ERROR] Screenshot failed: {e}")

async def solve_imss_captcha_and_submit(nss=None, curp=None, email=None):
    """
    Solve IMSS website CAPTCHA and submit the form using ngrok connection with ultimate CAPTCHA solver
    """
    # Use dummy credentials if none provided
    if not nss:
        nss = DUMMY_CREDENTIALS['nss']
    if not curp:
        curp = DUMMY_CREDENTIALS['curp']
    if not email:
        email = DUMMY_CREDENTIALS['email']
    
    target_url = "https://serviciosdigitales.imss.gob.mx/semanascotizadas-web/usuarios/IngresoAsegurado"
    ngrok_base_url = "https://agent-umi.ngrok.app"
    
    print(f"[TARGET] Starting IMSS CAPTCHA solver for NSS: {nss}, CURP: {curp}")
    print(f"[CONNECT] Connecting to ngrok: {ngrok_base_url}")
    
    try:
        # Get DevTools endpoint from ngrok
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{ngrok_base_url}/json/version") as resp:
                data = await resp.json()
                websocket_path = data["webSocketDebuggerUrl"].split("/devtools/")[1]
                cdp_endpoint = f"wss://agent-umi.ngrok.app/devtools/{websocket_path}"
        
        print(f"[CDP] CDP Endpoint: {cdp_endpoint}")
        
        # Connect to browser through ngrok
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            
            print("[OK] Connected to browser successfully")
            
            # Initialize Ultimate CAPTCHA solver (FIXED VERSION)
            print("[INIT] Initializing Ultimate CAPTCHA solver (Fixed Version)...")
            try:
                solver = UltimateCaptchaSolver()
                print("[OK] ✅ Solver initialized successfully")
                print(f"[INFO] Available services: ", end="")
                services = []
                if solver.cs_api_key:
                    services.append("CapSolver")
                if solver.tc_api_key:
                    services.append("2Captcha")
                if solver.ac_api_key:
                    services.append("AntiCaptcha")
                if solver.dbc_user and solver.dbc_pass:
                    services.append("DeathByCaptcha")
                print(", ".join(services) if services else "None configured!")
                
            except Exception as e:
                print(f"[ERROR] ❌ Failed to initialize solver: {e}")
                print("[ERROR] Make sure config.py has at least one API key configured!")
                return False
            
            # Install stealth mode and pre-page hooks
            print("[STEALTH] Installing stealth mode and CAPTCHA hooks...")
            try:
                await solver.install_stealth_mode(page)
                await solver.install_captcha_hooks(page)
                print("[OK] ✅ Stealth mode and hooks installed")
            except Exception as e:
                print(f"[WARNING] ⚠️ Stealth installation warning: {e}")
                print("[INFO] Continuing anyway...")
            
            # Navigate to IMSS website
            print(f"[NAVIGATE] Navigating to: {target_url}")
            await page.goto(target_url, wait_until='networkidle', timeout=30000)
            print("[OK] Page loaded successfully")
            
            # Take screenshot after initial page load
            print("[SCREENSHOT] Taking initial page screenshot...")
            await take_screenshot(page, "01_initial_page_load")
            
            # Wait a moment for any dynamic content to load
            await asyncio.sleep(2)
            
            # Debug: List all form fields on the page
            print("[DEBUG] Analyzing all form fields on the page...")
            try:
                all_inputs = await page.query_selector_all("input, select, textarea")
                print(f"[DEBUG] Found {len(all_inputs)} form elements:")
                
                for i, input_elem in enumerate(all_inputs):
                    try:
                        tag_name = await input_elem.evaluate("el => el.tagName")
                        input_type = await input_elem.evaluate("el => el.type || 'N/A'")
                        input_id = await input_elem.evaluate("el => el.id || 'N/A'")
                        input_name = await input_elem.evaluate("el => el.name || 'N/A'")
                        input_class = await input_elem.evaluate("el => el.className || 'N/A'")
                        placeholder = await input_elem.evaluate("el => el.placeholder || 'N/A'")
                        
                        print(f"   [{i+1}] {tag_name} - Type: {input_type}, ID: {input_id}, Name: {input_name}")
                        if placeholder != 'N/A':
                            print(f"        Placeholder: {placeholder}")
                        if input_class != 'N/A':
                            print(f"        Class: {input_class}")
                        
                    except Exception as e:
                        print(f"   [{i+1}] Error analyzing element: {e}")
                        
            except Exception as e:
                print(f"[DEBUG] Error analyzing form fields: {e}")
            
            # First, solve CAPTCHA before filling form fields
            print("=" * 60)
            print("[CAPTCHA] STEP 1: CAPTCHA Detection and Solving")
            print("=" * 60)
            
            # Take screenshot before CAPTCHA detection
            await take_screenshot(page, "02_before_captcha_detection")
            
            # Use ultimate solver's advanced CAPTCHA detection and solving
            captcha_solution = None
            try:
                print("[CAPTCHA] Running advanced CAPTCHA detection...")
                captcha_result = await solver.solve_captcha_if_present(page, target_url)
                
                # Take screenshot after CAPTCHA attempt
                await take_screenshot(page, "03_after_captcha_solving")
                
                # Check CAPTCHA result comprehensively
                if captcha_result.get('found'):
                    print(f"[DETECTED] ✅ CAPTCHA Found!")
                    print(f"   Type: {captcha_result.get('type', 'Unknown')}")
                    print(f"   Confidence: {captcha_result.get('confidence', 0)}%")
                    print(f"   Method: {captcha_result.get('method', 'Unknown')}")
                    
                    if captcha_result.get('solved'):
                        print(f"[SUCCESS] ✅ CAPTCHA solved successfully!")
                        print(f"   Service used: {captcha_result.get('service', 'Unknown')}")
                        print(f"   Solve time: {captcha_result.get('solve_time', 0):.2f}s")
                        
                        # Extract the solution token
                        captcha_solution = captcha_result.get('token')
                        if captcha_solution:
                            print(f"   Solution: {captcha_solution}")
                            print("[INFO] CAPTCHA solution ready for form filling")
                        else:
                            print("[WARNING] ⚠️ Solution token not found in result")
                        
                        # Wait for solution to be fully processed
                        await asyncio.sleep(2)
                        
                    else:
                        error_msg = captcha_result.get('error', 'Unknown error')
                        print(f"[ERROR] ❌ CAPTCHA solving failed: {error_msg}")
                        print("[WARNING] Will attempt form submission anyway...")
                else:
                    print("[INFO] ℹ️ No CAPTCHA detected on this page")
                    print("[INFO] This might mean:")
                    print("       1. CAPTCHA detection failed")
                    print("       2. Page structure changed")
                    print("       3. CAPTCHA not present on this version")
                    
            except Exception as captcha_error:
                print(f"[ERROR] ❌ CAPTCHA solving exception: {captcha_error}")
                import traceback
                print(f"[DEBUG] Traceback:\n{traceback.format_exc()}")
                print("[WARNING] Continuing with form submission anyway...")
                await take_screenshot(page, "03_captcha_error")
            
            print("=" * 60)
            print("[FORM] STEP 2: Form Field Filling")
            print("=" * 60)
            
            # Enhanced form filling with corrected selectors based on page source analysis
            print("[FORM] Filling form fields with corrected selectors...")
            
            # Corrected field selectors based on page source: id="NSS" name="nss"
            form_fields = {
                'NSS': {
                    'selectors': ['#NSS', 'input[name="nss"]', 'input[id="NSS"]', 'input[placeholder*="NSS"]'],
                    'value': nss
                },
                'CURP': {
                    'selectors': ['#CURP', 'input[name="curp"]', 'input[id="CURP"]', 'input[placeholder*="CURP"]'],
                    'value': curp
                },
                'CAPTCHA': {
                    'selectors': ['#captcha', 'input[name="captcha"]', 'input[id="captcha"]', 'input[placeholder*="captcha"]'],
                    'value': captcha_solution  # Use the solved CAPTCHA if available
                },
                'EMAIL': {
                    'selectors': ['#email', 'input[name="email"]', 'input[id="email"]', 'input[type="email"]'],
                    'value': email
                }
            }
            
            filled_fields = []
            
            for field_name, field_data in form_fields.items():
                selectors = field_data['selectors']
                value = field_data['value']
                
                # Skip if no value provided
                if not value:
                    print(f"[SKIP] {field_name}: No value provided")
                    continue
                
                field_filled = False
                for selector in selectors:
                    try:
                        # Wait for element to be available
                        element = await page.wait_for_selector(selector, timeout=5000, state='visible')
                        
                        if element:
                            # Clear the field first
                            await element.fill('')
                            await asyncio.sleep(0.3)
                            
                            # Type the value
                            await element.type(value, delay=50)  # Human-like typing
                            await asyncio.sleep(0.3)
                            
                            # Verify the value was filled
                            filled_value = await element.input_value()
                            if filled_value == value:
                                print(f"   [SUCCESS] ✅ Filled {field_name} using selector: {selector}")
                                filled_fields.append(field_name)
                                field_filled = True
                                break
                            else:
                                print(f"   [WARNING] ⚠️ {field_name} value mismatch: expected '{value}', got '{filled_value}'")
                    
                    except Exception as e:
                        # Silently try next selector
                        continue
                
                if not field_filled:
                    print(f"   [FAILED] ❌ Could not fill {field_name} with any selector")
            
            # Take screenshot after form filling
            await take_screenshot(page, "04_form_filled")
            
            print(f"[INFO] Successfully filled {len(filled_fields)}/{len([f for f in form_fields.values() if f['value']])} fields")
            print(f"[INFO] Filled fields: {', '.join(filled_fields)}")
            
            # Verify CAPTCHA was filled
            if captcha_solution and 'CAPTCHA' not in filled_fields:
                print("[WARNING] ⚠️ CAPTCHA solution was available but field was not filled!")
                print("[INFO] Attempting manual CAPTCHA field fill...")
                
                # Try to fill CAPTCHA manually
                for selector in form_fields['CAPTCHA']['selectors']:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            await element.fill(captcha_solution)
                            print(f"[SUCCESS] ✅ Manually filled CAPTCHA with selector: {selector}")
                            filled_fields.append('CAPTCHA')
                            break
                    except:
                        continue
            
            print("=" * 60)
            print("[SUBMIT] STEP 3: Form Submission")
            print("=" * 60)
            
            # Try different submit button selectors
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Consultar")',
                'button:has-text("Enviar")',
                'button:has-text("Aceptar")',
                '#btnConsultar',
                '#submit',
                '.btn-primary',
                '.btn-submit'
            ]
            
            submit_clicked = False
            for selector in submit_selectors:
                try:
                    submit_button = await page.query_selector(selector)
                    if submit_button:
                        # Check if button is visible and enabled
                        is_visible = await submit_button.is_visible()
                        is_enabled = await submit_button.is_enabled()
                        
                        if is_visible and is_enabled:
                            print(f"[SUBMIT] Clicking submit button: {selector}")
                            await submit_button.click()
                            submit_clicked = True
                            print("[OK] ✅ Submit button clicked")
                            break
                        else:
                            print(f"[SKIP] Button found but not clickable: {selector} (visible: {is_visible}, enabled: {is_enabled})")
                
                except Exception as e:
                    # Try next selector
                    continue
            
            if not submit_clicked:
                print("[ERROR] ❌ Could not find or click submit button")
                print("[INFO] Attempting to submit via Enter key...")
                try:
                    # Try pressing Enter on the last filled field
                    if filled_fields:
                        last_field_selector = form_fields[filled_fields[-1]]['selectors'][0]
                        await page.press(last_field_selector, 'Enter')
                        print("[OK] ✅ Pressed Enter to submit")
                        submit_clicked = True
                except Exception as e:
                    print(f"[ERROR] Enter key press failed: {e}")
            
            # Wait for response
            print("[WAIT] Waiting for response...")
            await asyncio.sleep(5)
            
            # Take screenshot of result
            await take_screenshot(page, "05_after_submit")
            
            # Check for success/error messages
            print("[CHECK] Analyzing response...")
            
            # Look for common success/error indicators
            success_indicators = [
                'semanas cotizadas',
                'resultado',
                'consulta exitosa',
                'success'
            ]
            
            error_indicators = [
                'error',
                'incorrecto',
                'inválido',
                'no encontrado',
                'captcha incorrecto',
                'datos incorrectos'
            ]
            
            page_content = await page.content()
            page_text = await page.evaluate("() => document.body.innerText")
            
            found_success = any(indicator.lower() in page_text.lower() for indicator in success_indicators)
            found_error = any(indicator.lower() in page_text.lower() for indicator in error_indicators)
            
            # Save result HTML
            with open('form_result.html', 'w', encoding='utf-8') as f:
                f.write(page_content)
            print("[SAVE] Form result saved to: form_result.html")
            
            # Check current URL for changes
            current_url = page.url
            url_changed = current_url != target_url
            
            print(f"[URL] Current URL: {current_url}")
            print(f"[URL] URL Changed: {url_changed}")
            
            if found_success:
                print("[RESULT] ✅ SUCCESS - Form submission appears successful!")
                print("[INFO] Check form_result.html for detailed results")
                return True
            elif found_error:
                print("[RESULT] ❌ ERROR - Error message detected in response")
                print("[INFO] Common errors:")
                print("       - Incorrect CAPTCHA")
                print("       - Invalid NSS/CURP")
                print("       - Service temporarily unavailable")
                print("[INFO] Check form_result.html for details")
                return False
            elif url_changed:
                print("[RESULT] ℹ️ POSSIBLE SUCCESS - URL changed (might indicate successful submission)")
                print("[INFO] Check form_result.html for confirmation")
                return True
            else:
                print("[RESULT] ⚠️ UNCLEAR - Could not determine success/failure")
                print("[INFO] Manual review required - check form_result.html")
                await take_screenshot(page, "10_unclear_result")
                return None
                        
    except Exception as e:
        import traceback
        print(f"[ERROR] Error during form submission: {e}")
        print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        
        # Try to take error screenshot if page is still available
        try:
            await take_screenshot(page, "99_error_state")
        except:
            pass
        
        return False

async def download_page_source():
    """Download page source from IMSS website using ngrok connection"""
    
    target_url = "https://serviciosdigitales.imss.gob.mx/semanascotizadas-web/usuarios/IngresoAsegurado"
    ngrok_base_url = "https://agent-umi.ngrok.app"
    
    print(f"[CONNECT] Connecting to ngrok: {ngrok_base_url}")
    print(f"[TARGET] Target URL: {target_url}")
    
    try:
        # Get DevTools endpoint from ngrok
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{ngrok_base_url}/json/version") as resp:
                data = await resp.json()
                websocket_path = data["webSocketDebuggerUrl"].split("/devtools/")[1]
                cdp_endpoint = f"wss://agent-umi.ngrok.app/devtools/{websocket_path}"
        
        print(f"[CDP] CDP Endpoint: {cdp_endpoint}")
        
        # Connect to browser through ngrok
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_endpoint)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            
            print("[OK] Connected to browser successfully")
            
            # Navigate to target URL
            print(f"[NAVIGATE] Navigating to: {target_url}")
            await page.goto(target_url, wait_until='networkidle', timeout=30000)
            
            print("[OK] Page loaded successfully")
            
            # Get page source
            page_source = await page.content()
            
            # Save page source to file
            with open('page_source.html', 'w', encoding='utf-8') as f:
                f.write(page_source)
            
            print(f"[SAVE] Page source saved to: page_source.html")
            print(f"[INFO] Page source size: {len(page_source)} characters")
            
            # Get page title and URL for verification
            title = await page.title()
            current_url = page.url
            
            print(f"[TITLE] Page title: {title}")
            print(f"[URL] Current URL: {current_url}")
            
            return True
            
    except Exception as e:
        print(f"[ERROR] Error: {e}")
        import traceback
        print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        return False

async def main():
    """Main function"""
    import sys
    
    print("="*70)
    print("   IMSS CAPTCHA SOLVER - Enhanced Version with Fixed Solver")
    print("="*70)
    print()
    
    if len(sys.argv) >= 2:
        if sys.argv[1].lower() == "test" or sys.argv[1].lower() == "dummy":
            # Test mode with dummy credentials
            print("[TEST] Starting IMSS CAPTCHA solver with DUMMY credentials...")
            print("=" * 60)
            print(f"[DUMMY] Using NSS: {DUMMY_CREDENTIALS['nss']}")
            print(f"[DUMMY] Using CURP: {DUMMY_CREDENTIALS['curp']}")
            print(f"[DUMMY] Using Email: {DUMMY_CREDENTIALS['email']}")
            print("=" * 60)
            
            success = await solve_imss_captcha_and_submit()
            
            print("=" * 60)
            if success:
                print("[SUCCESS] ✅ CAPTCHA solving completed with dummy credentials!")
                print("[INFO] Check form_result.html for the result")
            elif success is False:
                print("[FAILED] ❌ CAPTCHA solving failed - check the error messages above")
            else:
                print("[UNCLEAR] ⚠️ Result unclear - manual review required")
        
        elif sys.argv[1].lower() == "download":
            # Page source download mode
            print("[START] Starting page source download...")
            print("=" * 50)
            
            success = await download_page_source()
            
            print("=" * 50)
            if success:
                print("[SUCCESS] ✅ Download completed successfully!")
                print("[INFO] Check page_source.html for the downloaded content")
            else:
                print("[FAILED] ❌ Download failed - check the error messages above")
        
        elif len(sys.argv) >= 3:
            # CAPTCHA solving mode with provided credentials
            nss = sys.argv[1]
            curp = sys.argv[2]
            email = sys.argv[3] if len(sys.argv) > 3 else None
            
            print("[START] Starting IMSS CAPTCHA solver with provided credentials...")
            print("=" * 60)
            print(f"[INFO] NSS: {nss}")
            print(f"[INFO] CURP: {curp}")
            if email:
                print(f"[INFO] Email: {email}")
            print("=" * 60)
            
            success = await solve_imss_captcha_and_submit(nss, curp, email)
            
            print("=" * 60)
            if success:
                print("[SUCCESS] ✅ CAPTCHA solving completed!")
                print("[INFO] Check form_result.html for the result")
            elif success is False:
                print("[FAILED] ❌ CAPTCHA solving failed - check the error messages above")
            else:
                print("[UNCLEAR] ⚠️ Result unclear - manual review required")
        else:
            # Default to dummy credentials if single argument but not test/dummy
            print("[DEFAULT] Starting IMSS CAPTCHA solver with DUMMY credentials...")
            print("=" * 60)
            print(f"[DUMMY] Using NSS: {DUMMY_CREDENTIALS['nss']}")
            print(f"[DUMMY] Using CURP: {DUMMY_CREDENTIALS['curp']}")
            print(f"[DUMMY] Using Email: {DUMMY_CREDENTIALS['email']}")
            print("=" * 60)
            
            success = await solve_imss_captcha_and_submit()
            
            print("=" * 60)
            if success:
                print("[SUCCESS] ✅ CAPTCHA solving completed with dummy credentials!")
                print("[INFO] Check form_result.html for the result")
            elif success is False:
                print("[FAILED] ❌ CAPTCHA solving failed - check the error messages above")
            else:
                print("[UNCLEAR] ⚠️ Result unclear - manual review required")
    else:
        # No arguments - show usage and run with dummy credentials
        print("[USAGE] IMSS CAPTCHA Solver Usage Options:")
        print("=" * 60)
        print("1. Test with dummy credentials:")
        print("   python test.py test")
        print("   python test.py dummy")
        print()
        print("2. Use real credentials:")
        print("   python test.py <NSS> <CURP> [EMAIL]")
        print("   Example: python test.py 12345678901 ABCD123456EFGHIJ01")
        print()
        print("3. Download page source only:")
        print("   python test.py download")
        print()
        print("[DEFAULT] Running with DUMMY credentials in 3 seconds...")
        await asyncio.sleep(3)
        
        print("\n[START] Starting IMSS CAPTCHA solver with DUMMY credentials...")
        print("=" * 60)
        print(f"[DUMMY] Using NSS: {DUMMY_CREDENTIALS['nss']}")
        print(f"[DUMMY] Using CURP: {DUMMY_CREDENTIALS['curp']}")
        print(f"[DUMMY] Using Email: {DUMMY_CREDENTIALS['email']}")
        print("=" * 60)
        
        success = await solve_imss_captcha_and_submit()
        
        print("=" * 60)
        if success:
            print("[SUCCESS] ✅ CAPTCHA solving completed with dummy credentials!")
            print("[INFO] Check form_result.html for the result")
        elif success is False:
            print("[FAILED] ❌ CAPTCHA solving failed - check the error messages above")
        else:
            print("[UNCLEAR] ⚠️ Result unclear - manual review required")

if __name__ == "__main__":
    asyncio.run(main())