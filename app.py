import os
import uuid
import string
import random
import redis
from flask import Flask, render_template, request, redirect, url_for, send_file, jsonify, make_response
from werkzeug.utils import secure_filename
from flask_cors import CORS
from PIL import Image
import qrcode
from io import BytesIO
import json
import base64
from datetime import datetime
from utils.tableMaker import ocrBillMaker

# Check if running on Vercel
VERCEL_ENV = os.getenv("VERCEL", False)

# Redis connection setup
def get_redis_connection():
    """Get Redis connection with error handling"""
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
        print(f"✅ Redis connected: {redis_host}:{redis_port}")
        return r
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        print("Falling back to in-memory storage (not recommended for production)")
        return None

# Initialize Redis connection
redis_client = get_redis_connection()

# Fallback in-memory storage if Redis is not available
fallback_rooms = {} if redis_client is None else None

# Try to import PDF libraries with Vercel-aware fallback
PDF_AVAILABLE = False
WEASYPRINT_AVAILABLE = False
XHTML2PDF_AVAILABLE = False

if not VERCEL_ENV:
    # Try WeasyPrint first (not available on Vercel due to system dependencies)
    try:
        import weasyprint
        WEASYPRINT_AVAILABLE = True
        PDF_AVAILABLE = True
        print("WeasyPrint available for PDF generation")
    except ImportError:
        print("WeasyPrint not available, trying xhtml2pdf...")

# Try xhtml2pdf as fallback (works on Vercel)
if not PDF_AVAILABLE:
    try:
        from xhtml2pdf import pisa
        XHTML2PDF_AVAILABLE = True
        PDF_AVAILABLE = True
        print("xhtml2pdf available for PDF generation")
    except ImportError:
        print("xhtml2pdf not available")

if not PDF_AVAILABLE:
    print("Warning: No PDF libraries available. PDF generation will be disabled.")
elif VERCEL_ENV:
    print("Running on Vercel - using xhtml2pdf for PDF generation")

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# Vercel-compatible upload folder configuration
if VERCEL_ENV:
    # On Vercel, use /tmp for temporary files
    app.config['UPLOAD_FOLDER'] = '/tmp'
else:
    # Local development
    app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Redis helper functions
def save_room(room_code, room_data):
    """Save room data to Redis or fallback storage"""
    if redis_client:
        try:
            # Convert sets to lists for JSON serialization
            room_data_copy = room_data.copy()
            if 'submitted_users' in room_data_copy and isinstance(room_data_copy['submitted_users'], set):
                room_data_copy['submitted_users'] = list(room_data_copy['submitted_users'])
            
            redis_client.set(f"room:{room_code}", json.dumps(room_data_copy))
            return True
        except Exception as e:
            print(f"Redis save error: {e}")
            return False
    else:
        fallback_rooms[room_code] = room_data
        return True

def get_room(room_code):
    """Get room data from Redis or fallback storage"""
    if redis_client:
        try:
            room_data = redis_client.get(f"room:{room_code}")
            if room_data:
                data = json.loads(room_data)
                # Convert submitted_users back to set
                if 'submitted_users' in data and isinstance(data['submitted_users'], list):
                    data['submitted_users'] = set(data['submitted_users'])
                return data
            return None
        except Exception as e:
            print(f"Redis get error: {e}")
            return None
    else:
        return fallback_rooms.get(room_code)

def room_exists(room_code):
    """Check if room exists in Redis or fallback storage"""
    if redis_client:
        try:
            return redis_client.exists(f"room:{room_code}")
        except Exception as e:
            print(f"Redis exists error: {e}")
            return False
    else:
        return room_code in fallback_rooms

def delete_room(room_code):
    """Delete room from Redis or fallback storage"""
    if redis_client:
        try:
            return redis_client.delete(f"room:{room_code}")
        except Exception as e:
            print(f"Redis delete error: {e}")
            return False
    else:
        return fallback_rooms.pop(room_code, None) is not None

def generate_room_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def generate_qr_base64(link):
    """Generate QR code in base64 format"""
    qr = qrcode.make(link)
    buffer = BytesIO()
    qr.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

