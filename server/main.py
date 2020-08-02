import os
import jinja2
import requests
import logging 
from logging.config import dictConfig
from flask import Flask, render_template, redirect, render_template, request, session, url_for, flash
from flask_login import LoginManager, current_user, login_user, logout_user, login_required

# Configure logging before any local imports to ensure config is applied
dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    }},
    'root': {
        'level': os.environ.get("LOG_LEVEL", "INFO"),
        'handlers': ['wsgi']
    }
})

from switchboard import get_products, refresh_sb_token, send_audio_notification
from util import b64encode_str
from constants import *
from user import User, validate_google_user, repair_acct_type, validate_facebook_user
from forms import LoginForm, RegistrationForm
from images import get_product_image_name_and_filenames

# Use reverse proxy to ensure url_for populates with the correct scheme
class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get('HTTP_X_FORWARDED_PROTO')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)

# Create flask app
app = Flask(FLASK_NAME, static_folder="client/public")
app.wsgi_app = ReverseProxied(app.wsgi_app)
app.secret_key = os.environ['SESSION_KEY']

# Setup flask-login
login_manager = LoginManager()
login_manager.login_view = 'bam_login'
login_manager.init_app(app)

# Get logger
logger = logging.getLogger(FLASK_NAME)

# Set login user_loader
@login_manager.user_loader
def load_user(user_id):
    return User.load_user(user_id)

# Set templates dir
app.jinja_loader = jinja2.ChoiceLoader(
    [app.jinja_loader, jinja2.FileSystemLoader("client/templates")])


@app.route('/health')
def health():
    return 'OK', 200

