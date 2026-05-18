"""
AdMob Mediation Tool — single-file FastAPI app.

ADDITIONAL DEPENDENCY (install before running):
    pip install cryptography

Workflow:
  1. Sign in with Google (OAuth 2.0 + PKCE + admob.monetization scope)
  2. Sync apps + ad units from AdMob (real API)
  3. Open /networks to enter your 3rd-party network credentials per app
     (Meta, AppLovin, Unity, ironSource, Mintegral, Pangle) — stored encrypted.
  4. Open the mediation builder:
       - pick an app
       - select one or more ad units
       - choose country targeting (Global / Choose / Exclude)
       - set number of waterfall lines (1-20)
       - choose floor type
       - toggle bidding ON/OFF (default OFF)
       - fetch last-7-days AdMob report per ad unit
       - tool calculates waterfall eCPM values
       - assign one ad network per waterfall line via dropdown
       - click Create in AdMob → tool creates ad unit mappings + multi-line
         mediation group via AdMob v1beta API.
  5. Manage saved mediation groups.
"""
from __future__ import annotations

import json
import os
import random
import string
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Tuple

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
    host: str = "127.0.0.1"
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
# WATERFALL FORMULA  (tweak constants to change calculation)
# ============================================================================
WATERFALL_TOP_MULTIPLIER = 1.91
WATERFALL_STEP_FACTOR = 0.80
WATERFALL_DEFAULT_LINES = 5
WATERFALL_MAX_LINES = 20


