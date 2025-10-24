import base64
import json
import requests
import redis
from dotenv import load_dotenv
import os

load_dotenv()

# Redis connection for API key management
def get_redis_connection():
    """Get Redis connection for API key management"""
    try:
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", 6379))
        redis_password = os.getenv("REDIS_PASSWORD", None)
        
        r = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5
        )
        
        # Test connection
        r.ping()
        return r
    except Exception as e:
        print(f"Redis connection failed for API keys: {e}")
        return None

redis_client = get_redis_connection()

def initialize_api_keys():
    """Initialize API keys in Redis if not already present"""
    if redis_client:
        try:
            # Check if API keys are already stored
            if not redis_client.exists("ocr:api_keys"):
                # Get API keys from environment and store in Redis
                api_keys_str = os.getenv("gemini-api-key", "")
                if api_keys_str:
                    api_keys = api_keys_str.split(',')
                    redis_client.rpush("ocr:api_keys", *api_keys)
                    redis_client.set("ocr:api_key_index", 0)
                    print(f"✅ Initialized {len(api_keys)} API keys in Redis")
                else:
                    print("❌ No API keys found in environment")
            
            # Initialize index if it doesn't exist
            if not redis_client.exists("ocr:api_key_index"):
                redis_client.set("ocr:api_key_index", 0)
                
        except Exception as e:
            print(f"Error initializing API keys: {e}")

def get_next_api_key():
    """Get next API key from Redis with rotation"""
    if redis_client:
        try:
            # Get current index
            current_index = int(redis_client.get("ocr:api_key_index") or 0)
            
            # Get API keys list
            api_keys = redis_client.lrange("ocr:api_keys", 0, -1)
            
            if not api_keys:
                print("❌ No API keys found in Redis")
                return None
            
            # Get the key at current index
            key = api_keys[current_index % len(api_keys)]
            
            # Increment index for next call
            next_index = (current_index + 1) % len(api_keys)
            redis_client.set("ocr:api_key_index", next_index)
            
            return key
            
        except Exception as e:
            print(f"Error getting API key from Redis: {e}")
            return None
    else:
        # Fallback to old method if Redis is not available
        api_keys_str = os.getenv("gemini-api-key", "")
        if api_keys_str:
            api_keys = api_keys_str.split(',')
            # Simple rotation without state persistence
            import random
            return random.choice(api_keys)
        return None

# Initialize API keys when module is imported
initialize_api_keys()


class ocrBillMaker:
    def __init__(self):
        self.API_KEY = get_next_api_key()
        if not self.API_KEY:
            raise ValueError("No API key available")
        
        print(f"Using API key: {self.API_KEY[:8]}...")  # Only show first 8 chars for security
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={self.API_KEY}"
        self.headers = {"Content-Type": "application/json"}
        self.prompt = (
            "You are a receipt parsing assistant. Extract the following information from the receipt image "
            "and return it as a single line of valid, minified JSON. Do not include any text, explanation, or formatting—only the JSON.\n\n"
            'Expected JSON format: '
            '{"restaurant":"[restaurant name]","date":"[date]","time":"[time]","items":[{"name":"[item name]","price":"₹[price]"}],'
            '"subtotal":"₹[amount]","serviceCharge":"₹[amount]","discount":"₹[amount]","cgst":"₹[amount]","sgst":"₹[amount]","total":"₹[amount]"}\n\n'
            "Rules:\n"
            "1. Output ONLY valid JSON in the format shown above.\n"
            "2. Prices and amounts must include the rupee sign, e.g., \"₹199.00\".\n"
            "3. List all items under the \"items\" array, each with \"name\" and \"price\" keys.\n"
            "4. Use \"N/A\" if any field is missing.\n"
            "5. Do not include any markdown, headings, or explanation—only the JSON object."
        )
    
    def getText(self, file_input):
        """
        Process OCR from either a file path or file-like object
        Args:
            file_input: Can be a file path (str) or file-like object with read() method
        """
#         abc = '''
# {
#   "restaurant": "UD ROTIGHAR",
#   "date": "28/01/2023",
#   "time": "02:50:28 PM",
#   "items": [
#     { "name": "Baby Corn Chilly", "price": "₹200.00" },
#     { "name": "Dal Tadka", "price": "₹155.00" },
#     { "name": "Garlic Nan", "price": "₹455.00" },
#     { "name": "Gobi Manchoorian", "price": "₹160.00" },
#     { "name": "Kaju Paneer (A)", "price": "₹220.00" },
#     { "name": "Minaral Water (A)", "price": "₹19.00" },
#     { "name": "Paneer Tikka Manchoorian", "price": "₹220.00" }
#   ],
#   "subtotal": "₹1,429.00",
#   "serviceCharge": "₹400.00",
#   "discount": "N/A",
#   "cgst": "₹35.00",
#   "sgst": "₹35.00",
#   "total": "₹1,501.00"
# }
# '''     
#         abc=json.loads(abc)
#         return abc
        
        # Handle both file paths and file-like objects
        if isinstance(file_input, str):
            # File path - read from disk
            with open(file_input, "rb") as img_file:
                encoded_image = base64.b64encode(img_file.read()).decode("utf-8")
        else:
            # File-like object - read directly
            file_input.seek(0)  # Ensure we're at the beginning
            encoded_image = base64.b64encode(file_input.read()).decode("utf-8")
        
        # Define the API request payload
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": self.prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": encoded_image
                            }
                        }
                    ]
                }
            ]
        }
        
        response = requests.post(self.url, headers=self.headers, data=json.dumps(payload))
        
        try:
            content = response.json()
            print(content)
            json_text = content["candidates"][0]["content"]["parts"][0]["text"].lstrip("```json\n").rstrip("\n```")
            return json.loads(json_text)
        except Exception as e:
            return {"error": f"Failed to parse response: {e}"}

# a=ocrBillMaker()
# print(a.getText(r"C:\Users\rabhi\OneDrive\Desktop\new"))