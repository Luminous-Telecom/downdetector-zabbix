#!/usr/bin/env python3
"""
Coleta do Downdetector BR: logo, nome, status (success/warning/danger) e o
ÚLTIMO ponto de relatos do gráfico de 24h (o mesmo valor que aparece na
tooltip do gráfico ao passar o mouse: "Relatos: X" / "Linha de base: Y").

Saída em JSON para uso no Zabbix (External check / UserParameter + JSONPath).

Exemplos:
  python3 downdetector_scraper.py --pretty
  python3 downdetector_scraper.py --service whatsapp --pretty
  python3 downdetector_scraper.py --service whatsapp --numeric
  python3 downdetector_scraper.py --key services.whatsapp.reports
  python3 downdetector_scraper.py --with-reports --pretty

Como funciona:
  A página individual de cada serviço (/fora-do-ar/<slug>/) embute no HTML
  um JSON server-side com o status atual e os pontos do gráfico de 24h em
  buckets de 15 minutos: [{"timestampUtc", "reportsValue", "baselineValue"}].
  O campo "reports" no JSON de saída é o "reportsValue" do ÚLTIMO bucket
  (o ponto mais recente do gráfico) — não é um contador ao vivo minuto a
  minuto (isso é exclusivo da API paga/Enterprise), mas é o valor mais
  recente e granular disponível publicamente. Esse dado só existe na
  página individual do serviço (--service slug), não na home. Use
  --with-reports para preenchê-lo para todos os serviços da home (mais
  lento, uma requisição extra por serviço).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from http.cookiejar import MozillaCookieJar
from typing import Any
from urllib.parse import urljoin

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

BASE_URL = "https://downdetector.com.br/"
SERVICE_URL_TEMPLATE = BASE_URL + "fora-do-ar/{slug}/"

# Ordem importa: "sem problemas" precisa ser checado antes de "problemas".
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

STROKE_COLOR = {
    "var(--color-dd-blue)": "success",
    "var(--color-dd-yellow)": "warning",
    "var(--color-dd-red)": "danger",
}

CARD_STATUS_RE = re.compile(r"Status atual:\s*(.+)$", re.IGNORECASE)

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
    if "card-company" in html or "fora do ar? Falhas e problemas" in html:
        return False

    markers = (
        "Just a moment",
        "Um momento",
        "Enable JavaScript and cookies",
        "cf-chl-opt",
    )
    return any(marker in html for marker in markers)


def load_cookie_jar(cookie_file: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(default_headers())

    if not cookie_file:
        return session

    if cookie_file.endswith((".txt", ".cookies")):
        jar = MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        return session

    with open(cookie_file, encoding="utf-8") as handle:
        raw = json.load(handle)

    if isinstance(raw, list):
        for item in raw:
            session.cookies.set(
                item.get("name", ""),
                item.get("value", ""),
                domain=item.get("domain"),
                path=item.get("path", "/"),
            )

    return session


def fetch_with_requests(url: str, timeout: int, cookie_file: str | None = None) -> str:
    if HAS_CLOUDSCRAPER:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "desktop": True}
        )
        scraper.headers.update(default_headers())
        response = scraper.get(url, timeout=timeout)
    else:
        session = load_cookie_jar(cookie_file)
        response = session.get(url, timeout=timeout)

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
    timeout: int = 30,
    *,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
    cookie_file: str | None = None,
) -> str:
    errors: list[str] = []
    fetchers = (
        ["requests", "curl_cffi", "flaresolverr"] if fetcher == "auto" else [fetcher]
    )

    for method in fetchers:
        try:
            if method == "requests":
                html = fetch_with_requests(url, timeout, cookie_file=cookie_file)
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

    hint = (
        "O Downdetector bloqueou o acesso automatizado (Cloudflare). "
        "Suba o FlareSolverr (docker-compose up -d) e use --fetcher flaresolverr, "
        "ou defina a variável FLARESOLVERR_URL."
    )
    raise RuntimeError(f"{hint} Tentativas: {'; '.join(errors)}")


def slug_from_href(href: str | None) -> str:
    if not href:
        return ""
    match = re.search(r"/fora-do-ar/([^/]+)/?", href)
    return match.group(1) if match else ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def status_from_text(text: str) -> str:
    normalized = normalize(text)
    for phrase, status in STATUS_FROM_TEXT:
        if phrase in normalized:
            return status
    return "unknown"


def extract_chart_data(html: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Extrai o status literal e os pontos do gráfico de 24h embutidos no HTML.

    A página individual do serviço traz um bloco server-side como:
      "stats":{"status":"success","chartData":{"dataPoints":[
        {"timestampUtc":"...","reportsValue":11,"baselineValue":9}, ...
      ]}}
    (com aspas escapadas por estar dentro de uma string JS). Retornamos o
    status (se encontrado) e a lista de pontos na ordem cronológica.
    """
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


