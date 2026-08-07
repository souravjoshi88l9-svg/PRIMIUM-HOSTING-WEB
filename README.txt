Railway deploy:
1. Upload ALL these files to GitHub repo ROOT (app.py must be at root)
2. Railway -> New Project -> GitHub repo
3. Variable: SECRET_KEY = any-long-random-string
4. Start command (auto from Procfile): gunicorn app:app --bind 0.0.0.0:$PORT ...
5. Networking -> Generate Domain
6. Open the URL -> Register -> Login

NO bot.py. NO BOT_TOKEN needed.
