# MCQ Anchor

MCQ Anchor is a Django-based pilot website for an educational assessment concept that combines continuous online MCQ practice with short controlled validation checks.

## Stack

- Python 3.12+
- Django
- SQLite for local development
- Django templates
- Plain CSS
- Lightweight vanilla JavaScript
- OpenAI Python SDK for the product chatbot

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
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4.1-mini
CONTACT_EMAIL=replace-me@example.com
```

6. Run migrations:

```bash
python manage.py migrate
```

7. Start the local development server:

```bash
python manage.py runserver
```

8. Run tests:

```bash
python manage.py test
```

Then open `http://127.0.0.1:8000/`.

## Notes

- The chatbot is server-side only. `OPENAI_API_KEY` is never exposed to browser code.
- The site stores pilot enquiries in SQLite via the `PilotEnquiry` model.
- `LTI 1.3 enabled. Integrate into your VLE seamlessly or use it as a standalone product.` is the canonical LTI/VLE positioning line for the project.
- The UI is designed to target WCAG 2.2 AA expectations for color contrast, focus visibility, keyboard access, and responsive readability. A final production release should still be checked with automated and manual accessibility audits.
