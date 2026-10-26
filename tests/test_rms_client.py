import base64
import json

import pytest

import rms_client as rc


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}
        self.url = "https://example.test"
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes requests by (method, url-suffix) to canned responses and records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _handle(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), value in self.routes.items():
            if m == method and url.endswith(suffix):
                if isinstance(value, list):  # successive responses; last one repeats
                    return value.pop(0) if len(value) > 1 else value[0]
                return value
        raise AssertionError(f"Unexpected {method} {url}")

    def get(self, url, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, **kw):
        return self._handle("POST", url, **kw)


RG_ROUTE = ("GET", "/entitlements/RI-RISKMODELER/resourcegroups")


def client_with(routes):
    routes = {RG_ROUTE: FakeResponse([{"resourceGroupId": "rg-1", "default": True}]), **routes}
    return rc.RmsClient("key", session=FakeSession(routes))


# ---- pure helpers -------------------------------------------------------------------
def test_response_items_handles_dict_list_and_other():
    assert rc.response_items({"items": [1, 2]}) == [1, 2]
    assert rc.response_items([3]) == [3]
    assert rc.response_items(None) == []


def test_decode_upload_target_virtual_hosted_and_path_style():
    presign = {
        "accessKeyId": b64("AK"), "secretAccessKey": b64("SK"),
        "sessionToken": b64("TOK"), "region": b64("us-east-1"),
    }
    virtual = rc.decode_upload_target({
        "presignParams": presign, "fileUri": "https://x/files/abc",
        "uploadUrl": "https://my-bucket.s3.us-east-1.amazonaws.com/path/to/key.txt",
    })
    assert (virtual["bucket"], virtual["key"]) == ("my-bucket", "path/to/key.txt")
    assert virtual["access_key"] == "AK" and virtual["region"] == "us-east-1"

    path_style = rc.decode_upload_target({
        "presignParams": presign, "fileUri": "https://x/files/abc",
        "uploadUrl": "https://s3.us-east-1.amazonaws.com/my-bucket/path/key.txt",
    })
    assert (path_style["bucket"], path_style["key"]) == ("my-bucket", "path/key.txt")


def test_format_job_progress():
    percent, text = rc.format_job_progress({"progress": 50}, 65)
    assert percent == 50 and "50%" in text and "01:05" in text and text.count("#") == 10
    assert rc.format_job_progress({}, 5) == (None, "elapsed 00:05")
    assert rc.format_job_progress({"progress": 250}, 0)[0] == 100


def _analysis(model, pid, run, status, aid):
    return {"analysisName": f"{model}_{pid}_{run}", "analysisStatus": status, "analysisId": aid}


def test_select_ready_run_prefers_requested_then_latest_complete():
    models = list(rc.MODEL_CONFIG)
    analyses = (
        [_analysis(m, "9", "RunA", "ANALYZED", 10 + i) for i, m in enumerate(models)]
        + [_analysis(m, "9", "RunB", "ANALYZED", 20 + i) for i, m in enumerate(models)]
        + [_analysis(models[0], "9", "RunC", "ANALYZED", 99)]  # incomplete run
    )
    assert rc.select_ready_run(analyses, "9", "RunA")[0] == "RunA"
    assert rc.select_ready_run(analyses, "9", "missing")[0] == "RunB"  # latest by analysisId
    with pytest.raises(ValueError):
        rc.select_ready_run([_analysis(models[0], "9", "R", "RUNNING", 1)], "9", "R")


def test_write_minimal_mri_file_renames_and_defaults(tmp_path):
    src = tmp_path / "accounts.txt"
    src.write_text("ACCNTNUM\tACCNTNAME\tMINDEDAMT\nA1\tAcme\t500\n", encoding="utf-8")
    out = rc.write_minimal_mri_file(
        src, rc.MRI_ACCOUNT_COLUMNS, rc.MRI_ACCOUNT_DEFAULTS, rc.MRI_ACCOUNT_RENAMES)
    lines = out.read_text(encoding="utf-8").splitlines()
    row = dict(zip(lines[0].split("\t"), lines[1].split("\t")))
    assert row["ACCGRPID"] == "1" and row["ISVALID"] == "1"
    assert row["BLANDEDAMT"] == "500" and row["ACCNTNAME"] == "Acme"


def test_make_session_requires_key():
    with pytest.raises(RuntimeError):
        rc.make_session("")


def test_check_response_raises_with_status_and_body():
    with pytest.raises(Exception) as info:
        rc.check_response(FakeResponse({"err": 1}, status=403, text="forbidden"))
    assert "403" in str(info.value) and "forbidden" in str(info.value)