def best_logo_src(img_tag) -> str:
    if not img_tag:
        return ""

    srcset = img_tag.get("srcset", "")
    if srcset:
        candidates = [c.strip().split(" ")[0] for c in srcset.split(",") if c.strip()]
        if candidates:
            return candidates[-1]

    return img_tag.get("src", "")


def parse_homepage(html: str) -> list[ServiceStatus]:
    soup = BeautifulSoup(html, "html.parser")
    services: list[ServiceStatus] = []

    for card in soup.select('[data-testid^="card-company-"]'):
        name_tag = card.select_one("h2")
        link_tag = card.select_one("a[href]")
        img_tag = card.select_one("img")
        chart_tag = card.select_one('[role="img"]')
        stroke_tag = card.select_one("path[stroke]")

        name = name_tag.get_text(strip=True) if name_tag else ""
        href = link_tag.get("href") if link_tag else ""
        slug = slug_from_href(href)
        url = urljoin(BASE_URL, href) if href else ""
        logo = best_logo_src(img_tag)

        status = "unknown"
        aria_label = chart_tag.get("aria-label") if chart_tag else None
        match = CARD_STATUS_RE.search(aria_label) if aria_label else None
        if match:
            status = status_from_text(match.group(1))

        if status == "unknown" and stroke_tag:
            status = STROKE_COLOR.get(stroke_tag.get("stroke", ""), "unknown")

        services.append(
            ServiceStatus(
                slug=slug,
                name=name,
                logo=logo,
                status=status,
                status_code=STATUS_CODE[status],
                reports=None,
                reports_baseline=None,
                reports_at=None,
                url=url,
            )
        )

    return services


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


def enrich_with_reports(
    services: list[ServiceStatus],
    *,
    fetcher: str,
    flaresolverr_url: str,
    cookie_file: str | None,
    delay: float,
    output_path: str | None = None,
) -> list[ServiceStatus]:
    """Busca relatos serviço a serviço.

    Se `output_path` for informado, grava o resultado de CADA serviço em uma
    linha do arquivo (JSON Lines) imediatamente após ser coletado, com flush
    e fsync — assim nada se perde se o processo for interrompido no meio, e
    dá para acompanhar o progresso lendo o arquivo ao vivo (ex.: `tail -f`).
    """
    fetch_kwargs = {
        "fetcher": fetcher,
        "flaresolverr_url": flaresolverr_url,
        "cookie_file": cookie_file,
    }

    pending = [s for s in services if s.slug]
    total = len(pending)

    output_handle = open(output_path, "w", encoding="utf-8") if output_path else None

    try:
        for done, service in enumerate(pending, start=1):
            try:
                html = fetch_html(SERVICE_URL_TEMPLATE.format(slug=service.slug), **fetch_kwargs)
                detail = parse_service_page(html, service.slug)
                service.reports = detail.reports
                service.reports_baseline = detail.reports_baseline
                service.reports_at = detail.reports_at
                if detail.logo:
                    service.logo = detail.logo
                if detail.status != "unknown":
                    service.status = detail.status
                    service.status_code = detail.status_code
            except Exception as exc:
                print(f"Aviso: falha ao coletar relatos de {service.slug}: {exc}", file=sys.stderr)

            print(
                f"[{done}/{total}] {service.slug}: status={service.status} "
                f"reports={service.reports}",
                file=sys.stderr,
                flush=True,
            )

            if output_handle:
                output_handle.write(json.dumps(asdict(service), ensure_ascii=False) + "\n")
                output_handle.flush()
                os.fsync(output_handle.fileno())

            if delay:
                time.sleep(delay)
    finally:
        if output_handle:
            output_handle.close()

    return services


