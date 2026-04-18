import ipaddress
import json
import os
import socket
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import dns.message
import dns.query
import dns.rdatatype
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from routeros_api import RouterOsApiPool


load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "dns-resolver-dev-secret")


DEFAULT_GATEWAY = os.getenv("DEFAULT_GATEWAY", "192.168.222.201")
DEFAULT_DISTANCE = os.getenv("DEFAULT_DISTANCE", "20")
DEFAULT_COMMENT_PREFIX = os.getenv("DEFAULT_COMMENT_PREFIX", "")
DNS_PROVIDERS_RAW = os.getenv(
    "DNS_PROVIDERS",
    (
        "google|Google DNS|https://dns.google/resolve,"
        "cloudflare|Cloudflare DNS|https://cloudflare-dns.com/dns-query,"
        "yandex|Yandex DNS|ns://77.88.8.8,"
        "system|System DNS|system"
    ),
)
DEFAULT_DNS_PROVIDER = os.getenv("DEFAULT_DNS_PROVIDER", "google").strip().lower() or "google"
GATEWAYS_RAW = os.getenv("GATEWAYS", DEFAULT_GATEWAY)
RESOLVE_CACHE_DIR = Path(os.getenv("RESOLVE_CACHE_DIR", "/tmp/dns_resolver_resolves"))
RESOLVE_CACHE_TTL_SECONDS = int(os.getenv("RESOLVE_CACHE_TTL_SECONDS", "1800"))
DNS_REQUEST_TIMEOUT_SECONDS = float(os.getenv("DNS_REQUEST_TIMEOUT_SECONDS", "3"))
RESOLVE_MAX_DURATION_SECONDS = int(os.getenv("RESOLVE_MAX_DURATION_SECONDS", "55"))
AUDIT_DB_PATH = Path(os.getenv("AUDIT_DB_PATH", "/tmp/dns_resolver_audit.db"))
AUDIT_LOG_MAX_ENTRIES = int(os.getenv("AUDIT_LOG_MAX_ENTRIES", "200"))
INDEX_CONTEXT_SESSION_KEY = "index_context"
INDEX_CONTEXT_CACHE_DIR = Path(os.getenv("INDEX_CONTEXT_CACHE_DIR", "/tmp/dns_resolver_index_context"))
INDEX_CONTEXT_TTL_SECONDS = int(os.getenv("INDEX_CONTEXT_TTL_SECONDS", "900"))
RESOLVE_JOB_TTL_SECONDS = int(os.getenv("RESOLVE_JOB_TTL_SECONDS", "3600"))

RESOLVE_JOBS_LOCK = threading.Lock()
RESOLVE_JOBS: dict[str, dict[str, Any]] = {}
AUDIT_DB_INIT_LOCK = threading.Lock()
AUDIT_DB_READY = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_gateways(raw_value: str, fallback_gateway: str) -> tuple[list[str], str, dict[str, str]]:
    gateways: list[str] = []
    seen: set[str] = set()
    default_gateway = ""
    labels: dict[str, str] = {}

    for raw in raw_value.split(","):
        token = raw.strip()
        is_default = False
        label = ""

        if not token:
            continue

        if "|" in token:
            parts = token.split("|")
            token = parts[0].strip()

            default_marker = parts[1].strip().lower() if len(parts) > 1 else ""
            if default_marker in ("default", "*", "true", "1", "yes"):
                is_default = True

            if len(parts) > 2:
                label = parts[2].strip()

        lower = token.lower()
        if token.endswith("*"):
            token = token[:-1].strip()
            is_default = True
        elif lower.endswith(":default"):
            token = token[: -len(":default")].strip()
            is_default = True
        elif lower.startswith("default="):
            token = token[len("default=") :].strip()
            is_default = True

        gateway = token
        if not gateway or gateway in seen:
            continue
        gateways.append(gateway)
        seen.add(gateway)
        labels[gateway] = label if label else gateway
        if is_default and not default_gateway:
            default_gateway = gateway

    if not gateways:
        gateways = [fallback_gateway]
        labels[fallback_gateway] = fallback_gateway

    if not default_gateway:
        default_gateway = gateways[0]

    return gateways, default_gateway, labels


AVAILABLE_GATEWAYS, UI_DEFAULT_GATEWAY, GATEWAY_LABELS = parse_gateways(GATEWAYS_RAW, DEFAULT_GATEWAY)
GATEWAY_OPTIONS = [
    {"value": gateway, "label": GATEWAY_LABELS.get(gateway, gateway)}
    for gateway in AVAILABLE_GATEWAYS
]


