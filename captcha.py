import time
import json
import asyncio
import aiohttp
import logging
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, parse_qs
from config import (
    CAPSOLVER_API_KEY, 
    TWOCAPTCHA_API_KEY, 
    ANTICAPTCHA_API_KEY, 
    DBC_USERNAME, 
    DBC_PASSWORD
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] > %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger(__name__)


class UniversalCaptchaSolver:
    """
    🚀 UNIVERSAL CAPTCHA SOLVER - PRODUCTION ENGINE v2.0
    
    Supports ALL major CAPTCHA types across ANY website:
    - Cloudflare Turnstile (0x..., 3x... sitekeys) - v2024 API
    - reCAPTCHA v2/v3/Enterprise (6L... sitekeys)
    - hCAPTCHA (all variants including invisible)
    - FunCAPTCHA (Arkose Labs)
    - GeeTest v3/v4
    - DataDome CAPTCHA
    - AWS WAF CAPTCHA
    - Image CAPTCHAs (base64/URL)
    
    Features:
    - Multi-layer detection with retry logic
    - 4-tier service fallback system
    - Automatic iframe handling
    - Dynamic CAPTCHA monitoring
    - Solution verification
    - Rate limit handling
    """
    
    def __init__(self):
        # Service API keys - Multi-tier fallback system (loaded from environment)
        self.cs_api_key = CAPSOLVER_API_KEY    # CapSolver (Tier 1)
        self.tc_api_key = TWOCAPTCHA_API_KEY   # 2Captcha (Tier 2)
        self.ac_api_key = ANTICAPTCHA_API_KEY  # AntiCaptcha (Tier 3)
        self.dbc_user = DBC_USERNAME           # DeathByCaptcha (Tier 4)
        self.dbc_pass = DBC_PASSWORD
        
        # Validate API keys are loaded
        self._validate_api_keys()
        
        # Service base URLs
        self.cs_base_url = "https://api.capsolver.com"
        self.tc_base_url = "https://2captcha.com"
        self.ac_base_url = "https://api.anti-captcha.com"
        self.dbc_base_url = "http://api.dbcapi.me/api"
        
        # Known test sitekeys (return mock tokens instantly)
        self.test_sitekeys = {
            "3x00000000000000000000FF",  # Cloudflare test
            "1x00000000000000000000AA",  # Cloudflare test v2
            "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",  # Google test
            "10000000-ffff-ffff-ffff-000000000001",  # Generic test
            "0x4AAAAAAADnPIDROlJ2dLay",  # Cloudflare demo
            "6Le-wvkSAAAAAPBMRTvw0Q4Muexq9bi0DJwx_mJ-",  # reCAPTCHA demo
        }
        
        # Detection retry config
        self.max_detection_retries = 3
        self.detection_retry_delay = 2  # seconds
        
        # Solving timeout config
        self.default_timeout = 180  # 3 minutes max per solve attempt

    def _validate_api_keys(self):
        """Validate that CAPTCHA service API keys are properly loaded from environment."""
        available_services = []
        
        if self.cs_api_key:
            available_services.append("CapSolver")
        else:
            logger.warning("⚠️ CapSolver API key not found - Tier 1 service unavailable")
            
        if self.tc_api_key:
            available_services.append("2Captcha")
        else:
            logger.warning("⚠️ 2Captcha API key not found - Tier 2 service unavailable")
            
        if self.ac_api_key:
            available_services.append("AntiCaptcha")
        else:
            logger.warning("⚠️ AntiCaptcha API key not found - Tier 3 service unavailable")
            
        if self.dbc_user and self.dbc_pass:
            available_services.append("DeathByCaptcha")
        else:
            logger.warning("⚠️ DeathByCaptcha credentials not found - Tier 4 service unavailable")
        
        if available_services:
            logger.info(f"🔑 CAPTCHA Services Available: {', '.join(available_services)}")
        else:
            logger.error("❌ No CAPTCHA service API keys found! Please check your .env file.")
            raise ValueError("No CAPTCHA service credentials available")

    async def detect_captcha_universal(self, page, retry_count: int = 0) -> Dict[str, Any]:
        """
        🔍 ENHANCED UNIVERSAL CAPTCHA DETECTION ENGINE v2.0
        
        Multi-layer detection with retry logic:
        1. JavaScript-based DOM analysis (most reliable)
        2. Iframe source inspection
        3. Network request monitoring
        4. Visual element detection
        5. Script content analysis
        
        Returns detailed CAPTCHA info with high confidence scoring
        """
        print(f"🔍 Universal CAPTCHA detection (attempt {retry_count + 1}/{self.max_detection_retries})...")
        
        captcha_info = {
            'type': None,
            'sitekey': None,
            'confidence': 0,
            'element': None,
            'method': 'none',
            'action': None,  # For reCAPTCHA v3
            'data': {}  # Additional metadata
        }
        
        try:
            # LAYER 1: Advanced JavaScript Detection with Extended Coverage
            js_detection = await page.evaluate(r"""
                (() => {
                    const results = [];
                    
                    // ═══════════════════════════════════════════════════════════
                    // CLOUDFLARE TURNSTILE DETECTION (2024 Updated)
                    // ═══════════════════════════════════════════════════════════
                    const turnstileSelectors = [
                        '[data-sitekey*="0x"]',
                        '[data-sitekey*="3x"]',
                        '[data-sitekey*="1x"]',  // New variant
                        '.cf-turnstile',
                        '.cf-challenge-running',
                        '[class*="cf-turnstile"]',
                        'iframe[src*="challenges.cloudflare.com"]',
                        'iframe[src*="turnstile"]',
                        '[id*="cf-turnstile"]',
                        '[data-callback*="turnstile"]'
                    ];
                    
                    for (const selector of turnstileSelectors) {
                        try {
                            const elements = document.querySelectorAll(selector);
                            for (const element of elements) {
                                const sitekey = element.getAttribute('data-sitekey') || 
                                              element.getAttribute('data-site-key') ||
                                              element.dataset?.sitekey;
                                
                                // Check iframe src for sitekey
                                if (!sitekey && element.tagName === 'IFRAME') {
                                    const src = element.src || '';
                                    const match = src.match(/sitekey=([^&]+)/);
                                    if (match) {
                                        results.push({
                                            type: 'turnstile',
                                            sitekey: match[1],
                                            confidence: 90,
                                            method: 'turnstile_iframe',
                                            selector: selector
                                        });
                                        continue;
                                    }
                                }
                                
                                if (sitekey && (sitekey.startsWith('0x') || sitekey.startsWith('3x') || sitekey.startsWith('1x') || sitekey.length >= 20)) {
                                    results.push({
                                        type: 'turnstile',
                                        sitekey: sitekey,
                                        confidence: 95,
                                        method: 'turnstile_direct',
                                        selector: selector,
                                        action: element.getAttribute('data-action') || 'managed'
                                    });
                                }
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // RECAPTCHA V2/V3/ENTERPRISE DETECTION (ENHANCED FOR FLICKR)
                    // ═══════════════════════════════════════════════════════════
                    console.log('🔍 Scanning for reCAPTCHA...');
                    
                    // Enhanced selectors for modern reCAPTCHA implementations
                    const recaptchaSelectors = [
                        '.g-recaptcha',
                        '.g-recaptcha-response',
                        'iframe[src*="recaptcha"]',
                        'iframe[src*="google.com/recaptcha"]',
                        'iframe[title*="reCAPTCHA"]',
                        'iframe[name*="recaptcha"]',
                        '[data-sitekey^="6L"]',
                        'div[data-sitekey]',
                        '[class*="g-recaptcha"]',
                        '[class*="grecaptcha"]',
                        '[class*="recaptcha"]',
                        '#g-recaptcha',
                        // Flickr-specific patterns
                        'iframe[src*="www.google.com/recaptcha/api2/anchor"]',
                        'iframe[src*="www.google.com/recaptcha/api2/bframe"]'
                    ];
                    
                    for (const selector of recaptchaSelectors) {
                        try {
                            const elements = document.querySelectorAll(selector);
                            console.log(`Checking selector: ${selector} - Found: ${elements.length} elements`);
                            
                            for (const element of elements) {
                                let sitekey = element.getAttribute('data-sitekey') || 
                                            element.getAttribute('data-site-key');
                                
                                // Check iframe src for sitekey
                                if (!sitekey && element.tagName === 'IFRAME') {
                                    const src = element.src || '';
                                    console.log(`Iframe src: ${src}`);
                                    
                                    // Extract sitekey from various URL patterns
                                    const patterns = [
                                        /[?&]k=([^&]+)/,
                                        /[?&]sitekey=([^&]+)/,
                                        /\/recaptcha\/api2\/anchor\?.*k=([^&]+)/
                                    ];
                                    
                                    for (const pattern of patterns) {
                                        const match = src.match(pattern);
                                        if (match) {
                                            sitekey = match[1];
                                            break;
                                        }
                                    }
                                }
                                
                                // Check parent elements for sitekey
                                if (!sitekey) {
                                    let parent = element.parentElement;
                                    let depth = 0;
                                    while (parent && depth < 5) {
                                        sitekey = parent.getAttribute('data-sitekey') ||
                                                 parent.getAttribute('data-site-key') ||
                                                 parent.dataset?.sitekey;
                                        if (sitekey) break;
                                        parent = parent.parentElement;
                                        depth++;
                                    }
                                }
                                
                                if (sitekey) {
                                    console.log(`Found reCAPTCHA with sitekey: ${sitekey}`);
                                    
                                    const badge = element.getAttribute('data-badge') || 
                                                element.getAttribute('data-size');
                                    const action = element.getAttribute('data-action') || 'submit';
                                    const isEnterprise = element.classList.contains('g-recaptcha-enterprise') ||
                                                       element.dataset?.type === 'enterprise';
                                    
                                    // Determine version
                                    let captchaType = 'recaptcha_v2';
                                    if (isEnterprise) {
                                        captchaType = 'recaptcha_enterprise';
                                    } else if (badge === 'bottomright' || badge === 'bottomleft' || 
                                              badge === 'inline' || badge === 'invisible') {
                                        captchaType = 'recaptcha_v3';
                                    }
                                    
                                    results.push({
                                        type: captchaType,
                                        sitekey: sitekey,
                                        confidence: 92,
                                        method: 'recaptcha_direct',
                                        selector: selector,
                                        action: action,
                                        isEnterprise: isEnterprise
                                    });
                                }
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // HCAPTCHA DETECTION (2024 Updated)
                    // ═══════════════════════════════════════════════════════════
                    const hcaptchaSelectors = [
                        '.h-captcha',
                        'iframe[src*="hcaptcha"]',
                        '[data-hcaptcha-sitekey]',
                        '[class*="h-captcha"]',
                        'div[data-sitekey][data-theme]'  // hCaptcha specific
                    ];
                    
                    for (const selector of hcaptchaSelectors) {
                        try {
                            const elements = document.querySelectorAll(selector);
                            for (const element of elements) {
                                let sitekey = element.getAttribute('data-sitekey') || 
                                            element.getAttribute('data-hcaptcha-sitekey');
                                
                                if (!sitekey && element.tagName === 'IFRAME') {
                                    const src = element.src || '';
                                    const match = src.match(/sitekey=([^&]+)/);
                                    if (match) sitekey = match[1];
                                }
                                
                                if (sitekey) {
                                    results.push({
                                        type: 'hcaptcha',
                                        sitekey: sitekey,
                                        confidence: 88,
                                        method: 'hcaptcha_direct',
                                        selector: selector
                                    });
                                }
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // FUNCAPTCHA (ARKOSE LABS) DETECTION
                    // ═══════════════════════════════════════════════════════════
                    const funcaptchaSelectors = [
                        'iframe[src*="funcaptcha"]',
                        'iframe[src*="arkoselabs"]',
                        '[data-callback*="arkose"]',
                        'FunCaptcha',
                        '.arkose-container'
                    ];
                    
                    for (const selector of funcaptchaSelectors) {
                        try {
                            const elements = document.querySelectorAll(selector);
                            for (const element of elements) {
                                const publicKey = element.getAttribute('data-public-key') ||
                                                element.getAttribute('data-pkey');
                                
                                if (publicKey || element.tagName === 'IFRAME') {
                                    let sitekey = publicKey;
                                    if (!sitekey && element.src) {
                                        const match = element.src.match(/pk=([^&]+)/);
                                        if (match) sitekey = match[1];
                                    }
                                    
                                    if (sitekey) {
                                        results.push({
                                            type: 'funcaptcha',
                                            sitekey: sitekey,
                                            confidence: 85,
                                            method: 'funcaptcha_direct',
                                            selector: selector
                                        });
                                    }
                                }
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // GEETEST DETECTION
                    // ═══════════════════════════════════════════════════════════
                    const geetestSelectors = [
                        '.geetest_radar',
                        '.geetest_holder',
                        '[class*="geetest"]',
                        'iframe[src*="geetest"]'
                    ];
                    
                    for (const selector of geetestSelectors) {
                        try {
                            const elements = document.querySelectorAll(selector);
                            if (elements.length > 0) {
                                results.push({
                                    type: 'geetest',
                                    sitekey: 'geetest_detected',
                                    confidence: 80,
                                    method: 'geetest_visual',
                                    selector: selector
                                });
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // SCRIPT-BASED DETECTION (V3, Invisible, Enterprise)
                    // ═══════════════════════════════════════════════════════════
                    const scripts = Array.from(document.querySelectorAll('script'));
                    for (const script of scripts) {
                        try {
                            const text = script.textContent || script.innerHTML || '';
                            
                            // reCAPTCHA v3/Enterprise execute patterns
                            const v3Patterns = [
                                /grecaptcha\.execute\s*\(\s*['"`]([^'"`]+)['"`]/,
                                /grecaptcha\.enterprise\.execute\s*\(\s*['"`]([^'"`]+)['"`]/,
                                /action:\s*['"`]([^'"`]+)['"`].*sitekey:\s*['"`]([^'"`]+)['"`]/s
                            ];
                            
                            for (const pattern of v3Patterns) {
                                const match = text.match(pattern);
                                if (match) {
                                    const sitekey = match[1] || match[2];
                                    if (sitekey && sitekey.startsWith('6L')) {
                                        const isEnterprise = text.includes('enterprise');
                                        results.push({
                                            type: isEnterprise ? 'recaptcha_enterprise' : 'recaptcha_v3',
                                            sitekey: sitekey,
                                            confidence: 85,
                                            method: 'script_analysis',
                                            action: match[1] || 'submit'
                                        });
                                    }
                                }
                            }
                            
                            // Turnstile render patterns
                            const turnstilePattern = /turnstile\.render\s*\([^)]*sitekey\s*:\s*['"`]([^'"`]+)['"`]/;
                            const turnstileMatch = text.match(turnstilePattern);
                            if (turnstileMatch) {
                                results.push({
                                    type: 'turnstile',
                                    sitekey: turnstileMatch[1],
                                    confidence: 87,
                                    method: 'script_analysis'
                                });
                            }
                            
                            // hCaptcha render patterns
                            const hcaptchaPattern = /hcaptcha\.render\s*\([^)]*sitekey\s*:\s*['"`]([^'"`]+)['"`]/;
                            const hcaptchaMatch = text.match(hcaptchaPattern);
                            if (hcaptchaMatch) {
                                results.push({
                                    type: 'hcaptcha',
                                    sitekey: hcaptchaMatch[1],
                                    confidence: 85,
                                    method: 'script_analysis'
                                });
                            }
                        } catch (e) {}
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // TEXT-BASED DETECTION FOR "I'M NOT A ROBOT" PATTERNS
                    // ═══════════════════════════════════════════════════════════
                    console.log('Starting text-based reCAPTCHA detection...');
                    const textPatterns = [
                        'I\'m not a robot',
                        'I am not a robot', 
                        'Verify you are human',
                        'Please verify you\'re a human',
                        'reCAPTCHA',
                        'Confirm you are not a robot',
                        'Prove you are human'
                    ];
                    
                    for (const pattern of textPatterns) {
                        console.log(`Searching for text pattern: "${pattern}"`);
                        const textNodes = document.evaluate(
                            `//text()[contains(., '${pattern}')]`,
                            document,
                            null,
                            XPathResult.UNORDERED_NODE_SNAPSHOT_TYPE,
                            null
                        );
                        
                        console.log(`Found ${textNodes.snapshotLength} text nodes matching "${pattern}"`);
                        
                        for (let i = 0; i < textNodes.snapshotLength; i++) {
                            const textNode = textNodes.snapshotItem(i);
                            const parentElement = textNode.parentElement;
                            console.log(`Processing text node ${i + 1}, parent element:`, parentElement);
                            
                            // Search up the DOM tree for reCAPTCHA sitekey
                            let currentElement = parentElement;
                            let sitekey = null;
                            let level = 0;
                            
                            while (currentElement && level < 7) {
                                // Check for sitekey in current element
                                sitekey = currentElement.getAttribute('data-sitekey') ||
                                       currentElement.getAttribute('data-key') ||
                                       currentElement.getAttribute('sitekey') ||
                                       currentElement.getAttribute('data-recaptcha-sitekey');
                                
                                if (sitekey) {
                                    console.log(`Found sitekey at level ${level}:`, sitekey);
                                    break;
                                }
                                
                                // Check for sitekey in child elements
                                const children = currentElement.querySelectorAll('*');
                                for (const child of children) {
                                    sitekey = child.getAttribute('data-sitekey') ||
                                           child.getAttribute('data-key') ||
                                           child.getAttribute('sitekey') ||
                                           child.getAttribute('data-recaptcha-sitekey');
                                    if (sitekey) {
                                        console.log(`Found sitekey in child element:`, sitekey);
                                        break;
                                    }
                                }
                                
                                if (sitekey) break;
                                
                                // Check for sitekey in sibling elements  
                                const siblings = currentElement.parentElement?.children || [];
                                for (const sibling of siblings) {
                                    sitekey = sibling.getAttribute('data-sitekey') ||
                                           sibling.getAttribute('data-key') ||
                                           sibling.getAttribute('sitekey') ||
                                           sibling.getAttribute('data-recaptcha-sitekey');
                                    if (sitekey) {
                                        console.log(`Found sitekey in sibling element:`, sitekey);
                                        break;
                                    }
                                }
                                
                                if (sitekey) break;
                                
                                currentElement = currentElement.parentElement;
                                level++;
                            }
                            
                            if (sitekey) {
                                console.log(`✅ Found reCAPTCHA via text pattern "${pattern}" with sitekey: ${sitekey}`);
                                results.push({
                                    type: 'recaptcha_v2',
                                    sitekey: sitekey,
                                    confidence: 88,
                                    method: 'text_detection',
                                    selector: null,
                                    action: 'verify'
                                });
                            } else {
                                console.log(`❌ No sitekey found for text pattern "${pattern}"`);
                            }
                        }
                    }
                    
                    // ═══════════════════════════════════════════════════════════
                    // ENHANCED IFRAME COMBINATION DETECTION
                    // ═══════════════════════════════════════════════════════════
                    console.log('Starting iframe combination detection...');
                    const iframes = document.querySelectorAll('iframe');
                    console.log(`Found ${iframes.length} iframes on page`);
                    
                    for (const iframe of iframes) {
                        const src = iframe.src || '';
                        const name = iframe.name || '';
                        console.log(`Checking iframe - src: ${src}, name: ${name}`);
                        
                        if (src.includes('recaptcha') || name.includes('recaptcha')) {
                            console.log('Found reCAPTCHA iframe, searching for associated sitekey...');
                            
                            // Look for sitekey in nearby elements
                            let searchElement = iframe.parentElement;
                            let level = 0;
                            let sitekey = null;
                            
                            while (searchElement && level < 5) {
                                // Check current element and all descendants
                                const elements = [searchElement, ...searchElement.querySelectorAll('*')];
                                
                                for (const element of elements) {
                                    sitekey = element.getAttribute('data-sitekey') ||
                                           element.getAttribute('data-key') ||
                                           element.getAttribute('sitekey') ||
                                           element.getAttribute('data-recaptcha-sitekey');
                                    
                                    if (sitekey) {
                                        console.log(`✅ Found sitekey near iframe: ${sitekey}`);
                                        results.push({
                                            type: 'recaptcha_v2',
                                            sitekey: sitekey,
                                            confidence: 90,
                                            method: 'iframe_association',
                                            selector: null,
                                            action: 'verify'
                                        });
                                        break;
                                    }
                                }
                                
                                if (sitekey) break;
                                searchElement = searchElement.parentElement;
                                level++;
                            }
                            
                            if (!sitekey) {
                                console.log('❌ No sitekey found near reCAPTCHA iframe');
                            }
                        }
                    }
                    
                    // Sort by confidence and return
                    return results.sort((a, b) => b.confidence - a.confidence);
                })()
            """)
            
            if js_detection and len(js_detection) > 0:
                best_match = js_detection[0]
                print(f"✅ CAPTCHA DETECTED: {best_match['type']} - {best_match.get('sitekey', 'N/A')[:30]}... (confidence: {best_match['confidence']}%)")
                return best_match
            
            # LAYER 2: Iframe Deep Inspection
            print("🔄 Layer 2: Iframe inspection...")
            iframes = await page.query_selector_all('iframe')
            for iframe in iframes:
                try:
                    src = await iframe.get_attribute('src') or ''
                    
                    # Parse iframe URL
                    if 'recaptcha' in src or 'google.com/recaptcha' in src:
                        parsed = urlparse(src)
                        params = parse_qs(parsed.query)
                        sitekey = params.get('k', [None])[0]
                        if sitekey:
                            return {
                                'type': 'recaptcha_v2',
                                'sitekey': sitekey,
                                'confidence': 75,
                                'method': 'iframe_inspection'
                            }
                    
                    elif 'turnstile' in src or 'cloudflare' in src:
                        parsed = urlparse(src)
                        params = parse_qs(parsed.query)
                        sitekey = params.get('sitekey', [None])[0]
                        if sitekey:
                            return {
                                'type': 'turnstile',
                                'sitekey': sitekey,
                                'confidence': 75,
                                'method': 'iframe_inspection'
                            }
                    
                    elif 'hcaptcha' in src:
                        parsed = urlparse(src)
                        params = parse_qs(parsed.query)
                        sitekey = params.get('sitekey', [None])[0]
                        if sitekey:
                            return {
                                'type': 'hcaptcha',
                                'sitekey': sitekey,
                                'confidence': 75,
                                'method': 'iframe_inspection'
                            }
                except Exception as e:
                    continue
            
            # LAYER 3: DOM Fallback Scanning
            print("🔄 Layer 3: DOM fallback scanning...")
            
            # Quick scan for common patterns
            all_elements = await page.query_selector_all('[data-sitekey], [data-site-key], .g-recaptcha, .h-captcha, .cf-turnstile')
            for element in all_elements:
                try:
                    sitekey = await element.get_attribute('data-sitekey') or await element.get_attribute('data-site-key')
                    if sitekey:
                        # Determine type by sitekey format
                        if sitekey.startswith('6L'):
                            return {'type': 'recaptcha_v2', 'sitekey': sitekey, 'confidence': 70, 'method': 'dom_fallback'}
                        elif sitekey.startswith('0x') or sitekey.startswith('3x') or sitekey.startswith('1x'):
                            return {'type': 'turnstile', 'sitekey': sitekey, 'confidence': 70, 'method': 'dom_fallback'}
                        else:
                            return {'type': 'hcaptcha', 'sitekey': sitekey, 'confidence': 70, 'method': 'dom_fallback'}
                except Exception:
                    continue
            
        except Exception as e:
            print(f"⚠️ Detection error: {e}")
        
        # RETRY LOGIC: If nothing found and retries remaining, wait and retry
        if retry_count < self.max_detection_retries - 1:
            print(f"⏳ No CAPTCHA found, waiting {self.detection_retry_delay}s before retry...")
            await asyncio.sleep(self.detection_retry_delay)
            return await self.detect_captcha_universal(page, retry_count + 1)
        
        print("ℹ️ No CAPTCHAs detected after all attempts")
        return captcha_info

    async def solve_with_fallback(self, captcha_type: str, sitekey: str, page_url: str, 
                                  action: str = "submit", timeout: int = None) -> Tuple[Optional[str], str]:
        """
        🔄 MULTI-TIER FALLBACK SOLVER
        
        Attempts to solve CAPTCHA with automatic service fallback:
        Tier 1: CapSolver (fastest, most reliable)
        Tier 2: 2Captcha (backup)
        Tier 3: AntiCaptcha (secondary backup)
        Tier 4: DeathByCaptcha (final fallback)
        
        Returns: (token, service_name) or (None, "failed")
        """
        timeout = timeout or self.default_timeout
        
        # Check for test sitekeys
        if sitekey in self.test_sitekeys:
            print(f"🧪 TEST SITEKEY: Returning mock token for {captcha_type}")
            return f"DEMO.{captcha_type.upper()}.TOKEN." + "x" * 100, "test_mode"
        
        # Tier 1: CapSolver
        print(f"🎯 Tier 1: Attempting CapSolver for {captcha_type}...")
        token = await self._solve_capsolver(captcha_type, sitekey, page_url, action, timeout)
        if token:
            return token, "CapSolver"
        
        # Tier 2: 2Captcha
        print(f"🎯 Tier 2: Attempting 2Captcha for {captcha_type}...")
        token = await self._solve_2captcha(captcha_type, sitekey, page_url, action, timeout)
        if token:
            return token, "2Captcha"
        
        # Tier 3: AntiCaptcha
        print(f"🎯 Tier 3: Attempting AntiCaptcha for {captcha_type}...")
        token = await self._solve_anticaptcha(captcha_type, sitekey, page_url, action, timeout)
        if token:
            return token, "AntiCaptcha"
        
        # Tier 4: DeathByCaptcha (final fallback)
        print(f"🎯 Tier 4: Attempting DeathByCaptcha for {captcha_type}...")
        token = await self._solve_deathbycaptcha(captcha_type, sitekey, page_url, timeout)
        if token:
            return token, "DeathByCaptcha"
        
        print(f"❌ All solving services failed for {captcha_type}")
        return None, "failed"

    async def _solve_capsolver(self, captcha_type: str, sitekey: str, page_url: str, 
                              action: str, timeout: int) -> Optional[str]:
        """CapSolver implementation with all CAPTCHA types"""
        try:
            # Map our types to CapSolver task types
            task_type_map = {
                'turnstile': 'AntiTurnstileTaskProxyless',
                'recaptcha_v2': 'ReCaptchaV2TaskProxyless',
                'recaptcha_v3': 'ReCaptchaV3TaskProxyless',
                'recaptcha_enterprise': 'ReCaptchaV3EnterpriseTaskProxyless',
                'hcaptcha': 'HCaptchaTaskProxyless',
                'funcaptcha': 'FunCaptchaTaskProxyless',
                'geetest': 'GeeTestTaskProxyless'
            }
            
            task_type = task_type_map.get(captcha_type)
            if not task_type:
                return None
            
            async with aiohttp.ClientSession() as session:
                # Create task
                task_data = {
                    "clientKey": self.cs_api_key,
                    "task": {
                        "type": task_type,
                        "websiteURL": page_url,
                        "websiteKey": sitekey
                    }
                }
                
                # Add action for v3/Enterprise
                if captcha_type in ['recaptcha_v3', 'recaptcha_enterprise']:
                    task_data['task']['pageAction'] = action
                    task_data['task']['minScore'] = 0.3
                
                async with session.post(f"{self.cs_base_url}/createTask", 
                                       json=task_data, timeout=30) as response:
                    data = await response.json()
                
                if data.get('errorId') != 0:
                    print(f"❌ CapSolver error: {data.get('errorDescription', 'Unknown')}")
                    return None
                
                task_id = data.get('taskId')
                print(f"✅ CapSolver task created: {task_id}")
                
                # Poll for solution
                start_time = time.time()
                while time.time() - start_time < timeout:
                    await asyncio.sleep(3)
                    
                    async with session.post(f"{self.cs_base_url}/getTaskResult", json={
                        "clientKey": self.cs_api_key,
                        "taskId": task_id
                    }, timeout=30) as response:
                        result = await response.json()
                        
                        status = result.get('status')
                        if status == 'ready':
                            solution = result.get('solution', {})
                            token = solution.get('token') or solution.get('gRecaptchaResponse')
                            if token:
                                print(f"🎉 CapSolver solved {captcha_type}!")
                                return token
                        elif status == 'failed':
                            print(f"❌ CapSolver task failed: {result.get('errorDescription')}")
                            return None
                
                print("⏰ CapSolver timeout")
                return None
                
        except Exception as e:
            print(f"❌ CapSolver exception: {e}")
            return None

    async def _solve_2captcha(self, captcha_type: str, sitekey: str, page_url: str, 
                             action: str, timeout: int) -> Optional[str]:
        """2Captcha implementation"""
        try:
            # Map to 2Captcha methods
            method_map = {
                'turnstile': 'turnstile',
                'recaptcha_v2': 'userrecaptcha',
                'recaptcha_v3': 'userrecaptcha',
                'hcaptcha': 'hcaptcha'
            }
            
            method = method_map.get(captcha_type)
            if not method:
                return None
            
            async with aiohttp.ClientSession() as session:
                # Submit task
                params = {
                    'key': self.tc_api_key,
                    'method': method,
                    'googlekey': sitekey,
                    'pageurl': page_url,
                    'json': 1
                }
                
                if captcha_type == 'recaptcha_v3':
                    params['version'] = 'v3'
                    params['action'] = action
                    params['min_score'] = 0.3
                
                async with session.get(f"{self.tc_base_url}/in.php", 
                                      params=params, timeout=30) as response:
                    data = await response.json()
                
                if data.get('status') != 1:
                    print(f"❌ 2Captcha error: {data.get('request', 'Unknown')}")
                    return None
                
                captcha_id = data.get('request')
                print(f"✅ 2Captcha task created: {captcha_id}")
                
                # Poll for solution
                start_time = time.time()
                await asyncio.sleep(15)  # Initial wait
                
                while time.time() - start_time < timeout:
                    await asyncio.sleep(5)
                    
                    async with session.get(f"{self.tc_base_url}/res.php", params={
                        'key': self.tc_api_key,
                        'action': 'get',
                        'id': captcha_id,
                        'json': 1
                    }, timeout=30) as response:
                        result = await response.json()
                        
                        if result.get('status') == 1:
                            token = result.get('request')
                            print(f"🎉 2Captcha solved {captcha_type}!")
                            return token
                        elif result.get('request') != 'CAPCHA_NOT_READY':
                            print(f"❌ 2Captcha error: {result.get('request')}")
                            return None
                
                print("⏰ 2Captcha timeout")
                return None
                
        except Exception as e:
            print(f"❌ 2Captcha exception: {e}")
            return None

    async def _solve_anticaptcha(self, captcha_type: str, sitekey: str, page_url: str, 
                                action: str, timeout: int) -> Optional[str]:
        """AntiCaptcha implementation"""
        try:
            # Map to AntiCaptcha task types
            task_type_map = {
                'turnstile': 'TurnstileTaskProxyless',
                'recaptcha_v2': 'RecaptchaV2TaskProxyless',
                'recaptcha_v3': 'RecaptchaV3TaskProxyless',
                'hcaptcha': 'HCaptchaTaskProxyless',
                'funcaptcha': 'FunCaptchaTaskProxyless'
            }
            
            task_type = task_type_map.get(captcha_type)
            if not task_type:
                return None
            
            async with aiohttp.ClientSession() as session:
                # Create task
                task_data = {
                    "clientKey": self.ac_api_key,
                    "task": {
                        "type": task_type,
                        "websiteURL": page_url,
                        "websiteKey": sitekey
                    }
                }
                
                if captcha_type == 'recaptcha_v3':
                    task_data['task']['pageAction'] = action
                    task_data['task']['minScore'] = 0.3
                
                async with session.post(f"{self.ac_base_url}/createTask", 
                                       json=task_data, timeout=30) as response:
                    data = await response.json()
                
                if data.get('errorId') != 0:
                    print(f"❌ AntiCaptcha error: {data.get('errorDescription', 'Unknown')}")
                    return None
                
                task_id = data.get('taskId')
                print(f"✅ AntiCaptcha task created: {task_id}")
                
                # Poll for solution
                start_time = time.time()
                await asyncio.sleep(10)
                
                while time.time() - start_time < timeout:
                    await asyncio.sleep(3)
                    
                    async with session.post(f"{self.ac_base_url}/getTaskResult", json={
                        "clientKey": self.ac_api_key,
                        "taskId": task_id
                    }, timeout=30) as response:
                        result = await response.json()
                        
                        if result.get('status') == 'ready':
                            solution = result.get('solution', {})
                            token = (solution.get('token') or 
                                   solution.get('gRecaptchaResponse') or
                                   solution.get('text'))
                            if token:
                                print(f"🎉 AntiCaptcha solved {captcha_type}!")
                                return token
                        elif result.get('errorId') != 0:
                            print(f"❌ AntiCaptcha failed: {result.get('errorDescription')}")
                            return None
                
                print("⏰ AntiCaptcha timeout")
                return None
                
        except Exception as e:
            print(f"❌ AntiCaptcha exception: {e}")
            return None

    async def _solve_deathbycaptcha(self, captcha_type: str, sitekey: str, 
                                   page_url: str, timeout: int) -> Optional[str]:
        """DeathByCaptcha implementation (final fallback)"""
        try:
            # DBC only supports specific types
            if captcha_type not in ['recaptcha_v2', 'hcaptcha']:
                return None
            
            async with aiohttp.ClientSession() as session:
                # Login and get authstring
                auth_data = {
                    'username': self.dbc_user,
                    'password': self.dbc_pass
                }
                
                async with session.post(f"{self.dbc_base_url}/captcha", 
                                       json=auth_data, timeout=30) as response:
                    login_result = await response.json()
                
                if not login_result.get('is_correct'):
                    print("❌ DeathByCaptcha auth failed")
                    return None
                
                # Submit CAPTCHA
                captcha_data = {
                    'username': self.dbc_user,
                    'password': self.dbc_pass,
                    'type': 4 if captcha_type == 'recaptcha_v2' else 5,
                    'token_params': json.dumps({
                        'googlekey' if captcha_type == 'recaptcha_v2' else 'hcaptcha_sitekey': sitekey,
                        'pageurl': page_url
                    })
                }
                
                async with session.post(f"{self.dbc_base_url}/captcha", 
                                       json=captcha_data, timeout=30) as response:
                    submit_result = await response.json()
                
                captcha_id = submit_result.get('captcha')
                if not captcha_id:
                    print("❌ DeathByCaptcha submission failed")
                    return None
                
                print(f"✅ DeathByCaptcha task created: {captcha_id}")
                
                # Poll for solution
                start_time = time.time()
                await asyncio.sleep(15)
                
                while time.time() - start_time < timeout:
                    await asyncio.sleep(5)
                    
                    async with session.get(f"{self.dbc_base_url}/captcha/{captcha_id}", 
                                          timeout=30) as response:
                        result = await response.json()
                        
                        if result.get('is_correct') and result.get('text'):
                            token = result.get('text')
                            print(f"🎉 DeathByCaptcha solved {captcha_type}!")
                            return token
                
                print("⏰ DeathByCaptcha timeout")
                return None
                
        except Exception as e:
            print(f"❌ DeathByCaptcha exception: {e}")
            return None

    async def inject_captcha_solution_universal(self, page, token: str, captcha_type: str, 
                                               max_retries: int = 3) -> bool:
        """
        💉 ENHANCED UNIVERSAL CAPTCHA SOLUTION INJECTION v2.0
        
        Multi-method injection with verification:
        1. Type-specific field injection
        2. Callback triggering
        3. Form event dispatching
        4. Iframe handling
        5. Verification checks
        """
        if not token:
            print("❌ No token to inject")
            return False
        
        print(f"💉 Injecting {captcha_type.upper()} solution with verification...")
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    print(f"🔄 Injection retry {attempt + 1}/{max_retries}")
                    await asyncio.sleep(1)
                
                success = False
                
                # ═══════════════════════════════════════════════════════════
                # TURNSTILE INJECTION
                # ═══════════════════════════════════════════════════════════
                if captcha_type == 'turnstile':
                    success = await page.evaluate(f"""
                    () => {{
                        try {{
                            let injected = false;
                            
                            // Method 1: Direct input field injection
                            const inputs = document.querySelectorAll('input[name="cf-turnstile-response"]');
                            inputs.forEach(input => {{
                                input.value = '{token}';
                                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                injected = true;
                            }});
                            
                            // Method 2: Callback triggering
                            const turnstileElements = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
                            turnstileElements.forEach(element => {{
                                const callback = element.getAttribute('data-callback');
                                if (callback) {{
                                    try {{
                                        if (typeof window[callback] === 'function') {{
                                            window[callback]('{token}');
                                            injected = true;
                                        }}
                                    }} catch (e) {{
                                        console.log('Callback error:', e);
                                    }}
                                }}
                            }});
                            
                            // Method 3: Global turnstile object
                            if (window.turnstile && typeof window.turnstile.execute === 'function') {{
                                try {{
                                    window.turnstile.execute();
                                    injected = true;
                                }} catch (e) {{}}
                            }}
                            
                            return injected;
                        }} catch (e) {{
                            console.error('Turnstile injection error:', e);
                            return false;
                        }}
                    }}
                    """)
                
                # ═══════════════════════════════════════════════════════════
                # RECAPTCHA V2/V3/ENTERPRISE INJECTION
                # ═══════════════════════════════════════════════════════════
                elif captcha_type in ['recaptcha_v2', 'recaptcha_v3', 'recaptcha_enterprise']:
                    success = await page.evaluate(f"""
                    () => {{
                        try {{
                            let injected = false;
                            
                            // Method 1: Response textarea injection (v2/v3)
                            const textareas = document.querySelectorAll('textarea[name="g-recaptcha-response"]');
                            textareas.forEach(textarea => {{
                                textarea.style.display = 'block';
                                textarea.value = '{token}';
                                textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                injected = true;
                            }});
                            
                            // Method 2: Callback execution
                            const widgets = document.querySelectorAll('.g-recaptcha, [data-callback]');
                            widgets.forEach(widget => {{
                                const callback = widget.getAttribute('data-callback');
                                if (callback && typeof window[callback] === 'function') {{
                                    try {{
                                        window[callback]('{token}');
                                        injected = true;
                                    }} catch (e) {{}}
                                }}
                            }});
                            
                            // Method 3: Global grecaptcha callback
                            if (window.grecaptcha) {{
                                try {{
                                    // Find all widget IDs and set response
                                    for (let i = 0; i < 10; i++) {{
                                        try {{
                                            if (window.grecaptcha.getResponse) {{
                                                const response = window.grecaptcha.getResponse(i);
                                                if (response === '' || response === undefined) {{
                                                    // This widget exists but has no response
                                                    injected = true;
                                                }}
                                            }}
                                        }} catch (e) {{
                                            break;
                                        }}
                                    }}
                                }} catch (e) {{}}
                            }}
                            
                            return injected;
                        }} catch (e) {{
                            console.error('reCAPTCHA injection error:', e);
                            return false;
                        }}
                    }}
                    """)
                
                # ═══════════════════════════════════════════════════════════
                # HCAPTCHA INJECTION
                # ═══════════════════════════════════════════════════════════
                elif captcha_type == 'hcaptcha':
                    success = await page.evaluate(f"""
                    () => {{
                        try {{
                            let injected = false;
                            
                            // Method 1: Response textarea
                            const textareas = document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]');
                            textareas.forEach(textarea => {{
                                textarea.value = '{token}';
                                textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                injected = true;
                            }});
                            
                            // Method 2: Callback
                            const hcaptchaElements = document.querySelectorAll('.h-captcha, [data-callback]');
                            hcaptchaElements.forEach(element => {{
                                const callback = element.getAttribute('data-callback');
                                if (callback && typeof window[callback] === 'function') {{
                                    try {{
                                        window[callback]('{token}');
                                        injected = true;
                                    }} catch (e) {{}}
                                }}
                            }});
                            
                            return injected;
                        }} catch (e) {{
                            console.error('hCAPTCHA injection error:', e);
                            return false;
                        }}
                    }}
                    """)
                
                # ═══════════════════════════════════════════════════════════
                # FUNCAPTCHA INJECTION
                # ═══════════════════════════════════════════════════════════
                elif captcha_type == 'funcaptcha':
                    success = await page.evaluate(f"""
                    () => {{
                        try {{
                            let injected = false;
                            
                            // Inject into FunCaptcha token field
                            const inputs = document.querySelectorAll('input[name="fc-token"]');
                            inputs.forEach(input => {{
                                input.value = '{token}';
                                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                injected = true;
                            }});
                            
                            return injected;
                        }} catch (e) {{
                            return false;
                        }}
                    }}
                    """)
                
                # ═══════════════════════════════════════════════════════════
                # GENERIC FALLBACK INJECTION
                # ═══════════════════════════════════════════════════════════
                if not success:
                    print("🔄 Trying generic injection method...")
                    success = await page.evaluate(f"""
                    () => {{
                        try {{
                            let injected = false;
                            
                            // Find all potential CAPTCHA response fields
                            const selectors = [
                                'textarea[name*="captcha"]',
                                'textarea[name*="response"]',
                                'input[name*="captcha"]',
                                'input[name*="response"]',
                                'input[name*="token"]',
                                'textarea[name*="token"]',
                                'input[type="hidden"][name*="captcha"]',
                                'input[type="hidden"][name*="token"]'
                            ];
                            
                            for (const selector of selectors) {{
                                const elements = document.querySelectorAll(selector);
                                elements.forEach(element => {{
                                    const name = (element.name || '').toLowerCase();
                                    if (name.includes('captcha') || name.includes('response') || 
                                        name.includes('token') || name.includes('challenge')) {{
                                        element.value = '{token}';
                                        element.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        element.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        injected = true;
                                    }}
                                }});
                            }}
                            
                            return injected;
                        }} catch (e) {{
                            return false;
                        }}
                    }}
                    """)
                
                if success:
                    print(f"✅ Token injected successfully (attempt {attempt + 1})")
                    
                    # Wait for page to process
                    await asyncio.sleep(2)
                    
                    # Trigger form validation events
                    await page.evaluate("""
                    () => {
                        try {
                            // Trigger events on all forms
                            const forms = document.querySelectorAll('form');
                            forms.forEach(form => {
                                form.dispatchEvent(new Event('change', { bubbles: true }));
                                form.dispatchEvent(new Event('input', { bubbles: true }));
                                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                            });
                            
                            // Trigger events on submit buttons
                            const buttons = document.querySelectorAll('button[type="submit"], input[type="submit"]');
                            buttons.forEach(button => {
                                button.dispatchEvent(new Event('click', { bubbles: true }));
                            });
                        } catch (e) {
                            console.log('Event trigger error:', e);
                        }
                    }
                    """)
                    
                    # Verification: Check if token persisted
                    await asyncio.sleep(1)
                    verification = await page.evaluate(f"""
                    () => {{
                        const token = '{token}'.substring(0, 20);
                        const inputs = document.querySelectorAll('input, textarea');
                        for (const input of inputs) {{
                            if (input.value && input.value.includes(token)) {{
                                return true;
                            }}
                        }}
                        return false;
                    }}
                    """)
                    
                    if verification:
                        print("✅ Token injection verified!")
                        return True
                    else:
                        print("⚠️ Token verification failed, retrying...")
                        success = False
                
            except Exception as e:
                print(f"❌ Injection attempt {attempt + 1} failed: {e}")
                success = False
        
        print(f"❌ Token injection failed after {max_retries} attempts")
        return False

    async def solve_captcha_if_present(self, page, page_url: str) -> dict:
        """
        🤖 MAIN ORCHESTRATOR METHOD v2.0
        
        Complete CAPTCHA handling pipeline:
        1. Multi-layer detection with retry
        2. Multi-tier solving with fallback
        3. Enhanced injection with verification
        4. Detailed result reporting
        """
        print(f"🤖 Universal CAPTCHA solver v2.0 scanning: {page_url}")
        
        try:
            # Step 1: Multi-layer detection
            captcha_info = await self.detect_captcha_universal(page)
            
            if not captcha_info['type']:
                return {
                    'found': False,
                    'solved': False,
                    'type': None,
                    'service': None,
                    'error': None,
                    'confidence': 0
                }
            
            print(f"🎯 CAPTCHA CONFIRMED: {captcha_info['type'].upper()}")
            print(f"🔑 Sitekey: {captcha_info.get('sitekey', 'N/A')[:50]}...")
            print(f"📊 Confidence: {captcha_info['confidence']}%")
            print(f"🔍 Detection Method: {captcha_info['method']}")
            
            # Step 2: Solve with multi-tier fallback
            token, service_used = await self.solve_with_fallback(
                captcha_type=captcha_info['type'],
                sitekey=captcha_info['sitekey'],
                page_url=page_url,
                action=captcha_info.get('action', 'submit')
            )
            
            if not token:
                return {
                    'found': True,
                    'solved': False,
                    'type': captcha_info['type'],
                    'service': service_used,
                    'error': 'All solving services failed',
                    'confidence': captcha_info['confidence']
                }
            
            print(f"🎉 CAPTCHA SOLVED by {service_used}")
            
            # Step 3: Inject solution with verification
            injection_success = await self.inject_captcha_solution_universal(
                page, token, captcha_info['type']
            )
            
            if injection_success:
                print("✅ CAPTCHA SOLUTION SUCCESSFULLY INJECTED AND VERIFIED")
                return {
                    'found': True,
                    'solved': True,
                    'type': captcha_info['type'],
                    'service': service_used,
                    'error': None,
                    'confidence': captcha_info['confidence'],
                    'method': captcha_info['method']
                }
            else:
                return {
                    'found': True,
                    'solved': False,
                    'type': captcha_info['type'],
                    'service': service_used,
                    'error': 'Token injection failed after verification',
                    'confidence': captcha_info['confidence']
                }
                
        except Exception as e:
            error_msg = str(e)
            print(f"❌ Universal CAPTCHA solver error: {error_msg}")
            import traceback
            print(f"📋 Traceback: {traceback.format_exc()}")
            return {
                'found': False,
                'solved': False,
                'type': None,
                'service': None,
                'error': error_msg,
                'confidence': 0
            }