def build_payload(services: list[ServiceStatus]) -> dict[str, Any]:
    warnings = [s for s in services if s.status == "warning"]
    successes = [s for s in services if s.status == "success"]
    dangers = [s for s in services if s.status == "danger"]

    return {
        "source": BASE_URL,
        "summary": {
            "total": len(services),
            "success": len(successes),
            "warning": len(warnings),
            "danger": len(dangers),
            "unknown": len(services) - len(successes) - len(warnings) - len(dangers),
        },
        "success": [asdict(item) for item in successes],
        "warning": [asdict(item) for item in warnings],
        "danger": [asdict(item) for item in dangers],
        "services": {item.slug: asdict(item) for item in services if item.slug},
    }


def get_json_path(payload: dict[str, Any], key: str) -> Any:
    current: Any = payload
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(key)
    return current


def filter_payload(payload: dict[str, Any], statuses: set[str]) -> dict[str, Any]:
    selected = [s for s in payload["services"].values() if s["status"] in statuses]

    return {
        "source": payload["source"],
        "summary": {
            "total": len(selected),
            "success": sum(1 for s in selected if s["status"] == "success"),
            "warning": sum(1 for s in selected if s["status"] == "warning"),
            "danger": sum(1 for s in selected if s["status"] == "danger"),
            "unknown": sum(1 for s in selected if s["status"] == "unknown"),
        },
        "success": [s for s in selected if s["status"] == "success"],
        "warning": [s for s in selected if s["status"] == "warning"],
        "danger": [s for s in selected if s["status"] == "danger"],
        "services": {s["slug"]: s for s in selected if s["slug"]},
    }


