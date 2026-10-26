"""Streamlit front end for the RMS MRI submission workflow (replaces the notebook's ipywidgets form).

Secrets are read from Streamlit secrets or environment variables, never from source:
  RMS_API_KEY    required  RMS RiskModeler API key
  APP_PASSWORD   required  password for this app (see require_access below)
"""
from __future__ import annotations

import hmac
import io
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from rms_client import ACCUMULATION_PROFILE_SETS, MODEL_CONFIG, RmsClient

st.set_page_config(page_title="RMS MRI import", page_icon=None, layout="wide")


# --------------------------------------------------------------------------------------
# Secrets and access control
# --------------------------------------------------------------------------------------
def get_secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file configured
        pass
    return os.environ.get(name, default)


API_KEY = get_secret("RMS_API_KEY")


def require_access() -> None:
    """Stop the script unless the visitor has entered APP_PASSWORD.

    This app can create portfolios and launch jobs on your RMS account, so it fails closed:
    with no APP_PASSWORD it refuses to run, unless MRI_APP_ALLOW_NO_AUTH=1 is set explicitly
    (for example when an upstream proxy or SSO already protects it).
    """
    expected = get_secret("APP_PASSWORD")
    if not expected:
        if os.environ.get("MRI_APP_ALLOW_NO_AUTH") == "1":
            return
        st.title("RMS MRI import")
        st.error(
            "This app is locked because no access password is configured. "
            "Set APP_PASSWORD as a secret or environment variable, then reload. See the README."
        )
        st.stop()
    if st.session_state.get("authed"):
        return
    st.title("RMS MRI import")
    with st.form("login"):
        password = st.text_input("Access password", type="password")
        submitted = st.form_submit_button("Unlock")
    if submitted:
        if hmac.compare_digest(password.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("That password is not correct.")
    st.stop()


# --------------------------------------------------------------------------------------
# State and small helpers
# --------------------------------------------------------------------------------------
def init_state() -> None:
    now = datetime.now()
    defaults = {
        "edms": [],
        "portfolios": [],
        "rdm_servers": {},
        "rdm_databases": {},
        "history": {},
        "run_id": f"Run_{now:%Y%m%d_%H%M%S}",
        "new_portfolio_name": f"Portfolio_{now:%Y%m%d_%H%M%S}",
        "new_portfolio_number": f"{now:%Y%m%d%H%M%S}",
        "acc_currency": "USD",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    # An action (such as an import) can ask for a portfolio to be selected on the next run.
    # It has to be applied before the sidebar widget is created.
    pending = st.session_state.pop("_pending_portfolio", None)
    if pending:
        st.session_state["portfolio_id"] = pending


def keep_valid(key: str, options) -> None:
    """Drop a stored selection that is no longer one of the options."""
    if st.session_state.get(key) not in options:
        st.session_state.pop(key, None)


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", Path(name).name) or "upload.txt"


def fetch(label: str, state_key: str, fn) -> bool:
    """Run a quick lookup and store the result in session state. Returns True on success."""
    with st.spinner(label):
        try:
            st.session_state[state_key] = fn(RmsClient(API_KEY))
            return True
        except Exception as error:
            st.error(f"{label} failed: {type(error).__name__}: {error}")
            return False


def clear_confirmation(key: str) -> None:
    """Button callback: every confirmation covers one action, so uncheck it when the action starts."""
    st.session_state[key] = False


def run_action(key: str, title: str, fn):
    """Run a long action with live progress, keep its log for later reruns, return the result.

    fn receives an RmsClient wired to the live log and progress display. Returns None on failure.
    """
    lines: list[str] = []
    last_progress = {"text": ""}
    with st.status(title, expanded=True) as box:
        progress_box = st.empty()
        log_box = st.empty()

        def log(message: str) -> None:
            lines.append(str(message))
            log_box.code("\n".join(lines), language=None)

        def progress(text: str) -> None:
            last_progress["text"] = text
            progress_box.code(text, language=None)

        try:
            result = fn(RmsClient(API_KEY, log=log, progress=progress))
        except Exception as error:
            log(f"{type(error).__name__}: {error}")
            box.update(label=f"{title}: failed", state="error", expanded=True)
            ok, result = False, None
        else:
            box.update(label=f"{title}: finished", state="complete", expanded=True)
            ok = True
    final = lines + ([last_progress["text"]] if last_progress["text"] else [])
    st.session_state["history"][key] = {
        "ok": ok,
        "label": f"Last run: {title.lower()} {'finished' if ok else 'failed'}",
        "lines": final,
    }
    return result


def show_last(key: str) -> None:
    entry = st.session_state["history"].get(key)
    if entry:
        with st.expander(entry["label"], expanded=not entry["ok"]):
            st.code("\n".join(entry["lines"]), language=None)


def preview(upload) -> None:
    try:
        frame = pd.read_csv(io.BytesIO(upload.getvalue()), sep="\t", nrows=5, dtype=str,
                            encoding="utf-8-sig")
        st.caption(f"{upload.name}: {len(frame.columns)} columns, first {len(frame)} rows")
        st.dataframe(frame, hide_index=True)
    except Exception as error:
        st.warning(f"Could not preview {upload.name}: {error}")


# --------------------------------------------------------------------------------------
# Sidebar: the target shared by every step
# --------------------------------------------------------------------------------------
@dataclass
class Target:
    edm: str | None
    run_id: str
    portfolio: dict | None


def sidebar_target() -> Target:
    with st.sidebar:
        st.header("Target")

        edms = st.session_state["edms"]
        if st.button("Load EDMs") and fetch("Loading EDMs", "edms", lambda c: c.list_exposures()):
            st.rerun()
        keep_valid("edm", edms)
        edm = st.selectbox("EDM", edms, key="edm", disabled=not edms,
                           placeholder="Load EDMs first")

        # Portfolios belong to one EDM, so forget them when the EDM changes.
        if st.session_state.get("_portfolio_edm") != edm:
            st.session_state["portfolios"] = []
            st.session_state.pop("portfolio_id", None)
            st.session_state["_portfolio_edm"] = edm

        st.text_input("Run ID", key="run_id",
                      help="Names the model analyses. Grouping and export find them by this ID.")

        by_id = {p["id"]: p for p in st.session_state["portfolios"]}
        if st.button("Load portfolios", disabled=not edm) and fetch(
                "Loading portfolios", "portfolios", lambda c: c.list_portfolios(edm)):
            st.rerun()
        keep_valid("portfolio_id", list(by_id))
        portfolio_id = st.selectbox(
            "Existing portfolio", list(by_id), key="portfolio_id", disabled=not by_id,
            format_func=lambda i: f"{by_id[i]['name']} (ID {i})", placeholder="Load portfolios first",
        )
        st.caption(
            "Long jobs run inside this page, so keep it open until a run finishes. "
            "Jobs already submitted keep running in RMS if you close it."
        )
    return Target(edm=edm, run_id=st.session_state["run_id"].strip(),
                  portfolio=by_id.get(portfolio_id))


def blocker(target: Target, need_portfolio: bool = True, need_run_id: bool = True) -> str | None:
    if not target.edm:
        return "Load EDMs in the sidebar and choose one."
    if need_portfolio and not target.portfolio:
        return "Choose an existing portfolio in the sidebar."
    if need_run_id and not target.run_id:
        return "Enter a Run ID in the sidebar."
    return None


# --------------------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------------------
def tab_import(target: Target) -> None:
    st.write("Upload an account file and a location file (tab-delimited .txt). "
             "RMS imports them, then geocodes the portfolio.")
    mode = st.radio("Portfolio", ["Create new", "Use existing"], key="import_mode", horizontal=True)
    creating = mode == "Create new"
    if creating:
        c1, c2 = st.columns(2)
        c1.text_input("Portfolio name", key="new_portfolio_name")
        c2.text_input("Portfolio number", key="new_portfolio_number")
    else:
        if target.portfolio:
            st.write(f"Importing into **{target.portfolio['name']}**.")
        st.warning("This adds accounts and locations to a portfolio that may already have data.")

    c1, c2 = st.columns(2)
    accounts = c1.file_uploader("Account file", type=["txt"], key="accounts_file")
    locations = c2.file_uploader("Location file", type=["txt"], key="locations_file")
    if accounts and locations:
        with st.expander("Preview first rows"):
            preview(accounts)
            preview(locations)

    confirm = st.checkbox(
        "I confirm the import into the selected RMS portfolio" if creating
        else "I confirm the import into the existing portfolio and understand it appends to it",
        key="confirm_import",
    )

    reason = blocker(target, need_portfolio=not creating, need_run_id=False)
    if creating and not (st.session_state["new_portfolio_name"].strip()
                         and st.session_state["new_portfolio_number"].strip()):
        reason = "Enter a portfolio name and number."
    if not (accounts and locations):
        reason = reason or "Choose both files."
    if reason:
        st.info(reason)

    clicked = st.button("Import and geocode", type="primary", disabled=bool(reason) or not confirm,
                        on_click=clear_confirmation, args=("confirm_import",))
    if not clicked:
        show_last("import")
        return

    name = st.session_state["new_portfolio_name"].strip() if creating else target.portfolio["name"]
    number = st.session_state["new_portfolio_number"].strip() if creating else ""
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for label, upload in (("accounts", accounts), ("locations", locations)):
            folder = Path(tmp) / label
            folder.mkdir()
            paths[label] = folder / safe_filename(upload.name)
            paths[label].write_bytes(upload.getvalue())
        result = run_action(
            "import", "Importing MRI files",
            lambda c: c.import_mri(paths["accounts"], paths["locations"], target.edm, name,
                                   number, create_portfolio=creating),
        )
    if result:
        # Refresh the sidebar list so the portfolio can be selected for the next steps.
        try:
            st.session_state["portfolios"] = RmsClient(API_KEY).list_portfolios(target.edm)
            st.session_state["_pending_portfolio"] = str(result["portfolio_id"])
        except Exception:
            pass
        st.rerun()


def tab_models(target: Target) -> None:
    st.write(f"Runs {len(MODEL_CONFIG)} catastrophe models on the selected portfolio: "
             + ", ".join(MODEL_CONFIG) + ".")
    confirm = st.checkbox(f"I confirm running all {len(MODEL_CONFIG)} models on this portfolio",
                          key="confirm_models")
    reason = blocker(target)
    if reason:
        st.info(reason)
    if not st.button("Run models", type="primary", disabled=bool(reason) or not confirm,
                     on_click=clear_confirmation, args=("confirm_models",)):
        show_last("models")
        return

    def work(client: RmsClient):
        results = client.run_models(int(target.portfolio["id"]), target.edm, target.run_id)
        for model, info in results.items():
            client.log(f"{model}: {info['status']} (analysis ID {info['analysis_id']})")
        return results

    run_action("models", "Running catastrophe models", work)


def tab_group(target: Target) -> None:
    st.write("Combines the model analyses for the selected Run ID into one analysis group. "
             "Every model must have status Analyzed. If the Run ID has no complete set, "
             "the latest complete run is used.")
    confirm = st.checkbox("I confirm grouping the model analyses of this Run ID", key="confirm_group")
    reason = blocker(target)
    if reason:
        st.info(reason)
    if not st.button("Group analyses", type="primary", disabled=bool(reason) or not confirm,
                     on_click=clear_confirmation, args=("confirm_group",)):
        show_last("group")
        return
    run_action("group", "Grouping analyses", lambda c: c.group_analyses(
        int(target.portfolio["id"]), target.portfolio["name"], target.edm, target.run_id))


def tab_export(target: Target) -> None:
    st.write("Exports the model analyses and the analysis group of the selected Run ID to an RDM "
             "on a Data Bridge server. Accumulation results can't be exported this way; the "
             "Export API only supports flat-file results for them.")
    if st.button("Load Data Bridge servers"):
        try:
            servers, databases = RmsClient(API_KEY).list_rdm_targets()
            st.session_state["rdm_servers"], st.session_state["rdm_databases"] = servers, databases
        except Exception as error:
            st.error(f"Could not load Data Bridge servers: {type(error).__name__}: {error}")

    servers, databases = st.session_state["rdm_servers"], st.session_state["rdm_databases"]
    keep_valid("rdm_server", list(servers))
    server_id = st.selectbox("Data Bridge server", list(servers), key="rdm_server",
                             disabled=not servers, placeholder="Load servers first",
                             format_func=lambda i: f"{servers[i]} (ID {i})")
    options = {
        did: db for did, db in databases.items()
        if server_id is not None and str(db.get("serverName", "")) == servers.get(server_id)
    }
    keep_valid("rdm_database", list(options))
    database_id = st.selectbox("RDM database", list(options), key="rdm_database",
                               disabled=not options,
                               format_func=lambda i: f"{options[i].get('databaseName')} (ID {i})")

    confirm = st.checkbox("I confirm exporting the analyses of this Run ID to the selected RDM",
                          key="confirm_export")
    reason = blocker(target)
    if not reason and not (server_id and database_id):
        reason = "Choose a Data Bridge server and an RDM database."
    if reason:
        st.info(reason)
    if not st.button("Export to RDM", type="primary", disabled=bool(reason) or not confirm,
                     on_click=clear_confirmation, args=("confirm_export",)):
        show_last("export")
        return
    run_action("export", "Exporting to RDM", lambda c: c.export_to_rdm(
        int(target.portfolio["id"]), target.portfolio["name"], target.edm, target.run_id,
        server_id, database_id, str(options[database_id].get("databaseName", ""))))


def tab_accumulation(target: Target) -> None:
    st.write("Runs one accumulation job per peril (EQ, CS, FL, WS, WF) for the chosen area level "
             "on the selected portfolio.")
    c1, c2 = st.columns(2)
    c1.text_input("Currency", key="acc_currency")
    perspective = c2.selectbox(
        "Perspective", ["GR", "GU"], key="acc_perspective",
        format_func={"GR": "Gross loss (GR)", "GU": "Ground-up loss (GU)"}.get)
    confirm = st.checkbox("I confirm running five accumulation jobs for the selected area level",
                          key="confirm_accumulation")
    currency = st.session_state["acc_currency"].strip().upper()
    reason = blocker(target, need_run_id=False) or (None if currency else "Enter a currency code, such as USD.")
    if reason:
        st.info(reason)

    columns = st.columns(len(ACCUMULATION_PROFILE_SETS))
    chosen = None
    for column, level in zip(columns, ACCUMULATION_PROFILE_SETS):
        if column.button(f"Run {level.lower()} accumulation", disabled=bool(reason) or not confirm,
                         key=f"acc_{level}", on_click=clear_confirmation,
                         args=("confirm_accumulation",)):
            chosen = level
    if chosen is None:
        show_last("accumulation")
        return
    run_action("accumulation", f"Running {chosen.lower()} accumulation",
               lambda c: c.run_accumulation(chosen, target.portfolio["uri"],
                                            target.portfolio["name"], currency, perspective))


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------
require_access()
init_state()

st.title("RMS MRI import")
st.caption("Send account and location files to RMS RiskModeler, then run models, group, and export.")

if not API_KEY:
    st.error("RMS_API_KEY is not set. Add it as a secret or environment variable, then reload. "
             "See the README.")
    st.stop()

target = sidebar_target()
tabs = st.tabs(["Import MRI", "Run models", "Group", "Export to RDM", "Accumulation"])
for tab, render in zip(tabs, (tab_import, tab_models, tab_group, tab_export, tab_accumulation)):
    with tab:
        render(target)
