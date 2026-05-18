"""
AdMob Mediation Tool — single-file FastAPI app.

Per push, for each selected source ad unit:
  1) Push the user's saved /networks credentials to AdMob as AdUnitMappings
     on the source ad unit (so they can be replicated).
  2) Create N labeled tier ad units (named per-tier with the eCPM).
  3) Replicate every 3P AdUnitMapping from the source onto each tier.
  4) Create ONE mediation group in a SINGLE create call:
       - targeting:  [source, tier_1, ..., tier_N]
       - waterfall:  N MANUAL AdMob Network lines at descending tier eCPMs
       - bidding:    AdMob Network LIVE + LIVE lines for each replicated
                     third-party network
  5) Read the group back and report what AdMob actually persisted.

ADDITIONAL DEPENDENCY (install before running):
    pip install cryptography
"""
from __future__ import annotations

import os

# IMPORTANT: must be set BEFORE importing google_auth_oauthlib / oauthlib.
# Google often returns scopes in a different order than requested (especially
# when `include_granted_scopes=true`), which makes oauthlib raise
# `Warning: Scope has changed from ... to ...` and abort the token exchange.
# Relaxing this lets the callback succeed even when scopes are reordered or
# Google grants an extra one (e.g. openid getting merged in).
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
# Permit http://localhost during local development so oauthlib doesn't reject
# the non-HTTPS redirect URI.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

import json
import random
import string
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Tuple

# Force unbuffered stdout so _log() messages appear in the terminal in real
# time. Some Windows/PowerShell setups (and PyCharm's "Run" console) buffer
# stdout in big chunks even when print() is called with flush=True, which
# makes long-running pushes look like they've hung.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except (AttributeError, OSError):
    pass
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import requests
import uvicorn
from fastapi import APIRouter, Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware


# ============================================================================
# CONFIG
# ============================================================================
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/callback"
    admob_publisher_id: str = ""
    secret_key: str = "change-me-in-env"
    database_url: str = "sqlite:///./admob_tool.db"
    # Bind to "localhost" (not "127.0.0.1") so that the session cookie set on
    # the OAuth redirect URI (which defaults to http://localhost:8000/...)
    # is sent back on the callback. Mixing the two hostnames causes the
    # callback to lose request.session["oauth_state"] and fail with
    # "Invalid OAuth callback (state mismatch)".
    host: str = "localhost"
    port: int = 8000
    debug: bool = True
    oauth_scopes: list[str] = [
        "https://www.googleapis.com/auth/admob.readonly",
        "https://www.googleapis.com/auth/admob.report",
        "https://www.googleapis.com/auth/admob.monetization",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]


settings = Settings()
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


# ============================================================================
# STEP LOGGER  (timestamps + flush so the console reflects progress in real
# time, even when one API call is taking a long time). Also tees to
# `flow.log` next to flow.py so you have a file to inspect if the terminal
# is buffering or you can't see the console.
# ============================================================================
_LOG_FILE = Path(__file__).resolve().parent / "flow.log"

# Open flow.log once for the whole run (truncates at start) and keep the
# handle open — avoids an open()/close() syscall on every _log() line, which
# adds up to hundreds of them during a long push.
try:
    _LOG_FH = _LOG_FILE.open("w", encoding="utf-8")
    _LOG_FH.write(f"=== flow.py started "
                  f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _LOG_FH.flush()
except Exception:
    _LOG_FH = None


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


def _timed(label: str, fn):
    """Run fn(), log the elapsed time, return its result. On exception,
    log the failure and re-raise."""
    start = time.time()
    _log(f"  -> {label} ...")
    try:
        result = fn()
        elapsed = time.time() - start
        _log(f"     {label} OK ({elapsed:.2f}s)")
        return result
    except Exception as e:
        elapsed = time.time() - start
        _log(f"     {label} FAILED after {elapsed:.2f}s: {type(e).__name__}: {e}")
        raise


# ============================================================================
# WATERFALL FORMULA  (tweak constants to change calculation)
# ============================================================================
WATERFALL_TOP_MULTIPLIER = 1.91
WATERFALL_STEP_FACTOR = 0.80
WATERFALL_DEFAULT_LINES = 5
WATERFALL_MAX_LINES = 20


COMMON_COUNTRIES = [
    {"code": "US", "name": "United States"}, {"code": "GB", "name": "United Kingdom"},
    {"code": "DE", "name": "Germany"}, {"code": "FR", "name": "France"},
    {"code": "JP", "name": "Japan"}, {"code": "KR", "name": "South Korea"},
    {"code": "CA", "name": "Canada"}, {"code": "AU", "name": "Australia"},
    {"code": "BR", "name": "Brazil"}, {"code": "MX", "name": "Mexico"},
    {"code": "IN", "name": "India"}, {"code": "ID", "name": "Indonesia"},
    {"code": "PK", "name": "Pakistan"}, {"code": "BD", "name": "Bangladesh"},
    {"code": "PH", "name": "Philippines"}, {"code": "VN", "name": "Vietnam"},
    {"code": "TH", "name": "Thailand"}, {"code": "MY", "name": "Malaysia"},
    {"code": "SG", "name": "Singapore"}, {"code": "HK", "name": "Hong Kong"},
    {"code": "TW", "name": "Taiwan"}, {"code": "CN", "name": "China"},
    {"code": "SA", "name": "Saudi Arabia"}, {"code": "AE", "name": "United Arab Emirates"},
    {"code": "EG", "name": "Egypt"}, {"code": "TR", "name": "Turkey"},
    {"code": "RU", "name": "Russia"}, {"code": "ES", "name": "Spain"},
    {"code": "IT", "name": "Italy"}, {"code": "NL", "name": "Netherlands"},
    {"code": "SE", "name": "Sweden"}, {"code": "NO", "name": "Norway"},
    {"code": "PL", "name": "Poland"}, {"code": "IR", "name": "Iran"},
    {"code": "IQ", "name": "Iraq"}, {"code": "ZA", "name": "South Africa"},
    {"code": "NG", "name": "Nigeria"}, {"code": "KE", "name": "Kenya"},
    {"code": "AR", "name": "Argentina"}, {"code": "CL", "name": "Chile"},
    {"code": "CO", "name": "Colombia"}, {"code": "PE", "name": "Peru"},
    {"code": "NZ", "name": "New Zealand"}, {"code": "IE", "name": "Ireland"},
    {"code": "CH", "name": "Switzerland"}, {"code": "AT", "name": "Austria"},
    {"code": "BE", "name": "Belgium"}, {"code": "DK", "name": "Denmark"},
    {"code": "FI", "name": "Finland"}, {"code": "PT", "name": "Portugal"},
]
FLOOR_TYPES = ["ALL_PRICES", "PREMIUM_ONLY", "CUSTOM"]


# ============================================================================
# 3RD-PARTY NETWORK CATALOG
# ============================================================================
NETWORK_CATALOG = [
    {
        "code": "ADMOB",
        "name": "AdMob Network",
        "admob_source_id": "5450213213286189855",
        "supports_bidding": False,
        "app_fields": [],
        "ad_unit_fields": [],
        "internal_only": True,
    },
    {
        "code": "META",
        "name": "Meta Audience Network",
        "admob_source_id": "10568273989961140677",
        "supports_bidding": True,
        "app_fields": [],
        "ad_unit_fields": [
            {"key": "placement_id", "label": "Placement ID", "type": "text",
             "admob_key": "placement_id",
             "help": "From Meta Monetization Manager → Placements"},
        ],
    },
    {
        "code": "APPLOVIN",
        "name": "AppLovin",
        "admob_source_id": "1063618907739174004",
        "supports_bidding": True,
        "app_fields": [
            {"key": "sdk_key", "label": "SDK Key", "type": "password",
             "admob_key": "sdk_key",
             "help": "AppLovin dashboard → Account → Keys"},
        ],
        "ad_unit_fields": [
            {"key": "zone_id", "label": "Zone ID", "type": "text",
             "admob_key": "zone_id",
             "help": "AppLovin dashboard → MAX → Ad Units"},
        ],
    },
    {
        "code": "UNITY",
        "name": "Unity Ads",
        "admob_source_id": "4970775877303683148",
        "supports_bidding": True,
        "app_fields": [
            {"key": "game_id", "label": "Game ID", "type": "text",
             "admob_key": "game_id",
             "help": "Unity dashboard → Operate → Settings → Project ID"},
        ],
        "ad_unit_fields": [
            {"key": "placement_id", "label": "Placement ID", "type": "text",
             "admob_key": "placement_id",
             "help": "Unity dashboard → Operate → Placements"},
        ],
    },
    {
        "code": "IRONSOURCE",
        "name": "ironSource",
        "admob_source_id": "6925240245545091930",
        "supports_bidding": True,
        "app_fields": [
            {"key": "app_key", "label": "App Key", "type": "password",
             "admob_key": "app_key",
             "help": "ironSource dashboard → My Apps"},
        ],
        "ad_unit_fields": [
            {"key": "instance_id", "label": "Instance ID", "type": "text",
             "admob_key": "instance_id",
             "help": "ironSource dashboard → Setup → Instance"},
        ],
    },
    {
        "code": "MINTEGRAL",
        "name": "Mintegral",
        "admob_source_id": "1357746574408583713",
        "supports_bidding": True,
        "app_fields": [
            {"key": "app_id", "label": "App ID", "type": "text",
             "admob_key": "app_id",
             "help": "Mintegral dashboard → Apps"},
            {"key": "app_key", "label": "App Key", "type": "password",
             "admob_key": "app_key",
             "help": "Mintegral dashboard → Apps"},
        ],
        "ad_unit_fields": [
            {"key": "placement_id", "label": "Placement ID", "type": "text",
             "admob_key": "placement_id",
             "help": "Mintegral dashboard → Ad Placement"},
            {"key": "unit_id", "label": "Unit ID", "type": "text",
             "admob_key": "unit_id",
             "help": "Mintegral dashboard → Ad Placement → Unit"},
        ],
    },
    {
        "code": "PANGLE",
        "name": "Pangle",
        "admob_source_id": "4646036753406801667",
        "supports_bidding": True,
        "app_fields": [
            {"key": "app_id", "label": "App ID", "type": "text",
             "admob_key": "app_id",
             "help": "Pangle dashboard → App management"},
        ],
        "ad_unit_fields": [
            {"key": "slot_id", "label": "Slot ID", "type": "text",
             "admob_key": "slot_id",
             "help": "Pangle dashboard → Ad placements"},
        ],
    },
]
NETWORK_BY_CODE = {n["code"]: n for n in NETWORK_CATALOG}


# ============================================================================
# CREDENTIAL ENCRYPTION
# ============================================================================
_FERNET = None


def _get_fernet():
    # Built once and reused — the SHA-256 key derivation + Fernet construction
    # otherwise re-runs on every encrypt_dict / decrypt_dict call.
    global _FERNET
    if _FERNET is None:
        import base64, hashlib
        from cryptography.fernet import Fernet
        raw = (settings.secret_key or "change-me-in-env").encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        _FERNET = Fernet(key)
    return _FERNET


def encrypt_dict(d: dict) -> str:
    return _get_fernet().encrypt(json.dumps(d or {}).encode("utf-8")).decode("ascii")


def decrypt_dict(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_get_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


# ============================================================================
# DATABASE
# ============================================================================
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================================
# MODELS
# ============================================================================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    google_sub = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(255), nullable=False)
    name = Column(String(255), default="")
    picture = Column(String(512), default="")
    admob_publisher_id = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    token = relationship("OAuthToken", back_populates="user", uselist=False, cascade="all, delete-orphan")
    apps = relationship("AdMobApp", back_populates="user", cascade="all, delete-orphan")
    mediation_groups = relationship("MediationGroup", back_populates="user", cascade="all, delete-orphan")


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, default="")
    token_uri = Column(String(255), default="https://oauth2.googleapis.com/token")
    expiry = Column(DateTime, nullable=True)
    scopes = Column(Text, default="")
    user = relationship("User", back_populates="token")


class AdMobApp(Base):
    __tablename__ = "admob_apps"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    app_id = Column(String(128), nullable=False)
    name = Column(String(255), default="")
    platform = Column(String(16), default="ANDROID")
    package_name = Column(String(255), default="")
    last_synced_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User", back_populates="apps")
    ad_units = relationship("AdUnit", back_populates="app", cascade="all, delete-orphan")


class AdUnit(Base):
    __tablename__ = "ad_units"
    id = Column(Integer, primary_key=True)
    app_id = Column(Integer, ForeignKey("admob_apps.id"), nullable=False)
    ad_unit_id = Column(String(128), nullable=False)
    name = Column(String(255), default="")
    ad_format = Column(String(32), default="BANNER")
    last_synced_at = Column(DateTime, default=datetime.utcnow)
    app = relationship("AdMobApp", back_populates="ad_units")


class MediationGroup(Base):
    __tablename__ = "mediation_groups"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    ad_format = Column(String(32), nullable=False)
    platform = Column(String(16), nullable=False)
    status = Column(String(16), default="DRAFT")
    country_mode = Column(String(16), default="GLOBAL")
    countries = Column(JSON, default=list)
    floor_type = Column(String(32), default="ALL_PRICES")
    target_ad_unit_id = Column(String(128), default="")
    target_ad_unit_name = Column(String(255), default="")
    base_avg_ecpm = Column(Float, default=0.0)
    report_metrics = Column(JSON, default=dict)
    admob_group_id = Column(String(64), default="")
    admob_group_name = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_push_response = Column(Text, default="")
    user = relationship("User", back_populates="mediation_groups")
    waterfall_lines = relationship("WaterfallLine", back_populates="group",
                                   cascade="all, delete-orphan",
                                   order_by="WaterfallLine.priority")


class WaterfallLine(Base):
    __tablename__ = "waterfall_lines"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("mediation_groups.id"), nullable=False)
    priority = Column(Integer, default=0)
    line_name = Column(String(255), default="")
    ecpm_usd = Column(Float, default=0.0)
    enabled = Column(Boolean, default=True)
    network_code = Column(String(32), default="")
    cpm_mode = Column(String(16), default="MANUAL")
    admob_line_key = Column(String(64), default="")
    group = relationship("MediationGroup", back_populates="waterfall_lines")