def parse_dns_providers(raw_value: str) -> tuple[list[dict[str, str]], str]:
    providers: list[dict[str, str]] = []
    seen_keys: set[str] = set()

    for raw in raw_value.split(","):
        token = raw.strip()
        if not token:
            continue

        key = ""
        label = ""
        endpoint = ""

        if "|" in token:
            parts = [part.strip() for part in token.split("|")]
            if parts:
                key = (parts[0] or "").lower()
            if len(parts) > 1:
                label = parts[1]
            if len(parts) > 2:
                endpoint = parts[2]
        else:
            key = token.lower()
            label = token
            endpoint = token

        if not key:
            continue
        if key in seen_keys:
            continue
        if not endpoint:
            continue

        providers.append(
            {
                "key": key,
                "label": label or key,
                "endpoint": endpoint,
            }
        )
        seen_keys.add(key)

    if not providers:
        providers = [
            {
                "key": "google",
                "label": "Google DNS",
                "endpoint": "https://dns.google/resolve",
            }
        ]

    default_provider = DEFAULT_DNS_PROVIDER if DEFAULT_DNS_PROVIDER in seen_keys else providers[0]["key"]
    return providers, default_provider


DNS_PROVIDERS, UI_DEFAULT_DNS_PROVIDER = parse_dns_providers(DNS_PROVIDERS_RAW)
DNS_PROVIDER_ENDPOINTS = {item["key"]: item["endpoint"] for item in DNS_PROVIDERS}
DNS_PROVIDER_LABELS = {item["key"]: item["label"] for item in DNS_PROVIDERS}


class MikroTikClient:
    def __init__(self) -> None:
        self.host = os.getenv("MIKROTIK_HOST", "gateway.home")
        self.port = int(os.getenv("MIKROTIK_PORT", "18728"))
        self.username = os.getenv("MIKROTIK_USERNAME", "")
        self.password = os.getenv("MIKROTIK_PASSWORD", "")
        self.use_ssl = os.getenv("MIKROTIK_SSL", "false").lower() == "true"

    def _get_pool(self) -> RouterOsApiPool:
        if not self.username or not self.password:
            raise ValueError("MIKROTIK_USERNAME и MIKROTIK_PASSWORD должны быть заданы в конфигурации.")

        return RouterOsApiPool(
            self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            use_ssl=self.use_ssl,
            plaintext_login=True,
        )

    @staticmethod
    def extract_route_id(route: dict[str, Any]) -> str | None:
        route_id = route.get("id") or route.get(".id")
        if route_id is None:
            return None
        return str(route_id)

    def get_routes(self, gateway: str, distance: int) -> list[dict[str, Any]]:
        pool = self._get_pool()
        try:
            api = pool.get_api()
            route_resource = api.get_resource("/ip/route")
            return route_resource.get(gateway=gateway, distance=str(distance))
        finally:
            pool.disconnect()

    def find_exact_routes(self, dst_address: str, gateway: str, distance: int, comment: str) -> list[dict[str, Any]]:
        pool = self._get_pool()
        try:
            api = pool.get_api()
            route_resource = api.get_resource("/ip/route")
            return route_resource.get(
                **{
                    "dst-address": dst_address,
                    "gateway": gateway,
                    "distance": str(distance),
                    "comment": comment,
                }
            )
        finally:
            pool.disconnect()

    def add_route(self, dst_address: str, gateway: str, distance: int, comment: str) -> Any:
        pool = self._get_pool()
        try:
            api = pool.get_api()
            route_resource = api.get_resource("/ip/route")
            return route_resource.add(
                **{
                    "dst-address": dst_address,
                    "gateway": gateway,
                    "distance": str(distance),
                    "comment": comment,
                }
            )
        finally:
            pool.disconnect()

    def remove_route(self, route_id: str) -> None:
        pool = self._get_pool()
        try:
            api = pool.get_api()
            route_resource = api.get_resource("/ip/route")
            route_resource.remove(id=route_id)
        finally:
            pool.disconnect()


def ensure_parent_dir(file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)


