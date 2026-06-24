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
MEDIA_ROOT=media
DJANGO_ADMIN_USERNAME=
DJANGO_ADMIN_PASSWORD=
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4.1-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
CELERY_BROKER_URL=
CELERY_RESULT_BACKEND=
CELERY_TASK_ALWAYS_EAGER=False
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

Uploaded files default to the local `media/` directory. Set `MEDIA_ROOT` only if you want those files written somewhere else.

7. Start the local development server:

```bash
python manage.py runserver
```

8. If you want background processing for content ingestion and learning-objective generation, start a Celery worker and set a broker URL such as Redis in `.env`:

```bash
celery -A config worker --loglevel=info
```

If `CELERY_BROKER_URL` is left blank, the app will process uploads inline instead.

9. Run tests:

```bash
python manage.py test
```

Then open:

- `http://127.0.0.1:8000/` for the public site
- `http://127.0.0.1:8000/app/login/` for the standalone app

## Notes

- The chatbot is server-side only. `OPENAI_API_KEY` is never exposed to browser code.
- Standalone learning objectives and block/course summaries use the OpenAI API when `OPENAI_API_KEY` is set, with heuristic fallback if the API is unavailable.
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
4. Render will detect `render.yaml` and create one `starter` web service with a 1 GB persistent disk mounted at `/app/data`.
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
- SQLite data is stored at `/app/data/db.sqlite3`.
- Uploaded and imported files are stored at `/app/data/media`.
- Gunicorn binds to Render's `PORT` environment variable.
- The default Render blueprint pins `WEB_CONCURRENCY=1` to reduce SQLite lock contention in low-traffic demo environments.

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

### Durable demo limitations

This deployment shape is intentionally a single disk-backed web service so demos and early pilots can survive restarts and redeploys without introducing Postgres or object storage.

It is a good fit for low-traffic demos, but it has important limits:

- Only data written under `/app/data` persists across restarts and redeploys.
- Render persistent disks are single-instance only, so this service is not designed for horizontal scaling.
- Disk-backed Render services do not get zero-downtime deploys; a redeploy briefly stops the existing instance before the new one starts.
- SQLite remains the application database, so this setup is suited to demos and pilots rather than higher-concurrency production workloads.

### One-time cutover for an existing free or manual Render service

If the currently running service is still using Render Free or an older manual setup:

1. Upgrade or recreate it as the `starter` disk-backed service defined in `render.yaml`.
2. Before the final cutover, copy any existing SQLite database into `/app/data/db.sqlite3`.
3. If existing uploaded or imported files matter, copy the current `media/` directory into `/app/data/media/`.
4. After the cutover, treat `render.yaml` as the source of truth and keep the Render dashboard configuration aligned with it.

### If you later outgrow this deployment shape

Use one of these:

- Move relational data to Render Postgres
- Move uploaded files to object storage
