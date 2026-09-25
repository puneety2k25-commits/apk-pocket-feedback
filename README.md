# APK Pocket private feedback relay

This relay keeps the publisher's receiving email address out of the APK. Android contains
only an HTTPS URL such as `https://support.example.com/feedback`. The receiving address and
SMTP credentials stay on the server as environment variables.

## What the app sends

Only data the user submits in the Feedback form:

- category (Suggestion / Bug report / Other)
- message
- optional reply email entered by the user
- APK Pocket version
- Android version and phone model **only if the user enables the checkbox**

The installed-app inventory, extracted files, favorites, purchase token, Google account and
AdMob identifiers are not sent by this feedback flow.

## Server configuration

Use Python 3.11+ behind HTTPS. Create a virtual environment and install the small runtime:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Set these values privately on the host:

```sh
export SMTP_HOST=smtp.your-provider.example
export SMTP_PORT=587
export SMTP_SECURITY=starttls
export SMTP_USERNAME='...'
export SMTP_PASSWORD='...'
export FEEDBACK_FROM_EMAIL='noreply@yourdomain.example'
export FEEDBACK_TO_EMAIL='your-private-inbox@example.com'
```

`FEEDBACK_TO_EMAIL` is intentionally server-only. Do not put it in Android, public JavaScript,
GitHub Actions artifacts, app-ads.txt, or the public website source.

Run behind a TLS reverse proxy:

```sh
gunicorn --bind 127.0.0.1:8090 --workers 2 --threads 2 --timeout 30 wsgi:application
```

Expose only HTTPS `/feedback` (and optionally `/health`) to the Internet. Configure edge/WAF
rate limits and request-size limits. The bundled in-process limiter is only a second safety net.
Do not log request bodies because they can contain a user's feedback and optional reply email.

## Android configuration

Once deployed, set only the public endpoint in root `publisher.properties`:

```properties
FEEDBACK_ENDPOINT=https://support.yourdomain.example/feedback
```

Then rebuild `fullAdsDebug` for testing. Ads release builds deliberately reject a missing or
non-HTTPS feedback endpoint so a release cannot silently ship a broken Send button.

## SMTP provider

Use a dedicated transactional/support sender or a mailbox/provider that permits SMTP from your
server. Do not embed a Gmail password or app password in the APK. Provider credentials belong
only in server secrets/environment variables.
