"""RMS RiskModeler API client.

Ported from MRI_API_Submission.ipynb. This module has no UI dependencies: callers pass
`log` and `progress` callbacks and get plain Python results back, which keeps the logic
testable and lets the Streamlit app (app.py) stay thin.

The API key is never read from this file. Pass it to RmsClient explicitly.
"""
from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------------------
# Configuration (same values as the notebook). Edit here if your RMS setup changes.
# --------------------------------------------------------------------------------------
RMS_API_ROOT = "https://api-use1.rms.com"
RMS_BASE_URL = f"{RMS_API_ROOT}/platform"
RMS_EXPOSURES_URL = f"{RMS_BASE_URL}/riskdata/v1/exposures"
RMS_IMPORT_FOLDERS_URL = f"{RMS_BASE_URL}/import/v1/folders"
RMS_IMPORT_JOBS_URL = f"{RMS_BASE_URL}/import/v1/jobs"
ANALYSES_URL = f"{RMS_BASE_URL}/riskdata/v1/analyses"
RMS_ENTITLEMENT = "RI-RISKMODELER"

MODEL_CONFIG = {
    "US_PORT_NAEQ_v22": {"model_profile_id": 21, "event_rate_scheme_id": 168},
    "US_PORT_NAHU_v22": {"model_profile_id": 22, "event_rate_scheme_id": 203},
    "US_PORT_NACS_v22": {"model_profile_id": 27, "event_rate_scheme_id": 69},
}
OUTPUT_PROFILE_ID = 55
CURRENCY_AS_OF_DATE = "2025-05-28"
CURRENCY_VINTAGE = "RL25"

COUNTRY_ACCUMULATION_PROFILES = [(6, "EQ"), (7, "CS"), (17, "FL"), (18, "WS"), (19, "WF")]
STATE_ACCUMULATION_PROFILES = [(13, "EQ"), (23, "CS"), (24, "FL"), (12, "WS"), (25, "WF")]
COUNTY_ACCUMULATION_PROFILES = [(15, "EQ"), (20, "CS"), (22, "FL"), (14, "WS"), (21, "WF")]
ACCUMULATION_PROFILE_SETS = {
    "Country": ("CNTRY", COUNTRY_ACCUMULATION_PROFILES),
    "State": ("STATE", STATE_ACCUMULATION_PROFILES),
    "County": ("COUNTY", COUNTY_ACCUMULATION_PROFILES),
}

GROUP_NUM_SIMULATIONS = 50000  # required by the Grouping API; confirm the value you want

# Optional: send only the columns the CATFlow generator produces (renames MINDEDAMT ->
# BLANDEDAMT and fills ACCGRPID/ISVALID). Off by default, as in the notebook.
MRI_ACCOUNT_COLUMNS = [
    "ACCGRPID", "ACCNTNUM", "ACCNTNAME", "PARTOF", "BLANLIMAMT", "BLANDEDAMT", "ISVALID",
    "POLICYTYPE", "POLICYNUM",
]
MRI_LOCATION_COLUMNS = [
    "ACCNTNUM", "LOCNUM", "STREETNAME", "CITY", "STATECODE", "POSTALCODE",
    "CNTRYSCHEME", "CNTRYCODE", "BLDGSCHEME", "BLDGCLASS", "OCCSCHEME", "OCCTYPE",
    "YEARBUILT", "NUMSTORIES", "FLOORAREA",
    "EQCV1VAL", "WSCV1VAL", "TOCV1VAL", "FLCV1VAL", "FRCV1VAL",
    "EQSITEDED", "WSSITEDED", "TOSITEDED", "FLSITEDED", "FRSITEDED",
    "USERID1", "USERID2", "ISVALID", "LATITUDE", "LONGITUDE",
]
MRI_ACCOUNT_RENAMES = {"BLANDEDAMT": "MINDEDAMT"}
MRI_ACCOUNT_DEFAULTS = {"ACCGRPID": "1", "ISVALID": "1"}
MRI_LOCATION_DEFAULTS = {"ISVALID": "1"}

FAILED_JOB_STATUSES = {"FAILED", "ERROR", "CANCELED", "CANCELLED", "FINISHED_WITH_ERRORS"}
FAILED_ANALYSIS_STATUSES = {
    "failed", "error", "cancelled", "canceled", "error in analyzing", "error in persisting",
}

