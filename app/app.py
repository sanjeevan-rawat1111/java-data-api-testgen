import os
from logging.config import dictConfig
from flask import Flask
from helpers import app as helper_blueprint

dictConfig({
    "version": 1,
    "formatters": {"default": {"format": "[%(asctime)s] %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"wsgi": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "default"}},
    "root": {"level": os.getenv("LOG_LEVEL", "INFO"), "handlers": ["wsgi"]},
})

app = Flask(__name__)
app.register_blueprint(helper_blueprint, url_prefix="/")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