@app.route('/')
def landing():
    if current_user.is_authenticated:
        return redirect(url_for('sb_login'))
    return render_template('landing.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('sb_login'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User.create_user(form.username.data, form.password.data)
        logger.info(f"created user: {user}")
        login_user(user, remember=True)
        return redirect(url_for('sb_login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def bam_login():
    # If user is login, see if they're logged into Switchboard
    if current_user.is_authenticated:
        return redirect(url_for('sb_login'))

    # If user submitted a login form, attempt to log them in
    form = LoginForm()
    if form.validate_on_submit():
        user = User.get_user_by_username(form.username.data)
        if user is None:
            flash("Username not found.")
            return redirect(url_for('bam_login'))
        if not user.check_password(form.password.data):
            flash("Incorrect password.")
            return redirect(url_for('bam_login'))
        if user.acct_type is None:
            user = repair_acct_type(user)
        login_user(user, remember=True)
        return redirect(url_for('sb_login'))

    # Serve login page if not already logged in
    return render_template('login.html', form=form)

@app.route('/auth/google', methods=['POST'])
def google_auth():
    payload = request.get_json() 
    if payload is not None and payload['g_token'] is not None: # if there's a google token
        google_email = validate_google_user(payload['g_token'])
        logger.debug(f"validated google user email: {google_email}")
        user = User.get_user_by_username(google_email) 
        # check to see if the user already exists, log them in if yes, unless it's a BAM acct
        if user is not None:
            if user.acct_type is None:
                user = repair_acct_type(user)

            if user.acct_type == "bam":
                logger.info("username already taken by a BAM acct")
                flash("User already exists with BAM credentials, please sign in with those.")
                return redirect(url_for('bam_login'))

            if user.acct_type == "fb":
                logger.info("username already taken by a Facebook-linked BAM acct")
                flash("User already exists with Facebook credentials, please sign in with those.")
                return redirect(url_for('bam_login'))

            logger.debug("logging in already registered google user")
            login_user(user, remember=True)
            return redirect(url_for('sb_login'))

        # otherwise register the new user and log them in
        new_user = User.create_user(google_email, provider_access_token=payload['g_token'], acct_type="google")
        if new_user is None:
            logger.debug("Unknown google registration error with None user on creation.")
            return redirect(url_for('register'))

        logger.info(f"created google user: {new_user}")
        login_user(new_user, remember=True)
        return redirect(url_for('sb_login'))

    logger.debug("Unknown google auth error.")
    return redirect(url_for('register'))

@app.route('/auth/fb', methods=['POST'])
def fb_auth():
    payload = request.get_json()
    logger.debug(f"facebook payload: {payload}")
    if payload is not None and payload['fb_token'] is not None: # if there's a facebook token
        fb_email = validate_facebook_user(payload['fb_token'], payload['fb_user_id'])
        logger.debug(f"validated facebook user email: {fb_email}")
        user = User.get_user_by_username(fb_email)
        # check to see if the user already exists, log them in if yes, unless it's a BAM acct
        if user is not None:
            if user.acct_type is None:
                user = repair_acct_type(user)

            if user.acct_type == "bam":
                logger.warn("username already taken by a BAM acct")
                flash("User already exists with BAM credentials, please sign in with those")
                return redirect(url_for('bam_login'))

            if user.acct_type == "google":
                logger.warn("username already taken by a Google-linked BAM acct")
                flash("User already exists with Google credentials, please sign in with those")
                return redirect(url_for('bam_login'))

            logger.debug("logging in already registered facebook user")
            login_user(user, remember=True)
            return redirect(url_for('sb_login'))

        # otherwise register the new user and log them in
        new_user = User.create_user(fb_email, acct_type="fb")
        if new_user is None:
            logger.debug("Unknown facebook registration error with None user on creation.")
            return redirect(url_for('register'))

        logger.info(f"created facebook user: {new_user}")
        login_user(new_user, remember=True)
        return redirect(url_for('sb_login'))


    logger.debug("Unknown facebook auth error.")
    return redirect(url_for('register'))

@app.route('/logout/bam')
def bam_logout():
    logout_user()
    return redirect(url_for('bam_login'))

@app.route('/login/bose')
@login_required
def sb_login():
    refresh_token = current_user.get_refresh_token()
    if refresh_token:
        return redirect(url_for('app_home'))

    client_id = os.environ['SB_CLIENT_ID']
    redirect_url = os.environ['SB_REDIRECT_URL']

    if current_user.acct_type is None:
        repair_acct_type(current_user, silent=True)

    # First time? Get the login button
    return render_template('bose-link.html', client_id=client_id, redirect_url=redirect_url)

@app.route('/logout/bose')
@login_required
def sb_logout():
    # Disassociate the Bose Account from the BAM Account
    current_user.clear_tokens()
    return redirect(url_for('sb_login'))

@app.route('/auth')
def auth_redirect():
    # get Switchboard tokens and put in session
    t_auth_header = 'Basic ' + b64encode_str(os.environ['SB_CLIENT_ID'] + ':' + os.environ['SB_SECRET'])
    t_headers = {'Authorization': t_auth_header}
    t_data = {'grant_type': 'authorization_code', 'code': request.args['code'], 'redirect_uri': os.environ['SB_REDIRECT_URL']}
    tokens = requests.post('https://partners.api.bose.io/auth/oauth/token', headers=t_headers, data=t_data)
    current_user.set_refresh_token(tokens.json()['refresh_token'])
    current_user.set_access_token(tokens.json()['access_token'])

    # Redirect to app
    return redirect(url_for('app_home'))

@app.route('/app')
@login_required
def app_home():
    access_token = current_user.get_access_token()
    if access_token:
        products_res = get_products(access_token)
        if products_res.status_code == 403:
            refresh_token = current_user.get_refresh_token()
            if refresh_token:
                access_token = refresh_sb_token(refresh_token)
                if access_token is None:
                    current_user.clear_tokens()
                    return redirect(url_for('sb_login'))
                current_user.set_access_token(access_token)
                products_res = get_products(access_token)
            else:
                return redirect(url_for('sb_login'))
                
        products_array = products_res.json()['results']
        logger.debug(f"products: {products_array}")

        client_products = []
        image_filenames = []
        for p in products_array:
            image_name, product_image_filenames = get_product_image_name_and_filenames('Bose Home Speaker 500')
            # TODO: image_name, product_image_filenames = get_product_image_name_and_filenames(p['productType'])
            image_filenames += product_image_filenames
            client_products.append({
                'product_id': p['productID'],
                'product_name': p['productName'],
                'image_name': 'eddie-black' # TODO: image_name
            })
        
        if current_user.preferred_volume is None:
            current_user.set_preferred_volume(DEFAULT_PREFERRED_VOLUME)

        if current_user.acct_type is None:
            repair_acct_type(current_user, silent=True)

        # List the products
        return render_template('app.html', products=client_products, image_filenames=set(image_filenames))
                
    else:
        return redirect(url_for('sb_login'))

@app.route('/send', methods=['POST'])
@login_required
def play_msg():
    requested_msg = request.get_json()
    logger.debug(requested_msg)

    access_token = current_user.get_access_token()
    if access_token:
        an_res = send_audio_notification(access_token, requested_msg['target_product'], requested_msg['url'], volume=requested_msg['volume'])
        if an_res.status_code == 403:
            refresh_token = current_user.get_refresh_token()
            if refresh_token:
                access_token = refresh_sb_token(refresh_token)
                if access_token is None:
                    current_user.clear_tokens()
                    return redirect(url_for('sb_login'))
                current_user.set_access_token(access_token)
                an_res = send_audio_notification(access_token, requested_msg['target_product'], requested_msg['url'], volume=requested_msg['volume'])
            else:
                return redirect(url_for('sb_login'))

        logger.debug(an_res.json())

        # update the preferred volume 
        if requested_msg['volume'] != current_user.preferred_volume:
            current_user.set_preferred_volume(requested_msg['volume'])

        # tell the browser all was fine
        return "", 204
                
    return redirect(url_for('sb_login'))

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