class NetworkCredential(Base):
    __tablename__ = "network_credentials"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    app_id = Column(Integer, ForeignKey("admob_apps.id"), nullable=False)
    network_code = Column(String(32), nullable=False)
    encrypted_fields = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdUnitMapping(Base):
    __tablename__ = "ad_unit_network_mappings"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    app_id = Column(Integer, ForeignKey("admob_apps.id"), nullable=False)
    ad_unit_id = Column(String(128), nullable=False)
    network_code = Column(String(32), nullable=False)
    encrypted_fields = Column(Text, default="")
    admob_mapping_id = Column(String(64), default="")
    admob_mapping_name = Column(String(255), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdMobSession(Base):
    """The user's admob.google.com browser session, captured once from a
    DevTools cURL. Used by the internal-API path to create real AdMob
    Network waterfall lines. Stored encrypted; cookies expire so this is
    refreshed when the API returns 401/403."""
    __tablename__ = "admob_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    encrypted_blob = Column(Text, default="")  # {cookie, xsrf, f_sid}
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================================
# OAUTH HELPERS
# ============================================================================
def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def build_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(_client_config(), scopes=settings.oauth_scopes, state=state)
    flow.redirect_uri = settings.google_redirect_uri
    return flow


def get_authorization_url() -> Tuple[str, str, str]:
    flow = build_flow()
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return auth_url, state, flow.code_verifier or ""


def credentials_from_db(token_row) -> Credentials:
    creds = Credentials(
        token=token_row.access_token,
        refresh_token=token_row.refresh_token or None,
        token_uri=token_row.token_uri,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=(token_row.scopes or "").split(",") if token_row.scopes else settings.oauth_scopes,
    )
    if token_row.expiry:
        creds.expiry = token_row.expiry
    return creds


def refresh_if_needed(creds: Credentials) -> Credentials:
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
    return creds


def persist_credentials(db: Session, user: User, creds: Credentials) -> None:
    token_row = user.token
    if token_row is None:
        token_row = OAuthToken(user_id=user.id)
        db.add(token_row)
    token_row.access_token = creds.token or ""
    if creds.refresh_token:
        token_row.refresh_token = creds.refresh_token
    token_row.token_uri = creds.token_uri or "https://oauth2.googleapis.com/token"
    token_row.expiry = creds.expiry if isinstance(creds.expiry, datetime) else None
    token_row.scopes = ",".join(creds.scopes or [])
    db.commit()


# ============================================================================
# ADMOB API CLIENT
# ============================================================================
class AdMobAPIError(Exception):
    pass


def _format_http_error(e: HttpError) -> str:
    try:
        payload = json.loads(e.content.decode("utf-8"))
        err = payload.get("error", {}) or {}
        msg = err.get("message", str(e))
        status = err.get("status", "")
        parts = [f"AdMob API error: {msg}"]
        if status:
            parts.append(f"[status={status}]")
        # FAILED_PRECONDITION / INVALID_ARGUMENT errors carry the real
        # reason in error.details — surface it so we can see WHICH
        # precondition / field actually failed.
        for d in err.get("details", []) or []:
            dtype = d.get("@type", "")
            if "PreconditionFailure" in dtype:
                for v in d.get("violations", []) or []:
                    parts.append(
                        f"[precondition type={v.get('type','?')} "
                        f"subject={v.get('subject','?')} "
                        f"desc={v.get('description','?')}]"
                    )
            elif "BadRequest" in dtype:
                for v in d.get("fieldViolations", []) or []:
                    parts.append(
                        f"[field={v.get('field','?')} "
                        f"desc={v.get('description','?')}]"
                    )
            elif "ErrorInfo" in dtype:
                parts.append(
                    f"[reason={d.get('reason','?')} "
                    f"metadata={d.get('metadata',{})}]"
                )
        return " ".join(parts)
    except Exception:
        return f"AdMob API error: {e}"


def _date_parts(yyyy_mm_dd: str) -> dict:
    y, m, d = yyyy_mm_dd.split("-")
    return {"year": int(y), "month": int(m), "day": int(d)}


def _admob_today():
    """Today's date in AdMob's reporting timezone (America/Los_Angeles)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=8)).date()


def _today_iso() -> str:
    return _admob_today().isoformat()


def _days_ago_iso(n: int) -> str:
    return (_admob_today() - timedelta(days=n)).isoformat()


def _random_suffix(n: int = 6) -> str:
    return "".join(random.choices(string.digits, k=n))


def _build_tier_name(prefix: str, ad_unit_name: str, tier: int, ecpm: float,
                     unique: bool) -> str:
    """Build the display name for a tier ad unit (and its matching waterfall
    line). AdMob caps displayName at 80 chars."""
    suffix = f"_{_random_suffix()}" if unique else ""
    full = f"{prefix}_{ad_unit_name}_line_{tier}_waterfall_ecpm_{ecpm:.2f}{suffix}"
    full = full.replace(" ", "_")
    if len(full) <= 80:
        return full
    fixed = f"{prefix}_line_{tier}_waterfall_ecpm_{ecpm:.2f}{suffix}"
    fixed = fixed.replace(" ", "_")
    room = max(8, 80 - len(fixed) - 1)
    short_name = ad_unit_name.replace(" ", "_")[:room]
    return f"{prefix}_{short_name}_line_{tier}_waterfall_ecpm_{ecpm:.2f}{suffix}"[:80]


class AdMobClient:
    def __init__(self, db: Session, user: User):
        self.db = db
        self.user = user
        if user.token is None:
            raise RuntimeError("User has no OAuth token. Sign in again.")
        creds = credentials_from_db(user.token)
        prev_token = creds.token
        creds = refresh_if_needed(creds)
        # Only write the token back to the DB when it actually changed (i.e.
        # it was refreshed) — persisting unconditionally hits the DB on every
        # request even when nothing changed.
        if creds.token != prev_token:
            persist_credentials(db, user, creds)
        self.service = build("admob", "v1", credentials=creds, cache_discovery=False)
        self.service_beta = build("admob", "v1beta", credentials=creds, cache_discovery=False)
        self._creds = creds

    def list_accounts(self) -> list[dict]:
        try:
            return self.service.accounts().list().execute().get("account", [])
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    def get_publisher_id(self) -> str:
        if self.user.admob_publisher_id:
            return self.user.admob_publisher_id
        accounts = self.list_accounts()
        if not accounts:
            raise AdMobAPIError(
                "No AdMob account found for this Google user. "
                "Sign in with the Google account that owns the AdMob publisher."
            )
        pub_id = accounts[0]["publisherId"]
        self.user.admob_publisher_id = pub_id
        self.db.commit()
        return pub_id

    def list_apps(self) -> list[dict]:
        parent = f"accounts/{self.get_publisher_id()}"
        try:
            return self.service.accounts().apps().list(parent=parent).execute().get("apps", [])
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    def list_ad_units(self) -> list[dict]:
        parent = f"accounts/{self.get_publisher_id()}"
        try:
            return self.service.accounts().adUnits().list(parent=parent).execute().get("adUnits", [])
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    def fetch_network_report_for_ad_units(self, ad_unit_ids: list[str],
                                          start: str, end: str) -> dict[str, dict]:
        if not ad_unit_ids:
            return {}
        parent = f"accounts/{self.get_publisher_id()}"
        body = {
            "reportSpec": {
                "dateRange": {"startDate": _date_parts(start), "endDate": _date_parts(end)},
                "dimensions": ["AD_UNIT"],
                "dimensionFilters": [
                    {"dimension": "AD_UNIT", "matchesAny": {"values": ad_unit_ids}}
                ],
                "metrics": [
                    "AD_REQUESTS", "MATCHED_REQUESTS", "IMPRESSIONS",
                    "ESTIMATED_EARNINGS", "CLICKS", "IMPRESSION_CTR",
                    "IMPRESSION_RPM", "MATCH_RATE", "SHOW_RATE",
                ],
            }
        }
        try:
            resp = self.service.accounts().networkReport().generate(parent=parent, body=body).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

        rows = resp if isinstance(resp, list) else []
        out: dict[str, dict] = {}
        for entry in rows:
            row = entry.get("row")
            if not row:
                continue
            dims = row.get("dimensionValues", {})
            metrics = row.get("metricValues", {})
            ad_unit = dims.get("AD_UNIT", {}).get("value", "")
            if not ad_unit:
                continue

            def _int(key: str) -> int:
                v = metrics.get(key, {}).get("integerValue")
                return int(v) if v is not None else 0

            def _double(key: str) -> float:
                v = metrics.get(key, {}).get("doubleValue")
                if v is not None:
                    return float(v)
                iv = metrics.get(key, {}).get("integerValue")
                return float(iv) if iv is not None else 0.0

            def _micros(key: str) -> int:
                v = metrics.get(key, {}).get("microsValue")
                if v is not None:
                    return int(v)
                iv = metrics.get(key, {}).get("integerValue")
                if iv is not None:
                    return int(iv)
                dv = metrics.get(key, {}).get("doubleValue")
                return int(float(dv) * 1_000_000) if dv is not None else 0

            ad_requests = _int("AD_REQUESTS")
            matched = _int("MATCHED_REQUESTS")
            impressions = _int("IMPRESSIONS")
            clicks = _int("CLICKS")
            earnings_micros = _micros("ESTIMATED_EARNINGS")
            rpm_micros = _micros("IMPRESSION_RPM")

            revenue_usd = earnings_micros / 1_000_000.0
            rpm_usd = rpm_micros / 1_000_000.0
            ecpm_usd = (revenue_usd / impressions * 1000.0) if impressions else 0.0

            match_rate = (matched / ad_requests) if ad_requests else 0.0
            show_rate = (impressions / matched) if matched else 0.0
            fill_rate = (impressions / ad_requests) if ad_requests else 0.0
            ctr = _double("IMPRESSION_CTR")

            out[ad_unit] = {
                "ad_requests": ad_requests, "matched_requests": matched,
                "impressions": impressions, "clicks": clicks,
                "revenue_usd": round(revenue_usd, 2), "ecpm_usd": round(ecpm_usd, 2),
                "rpm_usd": round(rpm_usd, 2), "match_rate": round(match_rate, 4),
                "show_rate": round(show_rate, 4), "fill_rate": round(fill_rate, 4),
                "ctr": round(ctr, 4),
            }
        return out

    def list_ad_sources(self) -> list[dict]:
        if hasattr(self, "_ad_sources_cache"):
            return self._ad_sources_cache
        parent = f"accounts/{self.get_publisher_id()}"
        try:
            resp = self.service_beta.accounts().adSources().list(parent=parent).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e
        self._ad_sources_cache = resp.get("adSources", []) or []
        return self._ad_sources_cache

    def list_adapters_for_source(self, ad_source_id: str) -> list[dict]:
        cache_key = f"_adapters_cache_{ad_source_id}"
        if hasattr(self, cache_key):
            return getattr(self, cache_key)
        parent = f"accounts/{self.get_publisher_id()}/adSources/{ad_source_id}"
        try:
            resp = self.service_beta.accounts().adSources().adapters().list(parent=parent).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e
        out = resp.get("adapters", []) or []
        setattr(self, cache_key, out)
        return out

    def find_source_id_for_network(self, network_code: str) -> str:
        """Resolve the WATERFALL (non-bidding) ad source id for a network.

        Matches on the brand's first word (meta / applovin / unity /
        ironsource / mintegral / pangle) and PREFERS a title that does NOT
        contain '(bidding)', so MANUAL waterfall lines bind to the
        waterfall source rather than the bidding variant.
        """
        cat = NETWORK_BY_CODE.get(network_code.upper())
        if not cat:
            return ""
        name = cat["name"].lower()
        primary = name.split()[0] if name.split() else name
        try:
            sources = self.list_ad_sources()
        except AdMobAPIError:
            return cat["admob_source_id"]
        matches: list[tuple[str, str]] = []
        for src in sources:
            title = (src.get("title") or "").lower()
            sid = src.get("adSourceId") or ""
            if primary and primary in title and sid:
                matches.append((title, sid))
        # prefer non-bidding (the waterfall source)
        for title, sid in matches:
            if "(bidding)" not in title:
                return sid
        if matches:
            return matches[0][1]
        return cat["admob_source_id"]

    # Two distinct AdMob ad sources — DO NOT confuse them:
    #  - "AdMob Network"           -> LIVE bidding line. Only 1 per group.
    #  - "AdMob Network Waterfall" -> MANUAL waterfall lines. MANY per group.
    # Using "AdMob Network" for manual lines triggers AdMob's
    # "Max allowed AdMob Network lines exceeded" error.
    ADMOB_NETWORK_SOURCE_ID = "5450213213286189855"
    ADMOB_WATERFALL_SOURCE_ID = "1215381445328257950"

    def get_admob_network_source_id(self) -> str:
        """The 'AdMob Network' ad source — for the LIVE bidding line."""
        try:
            for src in self.list_ad_sources():
                if (src.get("title") or "").strip().lower() == "admob network":
                    return src.get("adSourceId") or self.ADMOB_NETWORK_SOURCE_ID
        except AdMobAPIError:
            pass
        return self.ADMOB_NETWORK_SOURCE_ID

    def get_admob_waterfall_source_id(self) -> str:
        """The 'AdMob Network Waterfall' ad source — for MANUAL waterfall
        lines. This source allows many manual lines in a single group."""
        try:
            for src in self.list_ad_sources():
                t = (src.get("title") or "").strip().lower()
                if t == "admob network waterfall":
                    return src.get("adSourceId") or self.ADMOB_WATERFALL_SOURCE_ID
        except AdMobAPIError:
            pass
        return self.ADMOB_WATERFALL_SOURCE_ID

    def get_admob_waterfall_adapter(self, ad_format: str, platform: str) -> dict:
        """Find the 'AdMob Network Waterfall' adapter for a given format +
        platform. Each adapter has exactly one required config field
        ('Ad Unit ID') whose value must be the tier ad unit's full ID.

        AdMob adapter `formats` use APP_OPEN (not APP_OPEN_AD) and may use
        BANNER_AND_INTERSTITIAL; we normalise accordingly.
        """
        source_id = self.get_admob_waterfall_source_id()
        adapters = self.list_adapters_for_source(source_id)
        fmt = (ad_format or "").upper()
        if fmt == "APP_OPEN_AD":
            fmt = "APP_OPEN"
        plat = (platform or "").upper()

        def fmt_match(a: dict) -> bool:
            adf = [f.upper() for f in (a.get("formats") or [])]
            if fmt in adf:
                return True
            # BANNER / INTERSTITIAL adapters are bundled as
            # BANNER_AND_INTERSTITIAL on the waterfall source.
            if fmt in ("BANNER", "INTERSTITIAL") and "BANNER_AND_INTERSTITIAL" in adf:
                return True
            return False

        for a in adapters:
            if (a.get("platform") or "").upper() == plat and fmt_match(a):
                return a
        # fallback: any adapter on the right platform
        for a in adapters:
            if (a.get("platform") or "").upper() == plat:
                return a
        if adapters:
            return adapters[0]
        raise AdMobAPIError(
            "No 'AdMob Network Waterfall' adapter found for "
            f"format={ad_format} platform={platform}"
        )

    def create_waterfall_mapping_on_source(
        self,
        source_ad_unit_id: str,
        tier_ad_unit_id: str,
        ad_format: str,
        platform: str,
        display_name: str = "",
    ) -> str:
        """Create an AdUnitMapping ON the source ad unit that routes a
        waterfall line to the given TIER ad unit, via the 'AdMob Network
        Waterfall' adapter.

        The adapter's single required config ('Ad Unit ID') is set to the
        tier ad unit's full id. Returns the created mapping's resource name
        (accounts/{pub}/adUnits/{src}/adUnitMappings/{id}) for use in a
        mediation group line's adUnitMappings dict.
        """
        adapter = self.get_admob_waterfall_adapter(ad_format, platform)
        adapter_id = str(adapter.get("adapterId", ""))
        meta = adapter.get("adapterConfigMetadata", []) or []
        if not meta:
            raise AdMobAPIError(
                f"Waterfall adapter {adapter_id} has no config metadata."
            )
        config_id = str(meta[0].get("adapterConfigMetadataId", ""))

        short_src = source_ad_unit_id.split("/")[-1]
        parent = f"accounts/{self.get_publisher_id()}/adUnits/{short_src}"
        body = {
            "adapterId": adapter_id,
            "adUnitConfigurations": {config_id: tier_ad_unit_id},
            "state": "ENABLED",
        }
        if display_name:
            body["displayName"] = display_name[:80]
        try:
            resp = self._with_quota_retry(
                lambda: self.service_beta.accounts().adUnits()
                .adUnitMappings().create(parent=parent, body=body).execute()
            )
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e
        return resp.get("name", "") or ""

    def get_mediation_group_in_admob(self, mediation_group_id: str) -> dict:
        """Read a single mediation group's current state by ID.

        NOTE: The AdMob v1beta `mediationGroups` resource only exposes
        `list`, `create`, `patch`, and `delete` — there is no `get` method.
        Calling `.get(...)` on the Resource raises
        `AttributeError: 'Resource' object has no attribute 'get'`.
        We emulate get by listing and filtering client-side.
        """
        parent = f"accounts/{self.get_publisher_id()}"
        full_name = f"{parent}/mediationGroups/{mediation_group_id}"
        target_id = str(mediation_group_id).strip()
        page_token: str | None = None
        while True:
            kwargs = {"parent": parent, "pageSize": 200}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._with_quota_retry(
                lambda kw=kwargs: self.service_beta.accounts()
                .mediationGroups().list(**kw).execute()
            )
            for g in resp.get("mediationGroups", []) or []:
                gid = str(g.get("mediationGroupId") or "").strip()
                gname = str(g.get("name") or "")
                if gid == target_id or gname == full_name \
                        or gname.endswith(f"/{target_id}"):
                    return g
            page_token = resp.get("nextPageToken") or None
            if not page_token:
                break
        raise AdMobAPIError(
            f"Mediation group {mediation_group_id} not found under {parent}."
        )

    # ========================================================================
    # Quota / rate-limit retry helper
    # ========================================================================
    def _with_quota_retry(self, fn, max_retries: int = 2, base_delay: float = 3.0):
        """Execute fn(); on RESOURCE_EXHAUSTED / quota / 429, retry with a
        SHORT backoff (3s, 6s ≈ 9s total) and then give up. We deliberately
        fail fast: if the quota window is genuinely empty (e.g. daily cap
        exceeded from prior test runs), waiting 2+ minutes per call doesn't
        help — the only fix is to wait minutes/hours or request a quota
        increase in Google Cloud Console.

        Sometimes a 60-second sleep helps; usually it doesn't. If you do
        want longer retries on a specific call, bump max_retries at the
        call site rather than globally."""
        last_err: HttpError | None = None
        for attempt in range(max_retries):
            try:
                return fn()
            except HttpError as e:
                content_lower = ""
                try:
                    content_lower = (e.content or b"").decode("utf-8", errors="ignore").lower()
                except Exception:
                    pass
                full = f"{str(e).lower()} {content_lower}"
                is_quota = any(kw in full for kw in (
                    "exhausted", "resource_exhausted", "quota",
                    "rate limit", "ratelimit", "429",
                ))
                if is_quota and attempt < max_retries - 1:
                    wait = base_delay * (2 ** attempt)
                    _log(f"     ⚠ AdMob quota hit; "
                         f"sleeping {wait:.0f}s before retry "
                         f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    last_err = e
                    continue
                raise AdMobAPIError(_format_http_error(e)) from e
        if last_err is not None:
            raise AdMobAPIError(_format_http_error(last_err))
        raise AdMobAPIError("Quota retries exhausted")

    # ========================================================================
    # AD UNIT MAPPING helpers (third-party bidding network configs)
    # ========================================================================
    def list_ad_unit_mappings(self, ad_unit_id: str) -> list[dict]:
        """List existing AdUnitMappings (third-party network configs) for
        a given ad unit. Accepts either 'ca-app-pub-X/Y' or the short Y form.
        """
        short_id = ad_unit_id.split("/")[-1] if "/" in ad_unit_id else ad_unit_id
        parent = f"accounts/{self.get_publisher_id()}/adUnits/{short_id}"
        try:
            resp = self.service_beta.accounts().adUnits().adUnitMappings().list(
                parent=parent,
            ).execute()
            return resp.get("adUnitMappings", []) or []
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    def get_adapter_to_source_map(self) -> dict[str, str]:
        """Build and cache {adapter_id: ad_source_id} for every adapter under
        every ad source."""
        if hasattr(self, "_adapter_source_map"):
            return self._adapter_source_map
        mapping: dict[str, str] = {}
        try:
            for src in self.list_ad_sources():
                src_id = src.get("adSourceId", "")
                if not src_id:
                    continue
                try:
                    for ad in self.list_adapters_for_source(src_id):
                        adapter_id = str(ad.get("adapterId", ""))
                        if adapter_id:
                            mapping[adapter_id] = src_id
                except AdMobAPIError:
                    continue
        except AdMobAPIError:
            pass
        self._adapter_source_map = mapping
        return mapping

    # ========================================================================
    # NEW (FIX): replicate source ad unit's bidding mappings to tier ad units
    # ========================================================================
    def replicate_source_mappings_to_tier_ad_units(
        self,
        source_ad_unit_id: str,
        tier_ad_unit_ids: list[str],
    ) -> tuple[dict[str, dict[str, str]], list[dict], list[str]]:
        """For each tier ad unit, recreate every third-party (non-AdMob)
        AdUnitMapping that the source ad unit has, using the same adapterId
        and adUnitConfigurations.

        This is the LINE-BY-LINE MAPPING that was missing. Without it, when
        the mediation group targets tier ad units alongside the source,
        the bidding LIVE lines have no place to ask for a bid for those
        tiers — so they go unfilled.

        Returns:
            (per_ad_unit_mappings, errors, network_titles)
            per_ad_unit_mappings: {ad_unit_id: {ad_source_id: mapping_name}}
                keyed by EVERY ad unit (source + each tier).
            errors: list of dicts for any replication that failed.
            network_titles: list of unique network titles successfully mapped.
        """
        out: dict[str, dict[str, str]] = {source_ad_unit_id: {}}
        for tid in tier_ad_unit_ids:
            if tid:
                out[tid] = {}
        errors: list[dict] = []
        network_titles: list[str] = []

        # 1. Read source's existing mappings
        try:
            source_mappings = self.list_ad_unit_mappings(source_ad_unit_id)
        except AdMobAPIError as e:
            errors.append({"tier_ad_unit_id": "", "ad_source_id": "",
                           "stage": "list_source_mappings", "error": str(e)})
            return out, errors, network_titles

        adapter_to_source = self.get_adapter_to_source_map()
        admob_source_id = self.get_admob_network_source_id()

        # 2. Source titles for nicer error messages
        source_titles: dict[str, str] = {}
        try:
            for src in self.list_ad_sources():
                sid = src.get("adSourceId", "")
                if sid:
                    source_titles[sid] = src.get("title", "") or sid
        except AdMobAPIError:
            pass

        # 3. Capture each non-AdMob mapping as a replication template
        # ad_source_id -> {adapter_id, configs, source_mapping_name, title}
        templates: dict[str, dict] = {}
        for m in source_mappings:
            adapter_id = str(m.get("adapterId", ""))
            configs = m.get("adUnitConfigurations", {}) or {}
            mapping_name = m.get("name", "")
            state = (m.get("state") or "").upper()
            src_id = adapter_to_source.get(adapter_id, "")

            if not src_id or not mapping_name:
                continue
            if src_id == admob_source_id:
                continue  # AdMob Network handled separately
            if state and state != "ENABLED":
                errors.append({"tier_ad_unit_id": "", "ad_source_id": src_id,
                               "stage": "source_mapping_disabled",
                               "error": f"source mapping for "
                                        f"{source_titles.get(src_id, src_id)} "
                                        f"is in state {state!r}; not replicating"})
                continue
            if not configs:
                errors.append({"tier_ad_unit_id": "", "ad_source_id": src_id,
                               "stage": "source_mapping_empty_config",
                               "error": f"source mapping for "
                                        f"{source_titles.get(src_id, src_id)} "
                                        f"has empty adUnitConfigurations; "
                                        f"cannot replicate"})
                continue

            # Record the source's own mapping
            out[source_ad_unit_id][src_id] = mapping_name
            templates[src_id] = {
                "adapter_id": adapter_id,
                "configs": configs,
                "title": source_titles.get(src_id, src_id),
            }
            network_titles.append(source_titles.get(src_id, src_id))

        if not templates:
            # No third-party bidding to replicate; bidding section will be empty.
            # AdMob Network LIVE line will be the only bidding line (auto-added).
            return out, errors, network_titles

        # 4. For each tier ad unit, create one mapping per template
        pub_id = self.get_publisher_id()
        for tier_id in tier_ad_unit_ids:
            if not tier_id:
                continue
            short = tier_id.split("/")[-1] if "/" in tier_id else tier_id
            parent = f"accounts/{pub_id}/adUnits/{short}"
            for src_id, tmpl in templates.items():
                # Throttle to avoid hammering AdMob's write quota; the
                # retry helper handles real saturation when it happens.
                time.sleep(1.0)
                # NOTE: `name` on AdUnitMapping is OUTPUT-ONLY (the server
                # assigns the resource path). User-supplied labels go in
                # `displayName`. Sending `name` here was silently ignored.
                body = {
                    "displayName": (f"tier_{src_id[:8]}_{short[-6:]}_"
                                    f"{_random_suffix(4)}")[:80],
                    "adapterId": tmpl["adapter_id"],
                    "adUnitConfigurations": tmpl["configs"],
                    "state": "ENABLED",
                }
                try:
                    resp = self._with_quota_retry(
                        lambda b=body, p=parent: self.service_beta.accounts()
                        .adUnits().adUnitMappings().create(
                            parent=p, body=b,
                        ).execute()
                    )
                    mapping_name = resp.get("name", "") or ""
                    if mapping_name:
                        out[tier_id][src_id] = mapping_name
                    else:
                        errors.append({
                            "tier_ad_unit_id": tier_id,
                            "ad_source_id": src_id,
                            "stage": "create_tier_mapping",
                            "error": (f"{tmpl['title']}: create returned "
                                      "no resource name"),
                        })
                except AdMobAPIError as e:
                    errors.append({
                        "tier_ad_unit_id": tier_id,
                        "ad_source_id": src_id,
                        "stage": "create_tier_mapping",
                        "error": f"{tmpl['title']}: {e}",
                    })

        # De-dupe network titles
        network_titles = list(dict.fromkeys(network_titles))
        return out, errors, network_titles

    # ========================================================================
    # NEW (FIX): build LIVE bidding lines from the per-ad-unit mapping dict
    # ========================================================================
    def build_bidding_lines_from_mappings(
        self,
        per_ad_unit_mappings: dict[str, dict[str, str]],
    ) -> tuple[list[dict], list[str]]:
        """Build LIVE bidding lines from {ad_unit_id: {ad_source_id: mapping_name}}.

        For each non-AdMob ad source present in any ad unit's mapping, emit
        one LIVE line whose adUnitMappings dict covers every ad unit that
        has a mapping for that source. AdMob Network is excluded here and
        added explicitly by the caller.

        Returns (lines, source_titles_added).
        """
        admob_source_id = self.get_admob_network_source_id()

        # ad_source_id -> {ad_unit_id: mapping_name}
        by_source: dict[str, dict[str, str]] = {}
        for ad_unit_id, src_to_name in per_ad_unit_mappings.items():
            for src_id, mapping_name in src_to_name.items():
                if src_id == admob_source_id or not mapping_name:
                    continue
                by_source.setdefault(src_id, {})[ad_unit_id] = mapping_name

        source_titles: dict[str, str] = {}
        try:
            for src in self.list_ad_sources():
                sid = src.get("adSourceId", "")
                if sid:
                    source_titles[sid] = src.get("title", "") or sid
        except AdMobAPIError:
            pass

        lines: list[dict] = []
        titles_added: list[str] = []
        for src_id, ad_unit_mappings in by_source.items():
            if not ad_unit_mappings:
                continue
            title = source_titles.get(src_id) or f"Source {src_id[:12]}"
            lines.append({
                "adSourceId": src_id,
                "displayName": f"{title} (bidding)"[:80],
                "cpmMode": "LIVE",
                "state": "ENABLED",
                "adUnitMappings": ad_unit_mappings,
            })
            titles_added.append(title)
        return lines, titles_added

    # ========================================================================
    # NEW (FIX): audit group state after creation (issue 1)
    # ========================================================================
    # ========================================================================
    # CREATE AD UNIT (tier creation)
    # ========================================================================
    def create_ad_unit_in_admob(
        self,
        app_id_full: str,
        display_name: str,
        ad_format: str,
    ) -> dict:
        """POST /v1beta/accounts/{pub}/adUnits — create a new AdMob ad unit.

        NOTE: The AdMob v1beta `AdUnit` resource has NO `state` field. The
        valid fields are: name (output), adUnitId (output), appId,
        displayName, adFormat, adTypes, rewardSettings. Sending `state`
        triggers: "Invalid JSON payload received. Unknown name 'state' at
        'ad_unit': Cannot find field." — which was breaking tier creation
        and cascading into empty bidding sections + 1/5 waterfall lines.
        """
        parent = f"accounts/{self.get_publisher_id()}"
        fmt_map = {
            "BANNER": "BANNER",
            "INTERSTITIAL": "INTERSTITIAL",
            "REWARDED": "REWARDED",
            "REWARDED_INTERSTITIAL": "REWARDED_INTERSTITIAL",
            "NATIVE": "NATIVE",
            "APP_OPEN": "APP_OPEN_AD",
            "APP_OPEN_AD": "APP_OPEN_AD",
        }
        admob_format = fmt_map.get((ad_format or "").upper(),
                                   (ad_format or "BANNER").upper())
        body: dict = {
            "appId": app_id_full,
            "displayName": display_name[:80],
            "adFormat": admob_format,
        }
        ad_types_default = {
            "BANNER": ["RICH_MEDIA", "VIDEO"],
            "INTERSTITIAL": ["RICH_MEDIA", "VIDEO"],
            "REWARDED": ["RICH_MEDIA", "VIDEO"],
            "REWARDED_INTERSTITIAL": ["VIDEO"],
            "NATIVE": ["RICH_MEDIA", "VIDEO"],
            "APP_OPEN_AD": ["RICH_MEDIA", "VIDEO"],
        }
        body["adTypes"] = ad_types_default.get(admob_format, ["RICH_MEDIA", "VIDEO"])
        if admob_format in ("REWARDED", "REWARDED_INTERSTITIAL"):
            body["rewardSettings"] = {
                "rewardAmount": "1",
                "rewardItem": "reward",
            }
        return self._with_quota_retry(
            lambda: self.service_beta.accounts().adUnits().create(
                parent=parent, body=body,
            ).execute()
        )

    # ========================================================================
    # (Third-party adapter helpers — kept for /networks UI parity)
    # ========================================================================
    def resolve_adapter_for_network(self, network_code: str, platform: str) -> dict | None:
        source_id = self.find_source_id_for_network(network_code)
        if not source_id:
            return None
        try:
            adapters = self.list_adapters_for_source(source_id)
        except AdMobAPIError:
            return None
        target = (platform or "").upper()
        for ad in adapters:
            ad_platform = (ad.get("platform") or "").upper()
            if ad_platform == target:
                return ad
        return adapters[0] if adapters else None

    def build_admob_config_payload(
        self,
        network_code: str,
        platform: str,
        user_fields: dict,
    ) -> tuple[str, dict, list[str]]:
        adapter = self.resolve_adapter_for_network(network_code, platform)
        if adapter is None:
            raise AdMobAPIError(
                f"Could not find an AdMob adapter for {network_code} on {platform}."
            )
        adapter_id = str(adapter.get("adapterId", ""))
        metadata = adapter.get("adapterConfigMetadata", []) or []
        cat = NETWORK_BY_CODE.get(network_code.upper(), {})
        all_field_specs = (cat.get("app_fields") or []) + (cat.get("ad_unit_fields") or [])
        configs: dict[str, str] = {}
        warnings: list[str] = []
        for spec in all_field_specs:
            value = user_fields.get(spec["key"])
            if not value:
                continue
            label = spec["label"].lower()
            key_norm = spec["key"].replace("_", "").lower()
            matched_id = None
            for md in metadata:
                md_label = (md.get("adapterConfigMetadataLabel") or "").lower()
                md_label_norm = md_label.replace(" ", "").replace("_", "")
                if (label in md_label or md_label in label
                        or key_norm in md_label_norm or md_label_norm in key_norm):
                    matched_id = str(md.get("adapterConfigMetadataId", ""))
                    break
            if matched_id:
                configs[matched_id] = str(value)
            else:
                warnings.append(
                    f"Could not map field '{spec['label']}' to any AdMob adapter "
                    f"metadata for {network_code}."
                )
        return adapter_id, configs, warnings

    def create_ad_unit_mapping_in_admob(
        self,
        ad_unit_id: str,
        network_code: str,
        platform: str,
        display_name: str,
        user_fields: dict,
    ) -> tuple[dict, list[str]]:
        adapter_id, configs, warnings = self.build_admob_config_payload(
            network_code=network_code, platform=platform, user_fields=user_fields,
        )
        if not configs:
            raise AdMobAPIError(
                f"No usable configuration values for {network_code} on {platform}."
            )
        short_id = ad_unit_id.split("/")[-1] if "/" in ad_unit_id else ad_unit_id
        parent = f"accounts/{self.get_publisher_id()}/adUnits/{short_id}"
        # `name` is output-only on AdUnitMapping; user-supplied labels go in
        # `displayName`.
        body = {
            "displayName": display_name[:80],
            "adapterId": adapter_id,
            "adUnitConfigurations": configs,
            "state": "ENABLED",
        }
        try:
            resp = self.service_beta.accounts().adUnits().adUnitMappings().create(
                parent=parent, body=body,
            ).execute()
            return resp, warnings
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    # ========================================================================
    # MEDIATION GROUPS
    # ========================================================================
    def export_group_config(self, group: MediationGroup) -> dict:
        return {
            "name": group.name,
            "ad_format": group.ad_format,
            "platform": group.platform,
            "status": group.status,
            "targeting": {
                "country_mode": group.country_mode,
                "countries": group.countries or [],
                "ad_unit_id": group.target_ad_unit_id,
                "ad_unit_name": group.target_ad_unit_name,
            },
            "floor_type": group.floor_type,
            "base_avg_ecpm_usd": group.base_avg_ecpm,
            "report_metrics_snapshot": group.report_metrics or {},
            "admob_group_id": group.admob_group_id,
            "admob_group_name": group.admob_group_name,
            "waterfall": [
                {"priority": l.priority, "line_name": l.line_name,
                 "ecpm_usd": l.ecpm_usd, "enabled": l.enabled,
                 "network_code": l.network_code, "cpm_mode": l.cpm_mode}
                for l in sorted(group.waterfall_lines, key=lambda x: x.priority)
            ],
        }

    def list_mediation_groups_in_admob(self) -> list[dict]:
        parent = f"accounts/{self.get_publisher_id()}"
        try:
            resp = self.service_beta.accounts().mediationGroups().list(
                parent=parent, pageSize=200,
            ).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e
        out = []
        for g in resp.get("mediationGroups", []) or []:
            targeting = g.get("targeting", {}) or {}
            out.append({
                "mediation_group_id": g.get("mediationGroupId", "") or g.get("name", "").split("/")[-1],
                "name_full": g.get("name", ""),
                "display_name": g.get("displayName", ""),
                "platform": targeting.get("platform", ""),
                "format": targeting.get("format", ""),
                "state": g.get("state", ""),
                "ad_unit_ids": targeting.get("adUnitIds", []) or [],
                "country_codes": targeting.get("targetedRegionCodes", []) or [],
                "line_count": len(g.get("mediationGroupLines", {}) or {}),
            })
        return out

    def patch_lines_into_group(
        self,
        mediation_group_id: str,
        admob_manual_ecpms: list[float],
    ) -> tuple[dict, int]:
        positive = sorted([e for e in admob_manual_ecpms if e and e > 0], reverse=True)
        if not positive:
            raise AdMobAPIError("No positive eCPM lines provided")
        last_exc: AdMobAPIError | None = None
        for n in range(len(positive), 0, -1):
            try:
                resp = self._patch_lines_body(mediation_group_id, positive[:n])
                return resp, n
            except AdMobAPIError as e:
                msg = str(e).lower()
                if "max allowed" in msg and "admob network" in msg:
                    last_exc = e
                    continue
                raise
        if last_exc:
            raise last_exc
        raise AdMobAPIError("Unknown error patching mediation group")

    def _patch_lines_body(self, mediation_group_id: str, ecpms: list[float]) -> dict:
        waterfall_source_id = self.get_admob_waterfall_source_id()
        new_lines: dict[str, dict] = {}
        update_mask_paths: list[str] = []
        for i, ecpm in enumerate(ecpms, start=1):
            line_id = f"-{i}"
            cpm_micros = int(round(ecpm * 1_000_000))
            new_lines[line_id] = {
                "displayName": f"Line {i} - ${ecpm:.2f}",
                "adSourceId": waterfall_source_id,
                "cpmMode": "MANUAL",
                "cpmMicros": str(cpm_micros),
                "state": "ENABLED",
            }
            # FieldMask for map subfields uses dot notation
            # (parent.key.subfield), not Python-style brackets. The bracketed
            # form `mediation_group_lines["-1"].cpm_micros` is rejected by
            # AdMob with INVALID_ARGUMENT. Also: AdMob accepts both snake_case
            # (the proto field name) and lowerCamelCase (the JSON name); we
            # use snake_case to match the proto definition.
            update_mask_paths.append(f"mediation_group_lines.{line_id}.cpm_micros")
            update_mask_paths.append(f"mediation_group_lines.{line_id}.cpm_mode")
            update_mask_paths.append(f"mediation_group_lines.{line_id}.display_name")
            update_mask_paths.append(f"mediation_group_lines.{line_id}.state")
            update_mask_paths.append(f"mediation_group_lines.{line_id}.ad_source_id")
        body = {"mediationGroupLines": new_lines}
        name = f"accounts/{self.get_publisher_id()}/mediationGroups/{mediation_group_id}"
        try:
            return self.service_beta.accounts().mediationGroups().patch(
                name=name,
                body=body,
                updateMask=",".join(update_mask_paths),
            ).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e

    def _count_lines_in_response(self, mg_resp: dict) -> dict:
        result = {"manual": 0, "live": 0}
        for line in (mg_resp.get("mediationGroupLines", {}) or {}).values():
            mode = (line.get("cpmMode") or "").upper()
            if mode == "MANUAL":
                result["manual"] += 1
            elif mode in ("LIVE", "OPTIMIZED"):
                result["live"] += 1
        return result

    def create_mediation_group_in_admob(
        self,
        display_name: str,
        platform: str,
        ad_format: str,
        targeting_ad_unit_ids: list[str],
        country_codes: list[str],
        manual_lines: list[dict],
        bidding_lines: list[dict] | None = None,
    ) -> tuple[dict, int, int, int]:
        """Create a mediation group in ONE call.

        `targeting_ad_unit_ids` = the SOURCE/placement ad unit(s) the group
        serves. `manual_lines` and `bidding_lines` are fully-built line
        dicts (each already carrying displayName / adSourceId / cpmMode /
        cpmMicros / state / adUnitMappings as needed).

        Returns (response, manual_actual, live_actual, manual_requested).
        """
        requested_manual = len(manual_lines)
        lines: dict[str, dict] = {}
        key = 1
        for ml in manual_lines:
            lines[f"-{key}"] = ml
            key += 1
        for bl in (bidding_lines or []):
            lines[f"-{key}"] = bl
            key += 1

        ad_format_map = {
            "BANNER": "BANNER", "INTERSTITIAL": "INTERSTITIAL",
            "REWARDED": "REWARDED", "REWARDED_INTERSTITIAL": "REWARDED_INTERSTITIAL",
            "NATIVE": "NATIVE", "APP_OPEN": "APP_OPEN_AD",
            "APP_OPEN_AD": "APP_OPEN_AD",
        }
        admob_format = ad_format_map.get(ad_format.upper(), ad_format.upper())
        targeting: dict = {
            "platform": platform.upper(),
            "format": admob_format,
            "adUnitIds": [x for x in targeting_ad_unit_ids if x],
        }
        if country_codes:
            targeting["targetedRegionCodes"] = country_codes
        body: dict = {
            "displayName": display_name[:80],
            "state": "ENABLED",
            "targeting": targeting,
        }
        if lines:
            body["mediationGroupLines"] = lines
        parent = f"accounts/{self.get_publisher_id()}"
        resp = self._with_quota_retry(
            lambda: self.service_beta.accounts().mediationGroups().create(
                parent=parent, body=body,
            ).execute()
        )
        counts = self._count_lines_in_response(resp)
        return resp, counts["manual"], counts["live"], requested_manual


# ============================================================================
# TEMPLATES + STATIC ASSETS
# ============================================================================
TEMPLATE_FILES: dict[str, str] = {}
CSS_CONTENT = ""

TEMPLATE_FILES["base.html"] = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{% block title %}AdMob Mediation Tool{% endblock %}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body>
  <header class="topbar">
    <div class="brand"><a href="/" class="brand-link"><span class="brand-mark">⌬</span><span class="brand-name">Mediation<span class="brand-dot">.</span>Tool</span></a></div>
    <nav class="topnav">
      {% if user and user.id %}
        <a href="/dashboard">Dashboard</a>
        <a href="/apps">Apps</a>
        <a href="/networks">Networks</a>
        <a href="/mediation">Mediation</a>
        <a href="/mediation/builder" class="cta">+ Builder</a>
        <span class="sep"></span>
        <span class="user-chip">{% if user.picture %}<img src="{{ user.picture }}" alt="" />{% endif %}<span>{{ user.email }}</span></span>
        <a href="/auth/logout" class="logout">Sign out</a>
      {% endif %}
    </nav>
  </header>
  <main class="content">{% block content %}{% endblock %}</main>
  <footer class="footer"><span>AdMob Mediation Tool</span><span class="footer-sep">·</span><span>1 group per source · Tier ad units · Replicated bidding mappings · Live audit</span></footer>
</body>
</html>"""

