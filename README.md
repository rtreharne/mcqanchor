# MCQ Anchor

MCQ Anchor is a Django-based product codebase with two surfaces:

- a public marketing/pilot site at `/`
- a standalone app at `/app/` for invite-based teacher and student workflows

## Stack

- Python 3.12+
- Django
- SQLite for local development
- SQLite for the initial single-service Render deployment
- Django templates
- Plain CSS
- Lightweight vanilla JavaScript
- OpenAI Python SDK for the product chatbot
- OpenAI Python SDK for standalone content/question generation
- Docker
- WhiteNoise for static file serving

## Local setup

### Ubuntu or macOS

1. Create the virtual environment:

```bash
python3 -m venv .venv
```

2. Activate the virtual environment:

```bash
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy the environment template:

```bash
cp .env.example .env
```

5. Edit `.env` and set the required environment variables:

```text
DJANGO_SECRET_KEY=replace-with-a-secret-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=
SQLITE_PATH=standalone.sqlite3
DJANGO_ADMIN_USERNAME=
DJANGO_ADMIN_PASSWORD=
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4.1-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
CONTACT_EMAIL=replace-me@example.com
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
DEFAULT_FROM_EMAIL=no-reply@mcqanchor.local
STANDALONE_ENABLE_MAGIC_LINKS=True
STANDALONE_ENABLE_SELF_ENROL=True
STANDALONE_INVITE_EXPIRY_HOURS=72
STANDALONE_MAGIC_LINK_EXPIRY_HOURS=72
```

6. Run migrations:

```bash
python manage.py migrate
```

If you are switching from the earlier marketing-site-only branch, use a fresh SQLite file for the standalone branch. The default local database path is `standalone.sqlite3`.

7. Start the local development server:

```bash
python manage.py runserver
```

8. Run tests:

```bash
python manage.py test
```

Then open:

- `http://127.0.0.1:8000/` for the public site
- `http://127.0.0.1:8000/app/login/` for the standalone app

## Notes

- The chatbot is server-side only. `OPENAI_API_KEY` is never exposed to browser code.
- The site stores pilot enquiries in SQLite via the `PilotEnquiry` model.
- The standalone app uses a custom Django user model plus course, enrolment, content-ingestion, question-bank, practice, and validation tables under the `standalone` app.
- Supported standalone upload types are `.html`, `.docx`, `.pdf`, `.txt`, `.R`, `.py`, `.ipynb`, `.Rmd`, `.md`, `.pptx`, and `.xlsx`.
- Standalone validation v1 currently generates printable PDF packs with QR identifiers; live scan/OMR capture is still a later step.
- `LTI 1.3 enabled. Integrate into your VLE seamlessly or use it as a standalone product.` is the canonical LTI/VLE positioning line for the project.
- The UI is designed to target WCAG 2.2 AA expectations for color contrast, focus visibility, keyboard access, and responsive readability. A final production release should still be checked with automated and manual accessibility audits.

## Render deployment

This repo includes:

- `Dockerfile`
- `bin/render-start.sh`
- `render.yaml`

These are set up for a single Docker-based Render web service using a Blueprint from GitHub.

### Deploy from GitHub with a Render Blueprint

1. Push this repository to GitHub.
2. In Render, choose `New +` -> `Blueprint`.
3. Connect the GitHub repository.
4. Render will detect `render.yaml` and create one `free` web service.
5. When prompted, provide values for:

```text
DJANGO_ADMIN_USERNAME
DJANGO_ADMIN_PASSWORD
CONTACT_EMAIL
OPENAI_API_KEY
```

6. Finish the Blueprint deploy.

### Runtime behavior on Render

- The container runs `python manage.py migrate --noinput` on startup.
- The container runs `python manage.py ensure_admin_user` on startup after migrations.
- Static files are collected on startup and served by WhiteNoise.
- Gunicorn binds to Render's `PORT` environment variable.

### Admin login on Render

If `DJANGO_ADMIN_USERNAME` and `DJANGO_ADMIN_PASSWORD` are set, each startup will create or update that Django superuser automatically. You can then sign in at `/admin`.

### Custom domain

The Render Blueprint registers `mcqanchor.com` as the service custom domain. Render automatically adds the corresponding `www.mcqanchor.com` host and redirects it to the root domain.

Django is configured through `render.yaml` to accept:

```text
mcqanchor.com
www.mcqanchor.com
*.onrender.com
```

After deploying the Blueprint, finish setup in Render's Custom Domains section:

1. Verify that `mcqanchor.com` is listed for the `mcq-anchor` web service.
2. Add the DNS records Render shows at your domain provider.
3. Remove any conflicting `AAAA` records for the domain.
4. Return to Render and click Verify.

### Important limitation of the single free-service setup

The initial Render setup uses SQLite inside the web service so everything stays in one app. This is suitable for demos and early pilot review, but not for durable production storage on the free plan.

On Render Free web services, the filesystem is ephemeral. That means:

- `db.sqlite3` data is lost on redeploy
- `db.sqlite3` data is lost on restart
- `db.sqlite3` data is lost when the free instance spins down and comes back

So contact submissions and any other SQLite-backed data should be treated as temporary in this deployment shape.

### If you later want durable data on Render

Use one of these:

- Move to a paid Render web service and attach a persistent disk for SQLite
- Keep the web service on free or paid and move relational data to Render Postgres