def process_uploaded_file(file):
    """
    Process uploaded file in a Vercel-compatible way
    Returns OCR result or raises exception
    """
    abc = ocrBillMaker()
    
    if VERCEL_ENV:
        # On Vercel: Use in-memory processing first, fallback to /tmp
        try:
            # Try in-memory processing first
            file_stream = BytesIO(file.read())
            return abc.getText(file_stream)
        except Exception as e:
            print(f"In-memory processing failed: {e}, trying /tmp method")
            # Fallback to temporary file in /tmp
            file.seek(0)  # Reset file pointer
            temp_filename = secure_filename(f"{uuid.uuid4().hex}_{file.filename}")
            temp_filepath = os.path.join('/tmp', temp_filename)
            
            try:
                # Save to temporary location
                file.save(temp_filepath)
                
                # Process with OCR
                result = abc.getText(temp_filepath)
                
                # Clean up temporary file
                try:
                    os.remove(temp_filepath)
                except OSError:
                    pass  # File might not exist or already deleted
                
                return result
                
            except Exception as temp_error:
                # Clean up on error
                try:
                    os.remove(temp_filepath)
                except OSError:
                    pass
                raise temp_error
    else:
        # Local development: Save to upload folder as before
        filename = secure_filename(f"{uuid.uuid4().hex}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return abc.getText(filepath)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/create', methods=['GET', 'POST'])
def create_room():
    if request.method == 'POST':
        host_name = request.form['host_name']
        room_name = request.form['room_name']
        num_people = request.form['num_people']
        file = request.files['bill_image']
        
        if not file or file.filename == '':
            return "No file uploaded", 400
        
        try:
            # Process uploaded file using Vercel-compatible method
            ocr_result = process_uploaded_file(file)
            
            # Extract items from OCR result
            items = ocr_result.get("items", [])
            print(f"Extracted items: {items}")
            
            # Generate room code and create room
            room_code = generate_room_code()
            room_data = {
                'host_name': host_name,
                'room_name': room_name,
                'num_people': int(num_people),
                'bill_image': file.filename if not VERCEL_ENV else None,  # Don't store filename on Vercel
                'items': items,
                'ocr_data': ocr_result,
                'users': [host_name],
                'selections': {},
                'submitted_users': set()
            }
            
            # Save room to Redis
            if not save_room(room_code, room_data):
                return "Error saving room data", 500
            
            print(f"OCR result: {ocr_result}")
            return render_template('edit_items.html', room_code=room_code, items=ocr_result)
            
        except Exception as e:
            print(f"Error processing file: {e}")
            return f"Error processing uploaded file: {str(e)}", 500
    
    return render_template('create_room.html')

@app.route('/edit/<room_code>', methods=['GET', 'POST'])
def edit_items(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    if request.method == 'POST':
        # Update items list
        items = []
        names = request.form.getlist('item_name')
        prices = request.form.getlist('item_price')
        for name, price in zip(names, prices):
            try:
                price_val = price
                items.append({'name': name.strip(), 'price': price_val})
            except ValueError:
                continue
        
        # Update OCR data with manually edited values
        def safe_get_form_value(field_name, default='N/A'):
            value = request.form.get(field_name, default).strip()
            return value if value else default
        
        # Update the OCR data with edited values
        if 'ocr_data' not in room:
            room['ocr_data'] = {}
        
        room['ocr_data'].update({
            'subtotal': safe_get_form_value('subtotal'),
            'serviceCharge': safe_get_form_value('serviceCharge'),
            'discount': safe_get_form_value('discount'),
            'cgst': safe_get_form_value('cgst'),
            'sgst': safe_get_form_value('sgst'),
            'total': safe_get_form_value('total')
        })
        
        # Update items
        room['items'] = items
        
        # Save updated room data
        if not save_room(room_code, room):
            return "Error saving room data", 500
        
        print(f"Updated OCR data for room {room_code}: {room['ocr_data']}")
        
        return redirect(url_for('room_summary', room_code=room_code))
    
    # For GET request, pass the full OCR data to the template
    ocr_data = room.get('ocr_data', {})
    return render_template('edit_items.html', room_code=room_code, items=ocr_data)

@app.route('/room/<room_code>')
def room_summary(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Ensure host is in users list
    if 'users' not in room:
        room['users'] = []
    if room['host_name'] not in room['users']:
        room['users'].insert(0, room['host_name'])  # Host first
    
    # Initialize other structures if they don't exist
    if 'selections' not in room:
        room['selections'] = {}
    if 'submitted_users' not in room:
        room['submitted_users'] = set()
    
    # Save any updates
    save_room(room_code, room)
    
    # Generate QR code for /join/<room_code>
    print(room)
    join_url = url_for('join_room', room_code=room_code, _external=True)
    qr_b64 = generate_qr_base64(join_url)
    return render_template('room_summary.html', room=room, room_code=room_code, qr_b64=qr_b64, join_url=join_url)

@app.route('/join', methods=['GET', 'POST'])
def join():
    if request.method == 'POST':
        user_name = request.form.get('user_name', '').strip()
        room_code = request.form.get('room_code', '').strip().upper()
        
        # Validate inputs
        if not user_name:
            return render_template('join_room.html', error="Please enter your name")
        
        if not room_code:
            return render_template('join_room.html', error="Please enter a room code")
        
        # Check if room exists
        if not room_exists(room_code):
            return render_template('join_room.html', error="Room not found. Please check the room code.")
        
        room = get_room(room_code)
        if not room:
            return render_template('join_room.html', error="Room not found. Please check the room code.")
        
        # Check if user already exists (prevent duplicates)
        if user_name in room.get('users', []):
            return render_template('join_room.html', error=f"User '{user_name}' has already joined this room. Please choose a different name.")
        
        # Check if room is full
        expected_people = int(room.get('num_people', 1))
        if len(room.get('users', [])) >= expected_people:
            return render_template('join_room.html', error="Room is full. Cannot join.")
        
        # Add user to room
        if 'users' not in room:
            room['users'] = []
        room['users'].append(user_name)
        
        # Save updated room
        if not save_room(room_code, room):
            return render_template('join_room.html', error="Error joining room. Please try again.")
        
        # Redirect to user-specific page
        return redirect(url_for('user_room', room_code=room_code, user_name=user_name))
    
    return render_template('join_room.html')

@app.route('/room/<room_code>/user/<user_name>')
def user_room(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Check if user has already submitted
    if user_name in room.get('submitted_users', set()):
        return redirect(url_for('waiting_room', room_code=room_code, user_name=user_name))
    
    # Redirect to item selection
    return redirect(url_for('select_items', room_code=room_code, user_name=user_name))

@app.route('/room/<room_code>/user/<user_name>/select-items', methods=['GET', 'POST'])
def select_items(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Check if user has already submitted
    if user_name in room.get('submitted_users', set()):
        return redirect(url_for('waiting_room', room_code=room_code, user_name=user_name))
    
    if request.method == 'POST':
        # Get selected items
        selected_items = request.form.getlist('selected_items')
        
        # Initialize selections and submitted_users if they don't exist
        if 'selections' not in room:
            room['selections'] = {}
        if 'submitted_users' not in room:
            room['submitted_users'] = set()
        
        # Save user's selections
        room['selections'][user_name] = selected_items
        room['submitted_users'].add(user_name)
        
        # Save updated room
        if not save_room(room_code, room):
            return "Error saving selections. Please try again.", 500
        
        print(f"User {user_name} selected items: {selected_items}")
        
        # Redirect to waiting room
        return redirect(url_for('waiting_room', room_code=room_code, user_name=user_name))
    
    return render_template('select_items.html', room=room, room_code=room_code, user_name=user_name)

@app.route('/room/<room_code>/waiting')
@app.route('/room/<room_code>/waiting/<user_name>')
def waiting_room(room_code, user_name=None):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # If user_name is not provided, try to get it from query parameters
    if not user_name:
        user_name = request.args.get('user', '')
    
    # Check if user is the host
    is_host = (user_name == room.get('host_name', ''))
    
    # Generate QR code for joining the room
    join_url = url_for('join_room', room_code=room_code, _external=True)
    qr_b64 = generate_qr_base64(join_url)
    
    return render_template('waiting_room.html', 
                         room=room, 
                         room_code=room_code, 
                         user_name=user_name,
                         is_host=is_host,
                         qr_b64=qr_b64,
                         join_url=join_url)

@app.route('/room/<room_code>/status')
def room_status(room_code):
    room = get_room(room_code)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    
    users = room.get('users', [])
    submitted_users = list(room.get('submitted_users', set()))
    expected_people = int(room.get('num_people', 1))
    
    # Check if expected number of people have joined and all have submitted
    enough_users_joined = len(users) >= expected_people
    all_submitted = len(submitted_users) == len(users) and len(users) > 0
    ready_to_proceed = enough_users_joined and all_submitted
    
    return jsonify({
        'status': 'success',
        'users': users,
        'submitted_users': submitted_users,
        'all_submitted': all_submitted,
        'enough_users_joined': enough_users_joined,
        'ready_to_proceed': ready_to_proceed,
        'total_users': len(users),
        'expected_people': expected_people,
        'submitted_count': len(submitted_users),
        'host_name': room.get('host_name', '')
    })

@app.route('/room/<room_code>/force-complete/<user_name>', methods=['POST'])
def force_complete(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Check if user is the host
    if user_name != room.get('host_name'):
        return "Only the host can force completion", 403
    
    # Mark all non-submitted users as submitted with empty selections
    users = room.get('users', [])
    submitted_users = room.get('submitted_users', set())
    selections = room.get('selections', {})
    
    for user in users:
        if user not in submitted_users:
            selections[user] = []  # Empty selection for non-submitted users
            submitted_users.add(user)
    
    room['selections'] = selections
    room['submitted_users'] = submitted_users
    
    # Save updated room
    if not save_room(room_code, room):
        return "Error saving room data", 500
    
    print(f"Host {user_name} forced completion for room {room_code}")
    
    return redirect(url_for('results_page', room_code=room_code))

@app.route('/room/<room_code>/results')
def results_page(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Calculate Bill Bear
    bill_split = calculate_bill_split(room)
    
    return render_template('results.html', 
                         room=room, 
                         room_code=room_code, 
                         bill_split=bill_split,
                         pdf_available=PDF_AVAILABLE)

def calculate_bill_split(room):
    """Calculate the Bill Bear with items shared equally among users who selected them"""
    items = room.get('items', [])
    selections = room.get('selections', {})
    ocr_data = room.get('ocr_data', {})
    
    # Create a mapping of item names to prices
    item_prices = {}
    for item in items:
        # Clean price string to get numeric value
        price_str = item.get('price', '₹0.00')
        price_clean = price_str.replace('₹', '').replace(',', '')
        try:
            item_prices[item['name']] = float(price_clean)
        except ValueError:
            item_prices[item['name']] = 0.0
    
    # Get additional charges from the OCR data
    def parse_amount(amount_str):
        if not amount_str or amount_str == 'N/A':
            return 0.0
        clean_amount = str(amount_str).replace('₹', '').replace(',', '')
        try:
            return float(clean_amount)
        except ValueError:
            return 0.0
    
    # Extract additional charges from OCR data
    service_charge = parse_amount(ocr_data.get('serviceCharge', 0))
    discount = parse_amount(ocr_data.get('discount', 0))
    cgst = parse_amount(ocr_data.get('cgst', 0))
    sgst = parse_amount(ocr_data.get('sgst', 0))
    
    # Calculate how many users selected each item
    item_selection_count = {}
    for item_name in item_prices.keys():
        count = sum(1 for user_selections in selections.values() if item_name in user_selections)
        item_selection_count[item_name] = count
    
    # Calculate each user's item total based on shared costs
    user_totals = {}
    all_users = list(selections.keys())
    
    for user in all_users:
        user_item_total = 0.0
        selected_items = selections.get(user, [])
        
        for item_name in selected_items:
            item_price = item_prices.get(item_name, 0.0)
            selection_count = item_selection_count.get(item_name, 1)
            
            # Split the item cost equally among users who selected it
            if selection_count > 0:
                user_share = item_price / selection_count
                user_item_total += user_share
        
        user_totals[user] = user_item_total
    
    # Calculate subtotal from all items (not user selections)
    subtotal = sum(item_prices.values())
    
    # Split taxes and service charges equally among all users
    num_users = len(all_users)
    if num_users > 0:
        cgst_per_user = cgst / num_users
        sgst_per_user = sgst / num_users
        service_charge_per_user = service_charge / num_users
    else:
        cgst_per_user = 0.0
        sgst_per_user = 0.0
        service_charge_per_user = 0.0
    
    # Calculate discount percentage based on subtotal
    discount_percentage = (discount / subtotal) if subtotal > 0 else 0
    
    # Calculate proportional splits
    user_breakdown = {}
    grand_total = 0
    
    for user, item_total in user_totals.items():
        # Apply discount proportionally to user's item total
        user_discount = item_total * discount_percentage
        
        # Calculate final amount including service charge
        final_amount = item_total + service_charge_per_user + cgst_per_user + sgst_per_user - user_discount
        
        user_breakdown[user] = {
            'selected_items': selections.get(user, []),
            'item_total': round(item_total, 2),
            'percentage': round((item_total / subtotal) * 100, 1) if subtotal > 0 else 0,
            'service_charge': round(service_charge_per_user, 2),
            'discount': round(user_discount, 2),
            'cgst': round(cgst_per_user, 2),
            'sgst': round(sgst_per_user, 2),
            'final_amount': round(final_amount, 2)
        }
        
        grand_total += final_amount
    
    return {
        'user_breakdown': user_breakdown,
        'totals': {
            'subtotal': round(sum(user_totals.values()), 2),
            'service_charge': round(service_charge, 2),
            'discount': round(discount, 2),
            'cgst': round(cgst, 2),
            'sgst': round(sgst, 2),
            'grand_total': round(grand_total, 2)
        },
        'item_sharing': item_selection_count  # For debugging purposes
    }

@app.route('/join/<room_code>', methods=['GET', 'POST'])
def join_room(room_code):
    room_code = room_code.upper()  # Ensure room code is uppercase for consistency
    
    # Check if room exists
    room = get_room(room_code)
    if not room:
        return render_template('join_room_direct.html', error="Room not found", room_code=room_code)
    
    if request.method == 'POST':
        user_name = request.form.get('user_name', '').strip()
        
        # Validate user name
        if not user_name:
            return render_template('join_room_direct.html', 
                                 error="Please enter your name", 
                                 room=room, 
                                 room_code=room_code)
        
        # Initialize users list if it doesn't exist
        if 'users' not in room:
            room['users'] = []
        
        # Check if user already exists (prevent duplicates)
        if user_name in room['users']:
            return render_template('join_room_direct.html', 
                                 error=f"User '{user_name}' has already joined this room. Please choose a different name.", 
                                 room=room, 
                                 room_code=room_code)
        
        # Check if room is full
        expected_people = int(room.get('num_people', 1))
        if len(room['users']) >= expected_people:
            return render_template('join_room_direct.html', 
                                 error="Room is full. Cannot join.", 
                                 room=room, 
                                 room_code=room_code)
        
        # Add user to room
        room['users'].append(user_name)
        
        # Save updated room
        if not save_room(room_code, room):
            return render_template('join_room_direct.html', 
                                 error="Error joining room. Please try again.", 
                                 room=room, 
                                 room_code=room_code)
        
        # Log the user joining
        print(f"User '{user_name}' joined room '{room_code}' - Room: {room['room_name']}")
        
        # Redirect to user-specific page
        return redirect(url_for('user_room', room_code=room_code, user_name=user_name))
    
    # GET request - show the join form
    return render_template('join_room_direct.html', room=room, room_code=room_code)

@app.route('/room/<room_code>/download')
def download_pdf(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    
    # Check if PDF generation is available
    if not PDF_AVAILABLE:
        return "PDF generation not available. Please install weasyprint or xhtml2pdf.", 500
    
    # Calculate Bill Bear
    bill_split = calculate_bill_split(room)
    
    # Generate PDF content
    html_content = render_template('results_pdf.html', 
                                 room=room, 
                                 room_code=room_code, 
                                 bill_split=bill_split,
                                 current_date=datetime.now())
    
    pdf_buffer = BytesIO()
    
    try:
        # Try WeasyPrint first (if available and not on Vercel)
        if WEASYPRINT_AVAILABLE and not VERCEL_ENV:
            weasyprint.HTML(string=html_content).write_pdf(pdf_buffer)
            pdf_buffer.seek(0)
            
            response = make_response(pdf_buffer.read())
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'attachment; filename="{room["room_name"]}_bill_split.pdf"'
            return response
            
        # Use xhtml2pdf as fallback (works on Vercel)
        elif XHTML2PDF_AVAILABLE:
            pisa_status = pisa.CreatePDF(html_content, dest=pdf_buffer)
            
            if pisa_status.err:
                return "Error generating PDF with xhtml2pdf", 500
            
            pdf_buffer.seek(0)
            response = make_response(pdf_buffer.read())
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'attachment; filename="{room["room_name"]}_bill_split.pdf"'
            return response
        
        else:
            return "No PDF generation library available", 500
            
    except Exception as e:
        print(f"PDF generation error: {e}")
        return f"Error generating PDF: {str(e)}", 500

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)