TEMPLATE_FILES["login.html"] = r"""{% extends "base.html" %}
{% block title %}Sign in · Mediation Tool{% endblock %}
{% block content %}
<section class="login-wrap">
  <div class="login-card">
    <p class="eyebrow">AdMob mediation workflow</p>
    <h1 class="display">Connect your <em>AdMob</em> account.</h1>
    <p class="lede">Sign in with Google. The tool pulls your AdMob apps, ad units, and last-7-day metrics live from the AdMob API, then helps you build mediation waterfalls — one mediation group per source ad unit, with N labeled tier ad units as lines.</p>
    <a class="btn-primary" href="/auth/login"><span class="g-mark">G</span>Continue with Google</a>
    <p class="fineprint">Requires AdMob API scopes: <code>admob.readonly</code>, <code>admob.report</code>, <code>admob.monetization</code>.</p>
  </div>
  <aside class="login-side">
    <h3>Workflow</h3>
    <ol>
      <li>Sign in with Google (OAuth)</li>
      <li>Sync your AdMob apps + ad units</li>
      <li>Open the Builder, pick app + ad units</li>
      <li>Choose country targeting + waterfall depth</li>
      <li>Fetch live AdMob report (7 days)</li>
      <li>Tool calculates waterfall eCPM values</li>
      <li>Push: tier ad units + 1 mediation group with N waterfall lines</li>
    </ol>
  </aside>
</section>
{% endblock %}"""

TEMPLATE_FILES["dashboard.html"] = r"""{% extends "base.html" %}
{% block title %}Dashboard · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head"><p class="eyebrow">Dashboard</p><h1 class="display">Welcome, {{ user.name or user.email }}.</h1></section>
{% if api_error %}<div class="alert alert-warn"><strong>AdMob API:</strong> {{ api_error }}</div>{% endif %}
<section class="grid grid-3">
  <article class="card"><p class="card-label">Publisher ID</p><p class="card-value mono">{{ publisher_id }}</p></article>
  <article class="card"><p class="card-label">Cached apps</p><p class="card-value">{{ app_count }}</p><a class="card-link" href="/apps">Manage →</a></article>
  <article class="card"><p class="card-label">Mediation groups</p><p class="card-value">{{ group_count }}</p><a class="card-link" href="/mediation">Open →</a></article>
</section>
<section class="cta-row">
  <a class="btn-primary" href="/mediation/builder">▶ Open Mediation Builder</a>
  <form method="post" action="/apps/sync" style="display:inline"><button type="submit" class="btn-secondary">↻ Sync Apps + Ad Units</button></form>
</section>
<section class="workflow">
  <h2 class="section-title">How the waterfall builder works</h2>
  <ol class="workflow-steps">
    <li><span class="step-no">01</span><span class="step-text">Sign in <span class="done">✓</span></span></li>
    <li><span class="step-no">02</span><span class="step-text">Sync your AdMob apps &amp; ad units <a href="/apps">→ Apps</a></span></li>
    <li><span class="step-no">03</span><span class="step-text">Open the Builder <a href="/mediation/builder">→ Builder</a></span></li>
    <li><span class="step-no">04</span><span class="step-text">Select an app + one or more source ad units</span></li>
    <li><span class="step-no">05</span><span class="step-text">Choose country targeting (Global / Choose / Exclude)</span></li>
    <li><span class="step-no">06</span><span class="step-text">Set waterfall depth (1–{{ max_lines }}) + floor type</span></li>
    <li><span class="step-no">07</span><span class="step-text">Fetch live AdMob report (last 7 days)</span></li>
    <li><span class="step-no">08</span><span class="step-text">Review/edit the calculated tier eCPM values</span></li>
    <li><span class="step-no">09</span><span class="step-text"><strong>Push to AdMob:</strong> For each source ad unit: (a) create N labeled tier ad units, (b) replicate every bidding mapping (Meta/AppLovin/etc.) from the source onto every tier, (c) create ONE mediation group targeting source + all tiers, with N MANUAL AdMob Network waterfall lines + AdMob Network LIVE bidding line (plus a LIVE line per replicated 3P network).</span></li>
    <li><span class="step-no">10</span><span class="step-text">Tool reads the group back and audits every line's state. Disabled lines are surfaced as errors.</span></li>
  </ol>
</section>
{% endblock %}"""

TEMPLATE_FILES["apps.html"] = r"""{% extends "base.html" %}
{% block title %}Apps · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head row-between">
  <div><p class="eyebrow">Apps</p><h1 class="display">Your AdMob apps</h1></div>
  <form method="post" action="/apps/sync"><button class="btn-secondary" type="submit">↻ Sync from AdMob</button></form>
</section>
{% if apps %}
<table class="table">
  <thead><tr><th>Name</th><th>Platform</th><th>AdMob App ID</th><th>Store ID</th><th>Ad units</th><th></th></tr></thead>
  <tbody>
    {% for app in apps %}
    <tr>
      <td>{{ app.name or "(unnamed)" }}</td>
      <td><span class="pill pill-{{ app.platform|lower }}">{{ app.platform }}</span></td>
      <td class="mono small">{{ app.app_id }}</td>
      <td class="mono small">{{ app.package_name or "—" }}</td>
      <td>{{ app.ad_units|length }}</td>
      <td><a href="/apps/{{ app.id }}">Open →</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}<div class="empty"><p>No apps cached yet. Click <strong>Sync from AdMob</strong> to pull them from the API.</p></div>{% endif %}
{% endblock %}"""

TEMPLATE_FILES["app_detail.html"] = r"""{% extends "base.html" %}
{% block title %}{{ app.name }} · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head">
  <p class="eyebrow"><a href="/apps">← Apps</a></p>
  <h1 class="display">{{ app.name or "(unnamed app)" }}</h1>
  <p class="mono small">{{ app.app_id }} · {{ app.platform }} · {{ app.package_name or "no store ID" }}</p>
</section>
<h2 class="section-title">Ad units</h2>
{% if ad_units %}
<table class="table">
  <thead><tr><th>Name</th><th>Format</th><th>Ad Unit ID</th></tr></thead>
  <tbody>
    {% for u in ad_units %}<tr><td>{{ u.name or "(unnamed)" }}</td><td><span class="pill">{{ u.ad_format }}</span></td><td class="mono small">{{ u.ad_unit_id }}</td></tr>{% endfor %}
  </tbody>
</table>
{% else %}<p class="empty">No ad units found for this app.</p>{% endif %}
{% endblock %}"""

TEMPLATE_FILES["mediation_list.html"] = r"""{% extends "base.html" %}
{% block title %}Mediation groups · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head row-between">
  <div><p class="eyebrow">Mediation</p><h1 class="display">Your mediation groups</h1></div>
  <a href="/mediation/builder" class="btn-primary">+ Open Builder</a>
</section>
{% if groups %}
<table class="table">
  <thead><tr><th>Name</th><th>Source Ad Unit</th><th>Format</th><th>Platform</th><th>Countries</th><th>Source eCPM</th><th>Status</th><th>Updated</th><th></th></tr></thead>
  <tbody>
    {% for g in groups %}
    <tr>
      <td>{{ g.name }}</td>
      <td class="mono small">{{ g.target_ad_unit_id }}</td>
      <td><span class="pill">{{ g.ad_format }}</span></td>
      <td><span class="pill pill-{{ g.platform|lower }}">{{ g.platform }}</span></td>
      <td class="small">{% if g.country_mode == "GLOBAL" %}Global{% elif g.country_mode == "INCLUDE" %}+{{ g.countries|length }}{% else %}−{{ g.countries|length }}{% endif %}</td>
      <td class="small">${{ "%.2f"|format(g.base_avg_ecpm) }}</td>
      <td><span class="status status-{{ g.status|lower }}">{{ g.status }}</span></td>
      <td class="small">{{ g.updated_at.strftime("%Y-%m-%d %H:%M") }}</td>
      <td><a href="/mediation/{{ g.id }}">Open →</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}<div class="empty"><p>No mediation groups yet. <a href="/mediation/builder">Open the Builder →</a></p></div>{% endif %}
{% endblock %}"""