def collect_data(
    service: str | None = None,
    *,
    fetcher: str = "auto",
    flaresolverr_url: str = DEFAULT_FLARESOLVERR_URL,
    cookie_file: str | None = None,
    with_reports: bool = False,
    delay: float = 1.0,
    output_jsonl: str | None = None,
) -> dict[str, Any]:
    fetch_kwargs = {
        "fetcher": fetcher,
        "flaresolverr_url": flaresolverr_url,
        "cookie_file": cookie_file,
    }

    if service:
        html = fetch_html(SERVICE_URL_TEMPLATE.format(slug=service), **fetch_kwargs)
        services = [parse_service_page(html, service)]
    else:
        html = fetch_html(BASE_URL, **fetch_kwargs)
        services = parse_homepage(html)
        if not services:
            raise RuntimeError(
                "Nenhum serviço encontrado na página inicial. "
                "Verifique se o HTML mudou ou se o acesso foi bloqueado."
            )
        if with_reports:
            services = enrich_with_reports(
                services,
                fetcher=fetcher,
                flaresolverr_url=flaresolverr_url,
                cookie_file=cookie_file,
                delay=delay,
                output_path=output_jsonl,
            )

    return build_payload(services)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta logo, nome, status e relatos do Downdetector BR para Zabbix."
    )
    parser.add_argument(
        "--service",
        help="Slug do serviço (ex.: whatsapp, nubank, steam). Consulta página individual "
        "e já traz o número de relatos (pico nas últimas 24h).",
    )
    parser.add_argument(
        "--with-reports",
        action="store_true",
        help="Na listagem da home, busca também o último ponto de relatos (reports) "
        "de cada serviço (1 requisição extra por serviço, mais lento).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Segundos de espera entre requisições ao usar --with-reports (padrão: 1.0).",
    )
    parser.add_argument(
        "--output-jsonl",
        help="Com --with-reports, grava cada serviço em uma linha deste arquivo "
        "IMEDIATAMENTE após ser coletado (JSON Lines, com flush+fsync). "
        "Nada se perde se o processo for interrompido; dá para acompanhar "
        "com 'tail -f arquivo.jsonl'.",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Com --service, imprime só o objeto do serviço (sem o envelope "
        "summary/success/warning/danger/services). Deixa o JSONPath dos itens "
        "dependentes do Zabbix simples e fixo: $.status, $.reports, $.logo, etc.",
    )
    parser.add_argument(
        "--lld",
        action="store_true",
        help="Imprime a lista de serviços da home no formato de Low-Level Discovery "
        "do Zabbix ({\"data\":[{\"{#SLUG}\":...,\"{#NAME}\":...}, ...]}). Rápido, "
        "não busca relatos.",
    )
    parser.add_argument("--only", help="Filtra status na saída (ex.: warning,success).")
    parser.add_argument(
        "--key",
        help="Retorna apenas um valor do JSON (ex.: summary.warning ou services.whatsapp.reports).",
    )
    parser.add_argument(
        "--numeric",
        action="store_true",
        help="Com --service, imprime apenas o código numérico (1=success, 2=warning, 3=danger, 0=unknown).",
    )
    parser.add_argument("--pretty", action="store_true", help="JSON formatado com indentação.")
    parser.add_argument(
        "--fetcher",
        choices=["auto", "requests", "curl_cffi", "flaresolverr"],
        default="auto",
        help="Método de coleta. 'auto' tenta requests, curl_cffi e FlareSolverr, nessa ordem.",
    )
    parser.add_argument(
        "--flaresolverr-url",
        default=DEFAULT_FLARESOLVERR_URL,
        help="URL da API do FlareSolverr (padrão: FLARESOLVERR_URL ou http://localhost:8191/v1).",
    )
    parser.add_argument(
        "--cookie-file",
        help="Arquivo de cookies exportado do navegador (JSON ou Netscape).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        if args.lld:
            html = fetch_html(
                BASE_URL,
                fetcher=args.fetcher,
                flaresolverr_url=args.flaresolverr_url,
                cookie_file=args.cookie_file,
            )
            services = parse_homepage(html)
            lld = {
                "data": [
                    {"{#SLUG}": s.slug, "{#NAME}": s.name}
                    for s in services
                    if s.slug
                ]
            }
            print(json.dumps(lld, ensure_ascii=False))
            return 0

        payload = collect_data(
            service=args.service,
            fetcher=args.fetcher,
            flaresolverr_url=args.flaresolverr_url,
            cookie_file=args.cookie_file,
            with_reports=args.with_reports,
            delay=args.delay,
            output_jsonl=args.output_jsonl,
        )

        if args.only:
            statuses = {item.strip().lower() for item in args.only.split(",") if item.strip()}
            payload = filter_payload(payload, statuses)

        if args.flat:
            if not args.service:
                print("Use --flat com --service.", file=sys.stderr)
                return 1
            flat = payload["services"].get(args.service, {})
            print(json.dumps(flat, ensure_ascii=False))
            return 0

        if args.key:
            print(get_json_path(payload, args.key))
            return 0

        if args.numeric:
            if not args.service:
                print("Use --service com --numeric.", file=sys.stderr)
                print(0)
                return 1
            service_data = payload["services"].get(args.service)
            print(service_data["status_code"] if service_data else 0)
            return 0

        if args.pretty:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

        return 0

    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        if args.numeric:
            print(0)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
