# WordSeek Web Solver

Web-only Flask version. Telegram autoplay files and session data have been removed.

## Vercel

1. Upload this folder to GitHub.
2. Import the repository into Vercel.
3. Keep the default settings and deploy.

`vercel.json` routes the Flask app automatically.

## Railway

1. Upload this folder to GitHub.
2. Create a Railway project from the repository.
3. Railway installs `requirements.txt` and runs the command in `railway.json`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:51332`.
