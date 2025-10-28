#!/usr/bin/env python3
"""
Test script to validate all error fixes
"""
import asyncio
import sys
from pathlib import Path

# Add current directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

async def test_captcha_import():
    """Test if CAPTCHA module imports without errors"""
    try:
        from captcha import UniversalCaptchaSolver
        print("✅ CAPTCHA module imported successfully")
        
        # Test initialization
        solver = UniversalCaptchaSolver()
        print("✅ UniversalCaptchaSolver initialized successfully")
        
        return True
    except Exception as e:
        print(f"❌ CAPTCHA import/initialization failed: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return False

async def test_main_import():
    """Test if main.py imports without syntax errors"""
    try:
        # Test basic imports from main.py
        import logging
        import json
        import asyncio
        print("✅ Basic main.py dependencies imported successfully")
        
        # Test if we can import from main without syntax errors
        # (we'll skip the full import to avoid dependency issues)
        with open('main.py', 'r') as f:
            content = f.read()
        
        # Simple syntax check by compiling
        compile(content, 'main.py', 'exec')
        print("✅ main.py syntax validation passed")
        
        return True
    except SyntaxError as e:
        print(f"❌ main.py syntax error: {e}")
        return False
    except Exception as e:
        print(f"❌ main.py import test failed: {e}")
        return False

async def test_llm_integration():
    """Test if LLM integration works with top_k extraction"""
    try:
        from llm import get_refined_prompt
        print("✅ LLM module imported successfully")
        
        # Test top_k extraction
        test_query = "Find the top 5 best laptops under $1000"
        try:
            result = await get_refined_prompt(test_query)
            if isinstance(result, tuple) and len(result) == 3:
                refined_query, extracted_top_k, usage = result
                print(f"✅ Top_k extraction working - extracted: {extracted_top_k}")
                print(f"✅ Refined query: {refined_query[:50]}...")
                return True
            else:
                print(f"❌ Unexpected result format: {result}")
                return False
        except Exception as e:
            print(f"❌ LLM prompt refinement failed: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            return False
            
    except Exception as e:
        print(f"❌ LLM import failed: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return False

async def main():
    """Run all tests"""
    print("🧪 Running CAPTCHA and integration tests...")
    print("=" * 50)
    
    tests = [
        ("CAPTCHA Import Test", test_captcha_import),
        ("Main.py Syntax Test", test_main_import),
        ("LLM Integration Test", test_llm_integration),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n🔍 {test_name}:")
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} crashed: {e}")
            results.append((test_name, False))
    
    print("\n" + "=" * 50)
    print("📋 TEST SUMMARY:")
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} - {test_name}")
        if not passed:
            all_passed = False
    
    if all_passed:
        print("\n🎉 All tests passed! System is ready.")
    else:
        print("\n⚠️ Some tests failed. Check the output above.")
    
    return all_passed

if __name__ == "__main__":
    asyncio.run(main())