def parse_domains(domains_text: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()

    for raw in domains_text.splitlines():
        domain = raw.strip()
        if not domain:
            continue
        if domain in seen:
            continue
        domains.append(domain)
        seen.add(domain)

    return domains


def extract_a_record_ips(payload: dict[str, Any]) -> list[str]:
    ips: list[str] = []
    for record in payload.get("Answer", []):
        if record.get("type") == 1:
            ip = str(record.get("data", "")).strip()
            if ip:
                ips.append(ip)
    return sorted(set(ips))


def resolve_ips(domain: str, provider_key: str) -> list[str]:
    provider = provider_key.strip().lower()
    endpoint = DNS_PROVIDER_ENDPOINTS.get(provider, DNS_PROVIDER_ENDPOINTS.get(UI_DEFAULT_DNS_PROVIDER, ""))

    if not endpoint:
        raise ValueError("Не удалось определить endpoint DNS-провайдера.")

    if endpoint.lower() == "system":
        records = socket.getaddrinfo(domain, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = [item[4][0] for item in records if item and len(item) > 4 and item[4]]
        return sorted(set(ips))

    if endpoint.lower().startswith("ns://"):
        nameserver = endpoint[5:].strip()
        if not nameserver:
            raise ValueError("Для ns:// провайдера не указан DNS сервер.")

        port = 53
        if ":" in nameserver and nameserver.count(":") == 1:
            host_part, port_part = nameserver.split(":", 1)
            nameserver = host_part.strip()
            port = int(port_part.strip())

        query = dns.message.make_query(domain, dns.rdatatype.A)
        try:
            answer = dns.query.udp(query, nameserver, port=port, timeout=DNS_REQUEST_TIMEOUT_SECONDS)
        except Exception:
            answer = dns.query.tcp(query, nameserver, port=port, timeout=DNS_REQUEST_TIMEOUT_SECONDS)

        ips: list[str] = []
        for rrset in answer.answer:
            if rrset.rdtype != dns.rdatatype.A:
                continue
            for record in rrset:
                ip = getattr(record, "address", "")
                if ip:
                    ips.append(ip)
        return sorted(set(ips))

    params: dict[str, str] = {"name": domain, "type": "A"}

    if endpoint.rstrip("/").endswith("/resolve"):
        response = requests.get(endpoint, params=params, timeout=DNS_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return extract_a_record_ips(response.json())

    if endpoint.rstrip("/").endswith("/dns-query"):
        query = dns.message.make_query(domain, dns.rdatatype.A)
        response = requests.post(
            endpoint,
            data=query.to_wire(),
            headers={
                "content-type": "application/dns-message",
                "accept": "application/dns-message",
            },
            timeout=DNS_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        answer = dns.message.from_wire(response.content)

        ips: list[str] = []
        for rrset in answer.answer:
            if rrset.rdtype != dns.rdatatype.A:
                continue
            for record in rrset:
                ip = getattr(record, "address", "")
                if ip:
                    ips.append(ip)
        return sorted(set(ips))

    response = requests.get(endpoint, params=params, timeout=DNS_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return extract_a_record_ips(response.json())


def parse_route_network(route: dict[str, Any]) -> ipaddress._BaseNetwork | None:
    dst = route.get("dst-address")
    if not dst:
        return None

    try:
        if "/" not in dst:
            dst = f"{dst}/32"
        return ipaddress.ip_network(dst, strict=False)
    except ValueError:
        return None


def route_distance_matches(route: dict[str, Any], required_distance: int) -> bool:
    route_distance = route.get("distance")
    if route_distance is None:
        return False
    return str(route_distance).strip() == str(required_distance)


def find_covering_route(
    ip: str,
    existing_routes: list[dict[str, Any]],
    required_distance: int,
) -> dict[str, Any] | None:
    ip_obj = ipaddress.ip_address(ip)
    for route in existing_routes:
        if not route_distance_matches(route, required_distance):
            continue
        network = parse_route_network(route)
        if network and ip_obj in network:
            return route
    return None


def build_route_comment(comment_prefix: str, base_comment: str) -> str:
    prefix = comment_prefix.strip()
    return f"{prefix}: {base_comment}" if prefix else base_comment


def cleanup_resolve_cache() -> None:
    if not RESOLVE_CACHE_DIR.exists():
        return

    now_ts = datetime.now(timezone.utc).timestamp()
    for file_path in RESOLVE_CACHE_DIR.glob("*.json"):
        try:
            raw = file_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            created_at = float(payload.get("created_at_ts", 0))
            if now_ts - created_at > RESOLVE_CACHE_TTL_SECONDS:
                file_path.unlink(missing_ok=True)
        except Exception:
            file_path.unlink(missing_ok=True)


def cleanup_index_context_cache() -> None:
    if not INDEX_CONTEXT_CACHE_DIR.exists():
        return

    now_ts = datetime.now(timezone.utc).timestamp()
    for file_path in INDEX_CONTEXT_CACHE_DIR.glob("*.json"):
        try:
            raw = file_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            created_at = float(payload.get("created_at_ts", 0))
            if now_ts - created_at > INDEX_CONTEXT_TTL_SECONDS:
                file_path.unlink(missing_ok=True)
        except Exception:
            file_path.unlink(missing_ok=True)


def store_index_context_payload(context: dict[str, Any]) -> str:
    INDEX_CONTEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_index_context_cache()

    context_id = uuid.uuid4().hex
    payload = {
        "context_id": context_id,
        "created_at_ts": datetime.now(timezone.utc).timestamp(),
        "context": context,
    }
    (INDEX_CONTEXT_CACHE_DIR / f"{context_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return context_id


def load_index_context_payload(context_id: str) -> dict[str, Any] | None:
    if not context_id:
        return None

    file_path = INDEX_CONTEXT_CACHE_DIR / f"{context_id}.json"
    if not file_path.exists():
        return None

    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        created_at_ts = float(payload.get("created_at_ts", 0))
        now_ts = datetime.now(timezone.utc).timestamp()
        if now_ts - created_at_ts > INDEX_CONTEXT_TTL_SECONDS:
            file_path.unlink(missing_ok=True)
            return None
        return payload.get("context")
    except Exception:
        return None
    finally:
        file_path.unlink(missing_ok=True)


def store_resolve_payload(domains_text: str, ips: list[str], ip_domain_map: dict[str, str]) -> str:
    RESOLVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_resolve_cache()

    resolve_id = uuid.uuid4().hex
    payload = {
        "resolve_id": resolve_id,
        "created_at": utc_now_iso(),
        "created_at_ts": datetime.now(timezone.utc).timestamp(),
        "domains_text": domains_text,
        "ips": ips,
        "ip_domain_map": ip_domain_map,
    }
    (RESOLVE_CACHE_DIR / f"{resolve_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return resolve_id


def load_resolve_payload(resolve_id: str) -> dict[str, Any] | None:
    if not resolve_id:
        return None

    file_path = RESOLVE_CACHE_DIR / f"{resolve_id}.json"
    if not file_path.exists():
        return None

    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    created_at_ts = float(payload.get("created_at_ts", 0))
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - created_at_ts > RESOLVE_CACHE_TTL_SECONDS:
        file_path.unlink(missing_ok=True)
        return None

    return payload


def init_audit_db() -> None:
    global AUDIT_DB_READY
    if AUDIT_DB_READY:
        return

    with AUDIT_DB_INIT_LOCK:
        if AUDIT_DB_READY:
            return
        ensure_parent_dir(AUDIT_DB_PATH)
        conn = sqlite3.connect(str(AUDIT_DB_PATH))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_entries (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_entries(created_at DESC)")
            conn.commit()
            AUDIT_DB_READY = True
        finally:
            conn.close()


def get_audit_db_connection() -> sqlite3.Connection:
    init_audit_db()
    conn = sqlite3.connect(str(AUDIT_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def read_audit_entries(limit: int = AUDIT_LOG_MAX_ENTRIES) -> list[dict[str, Any]]:
    conn = get_audit_db_connection()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM audit_entries ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()

    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            entries.append(json.loads(row["payload_json"]))
        except Exception:
            continue
    return entries


def append_audit_entry(entry: dict[str, Any]) -> None:
    entry_id = str(entry.get("id", "")).strip()
    if not entry_id:
        entry_id = uuid.uuid4().hex
        entry["id"] = entry_id

    created_at = str(entry.get("created_at", "")).strip() or utc_now_iso()
    entry["created_at"] = created_at
    status = str(entry.get("status", "")).strip() or "active"
    entry["status"] = status
    payload_json = json.dumps(entry, ensure_ascii=False)

    conn = get_audit_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO audit_entries (id, created_at, status, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                created_at=excluded.created_at,
                status=excluded.status,
                payload_json=excluded.payload_json
            """,
            (entry_id, created_at, status, payload_json),
        )
        conn.commit()
    finally:
        conn.close()


def split_header_ips(raw_value: str) -> list[str]:
    return [part.strip() for part in raw_value.split(",") if part.strip()]


def normalize_ip_or_host(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def try_reverse_dns(ip: str) -> str:
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        return host
    except Exception:
        return ""


def get_request_source() -> dict[str, str]:
    headers = {
        "x_forwarded_for": request.headers.get("X-Forwarded-For", "").strip(),
        "x_original_forwarded_for": request.headers.get("X-Original-Forwarded-For", "").strip(),
        "x_real_ip": request.headers.get("X-Real-IP", "").strip(),
        "true_client_ip": request.headers.get("True-Client-IP", "").strip(),
        "cf_connecting_ip": request.headers.get("CF-Connecting-IP", "").strip(),
        "x_forwarded_host": request.headers.get("X-Forwarded-Host", "").strip(),
        "host": request.headers.get("Host", "").strip(),
    }
    remote_addr = normalize_ip_or_host(request.remote_addr or "")

    candidates: list[str] = []
    for key in ("cf_connecting_ip", "true_client_ip", "x_original_forwarded_for", "x_forwarded_for", "x_real_ip"):
        for candidate in split_header_ips(headers[key]):
            normalized = normalize_ip_or_host(candidate)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

    if remote_addr and remote_addr not in candidates:
        candidates.append(remote_addr)

    source_ip = candidates[0] if candidates else ""
    source_hostname = ""
    if source_ip:
        try:
            ipaddress.ip_address(source_ip)
            source_hostname = try_reverse_dns(source_ip)
        except ValueError:
            source_hostname = source_ip

    source_display = source_ip
    if source_hostname and source_hostname != source_ip:
        source_display = f"{source_hostname} ({source_ip})"
    elif source_hostname:
        source_display = source_hostname

    return {
        "source_ip": source_ip,
        "source_hostname": source_hostname,
        "source_display": source_display,
        "x_forwarded_for": headers["x_forwarded_for"],
        "x_original_forwarded_for": headers["x_original_forwarded_for"],
        "x_real_ip": headers["x_real_ip"],
        "true_client_ip": headers["true_client_ip"],
        "cf_connecting_ip": headers["cf_connecting_ip"],
        "x_forwarded_host": headers["x_forwarded_host"],
        "host": headers["host"],
        "remote_addr": remote_addr,
    }


def update_audit_entry(entry_id: str, updates: dict[str, Any]) -> bool:
    conn = get_audit_db_connection()
    try:
        row = conn.execute(
            "SELECT payload_json FROM audit_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return False

        try:
            entry = json.loads(row["payload_json"])
        except Exception:
            return False

        entry.update(updates)
        created_at = str(entry.get("created_at", "")).strip() or utc_now_iso()
        status = str(entry.get("status", "")).strip() or "active"
        payload_json = json.dumps(entry, ensure_ascii=False)

        conn.execute(
            "UPDATE audit_entries SET created_at = ?, status = ?, payload_json = ? WHERE id = ?",
            (created_at, status, payload_json, entry_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def find_audit_entry(entry_id: str) -> dict[str, Any] | None:
    conn = get_audit_db_connection()
    try:
        row = conn.execute(
            "SELECT payload_json FROM audit_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:
        return None


def base_context() -> dict[str, Any]:
    return {
        "gateway": UI_DEFAULT_GATEWAY,
        "gateway_options": GATEWAY_OPTIONS,
        "dns_provider": UI_DEFAULT_DNS_PROVIDER,
        "dns_provider_options": DNS_PROVIDERS,
        "distance": DEFAULT_DISTANCE,
        "comment_prefix": DEFAULT_COMMENT_PREFIX,
        "domains_text": "",
        "ips": [],
        "ip_entries": [],
        "resolve_id": "",
        "warnings": [],
        "error": None,
        "result": None,
        "audit_entries": read_audit_entries(),
        "audit_message": None,
        "audit_error": None,
    }


def stash_index_context(context: dict[str, Any]) -> None:
    payload = {
        "gateway": context.get("gateway"),
        "dns_provider": context.get("dns_provider"),
        "distance": context.get("distance"),
        "comment_prefix": context.get("comment_prefix"),
        "domains_text": context.get("domains_text"),
        "ips": context.get("ips", []),
        "ip_entries": context.get("ip_entries", []),
        "resolve_id": context.get("resolve_id", ""),
        "warnings": context.get("warnings", []),
        "error": context.get("error"),
        "result": context.get("result"),
        "audit_message": context.get("audit_message"),
        "audit_error": context.get("audit_error"),
    }
    context_id = store_index_context_payload(payload)
    session[INDEX_CONTEXT_SESSION_KEY] = context_id


def redirect_with_index_context(context: dict[str, Any]):
    stash_index_context(context)
    return redirect(url_for("index"))


def hydrate_resolve_context_from_form(context: dict[str, Any], form: Any) -> dict[str, Any]:
    context["gateway"] = form.get("gateway", UI_DEFAULT_GATEWAY).strip()
    context["dns_provider"] = form.get("dns_provider", UI_DEFAULT_DNS_PROVIDER).strip().lower()
    context["distance"] = form.get("distance", DEFAULT_DISTANCE).strip()
    context["comment_prefix"] = form.get("comment_prefix", DEFAULT_COMMENT_PREFIX).strip()
    context["domains_text"] = form.get("domains", "").strip()
    return context


def validate_resolve_context(context: dict[str, Any]) -> tuple[dict[str, Any], list[str] | None]:
    domains = parse_domains(context["domains_text"])
    if not domains:
        context["error"] = "Введите хотя бы один домен (по одному в строке)."
        return context, None

    try:
        int(context["distance"])
    except ValueError:
        context["error"] = "Distance должен быть числом."
        return context, None

    if context["gateway"] not in AVAILABLE_GATEWAYS:
        context["error"] = "Gateway должен быть выбран из списка."
        return context, None

    if context["dns_provider"] not in DNS_PROVIDER_ENDPOINTS:
        context["error"] = "DNS provider должен быть выбран из списка."
        return context, None

    return context, domains


def execute_resolve(
    context: dict[str, Any],
    domains: list[str],
    progress_hook: Any = None,
) -> dict[str, Any]:
    ip_domain_map: dict[str, str] = {}
    ordered_ips: list[str] = []
    warnings: list[str] = []
    started_ts = datetime.now(timezone.utc).timestamp()
    total_domains = len(domains)

    for idx, domain in enumerate(domains, start=1):
        if callable(progress_hook):
            progress_hook(idx, total_domains, domain)

        elapsed = datetime.now(timezone.utc).timestamp() - started_ts
        if elapsed > RESOLVE_MAX_DURATION_SECONDS:
            warnings.append(
                "Достигнут лимит времени обработки списка доменов. "
                "Часть доменов не обработана, сократите список или повторите запрос."
            )
            break

        try:
            resolved_ips = resolve_ips(domain, context["dns_provider"])
        except Exception as exc:
            fallback_provider = "google"
            if context["dns_provider"] != fallback_provider and fallback_provider in DNS_PROVIDER_ENDPOINTS:
                try:
                    resolved_ips = resolve_ips(domain, fallback_provider)
                    source_label = DNS_PROVIDER_LABELS.get(context["dns_provider"], context["dns_provider"])
                    fallback_label = DNS_PROVIDER_LABELS.get(fallback_provider, fallback_provider)
                    warnings.append(
                        f"{domain}: {source_label} недоступен ({exc}). Использован {fallback_label}."
                    )
                except Exception as fallback_exc:
                    warnings.append(
                        f"{domain}: ошибка DNS-резолва через {context['dns_provider']} ({exc}); "
                        f"fallback {fallback_provider} также не сработал ({fallback_exc})"
                    )
                    continue
            else:
                warnings.append(f"{domain}: ошибка DNS-резолва ({exc})")
                continue

        for ip in resolved_ips:
            if ip not in ip_domain_map:
                ip_domain_map[ip] = domain
                ordered_ips.append(ip)

    context["ips"] = ordered_ips
    context["ip_entries"] = [
        {
            "ip": ip,
            "base_comment": ip_domain_map[ip],
            "comment": build_route_comment(context["comment_prefix"], ip_domain_map[ip]),
        }
        for ip in ordered_ips
    ]
    context["warnings"] = warnings

    if not ordered_ips:
        context["error"] = "Не удалось получить A-записи для введенных доменов."
        return context

    context["resolve_id"] = store_resolve_payload(
        domains_text=context["domains_text"],
        ips=ordered_ips,
        ip_domain_map=ip_domain_map,
    )
    return context


def cleanup_resolve_jobs() -> None:
    now_ts = datetime.now(timezone.utc).timestamp()
    with RESOLVE_JOBS_LOCK:
        expired_ids = [
            job_id
            for job_id, job in RESOLVE_JOBS.items()
            if now_ts - float(job.get("updated_at_ts", 0)) > RESOLVE_JOB_TTL_SECONDS
        ]
        for job_id in expired_ids:
            RESOLVE_JOBS.pop(job_id, None)


def run_resolve_job(job_id: str, domains: list[str]) -> None:
    def progress_hook(current: int, total: int, domain: str) -> None:
        with RESOLVE_JOBS_LOCK:
            job = RESOLVE_JOBS.get(job_id)
            if not job:
                return
            job["progress_current"] = current
            job["progress_total"] = total
            job["current_domain"] = domain
            job["updated_at_ts"] = datetime.now(timezone.utc).timestamp()

    with RESOLVE_JOBS_LOCK:
        job = RESOLVE_JOBS.get(job_id)
        if not job:
            return
        context = dict(job.get("context", {}))

    try:
        final_context = execute_resolve(context, domains, progress_hook=progress_hook)
        with RESOLVE_JOBS_LOCK:
            job = RESOLVE_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "done"
            job["done"] = True
            job["context"] = final_context
            job["current_domain"] = ""
            job["progress_current"] = len(domains)
            job["progress_total"] = len(domains)
            job["updated_at_ts"] = datetime.now(timezone.utc).timestamp()
    except Exception as exc:
        context["error"] = f"Ошибка резолва: {exc}"
        with RESOLVE_JOBS_LOCK:
            job = RESOLVE_JOBS.get(job_id)
            if not job:
                return
            job["status"] = "failed"
            job["done"] = True
            job["context"] = context
            job["current_domain"] = ""
            job["progress_current"] = len(domains)
            job["progress_total"] = len(domains)
            job["updated_at_ts"] = datetime.now(timezone.utc).timestamp()


@app.route("/", methods=["GET"])
def index():
    context = base_context()
    pending_context_id = session.pop(INDEX_CONTEXT_SESSION_KEY, "")
    pending_context = load_index_context_payload(str(pending_context_id))
    if isinstance(pending_context, dict):
        context.update(pending_context)
        # Always read latest audit log from disk on final render.
        context["audit_entries"] = read_audit_entries()
    return render_template("index.html", **context)


@app.route("/logs", methods=["GET"])
def logs():
    return render_template(
        "logs.html",
        audit_entries=read_audit_entries(500),
        message=request.args.get("message"),
        error=request.args.get("error"),
    )


@app.route("/resolve", methods=["POST"])
def resolve():
    context = base_context()
    context = hydrate_resolve_context_from_form(context, request.form)
    context, domains = validate_resolve_context(context)
    if domains is None:
        return redirect_with_index_context(context)

    context = execute_resolve(context, domains)
    return redirect_with_index_context(context)


@app.route("/resolve-start", methods=["POST"])
def resolve_start():
    cleanup_resolve_jobs()
    context = base_context()
    context = hydrate_resolve_context_from_form(context, request.form)
    context, domains = validate_resolve_context(context)
    if domains is None:
        return jsonify({"ok": False, "error": context.get("error")}), 400

    job_id = uuid.uuid4().hex
    now_ts = datetime.now(timezone.utc).timestamp()
    with RESOLVE_JOBS_LOCK:
        RESOLVE_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "done": False,
            "progress_current": 0,
            "progress_total": len(domains),
            "current_domain": "",
            "context": context,
            "updated_at_ts": now_ts,
        }

    worker = threading.Thread(target=run_resolve_job, args=(job_id, domains), daemon=True)
    worker.start()

    return jsonify({"ok": True, "job_id": job_id, "total": len(domains)})


@app.route("/resolve-progress/<job_id>", methods=["GET"])
def resolve_progress(job_id: str):
    cleanup_resolve_jobs()
    with RESOLVE_JOBS_LOCK:
        job = RESOLVE_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Задача резолва не найдена или устарела."}), 404

        return jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "status": job.get("status", "running"),
                "done": bool(job.get("done", False)),
                "progress_current": int(job.get("progress_current", 0)),
                "progress_total": int(job.get("progress_total", 0)),
                "current_domain": str(job.get("current_domain", "")),
            }
        )


@app.route("/resolve-finish/<job_id>", methods=["GET"])
def resolve_finish(job_id: str):
    cleanup_resolve_jobs()
    with RESOLVE_JOBS_LOCK:
        job = RESOLVE_JOBS.get(job_id)
        if not job:
            context = base_context()
            context["error"] = "Задача резолва не найдена или устарела. Выполните поиск заново."
            return redirect_with_index_context(context)

        if not bool(job.get("done", False)):
            context = base_context()
            context["error"] = "Резолв еще выполняется. Дождитесь завершения прогресса."
            return redirect_with_index_context(context)

        context = dict(job.get("context", {}))
        RESOLVE_JOBS.pop(job_id, None)

    return redirect_with_index_context(context)


@app.route("/add-routes", methods=["POST"])
def add_routes():
    context = base_context()
    context["gateway"] = request.form.get("gateway", UI_DEFAULT_GATEWAY).strip()
    context["dns_provider"] = request.form.get("dns_provider", UI_DEFAULT_DNS_PROVIDER).strip().lower()
    context["distance"] = request.form.get("distance", DEFAULT_DISTANCE).strip()
    context["comment_prefix"] = request.form.get("comment_prefix", DEFAULT_COMMENT_PREFIX).strip()
    resolve_id = request.form.get("resolve_id", "").strip()

    if not resolve_id:
        context["error"] = "Истекла сессия резолва. Выполните поиск IP заново."
        return redirect_with_index_context(context)

    payload = load_resolve_payload(resolve_id)
    if payload is None:
        context["error"] = "Данные резолва не найдены или устарели. Выполните поиск IP заново."
        return redirect_with_index_context(context)

    ips = [str(ip).strip() for ip in payload.get("ips", []) if str(ip).strip()]
    ip_domain_map = {
        str(ip).strip(): str(domain).strip()
        for ip, domain in payload.get("ip_domain_map", {}).items()
        if str(ip).strip() and str(domain).strip()
    }
    context["domains_text"] = str(payload.get("domains_text", "")).strip()
    context["ips"] = ips
    context["resolve_id"] = resolve_id
    context["ip_entries"] = [
        {
            "ip": ip,
            "base_comment": ip_domain_map.get(ip, ""),
            "comment": build_route_comment(context["comment_prefix"], ip_domain_map.get(ip, "")),
        }
        for ip in ips
    ]

    if not ips:
        context["error"] = "В выбранной сессии нет IP-адресов. Выполните поиск заново."
        return redirect_with_index_context(context)

    try:
        distance = int(context["distance"])
    except ValueError:
        context["error"] = "Distance должен быть числом."
        return redirect_with_index_context(context)

    if context["gateway"] not in AVAILABLE_GATEWAYS:
        context["error"] = "Gateway должен быть выбран из списка."
        return redirect_with_index_context(context)
    if context["dns_provider"] not in DNS_PROVIDER_ENDPOINTS:
        context["error"] = "DNS provider должен быть выбран из списка."
        return redirect_with_index_context(context)

    domains = parse_domains(context["domains_text"])
    fallback_comment = domains[0] if domains else "dns-resolver"

    client = MikroTikClient()
    source = get_request_source()

    try:
        existing_routes = client.get_routes(gateway=context["gateway"], distance=distance)

        added: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        for ip in ips:
            covering_route = find_covering_route(ip, existing_routes, distance)
            if covering_route:
                skipped.append(
                    {
                        "ip": ip,
                        "dst_address": str(covering_route.get("dst-address", "")).strip(),
                        "gateway": str(covering_route.get("gateway", "")).strip(),
                        "distance": str(covering_route.get("distance", "")).strip(),
                        "comment": str(covering_route.get("comment", "")).strip(),
                    }
                )
                continue

            comment = ip_domain_map.get(ip, fallback_comment)
            final_comment = build_route_comment(context["comment_prefix"], comment)
            dst_address = f"{ip}/32"
            client.add_route(
                dst_address=dst_address,
                gateway=context["gateway"],
                distance=distance,
                comment=final_comment,
            )

            exact_routes = client.find_exact_routes(
                dst_address=dst_address,
                gateway=context["gateway"],
                distance=distance,
                comment=final_comment,
            )
            route_id = None
            if exact_routes:
                route_id = MikroTikClient.extract_route_id(exact_routes[0])

            audit_entry = {
                "id": uuid.uuid4().hex,
                "created_at": utc_now_iso(),
                "action": "add_route",
                "status": "active",
                "source": source,
                "route": {
                    "route_id": route_id,
                    "dst_address": dst_address,
                    "gateway": context["gateway"],
                    "distance": distance,
                    "comment": final_comment,
                },
            }
            append_audit_entry(audit_entry)

            added.append({"ip": ip, "comment": final_comment})
            existing_routes.append({"dst-address": dst_address, "distance": str(distance)})

    except Exception as exc:
        context["error"] = f"Ошибка работы с MikroTik API: {exc}"
        context["audit_entries"] = read_audit_entries()
        return redirect_with_index_context(context)

    context["result"] = {
        "added": added,
        "skipped": skipped,
    }
    context["audit_entries"] = read_audit_entries()

    return redirect_with_index_context(context)


@app.route("/rollback", methods=["POST"])
def rollback_route():
    context = base_context()
    source = request.form.get("source", "").strip().lower()
    entry_ids = [value.strip() for value in request.form.getlist("entry_ids") if value.strip()]
    single_entry_id = request.form.get("entry_id", "").strip()
    if not entry_ids and single_entry_id:
        entry_ids = [single_entry_id]
    unique_entry_ids = list(dict.fromkeys(entry_ids))

    def fail(message: str):
        if source == "logs":
            return redirect(url_for("logs", error=message))
        context["audit_error"] = message
        return redirect_with_index_context(context)

    def ok(message: str):
        if source == "logs":
            return redirect(url_for("logs", message=message))
        context["audit_message"] = message
        context["audit_entries"] = read_audit_entries()
        return redirect_with_index_context(context)

    if not unique_entry_ids:
        return fail("Не выбраны записи для отката.")

    def rollback_one(client: MikroTikClient, entry_id: str) -> tuple[bool, str]:
        entry = find_audit_entry(entry_id)
        if entry is None:
            return False, "Запись аудита не найдена."

        if entry.get("status") == "rolled_back":
            return False, "Эта запись уже откатана ранее."

        route = entry.get("route", {})
        route_id = route.get("route_id")
        dst_address = str(route.get("dst_address", "")).strip()
        gateway = str(route.get("gateway", "")).strip()
        comment = str(route.get("comment", "")).strip()
        distance_raw = route.get("distance")

        try:
            distance = int(distance_raw)
        except (TypeError, ValueError):
            return False, "В записи аудита некорректный distance, откат невозможен."

        try:
            if route_id:
                client.remove_route(str(route_id))
            else:
                candidates = client.find_exact_routes(
                    dst_address=dst_address,
                    gateway=gateway,
                    distance=distance,
                    comment=comment,
                )
                if not candidates:
                    return False, "Маршрут для отката не найден в MikroTik."

                candidate_id = MikroTikClient.extract_route_id(candidates[0])
                if not candidate_id:
                    return False, "Не удалось определить route id для отката."

                client.remove_route(candidate_id)
                route_id = candidate_id
        except Exception as exc:
            return False, f"Ошибка отката маршрута: {exc}"

        updated = update_audit_entry(
            entry_id,
            {
                "status": "rolled_back",
                "rolled_back_at": utc_now_iso(),
                "rolled_back_route_id": str(route_id) if route_id else None,
            },
        )
        if not updated:
            return False, "Маршрут откатили, но запись аудита не удалось обновить."

        return True, "ok"

    client = MikroTikClient()
    rolled_back_count = 0
    failures: list[str] = []

    for entry_id in unique_entry_ids:
        success, reason = rollback_one(client, entry_id)
        if success:
            rolled_back_count += 1
        else:
            failures.append(f"{entry_id}: {reason}")

    if rolled_back_count == 0 and failures:
        return fail("Откат не выполнен.\n" + "\n".join(failures[:5]))

    if failures:
        summary = f"Откат выполнен частично. Успешно: {rolled_back_count}, ошибок: {len(failures)}."
        if source == "logs":
            return redirect(url_for("logs", message=summary, error="\n".join(failures[:5])))
        context["audit_message"] = summary
        context["audit_error"] = "\n".join(failures[:5])
        context["audit_entries"] = read_audit_entries()
        return redirect_with_index_context(context)

    return ok(f"Откат выполнен успешно. Записей: {rolled_back_count}.")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("APP_PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