def compute_waterfall_lines(avg_ecpm: float, count: int) -> list[float]:
    if avg_ecpm <= 0 or count <= 0:
        return [0.0] * max(0, count)
    lines: list[float] = []
    value = avg_ecpm * WATERFALL_TOP_MULTIPLIER
    for _ in range(count):
        lines.append(round(value, 2))
        value = value * WATERFALL_STEP_FACTOR
    return lines


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
# Each entry describes what credentials a network needs, AdMob's known adSourceId,
# and whether the network supports bidding (cpmMode=BIDDING) inside AdMob.
#
# The `app_fields` are stored once per (user, app, network).
# The `ad_unit_fields` are stored once per (user, app, network, ad_unit).
# Field type "text" / "password" controls input rendering only.
#
# AdMob adSourceIds are documented at:
#   https://developers.google.com/admob/api/v1beta/rest/v1beta/accounts.adSources
NETWORK_CATALOG = [
    {
        "code": "ADMOB",
        "name": "AdMob Network",
        "admob_source_id": "5450213213286189855",
        "supports_bidding": False,
        "app_fields": [],
        "ad_unit_fields": [],
        "internal_only": True,  # not shown in per-line dropdown; auto-handled
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
def _get_fernet():
    """Derive a Fernet key from SECRET_KEY. Done lazily so the import doesn't
    fail if cryptography isn't installed for a user only using read endpoints."""
    import base64, hashlib
    from cryptography.fernet import Fernet
    raw = (settings.secret_key or "change-me-in-env").encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


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
    admob_group_id = Column(String(64), default="")           # Set after successful push to AdMob
    admob_group_name = Column(String(255), default="")        # Full resource name from AdMob
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
    network_code = Column(String(32), default="")          # which 3rd-party network
    cpm_mode = Column(String(16), default="MANUAL")        # MANUAL | BIDDING
    admob_line_key = Column(String(64), default="")        # the negative key sent to API
    group = relationship("MediationGroup", back_populates="waterfall_lines")


class NetworkCredential(Base):
    """One row per (user, app, network). Holds APP-level fields as encrypted JSON.
    Ad-unit-level fields are in AdUnitMapping rows instead."""
    __tablename__ = "network_credentials"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    app_id = Column(Integer, ForeignKey("admob_apps.id"), nullable=False)
    network_code = Column(String(32), nullable=False)      # META | APPLOVIN | ...
    encrypted_fields = Column(Text, default="")            # Fernet-encrypted JSON
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdUnitMapping(Base):
    """Per (user, app, ad_unit, network) credential block. Also caches AdMob's
    returned mapping resource name so we don't recreate the mapping for every
    mediation group push."""
    __tablename__ = "ad_unit_network_mappings"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    app_id = Column(Integer, ForeignKey("admob_apps.id"), nullable=False)
    ad_unit_id = Column(String(128), nullable=False)       # AdMob ad unit ID (ca-app-pub-...)
    network_code = Column(String(32), nullable=False)
    encrypted_fields = Column(Text, default="")            # Fernet-encrypted JSON
    admob_mapping_id = Column(String(64), default="")      # set after first AdMob create
    admob_mapping_name = Column(String(255), default="")
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
        return f"AdMob API error: {payload.get('error', {}).get('message', str(e))}"
    except Exception:
        return f"AdMob API error: {e}"


def _date_parts(yyyy_mm_dd: str) -> dict:
    y, m, d = yyyy_mm_dd.split("-")
    return {"year": int(y), "month": int(m), "day": int(d)}


def _today_iso() -> str:
    # AdMob reports use the account's default timezone (typically America/Los_Angeles).
    # Using UTC dates produced numbers that differed from the AdMob web dashboard
    # because the time windows didn't line up. Use LA local date to match the dashboard.
    return _admob_today().isoformat()


def _days_ago_iso(n: int) -> str:
    return (_admob_today() - timedelta(days=n)).isoformat()


def _admob_today():
    """Today's date in AdMob's reporting timezone (America/Los_Angeles)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        # zoneinfo unavailable (very old Python) — fall back to a fixed -8h offset.
        # Note: this ignores DST. zoneinfo is in Python 3.9+ so this fallback is rare.
        return (datetime.utcnow() - timedelta(hours=8)).date()


class AdMobClient:
    def __init__(self, db: Session, user: User):
        self.db = db
        self.user = user
        if user.token is None:
            raise RuntimeError("User has no OAuth token. Sign in again.")
        creds = refresh_if_needed(credentials_from_db(user.token))
        persist_credentials(db, user, creds)
        self.service = build("admob", "v1", credentials=creds, cache_discovery=False)
        # v1beta hosts the write endpoints: mediationGroups create/patch, adUnitMappings, etc.
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
                # Monetary metrics (ESTIMATED_EARNINGS, IMPRESSION_RPM) come back
                # as microsValue per AdMob API docs. e.g. $6.50 == 6500000.
                v = metrics.get(key, {}).get("microsValue")
                if v is not None:
                    return int(v)
                # Some responses use integerValue or doubleValue for the same field.
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
        """List available ad sources from AdMob v1beta. Needed to get the AdMob Network
        adSourceId for waterfall lines. Cached on the client for the request lifetime."""
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
        """List adapters for one ad source. Each network has multiple adapters
        (typically one per platform: Android SDK, iOS SDK). Each adapter has its
        own adapterId and adapterConfigMetadata (the numeric config field IDs
        that adUnitMappings.create requires)."""
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
        """Resolve a network code (META, APPLOVIN, ...) to its live AdMob adSourceId.
        Falls back to the catalog's hardcoded value if the live lookup fails. The
        hardcoded values are validated against the catalog's network name (case-
        insensitive substring match against ad source 'title')."""
        cat = NETWORK_BY_CODE.get(network_code.upper())
        if not cat:
            return ""
        # Build a name pattern; first word usually matches the title.
        name = cat["name"].lower()
        try:
            for src in self.list_ad_sources():
                title = (src.get("title") or "").lower()
                # Match if title contains the network's primary word
                primary_words = [w for w in name.split() if len(w) > 3]
                if any(w in title for w in primary_words):
                    return src.get("adSourceId") or cat["admob_source_id"]
        except AdMobAPIError:
            pass
        return cat["admob_source_id"]

    def resolve_adapter_for_network(self, network_code: str, platform: str) -> dict | None:
        """For the given network and target platform (ANDROID/IOS), return the
        right adapter dict (with adapterId and adapterConfigMetadata). Returns
        None if nothing matches."""
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
        # No exact platform match — return first as best-effort
        return adapters[0] if adapters else None

    def build_admob_config_payload(
        self,
        network_code: str,
        platform: str,
        user_fields: dict,
    ) -> tuple[str, dict, list[str]]:
        """Translate user-supplied field values ({"placement_id": "abc", ...}) into
        AdMob's required adUnitConfigurations shape ({"<numeric_metadata_id>": "abc", ...})
        by reading the live adapter metadata.

        Returns (adapter_id, configurations_dict, warnings).
        Raises AdMobAPIError if the adapter cannot be found (caller decides how
        to surface that to the user).
        """
        adapter = self.resolve_adapter_for_network(network_code, platform)
        if adapter is None:
            raise AdMobAPIError(
                f"Could not find an AdMob adapter for {network_code} on {platform}. "
                f"Make sure the network is enabled in AdMob → Mediation → Ad networks."
            )
        adapter_id = str(adapter.get("adapterId", ""))
        metadata = adapter.get("adapterConfigMetadata", []) or []
        # adapterConfigMetadata: list of {adapterConfigMetadataId, adapterConfigMetadataLabel}
        # We match by label (case-insensitive substring) against our catalog's
        # field labels OR field keys. Then map user_fields[key] -> {metaId: value}.
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
                    f"metadata for {network_code}. Available labels: "
                    + ", ".join(m.get("adapterConfigMetadataLabel", "?") for m in metadata)
                )
        return adapter_id, configs, warnings

    def get_admob_network_source_id(self) -> str:
        return self.find_source_id_for_network("ADMOB") or "5450213213286189855"

    def create_ad_unit_mapping_in_admob(
        self,
        ad_unit_id: str,             # e.g. ca-app-pub-XXX/YYY
        network_code: str,           # META, APPLOVIN, ...
        platform: str,               # ANDROID / IOS (used to pick adapter)
        display_name: str,           # human label
        user_fields: dict,           # {"placement_id": "abc", "app_key": "xyz", ...}
    ) -> tuple[dict, list[str]]:
        """POST to /v1beta/accounts/{pub}/adUnits/{adUnitId}/adUnitMappings.

        Translates user_fields into AdMob's required shape:
          - adapterId: numeric ID for THIS network on THIS platform (looked up live)
          - adUnitConfigurations: {<numeric_metadata_id>: <value>} keys, also looked
            up live from adapterConfigMetadata.

        Returns (response_dict, warnings).
        """
        adapter_id, configs, warnings = self.build_admob_config_payload(
            network_code=network_code, platform=platform, user_fields=user_fields,
        )
        if not configs:
            raise AdMobAPIError(
                f"No usable configuration values for {network_code} on {platform}. "
                "Open /networks and fill in the credential fields."
            )

        # AdMob ad unit ID accepted by the URL is the SHORT fragment after the
        # slash (e.g. '1234567890' from 'ca-app-pub-XXX/1234567890'). Older
        # samples used the full ID too; the safe form is the fragment.
        short_id = ad_unit_id.split("/")[-1] if "/" in ad_unit_id else ad_unit_id
        parent = f"accounts/{self.get_publisher_id()}/adUnits/{short_id}"
        body = {
            "name": display_name[:80],
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

    def create_mediation_group_in_admob(
        self,
        display_name: str,
        platform: str,
        ad_format: str,
        ad_unit_id: str,
        country_codes: list[str],
        admob_manual_ecpms: list[float],   # 1+ AdMob Network MANUAL lines at these eCPMs
    ) -> dict:
        """POST to /v1beta/accounts/{pub}/mediationGroups.

        Pushes N AdMob Network MANUAL waterfall lines at the user-specified eCPMs.
        AdMob auto-adds 1 LIVE AdMob Network line on top, so the resulting group
        will have N + 1 lines.

        AdMob caps the number of AdMob Network MANUAL lines per group (currently
        around 3 per Google's internal rule — exact number not documented). If
        the caller passes more than AdMob allows, AdMob returns:
            'Max allowed AdMob Network lines exceeded'
        The error surfaces verbatim to the user; they can reduce the line count
        and retry.
        """
        admob_network_id = self.get_admob_network_source_id()
        lines: dict[str, dict] = {}
        for i, ecpm in enumerate(admob_manual_ecpms):
            if not ecpm or ecpm <= 0:
                continue
            cpm_micros = int(round(ecpm * 1_000_000))
            line_key = f"-{i + 1}"
            lines[line_key] = {
                "displayName": f"Line {i + 1} - ${ecpm:.2f}",
                "adSourceId": admob_network_id,
                "cpmMode": "MANUAL",
                "cpmMicros": str(cpm_micros),
                "state": "ENABLED",
            }

        ad_format_map = {
            "BANNER": "BANNER", "INTERSTITIAL": "INTERSTITIAL",
            "REWARDED": "REWARDED", "REWARDED_INTERSTITIAL": "REWARDED_INTERSTITIAL",
            "NATIVE": "NATIVE", "APP_OPEN": "APP_OPEN_AD",
        }
        admob_format = ad_format_map.get(ad_format.upper(), ad_format.upper())

        targeting: dict = {
            "platform": platform.upper(),
            "format": admob_format,
            "adUnitIds": [ad_unit_id],
        }
        if country_codes:
            targeting["targetedRegionCodes"] = country_codes

        body = {
            "displayName": display_name[:80],
            "state": "ENABLED",
            "targeting": targeting,
        }
        if lines:
            body["mediationGroupLines"] = lines

        parent = f"accounts/{self.get_publisher_id()}"
        try:
            return self.service_beta.accounts().mediationGroups().create(
                parent=parent, body=body,
            ).execute()
        except HttpError as e:
            raise AdMobAPIError(_format_http_error(e)) from e


        return {
            "name": group.name, "ad_format": group.ad_format,
            "platform": group.platform, "status": group.status,
            "targeting": {
                "country_mode": group.country_mode,
                "countries": group.countries or [],
                "ad_unit_id": group.target_ad_unit_id,
                "ad_unit_name": group.target_ad_unit_name,
            },
            "floor_type": group.floor_type,
            "base_avg_ecpm_usd": group.base_avg_ecpm,
            "report_metrics_snapshot": group.report_metrics or {},
            "waterfall": [
                {"priority": l.priority, "line_name": l.line_name,
                 "ecpm_usd": l.ecpm_usd, "enabled": l.enabled}
                for l in sorted(group.waterfall_lines, key=lambda x: x.priority)
            ],
        }


def _random_suffix(n: int = 6) -> str:
    return "".join(random.choices(string.digits, k=n))


def _build_group_name(prefix: str, ad_unit_name: str, unique: bool) -> str:
    base = f"{prefix}_{ad_unit_name}".replace(" ", "_")[:80]
    return f"{base}_{_random_suffix()}" if unique else base


# ============================================================================
# TEMPLATES + STATIC (filled in below via bash appends)
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
  <footer class="footer"><span>AdMob Mediation Tool</span><span class="footer-sep">·</span><span>Real AdMob API · Live reports · Waterfall config exported as JSON</span></footer>
</body>
</html>"""

TEMPLATE_FILES["login.html"] = r"""{% extends "base.html" %}
{% block title %}Sign in · Mediation Tool{% endblock %}
{% block content %}
<section class="login-wrap">
  <div class="login-card">
    <p class="eyebrow">AdMob mediation workflow</p>
    <h1 class="display">Connect your <em>AdMob</em> account.</h1>
    <p class="lede">Sign in with Google. The tool pulls your AdMob apps, ad units, and last-7-day metrics live from the AdMob API, then helps you build mediation waterfall configurations for each ad unit.</p>
    <a class="btn-primary" href="/auth/login"><span class="g-mark">G</span>Continue with Google</a>
    <p class="fineprint">Requires AdMob API scopes: <code>admob.readonly</code> and <code>admob.report</code>.</p>
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
      <li>Export JSON, apply in AdMob UI</li>
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
  <h2 class="section-title">How it works</h2>
  <ol class="workflow-steps">
    <li><span class="step-no">01</span><span class="step-text">Sign in <span class="done">✓</span></span></li>
    <li><span class="step-no">02</span><span class="step-text">Sync your AdMob apps &amp; ad units <a href="/apps">→ Apps</a></span></li>
    <li><span class="step-no">03</span><span class="step-text">Open the Builder <a href="/mediation/builder">→ Builder</a></span></li>
    <li><span class="step-no">04</span><span class="step-text">Select an app</span></li>
    <li><span class="step-no">05</span><span class="step-text">Select one or more ad units</span></li>
    <li><span class="step-no">06</span><span class="step-text">Choose country targeting (Global / Choose / Exclude)</span></li>
    <li><span class="step-no">07</span><span class="step-text">Set waterfall depth (1–{{ max_lines }}) + floor type</span></li>
    <li><span class="step-no">08</span><span class="step-text">Fetch live AdMob report (last 7 days)</span></li>
    <li><span class="step-no">09</span><span class="step-text">Review calculated waterfall values</span></li>
    <li><span class="step-no">10</span><span class="step-text">Generate mediation group(s) → JSON export</span></li>
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
  <thead><tr><th>Name</th><th>Ad Unit</th><th>Format</th><th>Platform</th><th>Countries</th><th>Avg eCPM</th><th>Lines</th><th>Status</th><th>Updated</th><th></th></tr></thead>
  <tbody>
    {% for g in groups %}
    <tr>
      <td>{{ g.name }}</td>
      <td class="mono small">{{ g.target_ad_unit_name or g.target_ad_unit_id }}</td>
      <td><span class="pill">{{ g.ad_format }}</span></td>
      <td><span class="pill pill-{{ g.platform|lower }}">{{ g.platform }}</span></td>
      <td class="small">{% if g.country_mode == "GLOBAL" %}Global{% elif g.country_mode == "INCLUDE" %}+{{ g.countries|length }}{% else %}−{{ g.countries|length }}{% endif %}</td>
      <td class="small">${{ "%.2f"|format(g.base_avg_ecpm) }}</td>
      <td>{{ g.waterfall_lines|length }}</td>
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
  <p class="lede">Pick app + ad units → choose targeting → fetch live AdMob report → generate waterfall config(s).</p>
</section>

{% if not apps %}
<div class="empty">
  <p>You don't have any apps cached yet. Sync them first.</p>
  <form method="post" action="/apps/sync"><button class="btn-primary" type="submit">↻ Sync from AdMob</button></form>
</div>
{% else %}

<div id="setup-banner" class="setup-banner" style="display:none">
  <div class="setup-banner-icon" id="setup-banner-icon">⚠</div>
  <div class="setup-banner-body">
    <strong id="setup-banner-title">Setup status</strong>
    <div id="setup-banner-detail" class="small muted"></div>
  </div>
  <a id="setup-banner-link" href="/networks" class="btn-secondary btn-sm">Configure networks →</a>
</div>

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
      <legend><span class="num">02</span> Select Ad Units</legend>
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
        <label><input type="radio" name="country_mode" value="EXCLUDE" /> <span><b>Exclude</b> specific countries</span></label>
      </div>
      <div id="country-picker" style="display:none">
        <input type="text" id="country-search" placeholder="Search countries…" />
        <div id="country-list" class="country-chips"></div>
        <p class="muted small">Or paste comma-separated ISO-2 codes:</p>
        <input type="text" id="country-paste" placeholder="US, GB, DE, JP" />
      </div>
    </fieldset>

    <fieldset class="builder-step">
      <legend><span class="num">04</span> Waterfall Depth &amp; Floor</legend>
      <div class="grid grid-2">
        <label><span class="lbl">Number of waterfall lines (1–{{ max_lines }})</span>
          <input type="number" id="line-count" min="1" max="{{ max_lines }}" value="{{ default_lines }}" /></label>
        <label><span class="lbl">Floor type</span>
          <select id="floor-type">
            {% for f in floor_types %}<option value="{{ f }}">{{ f.replace("_"," ").title() }}</option>{% endfor %}
          </select></label>
      </div>
      <label class="check-row"><input type="checkbox" id="unique-names" checked /> Generate unique mediation group names (append random suffix)</label>
      <label class="check-row"><span class="lbl">Group name prefix</span>
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
    Edit constants in <code>flow.py</code> to change.
  </div>
  <div class="form-actions">
    <button class="btn-primary btn-lg" id="push-btn">▶ Create in AdMob (real)</button>
    <button class="btn-secondary btn-lg" id="generate-btn">Save locally only</button>
  </div>
  <p class="muted small" style="margin-top: 12px">
    <strong>Note:</strong> AdMob only allows one AdMob Network MANUAL line per mediation group.
    Only the <strong>highest eCPM</strong> from your waterfall is pushed as a MANUAL floor.
    AdMob auto-adds its own LIVE (real-time) line below it. All your calculated lines
    are still saved here for reference. To get a full multi-line waterfall in AdMob,
    add 3rd-party networks (Meta, AppLovin, Unity, etc.) manually in the AdMob UI.
  </p>
</section>

{% endif %}

<script>
const APP_AD_UNITS = {{ ad_units_by_app|tojson }};
const COUNTRIES = {{ countries|tojson }};
const MAX_LINES = {{ max_lines }};
const DEFAULT_LINES = {{ default_lines }};
const NETWORK_CATALOG = {{ network_catalog|tojson }};
const CRED_AVAILABILITY = {{ cred_availability|tojson }};
const EXISTING_GROUPS = {{ existing_groups|tojson }};
const SETUP_STATUS = {{ setup_status|tojson }};

function updateSetupBanner(appId) {
  const banner = document.getElementById("setup-banner");
  if (banner) banner.style.display = "none";
}

const $ = sel => document.querySelector(sel);
const $$ = sel => [...document.querySelectorAll(sel)];

const state = {
  app_id: null, app_label: "",
  ad_units: [],
  country_mode: "GLOBAL",
  countries: new Set(),
  line_count: DEFAULT_LINES,
  floor_type: "{{ floor_types[0] }}",
  unique_names: true,
  name_prefix: "Global",
  report: null,
};

function networksForAdUnit(adUnitId) {
  const m = (CRED_AVAILABILITY[state.app_id] || {});
  const out = [];
  for (const code of Object.keys(m)) {
    if ((m[code] || []).includes(adUnitId)) out.push(code);
  }
  return out;
}

function updatePreview() {
  const el = $("#preview-summary");
  if (!state.app_id) { el.innerHTML = '<span class="muted">No app selected yet.</span>'; return; }
  const lines = [];
  lines.push(`<div class="kv"><span>App</span><b>${state.app_label || ""}</b></div>`);
  lines.push(`<div class="kv"><span>Ad units</span><b>${state.ad_units.length}</b></div>`);
  let c;
  if (state.country_mode === "GLOBAL") c = "Global";
  else if (state.countries.size === 0) c = `${state.country_mode === "INCLUDE" ? "Choose" : "Exclude"} (none)`;
  else c = `${state.country_mode === "INCLUDE" ? "+" : "−"}${state.countries.size}: ${[...state.countries].join(", ")}`;
  lines.push(`<div class="kv"><span>Countries</span><b>${c}</b></div>`);
  lines.push(`<div class="kv"><span>Lines</span><b>${state.line_count}</b> (all AdMob Network MANUAL)</div>`);
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
    const nets = networksForAdUnit(u.ad_unit_id);
    const netInfo = nets.length ? `<span class="small good">· ${nets.length} network(s) configured</span>` : `<span class="small warn">· no networks · <a href="/networks#app-${state.app_id}">add</a></span>`;
    const existing = EXISTING_GROUPS[u.ad_unit_id] || [];
    const existingInfo = existing.length
      ? `<div class="small muted" style="margin-top:6px">${existing.length} existing group(s) for this ad unit: ` +
        existing.slice(0, 3).map(g => `<a href="/mediation/${g.id}" target="_blank">${g.name}</a>` + (g.admob_group_id ? ` <span class="pill pill-good">in AdMob</span>` : "")).join(", ") +
        (existing.length > 3 ? `, +${existing.length - 3} more` : "") +
        `</div>`
      : "";
    card.innerHTML = `<div><div class="adunit-name">${u.name || "(unnamed)"} <span class="pill">${u.ad_format}</span> ${netInfo}</div><div class="adunit-id mono small">${u.ad_unit_id}</div>${existingInfo}</div><button type="button" class="btn-ghost btn-sm">${sel ? "Selected ✓" : "Select"}</button>`;
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
  state.app_label = e.target.options[e.target.selectedIndex].textContent;
  state.ad_units = [];
  $("#adunit-step").style.display = state.app_id ? "" : "none";
  updateSetupBanner(state.app_id);
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
    const networkWarning = `<div class="muted small" style="margin-top:8px">Will push <b>${lines.length}</b> AdMob Network MANUAL line(s) at the eCPMs below. AdMob auto-adds 1 LIVE line on top. Note: AdMob caps AdMob Network MANUAL lines per group at around 3 — if your line count is higher, AdMob may reject some.</div>`;

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
      ${networkWarning}
      <div class="line-table">
        <div class="line-table-head"><div>Line</div><div>eCPM (editable)</div><div>Source</div></div>
        ${lines.map((v, i) => `
          <div class="line-table-row">
            <div class="mono small">${i+1}</div>
            <input type="number" min="0" step="0.01" value="${v.toFixed(2)}" data-au="${u.ad_unit_id}" data-i="${i}" class="line-input" />
            <div class="small muted">AdMob Network (manual)</div>
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
        `• ${items.length} ad unit(s) selected\n` +
        `• ${totalLines} AdMob Network MANUAL line(s) total to push (across all ad units)\n` +
        `• AdMob will auto-add 1 LIVE AdMob Network line to each group on top\n\n` +
        `Note: AdMob caps the number of AdMob Network MANUAL lines per group (around 3). If your line count is higher, AdMob may reject the request.\n\n` +
        `Continue with push to AdMob?`;
      if (!confirm(summary)) return;
    }

    const body = {
      app_id: state.app_id, country_mode: state.country_mode,
      countries: [...state.countries], floor_type: state.floor_type,
      unique_names: state.unique_names, name_prefix: state.name_prefix,
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

      // Detailed result message
      let msg;
      if (label === "push") {
        const groups = data.groups || [];
        const ok = groups.filter(g => g.status === "PUSHED").length;
        const partial = groups.filter(g => g.status === "PUSHED_PARTIAL").length;
        const failed = groups.filter(g => g.status === "PUSH_FAILED").length;
        msg = `═══ PUSH RESULT ═══\n\n`;
        msg += `✓ Created in AdMob: ${ok}\n`;
        if (partial) msg += `⚠ Partial (some lines failed): ${partial}\n`;
        if (failed) msg += `✗ Failed entirely: ${failed}\n`;
        msg += `\n`;
        groups.forEach(g => {
          const mark = g.status === "PUSHED" ? "✓" : g.status === "PUSHED_PARTIAL" ? "⚠" : "✗";
          msg += `${mark} ${g.name}`;
          if (g.admob_group_id) msg += `  (AdMob ID: ${g.admob_group_id})`;
          msg += `\n`;
        });
        if ((data.push_errors || []).length) {
          msg += `\n═══ ERRORS ═══\n`;
          data.push_errors.forEach((e, i) => {
            msg += `\n${i+1}. ad unit ${e.ad_unit_id}:\n   ${e.error}\n`;
          });
          const firstErr = (data.push_errors[0].error || "").toLowerCase();
          if (firstErr.includes("permission")) {
            msg += `\n═══ HINT ═══\nThis usually means AdMob Write API is not enabled for your account. Contact AdMob support to request it.`;
          } else if (firstErr.includes("no credentials") || firstErr.includes("empty")) {
            msg += `\n═══ HINT ═══\nGo to /networks and configure credentials for the missing network(s).`;
          }
        }
      } else {
        msg = `Saved ${data.groups.length} group(s) locally (not pushed to AdMob).`;
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
      {% if group.admob_group_id %}· <strong style="color:var(--good)">AdMob ID: <code>{{ group.admob_group_id }}</code></strong>{% endif %}
    </p>
  </div>
  <div class="actions-col">
    <span class="status status-{{ group.status|lower }}">{{ group.status }}</span>
    <a href="/mediation/{{ group.id }}/export.json" class="btn-secondary" target="_blank">Export JSON</a>
    <form method="post" action="/mediation/{{ group.id }}/delete" onsubmit="return confirm('Delete this group?');"><button type="submit" class="btn-danger">Delete</button></form>
  </div>
</section>
<h2 class="section-title">Report snapshot (when generated)</h2>
{% if group.report_metrics %}
<div class="metric-grid">
  <div class="metric"><span class="metric-label">Avg eCPM</span><span class="metric-value">${{ "%.2f"|format(group.report_metrics.get("ecpm_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Revenue</span><span class="metric-value good">${{ "%.2f"|format(group.report_metrics.get("revenue_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Match Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("match_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">Show Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("show_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">Fill Rate</span><span class="metric-value">{{ "%.2f"|format((group.report_metrics.get("fill_rate", 0))*100) }}%</span></div>
  <div class="metric"><span class="metric-label">RPM</span><span class="metric-value">${{ "%.2f"|format(group.report_metrics.get("rpm_usd", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Requests</span><span class="metric-value">{{ "{:,}".format(group.report_metrics.get("ad_requests", 0)) }}</span></div>
  <div class="metric"><span class="metric-label">Impressions</span><span class="metric-value">{{ "{:,}".format(group.report_metrics.get("impressions", 0)) }}</span></div>
</div>
{% else %}<p class="muted">No report snapshot saved with this group.</p>{% endif %}
<h2 class="section-title">Waterfall ({{ group.waterfall_lines|length }} lines)</h2>
{% if group.waterfall_lines %}
<table class="table">
  <thead><tr><th>#</th><th>Line</th><th>eCPM</th><th>Enabled</th></tr></thead>
  <tbody>{% for line in group.waterfall_lines %}<tr><td>{{ line.priority + 1 }}</td><td>{{ line.line_name }}</td><td class="mono">${{ "%.2f"|format(line.ecpm_usd) }}</td><td>{{ "yes" if line.enabled else "no" }}</td></tr>{% endfor %}</tbody>
</table>
{% else %}<p class="muted">No waterfall lines.</p>{% endif %}
<div class="callout">
  <strong>Note:</strong> AdMob's REST API does not expose creation of mediation groups. Use the exported JSON as a reference to recreate this group manually in the AdMob web UI.
</div>
{% endblock %}"""

TEMPLATE_FILES["networks.html"] = r"""{% extends "base.html" %}
{% block title %}Networks · Mediation Tool{% endblock %}
{% block content %}
<section class="page-head">
  <p class="eyebrow">3rd-party networks</p>
  <h1 class="display">Network credentials</h1>
  <p class="lede">Per-app credentials for each ad network. Stored encrypted. Required before pushing multi-line mediation groups to AdMob.</p>
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
        {# app-level fields #}
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

        {# per-ad-unit fields #}
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
          <p class="muted small">No ad units cached for this app. Sync apps first.</p>
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
.status { display: inline-block; padding: 3px 9px; border-radius: 4px; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.05em; }
.status-draft { color: var(--ink-mute); background: rgba(132,124,105,0.12); }
.status-generated { color: var(--accent); background: rgba(244,185,66,0.12); }
.status-pushed { color: var(--good); background: rgba(127,182,133,0.16); }
.status-push_failed { color: var(--bad); background: rgba(226,112,91,0.14); }
label { display: flex; flex-direction: column; gap: 6px; }
.lbl { color: var(--ink-dim); font-size: 12.5px; }
input[type="text"], input[type="number"], input:not([type]), select, textarea { background: var(--bg-3); color: var(--ink); border: 1px solid var(--line-2); border-radius: var(--radius); padding: 9px 12px; font: 400 14px/1.3 var(--font-body); }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(244,185,66,0.12); }
.builder-grid { display: grid; grid-template-columns: 1.4fr 0.8fr; gap: 28px; align-items: start; }
@media (max-width: 1000px) { .builder-grid { grid-template-columns: 1fr; } }
.builder-step { background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 18px 20px; margin-bottom: 18px; }
.builder-step legend { padding: 0 6px; color: var(--ink-dim); font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; }
.builder-step legend .num { color: var(--accent); margin-right: 8px; }
.check-row { flex-direction: row; align-items: center; gap: 8px; margin-top: 12px; color: var(--ink-dim); font-size: 13px; }
.radio-row { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.radio-row label { flex-direction: row; align-items: center; gap: 10px; color: var(--ink-dim); }
.form-actions { display: flex; gap: 12px; align-items: center; padding-top: 4px; }
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
.line-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; padding-top: 12px; border-top: 1px dashed var(--line); }
@media (max-width: 700px) { .line-row { grid-template-columns: repeat(2, 1fr); } }
.line-cell { gap: 4px; }
.line-label { font-family: var(--font-mono); font-size: 10.5px; color: var(--ink-mute); letter-spacing: 0.06em; text-transform: uppercase; }
.line-input { font-family: var(--font-mono); }

.setup-banner { display: flex; align-items: center; gap: 14px; padding: 14px 18px; border-radius: var(--radius-lg); margin: 0 0 24px; border: 1px solid var(--line); }
.setup-banner-warn { background: rgba(244,185,66,0.08); border-color: rgba(244,185,66,0.3); }
.setup-banner-good { background: rgba(127,182,133,0.08); border-color: rgba(127,182,133,0.3); }
.setup-banner-icon { font-size: 24px; line-height: 1; flex-shrink: 0; }
.setup-banner-warn .setup-banner-icon { color: var(--accent); }
.setup-banner-good .setup-banner-icon { color: var(--good); }
.setup-banner-body { flex: 1; min-width: 0; }
.setup-banner-body strong { display: block; margin-bottom: 2px; }
.pill-good { background: rgba(127,182,133,0.18); color: var(--good); }
.good { color: var(--good); }
.warn { color: var(--accent); }
.line-quick-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 10px 0; padding: 8px 12px; background: rgba(0,0,0,0.12); border-radius: var(--radius); }
.line-table { margin-top: 12px; border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.line-table-head { display: grid; grid-template-columns: 50px 1fr 1.4fr; gap: 10px; padding: 8px 12px; background: rgba(0,0,0,0.18); font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mute); }
.line-table-row { display: grid; grid-template-columns: 50px 1fr 1.4fr; gap: 10px; padding: 8px 12px; border-top: 1px solid var(--line); align-items: center; }
.line-network { font-size: 13px; }

/* Networks page */
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
    """Lightweight schema reconciler for SQLite.

    SQLAlchemy's create_all() makes new tables but never alters existing ones,
    so adding a column to a model means the next query crashes with
    "no such column" until the user wipes the DB. This walks every table in
    Base.metadata, reads the live PRAGMA table_info, and issues
    ALTER TABLE ... ADD COLUMN for anything missing.

    Limitations: SQLite ALTER TABLE only supports adding columns (no drops,
    no type changes). For destructive changes you still need to delete the
    .db file. That's the right tradeoff for a single-file dev tool.

    SQLAlchemy ↔ SQLite type mapping is intentionally narrow: we map the
    Python types we actually use (Integer, String, Text, Float, Boolean,
    DateTime, JSON). Anything else falls back to TEXT, which SQLite tolerates.
    """
    if not settings.database_url.startswith("sqlite"):
        return  # Other DBs: use real migrations (Alembic). Out of scope here.

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import OperationalError

    sql_type_map = {
        "INTEGER": "INTEGER",
        "VARCHAR": "TEXT",
        "TEXT": "TEXT",
        "FLOAT": "REAL",
        "REAL": "REAL",
        "BOOLEAN": "INTEGER",
        "DATETIME": "TEXT",
        "JSON": "TEXT",
    }

    with engine.connect() as conn:
        insp = sa_inspect(conn)
        existing_tables = set(insp.get_table_names())
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue  # create_all() will handle brand-new tables
            existing_columns = {c["name"] for c in insp.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_columns:
                    continue
                # Figure out SQLite type
                py_type_name = str(col.type).upper().split("(")[0]
                sql_type = sql_type_map.get(py_type_name, "TEXT")
                # Build DEFAULT clause if the column has a static default
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
                    # Best-effort. If a column rename / type change is needed,
                    # tell the user to wipe the DB.
                    print(f"  [migrate] FAILED on {table_name}.{col.name}: {e}")
        conn.commit()


# ============================================================================
# APP
# ============================================================================
write_assets()
Base.metadata.create_all(bind=engine)
_auto_migrate_sqlite()


@asynccontextmanager
async def lifespan(_: "FastAPI"):
    url = f"http://localhost:{settings.port}"
    bar = "=" * (len(url) + 28)
    print()
    print(bar)
    print(f"  >>  Open in browser:  {url}")
    print(bar)
    print()
    yield


app = FastAPI(title="AdMob Mediation Tool", debug=settings.debug, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 7)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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


# ----- Root + auth ----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard")
    return tmpl(request).TemplateResponse("login.html", {"request": request})


auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.get("/login")
def login(request: Request):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET.")
    auth_url, state, code_verifier = get_authorization_url()
    request.session["oauth_state"] = state
    request.session["code_verifier"] = code_verifier
    return RedirectResponse(auth_url)


@auth_router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None, db: Session = Depends(get_db)):
    expected_state = request.session.get("oauth_state")
    if not code or not state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth callback (state mismatch).")
    flow = build_flow(state=state)
    code_verifier = request.session.get("code_verifier")
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    profile = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"}, timeout=15,
    ).json()
    sub = profile.get("sub")
    email = profile.get("email", "")
    if not sub:
        raise HTTPException(status_code=500, detail="Could not read Google profile (no 'sub' claim).")
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


# ----- Dashboard ------------------------------------------------------------
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


# ----- Apps -----------------------------------------------------------------
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


# ----- Networks (3rd-party credentials) -------------------------------------
networks_router = APIRouter(prefix="/networks", tags=["networks"])


@networks_router.get("", response_class=HTMLResponse)
def networks_view(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    apps = db.query(AdMobApp).filter(AdMobApp.user_id == user.id).order_by(AdMobApp.name).all()
    # app_creds_by_app_net[(app_id, network_code)] = decrypted dict
    app_creds: dict[tuple, dict] = {}
    for c in db.query(NetworkCredential).filter(NetworkCredential.user_id == user.id).all():
        app_creds[(c.app_id, c.network_code)] = decrypt_dict(c.encrypted_fields)
    # unit_creds_by_key[(app_id, ad_unit_id, network_code)] = {fields:..., admob_mapping_id:...}
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
    # If any field changed and we had an AdMob mapping ID, the cached mapping is
    # stale. Clear it so the next push recreates it with the new credentials.
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
            # Empty inputs → remove the row entirely
            db.delete(mp)
    db.commit()
    return RedirectResponse(f"/networks#app-{app_pk}", status_code=303)


# ----- Mediation ------------------------------------------------------------
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
    # cred_availability[app_id][network_code] = [ad_unit_id, ...]
    cred_availability: dict[int, dict[str, list[str]]] = {a.id: {} for a in apps}
    mappings = db.query(AdUnitMapping).filter(AdUnitMapping.user_id == user.id).all()
    for mp in mappings:
        cred_availability.setdefault(mp.app_id, {}).setdefault(mp.network_code, []).append(mp.ad_unit_id)

    # existing_groups[ad_unit_id] = [{name, status, admob_group_id}, ...] — surfaced
    # in the builder so the user can see what they already have for an ad unit
    # before creating a new one.
    existing_groups: dict[str, list[dict]] = {}
    for g in db.query(MediationGroup).filter(MediationGroup.user_id == user.id).order_by(MediationGroup.created_at.desc()).all():
        if not g.target_ad_unit_id:
            continue
        existing_groups.setdefault(g.target_ad_unit_id, []).append({
            "id": g.id, "name": g.name, "status": g.status,
            "admob_group_id": g.admob_group_id or "",
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else "",
        })

    # Setup status counts shown in the banner
    setup_status: dict[int, dict] = {}
    for a in apps:
        ad_unit_ids = {u.ad_unit_id for u in a.ad_units}
        per_app = cred_availability.get(a.id, {})
        unique_units_covered: set = set()
        for unit_list in per_app.values():
            unique_units_covered.update(unit_list)
        setup_status[a.id] = {
            "networks_configured": len([k for k, v in per_app.items() if v]),
            "units_with_networks": len(unique_units_covered & ad_unit_ids),
            "total_units": len(ad_unit_ids),
        }

    return tmpl(request).TemplateResponse("mediation_builder.html", {
        "request": request, "user": user, "apps": apps,
        "ad_units_by_app": ad_units_by_app,
        "countries": COMMON_COUNTRIES,
        "max_lines": WATERFALL_MAX_LINES,
        "default_lines": WATERFALL_DEFAULT_LINES,
        "floor_types": FLOOR_TYPES,
        "top_mult": WATERFALL_TOP_MULTIPLIER,
        "step_factor": WATERFALL_STEP_FACTOR,
        "network_catalog": [{"code": n["code"], "name": n["name"], "supports_bidding": n["supports_bidding"]} for n in NETWORK_CATALOG if not n.get("internal_only")],
        "cred_availability": cred_availability,
        "existing_groups": existing_groups,
        "setup_status": setup_status,
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


def _generate_groups(payload: dict, db: Session, user: User, push_to_admob: bool):
    try:
        app_pk = int(payload.get("app_id") or 0)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid app_id")
    app_row = db.query(AdMobApp).filter(AdMobApp.id == app_pk, AdMobApp.user_id == user.id).first()
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
    name_prefix = (payload.get("name_prefix") or "Group").strip()
    items = payload.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="No ad units selected")

    client = AdMobClient(db, user) if push_to_admob else None
    created: list[dict] = []
    push_errors: list[dict] = []

    for item in items:
        ad_unit_id = str(item.get("ad_unit_id") or "")
        if not ad_unit_id:
            continue
        ad_unit_name = str(item.get("ad_unit_name") or ad_unit_id)
        ad_format = str(item.get("ad_format") or "BANNER")
        metrics = item.get("metrics") or {}
        ecpms = [float(x) for x in (item.get("lines") or []) if float(x) > 0]
        group_name = _build_group_name(name_prefix, ad_unit_name, unique_names)

        admob_group_id = ""
        admob_group_full = ""
        status = "GENERATED"
        push_resp_text = ""

        if push_to_admob and client is not None:
            push_countries = countries if country_mode == "INCLUDE" else []
            try:
                resp = client.create_mediation_group_in_admob(
                    display_name=group_name,
                    platform=app_row.platform,
                    ad_format=ad_format,
                    ad_unit_id=ad_unit_id,
                    country_codes=push_countries,
                    admob_manual_ecpms=ecpms,
                )
                admob_group_id = resp.get("mediationGroupId", "") or ""
                admob_group_full = resp.get("name", "") or ""
                push_resp_text = json.dumps(resp)[:4000]
                status = "PUSHED"
            except AdMobAPIError as e:
                push_errors.append({"ad_unit_id": ad_unit_id, "error": str(e)})
                push_resp_text = str(e)[:4000]
                status = "PUSH_FAILED"

        group = MediationGroup(
            user_id=user.id, name=group_name, ad_format=ad_format,
            platform=app_row.platform, status=status,
            country_mode=country_mode, countries=countries,
            floor_type=floor_type, target_ad_unit_id=ad_unit_id,
            target_ad_unit_name=ad_unit_name,
            base_avg_ecpm=float(metrics.get("ecpm_usd") or 0.0),
            report_metrics=metrics,
            admob_group_id=admob_group_id,
            admob_group_name=admob_group_full,
            last_push_response=push_resp_text,
        )
        db.add(group); db.commit(); db.refresh(group)
        for i, ecpm in enumerate(ecpms):
            db.add(WaterfallLine(
                group_id=group.id, priority=i,
                line_name=f"Line {i+1}",
                ecpm_usd=ecpm, enabled=True,
                network_code="ADMOB",
                cpm_mode="MANUAL",
            ))
        db.commit()
        created.append({"id": group.id, "name": group.name,
                        "admob_group_id": admob_group_id, "status": status})

    return {"status": "ok", "groups": created, "push_errors": push_errors, "pushed": push_to_admob}


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


if __name__ == "__main__":
    uvicorn.run("flow:app", host=settings.host, port=settings.port, reload=settings.debug)