# ---- network logic with fakes ---------------------------------------------------------
def test_resource_group_prefers_default_and_is_cached():
    session = FakeSession({RG_ROUTE: FakeResponse([
        {"resourceGroupId": "a"}, {"resourceGroupId": "b", "default": True}])})
    client = rc.RmsClient("key", session=session)
    assert client.resource_group_id == "b"
    assert client.resource_group_id == "b"
    assert len(session.calls) == 1


def test_list_exposures_dedupes_and_sorts():
    client = client_with({("GET", "/riskdata/v1/exposures"): FakeResponse(
        {"items": [{"exposureName": "beta"}, {"exposureName": "Alpha"}, {"exposureName": "beta"}, {}]})})
    assert client.list_exposures() == ["Alpha", "beta"]


def test_get_or_create_portfolio_creates_when_missing():
    portfolios = "/exposures/5/portfolios"
    listing = [
        FakeResponse({"items": []}),
        FakeResponse({"items": [{"portfolioName": "New", "uri": "u/5/p/7", "portfolioId": 7}]}),
    ]
    client = client_with({("GET", portfolios): listing, ("POST", portfolios): FakeResponse({}, 201)})
    assert client.get_or_create_portfolio(5, "New", "123", True) == ("u/5/p/7", 7)
    post = [c for c in client.session.calls if c[0] == "POST"][0]
    assert post[2]["json"]["portfolioName"] == "New"
    assert post[2]["headers"]["x-rms-resource-group-id"] == "rg-1"


def test_get_or_create_portfolio_raises_when_creation_disabled():
    client = client_with({("GET", "/exposures/5/portfolios"): FakeResponse({"items": []})})
    with pytest.raises(ValueError):
        client.get_or_create_portfolio(5, "Missing", "1", False)


def test_poll_job_returns_on_finish_and_raises_on_failure(monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda _: None)
    seen = []
    client = client_with({("GET", "/jobs/1"): [
        FakeResponse({"status": "RUNNING", "progress": 10}),
        FakeResponse({"status": "FINISHED", "jobId": "1"}),
    ]})
    client.progress = seen.append
    assert client.poll_job("https://api/jobs/1")["jobId"] == "1"
    assert len(seen) == 2 and "RUNNING" in seen[0]

    failing = client_with({("GET", "/jobs/2"): FakeResponse({"status": "FAILED"})})
    with pytest.raises(RuntimeError):
        failing.poll_job("https://api/jobs/2")


def test_poll_job_times_out(monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda _: None)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(rc.time, "monotonic", lambda: next(ticks))
    client = client_with({("GET", "/jobs/3"): FakeResponse({"status": "RUNNING"})})
    with pytest.raises(TimeoutError):
        client.poll_job("https://api/jobs/3", timeout_seconds=250)


def test_group_analyses_submits_all_three_model_uris(monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda _: None)
    models = list(rc.MODEL_CONFIG)
    analyses = [
        {**_analysis(m, "9", "R1", "Analyzed", i), "uri": f"uri/{i}"} for i, m in enumerate(models)
    ]
    client = client_with({
        ("GET", "/riskdata/v1/analyses"): FakeResponse({"items": analyses}),
        ("GET", "/referencedata/v1/groupingsets"): FakeResponse(
            {"items": [{"groupingSetId": 4, "groupingSetName": "Default", "isDefault": True}]}),
        ("POST", "/grouping/v1/jobs"): FakeResponse(
            {}, 202, headers={"Location": "/platform/grouping/v1/jobs/77"}),
        ("GET", "/grouping/v1/jobs/77"): FakeResponse({"status": "FINISHED", "jobId": "77"}),
    })
    result = client.group_analyses(9, "Book", "EDM1", "R1")
    assert result["group_name"] == "Book_GROUP_R1"
    post = [c for c in client.session.calls if c[0] == "POST"][0]
    assert post[2]["json"]["resourceUris"] == ["uri/0", "uri/1", "uri/2"]
    assert post[2]["json"]["settings"]["groupingSetId"] == 4


def test_run_accumulation_submits_five_perils_and_polls(monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda _: None)
    client = client_with({
        ("POST", "/accumulation/v1/jobs"): FakeResponse(
            {}, 202, headers={"Location": "/platform/accumulation/v1/jobs/1"}),
        ("GET", "/accumulation/v1/jobs/1"): FakeResponse({"status": "FINISHED"}),
    })
    results = client.run_accumulation("State", "uri/portfolio", "Book", "USD", "GR")
    assert sorted(results) == ["CS", "EQ", "FL", "WF", "WS"]
    names = [c[2]["json"]["settings"]["name"] for c in client.session.calls if c[0] == "POST"]
    assert names[0] == "Book_STATE_EQ" and len(names) == 5