Callback = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --------------------------------------------------------------------------------------
# Pure helpers (no network)
# --------------------------------------------------------------------------------------
def response_items(payload) -> list:
    if isinstance(payload, dict) and "items" in payload:
        return payload["items"]
    if isinstance(payload, list):
        return payload
    return []


def check_response(response: requests.Response) -> None:
    if not response.ok:
        raise requests.HTTPError(
            f"RMS request failed ({response.status_code}) for {response.url}: {response.text[:1000]}",
            response=response,
        )


def absolute_url(location: str) -> str:
    return location if location.startswith("http") else f"{RMS_API_ROOT}{location}"


def format_elapsed(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_job_progress(job: dict, elapsed: float) -> tuple[float | None, str]:
    raw = job.get("progress")
    try:
        percent = max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        percent = None
    if percent is None:
        return None, f"elapsed {format_elapsed(elapsed)}"
    filled = int(percent // 5)
    bar = f"[{'#' * filled}{'-' * (20 - filled)}]"
    return percent, f"{bar} {percent:.0f}% elapsed {format_elapsed(elapsed)}"


def decode_upload_target(upload_detail: dict) -> dict:
    """Decode the base64 presign parameters and split the upload URL into bucket/key."""
    presign = upload_detail["presignParams"]

    def decode(value: str) -> str:
        return base64.b64decode("".join(value.split())).decode("utf-8")

    upload_url = urlparse(upload_detail["uploadUrl"])
    host = upload_url.netloc
    path = upload_url.path.lstrip("/")
    if ".s3." in host:  # virtual-hosted style: <bucket>.s3.<region>.amazonaws.com/<key>
        bucket, key = host.split(".s3.", 1)[0], path
    else:  # path style: s3.<region>.amazonaws.com/<bucket>/<key>
        bucket, key = path.split("/", 1)
    return {
        "access_key": decode(presign["accessKeyId"]),
        "secret_key": decode(presign["secretAccessKey"]),
        "session_token": decode(presign["sessionToken"]),
        "region": decode(presign["region"]),
        "bucket": bucket,
        "key": key,
        "file_uri": upload_detail["fileUri"],
    }


def select_ready_run(
    analyses: list, portfolio_id: str, run_id: str, log: Callback = _noop
) -> tuple[str, dict]:
    """Return (run_id, {model_name: analysis}) for the requested run, else the latest run
    in which every configured model has status Analyzed."""
    runs: dict[str, dict] = {}
    for item in analyses:
        name = str(item.get("analysisName", ""))
        for model_name in MODEL_CONFIG:
            prefix = f"{model_name}_{portfolio_id}_"
            if name.startswith(prefix):
                runs.setdefault(name[len(prefix):], {})[model_name] = item
    ready = {
        run: found
        for run, found in runs.items()
        if len(found) == len(MODEL_CONFIG)
        and all(str(i.get("analysisStatus", "")).lower() == "analyzed" for i in found.values())
    }
    log(f"Runs of the configured models on this portfolio: {sorted(runs)}")
    if not ready:
        raise ValueError("No Run ID has all configured models Analyzed for this portfolio.")
    if run_id in ready:
        chosen = run_id
    else:
        chosen = max(ready, key=lambda r: max(int(i["analysisId"]) for i in ready[r].values()))
        log(f"Run ID '{run_id}' has no complete set; using the latest complete run instead.")
    log(f"Run: {chosen}")
    return chosen, ready[chosen]


def analysis_uri(item: dict) -> str:
    return item.get("uri") or f"{RMS_BASE_URL}/riskdata/v1/analyses/{item['analysisId']}"


def write_minimal_mri_file(source: Path, columns: list, defaults: dict, renames: dict,
                           log: Callback = _noop) -> Path:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [line.rstrip("\r\n").split("\t") for line in handle if line.strip()]
    header, body = rows[0], rows[1:]
    picked, indexes = [], []
    for name in columns:
        source_name = renames.get(name, name)
        if source_name in header:
            picked.append(name)
            indexes.append(header.index(source_name))
        elif name in defaults:
            picked.append(name)
            indexes.append(None)
    lines = ["\t".join(picked)]
    for row in body:
        lines.append(
            "\t".join(
                defaults[name] if index is None else (row[index] if index < len(row) else "")
                for name, index in zip(picked, indexes)
            )
        )
    out_dir = Path(tempfile.gettempdir()) / "MRI_API_Submission" / "prepared"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / source.name
    target.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    log(f"{source.name}: {len(body)} data row(s); columns sent: {picked}")
    return target


def make_session(api_key: str) -> requests.Session:
    if not api_key:
        raise RuntimeError("RMS_API_KEY is empty. Set it as a secret or environment variable.")
    session = requests.Session()
    session.headers.update({"accept": "application/json", "Authorization": api_key})
    retries = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------
class RmsClient:
    def __init__(
        self,
        api_key: str,
        log: Callback = _noop,
        progress: Callback = _noop,
        session: requests.Session | None = None,
    ):
        self.session = session or make_session(api_key)
        self.log = log
        self.progress = progress
        self._resource_group_id: str | None = None

    # ---- lookups ----------------------------------------------------------------------
    @property
    def resource_group_id(self) -> str:
        if self._resource_group_id is None:
            url = f"{RMS_API_ROOT}/platform/tenantdata/v1/entitlements/{RMS_ENTITLEMENT}/resourcegroups"
            response = self.session.get(url, timeout=30)
            check_response(response)
            groups = response.json()
            if not groups:
                raise RuntimeError("No RiskModeler resource groups are available to this API key.")
            selected = next((g for g in groups if g.get("default") is True), groups[0])
            self._resource_group_id = str(selected["resourceGroupId"])
        return self._resource_group_id

    def _rg_headers(self, **extra) -> dict:
        return {"x-rms-resource-group-id": self.resource_group_id, **extra}

    def list_exposures(self) -> list[str]:
        rows, offset = [], 0
        while True:
            response = self.session.get(
                RMS_EXPOSURES_URL, params={"limit": 100, "offset": offset}, timeout=30
            )
            check_response(response)
            page = response_items(response.json())
            if not page:
                break
            rows.extend(page)
            if len(page) < 100:
                break
            offset += 100
        return sorted({str(r["exposureName"]) for r in rows if r.get("exposureName")}, key=str.casefold)

    def get_exposure_id(self, exposure_name: str) -> int:
        response = self.session.get(
            RMS_EXPOSURES_URL, params={"filter": f'exposureName = "{exposure_name}"'}, timeout=30
        )
        check_response(response)
        matches = [i for i in response_items(response.json()) if i.get("exposureName") == exposure_name]
        if not matches:
            raise ValueError(f"RMS exposure/EDM not found: {exposure_name}")
        return int(matches[0]["exposureId"])

    def list_portfolios(self, exposure_name: str) -> list[dict]:
        """Return [{'id': '12', 'name': 'Book A', 'uri': '...'}, ...]."""
        exposure_id = self.get_exposure_id(exposure_name)
        response = self.session.get(
            f"{RMS_EXPOSURES_URL}/{exposure_id}/portfolios", headers=self._rg_headers(), timeout=30
        )
        check_response(response)
        out = []
        for item in response_items(response.json()):
            name, pid = str(item.get("portfolioName", "")), str(item.get("portfolioId", ""))
            if name and pid:
                out.append({"id": pid, "name": name, "uri": str(item.get("uri", ""))})
        return out

    def get_or_create_portfolio(
        self, exposure_id: int, name: str, number: str, create_if_missing: bool
    ) -> tuple[str, int]:
        url = f"{RMS_EXPOSURES_URL}/{exposure_id}/portfolios"

        def find():
            response = self.session.get(url, headers=self._rg_headers(), timeout=30)
            check_response(response)
            return next(
                (i for i in response_items(response.json()) if i.get("portfolioName") == name), None
            )

        match = find()
        if match:
            return match["uri"], int(match["portfolioId"])
        if not create_if_missing:
            raise ValueError(f"Portfolio not found: {name}")
        response = self.session.post(
            url,
            json={
                "portfolioName": name,
                "portfolioNumber": number,
                "description": "Created by the MRI RMS submission app",
            },
            headers=self._rg_headers(**{"content-type": "application/json"}),
            timeout=30,
        )
        check_response(response)
        match = find()
        if not match:
            raise RuntimeError("Portfolio creation succeeded but the new portfolio was not returned by RMS.")
        return match["uri"], int(match["portfolioId"])

    # ---- jobs -------------------------------------------------------------------------
    def poll_job(self, job_url: str, timeout_seconds: int = 3600, label: str = "RMS job") -> dict:
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            response = self.session.get(job_url, timeout=30)
            check_response(response)
            job = response.json()
            status = str(job.get("status", "")).upper()
            _, detail = format_job_progress(job, time.monotonic() - started)
            self.progress(f"{label}: {status or 'unknown'} {detail}")
            if status == "FINISHED":
                return job
            if status in FAILED_JOB_STATUSES:
                raise RuntimeError(f"{label} did not complete successfully: {job}")
            time.sleep(5)
        raise TimeoutError(f"{label} did not finish within {timeout_seconds} seconds.")

    def _job_url_from(self, response: requests.Response, what: str) -> str:
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError(f"RMS accepted the {what} but returned no Location header.")
        return absolute_url(location)

    # ---- MRI import -------------------------------------------------------------------
    def upload_file(self, upload_detail: dict, local_path: Path) -> str:
        import boto3  # imported lazily so tests and the pure helpers don't need it

        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        target = decode_upload_target(upload_detail)
        s3 = boto3.client(
            "s3",
            aws_access_key_id=target["access_key"],
            aws_secret_access_key=target["secret_key"],
            aws_session_token=target["session_token"],
            region_name=target["region"],
        )
        s3.upload_file(str(local_path), target["bucket"], target["key"])
        return target["file_uri"].rstrip("/").split("/")[-1]

    def _location_counts(self, exposure_id: int, portfolio_id: int):
        url = f"{RMS_EXPOSURES_URL}/{exposure_id}/portfolios/{portfolio_id}/accounts"
        accounts, offset, page_size = [], 0, 1000
        while True:
            response = self.session.get(
                url, params={"limit": page_size, "offset": offset}, headers=self._rg_headers(), timeout=30
            )
            check_response(response)
            page = response_items(response.json())
            if not page:
                break
            accounts.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        locations = sum(int(a.get("locationsCount") or 0) for a in accounts)
        without = [
            str(a.get("accountNumber") or a.get("accountId") or "unknown")
            for a in accounts
            if not int(a.get("locationsCount") or 0)
        ]
        return len(accounts), locations, without

    def verify_portfolio_has_locations(self, portfolio_uri: str, attempts: int = 3,
                                       delay_seconds: int = 5) -> dict:
        segments = [s for s in urlparse(portfolio_uri).path.split("/") if s]
        try:
            i = segments.index("exposures")
            exposure_id, portfolio_id = int(segments[i + 1]), int(segments[i + 3])
        except (ValueError, IndexError) as error:
            raise ValueError(f"Could not parse the RMS portfolio resource URI: {portfolio_uri}") from error

        for attempt in range(1, attempts + 1):
            n_accounts, n_locations, without = self._location_counts(exposure_id, portfolio_id)
            if n_accounts and n_locations:
                break
            if attempt < attempts:
                time.sleep(delay_seconds)
        verified = bool(n_accounts and n_locations)
        if not verified:
            # Only a convenience pre-check: the import job already reported FINISHED, so do not
            # block geocoding on it. Geohaz itself fails if the portfolio is empty.
            samples = ", ".join(without[:5]) or "none"
            self.log(
                f"WARNING: could not confirm imported data via the accounts API ({n_accounts} "
                f"account(s), {n_locations} location(s); accounts without locations: {samples}). "
                "Continuing with geocoding. If Geohaz fails, check that the location file has data "
                "rows and each location ACCNTNUM matches an account ACCNTNUM."
            )
        return {"account_count": n_accounts, "location_count": n_locations, "verified": verified}

    def geocode(self, portfolio_uri: str) -> tuple[str, dict]:
        counts = self.verify_portfolio_has_locations(portfolio_uri)
        if counts["verified"]:
            self.log(
                f"Geohaz preflight: {counts['account_count']} account(s), "
                f"{counts['location_count']} linked location(s)."
            )
        payload = {
            "resourceUri": portfolio_uri,
            "resourceType": "portfolio",
            "settings": {
                "layers": [{
                    "type": "geocode", "name": "geocode", "engineType": "RL", "version": "25.0",
                    "layerOptions": {
                        "geoLicenseType": "0", "aggregateTriggerEnabled": False, "skipPrevGeocoded": True,
                    },
                }]
            },
        }
        response = self.session.post(
            f"{RMS_BASE_URL}/geohaz/v1/jobs", json=payload,
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=30,
        )
        check_response(response)
        location = response.headers.get("Location")
        if location:
            job_url = absolute_url(location)
        else:
            body = response.json() if (response.text or "").strip() else {}
            if not body.get("jobId"):
                raise RuntimeError("Geohaz job was created but no Location header or jobId was returned.")
            job_url = f"{RMS_BASE_URL}/geohaz/v1/jobs/{body['jobId']}"
        return job_url, self.poll_job(job_url, label="RMS Geohaz geocoding")

    def import_mri(
        self,
        account_path: Path,
        location_path: Path,
        exposure_name: str,
        portfolio_name: str,
        portfolio_number: str = "",
        create_portfolio: bool = True,
        minimal_columns: bool = False,
        append_locations: bool = False,
    ) -> dict:
        for path in (account_path, location_path):
            if not path.is_file():
                raise FileNotFoundError(path)
            if path.suffix.lower() != ".txt":
                raise ValueError(f"Expected RMS MRI .txt file, got: {path.name}")

        exposure_id = self.get_exposure_id(exposure_name)
        portfolio_uri, portfolio_id = self.get_or_create_portfolio(
            exposure_id, portfolio_name, portfolio_number, create_portfolio
        )
        self.log(f"Portfolio '{portfolio_name}' (ID {portfolio_id}) ready.")

        response = self.session.post(
            RMS_IMPORT_FOLDERS_URL,
            json={"folderType": "MRI",
                  "properties": {"fileExtension": "txt", "fileTypes": ["accountsFile", "locationsFile"]}},
            headers={"content-type": "application/json"}, timeout=30,
        )
        check_response(response)
        folder = response.json()
        details = folder["uploadDetails"]

        if minimal_columns:
            account_path = write_minimal_mri_file(
                account_path, MRI_ACCOUNT_COLUMNS, MRI_ACCOUNT_DEFAULTS, MRI_ACCOUNT_RENAMES, self.log)
            location_path = write_minimal_mri_file(
                location_path, MRI_LOCATION_COLUMNS, MRI_LOCATION_DEFAULTS, {}, self.log)

        self.log("Uploading account file...")
        account_id = self.upload_file(details["accountsFile"], account_path)
        self.log("Uploading location file...")
        location_id = self.upload_file(details["locationsFile"], location_path)

        response = self.session.post(
            RMS_IMPORT_JOBS_URL,
            json={
                "importType": "MRI",
                "resourceUri": portfolio_uri,
                "settings": {
                    "folderId": str(folder["folderId"]),
                    "importFiles": {"accountsFileId": str(account_id), "locationsFileId": str(location_id)},
                    "currency": "USD",
                    "delimiter": "TAB",
                    "skipLines": 1,
                    # True appends to locations that already exist for the account numbers; earlier
                    # API imports that used True loaded 0 accounts once those numbers existed in the EDM.
                    "appendLocations": append_locations,
                    "geohaz": False,
                },
            },
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=30,
        )
        check_response(response)
        import_job_url = self._job_url_from(response, "MRI import")
        import_result = self.poll_job(import_job_url, label="RMS MRI import")

        summary = (import_result.get("details") or {}).get("summary") or "no summary returned"
        self.log(f"RMS MRI import: job {import_result.get('jobId')} {import_result.get('status')} - {summary}")
        task_errors = [
            e.get("message")
            for task in import_result.get("tasks", [])
            for e in (task.get("output") or {}).get("errors", [])
        ]
        if task_errors:
            self.log(f"Import warnings/errors: {task_errors}")

        try:
            geocode_url, geocode_result = self.geocode(portfolio_uri)
        except Exception as error:
            raise RuntimeError(
                f"MRI import completed, but the follow-up Geohaz geocoding step failed: {error}"
            ) from error
        self.log("Geocoding finished.")
        return {
            "portfolio_id": portfolio_id,
            "portfolio_uri": portfolio_uri,
            "import_job_url": import_job_url,
            "import_job_result": import_result,
            "geocode_job_url": geocode_url,
            "geocode_job_result": geocode_result,
        }

    # ---- catastrophe models -----------------------------------------------------------
    def get_portfolio_analyses(self, portfolio_id: int, analysis_filter: str = "") -> list[dict]:
        rows, offset, page_size, seen_first_ids = [], 0, 1000, set()
        while True:
            params = {"limit": page_size, "offset": offset}
            if analysis_filter:
                params["filter"] = analysis_filter
            response = self.session.get(
                ANALYSES_URL, params=params, headers={"x-rms-resource-group-id": self.resource_group_id},
                timeout=60,
            )
            check_response(response)
            page = response_items(response.json())
            if not page:
                break
            first_id = page[0].get("analysisId")
            if first_id in seen_first_ids:
                break  # RMS returned a page already seen (offset ignored); stop instead of looping
            seen_first_ids.add(first_id)
            rows.extend(
                i for i in page
                if analysis_filter or str(i.get("exposureResourceId", "")) == str(portfolio_id)
            )
            if len(page) < page_size:
                break
            offset += len(page)
        return rows

    def submit_model(self, portfolio_id: int, exposure_name: str, run_id: str, model_name: str) -> str:
        cfg = MODEL_CONFIG[model_name]
        job_name = f"{model_name}_{portfolio_id}_{run_id}"
        payload = {
            "id": int(portfolio_id),
            "exposureType": "PORTFOLIO",
            "edm": exposure_name,
            "currency": {"code": "USD", "scheme": "RMS", "asOfDate": CURRENCY_AS_OF_DATE,
                         "vintage": CURRENCY_VINTAGE},
            "modelProfileId": cfg["model_profile_id"],
            "eventRateSchemeId": cfg["event_rate_scheme_id"],
            "outputProfileId": OUTPUT_PROFILE_ID,
            "jobName": job_name,
            "globalAnalysisSettings": {
                "franchiseDeductible": False, "minLossThreshold": 1.0,
                "treatConstructionOccupancyAsUnknown": True, "numMaxLossEvent": 1,
            },
        }
        response = self.session.post(
            f"{RMS_API_ROOT}/riskmodeler/v2/portfolios/{portfolio_id}/process", json=payload,
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=60,
        )
        check_response(response)
        self.log(f"Submitted catastrophe model: {model_name}")
        return job_name

    def _model_progress_text(self, names, last_status, started, finished_at) -> str:
        now = time.monotonic()
        lines = []
        for name in names:
            status = last_status.get(name) or "SUBMITTED"
            if name in finished_at:
                lines.append(f"  [done]    {name}: {status} in {format_elapsed(finished_at[name] - started)}")
            else:
                lines.append(f"  [running] {name}: {status} - elapsed {format_elapsed(now - started)}")
        return f"Catastrophe models - elapsed {format_elapsed(now - started)}\n" + "\n".join(lines)

    def run_models(self, portfolio_id: int, exposure_name: str, run_id: str,
                   timeout_seconds: int = 7200, poll_seconds: int = 15) -> dict:
        names = list(MODEL_CONFIG)
        job_names = {n: self.submit_model(portfolio_id, exposure_name, run_id, n) for n in names}
        pending, completed, failures = set(names), {}, {}
        last_status, finished_at = {}, {}
        started = time.monotonic()

        while pending and time.monotonic() - started < timeout_seconds:
            analyses = self.get_portfolio_analyses(portfolio_id, f'exposureName = "{exposure_name}"')
            for name in list(pending):
                analysis = next((i for i in analyses if i.get("analysisName") == job_names[name]), None)
                status = str(analysis.get("analysisStatus", "") if analysis else "SUBMITTED").strip()
                last_status[name] = status or "waiting for analysis record"
                normalized = status.lower()
                if normalized == "analyzed":
                    completed[name] = {"analysis_id": analysis.get("analysisId"),
                                       "analysis_name": job_names[name], "status": status}
                    pending.discard(name)
                    finished_at[name] = time.monotonic()
                elif normalized in FAILED_ANALYSIS_STATUSES:
                    failures[name] = status
                    pending.discard(name)
                    finished_at[name] = time.monotonic()
            self.progress(self._model_progress_text(names, last_status, started, finished_at))
            if pending:
                time.sleep(poll_seconds)

        failures.update({n: "TIMEOUT" for n in pending})
        if failures:
            raise RuntimeError(f"Catastrophe model run(s) failed: {failures}; completed: {completed}")
        return completed

    # ---- grouping ---------------------------------------------------------------------
    def default_grouping_set_id(self) -> int:
        response = self.session.get(
            f"{RMS_BASE_URL}/referencedata/v1/groupingsets", params={"limit": 100, "offset": 0},
            headers=self._rg_headers(), timeout=30,
        )
        check_response(response)
        active = [i for i in response_items(response.json()) if i.get("isActive", True)]
        if not active:
            raise RuntimeError("No active grouping set is available; create one in RMS Risk Modeler first.")
        selected = next((i for i in active if i.get("isDefault")), active[0])
        self.log(f"Using grouping set: {selected.get('groupingSetName')} (ID {selected['groupingSetId']})")
        return int(selected["groupingSetId"])

    def group_analyses(self, portfolio_id: int, portfolio_name: str, exposure_name: str,
                       run_id: str) -> dict:
        self.log("Searching analyses for the selected portfolio...")
        analyses = self.get_portfolio_analyses(portfolio_id, f'exposureName = "{exposure_name}"')
        self.log(f"Found {len(analyses)} analysis record(s) in this EDM.")
        run_id, ready = select_ready_run(analyses, str(portfolio_id), run_id, self.log)
        uris = [analysis_uri(i) for i in ready.values()]
        group_name = f"{portfolio_name}_GROUP_{run_id}"
        payload = {
            "resourceType": "analyses",
            "resourceUris": uris,
            "settings": {
                "analysisName": group_name,
                "currency": {"code": "USD", "scheme": "RMS", "asOfDate": CURRENCY_AS_OF_DATE,
                             "vintage": CURRENCY_VINTAGE},
                "numOfSimulations": GROUP_NUM_SIMULATIONS,
                "groupingSetId": self.default_grouping_set_id(),
                "simulateToPLT": False,
                "propagateDetailedLosses": True,
            },
        }
        response = self.session.post(
            f"{RMS_BASE_URL}/grouping/v1/jobs", json=payload,
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=30,
        )
        check_response(response)
        job_url = self._job_url_from(response, "grouping job")
        self.log(f"Grouping job submitted: {job_url}")
        result = self.poll_job(job_url, label="RMS analysis grouping")
        self.log(f"Analysis group '{group_name}': job {result.get('jobId')} {result.get('status')}")
        return {"group_name": group_name, "run_id": run_id, "job_result": result}

    # ---- export to RDM ----------------------------------------------------------------
    def list_rdm_targets(self) -> tuple[dict, dict]:
        """Return ({server_id: server_name}, {database_id: database_record})."""
        response = self.session.get(
            f"{RMS_BASE_URL}/riskdata/v1/dataservers",
            params={"filter": 'serverType = "DATA_BRIDGE"'}, timeout=30,
        )
        check_response(response)
        servers = {str(s["serverId"]): str(s.get("serverName", "")) for s in response_items(response.json())}
        databases, offset = {}, 0
        while True:
            response = self.session.get(
                f"{RMS_API_ROOT}/platform/admindata/v1/databases",
                params={"limit": 100, "offset": offset}, timeout=60,
            )
            check_response(response)
            page = response_items(response.json())
            for db in page:
                if "RDM" in str(db.get("databaseType", "")).upper():
                    databases[str(db["databaseId"])] = db
            if len(page) < 100:
                break
            offset += 100
        return servers, databases

    def export_to_rdm(self, portfolio_id: int, portfolio_name: str, exposure_name: str, run_id: str,
                      server_id: str, database_id: str, database_name: str = "") -> dict:
        analyses = self.get_portfolio_analyses(portfolio_id, f'exposureName = "{exposure_name}"')
        run_id, ready = select_ready_run(analyses, str(portfolio_id), run_id, self.log)
        uris = [analysis_uri(i) for i in ready.values()]

        group_name = f"{portfolio_name}_GROUP_{run_id}"
        groups = self.get_portfolio_analyses(portfolio_id, f'analysisName = "{group_name}"')
        group = next((i for i in groups if i.get("analysisName") == group_name), None)
        if group and str(group.get("analysisStatus", "")).lower() == "analyzed":
            uris.append(analysis_uri(group))
        else:
            self.log(f"WARNING: analysis group {group_name} not found or not Analyzed; "
                     "exporting model analyses only.")
        self.log("Accumulation results are not exported: the Export API only supports RESULTS flat "
                 "files for them.")

        response = self.session.post(
            f"{RMS_BASE_URL}/export/v1/jobs",
            json={
                "exportType": "RDM_DATABRIDGE",
                "resourceType": "analyses",
                "settings": {"serverId": int(server_id), "databaseId": int(database_id),
                             "exportHdLossesAs": "ELT"},
                "resourceUris": uris,
            },
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=60,
        )
        check_response(response)
        job_url = self._job_url_from(response, "export")
        self.log(f"Export job submitted for {len(uris)} analysis result(s): {job_url}")
        result = self.poll_job(job_url, label="RMS RDM export")
        self.log(f"Exported to RDM '{database_name}': job {result.get('jobId')} {result.get('status')}")
        return {"run_id": run_id, "exported": len(uris), "job_result": result}

    # ---- accumulation -----------------------------------------------------------------
    def submit_peril_accumulation(self, portfolio_uri: str, portfolio_name: str, region_code: str,
                                  currency: str, perspective: str, profile_id: int,
                                  peril_code: str) -> str:
        region_name = {"CNTRY": "Country", "STATE": "State", "COUNTY": "County"}[region_code]
        payload = {
            "resourceType": "portfolio",
            "resourceUri": portfolio_uri,
            "settings": {
                "portfolioProperties": {},
                "currency": {"currency": currency, "currencySchemeName": "RMS Default",
                             "currencyVersion": CURRENCY_VINTAGE, "asOfDate": CURRENCY_AS_OF_DATE},
                "profileIds": [profile_id],
                "financialPerspectives": [perspective],
                "eventInfo": {"eventDateBehavior": "ignore", "eventDate": time.strftime("%Y-%m-%d")},
                "name": f"{portfolio_name}_{region_code}_{peril_code}",
                "message": f"{region_name} accumulation for {peril_code}",
            },
        }
        response = self.session.post(
            f"{RMS_BASE_URL}/accumulation/v1/jobs", json=payload,
            headers=self._rg_headers(**{"content-type": "application/json"}), timeout=30,
        )
        check_response(response)
        return self._job_url_from(response, f"{region_name} {peril_code} accumulation")

    def poll_accumulation_jobs(self, job_urls: dict, level: str, timeout_seconds: int = 3600) -> dict:
        pending, completed, failures, last_status = dict(job_urls), {}, {}, {}
        started = time.monotonic()
        while pending and time.monotonic() - started < timeout_seconds:
            for peril, job_url in list(pending.items()):
                try:
                    response = self.session.get(job_url, timeout=30)
                    check_response(response)
                    job = response.json()
                except Exception as error:
                    failures[peril] = f"{type(error).__name__}: {error}"
                    pending.pop(peril)
                    continue
                status = str(job.get("status", "")).upper()
                if status != last_status.get(peril):
                    self.log(f"{level} accumulation {peril}: {status or 'unknown'}")
                    last_status[peril] = status
                if status == "FINISHED":
                    completed[peril] = {"job_url": job_url, "job_result": job}
                    pending.pop(peril)
                elif status in FAILED_JOB_STATUSES:
                    failures[peril] = str(job)
                    pending.pop(peril)
            if pending:
                time.sleep(5)
        failures.update({p: "TIMEOUT" for p in pending})
        if failures:
            raise RuntimeError(f"{level} peril-level accumulation failures: {failures}; completed: {completed}")
        return completed

    def run_accumulation(self, level: str, portfolio_uri: str, portfolio_name: str,
                         currency: str, perspective: str) -> dict:
        region_code, profiles = ACCUMULATION_PROFILE_SETS[level]
        job_urls, submission_error = {}, None
        for profile_id, peril in profiles:
            try:
                job_urls[peril] = self.submit_peril_accumulation(
                    portfolio_uri, portfolio_name, region_code, currency, perspective, profile_id, peril)
                self.log(f"Submitted {level.lower()} accumulation: {peril}")
            except Exception as error:
                submission_error = f"{peril}: {type(error).__name__}: {error}"
                break
        results = self.poll_accumulation_jobs(job_urls, level) if job_urls else {}
        if submission_error:
            raise RuntimeError(
                f"Could not submit all {level.lower()} jobs ({submission_error}); submitted jobs: {results}")
        self.log("To download accumulation results, use the RMS Export API with exportType='RESULTS'.")
        return results
