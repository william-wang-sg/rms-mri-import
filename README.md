# RMS MRI import

A small web app for sending MRI account and location files to RMS RiskModeler, then running models, grouping, and exporting results. It is the notebook `MRI_API_Submission.ipynb` turned into an app you can host from a GitHub repo.

| Notebook | App |
|---|---|
| Section 2 hardcoded API key | `RMS_API_KEY` secret, never in the code |
| `ipywidgets` form | Streamlit page with a shared sidebar and one tab per step |
| Import, geocode | **Import MRI** tab |
| Catastrophe models | **Run models** tab |
| Analysis grouping | **Group** tab |
| Export to RDM | **Export to RDM** tab |
| Country, state, county accumulation | **Accumulation** tab |

Every action that changes anything in RMS needs its own confirmation checkbox, which unchecks itself when the action starts.

## Before you push anything

The original notebook contains a live API key. **Rotate that key in RMS**, and never commit the notebook. `.gitignore` already excludes `*.ipynb`, `.streamlit/secrets.toml` and `.env`.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml: set RMS_API_KEY and APP_PASSWORD
streamlit run app.py
```

You can set `RMS_API_KEY` and `APP_PASSWORD` as environment variables instead of using the secrets file.

The app refuses to run without `APP_PASSWORD`, because anyone who can open it can create portfolios and launch jobs on your RMS account. If something in front of it already handles sign-in (SSO proxy, VPN-only network), set `MRI_APP_ALLOW_NO_AUTH=1` to skip the password.

## Host it from GitHub

GitHub Pages can't run this: it only serves static files, and this app needs Python and a secret key. Pick one of these instead.

**Streamlit Community Cloud** (simplest; deploys straight from the repo)
1. Push this repo to GitHub. A private repo is recommended.
2. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub and choose **New app**. Select the repo, branch, and `app.py`.
3. Under **Advanced settings → Secrets**, paste the two lines from `.streamlit/secrets.toml.example` with your real values.
4. Keep the app private and add teammates by email under **Share**. Check Streamlit's docs for the current limits on private apps.

**Docker** (any container host: Azure Container Apps or App Service, AWS, Google Cloud Run, your own server)
```bash
docker build -t rms-mri-import .
docker run -p 8501:8501 -e RMS_API_KEY=... -e APP_PASSWORD=... rms-mri-import
```
Set the same two variables in your host's secret store. The container listens on port 8501.

**Databricks Apps** (runs inside your Databricks workspace)
1. Store the two values as secrets in a Databricks secret scope.
2. Create an app in your workspace and deploy from this repo (for example through a Git folder). `app.yaml` holds the start command.
3. In the app's settings, add two **secret** resources with the keys `rms-api-key` and `app-password`, pointing at those secrets. `app.yaml` passes them to the app as `RMS_API_KEY` and `APP_PASSWORD`.
4. The workspace needs outbound network access to the RMS API.

Not tested on Databricks yet. Check the current Databricks Apps docs if a step differs.

**GitHub Codespaces** (no hosting; run it in a browser-based dev environment)
Add `RMS_API_KEY` and `APP_PASSWORD` as Codespaces secrets, open the repo in a codespace, and run `streamlit run app.py`. The port opens as a private preview.

## Things to know

- **Your MRI files pass through whatever host runs the app.** If your data can't leave your own infrastructure, use the Docker option on a host you control.
- **Long jobs run inside the open page.** Model runs can take an hour or more. Keep the tab open and don't click other controls while a job runs. If the page closes, jobs already submitted keep running in RMS; use **Group** later and it finds them by Run ID.
- **Model IDs, profile IDs and currency settings** live at the top of `rms_client.py`, exactly as they were in the notebook.
- **Touchstone upload is not included.** In the notebook it was an unused placeholder with no verified API contract, and the form never called it.
- New portfolios are now described as "Created by the MRI RMS submission app" instead of naming the notebook.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest
```

- `rms_client.py` holds all RMS API logic and has no UI code. It is covered by tests that use fake HTTP responses.
- `app.py` is the Streamlit interface only.
- `.github/workflows/ci.yml` runs `ruff` and `pytest` on every push and pull request.
