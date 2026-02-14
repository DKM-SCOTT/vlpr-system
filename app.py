import os
import cv2
import numpy as np
import uuid
import csv
from io import StringIO
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func

from database import db, User, Plate

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///vlpr.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PLATES_FOLDER'] = 'plates_detected'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PLATES_FOLDER'], exist_ok=True)
os.makedirs('static/css', exist_ok=True)
os.makedirs('static/js', exist_ok=True)
os.makedirs('templates', exist_ok=True)
os.makedirs('models', exist_ok=True)

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# Context processor for datetime
@app.context_processor
def utility_processor():
    return {'now': datetime.now}

# Load Haar Cascade
cascade_path = os.path.join('models', 'haarcascade_russian_plate_number.xml')
if os.path.exists(cascade_path):
    plate_cascade = cv2.CascadeClassifier(cascade_path)
    print("✅ Haar cascade loaded successfully!")
else:
    print(f"⚠️ Haar cascade file not found at {cascade_path}")
    plate_cascade = None

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Validation
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'danger')
            return redirect(url_for('register'))
        
        # Check if user exists
        user = User.query.filter_by(username=username).first()
        if user:
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))
        
        # Check if email exists
        email_exists = User.query.filter_by(email=email).first()
        if email_exists:
            flash('Email already registered', 'danger')
            return redirect(url_for('register'))
        
        # Create new user
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, email=email, password=hashed_password)
        
        try:
            db.session.add(new_user)
            db.session.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash('An error occurred. Please try again.', 'danger')
            print(f"Registration error: {e}")
            
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid username or password', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    plates = Plate.query.filter_by(user_id=current_user.id).order_by(Plate.detected_at.desc()).all()
    
    # Calculate today's detections
    today = datetime.now().date()
    today_count = sum(1 for plate in plates if plate.detected_at.date() == today)
    
    # Calculate average confidence
    avg_confidence = sum(p.confidence for p in plates) / len(plates) if plates else 0
    
    return render_template('dashboard.html', plates=plates, today_count=today_count, avg_confidence=avg_confidence)

@app.route('/detect', methods=['GET', 'POST'])
@login_required
def detect():
    if request.method == 'POST':
        if 'image' not in request.files:
            flash('No image uploaded', 'danger')
            return redirect(request.url)
        
        file = request.files['image']
        
        if file.filename == '':
            flash('No image selected', 'danger')
            return redirect(request.url)
        
        if file:
            # Generate unique filename
            filename = str(uuid.uuid4()) + '_' + secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Process image for plate detection
            result = detect_plate(filepath, filename)
            
            if result['success']:
                # Save to database
                plate = Plate(
                    plate_number=result['plate_text'],
                    image_path=result['original_image'],
                    plate_image_path=result['plate_image'],
                    confidence=result['confidence'],
                    user_id=current_user.id
                )
                db.session.add(plate)
                db.session.commit()
                
                return render_template('detect.html', result=result, success=True)
            else:
                flash('No license plate detected in the image', 'warning')
                return render_template('detect.html', error=True)
    
    return render_template('detect.html')

def detect_plate(image_path, filename):
    """Detect license plate using Haar Cascade"""
    try:
        # Read image
        img = cv2.imread(image_path)
        if img is None:
            return {'success': False}
        
        # Get image dimensions
        height, width = img.shape[:2]
        
        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Detect plates
        plates = plate_cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 10),
        )
        
        if len(plates) == 0:
            return {'success': False}
        
        # Process first detected plate
        (x, y, w, h) = plates[0]
        
        # Draw rectangle on original image
        img_with_rect = img.copy()
        cv2.rectangle(img_with_rect, (x, y), (x+w, y+h), (0, 255, 0), 3)
        cv2.putText(img_with_rect, 'License Plate', (x, y-10), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        
        # Save image with rectangle
        rect_filename = 'rect_' + filename
        rect_path = os.path.join(app.config['UPLOAD_FOLDER'], rect_filename)
        cv2.imwrite(rect_path, img_with_rect)
        
        # Extract and save plate
        plate_img = img[y:y+h, x:x+w]
        plate_filename = 'plate_' + filename
        plate_path = os.path.join(app.config['PLATES_FOLDER'], plate_filename)
        cv2.imwrite(plate_path, plate_img)
        
        # Generate plate number (placeholder - replace with actual OCR)
        plate_text = f"PLATE-{uuid.uuid4().hex[:6].upper()}"
        
        # Calculate confidence (simulated)
        confidence = 0.85 + (np.random.random() * 0.14)
        
        return {
            'success': True,
            'original_image': url_for('static', filename=f'uploads/{filename}'),
            'detected_image': url_for('static', filename=f'uploads/{rect_filename}'),
            'plate_image': url_for('static', filename=f'plates_detected/{plate_filename}'),
            'plate_text': plate_text,
            'confidence': float(confidence),
            'coordinates': {'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h)},
            'image_size': {'width': width, 'height': height}
        }
    except Exception as e:
        print(f"Error in plate detection: {e}")
        return {'success': False}

@app.route('/plate/<int:plate_id>')
@login_required
def plate_detail(plate_id):
    plate = Plate.query.get_or_404(plate_id)
    if plate.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('dashboard'))
    return render_template('plate_detail.html', plate=plate)

@app.route('/delete_plate/<int:plate_id>', methods=['POST'])
@login_required
def delete_plate(plate_id):
    plate = Plate.query.get_or_404(plate_id)
    if plate.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Access denied'})
    
    # Delete image files
    try:
        if plate.image_path:
            img_filename = os.path.basename(plate.image_path)
            img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_filename)
            if os.path.exists(img_path):
                os.remove(img_path)
        
        if plate.plate_image_path:
            plate_filename = os.path.basename(plate.plate_image_path)
            plate_path = os.path.join(app.config['PLATES_FOLDER'], plate_filename)
            if os.path.exists(plate_path):
                os.remove(plate_path)
    except Exception as e:
        print(f"Error deleting files: {e}")
    
    db.session.delete(plate)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Plate deleted successfully'})