TEMPLATE_FILES["mediation_builder.html"] = r"""{% extends "base.html" %}
{% block title %}Mediation Builder · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head">
  <p class="eyebrow">Builder</p>
  <h1 class="display">Mediation builder</h1>
  <p class="lede">Pick app + source ad units → choose targeting → fetch live AdMob report → push creates <strong>N tier ad units, replicated bidding mappings, and ONE mediation group per source ad unit</strong>. AdMob Network is the default — present even when no third-party bidding networks are configured.</p>
</section>

{% if not apps %}
<div class="empty">
  <p>You don't have any apps cached yet. Sync them first.</p>
  <form method="post" action="/apps/sync"><button class="btn-primary" type="submit">↻ Sync from AdMob</button></form>
</div>
{% else %}

<div class="builder-grid">
  <div>
    <fieldset class="builder-step">
      <legend><span class="num">01</span> Select App</legend>
      <select id="app-select">
        <option value="">— Choose an app —</option>
        {% for a in apps %}
          <option value="{{ a.id }}" data-platform="{{ a.platform }}">{{ a.name or a.app_id }} · {{ a.platform }} · {{ a.app_id }}</option>
        {% endfor %}
      </select>
    </fieldset>

    <fieldset class="builder-step" id="adunit-step" style="display:none">
      <legend><span class="num">02</span> Select Source Ad Units</legend>
      <div class="row-between" style="margin-bottom:10px">
        <input type="text" id="adunit-search" placeholder="Filter ad units…" />
        <div>
          <button type="button" class="btn-ghost btn-sm" id="adunit-all">Select all</button>
          <button type="button" class="btn-ghost btn-sm" id="adunit-none">Clear</button>
        </div>
      </div>
      <div id="adunit-list" class="adunit-cards"></div>
    </fieldset>

    <fieldset class="builder-step">
      <legend><span class="num">03</span> Country Targeting</legend>
      <div class="radio-row">
        <label><input type="radio" name="country_mode" value="GLOBAL" checked /> <span><b>Global</b> — target all countries</span></label>
        <label><input type="radio" name="country_mode" value="INCLUDE" /> <span><b>Choose</b> specific countries</span></label>
        <label><input type="radio" name="country_mode" value="EXCLUDE" /> <span><b>Exclude</b> specific countries (stored locally; AdMob API has no exclude field)</span></label>
      </div>
      <div id="country-picker" style="display:none">
        <input type="text" id="country-search" placeholder="Search countries…" />
        <div id="country-list" class="country-chips"></div>
        <p class="muted small">Or paste comma-separated ISO-2 codes:</p>
        <input type="text" id="country-paste" placeholder="US, GB, DE, JP" />
      </div>
    </fieldset>

    <fieldset class="builder-step">
      <legend><span class="num">04</span> Waterfall Depth &amp; Bidding</legend>
      <div class="grid grid-2">
        <label><span class="lbl">Number of tiers (1–{{ max_lines }})</span>
          <input type="number" id="line-count" min="1" max="{{ max_lines }}" value="{{ default_lines }}" /></label>
        <label><span class="lbl">Floor type (recorded locally)</span>
          <select id="floor-type">
            {% for f in floor_types %}<option value="{{ f }}">{{ f.replace("_"," ").title() }}</option>{% endfor %}
          </select></label>
      </div>
      <label class="check-row"><input type="checkbox" id="unique-names" checked /> Append random suffix to tier names</label>
      <label class="check-row"><input type="checkbox" id="use-internal-api" /> <span><b>AdMob internal API</b> — create real AdMob Network <b>waterfall</b> lines on the backend (uses your saved AdMob session)</span></label>
      <div id="internal-api-panel" style="display:none; margin-top:8px; padding:10px; border:1px solid var(--line); border-radius:8px">
        <div class="small" id="session-status" style="margin-bottom:6px"></div>
        <p class="muted small" style="margin:0 0 6px">Paste your AdMob cURL <b>once</b> — flow.py stores it encrypted and reuses it on every run. To get it: open <b>admob.google.com</b> → <b>F12</b> → <b>Network</b> tab → click any request whose URL contains <b>/rpc/</b> → right-click → Copy → <b>Copy as cURL (bash)</b>.</p>
        <textarea id="session-curl" rows="4" spellcheck="false" placeholder="curl 'https://admob.google.com/v2/...rpc...' -H '...' -b '...'" style="width:100%; font-family:monospace; font-size:11px"></textarea>
        <div style="margin-top:6px"><button type="button" class="btn-primary btn-sm" id="save-session-btn">Save AdMob session</button></div>
      </div>
      <div class="muted small" style="padding:6px 0">AdMob Network only mode — creates tier ad units + a mediation group with MANUAL AdMob Network waterfall lines + AdMob Network LIVE bidding line. No 3P bidding networks.</div>
      <div class="default-network-note">
        <strong>⚡ AdMob Network is always present.</strong> Every mediation group has N MANUAL AdMob Network waterfall lines (one per tier eCPM) plus an explicit AdMob Network LIVE bidding line. Third-party bidding networks are added when /networks credentials exist for them.
      </div>
      <label class="check-row"><span class="lbl">Name prefix</span>
        <input type="text" id="name-prefix" value="Global" /></label>
    </fieldset>

    <div class="form-actions">
      <button class="btn-primary" id="fetch-report-btn" disabled>📊 Fetch AdMob Report (Last 7 Days)</button>
      <a href="/mediation" class="btn-ghost">Cancel</a>
    </div>
  </div>

  <div>
    <div class="preview-panel" id="preview-panel">
      <p class="eyebrow">Live preview</p>
      <h3 class="section-title" style="margin-top:6px">Selected configuration</h3>
      <div id="preview-summary" class="muted">No app selected yet.</div>
    </div>
  </div>
</div>

<section id="report-section" style="display:none">
  <h2 class="section-title">Ad Unit Reports</h2>
  <p class="muted small" id="report-date-range"></p>
  <div id="report-cards"></div>
  <div class="calc-explainer">
    <strong>Waterfall Formula:</strong>
    Line 1 = avg eCPM × <span class="mono">{{ top_mult }}</span>,
    then each next line = previous × <span class="mono">{{ step_factor }}</span>.
  </div>
  <div class="form-actions">
    <button class="btn-primary btn-lg" id="push-btn">▶ Create mediation group + waterfall + bidding</button>
    <button class="btn-secondary btn-lg" id="generate-btn">Save locally only (preview/draft)</button>
  </div>

  <div class="form-actions" id="internal-actions" style="display:none">
    <button class="btn-primary btn-lg" id="internal-push-btn">▶ Create real AdMob Network waterfall (internal API)</button>
  </div>
  <div id="internal-result" class="muted small" style="display:none; white-space:pre-wrap; margin-top:10px; font-family:monospace; background:rgba(0,0,0,0.04); padding:10px; border-radius:8px"></div>

  <hr style="margin: 32px 0; border: 0; border-top: 1px solid var(--line)" />

  <h3 class="section-title" style="margin-top: 0">Advanced: Push lines to an existing AdMob group</h3>
  <p class="muted small" style="margin: 6px 0 14px">
    For Mediation Pro user-value-segment groups. Uses the eCPMs from the first selected ad unit's tier table.
  </p>

  <div class="form-actions" style="margin-bottom: 14px">
    <button type="button" class="btn-secondary btn-sm" id="fetch-admob-groups-btn">↻ Load my AdMob groups</button>
  </div>

  <div id="admob-groups-list" style="display:none">
    <label><span class="lbl">Pick the target group</span>
      <select id="target-group-select">
        <option value="">— Choose an AdMob group —</option>
      </select>
    </label>
    <p class="muted small" id="target-group-info" style="margin: 6px 0"></p>
    <div class="form-actions">
      <button type="button" class="btn-primary btn-lg" id="push-existing-btn" disabled>▶ Patch lines into selected group</button>
    </div>
  </div>

</section>

{% endif %}

<script>
const APP_AD_UNITS = {{ ad_units_by_app|tojson }};
const COUNTRIES = {{ countries|tojson }};
const MAX_LINES = {{ max_lines }};
const DEFAULT_LINES = {{ default_lines }};
const EXISTING_GROUPS = {{ existing_groups|tojson }};
const ADMOB_SESSION_SAVED = {{ 'true' if admob_session_saved else 'false' }};
const ADMOB_SESSION_AT = "{{ admob_session_at }}";

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];

const state = {
  app_id: null, app_label: "", platform: "",
  ad_units: [],
  country_mode: "GLOBAL",
  countries: new Set(),
  line_count: DEFAULT_LINES,
  floor_type: "{{ floor_types[0] }}",
  unique_names: true,
  include_bidding_networks: false,
  use_internal_api: false,
  name_prefix: "Global",
  report: null,
};

function updatePreview() {
  const el = $("#preview-summary");
  if (!state.app_id) { el.innerHTML = '<span class="muted">No app selected yet.</span>'; return; }
  const lines = [];
  lines.push(`<div class="kv"><span>App</span><b>${state.app_label || ""}</b></div>`);
  lines.push(`<div class="kv"><span>Source ad units</span><b>${state.ad_units.length}</b></div>`);
  let c;
  if (state.country_mode === "GLOBAL") c = "Global";
  else if (state.countries.size === 0) c = `${state.country_mode === "INCLUDE" ? "Choose" : "Exclude"} (none)`;
  else c = `${state.country_mode === "INCLUDE" ? "+" : "−"}${state.countries.size}: ${[...state.countries].join(", ")}`;
  lines.push(`<div class="kv"><span>Countries</span><b>${c}</b></div>`);
  lines.push(`<div class="kv"><span>Tiers per ad unit</span><b>${state.line_count}</b></div>`);
  lines.push(`<div class="kv"><span>Bidding</span><b>${state.include_bidding_networks ? "AdMob Network + replicated third-party" : "AdMob Network only (default)"}</b></div>`);
  const totalAdUnits = state.ad_units.length * state.line_count;
  lines.push(`<div class="kv"><span>Will create</span><b>${totalAdUnits} tier ad unit(s) + ${state.ad_units.length} mediation group(s)</b></div>`);
  lines.push(`<div class="kv"><span>Group targeting</span><b>source + ${state.line_count} tier ad units each</b></div>`);
  el.innerHTML = lines.join("");
  const btn = $("#fetch-report-btn");
  if (btn) btn.disabled = !(state.app_id && state.ad_units.length > 0);
}

function renderAdUnits() {
  const list = APP_AD_UNITS[state.app_id] || [];
  const wrap = $("#adunit-list");
  const filter = ($("#adunit-search").value || "").toLowerCase();
  wrap.innerHTML = "";
  const filtered = list.filter(u => !filter || (u.name||"").toLowerCase().includes(filter) || u.ad_unit_id.toLowerCase().includes(filter) || (u.ad_format||"").toLowerCase().includes(filter));
  if (!filtered.length) { wrap.innerHTML = '<p class="muted small">No ad units match.</p>'; return; }
  filtered.forEach(u => {
    const sel = state.ad_units.some(s => s.id === u.id);
    const card = document.createElement("div");
    card.className = "adunit-card" + (sel ? " is-selected" : "");
    const existing = EXISTING_GROUPS[u.ad_unit_id] || [];
    const existingInfo = existing.length
      ? `<div class="small muted" style="margin-top:6px">${existing.length} existing group(s): ` +
        existing.slice(0, 3).map(g => `<a href="/mediation/${g.id}" target="_blank">${g.name}</a>` + (g.admob_group_id ? ` <span class="pill pill-good">in AdMob</span>` : "")).join(", ") +
        (existing.length > 3 ? `, +${existing.length - 3} more` : "") +
        `</div>`
      : "";
    card.innerHTML = `<div><div class="adunit-name">${u.name || "(unnamed)"} <span class="pill">${u.ad_format}</span></div><div class="adunit-id mono small">${u.ad_unit_id}</div>${existingInfo}</div><button type="button" class="btn-ghost btn-sm">${sel ? "Selected ✓" : "Select"}</button>`;
    card.querySelector("button").addEventListener("click", () => {
      if (sel) state.ad_units = state.ad_units.filter(s => s.id !== u.id);
      else state.ad_units.push(u);
      renderAdUnits(); updatePreview();
    });
    wrap.appendChild(card);
  });
}

function renderCountries() {
  const filter = ($("#country-search").value || "").toLowerCase();
  const wrap = $("#country-list");
  wrap.innerHTML = "";
  COUNTRIES.filter(c => !filter || c.name.toLowerCase().includes(filter) || c.code.toLowerCase().includes(filter)).forEach(c => {
    const on = state.countries.has(c.code);
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "country-chip" + (on ? " is-selected" : "");
    chip.textContent = `${c.code} · ${c.name}`;
    chip.addEventListener("click", () => {
      if (on) state.countries.delete(c.code); else state.countries.add(c.code);
      renderCountries(); updatePreview();
    });
    wrap.appendChild(chip);
  });
}

$("#app-select").addEventListener("change", e => {
  state.app_id = e.target.value || null;
  const opt = e.target.options[e.target.selectedIndex];
  state.app_label = opt.textContent;
  state.platform = (opt.dataset.platform || "").toUpperCase();
  state.ad_units = [];
  $("#adunit-step").style.display = state.app_id ? "" : "none";
  if (state.app_id) renderAdUnits();
  updatePreview();
});
$("#adunit-search").addEventListener("input", renderAdUnits);
$("#adunit-all").addEventListener("click", () => { state.ad_units = [...(APP_AD_UNITS[state.app_id] || [])]; renderAdUnits(); updatePreview(); });
$("#adunit-none").addEventListener("click", () => { state.ad_units = []; renderAdUnits(); updatePreview(); });
$$('input[name="country_mode"]').forEach(r => r.addEventListener("change", () => {
  const mode = document.querySelector('input[name="country_mode"]:checked').value;
  state.country_mode = mode;
  $("#country-picker").style.display = mode === "GLOBAL" ? "none" : "";
  if (mode === "GLOBAL") state.countries.clear();
  if (mode !== "GLOBAL") renderCountries();
  updatePreview();
}));
$("#country-search").addEventListener("input", renderCountries);
$("#country-paste").addEventListener("change", e => {
  e.target.value.split(",").map(s => s.trim().toUpperCase()).filter(Boolean).forEach(c => state.countries.add(c));
  renderCountries(); updatePreview();
});
$("#line-count").addEventListener("change", e => { state.line_count = Math.max(1, Math.min(MAX_LINES, +e.target.value || DEFAULT_LINES)); updatePreview(); if (state.report) renderReport(); });
$("#floor-type").addEventListener("change", e => { state.floor_type = e.target.value; updatePreview(); });
$("#unique-names").addEventListener("change", e => { state.unique_names = e.target.checked; updatePreview(); });
$("#name-prefix").addEventListener("input", e => { state.name_prefix = e.target.value || "Group"; updatePreview(); });

$("#fetch-report-btn").addEventListener("click", async () => {
  const btn = $("#fetch-report-btn");
  btn.disabled = true; btn.textContent = "Fetching report…";
  try {
    const ad_unit_ids = state.ad_units.map(u => u.ad_unit_id);
    const res = await fetch("/mediation/builder/fetch-report", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ ad_unit_ids }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Fetch failed");
    state.report = data.report || {};
    $("#report-date-range").textContent = `Date range: ${data.start} → ${data.end} (last 7 days)`;
    renderReport();
    $("#report-section").style.display = "";
    $("#report-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    alert("Error fetching report: " + err.message);
  } finally {
    btn.disabled = false; btn.textContent = "📊 Fetch AdMob Report (Last 7 Days)";
  }
});

function fmtPct(n) { return (n * 100).toFixed(2) + "%"; }
function fmtUSD(n) { return "$" + (n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function computeLines(avg, count) {
  const TOP = {{ top_mult }}, STEP = {{ step_factor }};
  let v = avg * TOP, out = [];
  for (let i = 0; i < count; i++) { out.push(+v.toFixed(2)); v *= STEP; }
  return out;
}

function renderReport() {
  const wrap = $("#report-cards");
  wrap.innerHTML = "";
  state.ad_units.forEach(u => {
    const m = (state.report || {})[u.ad_unit_id] || {};
    const ecpm = m.ecpm_usd || 0;
    const lines = computeLines(ecpm, state.line_count);

    const card = document.createElement("div");
    card.className = "report-card";
    const planNote = `<div class="muted small" style="margin-top:8px">Will create <b>1</b> mediation group targeting this ad unit: <b>waterfall MANUAL lines</b> from your saved 3rd-party networks (one per network, at the computed tier eCPMs) + an <b>AdMob Network LIVE bidding line</b>. Save network credentials on the <b>/networks</b> page first — each saved network becomes one waterfall line.</div>`;

    card.innerHTML = `
      <div class="report-card-head">
        <div><div class="report-card-title">${u.name || "(unnamed)"}</div><div class="mono small muted">${u.ad_unit_id} · ${u.ad_format}</div></div>
        <div class="report-card-summary">Avg eCPM <b>${fmtUSD(ecpm)}</b> · Revenue <b>${fmtUSD(m.revenue_usd)}</b></div>
      </div>
      <div class="metric-grid">
        <div class="metric"><span class="metric-label">Avg eCPM</span><span class="metric-value">${fmtUSD(ecpm)}</span></div>
        <div class="metric"><span class="metric-label">Revenue</span><span class="metric-value good">${fmtUSD(m.revenue_usd)}</span></div>
        <div class="metric"><span class="metric-label">Match Rate</span><span class="metric-value">${fmtPct(m.match_rate || 0)}</span></div>
        <div class="metric"><span class="metric-label">Show Rate</span><span class="metric-value">${fmtPct(m.show_rate || 0)}</span></div>
        <div class="metric"><span class="metric-label">Fill Rate</span><span class="metric-value">${fmtPct(m.fill_rate || 0)}</span></div>
        <div class="metric"><span class="metric-label">RPM</span><span class="metric-value">${fmtUSD(m.rpm_usd)}</span></div>
        <div class="metric"><span class="metric-label">Requests</span><span class="metric-value">${(m.ad_requests||0).toLocaleString()}</span></div>
        <div class="metric"><span class="metric-label">Impressions</span><span class="metric-value">${(m.impressions||0).toLocaleString()}</span></div>
      </div>
      ${planNote}
      <div class="line-table">
        <div class="line-table-head"><div>Tier</div><div>eCPM (editable)</div><div>Source</div></div>
        ${lines.map((v, i) => `
          <div class="line-table-row">
            <div class="mono small">${i+1}</div>
            <input type="number" min="0" step="0.01" value="${v.toFixed(2)}" data-au="${u.ad_unit_id}" data-i="${i}" class="line-input" />
            <div class="small muted">AdMob Network (MANUAL)</div>
          </div>
        `).join("")}
      </div>
    `;
    wrap.appendChild(card);
  });
}

async function submitGroups(endpoint, label) {
    const btn = document.getElementById(label === "push" ? "push-btn" : "generate-btn");
    const items = state.ad_units.map(u => {
      const lineInputs = $$(`.line-input[data-au="${u.ad_unit_id}"]`);
      const lines = lineInputs.map(inp => +inp.value || 0);
      return {
        ad_unit_id: u.ad_unit_id, ad_unit_name: u.name, ad_format: u.ad_format,
        metrics: (state.report || {})[u.ad_unit_id] || {},
        lines,
      };
    });

    if (label === "push") {
      const totalLines = items.reduce((s, it) => s + it.lines.filter(l => l > 0).length, 0);
      const summary = `Pre-flight check:\n\n` +
        `• ${items.length} source ad unit(s) selected\n` +
        `• ${totalLines} waterfall tier eCPM(s) computed\n\n` +
        `Will create (via AdMob API), per source ad unit:\n` +
        `• 1 mediation group targeting that ad unit\n` +
        `• Waterfall MANUAL lines — one per 3rd-party network you saved\n` +
        `  on /networks, at the computed tier eCPMs\n` +
        `• 1 AdMob Network LIVE bidding line\n\n` +
        `If you have not saved any 3rd-party network credentials on the\n` +
        `/networks page, the group is still created but with 0 waterfall\n` +
        `lines (only the AdMob Network bidding line).\n\n` +
        `Continue?`;
      if (!confirm(summary)) return;
    }

    const body = {
      app_id: state.app_id, country_mode: state.country_mode,
      countries: [...state.countries], floor_type: state.floor_type,
      unique_names: state.unique_names, name_prefix: state.name_prefix,
      include_bidding_networks: state.include_bidding_networks,
      items,
    };
    btn.disabled = true; const orig = btn.textContent; btn.textContent = "Working...";
    try {
      const res = await fetch(endpoint, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");

      let msg;
      if (label === "push") {
        const groups = data.groups || [];
        const ok = groups.filter(g => g.status === "PUSHED").length;
        const partial = groups.filter(g => g.status === "PUSHED_PARTIAL").length;
        const failed = groups.filter(g => g.status === "PUSH_FAILED").length;
        const inAdMob = ok + partial;
        msg = `═══ PUSH RESULT ═══\n\n`;
        const okCount = groups.filter(g => g.status === "PUSHED").length;
        const partialCount = groups.filter(g => g.status === "PUSHED_PARTIAL").length;
        const failCount = groups.filter(g => g.status === "PUSH_FAILED").length;
        msg += `Mediation groups in AdMob: ${okCount}/${groups.length}`;
        if (partialCount) msg += ` (${partialCount} partial)`;
        msg += `\n`;
        if (failCount) msg += `✗ Failed entirely: ${failCount}\n`;
        msg += `\n`;
        groups.slice(0, 20).forEach(g => {
          const mark = g.status === "PUSHED" ? "✓"
                     : g.status === "PUSHED_PARTIAL" ? "⚠" : "✗";
          msg += `${mark} ${g.name}\n`;
          msg += `   source ad unit: ${g.source_ad_unit_id}\n`;
          if (g.admob_group_id)
            msg += `   AdMob mediation group id: ${g.admob_group_id}\n`;
          msg += `   Waterfall lines in group: ${g.waterfall_lines_actual ?? 0}`;
          const nets = g.waterfall_networks || [];
          if (nets.length) msg += `  (${nets.join(", ")})`;
          msg += `\n`;
          msg += `   Bidding line (AdMob Network): ${g.bidding_lines_actual ?? 0}\n`;
          const tiers = g.waterfall_tier_ecpms || [];
          if (tiers.length) {
            msg += `   Computed tier eCPMs: ` +
                   tiers.map(e => `$${Number(e).toFixed(2)}`).join(", ") + `\n`;
          }
          if ((g.waterfall_lines_actual ?? 0) === 0) {
            msg += `   ⚠ 0 waterfall lines — save 3rd-party network creds on /networks\n`;
          }
          msg += `\n`;
        });
        if (groups.length > 20) msg += `\n…and ${groups.length - 20} more (see /mediation)\n`;

        if ((data.push_errors || []).length) {
          msg += `\n═══ ERRORS ═══\n`;
          data.push_errors.slice(0, 15).forEach((e, i) => {
            const tierInfo = e.tier ? ` (tier ${e.tier})` : "";
            const stageInfo = e.stage ? ` at ${e.stage}` : "";
            msg += `\n${i+1}. ad unit ${e.ad_unit_id}${tierInfo}${stageInfo}:\n   ${e.error}\n`;
          });
          if (data.push_errors.length > 15) msg += `\n…and ${data.push_errors.length - 15} more errors\n`;
          const allErrs = data.push_errors.map(e => (e.error || "").toLowerCase()).join(" ");
          if (allErrs.includes("permission") || allErrs.includes("403")) {
            msg += `\n═══ HINT ═══\nAdMob Write API may not be enabled. Contact your AdMob account manager.`;
          } else if (allErrs.includes("exhausted") || allErrs.includes("quota")) {
            msg += `\n═══ HINT ═══\nQuota exhausted. Wait 60s and retry with fewer tiers / ad units.`;
          }
        }
      } else {
        msg = `Saved ${data.groups.length} draft group(s) locally (not pushed to AdMob).`;
      }
      alert(msg);
      window.location = "/mediation";
    } catch (err) {
      alert("Error: " + err.message);
      btn.disabled = false; btn.textContent = orig;
    }
}

document.getElementById("push-btn").addEventListener("click", () => submitGroups("/mediation/builder/push-to-admob", "push"));
document.getElementById("generate-btn").addEventListener("click", () => submitGroups("/mediation/builder/generate", "save"));

let LOADED_ADMOB_GROUPS = [];

document.getElementById("fetch-admob-groups-btn").addEventListener("click", async () => {
  const btn = document.getElementById("fetch-admob-groups-btn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Loading…";
  try {
    const res = await fetch("/mediation/builder/fetch-admob-groups", {method: "POST"});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load groups");
    LOADED_ADMOB_GROUPS = data.groups || [];
    const sel = document.getElementById("target-group-select");
    sel.innerHTML = '<option value="">— Choose an AdMob group —</option>';
    LOADED_ADMOB_GROUPS.forEach(g => {
      const opt = document.createElement("option");
      opt.value = g.mediation_group_id;
      opt.textContent = `${g.display_name || g.mediation_group_id} · ${g.platform || "?"} · ${g.format || "?"} · ${g.line_count} line(s) · ${g.state}`;
      sel.appendChild(opt);
    });
    document.getElementById("admob-groups-list").style.display = "";
    if (!LOADED_ADMOB_GROUPS.length) {
      document.getElementById("target-group-info").innerHTML = "<em>No groups returned.</em>";
    }
  } catch (err) {
    alert("Error loading AdMob groups: " + err.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});

document.getElementById("target-group-select").addEventListener("change", e => {
  const id = e.target.value;
  const pushBtn = document.getElementById("push-existing-btn");
  const info = document.getElementById("target-group-info");
  if (!id) { pushBtn.disabled = true; info.textContent = ""; return; }
  const g = LOADED_ADMOB_GROUPS.find(x => x.mediation_group_id === id);
  if (!g) { pushBtn.disabled = true; return; }
  pushBtn.disabled = false;
  info.innerHTML = `Currently has <b>${g.line_count}</b> line(s). Targets: ${(g.ad_unit_ids || []).map(x => `<code>${x}</code>`).join(", ") || "(none)"}.`;
});

document.getElementById("push-existing-btn").addEventListener("click", async () => {
  const sel = document.getElementById("target-group-select");
  const groupId = sel.value;
  if (!groupId) { alert("Pick a group first."); return; }
  const g = LOADED_ADMOB_GROUPS.find(x => x.mediation_group_id === groupId);
  if (!state.ad_units.length || !state.report) {
    alert("Select an ad unit and fetch the AdMob report first.");
    return;
  }
  const firstAu = state.ad_units[0];
  const inputs = $$(`.line-input[data-au="${firstAu.ad_unit_id}"]`);
  const ecpms = inputs.map(i => +i.value || 0).filter(v => v > 0);
  if (!ecpms.length) { alert("No positive eCPMs to push."); return; }
  if (!confirm(`Patch ${ecpms.length} line(s) into AdMob group "${g.display_name}"?`)) return;

  const btn = document.getElementById("push-existing-btn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Patching…";
  try {
    const res = await fetch("/mediation/builder/push-to-existing", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        mediation_group_id: groupId,
        ecpms,
        group_display_name: g.display_name || groupId,
      }),
    });
    const data = await res.json();
    if (data.status === "failed") {
      alert(`Failed to patch group.\n\nError: ${data.error}`);
    } else {
      const msg = data.status === "ok"
        ? `✓ Patched ${data.lines_pushed} of ${data.lines_requested} lines.`
        : `⚠ Partial: patched ${data.lines_pushed} of ${data.lines_requested} lines.`;
      alert(msg);
      window.location = "/mediation";
    }
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});

// ============================================================
// AdMob internal-API path — the backend creates real AdMob Network
// waterfall lines using the user's saved admob.google.com session.
// The checkbox reveals a session-paste panel + a push button.
// ============================================================
function refreshSessionStatus(saved, at) {
  const el = document.getElementById("session-status");
  if (!el) return;
  el.innerHTML = saved
    ? '<span class="pill pill-good">AdMob session saved</span> ' +
      (at ? '<span class="muted">updated ' + at + '</span>' : '')
    : '<span class="pill">No AdMob session saved yet</span>';
}
refreshSessionStatus(ADMOB_SESSION_SAVED, ADMOB_SESSION_AT);

const __useInternalApiCb = $("#use-internal-api");
if (__useInternalApiCb) {
  __useInternalApiCb.addEventListener("change", e => {
    state.use_internal_api = e.target.checked;
    const panel = document.getElementById("internal-api-panel");
    const acts = document.getElementById("internal-actions");
    if (panel) panel.style.display = e.target.checked ? "" : "none";
    if (acts) acts.style.display = e.target.checked ? "" : "none";
  });
}

const __saveSessionBtn = document.getElementById("save-session-btn");
if (__saveSessionBtn) {
  __saveSessionBtn.addEventListener("click", async () => {
    const curl = (document.getElementById("session-curl").value || "").trim();
    if (!curl) { alert("Paste your AdMob cURL first."); return; }
    __saveSessionBtn.disabled = true;
    const orig = __saveSessionBtn.textContent;
    __saveSessionBtn.textContent = "Saving...";
    try {
      const res = await fetch("/mediation/builder/save-admob-session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ curl }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      refreshSessionStatus(true, data.saved_at);
      document.getElementById("session-curl").value = "";
      alert("AdMob session saved (cookie length " + data.cookie_len + "). " +
            "It will be reused on every run until it expires.");
    } catch (err) {
      alert("Could not save session:\n\n" + err.message);
    } finally {
      __saveSessionBtn.disabled = false;
      __saveSessionBtn.textContent = orig;
    }
  });
}

const __internalPushBtn = document.getElementById("internal-push-btn");
if (__internalPushBtn) {
  __internalPushBtn.addEventListener("click", async () => {
    const items = state.ad_units.map(u => {
      const lines = $$(`.line-input[data-au="${u.ad_unit_id}"]`)
        .map(i => +i.value || 0);
      return {
        ad_unit_id: u.ad_unit_id, ad_unit_name: u.name,
        ad_format: u.ad_format,
        metrics: (state.report || {})[u.ad_unit_id] || {},
        lines,
      };
    });
    const total = items.reduce((s, it) => s + it.lines.filter(l => l > 0).length, 0);
    if (!items.length || !total) {
      alert("Select ad units and make sure their tier eCPMs are filled.");
      return;
    }
    if (!confirm("Create real AdMob Network waterfall groups via the internal API?\n\n" +
        "• " + items.length + " mediation group(s)\n" +
        "• " + total + " waterfall tier line(s) total\n\n" +
        "This writes directly to your AdMob account. Continue?")) return;
    __internalPushBtn.disabled = true;
    const orig = __internalPushBtn.textContent;
    __internalPushBtn.textContent = "Creating... (may take a minute)";
    const out = document.getElementById("internal-result");
    out.style.display = ""; out.textContent = "Working...";
    try {
      const res = await fetch("/mediation/builder/push-internal", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          app_id: state.app_id, name_prefix: state.name_prefix,
          unique_names: state.unique_names, items,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Request failed");
      let msg = "";
      (data.results || []).forEach(r => {
        msg += (r.ok ? "OK    " : "FAIL  ") + r.group + "\n";
        if (r.group_id) msg += "      AdMob group id: " + r.group_id + "\n";
        (r.log || []).forEach(l => { msg += "      " + l + "\n"; });
        if (r.error) msg += "      ERROR: " + r.error + "\n";
        msg += "\n";
      });
      if (data.session_expired) {
        msg += ">>> Your AdMob session expired. Paste a fresh cURL in the " +
               "panel above, save it, then retry.\n";
      }
      out.textContent = msg || "No results returned.";
      const okCount = (data.results || []).filter(r => r.ok).length;
      if (okCount && !data.session_expired) {
        alert(okCount + " group(s) created. Refresh AdMob → Mediation to confirm.");
      }
    } catch (err) {
      out.textContent = "Error: " + err.message;
    } finally {
      __internalPushBtn.disabled = false;
      __internalPushBtn.textContent = orig;
    }
  });
}

updatePreview();
</script>
{% endblock %}"""

