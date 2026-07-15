#!/usr/bin/env python3
"""
Coleta serviços do Downdetector BR para o Zabbix.

O host define o que monitorar em services.txt. O template descobre a lista
(LLD) e coleta cada slug via agent.

  python3 downdetector_scraper.py --lld
  python3 downdetector_scraper.py --service whatsapp --flat
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
DEFAULT_SERVICES_FILE = os.environ.get(
    "DOWNDETECTOR_SERVICES_FILE",
    "/opt/downdetector-zabbix/services.txt",
)

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


def read_services_file(path: str) -> list[tuple[str, str]]:
    """Lê a lista de serviços do host.

    Formato (um por linha):
      whatsapp
      nubank|Nubank
      # comentário
    """
    entries: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                slug, name = line.split("|", 1)
            else:
                slug, name = line, line
            slug = slug.strip()
            name = name.strip() or slug
            validate_slug(slug)
            entries.append((slug, name))
    return entries


def build_lld(entries: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "data": [
            {"{#SLUG}": slug, "{#NAME}": name} for slug, name in entries
        ]
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta serviços do Downdetector BR para o Zabbix."
    )
    parser.add_argument(
        "--service",
        help="Slug do serviço (ex.: whatsapp). Obrigatório sem --lld.",
    )
    parser.add_argument(
        "--lld",
        action="store_true",
        help="Imprime LLD JSON a partir do arquivo de serviços do host "
        "(não acessa a internet).",
    )
    parser.add_argument(
        "--services-file",
        default=DEFAULT_SERVICES_FILE,
        help=f"Lista de slugs do host (padrão: {DEFAULT_SERVICES_FILE}).",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Mantido por compatibilidade com o UserParameter do agent.",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.lld:
        try:
            entries = read_services_file(args.services_file)
        except FileNotFoundError:
            print(
                f"erro: arquivo de serviços não encontrado: {args.services_file}\n"
                "Crie o arquivo (veja services.txt.example) com um slug por linha.",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(build_lld(entries), ensure_ascii=False, separators=(",", ":")))
        return 0

    if not args.service:
        print("erro: use --service SLUG  ou  --lld", file=sys.stderr)
        return 1

    try:
        service = fetch_service(
            args.service,
            fetcher=args.fetcher,
            flaresolverr_url=args.flaresolverr_url,
        )
    except Exception as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1

    if args.numeric:
        print(service.status_code)
        return 0

    print(
        json.dumps(
            asdict(service),
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
