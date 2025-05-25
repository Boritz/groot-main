import logging

from app import create_app


app = create_app()

@app.route("/")
def index():
    return "Groot bot is running!"

if __name__ == "__main__":
    logging.info("Flask app started")
    app.run()