TEMPLATE_FILES["mediation_detail.html"] = r"""{% extends "base.html" %}
{% block title %}{{ group.name }} · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head row-between">
  <div>
    <p class="eyebrow"><a href="/mediation">← Mediation</a></p>
    <h1 class="display">{{ group.name }}</h1>
    <p class="mono small">{{ group.ad_format }} · {{ group.platform }} ·
      {% if group.country_mode == "GLOBAL" %}global
      {% elif group.country_mode == "INCLUDE" %}include {{ group.countries|join(", ") }}
      {% else %}exclude {{ group.countries|join(", ") }}{% endif %}
      · floor: {{ group.floor_type.replace("_"," ").title() }}
      · ad unit: <code>{{ group.target_ad_unit_id }}</code>
      {% if group.admob_group_id %}· <strong style="color:var(--good)">AdMob group ID: <code>{{ group.admob_group_id }}</code></strong>{% endif %}
    </p>
  </div>
  <div class="actions-col">
    <span class="status status-{{ group.status|lower }}">{{ group.status }}</span>
    <a href="/mediation/{{ group.id }}/export.json" class="btn-secondary" target="_blank">Export JSON</a>
    <form method="post" action="/mediation/{{ group.id }}/delete" onsubmit="return confirm('Delete this group?');"><button type="submit" class="btn-danger">Delete</button></form>
  </div>
</section>
<h2 class="section-title">Report snapshot</h2>
{% if group.report_metrics %}
<div class="metric-grid">
  <div class="metric"><span class="metric-label">Source Avg eCPM</span><span class="metric-value">${{ "%.2f"|format(group.report_metrics.get("ecpm_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Revenue</span><span class="metric-value good">${{ "%.2f"|format(group.report_metrics.get("revenue_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Match Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("match_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">Show Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("show_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">Fill Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("fill_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">RPM</span><span class="metric-value">${{ "%.2f"|format(group.report_metrics.get("rpm_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Requests</span><span class="metric-value">{{ "{:,}".format(group.report_metrics.get("ad_requests", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Impressions</span><span class="metric-value">{{ "{:,}".format(group.report_metrics.get("impressions", 0)) }}</span></div>
</div>
{% else %}<p class="muted">No report snapshot saved.</p>{% endif %}
<h2 class="section-title">Waterfall lines</h2>
{% if group.waterfall_lines %}
<table class="table">
  <thead><tr><th>Tier</th><th>Line name</th><th>eCPM</th><th>Source</th><th>Mode</th><th>Tier ad unit ID</th><th>Enabled</th></tr></thead>
  <tbody>{% for line in group.waterfall_lines %}<tr><td>{{ line.priority + 1 }}</td><td>{{ line.line_name }}</td><td class="mono">${{ "%.2f"|format(line.ecpm_usd) }}</td><td>{{ line.network_code or "ADMOB" }}</td><td>{{ line.cpm_mode }}</td><td class="mono small">{{ line.admob_line_key or "—" }}</td><td>{{ "yes" if line.enabled else "no" }}</td></tr>{% endfor %}</tbody>
</table>
{% else %}<p class="muted">No waterfall lines.</p>{% endif %}
<div class="callout">
  <strong>Group structure:</strong> targets source ad unit + N tier ad units. Waterfall has N MANUAL AdMob Network lines (one per tier eCPM). Bidding has AdMob Network LIVE plus one LIVE line per replicated 3P network.
</div>
{% if group.last_push_response %}
<h2 class="section-title">Last push response (forensics)</h2>
<pre class="forensic-block">{{ group.last_push_response }}</pre>
{% endif %}
{% endblock %}"""

TEMPLATE_FILES["networks.html"] = r"""{% extends "base.html" %}
{% block title %}Networks · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head">
  <p class="eyebrow">3rd-party networks</p>
  <h1 class="display">Network credentials</h1>
  <p class="lede">Per-app credentials for each ad network. Stored encrypted. Used as documentation; the actual mappings are read live from AdMob during the push.</p>
</section>

{% if not apps %}
<div class="empty">
  <p>No apps cached yet. <form method="post" action="/apps/sync" style="display:inline"><button class="btn-primary" type="submit">↻ Sync from AdMob</button></form></p>
</div>
{% else %}

<div class="networks-page">
  <aside class="net-app-list">
    <h3>Apps</h3>
    {% for a in apps %}
      <a href="#app-{{ a.id }}" class="net-app-link">{{ a.name or a.app_id }}<br><span class="small mono muted">{{ a.platform }}</span></a>
    {% endfor %}
  </aside>

  <div class="net-app-content">
    {% for a in apps %}
    <section id="app-{{ a.id }}" class="net-app-section">
      <h2 class="section-title">{{ a.name or "(unnamed app)" }} <span class="pill pill-{{ a.platform|lower }}">{{ a.platform }}</span></h2>
      <p class="mono small muted">{{ a.app_id }}</p>

      <div class="net-tabs">
        {% for net in networks %}
          <button type="button" class="net-tab {% if loop.first %}is-active{% endif %}" data-tab="net-{{ a.id }}-{{ net.code }}">{{ net.name }}</button>
        {% endfor %}
      </div>

      {% for net in networks %}
      <div class="net-tab-content {% if loop.first %}is-active{% endif %}" id="net-{{ a.id }}-{{ net.code }}">
        <form method="post" action="/networks/{{ a.id }}/{{ net.code }}/save-app" class="net-form">
          <h4>App-level credentials</h4>
          {% if net.app_fields %}
            {% set app_creds = app_creds_by_app_net.get((a.id, net.code), {}) %}
            <div class="grid grid-2">
              {% for f in net.app_fields %}
                <label><span class="lbl">{{ f.label }}</span>
                  <input name="{{ f.key }}" type="{{ f.type }}" value="{{ app_creds.get(f.key, '') }}" placeholder="{{ f.help }}" />
                </label>
              {% endfor %}
            </div>
            <button class="btn-secondary btn-sm" type="submit">Save app credentials</button>
          {% else %}
            <p class="muted small">No app-level credentials needed for {{ net.name }}.</p>
          {% endif %}
        </form>

        <h4 style="margin-top:24px">Per ad unit ({{ a.ad_units|length }})</h4>
        {% if a.ad_units %}
          {% for u in a.ad_units %}
            {% set mapping = unit_creds_by_key.get((a.id, u.ad_unit_id, net.code), {}) %}
            <form method="post" action="/networks/{{ a.id }}/{{ net.code }}/save-unit/{{ u.ad_unit_id|urlencode }}" class="net-unit-row">
              <div class="net-unit-head">
                <div>
                  <div class="adunit-name">{{ u.name or "(unnamed)" }} <span class="pill">{{ u.ad_format }}</span></div>
                  <div class="mono small muted">{{ u.ad_unit_id }}</div>
                </div>
                {% if mapping.admob_mapping_id %}
                  <span class="status status-pushed">Mapped · {{ mapping.admob_mapping_id }}</span>
                {% endif %}
              </div>
              <div class="grid grid-2">
                {% for f in net.ad_unit_fields %}
                  <label><span class="lbl">{{ f.label }}</span>
                    <input name="{{ f.key }}" type="{{ f.type }}" value="{{ mapping.fields.get(f.key, '') if mapping else '' }}" placeholder="{{ f.help }}" />
                  </label>
                {% endfor %}
              </div>
              <button class="btn-secondary btn-sm" type="submit">Save</button>
            </form>
          {% endfor %}
        {% else %}
          <p class="muted small">No ad units cached for this app.</p>
        {% endif %}
      </div>
      {% endfor %}
    </section>
    {% endfor %}
  </div>
</div>

<script>
document.querySelectorAll(".net-tab").forEach(t => t.addEventListener("click", () => {
  const target = t.dataset.tab;
  const sec = t.closest(".net-app-section");
  sec.querySelectorAll(".net-tab").forEach(x => x.classList.toggle("is-active", x === t));
  sec.querySelectorAll(".net-tab-content").forEach(x => x.classList.toggle("is-active", x.id === target));
}));
</script>
{% endif %}
{% endblock %}"""

