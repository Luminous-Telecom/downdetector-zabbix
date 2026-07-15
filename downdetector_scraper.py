#!/usr/bin/env python3
"""
Coleta um serviço do Downdetector BR para o Zabbix.

Cada host no Zabbix web = um serviço (Host name = slug).
Agent: --from-cache. Timer: --refresh-all (lista hosts via API do Zabbix).

  python3 downdetector_scraper.py --service whatsapp --from-cache --flat
  python3 downdetector_scraper.py --refresh-all
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper

    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

DEFAULT_FLARESOLVERR_URL = os.environ.get(
    "FLARESOLVERR_URL", "http://localhost:8191/v1"
)
DEFAULT_CACHE_DIR = os.environ.get(
    "DOWNDETECTOR_CACHE_DIR", "/var/cache/downdetector-zabbix"
)
DEFAULT_CACHE_TTL = int(os.environ.get("DOWNDETECTOR_CACHE_TTL", "300"))
BASE_URL = "https://downdetector.com.br/"
SERVICE_URL_TEMPLATE = BASE_URL + "fora-do-ar/{slug}/"
SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,80}$")

STATUS_FROM_TEXT = [
    ("sem problemas", "success"),
    ("possíveis problemas", "warning"),
    ("problemas", "danger"),
]

STATUS_CODE = {
    "success": 1,
    "warning": 2,
    "danger": 3,
    "unknown": 0,
}

STATS_STATUS_RE = re.compile(r'\\"status\\":\\"(success|warning|danger)\\"')
DATA_POINTS_KEY = '\\"dataPoints\\":'


@dataclass
class ServiceStatus:
    slug: str
    name: str
    logo: str
    status: str
    status_code: int
    reports: int | None
    reports_baseline: int | None
    reports_at: str | None
    url: str


def default_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def is_cloudflare_challenge(html: str) -> bool:
    if "dataPoints" in html or "fora do ar?" in html.lower():
        return False
    markers = (
        "Just a moment",
        "Um momento",
        "Enable JavaScript and cookies",
        "cf-chl-opt",
    )
    return any(marker in html for marker in markers)


def fetch_with_requests(url: str, timeout: int) -> str:
    if HAS_CLOUDSCRAPER:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "desktop": True}
        )
        scraper.headers.update(default_headers())
        response = scraper.get(url, timeout=timeout)
    else:
        response = requests.get(url, timeout=timeout, headers=default_headers())

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.text


def fetch_with_curl_cffi(url: str, timeout: int) -> str:
    if not HAS_CURL_CFFI:
        raise RuntimeError("curl_cffi não instalado")
    response = curl_requests.get(
        url, impersonate="chrome131", timeout=timeout, headers=default_headers()
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.text


def _flaresolverr_request(
    flaresolverr_url: str,
    url: str,
    timeout: int,
    *,
    session_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max(timeout, 60) * 1000,
    }
    if session_id:
        payload["session"] = session_id
    response = requests.post(flaresolverr_url, json=payload, timeout=max(timeout, 120))
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ok":
        raise RuntimeError(data.get("message", "erro desconhecido no FlareSolverr"))
    solution = data.get("solution", {})
    status_code = solution.get("status")
    html = solution.get("response", "")
    if status_code != 200:
        raise RuntimeError(f"HTTP {status_code}")
    if not html:
        raise RuntimeError("resposta vazia do FlareSolverr")
    return html


class FlareSolverrPool:
    """Várias sessões quentes do FlareSolverr para scrape paralelo."""

    def __init__(self, api_url: str, size: int = 4) -> None:
        self.api_url = api_url
        self.size = max(1, size)
        self._sessions: list[str] = []
        self._free: queue.Queue[str] = queue.Queue()

    def __enter__(self) -> "FlareSolverrPool":
        print(
            f"FlareSolverr: criando {self.size} sessão(ões)...",
            file=sys.stderr,
            flush=True,
        )
        for _ in range(self.size):
            response = requests.post(
                self.api_url, json={"cmd": "sessions.create"}, timeout=30
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok" or not data.get("session"):
                raise RuntimeError(
                    data.get("message", "falha ao criar sessão no FlareSolverr")
                )
            self._sessions.append(data["session"])

        def warm(sid: str) -> None:
            _flaresolverr_request(self.api_url, BASE_URL, 120, session_id=sid)

        with ThreadPoolExecutor(max_workers=self.size) as executor:
            list(executor.map(warm, self._sessions))
        for sid in self._sessions:
            self._free.put(sid)
        print("FlareSolverr: sessões aquecidas.", file=sys.stderr, flush=True)
        return self

    def __exit__(self, *exc: object) -> None:
        for sid in self._sessions:
            try:
                requests.post(
                    self.api_url,
                    json={"cmd": "sessions.destroy", "session": sid},
                    timeout=30,
                )
            except Exception:
                pass
        self._sessions.clear()

    def fetch(self, url: str, timeout: int = 60) -> str:
        sid = self._free.get()
        try:
            return _flaresolverr_request(
                self.api_url, url, timeout, session_id=sid
            )
        finally:
            self._free.put(sid)


_fs_pool: FlareSolverrPool | None = None
_fs_pool_lock = threading.Lock()


def fetch_with_flaresolverr(url: str, timeout: int, flaresolverr_url: str) -> str:
    with _fs_pool_lock:
        pool = _fs_pool
    if pool is not None:
        return pool.fetch(url, timeout)
    return _flaresolverr_request(flaresolverr_url, url, timeout)


def fetch_html(
    url: str,
    timeout: int = 60,
    *,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
) -> str:
    errors: list[str] = []
    if _fs_pool is not None and fetcher in ("auto", "flaresolverr"):
        fetchers = ["flaresolverr"]
    else:
        fetchers = (
            ["requests", "curl_cffi", "flaresolverr"]
            if fetcher == "auto"
            else [fetcher]
        )

    for method in fetchers:
        try:
            if method == "requests":
                html = fetch_with_requests(url, timeout)
            elif method == "curl_cffi":
                html = fetch_with_curl_cffi(url, timeout)
            elif method == "flaresolverr":
                html = fetch_with_flaresolverr(url, timeout, flaresolverr_url)
            else:
                raise RuntimeError(f"Fetcher inválido: {method}")

            if is_cloudflare_challenge(html):
                raise RuntimeError("página de desafio Cloudflare")
            return html
        except Exception as exc:
            errors.append(f"{method}: {exc}")

    raise RuntimeError(
        "Downdetector bloqueado (Cloudflare). Suba o FlareSolverr "
        f"(docker compose up -d). Tentativas: {'; '.join(errors)}"
    )


def validate_slug(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug inválido: {slug!r}")
    return slug


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def status_from_text(text: str) -> str:
    normalized = normalize(text)
    for phrase, status in STATUS_FROM_TEXT:
        if phrase in normalized:
            return status
    return "unknown"


def extract_chart_data(html: str) -> tuple[str | None, list[dict[str, Any]]]:
    status_match = STATS_STATUS_RE.search(html)
    status = status_match.group(1) if status_match else None

    key_idx = html.find(DATA_POINTS_KEY)
    if key_idx == -1:
        return status, []

    start = key_idx + len(DATA_POINTS_KEY)
    depth = 0
    end = start
    for i in range(start, len(html)):
        char = html[i]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    raw = html[start:end].replace('\\"', '"')
    try:
        points = json.loads(raw)
    except json.JSONDecodeError:
        return status, []
    return status, points


def parse_service_page(html: str, slug: str) -> ServiceStatus:
    soup = BeautifulSoup(html, "html.parser")

    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")

    name = slug
    if og_title and og_title.get("content"):
        name = og_title["content"].split(" fora do ar?")[0].strip() or slug

    logo = og_image.get("content", "") if og_image else ""

    embedded_status, data_points = extract_chart_data(html)

    reports: int | None = None
    reports_baseline: int | None = None
    reports_at: str | None = None
    if data_points:
        last_point = data_points[-1]
        reports = last_point.get("reportsValue")
        reports_baseline = last_point.get("baselineValue")
        reports_at = last_point.get("timestampUtc")

    status = embedded_status or "unknown"

    if status == "unknown":
        for chart in soup.select('[role="img"]'):
            aria_label = chart.get("aria-label", "")
            match = re.search(r"status:\s*(.+)$", aria_label, re.IGNORECASE)
            if match:
                status = status_from_text(match.group(1))
                break

    if status == "unknown":
        heading = soup.select_one("h1")
        if heading:
            status = status_from_text(heading.get_text(strip=True))

    return ServiceStatus(
        slug=slug,
        name=name,
        logo=logo,
        status=status,
        status_code=STATUS_CODE[status],
        reports=reports,
        reports_baseline=reports_baseline,
        reports_at=reports_at,
        url=SERVICE_URL_TEMPLATE.format(slug=slug),
    )


def fetch_service(
    slug: str,
    *,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
) -> ServiceStatus:
    slug = validate_slug(slug)
    html = fetch_html(
        SERVICE_URL_TEMPLATE.format(slug=slug),
        fetcher=fetcher,
        flaresolverr_url=flaresolverr_url,
    )
    return parse_service_page(html, slug)


def cache_path(cache_dir: str, slug: str) -> str:
    return os.path.join(cache_dir, f"{slug}.json")


def read_cache_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_cache_file(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def cache_is_fresh(path: str, ttl: int) -> bool:
    if ttl <= 0 or not os.path.isfile(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < ttl


def get_service_cached(
    slug: str,
    *,
    cache_dir: str,
    ttl: int,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
) -> dict[str, Any]:
    """Lê cache se fresco; senão atualiza via FlareSolverr (com lock por slug)."""
    slug = validate_slug(slug)
    path = cache_path(cache_dir, slug)
    lock_path = path + ".lock"

    if cache_is_fresh(path, ttl):
        return read_cache_file(path)

    os.makedirs(cache_dir, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if cache_is_fresh(path, ttl):
                return read_cache_file(path)
            try:
                service = fetch_service(
                    slug, fetcher=fetcher, flaresolverr_url=flaresolverr_url
                )
                payload = asdict(service)
                write_cache_file(path, payload)
                return payload
            except Exception:
                if os.path.isfile(path):
                    return read_cache_file(path)
                raise
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def read_from_cache(cache_dir: str, slug: str) -> dict[str, Any]:
    """Só lê disco — nunca chama FlareSolverr (modo Zabbix Agent)."""
    slug = validate_slug(slug)
    path = cache_path(cache_dir, slug)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"cache ausente para {slug!r} ({path}). "
            "Espere o timer downdetector-refresh ou rode: "
            "systemctl start downdetector-refresh.service"
        )
    return read_cache_file(path)


def discover_cached_slugs(cache_dir: str) -> list[str]:
    if not os.path.isdir(cache_dir):
        return []
    slugs: list[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith(".json"):
            continue
        if name in ("lld.json",) or name.endswith(".tmp"):
            continue
        slug = name[: -len(".json")]
        if SLUG_RE.match(slug) and slug not in slugs:
            slugs.append(slug)
    return slugs


def zabbix_api(
    url: str,
    method: str,
    params: dict[str, Any],
    *,
    token: str | None = None,
    auth: str | None = None,
) -> Any:
    """Chamada JSON-RPC na API Zabbix.

    Tokens de API vão no campo ``auth`` do body (mais confiável que Bearer:
    proxies/PHP-FPM muitas vezes engolem o header Authorization e a API
    responde "Session terminated").
    """
    headers = {"Content-Type": "application/json-rpc"}
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }

    cred = (token or auth or "").strip()
    if method != "user.login" and cred:
        payload["auth"] = cred

    response = requests.post(url, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        err = data["error"]
        detail = f"{err.get('message')} — {err.get('data')}"
        if "Session terminated" in detail or "Not authorized" in detail:
            detail += (
                " | Confira ZABBIX_TOKEN em /etc/zabbix/downdetector-api.env "
                "(token ativo, sem aspas; URL correta api_jsonrpc.php)."
            )
        raise RuntimeError(f"Zabbix API {method}: {detail}")
    return data.get("result")


def discover_zabbix_slugs(
    url: str,
    *,
    token: str | None = None,
    user: str | None = None,
    password: str | None = None,
    template_name: str = "downdetector",
) -> list[str]:
    """Hosts ativos com o template downdetector (Host name = slug)."""
    url = (url or "").strip()
    token = (token or "").strip() or None
    user = (user or "").strip() or None
    password = (password or "").strip() or None

    if not url:
        raise RuntimeError("ZABBIX_URL vazio.")

    placeholders = {
        "",
        "COLE_O_TOKEN_AQUI",
        "cole_aqui_o_token_gerado_no_zabbix",
        "changeme",
        "YOUR_TOKEN",
    }
    if token in placeholders:
        token = None

    auth: str | None = None
    if token:
        auth_token = token
    else:
        if not user or not password:
            raise RuntimeError(
                "Configure ZABBIX_TOKEN em /etc/zabbix/downdetector-api.env "
                "(Users → API tokens no Zabbix web), "
                "ou ZABBIX_USER + ZABBIX_PASSWORD."
            )
        auth_token = None
        auth = zabbix_api(
            url,
            "user.login",
            {"username": user, "password": password},
        )
        if not isinstance(auth, str):
            # Zabbix antigo usava "user" em vez de "username"
            auth = zabbix_api(
                url,
                "user.login",
                {"user": user, "password": password},
            )
        if not isinstance(auth, str):
            raise RuntimeError("Falha no user.login da API Zabbix.")
        auth_token = auth

    templates = zabbix_api(
        url,
        "template.get",
        {"output": ["templateid", "host", "name"], "filter": {"host": [template_name]}},
        token=auth_token,
    )
    if not templates:
        templates = zabbix_api(
            url,
            "template.get",
            {
                "output": ["templateid", "host", "name"],
                "filter": {"name": [template_name]},
            },
            token=auth_token,
        )
    if not templates:
        raise RuntimeError(
            f"Template {template_name!r} não encontrado na API Zabbix."
        )

    hosts = zabbix_api(
        url,
        "host.get",
        {
            "output": ["host", "name", "status"],
            "templateids": [templates[0]["templateid"]],
            "filter": {"status": 0},
        },
        token=auth_token,
    )

    slugs: list[str] = []
    for host in hosts or []:
        slug = (host.get("host") or "").strip()
        if SLUG_RE.match(slug) and slug not in slugs:
            slugs.append(slug)
    return slugs


def resolve_refresh_slugs(
    *,
    cache_dir: str,
    zabbix_url: str | None,
    zabbix_token: str | None,
    zabbix_user: str | None,
    zabbix_password: str | None,
    template_name: str,
) -> list[str]:
    ordered: list[str] = []

    if zabbix_url and (zabbix_token or (zabbix_user and zabbix_password)):
        api_slugs = discover_zabbix_slugs(
            zabbix_url,
            token=zabbix_token,
            user=zabbix_user,
            password=zabbix_password,
            template_name=template_name,
        )
        print(
            f"API Zabbix: {len(api_slugs)} host(s) com template {template_name!r}",
            file=sys.stderr,
            flush=True,
        )
        ordered.extend(api_slugs)
    else:
        print(
            "AVISO: ZABBIX_URL/TOKEN não configurados — "
            "usando só arquivos já existentes no cache.",
            file=sys.stderr,
            flush=True,
        )

    for slug in discover_cached_slugs(cache_dir):
        if slug not in ordered:
            ordered.append(slug)
    return ordered


def refresh_all(
    *,
    cache_dir: str,
    fetcher: str,
    flaresolverr_url: str,
    delay: float,
    workers: int,
    zabbix_url: str | None,
    zabbix_token: str | None,
    zabbix_user: str | None,
    zabbix_password: str | None,
    template_name: str,
) -> int:
    """Atualiza caches em paralelo (FlareSolverr sessions). Lista via API."""
    global _fs_pool

    ordered = resolve_refresh_slugs(
        cache_dir=cache_dir,
        zabbix_url=zabbix_url,
        zabbix_token=zabbix_token,
        zabbix_user=zabbix_user,
        zabbix_password=zabbix_password,
        template_name=template_name,
    )

    if not ordered:
        print(
            "Nenhum host para atualizar. Cadastre hosts no Zabbix web "
            f"com o template {template_name!r} (Host name = slug) e "
            "configure /etc/zabbix/downdetector-api.env",
            file=sys.stderr,
        )
        return 1

    workers = max(1, workers)
    total = len(ordered)
    ok = 0
    fail = 0
    done = 0
    done_lock = threading.Lock()

    use_pool = fetcher in ("auto", "flaresolverr") and workers >= 1
    pool: FlareSolverrPool | None = None
    if use_pool:
        pool = FlareSolverrPool(flaresolverr_url, size=workers)
        pool.__enter__()
        with _fs_pool_lock:
            _fs_pool = pool
        fetcher = "flaresolverr"

    def one(slug: str) -> tuple[str, bool, str]:
        nonlocal done
        try:
            service = fetch_service(
                slug, fetcher=fetcher, flaresolverr_url=flaresolverr_url
            )
            write_cache_file(cache_path(cache_dir, slug), asdict(service))
            msg = f"{service.status} reports={service.reports}"
            success = True
        except Exception as exc:
            msg = f"ERRO {exc}"
            success = False
        if delay > 0:
            time.sleep(delay)
        with done_lock:
            done += 1
            print(f"[{done}/{total}] {slug}: {msg}", file=sys.stderr, flush=True)
        return slug, success, msg

    try:
        if workers <= 1:
            for slug in ordered:
                _, success, _ = one(slug)
                if success:
                    ok += 1
                else:
                    fail += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(one, slug) for slug in ordered]
                for fut in as_completed(futures):
                    _, success, _ = fut.result()
                    if success:
                        ok += 1
                    else:
                        fail += 1
    finally:
        with _fs_pool_lock:
            _fs_pool = None
        if pool is not None:
            pool.__exit__(None, None, None)

    print(
        json.dumps(
            {
                "updated": ok,
                "failed": fail,
                "total": total,
                "workers": workers,
                "cache_dir": cache_dir,
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta um serviço do Downdetector BR para o Zabbix."
    )
    parser.add_argument(
        "--service",
        help="Slug do serviço (ex.: whatsapp, caixa, banco-inter).",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Só lê o cache em disco (modo Agent). Nunca chama FlareSolverr.",
    )
    parser.add_argument(
        "--refresh-all",
        action="store_true",
        help="Atualiza em paralelo os hosts do template via API Zabbix. "
        "Use no systemd timer — NÃO no UserParameter.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Pausa extra por worker após cada scrape (padrão: 0).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("DOWNDETECTOR_WORKERS", "4")),
        help="Sessões FlareSolverr em paralelo no --refresh-all (padrão: 4).",
    )
    parser.add_argument(
        "--zabbix-url",
        default=os.environ.get("ZABBIX_URL"),
        help="URL da API (ex.: http://127.0.0.1/api_jsonrpc.php). Env ZABBIX_URL.",
    )
    parser.add_argument(
        "--zabbix-token",
        default=os.environ.get("ZABBIX_TOKEN"),
        help="API token (Users → API tokens). Env ZABBIX_TOKEN.",
    )
    parser.add_argument(
        "--zabbix-user",
        default=os.environ.get("ZABBIX_USER"),
        help="Usuário API (alternativa ao token). Env ZABBIX_USER.",
    )
    parser.add_argument(
        "--zabbix-password",
        default=os.environ.get("ZABBIX_PASSWORD"),
        help="Senha API. Env ZABBIX_PASSWORD.",
    )
    parser.add_argument(
        "--zabbix-template",
        default=os.environ.get("ZABBIX_TEMPLATE", "downdetector"),
        help="Nome técnico do template (padrão: downdetector).",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Mantido por compatibilidade com o UserParameter.",
    )
    parser.add_argument(
        "--numeric",
        action="store_true",
        help="Imprime só o status_code (1=ok, 2=warning, 3=danger, 0=unknown).",
    )
    parser.add_argument("--pretty", action="store_true", help="JSON indentado.")
    parser.add_argument(
        "--fetcher",
        choices=["auto", "requests", "curl_cffi", "flaresolverr"],
        default="auto",
    )
    parser.add_argument(
        "--flaresolverr-url",
        default=DEFAULT_FLARESOLVERR_URL,
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"Diretório de cache por slug (padrão: {DEFAULT_CACHE_DIR}).",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help="Com --service sem --from-cache: reutiliza disco se fresco "
        "(padrão: 300). 0 = sempre ao vivo.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.refresh_all:
        return refresh_all(
            cache_dir=args.cache_dir,
            fetcher=args.fetcher,
            flaresolverr_url=args.flaresolverr_url,
            delay=args.delay,
            workers=args.workers,
            zabbix_url=args.zabbix_url,
            zabbix_token=args.zabbix_token,
            zabbix_user=args.zabbix_user,
            zabbix_password=args.zabbix_password,
            template_name=args.zabbix_template,
        )

    if not args.service:
        print("erro: use --service SLUG  ou  --refresh-all", file=sys.stderr)
        return 1

    try:
        if args.from_cache:
            payload = read_from_cache(args.cache_dir, args.service)
        elif args.cache_ttl > 0:
            payload = get_service_cached(
                args.service,
                cache_dir=args.cache_dir,
                ttl=args.cache_ttl,
                fetcher=args.fetcher,
                flaresolverr_url=args.flaresolverr_url,
            )
        else:
            payload = asdict(
                fetch_service(
                    args.service,
                    fetcher=args.fetcher,
                    flaresolverr_url=args.flaresolverr_url,
                )
            )
    except Exception as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1

    if args.numeric:
        print(payload.get("status_code", 0))
        return 0

    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
