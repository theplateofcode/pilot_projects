"""Dataverse Web API client: authentication, retries, metadata helpers.

Two sign-in modes:

* ``device`` (default) — interactive device-code flow using the well-known
  Power Platform public client id. Needs no app registration: you sign in as
  yourself and get your own privileges. The token is cached in
  ``~/.pilot-crm/msal_cache.json``.
* ``secret`` — client credentials (service principal). Requires an Entra app
  registration whose application user exists in the environment with the
  System Customizer / System Administrator role.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import time
from typing import Any, Iterable
from urllib.parse import urlparse

# Public client id shipped with the Power Platform tooling (pac, XrmToolBox).
PUBLIC_CLIENT_ID = "51f81489-12ee-4a9e-aaae-a2591f45987d"
REDIRECT_URI = "http://localhost"
API_VERSION = "v9.2"
CACHE_DIR = pathlib.Path(os.environ.get("PILOT_CRM_HOME", pathlib.Path.home() / ".pilot-crm"))


class DataverseError(RuntimeError):
    """A Dataverse Web API call failed."""

    def __init__(self, message: str, status: int | None = None, body: Any = None, request: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.request = request

    @property
    def code(self) -> str:
        body = self.body
        if isinstance(body, dict):
            return str(body.get("error", {}).get("code", ""))
        return ""

    @property
    def is_duplicate(self) -> bool:
        """True when the failure means "this already exists"."""
        text = str(self)
        needles = (
            "duplicate",
            "already exists",
            "already in use",
            "0x80040237",  # DuplicateRecord
            "0x8004f017",  # entity key already exists
            "0x80071034",  # object already exists in solution / metadata name in use
            "0x80060892",
        )
        low = text.lower()
        return any(n in low for n in needles)


def normalise_org_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


class Dataverse:
    """Thin, retrying Web API wrapper."""

    def __init__(
        self,
        org_url: str,
        auth: str = "device",
        tenant: str = "common",
        client_id: str | None = None,
        client_secret: str | None = None,
        dry_run: bool = False,
        verbose: bool = False,
        log=None,
        device_code_rounds: int = 4,
    ):
        self.org_url = normalise_org_url(org_url)
        self.base = f"{self.org_url}/api/data/{API_VERSION}"
        self.auth_mode = auth
        self.tenant = tenant
        self.client_id = client_id or PUBLIC_CLIENT_ID
        self.client_secret = client_secret
        self.device_code_rounds = max(1, device_code_rounds)
        self.dry_run = dry_run
        self.verbose = verbose
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self._token: str | None = None
        self._token_expiry = 0.0
        self._session = None
        self.calls = 0
        self.recorded: list[dict] = []

    @property
    def session(self):
        """The HTTP session. ``requests`` is imported lazily so planning works without it."""
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    # ------------------------------------------------------------------ auth
    def _acquire_token(self) -> str:
        import msal  # imported lazily so --dry-run works without msal

        scope = [f"{self.org_url}/.default"]
        authority = f"https://login.microsoftonline.com/{self.tenant}"

        if self.auth_mode == "secret":
            if not self.client_secret:
                raise DataverseError("auth=secret needs --client-secret (or DATAVERSE_CLIENT_SECRET)")
            app = msal.ConfidentialClientApplication(
                self.client_id, authority=authority, client_credential=self.client_secret
            )
            result = app.acquire_token_for_client(scopes=scope)
        else:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path = CACHE_DIR / "msal_cache.json"
            cache = msal.SerializableTokenCache()
            if cache_path.exists():
                cache.deserialize(cache_path.read_text())
            app = msal.PublicClientApplication(
                self.client_id, authority=authority, token_cache=cache, client_credential=None
            )
            result = None
            for account in app.get_accounts():
                result = app.acquire_token_silent(scope, account=account)
                if result and "access_token" in result:
                    break
            if not result or "access_token" not in result:
                # Each device code lives about 15 minutes. A deployment often starts while
                # nobody is at a browser, so issue a fresh code rather than giving up.
                for round_number in range(1, self.device_code_rounds + 1):
                    flow = app.initiate_device_flow(scopes=scope)
                    if "user_code" not in flow:
                        raise DataverseError(f"could not start device-code sign-in: {flow}")
                    suffix = (
                        ""
                        if round_number == 1
                        else f" (code {round_number} of {self.device_code_rounds} — the previous one expired)"
                    )
                    self._log("\n" + flow["message"] + suffix + "\n")
                    result = app.acquire_token_by_device_flow(flow)
                    if result and "access_token" in result:
                        break
            if cache.has_state_changed:
                cache_path.write_text(cache.serialize())
                os.chmod(cache_path, 0o600)

        if not result or "access_token" not in result:
            raise DataverseError(f"sign-in failed: {(result or {}).get('error_description', result)}")
        self._token_expiry = time.time() + float(result.get("expires_in", 3000)) - 120
        return result["access_token"]

    def token(self) -> str:
        if not self._token or time.time() > self._token_expiry:
            self._token = self._acquire_token()
        return self._token

    def whoami(self) -> dict:
        return self.get("WhoAmI")

    # ------------------------------------------------------------- transport
    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Authorization": f"Bearer {self.token()}",
        }
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict | None = None,
        tolerate_duplicate: bool = False,
        mutating: bool | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        if mutating is None:
            mutating = method.upper() in {"POST", "PATCH", "PUT", "DELETE"}

        if self.dry_run and mutating:
            self.recorded.append({"method": method.upper(), "url": url, "headers": headers or {}, "body": body})
            if self.verbose:
                self._log(f"  [dry-run] {method.upper()} {url}")
            return {"dry_run": True}

        import requests

        last: Exception | None = None
        session = self.session
        for attempt in range(6):
            try:
                self.calls += 1
                response = session.request(
                    method.upper(),
                    url,
                    headers=self._headers(headers),
                    data=json.dumps(body).encode("utf-8") if body is not None else None,
                    timeout=180,
                )
            except requests.RequestException as exc:  # network blip
                last = exc
                time.sleep(min(2 ** attempt, 30) + random.random())
                continue

            if response.status_code in (429, 503, 504):
                wait = float(response.headers.get("Retry-After", min(2 ** attempt, 60)))
                self._log(f"  throttled ({response.status_code}), waiting {wait:.0f}s")
                time.sleep(wait)
                continue

            if response.status_code >= 400:
                try:
                    payload = response.json()
                    message = payload.get("error", {}).get("message", response.text)
                except ValueError:
                    payload, message = response.text, response.text
                error = DataverseError(
                    f"{method.upper()} {url} -> {response.status_code}: {message}",
                    response.status_code,
                    payload,
                    body,
                )
                if tolerate_duplicate and error.is_duplicate:
                    return {"duplicate": True}
                raise error

            if response.status_code == 204 or not response.content:
                created = response.headers.get("OData-EntityId", "")
                return {"id": created.rsplit("(", 1)[-1].rstrip(")")} if created else {}
            return response.json()

        raise DataverseError(f"{method.upper()} {url} failed after retries: {last}")

    def get(self, path: str, headers: dict | None = None) -> Any:
        return self.request("GET", path, headers=headers, mutating=False)

    def post(self, path: str, body: Any, headers: dict | None = None, tolerate_duplicate: bool = False) -> Any:
        return self.request("POST", path, body, headers, tolerate_duplicate)

    def patch(self, path: str, body: Any, headers: dict | None = None) -> Any:
        return self.request("PATCH", path, body, headers)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def create(self, entity_set: str, body: dict, solution: str | None = None) -> str | None:
        """Create a record and return its id (None in dry-run)."""
        headers = {"MSCRM.SolutionUniqueName": solution} if solution else None
        result = self.post(entity_set, body, headers)
        if isinstance(result, dict):
            return result.get("id")
        return None

    def query(self, path: str) -> list[dict]:
        """GET an OData collection, following @odata.nextLink."""
        rows: list[dict] = []
        next_path: str | None = path
        while next_path:
            page = self.get(next_path)
            rows.extend(page.get("value", []))
            next_path = page.get("@odata.nextLink")
        return rows

    def first(self, path: str) -> dict | None:
        rows = self.query(path)
        return rows[0] if rows else None

    def action(self, name: str, body: dict | None = None, bound_to: str | None = None) -> Any:
        path = f"{bound_to}/Microsoft.Dynamics.CRM.{name}" if bound_to else name
        return self.post(path, body or {})

    # -------------------------------------------------------------- metadata
    def entity_metadata(self, logical_name: str, select: Iterable[str] = ()) -> dict | None:
        fields = ",".join(select) or "LogicalName,ObjectTypeCode,EntitySetName,PrimaryIdAttribute,PrimaryNameAttribute"
        try:
            return self.get(f"EntityDefinitions(LogicalName='{logical_name}')?$select={fields}")
        except DataverseError as exc:
            if exc.status == 404:
                return None
            raise

    def publish_xml(self, parameter_xml: str) -> Any:
        return self.post("PublishXml", {"ParameterXml": parameter_xml})

    def publish_all(self) -> Any:
        return self.post("PublishAllXml", {})