CSS_CONTENT = r""":root {
  --bg: #0e0d0b; --bg-2: #16140f; --bg-3: #1d1a14;
  --line: #2a261d; --line-2: #3a3326;
  --ink: #f1ecdf; --ink-dim: #b8b09d; --ink-mute: #847c69;
  --accent: #f4b942; --accent-2: #ef6f3c;
  --good: #7fb685; --bad: #e2705b;
  --font-display: "Fraunces", "Iowan Old Style", Charter, Georgia, serif;
  --font-body: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "JetBrains Mono", Menlo, monospace;
  --radius: 6px; --radius-lg: 10px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink); font-family: var(--font-body); font-size: 15px; line-height: 1.55; -webkit-font-smoothing: antialiased; }
body { background: radial-gradient(1200px 600px at 85% -10%, rgba(244,185,66,0.07), transparent 60%), radial-gradient(900px 500px at 10% 110%, rgba(239,111,60,0.05), transparent 55%), var(--bg); min-height: 100vh; }
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-2); }
code, .mono { font-family: var(--font-mono); }
.small { font-size: 12.5px; }
.muted { color: var(--ink-mute); }
.good { color: var(--good); }
.topbar { display: flex; align-items: center; justify-content: space-between; padding: 16px 36px; border-bottom: 1px solid var(--line); background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent); position: sticky; top: 0; z-index: 10; backdrop-filter: blur(8px); }
.brand-link { display: inline-flex; align-items: center; gap: 10px; color: var(--ink); }
.brand-mark { font-size: 22px; color: var(--accent); }
.brand-name { font-family: var(--font-display); font-weight: 600; font-size: 19px; letter-spacing: -0.01em; }
.brand-dot { color: var(--accent-2); }
.topnav { display: flex; align-items: center; gap: 18px; }
.topnav a { color: var(--ink-dim); font-size: 14px; }
.topnav a:hover { color: var(--ink); }
.topnav a.cta { color: var(--accent); }
.topnav .sep { width: 1px; height: 18px; background: var(--line); }
.user-chip { display: inline-flex; align-items: center; gap: 8px; color: var(--ink-mute); font-size: 13px; }
.user-chip img { width: 22px; height: 22px; border-radius: 50%; border: 1px solid var(--line-2); }
.topnav .logout { color: var(--ink-mute); font-size: 13px; }
.content { max-width: 1240px; margin: 0 auto; padding: 36px 36px 80px; }
.footer { max-width: 1240px; margin: 0 auto; padding: 24px 36px 40px; color: var(--ink-mute); font-size: 12.5px; }
.footer-sep { margin: 0 10px; opacity: 0.5; }
.page-head { margin-bottom: 28px; }
.row-between { display: flex; align-items: center; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
.eyebrow { font-family: var(--font-mono); font-size: 11.5px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); margin: 0 0 8px; }
.display { font-family: var(--font-display); font-weight: 400; font-size: clamp(28px, 4.2vw, 44px); line-height: 1.08; letter-spacing: -0.02em; margin: 0; }
.display em { font-style: italic; color: var(--accent); }
.lede { color: var(--ink-dim); max-width: 70ch; }
.section-title { font-family: var(--font-display); font-weight: 500; font-size: 22px; margin: 36px 0 14px; letter-spacing: -0.01em; }
.grid { display: grid; gap: 18px; }
.grid-2 { grid-template-columns: repeat(2, 1fr); }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
@media (max-width: 800px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }
.cta-row { display: flex; gap: 12px; margin: 18px 0 6px; flex-wrap: wrap; }
.card { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 22px 22px 18px; position: relative; }
.card-label { font-family: var(--font-mono); font-size: 11.5px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-mute); margin: 0 0 8px; }
.card-value { font-family: var(--font-display); font-size: 30px; margin: 0; letter-spacing: -0.01em; }
.card-value.mono { font-family: var(--font-mono); font-size: 16px; word-break: break-all; }
.card-link { display: inline-block; margin-top: 14px; font-size: 13px; }
.workflow-steps { list-style: none; padding: 0; margin: 12px 0 0; }
.workflow-steps li { display: flex; align-items: baseline; gap: 16px; padding: 10px 0; border-bottom: 1px dashed var(--line); }
.workflow-steps li:last-child { border-bottom: 0; }
.step-no { font-family: var(--font-mono); font-size: 12px; color: var(--accent); width: 36px; flex-shrink: 0; letter-spacing: 0.04em; }
.step-text { color: var(--ink-dim); }
.done { color: var(--good); margin-left: 8px; font-family: var(--font-mono); }
.btn-primary, .btn-secondary, .btn-ghost, .btn-danger { display: inline-flex; align-items: center; gap: 8px; font: 500 14px/1 var(--font-body); padding: 11px 18px; border-radius: var(--radius); border: 1px solid transparent; cursor: pointer; transition: all .15s ease; text-decoration: none; }
.btn-primary { background: var(--accent); color: #1a1407; border-color: var(--accent); }
.btn-primary:hover { background: var(--accent-2); border-color: var(--accent-2); color: #1a1407; }
.btn-primary.btn-lg { padding: 14px 22px; font-size: 15px; }
.btn-secondary { background: transparent; color: var(--ink); border-color: var(--line-2); }
.btn-secondary:hover { border-color: var(--ink-dim); }
.btn-ghost { background: transparent; color: var(--ink-dim); border-color: transparent; }
.btn-ghost:hover { color: var(--ink); border-color: var(--line); }
.btn-danger { background: transparent; color: var(--bad); border-color: var(--line); }
.btn-danger:hover { border-color: var(--bad); }
.btn-sm { padding: 6px 10px; font-size: 12.5px; }
button:disabled { opacity: .5; cursor: not-allowed; }
.g-mark { display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: #1a1407; color: var(--accent); font-family: var(--font-display); font-weight: 600; }
.login-wrap { display: grid; grid-template-columns: 1.4fr 0.8fr; gap: 60px; align-items: start; padding-top: 30px; }
@media (max-width: 900px) { .login-wrap { grid-template-columns: 1fr; gap: 30px; } }
.login-card .fineprint { color: var(--ink-mute); font-size: 12.5px; margin-top: 16px; }
.login-card .fineprint code { background: var(--bg-3); padding: 2px 6px; border-radius: 4px; color: var(--accent); }
.login-card .btn-primary { margin-top: 24px; padding: 14px 22px; font-size: 15px; }
.login-side { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 24px; }
.login-side h3 { font-family: var(--font-display); font-weight: 500; margin: 0 0 14px; font-size: 18px; }
.login-side ol { padding-left: 22px; color: var(--ink-dim); margin: 0; }
.login-side li { padding: 4px 0; }
.table { width: 100%; border-collapse: collapse; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); overflow: hidden; }
.table th, .table td { padding: 11px 14px; text-align: left; border-bottom: 1px solid var(--line); }
.table th { font-size: 11.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-mute); background: rgba(0,0,0,0.15); font-weight: 500; font-family: var(--font-mono); }
.table tr:last-child td { border-bottom: 0; }
.table tr:hover td { background: rgba(255,255,255,0.015); }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; background: var(--bg-3); border: 1px solid var(--line-2); font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.04em; color: var(--ink-dim); }
.pill-android { color: var(--good); border-color: rgba(127,182,133,0.3); }
.pill-ios { color: #9bb7e2; border-color: rgba(155,183,226,0.3); }
.pill-good { background: rgba(127,182,133,0.18); color: var(--good); }
.status { display: inline-block; padding: 3px 9px; border-radius: 4px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.05em; }
.status-draft { color: var(--ink-mute); background: rgba(132,124,105,0.12); }
.status-generated { color: var(--accent); background: rgba(244,185,66,0.12); }
.status-pushed { color: var(--good); background: rgba(127,182,133,0.16); }
.status-pushed_partial { color: var(--accent); background: rgba(244,185,66,0.16); }
.status-push_failed { color: var(--bad); background: rgba(226,112,91,0.14); }
label { display: flex; flex-direction: column; gap: 6px; }
.lbl { color: var(--ink-dim); font-size: 12.5px; }
input[type="text"], input[type="number"], input[type="password"], input:not([type]), select, textarea { background: var(--bg-3); color: var(--ink); border: 1px solid var(--line-2); border-radius: var(--radius); padding: 9px 12px; font: 400 14px/1.3 var(--font-body); }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(244,185,66,0.12); }
.builder-grid { display: grid; grid-template-columns: 1.4fr 0.8fr; gap: 28px; align-items: start; }
@media (max-width: 1000px) { .builder-grid { grid-template-columns: 1fr; } }
.builder-step { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 18px 20px; margin-bottom: 18px; }
.builder-step legend { padding: 0 6px; color: var(--ink-dim); font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; }
.builder-step legend .num { color: var(--accent); margin-right: 8px; }
.check-row { flex-direction: row; align-items: center; gap: 8px; margin-top: 12px; color: var(--ink-dim); font-size: 13px; }
.radio-row { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.radio-row label { flex-direction: row; align-items: center; gap: 10px; color: var(--ink-dim); }
.form-actions { display: flex; gap: 12px; align-items: center; padding-top: 4px; flex-wrap: wrap; }
.preview-panel { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 20px; position: sticky; top: 84px; }
.kv { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed var(--line); font-size: 13.5px; }
.kv:last-child { border-bottom: 0; }
.kv span { color: var(--ink-mute); }
.kv b { color: var(--ink); font-weight: 500; }
.adunit-cards { display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow-y: auto; padding-right: 6px; }
.adunit-card { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; background: rgba(0,0,0,0.18); border: 1px solid var(--line); border-radius: var(--radius); gap: 12px; }
.adunit-card.is-selected { border-color: var(--accent); background: rgba(244,185,66,0.06); }
.adunit-card .adunit-name { font-weight: 500; }
.adunit-card .adunit-id { color: var(--ink-mute); }
.country-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; max-height: 220px; overflow-y: auto; }
.country-chip { background: var(--bg-3); border: 1px solid var(--line-2); color: var(--ink-dim); padding: 6px 10px; border-radius: 999px; font-family: var(--font-mono); font-size: 12px; cursor: pointer; }
.country-chip:hover { border-color: var(--ink-mute); }
.country-chip.is-selected { background: rgba(244,185,66,0.16); color: var(--accent); border-color: var(--accent); }
.report-card { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 18px 20px; margin-bottom: 16px; }
.report-card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; margin-bottom: 14px; flex-wrap: wrap; }
.report-card-title { font-family: var(--font-display); font-size: 18px; font-weight: 500; }
.report-card-summary { color: var(--ink-dim); font-size: 13px; }
.metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
@media (max-width: 700px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
.metric { background: var(--bg-3); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 12px; }
.metric-label { display: block; font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mute); margin-bottom: 4px; }
.metric-value { font-family: var(--font-display); font-size: 20px; color: var(--accent); }
.metric-value.good { color: var(--good); }
.line-table { margin-top: 12px; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.line-table-head { display: grid; grid-template-columns: 50px 1fr 1.4fr; gap: 10px; padding: 8px 12px; background: rgba(0,0,0,0.18); font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mute); }
.line-table-row { display: grid; grid-template-columns: 50px 1fr 1.4fr; gap: 10px; padding: 8px 12px; border-top: 1px solid var(--line); align-items: center; }
.default-network-note { margin-top: 12px; padding: 10px 14px; background: rgba(244,185,66,0.07); border: 1px solid rgba(244,185,66,0.25); border-radius: var(--radius); color: var(--ink-dim); font-size: 13px; }
.networks-page { display: grid; grid-template-columns: 220px 1fr; gap: 28px; align-items: start; }
@media (max-width: 900px) { .networks-page { grid-template-columns: 1fr; } }
.net-app-list { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 16px; position: sticky; top: 84px; }
.net-app-list h3 { margin: 0 0 12px; font-family: var(--font-display); font-size: 16px; font-weight: 500; }
.net-app-link { display: block; padding: 10px 12px; border-radius: var(--radius); color: var(--ink-dim); margin-bottom: 6px; }
.net-app-link:hover { background: rgba(255,255,255,0.03); color: var(--ink); }
.net-app-section { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 22px; margin-bottom: 22px; }
.net-tabs { display: flex; gap: 4px; flex-wrap: wrap; margin: 20px 0 16px; border-bottom: 1px solid var(--line); }
.net-tab { background: transparent; color: var(--ink-mute); border: 0; border-bottom: 2px solid transparent; padding: 10px 14px; cursor: pointer; font: 500 13px/1 var(--font-body); }
.net-tab:hover { color: var(--ink); }
.net-tab.is-active { color: var(--accent); border-bottom-color: var(--accent); }
.net-tab-content { display: none; }
.net-tab-content.is-active { display: block; }
.net-tab-content h4 { font-family: var(--font-display); font-weight: 500; margin: 0 0 12px; font-size: 16px; }
.net-form { padding: 14px; background: rgba(0,0,0,0.18); border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 10px; }
.net-unit-row { padding: 12px 14px; background: rgba(0,0,0,0.12); border: 1px solid var(--line); border-radius: var(--radius); margin-bottom: 8px; }
.net-unit-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 10px; flex-wrap: wrap; }
.calc-explainer { background: var(--bg-3); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 16px; color: var(--ink-dim); font-size: 13px; margin: 18px 0; }
.callout { background: rgba(244,185,66,0.08); border: 1px solid rgba(244,185,66,0.25); border-radius: var(--radius); padding: 12px 16px; color: var(--ink-dim); font-size: 13px; margin-top: 24px; }
.empty { background: var(--bg-2); border: 1px dashed var(--line-2); border-radius: var(--radius-lg); padding: 40px; text-align: center; color: var(--ink-dim); }
.empty p { margin-top: 0; }
.alert { border-radius: var(--radius); padding: 12px 16px; margin-bottom: 20px; font-size: 14px; }
.alert-warn { background: rgba(244,185,66,0.07); border: 1px solid rgba(244,185,66,0.3); color: var(--accent); }
.actions-col { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.actions-col form { display: inline; }
.forensic-block { background: var(--bg-3); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px; font-family: var(--font-mono); font-size: 11.5px; color: var(--ink-dim); overflow-x: auto; max-height: 240px; }
"""


def write_assets() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in TEMPLATE_FILES.items():
        (TEMPLATES_DIR / name).write_text(body, encoding="utf-8")
    (STATIC_DIR / "style.css").write_text(CSS_CONTENT, encoding="utf-8")


# ============================================================================
# AUTO-MIGRATIONS (SQLite only)
# ============================================================================
def _auto_migrate_sqlite() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import OperationalError

    sql_type_map = {
        "INTEGER": "INTEGER", "VARCHAR": "TEXT", "TEXT": "TEXT",
        "FLOAT": "REAL", "REAL": "REAL", "BOOLEAN": "INTEGER",
        "DATETIME": "TEXT", "JSON": "TEXT",
    }
    with engine.connect() as conn:
        insp = sa_inspect(conn)
        existing_tables = set(insp.get_table_names())
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {c["name"] for c in insp.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_columns:
                    continue
                py_type_name = str(col.type).upper().split("(")[0]
                sql_type = sql_type_map.get(py_type_name, "TEXT")
                default_clause = ""
                default_val = col.default.arg if col.default is not None and not callable(getattr(col.default, "arg", None)) else None
                if isinstance(default_val, bool):
                    default_clause = f" DEFAULT {1 if default_val else 0}"
                elif isinstance(default_val, (int, float)):
                    default_clause = f" DEFAULT {default_val}"
                elif isinstance(default_val, str):
                    safe = default_val.replace("'", "''")
                    default_clause = f" DEFAULT '{safe}'"
                stmt = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {sql_type}{default_clause}'
                try:
                    conn.exec_driver_sql(stmt)
                    print(f"  [migrate] {table_name}: + {col.name} {sql_type}")
                except OperationalError as e:
                    print(f"  [migrate] FAILED on {table_name}.{col.name}: {e}")
        conn.commit()


# ============================================================================
# APP
# ============================================================================
write_assets()
Base.metadata.create_all(bind=engine)
_auto_migrate_sqlite()


BUILD_TAG = "waterfall-3p-networks-admob-bidding-v7"


@asynccontextmanager
async def lifespan(_: "FastAPI"):
    url = f"http://localhost:{settings.port}"
    try:
        mtime = datetime.fromtimestamp(
            Path(__file__).stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        mtime = "?"
    log_abs = str(_LOG_FILE.resolve())
    longest = max(len(url), len(log_abs)) + 30
    bar = "=" * longest
    print(flush=True)
    print(bar, flush=True)
    print(f"  >>  Open in browser:    {url}", flush=True)
    print(f"  >>  flow.py build:      {BUILD_TAG}", flush=True)
    print(f"  >>  flow.py modified:   {mtime}", flush=True)
    print(bar, flush=True)
    print(f"  >>  LIVE LOG FILE: {log_abs}", flush=True)
    print(f"  >>  Tail it in another PowerShell window with:", flush=True)
    print(f"  >>    Get-Content '{log_abs}' -Wait -Tail 50", flush=True)
    print(bar, flush=True)
    print(flush=True)
    _log(f"server ready (build={BUILD_TAG}, mtime={mtime})")
    _log(f"log file: {log_abs}")
    _log("when you click push in the browser, step-by-step logs will appear here AND in flow.log")
    yield


# NOTE: keep FastAPI's `debug` flag OFF even when settings.debug is True.
# When debug=True, Starlette's ServerErrorMiddleware intercepts unhandled
# exceptions and returns a plain-text traceback BEFORE our custom
# `_unhandled_to_json` handler can run — which makes the browser's
# fetch().json() blow up with "Unexpected token 'T', Traceback ...".
# Our handler below already prints the full traceback to the server console,
# so we're not losing any debug info.
app = FastAPI(title="AdMob Mediation Tool", debug=False, lifespan=lifespan)
# same_site="lax" lets the session cookie survive the Google -> /auth/callback
# top-level redirect; https_only=False is required for http://localhost dev.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ============================================================================
# GLOBAL JSON ERROR HANDLERS
# Make sure every error response is JSON so the builder's fetch().json()
# never chokes on an HTML traceback page with "Unexpected token T".
# Also log the full traceback to the server console for debugging.
# ============================================================================
import traceback as _tb_mod
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def _http_exc_to_json(request: Request, exc: StarletteHTTPException):
    # Builder POST endpoints expect JSON; redirect-style HTML pages would
    # break the UI. We keep HTML for normal GET routes by checking Accept.
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and request.method.upper() == "GET"
    if wants_html:
        # Fall back to default HTML behavior for HTML page requests
        from fastapi.responses import HTMLResponse as _H
        return _H(content=f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
                  status_code=exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code},
    )


@app.exception_handler(RequestValidationError)
async def _validation_exc_to_json(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": "Validation failed", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def _unhandled_to_json(request: Request, exc: Exception):
    # ALWAYS return JSON, even if our own logging fails. The old version
    # called _log() which writes to a file; if that file write somehow
    # raised, the handler itself crashed and Starlette fell back to the
    # plain "Internal Server Error" response that breaks the frontend's
    # fetch().json() call.
    try:
        tb = _tb_mod.format_exc()
    except Exception:
        tb = ""
    try:
        path = f"{request.method} {request.url.path}"
    except Exception:
        path = "<unknown>"
    try:
        _log(f"=== UNHANDLED EXCEPTION  {path} ===")
        for ln in tb.splitlines():
            _log(f"  {ln}")
        _log("=" * 50)
    except Exception:
        pass
    short_tb = ""
    try:
        lines = tb.strip().splitlines()
        short_tb = "\n".join(lines[-8:])
    except Exception:
        pass
    detail = "Internal error"
    try:
        detail = f"{type(exc).__name__}: {exc}"
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "traceback_tail": short_tb, "path": path},
    )



def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not signed in")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session invalid; sign in again")
    return user


def tmpl(request: Request) -> Jinja2Templates:
    return request.app.state.templates


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return tmpl(request).TemplateResponse("login.html", {"request": request})


# ============================================================================
# AUTH ROUTES
# ============================================================================
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.get("/login")
def login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth not configured.")
    auth_url, state, code_verifier = get_authorization_url()
    request.session["oauth_state"] = state
    request.session["code_verifier"] = code_verifier
    return RedirectResponse(auth_url)


@auth_router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None, db: Session = Depends(get_db)):
    # If Google bounced the user back with an explicit error param, surface it.
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")
    expected_state = request.session.get("oauth_state")
    if not code:
        raise HTTPException(status_code=400, detail="OAuth callback missing authorization code.")
    if not state:
        raise HTTPException(status_code=400, detail="OAuth callback missing state parameter.")
    if not expected_state:
        # The session cookie didn't make it back. Almost always a hostname or
        # SameSite issue. Tell the user what to check rather than a generic 400.
        raise HTTPException(
            status_code=400,
            detail=(
                "OAuth session expired or cookie missing. Make sure you reach the "
                "app on the same host as the OAuth redirect URI "
                f"({settings.google_redirect_uri}). If you opened the app on "
                "127.0.0.1 but the redirect URI uses localhost (or vice versa) "
                "the session cookie is dropped."
            ),
        )
    if state != expected_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch (possible CSRF or stale session).")
    flow = build_flow(state=state)
    code_verifier = request.session.get("code_verifier")
    if code_verifier:
        flow.code_verifier = code_verifier
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        # Surface the real reason (invalid_grant, redirect_uri_mismatch,
        # Warning: Scope has changed, etc.) instead of a generic 500.
        raise HTTPException(
            status_code=400,
            detail=f"Failed to exchange auth code for token: {type(e).__name__}: {e}",
        )
    creds = flow.credentials
    profile = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"}, timeout=15,
    ).json()
    sub = profile.get("sub")
    email = profile.get("email", "")
    if not sub:
        raise HTTPException(status_code=500, detail="Could not read Google profile.")
    user = db.query(User).filter(User.google_sub == sub).first()
    if user is None:
        user = User(google_sub=sub, email=email, name=profile.get("name", ""),
                    picture=profile.get("picture", ""),
                    admob_publisher_id=settings.admob_publisher_id or "")
        db.add(user); db.commit(); db.refresh(user)
    else:
        user.email = email
        user.name = profile.get("name", user.name)
        user.picture = profile.get("picture", user.picture)
        db.commit()
    persist_credentials(db, user, creds)
    request.session.update({"user_id": user.id, "user_email": user.email,
                            "user_name": user.name, "user_picture": user.picture})
    request.session.pop("oauth_state", None)
    request.session.pop("code_verifier", None)
    return RedirectResponse("/dashboard")


@auth_router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


# ============================================================================
# DASHBOARD
# ============================================================================
dash_router = APIRouter(tags=["dashboard"])


@dash_router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    publisher_id = user.admob_publisher_id
    api_error = None
    if not publisher_id:
        try:
            publisher_id = AdMobClient(db, user).get_publisher_id()
        except AdMobAPIError as e:
            api_error = str(e)
    app_count = db.query(AdMobApp).filter(AdMobApp.user_id == user.id).count()
    group_count = db.query(MediationGroup).filter(MediationGroup.user_id == user.id).count()
    return tmpl(request).TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        "publisher_id": publisher_id or "(not detected - click Sync Apps)",
        "app_count": app_count, "group_count": group_count, "api_error": api_error,
        "max_lines": WATERFALL_MAX_LINES,
    })


