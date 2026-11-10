import os
from pathlib import Path

from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect

from config import Config
from .db import init_app as init_db
from .routes import main

csrf = CSRFProtect()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    csrf.init_app(app)
    init_db(app)
    app.register_blueprint(main)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="We couldn't find that page."), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error.html", code=500, message="Something went wrong. Please try again."), 500

    @app.errorhandler(413)
    def upload_too_large(_error):
        return render_template("error.html", code=413, message="That upload is larger than the allowed file size."), 413

    return app
