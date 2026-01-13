import os
import uuid
import random
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, url_for, flash
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from bson import ObjectId
from geopy.geocoders import Nominatim
from urllib.parse import quote_plus

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "replace_with_a_strong_secret")

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# MongoDB client (adjust URI if needed)
client = MongoClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27017/"))
db = client["rental_db"]
users_col = db["users"]
props_col = db["properties"]
bookings_col = db["bookings"]
notifications_col = db["notifications"]

geolocator = Nominatim(user_agent="rental_app")


def get_lat_lon(address):
    try:
        location = geolocator.geocode(address, timeout=10)
        if location:
            return location.latitude, location.longitude
    except Exception:
        pass
    return None, None


def is_logged_in():
    return session.get("user_email") is not None


def role_is(r):
    return session.get("role") == r


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_file(file):
    """Save uploaded file with unique name and return filename."""
    if file and allowed_file(file.filename):
        orig_name = secure_filename(file.filename)
        unique_name = f"{uuid.uuid4().hex}_{orig_name}"
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
        return unique_name
    return None


# ------------------ Email helper ------------------

def send_email(to_email: str, subject: str, body: str):
    """
    Send an email using SMTP settings from environment variables.
    If SMTP is not configured, fallback to printing the email (safe).
    """
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "0")) if os.environ.get("SMTP_PORT") else None
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    email_from = os.environ.get("EMAIL_FROM", smtp_user or "no-reply@example.com")

    if not smtp_host or not smtp_port or not smtp_user or not smtp_pass:
        # Fallback: print to console for development/testing
        print("---- Email fallback (SMTP not configured) ----")
        print(f"To: {to_email}")
        print(f"From: {email_from}")
        print(f"Subject: {subject}")
        print("Body:")
        print(body)
        print("---- End email ----")
        return

    try:
        msg = EmailMessage()
        msg["From"] = email_from
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        # SSL vs TLS: port 465 = SSL, else use STARTTLS
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
    except Exception as e:
        # Never crash the app for email errors; just log
        print("Failed to send email:", e)
        print("Email contents were:")
        print(f"To: {to_email}\nSubject: {subject}\n{body}")


# ------------------ Helpers for reviews ------------------

def compute_avg_rating(reviews):
    """Return average rating (rounded to 2 decimals) or None if no reviews."""
    if not reviews:
        return None
    total = 0.0
    count = 0
    for r in reviews:
        try:
            total += float(r.get("rating", 0))
            count += 1
        except Exception:
            pass
    return round(total / count, 2) if count else None