# ============================================================================
# APPS ROUTES
# ============================================================================
apps_router = APIRouter(prefix="/apps", tags=["apps"])


@apps_router.get("", response_class=HTMLResponse)
def list_apps_view(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    apps = db.query(AdMobApp).filter(AdMobApp.user_id == user.id).all()
    return tmpl(request).TemplateResponse("apps.html", {"request": request, "user": user, "apps": apps})


@apps_router.post("/sync")
def sync_apps(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        client = AdMobClient(db, user)
        api_apps = client.list_apps()
        api_ad_units = client.list_ad_units()
    except AdMobAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    existing = {a.app_id: a for a in db.query(AdMobApp).filter(AdMobApp.user_id == user.id).all()}
    now = datetime.utcnow()
    for api_app in api_apps:
        admob_id = api_app.get("appId", "")
        platform = api_app.get("platform", "ANDROID")
        details = api_app.get("manualAppInfo") or api_app.get("linkedAppInfo") or {}
        name = details.get("displayName", "") or api_app.get("name", "")
        pkg = (api_app.get("linkedAppInfo") or {}).get("appStoreId", "")
        row = existing.get(admob_id)
        if row is None:
            db.add(AdMobApp(user_id=user.id, app_id=admob_id, name=name,
                            platform=platform, package_name=pkg, last_synced_at=now))
        else:
            row.name = name or row.name
            row.platform = platform
            row.package_name = pkg or row.package_name
            row.last_synced_at = now
    db.commit()
    db_apps = {a.app_id: a for a in db.query(AdMobApp).filter(AdMobApp.user_id == user.id).all()}
    for app_row in db_apps.values():
        db.query(AdUnit).filter(AdUnit.app_id == app_row.id).delete()
    for unit in api_ad_units:
        parent = db_apps.get(unit.get("appId", ""))
        if parent is None:
            continue
        db.add(AdUnit(app_id=parent.id, ad_unit_id=unit.get("adUnitId", ""),
                      name=unit.get("displayName", "") or unit.get("name", ""),
                      ad_format=unit.get("adFormat", "BANNER"), last_synced_at=now))
    db.commit()
    return RedirectResponse("/apps", status_code=303)


@apps_router.get("/{db_app_id}", response_class=HTMLResponse)
def app_detail(db_app_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app_row = db.query(AdMobApp).filter(AdMobApp.id == db_app_id, AdMobApp.user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="App not found")
    return tmpl(request).TemplateResponse("app_detail.html", {
        "request": request, "user": user, "app": app_row, "ad_units": app_row.ad_units,
    })


# ============================================================================
# NETWORKS ROUTES
# ============================================================================
networks_router = APIRouter(prefix="/networks", tags=["networks"])


@networks_router.get("", response_class=HTMLResponse)
def networks_view(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    apps = db.query(AdMobApp).filter(AdMobApp.user_id == user.id).order_by(AdMobApp.name).all()
    app_creds: dict[tuple, dict] = {}
    for c in db.query(NetworkCredential).filter(NetworkCredential.user_id == user.id).all():
        app_creds[(c.app_id, c.network_code)] = decrypt_dict(c.encrypted_fields)
    unit_creds: dict[tuple, dict] = {}
    for m in db.query(AdUnitMapping).filter(AdUnitMapping.user_id == user.id).all():
        unit_creds[(m.app_id, m.ad_unit_id, m.network_code)] = {
            "fields": decrypt_dict(m.encrypted_fields),
            "admob_mapping_id": m.admob_mapping_id,
        }
    return tmpl(request).TemplateResponse("networks.html", {
        "request": request, "user": user,
        "apps": apps,
        "networks": [n for n in NETWORK_CATALOG if not n.get("internal_only")],
        "app_creds_by_app_net": app_creds,
        "unit_creds_by_key": unit_creds,
    })


@networks_router.post("/{app_pk}/{network_code}/save-app")
async def save_app_creds(
    app_pk: int, network_code: str, request: Request,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    cat = NETWORK_BY_CODE.get(network_code.upper())
    if not cat:
        raise HTTPException(status_code=404, detail="Unknown network")
    app_row = db.query(AdMobApp).filter(AdMobApp.id == app_pk, AdMobApp.user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="App not found")
    form = await request.form()
    fields = {f["key"]: (form.get(f["key"]) or "").strip() for f in cat["app_fields"]}
    cred = db.query(NetworkCredential).filter(
        NetworkCredential.user_id == user.id,
        NetworkCredential.app_id == app_pk,
        NetworkCredential.network_code == cat["code"],
    ).first()
    if cred is None:
        cred = NetworkCredential(user_id=user.id, app_id=app_pk, network_code=cat["code"])
        db.add(cred)
    cred.encrypted_fields = encrypt_dict(fields)
    db.commit()
    return RedirectResponse(f"/networks#app-{app_pk}", status_code=303)


@networks_router.post("/{app_pk}/{network_code}/save-unit/{ad_unit_id:path}")
async def save_unit_creds(
    app_pk: int, network_code: str, ad_unit_id: str, request: Request,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    cat = NETWORK_BY_CODE.get(network_code.upper())
    if not cat:
        raise HTTPException(status_code=404, detail="Unknown network")
    app_row = db.query(AdMobApp).filter(AdMobApp.id == app_pk, AdMobApp.user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="App not found")
    form = await request.form()
    fields = {f["key"]: (form.get(f["key"]) or "").strip() for f in cat["ad_unit_fields"]}
    mp = db.query(AdUnitMapping).filter(
        AdUnitMapping.user_id == user.id,
        AdUnitMapping.app_id == app_pk,
        AdUnitMapping.ad_unit_id == ad_unit_id,
        AdUnitMapping.network_code == cat["code"],
    ).first()
    has_value = any(fields.values())
    if mp is None and has_value:
        mp = AdUnitMapping(user_id=user.id, app_id=app_pk, ad_unit_id=ad_unit_id, network_code=cat["code"])
        db.add(mp)
    if mp is not None:
        existing = decrypt_dict(mp.encrypted_fields) if mp.encrypted_fields else {}
        if existing != fields:
            mp.admob_mapping_id = ""
            mp.admob_mapping_name = ""
        mp.encrypted_fields = encrypt_dict(fields)
        if not has_value and mp.id:
            db.delete(mp)
    db.commit()
    return RedirectResponse(f"/networks#app-{app_pk}", status_code=303)


# ============================================================================
# MEDIATION ROUTES
# ============================================================================
med_router = APIRouter(prefix="/mediation", tags=["mediation"])


@med_router.get("", response_class=HTMLResponse)
def list_groups(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    groups = db.query(MediationGroup).filter(MediationGroup.user_id == user.id).order_by(MediationGroup.updated_at.desc()).all()
    return tmpl(request).TemplateResponse("mediation_list.html", {"request": request, "user": user, "groups": groups})


@med_router.get("/builder", response_class=HTMLResponse)
def builder_view(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    apps = db.query(AdMobApp).filter(AdMobApp.user_id == user.id).order_by(AdMobApp.name).all()
    ad_units_by_app: dict[int, list[dict]] = {}
    for a in apps:
        ad_units_by_app[a.id] = [
            {"id": u.id, "ad_unit_id": u.ad_unit_id, "name": u.name, "ad_format": u.ad_format}
            for u in a.ad_units
        ]
    existing_groups: dict[str, list[dict]] = {}
    for g in db.query(MediationGroup).filter(MediationGroup.user_id == user.id).order_by(MediationGroup.created_at.desc()).all():
        if not g.target_ad_unit_id:
            continue
        existing_groups.setdefault(g.target_ad_unit_id, []).append({
            "id": g.id, "name": g.name, "status": g.status,
            "admob_group_id": g.admob_group_id or "",
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else "",
        })

    sess_row = db.query(AdMobSession).filter(
        AdMobSession.user_id == user.id).first()
    admob_session_saved = bool(sess_row and sess_row.encrypted_blob)
    admob_session_at = (sess_row.updated_at.strftime("%Y-%m-%d %H:%M")
                        if sess_row and sess_row.updated_at else "")

    return tmpl(request).TemplateResponse("mediation_builder.html", {
        "request": request, "user": user, "apps": apps,
        "ad_units_by_app": ad_units_by_app,
        "countries": COMMON_COUNTRIES,
        "max_lines": WATERFALL_MAX_LINES,
        "default_lines": WATERFALL_DEFAULT_LINES,
        "floor_types": FLOOR_TYPES,
        "top_mult": WATERFALL_TOP_MULTIPLIER,
        "step_factor": WATERFALL_STEP_FACTOR,
        "existing_groups": existing_groups,
        "admob_session_saved": admob_session_saved,
        "admob_session_at": admob_session_at,
    })


@med_router.post("/builder/fetch-report")
def builder_fetch_report(payload: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    ad_unit_ids = payload.get("ad_unit_ids") or []
    if not ad_unit_ids:
        raise HTTPException(status_code=400, detail="No ad unit IDs supplied")
    start, end = _days_ago_iso(6), _today_iso()
    try:
        client = AdMobClient(db, user)
        report = client.fetch_network_report_for_ad_units(ad_unit_ids, start, end)
    except AdMobAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    for au in ad_unit_ids:
        report.setdefault(au, {
            "ad_requests": 0, "matched_requests": 0, "impressions": 0, "clicks": 0,
            "revenue_usd": 0.0, "ecpm_usd": 0.0, "rpm_usd": 0.0,
            "match_rate": 0.0, "show_rate": 0.0, "fill_rate": 0.0, "ctr": 0.0,
        })
    return {"start": start, "end": end, "report": report}


@med_router.post("/builder/generate")
def builder_generate(payload: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _generate_groups(payload, db, user, push_to_admob=False)


@med_router.post("/builder/push-to-admob")
def builder_push_to_admob(payload: dict = Body(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _generate_groups(payload, db, user, push_to_admob=True)


@med_router.post("/builder/save-admob-session")
def builder_save_admob_session(payload: dict = Body(...),
                               db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Store (encrypted) the user's admob.google.com session, parsed from a
    pasted DevTools cURL. Used by the internal-API waterfall path."""
    parsed = parse_admob_curl(payload.get("curl") or "")
    if not parsed["cookie"]:
        raise HTTPException(
            status_code=400,
            detail="No cookie found. In admob.google.com DevTools -> Network, "
                   "right-click any request to a URL containing /rpc/ -> "
                   "Copy -> Copy as cURL (bash), then paste the whole thing.")
    if not parsed["xsrf"] or not parsed["f_sid"]:
        raise HTTPException(
            status_code=400,
            detail="The cURL is missing the x-framework-xsrf-token header or "
                   "the f.sid value. Copy a request that goes to a /rpc/ URL "
                   "(e.g. open a mediation group first), not a static asset.")
    row = db.query(AdMobSession).filter(AdMobSession.user_id == user.id).first()
    if not row:
        row = AdMobSession(user_id=user.id)
        db.add(row)
    row.encrypted_blob = encrypt_dict(parsed)
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "ok", "cookie_len": len(parsed["cookie"]),
            "saved_at": row.updated_at.strftime("%Y-%m-%d %H:%M")}


@med_router.post("/builder/push-internal")
def builder_push_internal(payload: dict = Body(...),
                          db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Create real AdMob Network waterfall mediation groups via the internal
    API, using the stored browser session."""
    row = db.query(AdMobSession).filter(AdMobSession.user_id == user.id).first()
    if not row or not row.encrypted_blob:
        raise HTTPException(status_code=400,
                            detail="No AdMob session saved. Paste your AdMob "
                                   "cURL in the builder first.")
    sess = decrypt_dict(row.encrypted_blob)
    if not sess.get("cookie"):
        raise HTTPException(status_code=400,
                            detail="Saved AdMob session is empty/undecryptable "
                                   "— paste a fresh cURL.")
    app_row = db.query(AdMobApp).filter(
        AdMobApp.id == int(payload.get("app_id") or 0),
        AdMobApp.user_id == user.id).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="App not found")
    items = payload.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No ad units selected")
    name_prefix = (payload.get("name_prefix") or "Group").strip().replace(" ", "_")
    unique_names = bool(payload.get("unique_names"))

    results: list[dict] = []
    session_expired = False
    _log(f"==== PUSH-INTERNAL START — {len(items)} ad unit(s) ====")
    for item in items:
        au_full = str(item.get("ad_unit_id") or "")
        au_name = str(item.get("ad_unit_name") or au_full)
        ad_format = str(item.get("ad_format") or "APP_OPEN")
        ecpms = sorted([float(x) for x in (item.get("lines") or [])
                        if float(x) > 0], reverse=True)
        if not au_full or not ecpms:
            continue
        suffix = f"_{_random_suffix()}" if unique_names else ""
        gname = f"{name_prefix}_{au_name}{suffix}".replace(" ", "_")[:80]
        _log(f"  internal-create group={gname!r} target={au_full} "
             f"tiers={len(ecpms)}")
        try:
            res = internal_create_waterfall_group(
                sess, group_name=gname, platform=app_row.platform,
                ad_format=ad_format, target_full_id=au_full, ecpms=ecpms,
                include_bidding=True)
        except AdMobInternalError as e:
            session_expired = True
            _log(f"  SESSION EXPIRED: {e}")
            results.append({"ad_unit_id": au_full, "group": gname,
                            "ok": False, "error": str(e), "log": []})
            break
        status = "PUSHED" if res.get("ok") else "PUSH_FAILED"
        grp = MediationGroup(
            user_id=user.id, name=gname, ad_format=ad_format,
            platform=app_row.platform, status=status,
            country_mode="GLOBAL", countries=[], floor_type=FLOOR_TYPES[0],
            target_ad_unit_id=au_full, target_ad_unit_name=au_name,
            base_avg_ecpm=ecpms[0], report_metrics=item.get("metrics") or {},
            admob_group_id=res.get("group_id", "") if res.get("ok") else "",
            last_push_response=json.dumps(res)[:4000])
        db.add(grp); db.commit(); db.refresh(grp)
        for i, ec in enumerate(ecpms, start=1):
            db.add(WaterfallLine(
                group_id=grp.id, priority=i - 1,
                line_name=f"AdMob Network Waterfall {i} — ${ec:.2f}",
                ecpm_usd=ec, enabled=bool(res.get("ok")),
                network_code="ADMOB", cpm_mode="MANUAL"))
        db.commit()
        results.append({"ad_unit_id": au_full, "group": gname,
                        "ok": bool(res.get("ok")),
                        "error": res.get("error", ""),
                        "group_id": res.get("group_id", ""),
                        "lines": res.get("lines", 0),
                        "log": res.get("log", []),
                        "response": res.get("response", "")})
    _log(f"==== PUSH-INTERNAL DONE — {sum(1 for r in results if r['ok'])}"
         f"/{len(results)} ok ====")
    return {"status": "ok", "results": results,
            "session_expired": session_expired}


@med_router.post("/builder/fetch-admob-groups")
def builder_fetch_admob_groups(db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        client = AdMobClient(db, user)
        groups = client.list_mediation_groups_in_admob()
        return {"groups": groups}
    except AdMobAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@med_router.post("/builder/push-to-existing")
def builder_push_to_existing(payload: dict = Body(...),
                              db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    group_id = str(payload.get("mediation_group_id") or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="mediation_group_id required")
    ecpms = [float(x) for x in (payload.get("ecpms") or []) if float(x) > 0]
    if not ecpms:
        raise HTTPException(status_code=400, detail="At least one positive eCPM required")
    display_name = str(payload.get("group_display_name") or f"Group {group_id}")
    try:
        client = AdMobClient(db, user)
        resp, lines_pushed = client.patch_lines_into_group(group_id, ecpms)
    except AdMobAPIError as e:
        return {
            "status": "failed", "error": str(e),
            "lines_requested": len(ecpms), "lines_pushed": 0,
        }
    local_group = MediationGroup(
        user_id=user.id,
        name=f"{display_name} (patched +{lines_pushed} lines)",
        ad_format="", platform="",
        status="PUSHED" if lines_pushed == len(ecpms) else "PUSHED_PARTIAL",
        country_mode="GLOBAL", countries=[],
        floor_type=FLOOR_TYPES[0],
        target_ad_unit_id="", target_ad_unit_name="",
        base_avg_ecpm=ecpms[0],
        report_metrics={},
        admob_group_id=group_id,
        admob_group_name=resp.get("name", "") or "",
        last_push_response=json.dumps(resp)[:4000],
    )
    db.add(local_group); db.commit(); db.refresh(local_group)
    for i, ecpm in enumerate(sorted(ecpms, reverse=True)[:lines_pushed]):
        db.add(WaterfallLine(
            group_id=local_group.id, priority=i,
            line_name=f"Line {i+1}", ecpm_usd=ecpm, enabled=True,
            network_code="ADMOB", cpm_mode="MANUAL",
        ))
    db.commit()
    return {
        "status": "ok" if lines_pushed == len(ecpms) else "partial",
        "lines_requested": len(ecpms),
        "lines_pushed": lines_pushed,
        "admob_group_id": group_id,
        "local_group_id": local_group.id,
    }


# ============================================================================
# Helper: ensure source ad unit has 3P bidding mappings created in AdMob
# ============================================================================
def _build_waterfall_lines_from_credentials(
    db: Session,
    client: "AdMobClient",
    user: User,
    app_row: "AdMobApp",
    source_ad_unit_id: str,
    ad_format: str,
    ecpms: list[float],
    push_errors: list[dict],
) -> tuple[list[dict], list[str]]:
    """Build MANUAL waterfall lines for the mediation group using the
    third-party networks the user saved credentials for on /networks.

    AdMob Network manual waterfall lines are not creatable via the API, so
    waterfall TIERS use 3rd-party networks (Meta / AppLovin / Unity /
    ironSource / Mintegral / Pangle). AdMob Network is added separately by
    the caller as the LIVE bidding line.

    For each network: combines app-level fields (NetworkCredential) with
    ad-unit-level fields (AdUnitMapping DB row), creates an AdUnitMapping
    on the source ad unit, and builds one MANUAL line. The computed eCPM
    tiers are assigned descending (highest eCPM = first network line).

    Returns (manual_line_dicts, network_names_used).
    """
    creds_rows = db.query(NetworkCredential).filter(
        NetworkCredential.user_id == user.id,
        NetworkCredential.app_id == app_row.id,
    ).all()
    # Fetch every AdUnitMapping row for this source ad unit once, indexed by
    # network_code — avoids a separate query per network credential below.
    unit_rows_by_code = {
        r.network_code: r
        for r in db.query(AdUnitMapping).filter(
            AdUnitMapping.user_id == user.id,
            AdUnitMapping.app_id == app_row.id,
            AdUnitMapping.ad_unit_id == source_ad_unit_id,
        ).all()
    }
    platform = (app_row.platform or "").upper()

    built: list[dict] = []
    pending = 0
    for cred in creds_rows:
        code = (cred.network_code or "").upper()
        cat = NETWORK_BY_CODE.get(code)
        if not cat or code == "ADMOB":
            continue

        app_fields = decrypt_dict(cred.encrypted_fields) if cred.encrypted_fields else {}
        unit_row = unit_rows_by_code.get(cred.network_code)
        unit_fields = (decrypt_dict(unit_row.encrypted_fields)
                       if unit_row and unit_row.encrypted_fields else {})
        user_fields = {**app_fields, **unit_fields}
        if not user_fields:
            _log(f"  {cat['name']}: no credentials saved — skipped")
            continue

        if pending > 0:
            time.sleep(1.2)
        pending += 1

        suffix = source_ad_unit_id.split("/")[-1][-8:]
        display_name = f"{cat['name']} waterfall {suffix}"[:80]
        _log(f"  creating AdUnitMapping for {cat['name']} on source ad unit")
        try:
            resp, warnings = client.create_ad_unit_mapping_in_admob(
                ad_unit_id=source_ad_unit_id,
                network_code=code,
                platform=platform,
                display_name=display_name,
                user_fields=user_fields,
            )
        except AdMobAPIError as e:
            _log(f"     {cat['name']} mapping FAILED: {e}")
            push_errors.append({
                "ad_unit_id": source_ad_unit_id, "tier": "",
                "stage": f"create_mapping({code})", "error": str(e),
            })
            continue

        mapping_name = resp.get("name", "") or ""
        source_id = client.find_source_id_for_network(code)
        if not mapping_name or not source_id:
            push_errors.append({
                "ad_unit_id": source_ad_unit_id, "tier": "",
                "stage": f"create_mapping({code})",
                "error": "mapping created but missing name/source id",
            })
            continue
        built.append({
            "network_code": code, "network_name": cat["name"],
            "source_id": source_id, "mapping_name": mapping_name,
        })
        _log(f"     {cat['name']} mapping OK")

    # Assign eCPM tiers descending — highest eCPM to the first network line.
    sorted_ecpms = sorted([e for e in ecpms if e and e > 0], reverse=True)
    manual_lines: list[dict] = []
    names_used: list[str] = []
    for i, b in enumerate(built):
        if i < len(sorted_ecpms):
            ecpm = sorted_ecpms[i]
        elif sorted_ecpms:
            ecpm = sorted_ecpms[-1]
        else:
            ecpm = 0.20
        cpm_micros = int(round(ecpm * 1_000_000))
        manual_lines.append({
            "displayName": f"{b['network_name']} - ${ecpm:.2f}"[:80],
            "adSourceId": b["source_id"],
            "cpmMode": "MANUAL",
            "cpmMicros": str(cpm_micros),
            "state": "ENABLED",
            "adUnitMappings": {source_ad_unit_id: b["mapping_name"]},
        })
        names_used.append(b["network_name"])
    return manual_lines, names_used


# ============================================================================
# Core builder: per source ad unit, create tier ad units + one mediation group
# ============================================================================
# ============================================================================
# AdMob INTERNAL API (admob.google.com/v2/...) — creates real AdMob Network
# waterfall lines. Auth = the user's browser session (cookie + xsrf token),
# captured once from a DevTools cURL and stored encrypted. NOT the public API.
# ============================================================================
_ALLOC_RPC = ("https://admob.google.com/v2/mediationAllocation/_/rpc/"
              "MediationAllocationService/V2Update")
_GROUP_RPC = ("https://admob.google.com/v2/mediationGroup/_/rpc/"
              "MediationGroupService/V2Update")

# ad_format -> (targeting code, line format code, {platform: adapter}, verified)
# Only APP_OPEN is confirmed from a real capture; others are best-guess.
_INTERNAL_FMT = {
    "APP_OPEN":              (7, 8, {"ANDROID": "616", "IOS": "617"}, True),
    "BANNER":                (0, 1, {"ANDROID": "504", "IOS": "505"}, False),
    "INTERSTITIAL":          (1, 1, {"ANDROID": "504", "IOS": "505"}, False),
    "REWARDED":              (5, 1, {"ANDROID": "506", "IOS": "507"}, False),
    "REWARDED_INTERSTITIAL": (6, 1, {"ANDROID": "510", "IOS": "511"}, False),
    "NATIVE":                (2, 1, {"ANDROID": "508", "IOS": "509"}, False),
}


class AdMobInternalError(Exception):
    """Raised when the internal-API session is invalid/expired."""


def parse_admob_curl(curl_text: str) -> dict:
    """Pull cookie, x-framework-xsrf-token and f.sid out of a DevTools
    'Copy as cURL' blob (bash or cmd style)."""
    import re
    text = curl_text or ""
    cookie = xsrf = f_sid = ""
    m = re.search(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", text, re.S)
    if m:
        cookie = m.group(2)
    for hm in re.finditer(r"-H\s+(['\"])(.*?)\1", text, re.S):
        h = hm.group(2)
        low = h.lower()
        if low.startswith("cookie:") and not cookie:
            cookie = h.split(":", 1)[1].strip()
        elif low.startswith("x-framework-xsrf-token:"):
            xsrf = h.split(":", 1)[1].strip()
    m = re.search(r"[?&]f\.sid=([^&'\"\s]+)", text)
    if m:
        f_sid = m.group(1)
    return {"cookie": cookie.strip(), "xsrf": xsrf.strip(), "f_sid": f_sid.strip()}


def _internal_post(sess: dict, url: str, activity: str, body_obj: dict) -> dict:
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "cookie": sess.get("cookie", ""),
        "x-framework-xsrf-token": sess.get("xsrf", ""),
        "x-same-domain": "1",
        "origin": "https://admob.google.com",
        "referer": "https://admob.google.com/v2/mediation/groups/list",
        "appname": "tlc",
        "activityname": activity,
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/146.0.0.0 Safari/537.36"),
    }
    body = "f.req=" + requests.utils.quote(
        json.dumps(body_obj, separators=(",", ":")), safe="")
    full = f"{url}?authuser=0&authuser=0&f.sid={sess.get('f_sid', '')}"
    resp = requests.post(full, data=body, headers=headers, timeout=45)
    return {"status": resp.status_code, "text": resp.text}


def _strip_xssi(text: str) -> str:
    t = (text or "").strip()
    if t.startswith(")]}'"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t[4:]
    return t.strip()


def _find_placement_id(parsed) -> str | None:
    """Locate the created backing-placement id — an object carrying the
    AdMob Network Waterfall source code "402" with a numeric id in field 1."""
    hit: list[str] = []

    def walk(o):
        if hit:
            return
        if isinstance(o, dict):
            v1 = o.get("1")
            if o.get("3") == "402" and isinstance(v1, str) and v1.isdigit() \
                    and v1 != "-1":
                hit.append(v1)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(parsed)
    return hit[0] if hit else None


def _find_group_id(parsed) -> str:
    def walk(o):
        if isinstance(o, dict):
            v = o.get("1")
            if isinstance(v, str) and v.isdigit() and len(v) >= 6:
                return v
            for x in o.values():
                g = walk(x)
                if g:
                    return g
        elif isinstance(o, list):
            for x in o:
                g = walk(x)
                if g:
                    return g
        return ""
    return walk(parsed) or ""


def internal_create_waterfall_group(sess: dict, *, group_name: str,
                                     platform: str, ad_format: str,
                                     target_full_id: str, ecpms: list,
                                     include_bidding: bool = True) -> dict:
    """Create ONE AdMob Network waterfall mediation group via the internal
    API: a backing placement per eCPM tier, then the group with one
    waterfall line per tier (+ AdMob Network bidding line). Raises
    AdMobInternalError when the session is expired (HTTP 401/403)."""
    fmt = _INTERNAL_FMT.get((ad_format or "").upper()) or _INTERNAL_FMT["APP_OPEN"]
    target_code, line_fmt, adapters, _verified = fmt
    plat_name = "IOS" if (platform or "").upper() == "IOS" else "ANDROID"
    plat_code = 1 if plat_name == "IOS" else 2
    adapter = adapters[plat_name]
    short = str(target_full_id).split("/")[-1]
    log: list[str] = []

    # Diagnostic — list existing AdMob Network Waterfall (source 402)
    # placements so the first run reveals the response shape + which
    # backing ad units ("pubid") are available.
    try:
        lr = _internal_post(
            sess, _ALLOC_RPC.replace("/V2Update", "/List"),
            "MediationGroup.AdSourcePlacementSettingsInit", {"1": ["402"]})
        log.append(f"List(402) HTTP {lr['status']}: {lr['text'][:700]}")
    except Exception as e:
        log.append(f"List(402) failed: {type(e).__name__}: {e}")

    # PHASE 1 — one backing placement per eCPM tier.
    placements: list[str] = []
    for i, ecpm in enumerate(ecpms, start=1):
        body = {"1": [{
            "1": "-1", "2": True, "3": "402",
            "4": [{"1": "pubid", "2": str(target_full_id)}],
            "10": line_fmt, "12": short, "16": adapter,
        }]}
        r = _internal_post(sess, _ALLOC_RPC,
                           "MediationGroup.CreateMappingInEditModal", body)
        if r["status"] in (401, 403):
            raise AdMobInternalError(f"session expired (HTTP {r['status']})")
        if r["status"] != 200:
            return {"ok": False, "log": log,
                    "error": f"placement {i}: HTTP {r['status']} — "
                             f"{r['text'][:600]}"}
        pid = None
        try:
            pid = _find_placement_id(json.loads(_strip_xssi(r["text"])))
        except Exception:
            pass
        if not pid:
            return {"ok": False, "log": log,
                    "error": f"placement {i}: id not found in response — "
                             f"{r['text'][:600]}"}
        log.append(f"placement {i} (${ecpm:.2f}) -> {pid}")
        placements.append(pid)

    # PHASE 2 — save the mediation group.
    lines = []
    if include_bidding:
        lines.append({"2": "1", "3": 1, "4": 1,
                      "5": {"1": "10000", "2": "USD"}, "6": False,
                      "9": "AdMob Network", "11": 1, "14": "1"})
    for ecpm, pid in zip(ecpms, placements):
        micros = str(int(round(float(ecpm) * 1_000_000)))
        lines.append({"2": "402", "3": line_fmt, "4": 2,
                      "5": {"1": micros, "2": "USD"},
                      "9": "AdMob Network Waterfall", "11": 1,
                      "13": [pid], "14": adapter})
    group = {"2": group_name, "3": 1,
             "4": {"1": plat_code, "2": target_code, "3": [short]},
             "5": lines, "10": {"1": 0}, "14": {}, "15": 0,
             "16": {"1": False}, "17": False}
    r = _internal_post(sess, _GROUP_RPC, "MediationGroup.Save", {"1": group})
    if r["status"] in (401, 403):
        raise AdMobInternalError(f"session expired (HTTP {r['status']})")
    if r["status"] != 200:
        return {"ok": False, "log": log,
                "error": f"group save: HTTP {r['status']} — {r['text'][:800]}"}
    gid = ""
    try:
        gid = _find_group_id(json.loads(_strip_xssi(r["text"])))
    except Exception:
        pass
    return {"ok": True, "group_id": gid, "lines": len(lines),
            "placements": placements, "log": log,
            "response": r["text"][:1000]}


def _generate_groups(payload: dict, db: Session, user: User, push_to_admob: bool):
    """For each selected source ad unit:
      1. Push the user's saved /networks creds to AdMob as AdUnitMappings on
         the source ad unit.
      2. Create N tier ad units.
      3. Replicate the source's 3P AdUnitMappings onto each tier.
      4. Build bidding lines (AdMob Network LIVE + each replicated 3P source).
      5. Create ONE mediation group (single API call) targeting source + all
         tiers, with N MANUAL AdMob Network waterfall lines + the bidding
         lines from step 4.
      6. Read the group back and report what AdMob persisted.
    """
    try:
        app_pk = int(payload.get("app_id") or 0)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid app_id")
    app_row = db.query(AdMobApp).filter(
        AdMobApp.id == app_pk, AdMobApp.user_id == user.id,
    ).first()
    if not app_row:
        raise HTTPException(status_code=404, detail="App not found")

    country_mode = (payload.get("country_mode") or "GLOBAL").upper()
    if country_mode not in ("GLOBAL", "INCLUDE", "EXCLUDE"):
        raise HTTPException(status_code=400, detail="Bad country_mode")
    countries = [str(c).upper() for c in (payload.get("countries") or [])]
    floor_type = payload.get("floor_type") or FLOOR_TYPES[0]
    if floor_type not in FLOOR_TYPES:
        raise HTTPException(status_code=400, detail="Bad floor_type")
    unique_names = bool(payload.get("unique_names"))
    name_prefix = (payload.get("name_prefix") or "Group").strip().replace(" ", "_")
    # AdMob Network only: tier ad units + one group with MANUAL AdMob lines
    # + AdMob Network LIVE bidding. The 3P bidding-replication path is
    # disabled — ignore whatever the UI sends.
    include_bidding = False
    items = payload.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No ad units selected")

    overall_start = time.time()
    mode = "PUSH" if push_to_admob else "GENERATE (no AdMob writes)"
    _log(f"==== {mode} START — {len(items)} source ad unit(s) ====")

    if push_to_admob:
        _log("Initializing AdMob client (OAuth refresh if needed) ...")
    client = AdMobClient(db, user) if push_to_admob else None
    if push_to_admob:
        _log("AdMob client ready.")
    created: list[dict] = []
    push_errors: list[dict] = []
    push_countries = countries if country_mode == "INCLUDE" else []

    for item_idx, item in enumerate(items, start=1):
        ad_unit_id = str(item.get("ad_unit_id") or "")
        if not ad_unit_id:
            continue
        ad_unit_name = str(item.get("ad_unit_name") or ad_unit_id)
        ad_format = str(item.get("ad_format") or "BANNER")
        metrics = item.get("metrics") or {}
        ecpms = [float(x) for x in (item.get("lines") or []) if float(x) > 0]
        if not ecpms:
            continue
        _log(f"---- [{item_idx}/{len(items)}] source ad_unit={ad_unit_id} "
             f"name={ad_unit_name!r} tiers={len(ecpms)} ----")

        # ====================================================================
        # Build the mediation group:
        #   - Waterfall MANUAL lines  -> 3rd-party networks (from /networks
        #     credentials). AdMob Network manual waterfall lines are not
        #     API-creatable, so tiers use Meta / AppLovin / Unity / etc.
        #   - Bidding LIVE line       -> AdMob Network (always added).
        # All in ONE mediationGroups.create call, targeting the source ad
        # unit.
        # ====================================================================
        group_suffix = f"_{_random_suffix()}" if unique_names else ""
        group_display_name = (
            f"{name_prefix}_{ad_unit_name}{group_suffix}"
            .replace(" ", "_")[:80]
        )

        admob_group_id = ""
        admob_group_name = ""
        live_actual = 0
        manual_actual = 0
        group_error_log = ""
        waterfall_networks: list[str] = []

        if push_to_admob and client is not None:
            # Waterfall MANUAL lines from saved 3rd-party network creds.
            _log("Building waterfall lines from saved /networks credentials")
            manual_lines, waterfall_networks = _build_waterfall_lines_from_credentials(
                db=db, client=client, user=user, app_row=app_row,
                source_ad_unit_id=ad_unit_id, ad_format=ad_format,
                ecpms=ecpms, push_errors=push_errors,
            )
            _log(f"  built {len(manual_lines)} waterfall MANUAL line(s): "
                 f"{', '.join(waterfall_networks) or 'none'}")

            # AdMob Network LIVE bidding line — always present.
            try:
                admob_src_id = client.get_admob_network_source_id()
            except AdMobAPIError:
                admob_src_id = AdMobClient.ADMOB_NETWORK_SOURCE_ID
            bidding_lines = [{
                "displayName": "AdMob Network",
                "adSourceId": admob_src_id,
                "cpmMode": "LIVE",
                "state": "ENABLED",
            }]

            _log(f"Creating mediation group name={group_display_name!r} "
                 f"targeting {ad_unit_id} — "
                 f"{len(manual_lines)} waterfall line(s) + "
                 f"1 AdMob Network bidding line")
            try:
                mg_resp, manual_actual, live_actual, _m_req = _timed(
                    "create_mediation_group_in_admob",
                    lambda: client.create_mediation_group_in_admob(
                        display_name=group_display_name,
                        platform=app_row.platform,
                        ad_format=ad_format,
                        targeting_ad_unit_ids=[ad_unit_id],
                        country_codes=push_countries,
                        manual_lines=manual_lines,
                        bidding_lines=bidding_lines,
                    ),
                )
                admob_group_id = mg_resp.get("mediationGroupId", "") or ""
                admob_group_name = mg_resp.get("name", "") or ""
                _log(f"     group created id={admob_group_id} "
                     f"waterfall_lines={manual_actual} bidding_lines={live_actual}")
            except AdMobAPIError as e:
                _log(f"create_mediation_group FAILED: {e}")
                push_errors.append({
                    "ad_unit_id": ad_unit_id, "tier": "",
                    "stage": "create_mediation_group", "error": str(e),
                })
                group_error_log = f"[create_mediation_group] {e}"

        if not push_to_admob:
            group_status = "GENERATED"
        elif admob_group_id:
            group_status = "PUSHED"
        else:
            group_status = "PUSH_FAILED"

        api_response_summary = json.dumps({
            "admob_group_id": admob_group_id,
            "admob_group_name": admob_group_name,
            "waterfall_lines_in_group": manual_actual,
            "bidding_lines_in_group": live_actual,
            "waterfall_networks": waterfall_networks,
            "waterfall_tier_ecpms": ecpms,
        })[:4000]

        # ====================================================================
        # Persist locally.
        # ====================================================================
        group = MediationGroup(
            user_id=user.id,
            name=group_display_name,
            ad_format=ad_format,
            platform=app_row.platform,
            status=group_status,
            country_mode=country_mode,
            countries=countries,
            floor_type=floor_type,
            target_ad_unit_id=ad_unit_id,
            target_ad_unit_name=ad_unit_name,
            base_avg_ecpm=(metrics.get("ecpm_usd") or (ecpms[0] if ecpms else 0.0)),
            report_metrics=metrics,
            admob_group_id=admob_group_id,
            admob_group_name=admob_group_name,
            last_push_response=(api_response_summary or group_error_log)[:4000],
        )
        db.add(group); db.commit(); db.refresh(group)
        sorted_ecpms = sorted([e for e in ecpms if e and e > 0], reverse=True)
        for i, ecpm in enumerate(sorted_ecpms, start=1):
            net = (waterfall_networks[i - 1]
                   if i - 1 < len(waterfall_networks) else "(needs network)")
            db.add(WaterfallLine(
                group_id=group.id,
                priority=i - 1,
                line_name=f"Waterfall line {i} — {net} — ${ecpm:.2f}",
                ecpm_usd=ecpm,
                enabled=(i - 1 < len(waterfall_networks)),
                network_code=net,
                cpm_mode="MANUAL",
            ))
        db.add(WaterfallLine(
            group_id=group.id, priority=99,
            line_name="AdMob Network (bidding)",
            ecpm_usd=0.0, enabled=bool(admob_group_id),
            network_code="ADMOB", cpm_mode="LIVE",
        ))
        db.commit()

        created.append({
            "id": group.id,
            "name": group_display_name,
            "source_ad_unit_id": ad_unit_id,
            "admob_group_id": admob_group_id,
            "waterfall_lines_actual": manual_actual,
            "bidding_lines_actual": live_actual,
            "waterfall_networks": waterfall_networks,
            "waterfall_tier_ecpms": ecpms,
            "status": group_status,
        })
        _log(f"     item DONE status={group_status} "
             f"admob_group_id={admob_group_id or '-'} "
             f"waterfall_lines={manual_actual} bidding_lines={live_actual}")

    total_elapsed = time.time() - overall_start
    _log(f"==== {mode} DONE in {total_elapsed:.2f}s — "
         f"{len(created)} group plan(s), {len(push_errors)} error(s) ====")
    return {"status": "ok", "groups": created,
            "push_errors": push_errors, "pushed": push_to_admob}


@med_router.get("/{group_id}", response_class=HTMLResponse)
def show_group(group_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    group = db.query(MediationGroup).filter(MediationGroup.id == group_id, MediationGroup.user_id == user.id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return tmpl(request).TemplateResponse("mediation_detail.html", {"request": request, "user": user, "group": group})


@med_router.post("/{group_id}/delete")
def delete_group(group_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    group = db.query(MediationGroup).filter(MediationGroup.id == group_id, MediationGroup.user_id == user.id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    db.delete(group); db.commit()
    return RedirectResponse("/mediation", status_code=303)


@med_router.get("/{group_id}/export.json")
def export_group(group_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    group = db.query(MediationGroup).filter(MediationGroup.id == group_id, MediationGroup.user_id == user.id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return JSONResponse(AdMobClient(db, user).export_group_config(group))


app.include_router(auth_router)
app.include_router(dash_router)
app.include_router(apps_router)
app.include_router(networks_router)
app.include_router(med_router)


def _free_port_if_stuck(port: int) -> None:
    """If `port` is held by a stale Python process from a previous server
    run, kill that process so we can bind. Avoids the WinError 10048
    "only one usage of each socket address" error when the user restarts
    immediately after Ctrl+C while a long-running request was in flight
    (the underlying process keeps the socket bound until the request's
    AdMob calls finish).

    Only kills python.exe — never touches other processes — so this is
    safe to run unconditionally on startup.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return  # port is free; nothing to do
    except OSError:
        pass
    finally:
        try:
            probe.close()
        except Exception:
            pass

    if os.name != "nt":
        print(f"[startup] Port {port} is in use. Free it manually and retry.")
        return

    import subprocess
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[startup] Port {port} in use but couldn't run netstat: {e}")
        return

    pids: set[int] = set()
    needle = f":{port}"
    for line in out.splitlines():
        # Lines look like: TCP    127.0.0.1:8000   0.0.0.0:0   LISTENING   12345
        if needle not in line or "LISTENING" not in line.upper():
            continue
        # The local address column must end with :PORT (avoid matching
        # connections where the remote port happens to be the same).
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        if not local.endswith(f":{port}"):
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            pass

    my_pid = os.getpid()
    killed_any = False
    for pid in pids:
        if pid == my_pid:
            continue
        # Confirm it's a python process before killing — never kill
        # something unrelated that happens to be on this port.
        try:
            info = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except Exception:
            info = ""
        if "python" not in info.lower():
            print(f"[startup] Port {port} held by non-python PID {pid}; "
                  "not killing. Free it manually if you want to use this port.")
            continue
        print(f"[startup] Port {port} held by stale python PID {pid} — killing.")
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=False, capture_output=True,
            )
            killed_any = True
        except Exception as e:
            print(f"[startup] Failed to kill PID {pid}: {e}")

    if killed_any:
        # Give Windows a moment to release the socket fully.
        time.sleep(1.0)


if __name__ == "__main__":
    # Auto-free port from a stale previous run before binding.
    _free_port_if_stuck(settings.port)

    # NOTE: uvicorn's `reload=True` spawns a watcher subprocess that, on
    # Windows + Python 3.13, re-imports the whole module via
    # multiprocessing.spawn. That re-import triggers SQLAlchemy's
    # `platform.win32_ver()` (a slow WMI query) and any Ctrl+C / startup
    # race during that window produces a giant traceback, even though the
    # main server has already started fine. We default to no-reload and
    # let it be opted in explicitly via UVICORN_RELOAD=1 — restart by hand
    # after code changes.
    reload_enabled = os.environ.get("UVICORN_RELOAD", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "flow:app",
        host=settings.host,
        port=settings.port,
        reload=reload_enabled,
    )