#!/usr/bin/env python3
"""
Coleta um serviço do Downdetector BR para o Zabbix.

Modelo: cada *host* no Zabbix é um serviço (WhatsApp, Instagram, …).
O template usa a macro {$DOWNDETECTOR.SLUG} nesse host.

  python3 downdetector_scraper.py --service whatsapp --flat
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
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
DEFAULT_CACHE_TTL = int(os.environ.get("DOWNDETECTOR_CACHE_TTL", "900"))

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


def fetch_with_flaresolverr(url: str, timeout: int, flaresolverr_url: str) -> str:
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max(timeout, 60) * 1000,
    }
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


def fetch_html(
    url: str,
    timeout: int = 60,
    *,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
) -> str:
    errors: list[str] = []
    fetchers = (
        ["requests", "curl_cffi", "flaresolverr"] if fetcher == "auto" else [fetcher]
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
    """Lê cache se fresco; senão atualiza via FlareSolverr (com lock por slug).

    Assim o agent responde em ms na maior parte do tempo (Timeout máx. 30s
    no zabbix_agentd clássico). Vários hosts paralelos não disparam N scrapes.
    """
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
                    # Cache velho melhor que timeout no Zabbix
                    return read_cache_file(path)
                raise
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta um serviço do Downdetector BR para o Zabbix."
    )
    parser.add_argument(
        "--service",
        required=True,
        help="Slug do serviço (ex.: whatsapp, instagram, nubank).",
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
        help="Segundos para reutilizar o JSON em disco (padrão: 900). "
        "0 = sempre coletar ao vivo. Necessário porque o agent clássico "
        "limita Timeout a 30s e o FlareSolverr costuma passar disso.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.cache_ttl > 0:
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