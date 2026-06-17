"""Client for Hero and the Hero-linked Google Calendar feed."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime, timedelta
import hashlib
import json
from html import unescape
from html.parser import HTMLParser
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from aiohttp import ClientError, CookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    API_BASE,
    APP_ORIGIN,
    APP_REFERER,
    AUTH_BASE,
    CLIENT_ID,
    CONF_TENANT_ID,
    CONF_TIMEZONE,
    DEFAULT_TENANT_ID,
    DEFAULT_TIMEZONE,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SKEW = timedelta(seconds=60)
TOKEN_URL = f"{AUTH_BASE}/oauth2/token"


class HeroApiError(Exception):
    """Raised when Hero or Google calendar requests fail."""


def _code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
    except (IndexError, ValueError) as err:
        raise HeroApiError("Hero returned an invalid access token") from err

    import json

    return json.loads(decoded)


class HeroClient:
    """Async client for Hero."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._google_api_key: str | None = None
        self.session = async_create_clientsession(
            hass,
            cookie_jar=CookieJar(unsafe=True),
        )

    @property
    def _data(self) -> Mapping[str, Any]:
        return self.entry.data

    @property
    def timezone(self) -> str:
        return self._data.get(CONF_TIMEZONE, DEFAULT_TIMEZONE)

    async def authenticate(self, username: str, password: str, tenant_id: str, timezone: str) -> dict[str, Any]:
        """Perform Hero's browser OAuth flow and return token data."""
        _LOGGER.debug("Starting Hero OAuth login for %s using tenant %s and timezone %s", username, tenant_id, timezone)
        # Hero creates the identity PKCE verifier and state server-side and binds
        # them to hero.sid. Start at the entry point, just like the browser.
        response = await self._request(
            "GET",
            f"{AUTH_BASE}/oauth2/authorize",
            headers=self._auth_browser_headers(),
        )
        login_form = await self._form_from_response(response)
        if login_form is None or not login_form.inputs.get("code_challenge"):
            raise HeroApiError("Hero did not return a login form")
        login_params = dict(login_form.inputs)
        login_action = urljoin(str(response.url), login_form.action or "/oauth2/authorize")
        common_form = {
            **login_params,
            "tenantId": tenant_id,
            "timezone": timezone,
            "metaData.device.name": "iPhone/iPod Safari",
            "metaData.device.type": "BROWSER",
            "captcha_token": "",
            "nonce": "",
            "oauth_context": "",
            "pendingIdPLinkId": "",
            "response_mode": "",
            "user_code": "",
            "userVerifyingPlatformAuthenticatorAvailable": "false",
            "loginId": username,
            "rememberDevice": "true",
        }
        _LOGGER.debug("Hero OAuth step 2: submitting login identifier")
        response = await self._request(
            "POST",
            login_action,
            data={**common_form, "showPasswordField": "false"},
            headers=self._auth_browser_headers(),
            allow_redirects=False,
        )
        password_form = await self._form_from_response(response)
        password_form_data = {**common_form, "showPasswordField": "true"}
        if password_form:
            _LOGGER.debug("Hero OAuth step 2 returned password form; replaying returned hidden fields")
            password_form_data = dict(password_form.inputs)
            password_form_data["loginId"] = password_form_data.get("loginId") or username
            password_form_data["showPasswordField"] = "true"
            password_action = urljoin(str(response.url), password_form.action or "/oauth2/authorize")
            password_method = password_form.method
        else:
            password_action = f"{AUTH_BASE}/oauth2/authorize"
            password_method = "POST"

        _LOGGER.debug("Hero OAuth step 3: submitting password")
        response = await self._request(
            password_method,
            password_action,
            data={**password_form_data, "password": password},
            headers=self._auth_browser_headers(),
            allow_redirects=False,
        )

        location = response.headers.get("Location")
        if not location:
            text = await _response_snippet(response)
            _LOGGER.debug("Hero password step returned no redirect. Status=%s body=%s", response.status, _safe_snippet(text))
            raise HeroApiError("Hero login did not return an authorization redirect")

        _LOGGER.debug("Hero OAuth step 4: completing identity login and Hero session cookies")
        await self._complete_identity_login(location, username, password)

        app_verifier = _code_verifier()
        app_params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": f"{APP_ORIGIN}/callback",
            "scope": "urn:linced:organisation:role:caregiver offline_access",
            "code_challenge_method": "S256",
            "code_challenge": _code_challenge(app_verifier),
            "state": base64.urlsafe_b64encode(b'{"returnUrl":"/"}').decode().rstrip("="),
        }
        _LOGGER.debug("Hero OAuth step 5: requesting app authorization code")
        response = await self._request(
            "GET",
            f"{AUTH_BASE}/oauth2/authorize",
            params=app_params,
            headers=self._app_authorize_headers(),
            allow_redirects=False,
        )
        app_location = response.headers.get("Location")
        if not app_location:
            text = await _response_snippet(response)
            _LOGGER.debug(
                "Hero app authorize step returned no redirect. Status=%s body=%s",
                response.status,
                _safe_snippet(text),
            )
            raise HeroApiError("Hero app authorization did not return a redirect")

        _LOGGER.debug("Hero OAuth step 6: following app authorization redirects")
        code = await self._follow_to_code(app_location, headers=self._app_authorize_headers())
        _LOGGER.debug("Hero OAuth step 7: exchanging authorization code for tokens")
        token = await self._token_request(
            {
                "grant_type": "authorization_code",
                "redirect_uri": f"{APP_ORIGIN}/callback",
                "code_verifier": app_verifier,
                "code": code,
                "client_id": CLIENT_ID,
            }
        )
        token = self._with_expiry(token)
        _LOGGER.debug("Hero OAuth login succeeded; access token expires in %s seconds", token.get("expires_in"))
        return token

    async def ensure_token(self) -> str:
        """Return a fresh access token."""
        token = self._data.get("access_token")
        expires_at = self._data.get("expires_at", 0)
        if token and datetime.now().timestamp() < expires_at - TOKEN_SKEW.total_seconds():
            _LOGGER.debug("Using cached Hero access token")
            return token

        refresh_token = self._data.get("refresh_token")
        if not refresh_token:
            _LOGGER.debug("No Hero refresh token is stored; logging in again")
            username = self._data[CONF_USERNAME]
            password = self._data[CONF_PASSWORD]
            tenant_id = self._data.get(CONF_TENANT_ID, DEFAULT_TENANT_ID)
            token_data = await self.authenticate(username, password, tenant_id, self.timezone)
        else:
            _LOGGER.debug("Refreshing Hero access token")
            token_data = await self._refresh(refresh_token)

        await self._update_entry_tokens(token_data)
        return token_data["access_token"]

    async def _refresh(self, refresh_token: str) -> dict[str, Any]:
        token = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            }
        )
        if "refresh_token" not in token:
            token["refresh_token"] = refresh_token
        return self._with_expiry(token)

    async def validate_login(self, username: str, password: str, tenant_id: str, timezone: str) -> dict[str, Any]:
        """Validate credentials and return config-entry data."""
        token = await self.authenticate(username, password, tenant_id, timezone)
        payload = _decode_jwt_payload(token["access_token"])
        user_id = payload.get("uid") or payload.get("sub") or username
        school_ids = payload.get("tid") or []
        if isinstance(school_ids, str):
            school_ids = [school_ids]
        _LOGGER.debug("Hero login validation succeeded for user_id=%s school_ids=%s", user_id, school_ids)

        return {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_TENANT_ID: tenant_id,
            CONF_TIMEZONE: timezone,
            "user_id": user_id,
            "school_ids": school_ids,
            **token,
        }

    async def async_get_school_calendar_id(self) -> tuple[str, str]:
        """Return the Hero school name and configured Google Calendar ID."""
        token = await self.ensure_token()
        school_ids = self._data.get("school_ids") or []
        if not school_ids:
            payload = _decode_jwt_payload(token)
            school_ids = payload.get("tid") or []

        for school_id in school_ids:
            _LOGGER.debug("Fetching Hero school options for school_id=%s", school_id)
            school = await self._hero_get(f"/schools/v4/schools/{school_id}", token)
            school_data = school.get("school", school)
            options = school_data.get("options", {})
            calendar_id = _option_value(options.get("app:links:googleCalendar")) or _option_value(
                options.get("app:features:googlecalendar")
            )
            if calendar_id:
                _LOGGER.debug("Found Hero Google Calendar ID for school_id=%s", school_id)
                return school_data.get("name", "Hero"), calendar_id

        _LOGGER.debug("No Hero Google Calendar ID found in school options for school_ids=%s", school_ids)
        raise HeroApiError("No Hero Google Calendar option was found for this account")

    async def async_get_google_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Fetch Google Calendar events using Hero's referrer-restricted API key."""
        api_key = await self._get_google_api_key()
        url = f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
        _LOGGER.debug("Fetching Hero Google Calendar events from %s to %s", start.isoformat(), end.isoformat())
        payload = await self._json_request(
            "GET",
            url,
            params={
                "key": api_key,
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "maxResults": "9999",
                "orderBy": "startTime",
            },
            headers={
                "Accept": "*/*",
                "Origin": APP_ORIGIN,
                "Referer": APP_REFERER,
                "User-Agent": "Mozilla/5.0 AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            },
        )
        events = [item for item in payload.get("items", []) if item.get("status") != "cancelled"]
        _LOGGER.debug("Fetched %s Hero Google Calendar events", len(events))
        return events

    async def _get_google_api_key(self) -> str:
        """Load the calendar key from Hero's runtime app configuration."""
        if self._google_api_key:
            return self._google_api_key
        response = await self._request(
            "GET", APP_REFERER, headers=self._app_authorize_headers()
        )
        parser = _AppConfigParser()
        parser.feed(await _response_text(response))
        try:
            config = json.loads(unquote(parser.content or ""))
        except (ValueError, TypeError):
            raise HeroApiError("Hero did not return valid app configuration") from None
        key = config.get("googleApiKey") if isinstance(config, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise HeroApiError("Hero app configuration is missing the Google API key")
        self._google_api_key = key
        return key

    async def _hero_get(self, path: str, token: str) -> dict[str, Any]:
        return await self._json_request(
            "GET",
            f"{API_BASE}{path}",
            headers={
                "Accept": "*/*",
                "Authorization": f"Bearer {token}",
                "Origin": APP_ORIGIN,
                "Referer": APP_REFERER,
            },
        )

    async def _complete_identity_login(self, location: str, username: str, password: str) -> None:
        """Follow the first Hero identity-login redirects until the web app session is set."""
        next_url = urljoin(AUTH_BASE, location)
        for redirect_count in range(8):
            parsed = urlparse(next_url)
            if parsed.netloc and parsed.netloc != urlparse(AUTH_BASE).netloc:
                _LOGGER.debug("Hero identity login completed at %s", _redact_url(next_url))
                return

            _LOGGER.debug("Following Hero identity redirect %s to %s", redirect_count + 1, _redact_url(next_url))
            response = await self._request("GET", next_url, headers=self._auth_browser_headers(), allow_redirects=False)
            # The callback sets hero.fa.at before its JavaScript navigates to
            # /verify. That continuation must run before app OAuth will work.
            next_location = response.headers.get("Location")
            if not next_location:
                text = await _response_text(response)
                form = _parse_first_form(text)
                if form and form.action:
                    _LOGGER.debug("Hero identity returned another sign-in form; submitting returned form fields")
                    form_data = dict(form.inputs)
                    form_data["loginId"] = form_data.get("loginId") or username
                    if "password" in form.inputs or form_data.get("showPasswordField") == "true":
                        form_data["password"] = password
                        form_data["showPasswordField"] = "true"
                    else:
                        form_data["showPasswordField"] = "false"
                    response = await self._request(
                        form.method,
                        urljoin(str(response.url), form.action),
                        data=form_data,
                        headers=self._auth_browser_headers(),
                        allow_redirects=False,
                    )
                    next_location = response.headers.get("Location")
                    if next_location:
                        next_url = urljoin(str(response.url), next_location)
                        continue
                    text = await _response_text(response)

                link_location = _html_href(text)
                if link_location:
                    _LOGGER.debug(
                        "Hero identity redirect provided HTML fallback link to %s",
                        _redact_url(urljoin(next_url, link_location)),
                    )
                    next_url = urljoin(next_url, link_location)
                    continue

                _LOGGER.debug(
                    "Hero identity redirect chain stopped without Location. Url=%s status=%s body=%s",
                    _redact_url(str(response.url)),
                    response.status,
                    _safe_snippet(text),
                )
                raise HeroApiError(
                    "Hero identity login stopped before creating an app session: "
                    f"{_redact_url(str(response.url))} HTTP {response.status}: {_safe_snippet(text)}"
                )
            next_url = urljoin(next_url, next_location)

        raise HeroApiError("Hero identity login produced too many redirects")

    async def _follow_to_code(self, location: str, headers: Mapping[str, str] | None = None) -> str:
        next_url = urljoin(AUTH_BASE, location)
        for redirect_count in range(8):
            parsed = urlparse(next_url)
            query = parse_qs(parsed.query)
            if (
                parsed.scheme == "https"
                and parsed.netloc == urlparse(APP_ORIGIN).netloc
                and parsed.path == "/callback"
                and "code" in query
            ):
                _LOGGER.debug("Hero OAuth authorization code found after %s redirect(s)", redirect_count)
                return query["code"][0]
            if parsed.netloc and parsed.netloc not in {
                urlparse(AUTH_BASE).netloc,
                urlparse(APP_ORIGIN).netloc,
            }:
                raise HeroApiError(
                    "Hero authentication redirected to an unexpected host before returning an authorization code: "
                    f"{_redact_url(next_url)}"
                )
            if parsed.netloc == urlparse(APP_ORIGIN).netloc:
                raise HeroApiError(
                    "Hero authentication reached the Hero web app without an authorization code: "
                    f"{_redact_url(next_url)}"
                )

            _LOGGER.debug("Following Hero OAuth redirect %s to %s", redirect_count + 1, _redact_url(next_url))
            response = await self._request(
                "GET",
                next_url,
                headers=headers or self._auth_browser_headers(),
                allow_redirects=False,
            )
            next_location = response.headers.get("Location")
            if not next_location:
                text = await _response_text(response)
                link_location = _html_href(text)
                if link_location:
                    _LOGGER.debug(
                        "Hero OAuth redirect provided HTML fallback link to %s",
                        _redact_url(urljoin(next_url, link_location)),
                    )
                    next_url = urljoin(next_url, link_location)
                    continue

                _LOGGER.debug(
                    "Hero OAuth redirect chain stopped without Location. Url=%s status=%s body=%s",
                    _redact_url(str(response.url)),
                    response.status,
                    _safe_snippet(text),
                )
                raise HeroApiError(
                    "Hero authentication stopped before returning an authorization code: "
                    f"{_redact_url(str(response.url))} HTTP {response.status}: {_safe_snippet(text)}"
                )
            next_url = urljoin(next_url, next_location)

        raise HeroApiError("Hero authentication produced too many redirects")

    async def _token_request(self, data: Mapping[str, str]) -> dict[str, Any]:
        return await self._json_request(
            "POST",
            TOKEN_URL,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": APP_ORIGIN,
                "Referer": APP_REFERER,
            },
        )

    async def _json_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._request(method, url, **kwargs)
        try:
            return await response.json(content_type=None)
        except Exception as err:
            text = await _response_snippet(response)
            _LOGGER.exception("Unexpected non-JSON response from %s: %s", _redact_url(url), _safe_snippet(text))
            raise HeroApiError(f"Unexpected response from {_redact_url(url)}: {_safe_snippet(text)}") from err

    async def _form_from_response(self, response) -> _ParsedForm | None:
        """Parse a returned HTML form, if the response body contains one."""
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type:
            return None
        text = await _response_text(response)
        return _parse_first_form(text)

    async def _request(self, method: str, url: str, **kwargs: Any):
        _LOGGER.debug("%s %s", method, _redact_url(url))
        try:
            response = await self.session.request(method, url, **kwargs)
            _LOGGER.debug("%s %s returned HTTP %s", method, _redact_url(str(response.url)), response.status)
            if response.status >= 400:
                text = await _response_snippet(response)
                _LOGGER.warning(
                    "%s %s failed with HTTP %s: %s",
                    method,
                    _redact_url(str(response.url)),
                    response.status,
                    _safe_snippet(text),
                )
                raise HeroApiError(
                    f"{method} {_redact_url(str(response.url))} failed: HTTP {response.status}: {_safe_snippet(text)}"
                )
            return response
        except ClientError as err:
            _LOGGER.exception("%s %s failed with a client error", method, _redact_url(url))
            raise HeroApiError(f"{method} {_redact_url(url)} failed: {err}") from err

    def _auth_browser_headers(self) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": AUTH_BASE,
            "Referer": f"{AUTH_BASE}/",
            "User-Agent": "Mozilla/5.0 AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        }

    def _app_authorize_headers(self) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": APP_REFERER,
            "User-Agent": "Mozilla/5.0 AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        }

    def _with_expiry(self, token: dict[str, Any]) -> dict[str, Any]:
        token = dict(token)
        token["expires_at"] = datetime.now().timestamp() + int(token.get("expires_in", 0))
        return token

    async def _update_entry_tokens(self, token_data: Mapping[str, Any]) -> None:
        data = {**self.entry.data, **token_data}
        self.hass.config_entries.async_update_entry(self.entry, data=data)


class _AppConfigParser(HTMLParser):
    """Read the same environment metadata consumed by the Hero web app."""

    def __init__(self) -> None:
        super().__init__()
        self.content: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "linced-parents/config/environment":
            self.content = attributes.get("content")


def _option_value(option: Any) -> str | None:
    if isinstance(option, dict):
        return option.get("valueString") or option.get("value")
    if isinstance(option, str):
        return option
    return None


def _safe_snippet(text: str, limit: int = 500) -> str:
    """Return a short, log-safe response snippet."""
    clean = " ".join(text.split())
    return clean[:limit]


async def _response_snippet(response, limit: int = 500) -> str:
    """Return a short response snippet without assuming the body is text."""
    return _safe_snippet(await _response_text(response), limit)


async def _response_text(response) -> str:
    """Return response text without assuming UTF-8 or text content."""
    body = await response.read()
    content_type = response.headers.get("Content-Type", "unknown")
    if not body:
        return f"<empty response; content-type={content_type}>"

    try:
        text = body.decode(response.charset or "utf-8", errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    if "\ufffd" in text[:80] and not content_type.startswith(("text/", "application/json", "application/xml")):
        return f"<binary response; content-type={content_type}; bytes={len(body)}>"
    return text


def _redact_url(url: str) -> str:
    """Redact query parameters that may contain credentials or tokens."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    safe_params = []
    sensitive = {"code", "code_verifier", "refresh_token", "access_token", "password", "key"}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        value = "***" if key in sensitive else values[0] if values else ""
        safe_params.append(f"{key}={value}")
    return parsed._replace(query="&".join(safe_params)).geturl()


def _html_href(text: str) -> str | None:
    """Read explicit browser navigation, never stylesheet or favicon links."""
    match = re.search(
        r"""(?:window\.)?location\.replace\(\s*["']([^"']+)["']\s*\)""",
        text,
    )
    if match:
        return unescape(match.group(1))
    match = re.search(r"""<a\b[^>]*\bhref=["']([^"']+)["']""", text, re.I)
    return unescape(match.group(1)) if match else None


class _ParsedForm:
    def __init__(self) -> None:
        self.action: str | None = None
        self.method = "POST"
        self.inputs: dict[str, str] = {}


class _FirstFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form: _ParsedForm | None = None
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form" and self.form is None:
            values = dict(attrs)
            self.form = _ParsedForm()
            self.form.action = values.get("action")
            self.form.method = (values.get("method") or "POST").upper()
            self._in_form = True
            return

        if self._in_form and tag == "input" and self.form is not None:
            values = dict(attrs)
            name = values.get("name")
            if name:
                self.form.inputs[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False


def _parse_first_form(text: str) -> _ParsedForm | None:
    parser = _FirstFormParser()
    parser.feed(text)
    return parser.form