@app.route('/profile')
@login_required
def profile():
    plates = Plate.query.filter_by(user_id=current_user.id).all()
    
    # Calculate statistics
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    week_start = now - timedelta(days=now.weekday())
    
    stats = {
        'total_plates': len(plates),
        'month_plates': sum(1 for p in plates if p.detected_at >= month_start),
        'week_plates': sum(1 for p in plates if p.detected_at >= week_start),
        'avg_confidence': sum(p.confidence for p in plates) / len(plates) if plates else 0
    }
    
    recent_plates = Plate.query.filter_by(user_id=current_user.id)\
                               .order_by(Plate.detected_at.desc())\
                               .limit(5).all()
    
    return render_template('profile.html', stats=stats, recent_plates=recent_plates)

@app.route('/search')
@login_required
def search():
    query = request.args.get('query', '')
    date_filter = request.args.get('date_filter', 'all')
    confidence_filter = request.args.get('confidence', 'all')
    
    # Base query
    plates_query = Plate.query.filter_by(user_id=current_user.id)
    
    # Apply search filter
    if query:
        plates_query = plates_query.filter(Plate.plate_number.contains(query.upper()))
    
    # Apply date filter
    now = datetime.now()
    if date_filter == 'today':
        plates_query = plates_query.filter(func.date(Plate.detected_at) == now.date())
    elif date_filter == 'week':
        week_start = now - timedelta(days=now.weekday())
        plates_query = plates_query.filter(Plate.detected_at >= week_start)
    elif date_filter == 'month':
        month_start = datetime(now.year, now.month, 1)
        plates_query = plates_query.filter(Plate.detected_at >= month_start)
    elif date_filter == 'year':
        year_start = datetime(now.year, 1, 1)
        plates_query = plates_query.filter(Plate.detected_at >= year_start)
    
    # Apply confidence filter
    if confidence_filter == '90':
        plates_query = plates_query.filter(Plate.confidence >= 0.9)
    elif confidence_filter == '80':
        plates_query = plates_query.filter(Plate.confidence >= 0.8)
    elif confidence_filter == '70':
        plates_query = plates_query.filter(Plate.confidence >= 0.7)
    
    plates = plates_query.order_by(Plate.detected_at.desc()).all()
    
    return render_template('search.html', plates=plates, query=query)

@app.route('/export_data')
@login_required
def export_data():
    plates = Plate.query.filter_by(user_id=current_user.id).order_by(Plate.detected_at.desc()).all()
    
    # Create CSV
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Plate Number', 'Detection Date', 'Confidence', 'Image Path'])
    
    for plate in plates:
        cw.writerow([
            plate.plate_number,
            plate.detected_at.strftime('%Y-%m-%d %H:%M:%S'),
            f"{plate.confidence * 100:.1f}%",
            plate.image_path
        ])
    
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = f"attachment; filename=vlpr_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    output.headers["Content-type"] = "text/csv"
    
    return output

@app.route('/analytics')
@login_required
def analytics():
    plates = Plate.query.filter_by(user_id=current_user.id).order_by(Plate.detected_at).all()
    
    # Prepare chart data
    dates = []
    counts = []
    confidences = []
    
    if plates:
        # Group by date
        date_counts = {}
        for plate in plates:
            date_str = plate.detected_at.strftime('%Y-%m-%d')
            date_counts[date_str] = date_counts.get(date_str, 0) + 1
        
        dates = list(date_counts.keys())
        counts = list(date_counts.values())
        confidences = [p.confidence for p in plates[-10:]]  # Last 10 confidences
    
    return render_template('analytics.html', 
                         dates=dates, 
                         counts=counts, 
                         confidences=confidences,
                         total_plates=len(plates))

@app.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    
    # Check if username already exists
    if username != current_user.username:
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            return jsonify({'success': False, 'message': 'Username already exists'})
    
    # Check if email already exists
    if email != current_user.email:
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            return jsonify({'success': False, 'message': 'Email already exists'})
    
    current_user.username = username
    current_user.email = email
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Profile updated successfully'})

@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json()
    current_password = data.get('current_password')
    new_password = data.get('new_password')
    
    if not check_password_hash(current_user.password, current_password):
        return jsonify({'success': False, 'message': 'Current password is incorrect'})
    
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': 'New password must be at least 6 characters'})
    
    current_user.password = generate_password_hash(new_password)
    db.session.commit()
    
    return jsonify({'success': True, 'message': 'Password changed successfully'})

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    flash('Page not found', 'warning')
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    flash('An internal error occurred', 'danger')
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("✅ Database tables created successfully!")
    app.run(debug=True, host='0.0.0.0', port=5000)