# ------------------ ROUTES ------------------


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip().lower()

        if not (name and email and password and role in ("owner", "tenant")):
            return render_template("register.html", error="Fill all fields and choose a valid role.")
        if users_col.find_one({"email": email}):
            return render_template("register.html", error="Email already registered.")

        users_col.insert_one({
            "name": name,
            "email": email,
            "password": generate_password_hash(password),
            "role": role
        })
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = users_col.find_one({"email": email})
        if not user or not check_password_hash(user["password"], password):
            return render_template("login.html", error="Invalid email or password.")

        session["user_email"] = user["email"]
        session["role"] = user["role"]
        return redirect(url_for(f"{user['role']}_dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ------------------ OWNER DASHBOARD ------------------


@app.route("/owner/dashboard")
def owner_dashboard():
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    owner_email = session["user_email"]
    my_props = list(props_col.find({"owner_email": owner_email}))
    for p in my_props:
        p["_id"] = str(p["_id"])
        p["images"] = p.get("images", [])  # Always a list
        # reviews if present
        p["reviews"] = p.get("reviews", [])
        p["avg_rating"] = compute_avg_rating(p["reviews"])
        # booking summary
        approved = bookings_col.find_one({"property_id": ObjectId(p["_id"]), "status": "APPROVED"})
        p["booked"] = bool(approved)
        p["booked_by"] = approved["tenant_email"] if approved else None

    # show recent notifications (owner)
    notifications = list(notifications_col.find({"owner_email": owner_email}).sort("timestamp", -1).limit(10))
    for n in notifications:
        n["_id"] = str(n["_id"])
        n["timestamp_str"] = n["timestamp"].strftime("%Y-%m-%d %H:%M")
    return render_template("owner_dashboard.html", properties=my_props, notifications=notifications)


@app.route("/owner/add", methods=["GET", "POST"])
def owner_add():
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        location = request.form.get("location", "").strip()
        price = request.form.get("price", "").strip()
        rooms = request.form.get("rooms", "").strip()

        if not (title and location and price and rooms):
            return render_template("add_property.html", error="Fill title, location, price and rooms.")

        try:
            price_val = float(price)
            rooms_val = int(rooms)
        except ValueError:
            return render_template("add_property.html", error="Price must be number and rooms must be integer.")

        # Handle multiple images
        files = request.files.getlist("images")
        images_list = [save_file(f) for f in files if f and f.filename != ""]

        lat, lon = get_lat_lon(location)

        props_col.insert_one({
            "title": title,
            "description": description,
            "location": location,
            "latitude": lat,
            "longitude": lon,
            "price": price_val,
            "rooms": rooms_val,
            "owner_email": session["user_email"],
            "images": images_list,
            "reviews": [],          # initialize reviews list
            "fake": False,
            "created_at": datetime.utcnow()
        })
        return redirect(url_for("owner_dashboard"))

    return render_template("add_property.html")


@app.route("/owner/edit/<property_id>", methods=["GET", "POST"])
def edit_property(property_id):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    prop = props_col.find_one({"_id": ObjectId(property_id), "owner_email": session["user_email"]})
    if not prop:
        return redirect(url_for("owner_dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        location = request.form.get("location", "").strip()
        price = request.form.get("price", "").strip()
        rooms = request.form.get("rooms", "").strip()

        lat, lon = get_lat_lon(location if location else prop["location"])

        update_data = {
            "title": title if title else prop["title"],
            "description": description if description else prop.get("description", ""),
            "location": location if location else prop["location"],
            "latitude": lat if lat else prop.get("latitude"),
            "longitude": lon if lon else prop.get("longitude"),
            "price": float(price) if price else prop["price"],
            "rooms": int(rooms) if rooms else prop["rooms"],
            "images": prop.get("images", [])
        }

        # Update images if new files uploaded
        files = request.files.getlist("images")
        for f in files:
            if f and f.filename != "":
                filename = save_file(f)
                if filename:
                    update_data["images"].append(filename)

        props_col.update_one({"_id": ObjectId(property_id)}, {"$set": update_data})
        return redirect(url_for("owner_dashboard"))

    prop["images"] = prop.get("images", [])
    prop["_id"] = str(prop["_id"])
    return render_template("edit_property.html", property=prop)


@app.route("/owner/delete/<prop_id>", methods=["POST"])
def owner_delete(prop_id):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))
    props_col.delete_one({"_id": ObjectId(prop_id), "owner_email": session["user_email"]})
    # Also remove related bookings and notifications (safe cleanup)
    try:
        bookings_col.delete_many({"property_id": ObjectId(prop_id)})
        notifications_col.delete_many({"property_id": ObjectId(prop_id)})
    except Exception:
        pass
    return redirect(url_for("owner_dashboard"))


# ------------------ TENANT DASHBOARD ------------------


@app.route("/tenant/dashboard")
def tenant_dashboard():
    if not is_logged_in() or not role_is("tenant"):
        return redirect(url_for("login"))

    location = request.args.get("location", "").strip()
    min_price = request.args.get("min_price", "").strip()
    max_price = request.args.get("max_price", "").strip()
    rooms = request.args.get("rooms", "").strip()

    query = {}
    if location:
        query["location"] = {"$regex": location, "$options": "i"}

    price_q = {}
    try:
        if min_price != "":
            price_q["$gte"] = float(min_price)
        if max_price != "":
            price_q["$lte"] = float(max_price)
    except ValueError:
        price_q = {}
    if price_q:
        query["price"] = price_q

    if rooms:
        try:
            query["rooms"] = int(rooms)
        except ValueError:
            pass

    found = list(props_col.find(query))
    for p in found:
        p["_id"] = str(p["_id"])
        p["images"] = p.get("images", [])
        p["reviews"] = p.get("reviews", [])
        p["avg_rating"] = compute_avg_rating(p["reviews"])
        if "location" in p and p["location"]:
            p["map_url"] = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(p["location"])
        else:
            p["map_url"] = "#"
        # booking status
        approved = bookings_col.find_one({"property_id": ObjectId(p["_id"]), "status": "APPROVED"})
        p["booked"] = bool(approved)
        p["booked_by"] = approved["tenant_email"] if approved else None

    # tenant's bookings
    my_bookings = list(bookings_col.find({"tenant_email": session["user_email"]}).sort("created_at", -1))
    for b in my_bookings:
        b["_id"] = str(b["_id"])
        b["property_id_str"] = str(b["property_id"])
        b["created_at_str"] = b["created_at"].strftime("%Y-%m-%d %H:%M")

    filters = {"location": location, "min_price": min_price, "max_price": max_price, "rooms": rooms}
    return render_template("tenant_dashboard.html", properties=found, filters=filters, bookings=my_bookings)


# ------------------ PROPERTY DETAILS ------------------


@app.route("/property/<property_id>")
def property_details(property_id):
    if not is_logged_in() or not role_is("tenant"):
        return redirect(url_for("login"))

    prop = props_col.find_one({"_id": ObjectId(property_id)})
    if not prop:
        return redirect(url_for("tenant_dashboard"))

    prop["_id"] = str(prop["_id"])
    prop["images"] = prop.get("images", [])
    prop["reviews"] = prop.get("reviews", [])
    prop["avg_rating"] = compute_avg_rating(prop["reviews"])
    prop["map_url"] = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(prop.get("location", "")) if prop.get("location") else "#"

    # booking state for this property
    approved = bookings_col.find_one({"property_id": ObjectId(prop["_id"]), "status": "APPROVED"})
    prop["booked"] = bool(approved)
    prop["booked_by"] = approved["tenant_email"] if approved else None

    # whether current tenant already has a pending/approved booking
    existing = bookings_col.find_one({"property_id": ObjectId(prop["_id"]), "tenant_email": session["user_email"]})
    prop["my_booking"] = existing

    return render_template("property_details.html", property=prop)


# ------------------ BOOKING ROUTES ------------------

@app.route("/book/<property_id>", methods=["POST"])
def book_property(property_id):
    if not is_logged_in() or not role_is("tenant"):
        return redirect(url_for("login"))

    # verify property exists
    try:
        prop = props_col.find_one({"_id": ObjectId(property_id)})
    except Exception:
        prop = None
    if not prop:
        flash("Property not found.", "danger")
        return redirect(url_for("tenant_dashboard"))

    tenant = users_col.find_one({"email": session["user_email"]})
    tenant_name = tenant.get("name", session["user_email"]) if tenant else session["user_email"]

    # create booking with status PENDING
    booking_doc = {
        "property_id": ObjectId(property_id),
        "tenant_email": session["user_email"],
        "tenant_name": tenant_name,
        "created_at": datetime.utcnow(),
        "status": "PENDING"
    }
    insert_res = bookings_col.insert_one(booking_doc)

    # create a notification for the owner
    notif = {
        "owner_email": prop["owner_email"],
        "property_id": ObjectId(property_id),
        "message": f"New booking request from {tenant_name} ({session['user_email']}) for '{prop.get('title', 'Property')}'",
        "timestamp": datetime.utcnow(),
        "read": False
    }
    notifications_col.insert_one(notif)

    # Send email to owner (non-blocking best-effort)
    owner = users_col.find_one({"email": prop["owner_email"]})
    if owner and owner.get("email"):
        try:
            send_email(
                to_email=owner["email"],
                subject=f"New booking request for '{prop.get('title', 'Property')}'",
                body=f"Hello {owner.get('name','')},\n\nYou have a new booking request from {tenant_name} ({session['user_email']}) for your property '{prop.get('title','Property')}'.\n\nLog in to the dashboard to approve or reject the request.\n\nThanks,\nRental App"
            )
        except Exception as e:
            print("Email error (owner notify):", e)

    flash("Booking request sent. The owner will review it.", "success")
    return redirect(url_for("property_details", property_id=property_id))


@app.route("/my-bookings")
def my_bookings():
    if not is_logged_in() or not role_is("tenant"):
        return redirect(url_for("login"))
    my_bookings = list(bookings_col.find({"tenant_email": session["user_email"]}).sort("created_at", -1))
    # enrich with property title
    for b in my_bookings:
        b["_id"] = str(b["_id"])
        b["created_at_str"] = b["created_at"].strftime("%Y-%m-%d %H:%M")
        try:
            p = props_col.find_one({"_id": b["property_id"]})
            b["property_title"] = p.get("title") if p else "Deleted property"
            b["property_id_str"] = str(b["property_id"])
        except Exception:
            b["property_title"] = "Unknown"
            b["property_id_str"] = str(b.get("property_id", ""))
    return render_template("my_bookings.html", bookings=my_bookings)


# ------------------ OWNER: view booking requests per property ------------------

@app.route("/owner/requests/<property_id>")
def owner_requests(property_id):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    # ensure owner owns the property
    prop = props_col.find_one({"_id": ObjectId(property_id), "owner_email": session["user_email"]})
    if not prop:
        flash("Property not found or you are not the owner.", "danger")
        return redirect(url_for("owner_dashboard"))

    reqs = list(bookings_col.find({"property_id": ObjectId(property_id)}).sort("created_at", 1))
    requests_list = []
    for r in reqs:
        requests_list.append({
            "_id": str(r["_id"]),
            "tenant_name": r.get("tenant_name"),
            "tenant_email": r.get("tenant_email"),
            "status": r.get("status"),
            "created_at": r.get("created_at").strftime("%Y-%m-%d %H:%M")
        })

    return render_template("owner_property_requests.html", requests=requests_list, property_title=prop.get("title", "Property"))


@app.route("/owner/approve/<booking_id>", methods=["POST"])
def owner_approve_request(booking_id):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    booking = bookings_col.find_one({"_id": ObjectId(booking_id)})
    if not booking:
        flash("Booking not found.", "danger")
        return redirect(url_for("owner_dashboard"))

    # verify owner owns the property
    prop = props_col.find_one({"_id": booking["property_id"], "owner_email": session["user_email"]})
    if not prop:
        flash("You are not authorized to approve this booking.", "danger")
        return redirect(url_for("owner_dashboard"))

    # check if already an approved booking exists
    existing_approved = bookings_col.find_one({"property_id": booking["property_id"], "status": "APPROVED"})
    if existing_approved:
        flash("This property is already booked by someone else.", "warning")
        return redirect(url_for("owner_requests", property_id=str(booking["property_id"])))

    # Approve this booking
    bookings_col.update_one({"_id": booking["_id"]}, {"$set": {"status": "APPROVED"}})

    # Mark property as booked (simple flag)
    props_col.update_one({"_id": booking["property_id"]}, {"$set": {"booked": True, "booked_by": booking["tenant_email"]}})

    # Auto-reject all other pending bookings for the same property
    bookings_col.update_many(
        {"property_id": booking["property_id"], "status": "PENDING", "_id": {"$ne": booking["_id"]}},
        {"$set": {"status": "REJECTED"}}
    )

    # Notify tenant (in-app)
    notifications_col.insert_one({
        "owner_email": session["user_email"],
        "tenant_email": booking["tenant_email"],
        "property_id": booking["property_id"],
        "booking_id": booking["_id"],
        "message": f"Your booking for '{prop.get('title', 'Property')}' has been APPROVED by the owner.",
        "timestamp": datetime.utcnow(),
        "read": False
    })

    # Send email to tenant
    try:
        send_email(
            to_email=booking["tenant_email"],
            subject=f"Your booking for '{prop.get('title', 'Property')}' is APPROVED",
            body=f"Hello {booking.get('tenant_name','')},\n\nYour booking for the property '{prop.get('title','Property')}' has been APPROVED by the owner ({session['user_email']}).\n\nPlease contact the owner to finalize details.\n\nThanks,\nRental App"
        )
    except Exception as e:
        print("Email error (tenant notify approve):", e)

    flash("Booking approved. Other pending requests (if any) were rejected.", "success")
    return redirect(url_for("owner_requests", property_id=str(booking["property_id"])))


@app.route("/owner/reject/<booking_id>", methods=["POST"])
def owner_reject_request(booking_id):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    booking = bookings_col.find_one({"_id": ObjectId(booking_id)})
    if not booking:
        flash("Booking not found.", "danger")
        return redirect(url_for("owner_dashboard"))

    # verify owner owns the property
    prop = props_col.find_one({"_id": booking["property_id"], "owner_email": session["user_email"]})
    if not prop:
        flash("You are not authorized to reject this booking.", "danger")
        return redirect(url_for("owner_dashboard"))

    bookings_col.update_one({"_id": booking["_id"]}, {"$set": {"status": "REJECTED"}})

    # Notify tenant (in-app)
    notifications_col.insert_one({
        "owner_email": session["user_email"],
        "tenant_email": booking["tenant_email"],
        "property_id": booking["property_id"],
        "booking_id": booking["_id"],
        "message": f"Your booking for '{prop.get('title', 'Property')}' has been REJECTED by the owner.",
        "timestamp": datetime.utcnow(),
        "read": False
    })

    # Send email to tenant
    try:
        send_email(
            to_email=booking["tenant_email"],
            subject=f"Your booking for '{prop.get('title', 'Property')}' was REJECTED",
            body=f"Hello {booking.get('tenant_name','')},\n\nYour booking for the property '{prop.get('title','Property')}' has been rejected by the owner ({session['user_email']}).\n\nYou can try other listings.\n\nThanks,\nRental App"
        )
    except Exception as e:
        print("Email error (tenant notify reject):", e)

    flash("Booking rejected.", "info")
    return redirect(url_for("owner_requests", property_id=str(booking["property_id"])))


# ------------------ NOTIFICATIONS: list & mark read ------------------

@app.route("/notifications")
def notifications():
    if not is_logged_in():
        return redirect(url_for("login"))

    user_email = session["user_email"]
    role = session.get("role")
    # show notifications relevant to the user (owner or tenant)
    query = {"$or": [{"owner_email": user_email}, {"tenant_email": user_email}]}
    notifs = list(notifications_col.find(query).sort("timestamp", -1))
    for n in notifs:
        n["_id"] = str(n["_id"])
        n["timestamp_str"] = n["timestamp"].strftime("%Y-%m-%d %H:%M")
    return render_template("notifications.html", notifications=notifs)


@app.route("/notifications/read/<notif_id>", methods=["POST"])
def mark_notification_read(notif_id):
    if not is_logged_in():
        return redirect(url_for("login"))
    try:
        notifications_col.update_one({"_id": ObjectId(notif_id)}, {"$set": {"read": True}})
    except Exception:
        pass
    return redirect(url_for("notifications"))


# ------------------ ADD REVIEW (TENANT) ------------------

@app.route("/property/<property_id>/review", methods=["POST"])
def add_review(property_id):
    if not is_logged_in() or not role_is("tenant"):
        return redirect(url_for("login"))

    rating = request.form.get("rating", "").strip()
    comment = request.form.get("comment", "").strip()

    try:
        rating_val = float(rating)
        if rating_val < 0 or rating_val > 5:
            raise ValueError
    except Exception:
        # invalid rating -> redirect back silently
        return redirect(url_for("property_details", property_id=property_id))

    reviewer = users_col.find_one({"email": session.get("user_email")})
    reviewer_name = reviewer.get("name") if reviewer and reviewer.get("name") else session.get("user_email")

    review_doc = {
        "by": reviewer_name,
        "email": session.get("user_email"),
        "rating": rating_val,
        "comment": comment,
        "created_at": datetime.utcnow()
    }

    try:
        props_col.update_one({"_id": ObjectId(property_id)}, {"$push": {"reviews": review_doc}})
    except Exception:
        pass

    return redirect(url_for("property_details", property_id=property_id))


# ------------------ FAKE DATA HELPERS (OWNER ONLY) ------------------

@app.route("/owner/generate-fake/<int:count>")
def generate_fake_props(count=3):
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))

    sample_locations = [
        "MG Road, Bangalore",
        "Park Street, Kolkata",
        "Connaught Place, New Delhi",
        "Bandra West, Mumbai",
        "Salt Lake, Kolkata"
    ]

    count = max(1, min(20, count))
    for i in range(count):
        loc = random.choice(sample_locations)
        props_col.insert_one({
            "title": f"Sample Flat {random.randint(100,999)}",
            "description": "Auto-generated sample listing for development/testing.",
            "location": loc,
            "latitude": None,
            "longitude": None,
            "price": round(random.uniform(5000, 50000), 2),
            "rooms": random.randint(1, 5),
            "owner_email": session["user_email"],
            "images": [],
            "reviews": [],
            "fake": True,
            "created_at": datetime.utcnow()
        })

    return redirect(url_for("owner_dashboard"))


@app.route("/owner/clear-fake")
def clear_fake_props():
    if not is_logged_in() or not role_is("owner"):
        return redirect(url_for("login"))
    props_col.delete_many({"fake": True, "owner_email": session["user_email"]})
    return redirect(url_for("owner_dashboard"))


# ------------------ MAIN ------------------

if __name__ == "__main__":
    app.run(debug=True)
